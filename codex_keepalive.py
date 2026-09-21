"""Isolated interactive Codex sessions for the opt-in keepalive backend.

The PTY lifecycle and role-aware reply rules follow /mnt/test/atry/main.go
(SHA-256 2ccf86c30513f3b825329dfaa95d9ced35e33c1e8bc76beaa778e508cbfffde4).
No user's CODEX_HOME, auth.json, conversation, hooks, or workspace is copied.
"""

from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid

from secret_utils import provider_secrets, redact_text


API_KEY_ENV = "AIPROXY_KEEPALIVE_API_KEY"
OUTPUT_LIMIT = 256 * 1024
SETTLE_DELAY = 0.25
INPUT_DELAY = 0.5
_CSI = re.compile(r"\x1b\[([0-?]*[ -/]*)([@-~])")
_OSC = re.compile(r"\x1b[\]P^_].*?(?:\x07|\x1b\\)", re.S)
_BOUNDARY = frozenset("ABCDEFGHJKSTdfsu")
_IGNORED = (
    "codex", "openai", "update available", "npm install -g", "release notes",
    "model:", "directory:", "/model", "gpt-", "explain this codebase", "tip:",
    "working", "esc to interrupt", "retrying in ", "reconnecting",
    "stream disconnected", "sse stream error", "shutting down", "token usage:",
    "to continue this session", "conversation", "cybersecurity risk",
    "responses may take longer", "trusted access", "chatgpt.com/cyber",
)
# The small exec wrapper acquires the controlling PTY after setsid(), without
# using preexec_fn (unsafe when Popen is called from the proxy's worker threads).
_PTY_EXEC = (
    "import fcntl,os,sys,termios;"
    "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
    "os.execv(sys.argv[1],sys.argv[1:])"
)


class CodexConfigurationError(ValueError):
    pass


def build_config(provider: dict, model: str, effort: str, workspace: Path) -> str:
    """Create only the selected provider/model configuration, not a user clone."""
    if provider.get("api_mode") not in {"codex_responses", "responses"}:
        raise CodexConfigurationError("codex_cli keepalive requires native Responses mode")
    if provider.get("auth_mode", "bearer") != "bearer":
        raise CodexConfigurationError("codex_cli keepalive requires bearer authentication")
    if provider.get("remove_headers"):
        raise CodexConfigurationError("codex_cli keepalive does not support remove_headers")
    if not model:
        raise CodexConfigurationError("provider has no model configured")
    if not str(provider.get("api_key") or "").strip():
        raise CodexConfigurationError("codex_cli keepalive requires the provider API key")
    base_url = str(provider.get("base_url") or "").rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise CodexConfigurationError("provider base_url must be HTTP(S)")
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    lines = [
        f"model = {quote(model)}",
        'model_provider = "keepalive_upstream"',
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
        "check_for_update_on_startup = false",
        'cli_auth_credentials_store = "ephemeral"',
    ]
    if effort:
        # Preserve the provider's value. An unsupported value must produce a
        # visible configuration error, never silently fall back to medium.
        lines.append(f"model_reasoning_effort = {quote(effort)}")
    lines += [
        "",
        "[features]",
        "shell_tool = false",
        "unified_exec = false",
        "",
        f"[projects.{quote(workspace)}]",
        'trust_level = "trusted"',
        "",
        "[model_providers.keepalive_upstream]",
        'name = "Keepalive upstream"',
        f"base_url = {quote(base_url)}",
        f"env_key = {quote(API_KEY_ENV)}",
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "request_max_retries = 0",
        "stream_max_retries = 0",
    ]
    headers = provider.get("headers") or {}
    if headers:
        lines += ["", "[model_providers.keepalive_upstream.http_headers]"]
        lines += [f"{quote(key)} = {quote(value)}" for key, value in headers.items()]
    return "\n".join(lines) + "\n"


def build_environment(provider: dict, home: Path) -> dict[str, str]:
    """Do not leak parent API keys, thread identities, hooks or app-server URLs."""
    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    if provider.get("trust_env_proxy"):
        for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            if key in os.environ:
                env[key] = os.environ[key]
    env.update({
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "ORCA_CODEX_HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "TERM": "xterm-256color",
        API_KEY_ENV: str(provider.get("api_key") or "").strip(),
    })
    return env


def output_lines(text: str) -> list[tuple[str, str]]:
    """Retain input/assistant roles across the inline TUI's ANSI redraws."""
    row = None

    def strip_csi(match) -> str:
        nonlocal row
        if match[2] in {"H", "f"}:
            position = match[1].split(";")[0]
            next_row = int(position) if position.isdigit() else 1
            same_row = row == next_row
            row = next_row
            # Ratatui positions each wide character separately. Horizontal
            # moves within one row are not new lines ("在吗" must stay whole).
            return "" if same_row else "\n"
        return "\n" if match[2] in _BOUNDARY else ""

    text = _OSC.sub("", text)
    text = _CSI.sub(strip_csi, text)
    text = re.sub(r"\x1b[()*+].", "", text)
    text = re.sub(r"\x1b[8DEMc]", "\n", text)
    text = re.sub(r"\x1b.", "", text)
    text = "".join(
        "\n" if char in "\r\n" else " " if char.isspace()
        else "" if unicodedata.category(char) == "Cc" else char
        for char in text
    )
    lines = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("WARNING:"):
            continue
        role = ""
        if line.startswith(("›", "❯", "•")):
            role, line = ("input" if line[0] in {"›", "❯"} else "assistant"), line[1:].strip()
        elif line == ">" or line.startswith("> "):
            role, line = "input", line[1:].strip()
        if role:
            if not line:
                pending = role
                continue
            pending = ""
        elif pending:
            role, pending = pending, ""
        lines.append((role, line))
    return lines


def lines_after_prompt(text: str, prompt: str) -> list[tuple[str, str]]:
    lines = output_lines(text)
    indices = [i for i, (role, line) in enumerate(lines)
               if role == "input" and line.casefold() == prompt.strip().casefold()]
    return lines[indices[-1] + 1:] if indices else []


def terminal_ready(text: str) -> bool:
    lines = output_lines(text)
    model_lines = [line.lower() for _, line in lines if "model:" in line.lower()]
    return any(role == "input" for role, _ in lines) and (
        not model_lines or "loading" not in model_lines[-1]
    )


def prompt_visible(text: str, prompt: str) -> bool:
    return any(
        role in {"", "input"} and line.casefold() == prompt.strip().casefold()
        for role, line in output_lines(text)
    )


def retry_signal(lines: list[tuple[str, str]]) -> str:
    for _, line in lines:
        lower = line.lower()
        if ("retrying in " in lower and "attempt " in lower) or any(
            marker in lower for marker in (
                "reconnecting", "stream disconnected - retrying",
                "retry sse stream error", "retrying after auth recovery",
                "retrying sampling request",
            )
        ):
            return line
    return ""


def assistant_reply(lines: list[tuple[str, str]], prompt: str, short: bool) -> bool:
    for role, line in lines:
        lower = line.lower()
        if role != "assistant" or lower == prompt.lower():
            continue
        if "esctointerrupt" in "".join(lower.split()) or lower.startswith(
            ("run /review ", "write tests for @")
        ) or any(
            fragment in lower for fragment in _IGNORED
        ):
            continue
        if not any(char.isalnum() for char in line):
            continue
        letters = sum(char.isalpha() for char in line)
        if letters >= 8 and (any(char in line for char in ".?!。！？") or len(line) >= 20):
            return True
        if short and len(line) <= 80:
            normalized = lower.strip(" \t\r\n.,!?;:。！？，；：'\"")
            if normalized in {"ok", "yes", "yep", "yeah", "here", "online", "ready"}:
                return True
            # 题库答案可能是数字、分数、百分比、字母、布尔值或短中文。
            # 角色与当前输入之后的边界已由调用方限定，这里只排除 UI/重试文本。
            if normalized and any(char.isalnum() for char in normalized):
                return True
    return False


class CodexSession:
    """One owned CLI process and conversation; prompt() is called serially."""

    def __init__(self, provider: dict, model: str, effort: str) -> None:
        self.provider = provider
        self.model = model
        self.effort = effort
        self.tag = uuid.uuid4().hex[:12]
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._master: int | None = None
        self._directory: tempfile.TemporaryDirectory | None = None
        self._ready = False
        self.first_token_ms: float | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and not self.cancelled.is_set()

    def _start(self) -> None:
        with self._lock:
            if self.cancelled.is_set():
                return
            if self._process is not None:
                return
            if os.name != "posix":
                raise CodexConfigurationError("codex_cli keepalive requires a POSIX PTY")
            import fcntl
            import pty
            import struct
            import termios

            executable = shutil.which(str(self.provider.get("keepalive_codex_path") or "codex"))
            if not executable:
                raise CodexConfigurationError("Codex executable not found; configure keepalive_codex_path")
            self._directory = tempfile.TemporaryDirectory(prefix="aiproxy-codex-")
            root = Path(self._directory.name)
            home, workspace = root / "home", root / "work"
            home.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            config = build_config(self.provider, self.model, self.effort, workspace)
            fd = os.open(home / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(config)
            master, slave = pty.openpty()
            self._master = master
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
            try:
                self._process = subprocess.Popen(
                    [sys.executable, "-c", _PTY_EXEC, executable,
                     "--no-alt-screen", "--sandbox", "read-only", "--ask-for-approval", "never"],
                    stdin=slave, stdout=slave, stderr=slave,
                    cwd=workspace, env=build_environment(self.provider, home),
                    start_new_session=True, close_fds=True,
                )
            finally:
                os.close(slave)

    def _write(self, value: bytes) -> None:
        if self._master is None or self.cancelled.is_set():
            return
        offset = 0
        while offset < len(value):
            offset += os.write(self._master, value[offset:])

    def prompt(self, prompt: str, timeout: float, *, short: bool = False) -> tuple[bool, str, str]:
        """Return (ok, kind, redacted detail); never return raw terminal output."""
        deadline = time.monotonic() + timeout
        self.first_token_ms = None
        self._start()
        if self.cancelled.is_set():
            return False, "skipped", "Codex probe cancelled"
        if not self.alive:
            return False, "stale", "Codex process exited"
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        before = ""
        typed_output = ""
        after = ""
        typed = submitted = False
        submitted_at: float | None = None
        typed_at = time.monotonic() + INPUT_DELAY
        settled_at: float | None = None
        size = 0
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                return False, "skipped", "Codex probe cancelled"
            if not self.alive:
                return False, "stale", "Codex process exited before a reusable reply"
            if not typed and time.monotonic() >= typed_at and (self._ready or terminal_ready(before)):
                # One paste event avoids per-character TUI redraws (notably
                # CJK input split across cursor-position frames). Still wait
                # for the composer echo before sending Enter, as atry does.
                self._write(b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~")
                typed = True
                self._ready = False
            if submitted and settled_at is not None and time.monotonic() >= settled_at:
                lines = lines_after_prompt(after, prompt)
                retry = retry_signal(lines)
                if retry:
                    return False, "busy", self._redact(retry)
                if assistant_reply(lines, prompt, short):
                    self._ready = True
                    return True, "ok", ""
                settled_at = None
            try:
                ready, _, _ = select.select([self._master], [], [], 0.05)
                if not ready:
                    continue
                raw = os.read(self._master, 4096)
            except (OSError, ValueError):
                return False, "skipped" if self.cancelled.is_set() else "stale", "Codex terminal closed"
            if not raw:
                return False, "stale", "Codex terminal closed"
            size += len(raw)
            if size > OUTPUT_LIMIT:
                return False, "protocol", "Codex probe terminal output limit exceeded"
            # Answer cursor-position requests from the real TUI; this is not a
            # human terminal emulator and must not wait for external input.
            if b"\x1b[6n" in raw:
                self._write(b"\x1b[1;1R")
            chunk = decoder.decode(raw)
            current = after if submitted else before
            if not submitted:
                before += chunk
                current = before
            else:
                after += chunk
                current = after
                # Current TUIs emit synchronized-update/cursor frames even
                # when idle. Those empty frames must not postpone success.
                if output_lines(chunk):
                    settled_at = time.monotonic() + SETTLE_DELAY
            # Explicit TUI errors are metadata, not generated assistant text.
            error_lines = lines_after_prompt(current, prompt) if submitted else output_lines(current)
            for role, line in error_lines:
                if role != "assistant" and line.lower().startswith(("error:", "error loading", "fatal:", "■")):
                    kind = "permanent" if any(
                        marker in line.lower() for marker in (
                            "config.toml", "error loading config", "unknown variant",
                            "model_reasoning_effort", "unexpected argument",
                        )
                    ) else "error"
                    return False, kind, self._redact(line)
            if submitted_at is not None and self.first_token_ms is None:
                for role, line in error_lines:
                    lower = line.lower()
                    if role == "assistant" and any(char.isalnum() for char in line) and not any(
                        fragment in lower for fragment in _IGNORED
                    ) and "esctointerrupt" not in "".join(lower.split()):
                        self.first_token_ms = (time.monotonic() - submitted_at) * 1000
                        break
            if typed and not submitted:
                typed_output += chunk
                if prompt_visible(typed_output, prompt):
                    self._write(b"\r")
                    submitted_at = time.monotonic()
                    submitted = True
                    after = "› " + prompt + "\n"
            if submitted:
                retry = retry_signal(lines_after_prompt(current, prompt))
                if retry:
                    return False, "busy", self._redact(retry)
        return False, "timeout", f"Codex probe timeout ({timeout:g}s)"

    def _redact(self, text: str) -> str:
        return redact_text(text, provider_secrets(self.provider), limit=300)

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            if self._process is not None:
                try:
                    os.killpg(self._process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def stop(self) -> None:
        self.cancel()
        with self._lock:
            if self._process is not None:
                try:
                    self._process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                # Kill remaining descendants even if the main CLI already
                # exited. This session owns its process group exclusively.
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._process.wait(timeout=2.0)
                self._process = None
            if self._master is not None:
                os.close(self._master)
                self._master = None
            if self._directory is not None:
                self._directory.cleanup()
                self._directory = None
