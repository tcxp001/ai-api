"""Authentication and private-file primitives for the local management UI."""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


PASSWORD_ITERATIONS = 600_000


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("密码必须是字符串")


def _basic_credentials(value: str) -> tuple[str, str] | None:
    if len(value) > 1024:
        return None
    scheme, _, encoded = value.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        credentials = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        return None
    username, separator, password = credentials.partition(":")
    if username != "admin" or not separator:
        return None
    return username, password


def _password_record(password: str) -> dict:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return {"username": "admin", "algorithm": "pbkdf2-sha256", "iterations": PASSWORD_ITERATIONS,
            "salt": salt.hex(), "digest": digest.hex()}


def _private_fd(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("管理密码文件必须是独立的普通文件，不能是链接")
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_password_record(path: Path) -> dict:
    with os.fdopen(_private_fd(path, os.O_RDONLY), "r", encoding="utf-8") as handle:
        try:
            record = json.loads(handle.read(4097))
            if (not isinstance(record, dict) or record.get("username") != "admin"
                    or record.get("algorithm") != "pbkdf2-sha256"
                    or record.get("iterations") != PASSWORD_ITERATIONS
                    or len(record.get("salt", "")) != 32 or len(bytes.fromhex(record["salt"])) != 16
                    or len(record.get("digest", "")) != 64 or len(bytes.fromhex(record["digest"])) != 32):
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError("管理密码文件格式无效，请在服务器上用 --set-password 重新设置") from None
    return record


@contextmanager
def _password_update_lock(path: Path):
    # A stable lock file also serializes a local CLI reset with a web change.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("管理密码目录不能是符号链接")
    with os.fdopen(_private_fd(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT), "r+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
            raise ValueError("管理密码文件不能是链接或特殊文件")
        yield


def set_password(path: Path, password: str) -> None:
    """Local administrator reset; never generate or store a plaintext password."""
    record = _password_record(password)
    path = Path(path)
    with _password_update_lock(path):
        write_private_file(path, (json.dumps(record) + "\n").encode("utf-8"))


def _matches_password(password: str, record: dict) -> bool:
    if not isinstance(password, str):
        return False
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                   bytes.fromhex(record["salt"]), record["iterations"])
    except UnicodeError:
        return False
    return hmac.compare_digest(digest.hex(), record["digest"])


class PasswordAuth:
    """Reload atomic credential changes on every request; fail closed on bad files."""

    def __init__(self, path: Path):
        self.path = Path(path)
        _read_password_record(self.path)
        self._lock = threading.Lock()
        self._cached_record = None
        self._cached_credentials = None

    def authenticate(self, value: str) -> bool:
        credentials = _basic_credentials(value)
        return credentials is not None and self.verify_password(credentials[1])

    def verify_password(self, password: str) -> bool:
        with self._lock:
            record = _read_password_record(self.path)
            if not isinstance(password, str):
                return False
            digest = hashlib.sha256(f"admin:{password}".encode("utf-8")).digest()
            # Cache only one successful login in memory, bound to the on-disk
            # salted verifier. Wrong passwords never populate the cache.
            if (record == self._cached_record and self._cached_credentials is not None
                    and hmac.compare_digest(digest, self._cached_credentials)):
                return True
            if not _matches_password(password, record):
                return False
            self._cached_record = record
            self._cached_credentials = digest
            return True

    def change_password(self, current: str, new: str) -> bool:
        validate_password(new)
        with self._lock, _password_update_lock(self.path):
            record = _read_password_record(self.path)
            if not _matches_password(current, record):
                return False
            replacement = _password_record(new)
            write_private_file(self.path, (json.dumps(replacement) + "\n").encode("utf-8"))
            self._cached_record = self._cached_credentials = None
            return True


def allowed_browser_origin(headers) -> bool:
    if str(headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return False
    origin = headers.get("Origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            return (
                parsed.scheme in {"http", "https"}
                and parsed.netloc.lower() == str(headers.get("Host") or "").lower()
                and not parsed.username and not parsed.password
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            )
        except ValueError:
            return False
    return True


def private_directories(directory: Path, root: Path) -> None:
    """Make only the explicitly managed subtree private, never its ancestors."""
    root = Path(root)
    directory = Path(directory)
    relative = directory.relative_to(root)
    current = root
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
        if current.is_symlink():
            raise ValueError("private storage must not traverse symlinks")
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)


def write_private_file(path: Path, data: bytes) -> None:
    """Atomic mode-0600 publication, including replacement of an older file."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("private file destination must not be a symlink")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
