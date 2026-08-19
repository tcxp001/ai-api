#!/usr/bin/env python3
"""Local web dashboard for provider configuration and AIProxy management."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import sqlite3
import sys
sys.dont_write_bytecode = True
import subprocess
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python versions without zoneinfo
    ZoneInfo = None  # type: ignore[assignment,misc]

import api as api_checks
from proxy import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_CONCURRENCY,
    DEFAULT_KEEPALIVE_INTERVAL,
    DEFAULT_KEEPALIVE_MAX_OUTPUT_TOKENS,
    DEFAULT_KEEPALIVE_REASONING_EFFORT,
    DEFAULT_KEEPALIVE_RETRY_INTERVAL,
    DEFAULT_KEEPALIVE_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    KEEPALIVE_CONCURRENCY_MAX,
    load_config as load_proxy_config,
)


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_YAML_FILE = CONFIG_DIR / "config.yaml"
CONFIG_JSON_FILE = CONFIG_DIR / "config.json"
CONFIG_FILE = CONFIG_JSON_FILE if CONFIG_JSON_FILE.exists() else CONFIG_YAML_FILE
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "log"
SETTINGS_FILE = DATA_DIR / "settings.json"
CHECKINS_FILE = DATA_DIR / "checkins.json"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
STATS_HTML = BASE_DIR / "stats.html"
STATS_DB = DATA_DIR / "request_stats.sqlite3"
CODEX_CONFIG = Path("/root/.codex/config.toml")
CODEX_DIR = Path("/root/.codex")
CODEX_MODEL_CATALOG_DIR = CODEX_DIR / "model-catalogs"
SHELL_HOME = Path.home()
CLAUDE_FUNCTIONS_FILE = BASE_DIR / "claude-code-functions.sh"
BASHRC_FILE = SHELL_HOME / ".bashrc"
CLAUDE_FUNCTIONS_SOURCE_START = "# >>> ai-api Claude Code functions"
CLAUDE_FUNCTIONS_SOURCE_END = "# <<< ai-api Claude Code functions"
SYSTEMD_DIR = Path("/etc/systemd/system")
AIPROXY_SERVICE_PREFIX = "ai-api-proxy-"
AIPROXY_KEEPALIVE_SERVICE = "aiproxy-keepalive.service"
AIPROXY_KEEPALIVE_ID = "aiproxy-keepalive"
AIPROXY_SYSTEMD_SERVICES = ("aiproxy.service", AIPROXY_KEEPALIVE_SERVICE)
AIPROXY_SINGLE_SERVICE = "aiproxy.service"
AIPROXY_SINGLE_ID = "aiproxy"
AIPROXY_INSTANCES_FILE = DATA_DIR / "aiproxy-instances.json"
PROXY_RESTART_LOG = LOG_DIR / "proxy-restart.log"
DEFAULT_PROXY_BASE = "http://127.0.0.1:18006"
DEFAULT_KEEPALIVE_PROXY_BASE = "http://127.0.0.1:18007"
AIPROXY_HTTP_TRANSIENT_SECONDS = 12
AIPROXY_HTTP_FAILURE_THRESHOLD = 3

MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_LISTEN = "0.0.0.0"
DEFAULT_PUBLIC_HOST = "192.168.2.10"
DEFAULT_PORT = 18080
DEFAULT_AUTO_COMPACT_PERCENT = 70
MIN_AUTO_COMPACT_PERCENT = 1
MAX_AUTO_COMPACT_PERCENT = 95
DEFAULT_AUTO_COMPACT_TOKEN_LIMIT = 200000
MIN_AUTO_COMPACT_TOKEN_LIMIT = 100000
MAX_AUTO_COMPACT_TOKEN_LIMIT = 1000000
CODEX_STREAM_MAX_RETRIES_DEFAULT = 5
CODEX_RETRY_MAX = 100

write_lock = threading.Lock()
aiproxy_http_probe_lock = threading.Lock()
aiproxy_http_probe_state: dict[str, dict[str, Any]] = {}

PROVIDER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BACKUP_DIR = BASE_DIR / "backup"

if ZoneInfo is not None:
    try:
        STATS_TIMEZONE = ZoneInfo("Asia/Shanghai")
    except Exception:  # pragma: no cover - only relevant on incomplete tzdata
        STATS_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
else:  # pragma: no cover - Python versions without zoneinfo
    STATS_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        tmp_name = f.name
        f.write(encoded)
    os.replace(tmp_name, path)


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        tmp_name = f.name
        f.write(content)
    os.replace(tmp_name, path)


def normalize_auto_compact_percent(value: Any, default: int = DEFAULT_AUTO_COMPACT_PERCENT) -> int:
    if value is None or value == "":
        return default
    try:
        percent = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError("autoCompactPercent must be a number")
    if percent < MIN_AUTO_COMPACT_PERCENT or percent > MAX_AUTO_COMPACT_PERCENT:
        raise ValueError(f"autoCompactPercent must be between {MIN_AUTO_COMPACT_PERCENT} and {MAX_AUTO_COMPACT_PERCENT}")
    return percent


def normalize_auto_compact_token_limit(
    value: Any,
    default: int = DEFAULT_AUTO_COMPACT_TOKEN_LIMIT,
) -> int:
    if value is None or value == "":
        return default
    try:
        limit = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError("autoCompactTokenLimit must be a number")
    if limit < MIN_AUTO_COMPACT_TOKEN_LIMIT or limit > MAX_AUTO_COMPACT_TOKEN_LIMIT:
        raise ValueError(
            f"autoCompactTokenLimit must be between "
            f"{MIN_AUTO_COMPACT_TOKEN_LIMIT} and {MAX_AUTO_COMPACT_TOKEN_LIMIT}"
        )
    return limit


def load_app_settings() -> dict[str, Any]:
    raw = read_json(SETTINGS_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    try:
        percent = normalize_auto_compact_percent(raw.get("autoCompactPercent"))
    except ValueError:
        percent = DEFAULT_AUTO_COMPACT_PERCENT
    try:
        token_limit = normalize_auto_compact_token_limit(raw.get("autoCompactTokenLimit"))
    except ValueError:
        token_limit = DEFAULT_AUTO_COMPACT_TOKEN_LIMIT
    return {
        "autoCompactPercent": percent,
        "autoCompactTokenLimit": token_limit,
    }


def save_app_settings(settings: dict[str, Any]) -> dict[str, Any]:
    current = load_app_settings()
    if "autoCompactPercent" in settings:
        current["autoCompactPercent"] = normalize_auto_compact_percent(settings.get("autoCompactPercent"))
    if "autoCompactTokenLimit" in settings:
        current["autoCompactTokenLimit"] = normalize_auto_compact_token_limit(
            settings.get("autoCompactTokenLimit")
        )
    write_json_atomic(SETTINGS_FILE, current)
    return current


def current_auto_compact_percent(value: Any = None) -> int:
    if value is not None:
        return normalize_auto_compact_percent(value)
    return int(load_app_settings().get("autoCompactPercent") or DEFAULT_AUTO_COMPACT_PERCENT)


def current_auto_compact_token_limit(value: Any = None) -> int:
    if value is not None:
        return normalize_auto_compact_token_limit(value)
    return int(
        load_app_settings().get("autoCompactTokenLimit")
        or DEFAULT_AUTO_COMPACT_TOKEN_LIMIT
    )


def auto_compact_token_limit(
    context_window: int,
    percent: int,
    fixed_token_limit: int = DEFAULT_AUTO_COMPACT_TOKEN_LIMIT,
) -> int:
    try:
        context = int(context_window)
    except (TypeError, ValueError):
        context = 0
    if context <= 0:
        context = 128000
    percentage_limit = int(context * normalize_auto_compact_percent(percent) / 100)
    return max(
        1,
        min(
            percentage_limit,
            normalize_auto_compact_token_limit(fixed_token_limit),
        ),
    )


def backup_destination(path: Path, now: datetime) -> Path:
    """Return the backup path for a file without placing app-managed backups next to it.

    Project config backups stay under ``ai-api/backup`` so the dashboard backup
    APIs can list and restore them. Codex configs are backed up under their
    own ``backup`` directory instead of polluting ``/root/.codex`` with
    ``*.bak-*`` files.
    """
    path = Path(path)
    backup_root = BACKUP_DIR
    relative_parent = Path()

    for source_root, target_root in (
        (CODEX_DIR, CODEX_DIR / "backup"),
        (CONFIG_DIR, BACKUP_DIR),
        (BASE_DIR, BACKUP_DIR),
    ):
        try:
            relative = path.resolve().relative_to(source_root.resolve())
        except ValueError:
            continue
        backup_root = target_root
        relative_parent = relative.parent
        break

    backup_dir = backup_root / now.strftime("%Y-%m-%d") / relative_parent
    stamp = now.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{path.name}.bak-{stamp}"
    if backup.exists():
        backup = backup_dir / f"{path.name}.bak-{stamp}-{now.strftime('%f')}"
    return backup


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    now = datetime.now()
    backup = backup_destination(path, now)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(path.read_bytes())
    return backup


def active_config_file() -> Path:
    return CONFIG_JSON_FILE if CONFIG_JSON_FILE.exists() else CONFIG_YAML_FILE


def config_backup_paths() -> list[Path]:
    paths: list[Path] = []
    for pattern in ("config.yaml.bak-*", "config.json.bak-*"):
        paths.extend(BACKUP_DIR.glob(f"*/{pattern}"))
    return paths


def backup_display_name(path: Path) -> str:
    try:
        return path.relative_to(BACKUP_DIR).as_posix()
    except ValueError:
        return path.name


def list_config_backups() -> list[dict[str, Any]]:
    backups = []
    for path in sorted(config_backup_paths(), key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        backups.append({
            "name": backup_display_name(path),
            "path": str(path),
            "size": stat.st_size,
            "createdAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds"),
        })
    return backups


def resolve_config_backup(name: str) -> Path:
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("backup not found")
    candidate = (BACKUP_DIR / raw).resolve()
    backup_root = BACKUP_DIR.resolve()
    if not (candidate == backup_root or backup_root in candidate.parents):
        raise ValueError("backup not found")
    backup_name = candidate.name
    if not (backup_name.startswith("config.yaml.bak-") or backup_name.startswith("config.json.bak-")) or not candidate.exists():
        raise ValueError("backup not found")
    return candidate


def restore_config_backup(name: str) -> Path:
    backup = resolve_config_backup(name)
    backup_name = backup.name
    target = CONFIG_JSON_FILE if backup_name.startswith("config.json") else CONFIG_YAML_FILE
    with write_lock:
        current_backup = backup_file(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(backup.read_bytes())
    return current_backup or Path("")


def load_raw_config() -> Any:
    path = active_config_file()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        if path.suffix == ".json":
            return json.load(f) or []
        return yaml.safe_load(f) or []


def load_provider_list() -> list[dict[str, Any]]:
    cfg = load_raw_config()
    if isinstance(cfg, dict) and isinstance(cfg.get("providers"), list):
        return cfg["providers"]
    if isinstance(cfg, list):
        return cfg
    raise ValueError("config.yaml root must be a provider list or {providers: [...]}")


def provider_validation_label(index: int, name: str = "") -> str:
    return f"provider {name!r}" if name else f"provider #{index}"


KEEPALIVE_FIELDS = (
    "keepalive",
    "keepalive_interval",
    "keepalive_retry_interval",
    "keepalive_timeout",
    "keepalive_concurrency",
    "keepalive_model",
    "keepalive_max_attempts",
    "keepalive_reasoning_effort",
    "keepalive_max_output_tokens",
)


def normalize_keepalive_number(value: Any, field_name: str, low: float, high: float, default: float) -> float | int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number")
    if number < low or number > high:
        raise ValueError(f"{field_name} must be between {low:g} and {high:g}")
    return int(number) if number.is_integer() else number


def normalize_keepalive(provider: dict[str, Any], label: str) -> None:
    """Normalize the keepalive fields in place. Bounds mirror proxy.py."""
    provider["keepalive"] = api_checks.coerce_bool(provider.get("keepalive"), False)
    try:
        provider["keepalive_interval"] = normalize_keepalive_number(
            provider.get("keepalive_interval"), "keepalive_interval", 5, 3600, DEFAULT_KEEPALIVE_INTERVAL
        )
        provider["keepalive_retry_interval"] = normalize_keepalive_number(
            provider.get("keepalive_retry_interval"),
            "keepalive_retry_interval",
            1,
            3600,
            DEFAULT_KEEPALIVE_RETRY_INTERVAL,
        )
        provider["keepalive_timeout"] = normalize_keepalive_number(
            provider.get("keepalive_timeout"), "keepalive_timeout", 5, 600, DEFAULT_KEEPALIVE_TIMEOUT
        )
        provider["keepalive_concurrency"] = int(
            normalize_keepalive_number(
                provider.get("keepalive_concurrency"), "keepalive_concurrency", 1, KEEPALIVE_CONCURRENCY_MAX, DEFAULT_KEEPALIVE_CONCURRENCY
            )
        )
        provider["keepalive_max_attempts"] = int(
            normalize_keepalive_number(provider.get("keepalive_max_attempts"), "keepalive_max_attempts", 0, 100000, 0)
        )
        provider["keepalive_max_output_tokens"] = int(
            normalize_keepalive_number(
                provider.get("keepalive_max_output_tokens"),
                "keepalive_max_output_tokens",
                16,
                4096,
                DEFAULT_KEEPALIVE_MAX_OUTPUT_TOKENS,
            )
        )
    except ValueError as exc:
        raise ValueError(f"{label} {exc}") from exc
    provider["keepalive_model"] = str(provider.get("keepalive_model") or "").strip()
    effort = str(
        provider.get("keepalive_reasoning_effort") or DEFAULT_KEEPALIVE_REASONING_EFFORT
    ).strip().lower()
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError(
            f"{label} keepalive_reasoning_effort must be one of: low, medium, high, xhigh, max"
        )
    provider["keepalive_reasoning_effort"] = effort


def validate_provider(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError(f"provider #{index} must be an object")
    provider = dict(entry)
    name = str(provider.get("name") or "").strip()
    base_url = str(provider.get("base_url") or provider.get("url") or "").strip()
    if not name:
        raise ValueError(f"provider #{index} missing name")
    label = provider_validation_label(index, name)
    if not PROVIDER_NAME_PATTERN.match(name):
        raise ValueError(
            f"{label} name must be a URL-safe path segment: letters, numbers, dot, underscore or hyphen; "
            "it must start with a letter or number"
        )
    if not re.match(r"^https?://", base_url):
        raise ValueError(f"{label} base_url must start with http:// or https://")
    provider["name"] = name
    provider["base_url"] = base_url.rstrip("/")
    provider.pop("url", None)
    provider.pop("request_max_retries", None)
    stream_default = codex_stream_retry_default()
    try:
        provider["stream_max_retries"] = normalize_codex_retry(
            provider.get("stream_max_retries"),
            stream_default,
            "stream_max_retries",
        )
    except ValueError as exc:
        raise ValueError(f"{label} {exc}") from exc
    if "api_key" not in provider and "key" in provider:
        provider["api_key"] = provider.pop("key")
    mode = str(provider.get("api_mode") or "").strip()
    valid_modes = {"codex_responses", "responses", "chat_completions", "messages", "custom_endpoint"}
    provider["api_mode"] = mode or "codex_responses"
    if provider["api_mode"] not in valid_modes:
        raise ValueError(f"{label} api_mode must be one of: {', '.join(sorted(valid_modes))}")
    custom_endpoint = str(provider.get("custom_endpoint") or provider.get("endpoint") or "").strip()
    if provider["api_mode"] == "custom_endpoint":
        if not custom_endpoint:
            raise ValueError(f"{label} custom_endpoint is required")
        if not custom_endpoint.startswith("/"):
            custom_endpoint = "/" + custom_endpoint
        if custom_endpoint == "/message":
            raise ValueError(f"{label} custom_endpoint must be /messages, not /message")
        provider["custom_endpoint"] = custom_endpoint
    else:
        provider.pop("custom_endpoint", None)
        custom_endpoint = ""
    provider.pop("endpoint", None)
    try:
        provider["auth_mode"] = normalize_auth_mode(provider.get("auth_mode"), provider["api_mode"], custom_endpoint)
    except ValueError as exc:
        raise ValueError(f"{label} {exc}") from exc
    anthropic_version = str(provider.get("anthropic_version") or "").strip()
    if provider["auth_mode"] == "anthropic":
        provider["anthropic_version"] = anthropic_version or "2023-06-01"
    else:
        provider.pop("anthropic_version", None)
    provider["enabled"] = api_checks.coerce_bool(provider.get("enabled"), True)
    provider["pinned"] = api_checks.coerce_bool(provider.get("pinned"), False)
    if provider["pinned"]:
        pinned_at = provider.get("pinned_at")
        if pinned_at not in (None, ""):
            try:
                pinned_at = int(pinned_at)
            except (TypeError, ValueError):
                raise ValueError(f"{label} pinned_at must be an integer") from None
            if pinned_at <= 0:
                raise ValueError(f"{label} pinned_at must be positive")
            provider["pinned_at"] = pinned_at
        else:
            provider.pop("pinned_at", None)
    else:
        provider.pop("pinned_at", None)
    normalize_keepalive(provider, label)
    headers = provider.get("headers", {})
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise ValueError(f"{label} headers must be an object")
    provider["headers"] = headers
    remove_headers = provider.get("remove_headers", [])
    if remove_headers is None:
        remove_headers = []
    if not isinstance(remove_headers, list):
        raise ValueError(f"{label} remove_headers must be an array")
    provider["remove_headers"] = remove_headers
    models = provider.get("models") or provider.get("model")
    if isinstance(models, str):
        provider["models"] = {models: {}}
        provider.pop("model", None)
    elif isinstance(models, list):
        provider["models"] = {str(item): {} for item in models if str(item).strip()}
    elif isinstance(models, dict):
        provider["models"] = models
    else:
        provider["models"] = {}
    return provider


def provider_auth_mode_for_endpoint(api_mode: str, custom_endpoint: str = "") -> str:
    mode = str(api_mode or "").strip().lower()
    endpoint = str(custom_endpoint or "").strip().lower()
    if mode == "messages" or (mode == "custom_endpoint" and endpoint == "/messages"):
        return "anthropic"
    return "bearer"


def normalize_auth_mode(value: Any, api_mode: str, custom_endpoint: str = "") -> str:
    default = provider_auth_mode_for_endpoint(api_mode, custom_endpoint)
    mode = str(value or default).strip().lower()
    if mode not in {"bearer", "anthropic"}:
        raise ValueError("auth_mode must be bearer or anthropic")
    return mode


def codex_stream_retry_default() -> int:
    return CODEX_STREAM_MAX_RETRIES_DEFAULT


def normalize_codex_retry(value: Any, default: int, field_name: str) -> int:
    if value is None or value == "":
        return int(default)
    try:
        if isinstance(value, bool):
            raise ValueError
        retry = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer")
    if retry < 0 or retry > CODEX_RETRY_MAX:
        raise ValueError(f"{field_name} must be between 0 and {CODEX_RETRY_MAX}")
    return retry


def compact_provider(provider: dict[str, Any]) -> dict[str, Any]:
    item = dict(provider)
    # Drop empty optional fields from persisted/exported config. Keep meaningful
    # falsy values such as headers.User-Agent: "" for the Empty UA preset and
    # enabled: false for disabled providers.
    for key in ("remove_headers",):
        if item.get(key) == []:
            item.pop(key, None)
    for key in ("headers", "models"):
        if item.get(key) == {}:
            item.pop(key, None)
    if item.get("auth_mode") == "bearer":
        item.pop("auth_mode", None)
    if item.get("anthropic_version") == "2023-06-01":
        item.pop("anthropic_version", None)
    if not item.get("pinned"):
        item.pop("pinned", None)
        item.pop("pinned_at", None)
    item.pop("request_max_retries", None)
    # keepalive 关闭时仍保留非默认参数，允许先配置参数、之后再显式启用。
    # 默认值继续省略，未配置过 Keepalive 的 provider 不会产生额外噪音。
    if not item.get("keepalive"):
        item.pop("keepalive", None)
    if item.get("keepalive_interval") == DEFAULT_KEEPALIVE_INTERVAL:
        item.pop("keepalive_interval", None)
    if item.get("keepalive_retry_interval") == DEFAULT_KEEPALIVE_RETRY_INTERVAL:
        item.pop("keepalive_retry_interval", None)
    if item.get("keepalive_timeout") == DEFAULT_KEEPALIVE_TIMEOUT:
        item.pop("keepalive_timeout", None)
    if item.get("keepalive_concurrency") == DEFAULT_KEEPALIVE_CONCURRENCY:
        item.pop("keepalive_concurrency", None)
    if not item.get("keepalive_max_attempts"):
        item.pop("keepalive_max_attempts", None)
    if not item.get("keepalive_model"):
        item.pop("keepalive_model", None)
    if item.get("keepalive_reasoning_effort") == DEFAULT_KEEPALIVE_REASONING_EFFORT:
        item.pop("keepalive_reasoning_effort", None)
    if item.get("keepalive_max_output_tokens") == DEFAULT_KEEPALIVE_MAX_OUTPUT_TOKENS:
        item.pop("keepalive_max_output_tokens", None)
    for key in ("note", "api_key", "key", "reasoning_effort", "reasoning", "anthropic_version"):
        if item.get(key) == "":
            item.pop(key, None)
    return item


def compact_provider_list(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [compact_provider(provider) for provider in providers]


def validate_provider_list(providers: list[Any]) -> list[dict[str, Any]]:
    normalized = [validate_provider(provider, index) for index, provider in enumerate(providers, start=1)]
    seen: dict[str, int] = {}
    for index, provider in enumerate(normalized, start=1):
        name = str(provider.get("name") or "")
        key = name.lower()
        if key in seen:
            raise ValueError(f"provider {name!r} duplicates provider #{seen[key]}")
        seen[key] = index
    return normalized


def provider_yaml_text(providers: list[dict[str, Any]]) -> str:
    normalized = compact_provider_list(validate_provider_list(providers))
    return yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False)


def parse_provider_yaml_text(content: str) -> list[dict[str, Any]]:
    parsed = yaml.safe_load(content or "")
    if isinstance(parsed, dict) and isinstance(parsed.get("providers"), list):
        parsed = parsed["providers"]
    if not isinstance(parsed, list):
        raise ValueError("YAML root must be a provider list or {providers: [...]}")
    return validate_provider_list(parsed)


def provider_config_warnings(providers: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for provider in providers:
        name = str(provider.get("name") or "未命名")
        if not str(provider.get("api_key") or provider.get("key") or "").strip():
            warnings.append(f"{name}: api_key 为空；仅在上游不需要鉴权时可忽略")
        models = provider.get("models") or {}
        if not isinstance(models, dict) or not models:
            warnings.append(f"{name}: models 为空；Codex 可能无法选择模型")
    return warnings


def save_provider_list(providers: list[Any], fmt: str = "auto") -> tuple[Path | None, list[str]]:
    normalized = validate_provider_list(providers)
    warnings = provider_config_warnings(normalized)
    persisted = compact_provider_list(normalized)
    raw_config = load_raw_config()
    target = active_config_file()
    if fmt == "json":
        target = CONFIG_JSON_FILE
    elif fmt == "yaml":
        target = CONFIG_YAML_FILE
    with write_lock:
        backup = backup_file(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=BASE_DIR, delete=False) as f:
            tmp_name = f.name
            payload: Any = persisted
            if isinstance(raw_config, dict):
                payload = {"providers": persisted}
            if target.suffix == ".json":
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            else:
                yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_name, target)
    return backup, warnings


def save_provider_pin(provider_name: str, pinned: bool, pinned_at: Any = None) -> tuple[Path | None, list[str]]:
    """Persist dashboard-only pin metadata without syncing or restarting services."""
    name = str(provider_name or "").strip()
    if not name:
        raise ValueError("provider name is required")
    providers = load_provider_list()
    provider = next(
        (
            item
            for item in providers
            if str(item.get("name") or "").strip().lower() == name.lower()
        ),
        None,
    )
    if provider is None:
        raise ValueError(f"provider {name!r} not found")

    provider["pinned"] = api_checks.coerce_bool(pinned, False)
    if provider["pinned"]:
        if pinned_at in (None, ""):
            raise ValueError("pinned_at is required when enabling pin")
        try:
            timestamp = int(pinned_at)
        except (TypeError, ValueError):
            raise ValueError("pinned_at must be an integer") from None
        if timestamp <= 0:
            raise ValueError("pinned_at must be positive")
        provider["pinned_at"] = timestamp
    else:
        provider.pop("pinned_at", None)

    return save_provider_list(providers, "auto")


def provider_keepalive_settings(provider: dict[str, Any]) -> dict[str, Any]:
    item = dict(provider)
    normalize_keepalive(item, provider_validation_label(1, str(item.get("name") or "")))
    return {
        "enabled": item["keepalive"],
        "concurrency": item["keepalive_concurrency"],
        "retryInterval": item["keepalive_retry_interval"],
        "interval": item["keepalive_interval"],
    }


def save_provider_keepalive(
    provider_name: str,
    *,
    enabled: Any = None,
    update_enabled: bool = False,
    concurrency: Any = None,
    retry_interval: Any = None,
    interval: Any = None,
    update_parameters: bool = False,
) -> tuple[Path | None, list[str], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Persist only one provider's Keepalive fields.

    Provider 基础字段和未在 Keepalive 卡片中暴露的 Keepalive 高级字段保持不变。
    """
    name = str(provider_name or "").strip()
    if not name:
        raise ValueError("provider name is required")
    if not update_enabled and not update_parameters:
        raise ValueError("keepalive update is required")

    before_providers = load_provider_list()
    providers = [dict(item) for item in before_providers]
    provider = next(
        (
            item
            for item in providers
            if str(item.get("name") or "").strip().lower() == name.lower()
        ),
        None,
    )
    if provider is None:
        raise ValueError(f"provider {name!r} not found")

    if update_enabled:
        provider["keepalive"] = api_checks.coerce_bool(enabled, False)
    if update_parameters:
        provider["keepalive_concurrency"] = int(
            normalize_keepalive_number(
                concurrency,
                "keepalive_concurrency",
                1,
                KEEPALIVE_CONCURRENCY_MAX,
                DEFAULT_KEEPALIVE_CONCURRENCY,
            )
        )
        provider["keepalive_retry_interval"] = normalize_keepalive_number(
            retry_interval,
            "keepalive_retry_interval",
            1,
            3600,
            DEFAULT_KEEPALIVE_RETRY_INTERVAL,
        )
        provider["keepalive_interval"] = normalize_keepalive_number(
            interval,
            "keepalive_interval",
            5,
            3600,
            DEFAULT_KEEPALIVE_INTERVAL,
        )

    backup, warnings = save_provider_list(providers, "auto")
    after_providers = load_provider_list()
    saved_provider = next(
        (
            item
            for item in after_providers
            if str(item.get("name") or "").strip().lower() == name.lower()
        ),
        None,
    )
    if saved_provider is None:
        raise ValueError(f"provider {name!r} not found after save")
    return backup, warnings, before_providers, after_providers, provider_keepalive_settings(saved_provider)


def provider_public(provider: dict[str, Any]) -> dict[str, Any]:
    item = dict(provider)
    key = str(item.get("api_key") or item.get("key") or "")
    item["api_key_masked"] = mask_secret(key)
    item["has_api_key"] = bool(key)
    return item


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:6]}...{value[-4:]}"


def default_checkins(providers: list[dict[str, Any]]) -> dict[str, Any]:
    existing = read_json(CHECKINS_FILE, {"items": []})
    by_id = {str(item.get("providerId")): item for item in existing.get("items", []) if item.get("providerId")}
    items = []
    for provider in providers:
        pid = str(provider.get("name") or "").strip()
        if not pid:
            continue
        item = dict(by_id.get(pid) or {})
        item.setdefault("providerId", pid)
        item.setdefault("name", pid)
        item.setdefault("loginUrl", "")
        item.setdefault("checkinUrl", "")
        item.setdefault("accountNote", "")
        item.setdefault("frequency", "daily")
        item.setdefault("enabled", True)
        item.setdefault("lastConfirmedAt", "")
        item.setdefault("note", "")
        items.append(item)
    return {"items": items, "updatedAt": existing.get("updatedAt", "")}


def provider_model_names(provider: dict[str, Any]) -> list[str]:
    models = provider.get("models") or provider.get("model")
    names: list[str] = []
    if isinstance(models, str):
        names = [models]
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.append(str(item.get("id") or item.get("name") or ""))
            else:
                names.append(str(item))
    elif isinstance(models, dict):
        names = [str(key) for key in models.keys()]

    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        model = str(name or "").strip()
        if model and model not in seen:
            deduped.append(model)
            seen.add(model)
    return deduped


def first_model(provider: dict[str, Any]) -> str:
    models = provider_model_names(provider)
    return models[0] if models else ""


def provider_api_mode(provider: dict[str, Any]) -> str:
    mode = str(provider.get("api_mode") or "").strip().lower()
    if mode == "custom_endpoint":
        return "codex_responses"
    return mode or "codex_responses"


def provider_endpoint(provider: dict[str, Any]) -> str:
    mode = str(provider.get("api_mode") or "").strip().lower()
    if mode == "custom_endpoint":
        endpoint = str(provider.get("custom_endpoint") or "").strip()
        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        if endpoint == "/message":
            return "/message"
        return endpoint or "/responses"
    if mode == "chat_completions":
        return "/chat/completions"
    if mode == "messages":
        return "/messages"
    return "/responses"


def claude_code_function_name(provider_name: str) -> str:
    encoded: list[str] = []
    for char in str(provider_name or "").strip():
        if char.isalnum():
            encoded.append(char)
        elif char == "_":
            encoded.append("_u")
        elif char == "-":
            encoded.append("_d")
        elif char == ".":
            encoded.append("_p")
    return "cc" + ("".join(encoded) or "provider")


def provider_uses_messages_endpoint(provider: dict[str, Any]) -> bool:
    return provider_endpoint(provider).lower() == "/messages"


def keepalive_proxy_base(proxy_base: str = DEFAULT_PROXY_BASE) -> str:
    """Return the companion keepalive proxy address (the next TCP port)."""
    text = str(proxy_base or DEFAULT_PROXY_BASE).strip().rstrip("/")
    try:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return DEFAULT_KEEPALIVE_PROXY_BASE
    if not parsed.scheme or not host:
        return DEFAULT_KEEPALIVE_PROXY_BASE
    normalized_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{normalized_host}:{port + 1}"


def provider_proxy_base(
    provider: dict[str, Any],
    proxy_base: str = DEFAULT_PROXY_BASE,
    keepalive_base: str = "",
) -> str:
    if api_checks.coerce_bool(provider.get("keepalive"), False):
        return (keepalive_base or keepalive_proxy_base(proxy_base)).rstrip("/")
    return proxy_base.rstrip("/")


def claude_code_function_block(
    provider: dict[str, Any],
    proxy_base: str = DEFAULT_PROXY_BASE,
    keepalive_base: str = "",
    auto_compact_percent: int | None = None,
    auto_compact_token_limit_value: int | None = None,
) -> tuple[str, dict[str, Any]]:
    provider_name = str(provider.get("name") or "").strip()
    function_name = claude_code_function_name(provider_name)
    models = provider_model_names(provider)
    local_base_url = f"{provider_proxy_base(provider, proxy_base, keepalive_base)}/{provider_name}"
    compact_percent = current_auto_compact_percent(auto_compact_percent)
    compact_token_limit = current_auto_compact_token_limit(
        auto_compact_token_limit_value
    )
    lines = [
        f"# Provider: {provider_name}",
        f"{function_name}() {{",
    ]

    if not models:
        lines.extend([
            f"  printf '%s\\n' {shlex.quote(f'Provider {provider_name} 尚未配置模型，请先在 ai-api 上游管理页添加模型。')} >&2",
            "  return 2",
            "}",
        ])
        return "\n".join(lines), {
            "provider": provider_name,
            "function": function_name,
            "models": [],
            "defaultModel": "",
        }

    lines.extend([
        f"  local model={shlex.quote(models[0])}",
        "",
        '  if [[ "${1:-}" == "-m" || "${1:-}" == "--model" ]]; then',
        '    case "${2:-}" in',
        "      list)",
        "        printf '%s\\n' \\",
    ])
    for index, model in enumerate(models, start=1):
        suffix = "（默认）" if index == 1 else ""
        continuation = " \\" if index < len(models) else ""
        lines.append(f"          {shlex.quote(f'{index}. {model}{suffix}')}{continuation}")
    lines.extend([
        "        return",
        "        ;;",
    ])
    for index, model in enumerate(models, start=1):
        lines.extend([
            f"      {index}|{shlex.quote(model)})",
            f"        model={shlex.quote(model)}",
            "        ;;",
        ])
    lines.extend([
        "      *)",
        '        printf \'未知模型：%s\\n\' "${2:-未指定}" >&2',
        f"        printf '%s\\n' {shlex.quote(f'执行 {function_name} -m list 查看可用模型。')} >&2",
        "        return 2",
        "        ;;",
        "    esac",
        "    shift 2",
        "  fi",
        "",
        "  command env \\",
        f"    ANTHROPIC_BASE_URL={shlex.quote(local_base_url)} \\",
        "    ANTHROPIC_AUTH_TOKEN=local-ai-api \\",
        '    ANTHROPIC_MODEL="$model" \\',
        f"    CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={compact_percent} \\",
        f"    CLAUDE_CODE_AUTO_COMPACT_WINDOW={compact_token_limit} \\",
        "    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \\",
        '    claude "$@"',
        "}",
    ])
    return "\n".join(lines), {
        "provider": provider_name,
        "function": function_name,
        "models": models,
        "defaultModel": models[0],
    }


def generated_claude_code_functions(
    providers: list[dict[str, Any]],
    proxy_base: str = DEFAULT_PROXY_BASE,
    keepalive_base: str = "",
    auto_compact_percent: int | None = None,
    auto_compact_token_limit_value: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    blocks: list[str] = []
    items: list[dict[str, Any]] = []
    compact_percent = current_auto_compact_percent(auto_compact_percent)
    compact_token_limit = current_auto_compact_token_limit(
        auto_compact_token_limit_value
    )
    for provider in providers:
        if not api_checks.coerce_bool(provider.get("enabled"), True):
            continue
        if not provider_uses_messages_endpoint(provider):
            continue
        provider_name = str(provider.get("name") or "").strip()
        if not provider_name:
            continue
        block, item = claude_code_function_block(
            provider,
            proxy_base,
            keepalive_base,
            compact_percent,
            compact_token_limit,
        )
        blocks.append(block)
        items.append(item)

    header = "\n".join([
        "# Generated by ai-api. Manual changes will be overwritten.",
        "# Reload the current shell with: source ~/.bashrc",
    ])
    content = header + "\n"
    if blocks:
        content += "\n" + "\n\n".join(blocks) + "\n"
    return content, items


def claude_functions_source_line(path: Path | None = None) -> str:
    quoted_path = shlex.quote(str(path or CLAUDE_FUNCTIONS_FILE))
    return f"[ -f {quoted_path} ] && source {quoted_path}"


def ensure_claude_functions_sourced() -> tuple[bool, Path | None]:
    text = BASHRC_FILE.read_text(encoding="utf-8") if BASHRC_FILE.exists() else ""
    source_line = claude_functions_source_line()
    expected_block = "\n".join([
        CLAUDE_FUNCTIONS_SOURCE_START,
        source_line,
        CLAUDE_FUNCTIONS_SOURCE_END,
    ])
    if expected_block in text:
        return False, None

    managed_pattern = re.compile(
        rf"(?ms)^{re.escape(CLAUDE_FUNCTIONS_SOURCE_START)}\n.*?^{re.escape(CLAUDE_FUNCTIONS_SOURCE_END)}\n?"
    )
    updated = managed_pattern.sub("", text)
    if updated and not updated.endswith("\n"):
        updated += "\n"
    if updated:
        updated += "\n"
    updated += expected_block + "\n"
    backup = backup_file(BASHRC_FILE)
    write_text_atomic(BASHRC_FILE, updated)
    return True, backup


def sync_claude_code_functions(
    providers: list[dict[str, Any]],
    proxy_base: str | None = None,
    keepalive_base: str = "",
    auto_compact_percent: int | None = None,
    auto_compact_token_limit_value: int | None = None,
) -> dict[str, Any]:
    base_url = (proxy_base or current_aiproxy_proxy_base()).rstrip("/")
    dedicated_url = (
        keepalive_base
        or (current_keepalive_proxy_base() if proxy_base is None else keepalive_proxy_base(base_url))
    ).rstrip("/")
    compact_percent = current_auto_compact_percent(auto_compact_percent)
    compact_token_limit = current_auto_compact_token_limit(
        auto_compact_token_limit_value
    )
    content, items = generated_claude_code_functions(
        providers,
        base_url,
        dedicated_url,
        compact_percent,
        compact_token_limit,
    )
    previous = CLAUDE_FUNCTIONS_FILE.read_text(encoding="utf-8") if CLAUDE_FUNCTIONS_FILE.exists() else ""
    functions_changed = previous != content
    should_write = bool(items) or CLAUDE_FUNCTIONS_FILE.exists()
    if should_write and functions_changed:
        write_text_atomic(CLAUDE_FUNCTIONS_FILE, content)

    source_added = False
    bashrc_backup: Path | None = None
    if items:
        source_added, bashrc_backup = ensure_claude_functions_sourced()

    return {
        "ok": True,
        "path": str(CLAUDE_FUNCTIONS_FILE),
        "bashrc": str(BASHRC_FILE),
        "functions": items,
        "count": len(items),
        "changed": functions_changed and should_write,
        "sourceAdded": source_added,
        "bashrcBackup": str(bashrc_backup) if bashrc_backup else "",
        "reloadCommand": "source ~/.bashrc",
        "proxyBase": base_url,
        "autoCompactPercent": compact_percent,
        "autoCompactTokenLimit": compact_token_limit,
    }


def codex_wire_api(provider: dict[str, Any]) -> str:
    return "responses"


def safe_codex_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("._")
    return safe or "provider"


def model_meta_for(provider: dict[str, Any], model: str) -> dict[str, Any]:
    models = provider.get("models")
    if isinstance(models, dict):
        meta = models.get(model) or {}
        return meta if isinstance(meta, dict) else {}
    return {}


def model_context_window(meta: dict[str, Any]) -> int:
    for key in ("context_length", "max_model_len", "max_tokens"):
        try:
            value = int(meta.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 128000


def codex_model_catalog_entry(provider: dict[str, Any], model: str, priority: int) -> dict[str, Any]:
    meta = model_meta_for(provider, model)
    context = model_context_window(meta)
    effort = str(meta.get("reasoning_effort") or provider.get("reasoning_effort") or "medium").strip() or "medium"
    if effort == "none":
        effort = "medium"
    return {
        "slug": model,
        "display_name": model,
        "description": f"{model} via ai-api",
        "default_reasoning_level": effort,
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Fast responses with lighter reasoning"},
            {"effort": "medium", "description": "Balanced speed and reasoning"},
            {"effort": "high", "description": "Greater reasoning depth"},
            {"effort": "xhigh", "description": "Extra high reasoning depth"},
            {"effort": "max", "description": "Maximum reasoning depth"},
        ],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": "You are Codex, a coding agent. Help the user complete software engineering tasks accurately and efficiently.",
        "model_messages": {},
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "auto",
        "support_verbosity": True,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "supports_image_detail_original": False,
        "context_window": context,
        "max_context_window": context,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text"],
        "supports_search_tool": False,
        "use_responses_lite": False,
    }


def write_codex_model_catalog(provider: dict[str, Any]) -> dict[str, str] | None:
    name = str(provider.get("name") or "").strip()
    models = provider_model_names(provider)
    if not name or not models:
        return None
    catalog_path = CODEX_MODEL_CATALOG_DIR / f"{safe_codex_filename(name)}.json"
    backup = backup_file(catalog_path)
    payload = {"models": [codex_model_catalog_entry(provider, model, index) for index, model in enumerate(models)]}
    write_json_atomic(catalog_path, payload)
    return {"path": str(catalog_path), "backup": str(backup) if backup else ""}


def proxy_provider_url(provider_name: str, proxy_base: str = "http://127.0.0.1:18006") -> str:
    return f"{proxy_base.rstrip('/')}/{provider_name}/v1"


CODEX_SYNC_START = "# >>> ai-api generated model providers"
CODEX_SYNC_END = "# <<< ai-api generated model providers"


def toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def generated_codex_provider_block(
    providers: list[dict[str, Any]],
    proxy_base: str,
    keepalive_base: str = "",
) -> str:
    lines: list[str] = [CODEX_SYNC_START]
    for provider in providers:
        name = str(provider.get("name") or "").strip()
        if not name:
            continue
        stream_default = codex_stream_retry_default()
        stream_retries = normalize_codex_retry(
            provider.get("stream_max_retries"),
            stream_default,
            "stream_max_retries",
        )
        lines.extend([
            f'[model_providers.{toml_string(name)}]',
            f'name = {toml_string(name)}',
            f'base_url = {toml_string(proxy_provider_url(name, provider_proxy_base(provider, proxy_base, keepalive_base)))}',
            f'wire_api = {toml_string(codex_wire_api(provider))}',
            f"stream_max_retries = {stream_retries}",
            "",
        ])
    lines.append(CODEX_SYNC_END)
    return "\n".join(lines).rstrip() + "\n"


def strip_codex_generated_blocks(text: str, provider_names: set[str] | None = None, proxy_base: str = "") -> str:
    names = {str(name) for name in provider_names} if provider_names is not None else None
    proxy_prefix = proxy_base.rstrip("/") + "/" if proxy_base else ""
    lines = text.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == CODEX_SYNC_START:
            index += 1
            while index < len(lines) and lines[index].strip() != CODEX_SYNC_END:
                index += 1
            if index < len(lines):
                index += 1
            continue

        match = re.match(r'^\s*\[model_providers\."?([^"\]]+)"?\]\s*$', line)
        if match:
            block_name = match.group(1)
            block: list[str] = [line]
            index += 1
            while (
                index < len(lines)
                and not lines[index].startswith("[")
                and lines[index].strip() != CODEX_SYNC_START
            ):
                block.append(lines[index])
                index += 1
            block_base_url = ""
            for block_line in block:
                if block_line.strip().startswith("base_url") and "=" in block_line:
                    block_base_url = block_line.split("=", 1)[1].strip().strip("\"")
            if names is None or block_name in names or (proxy_prefix and block_base_url.startswith(proxy_prefix)):
                continue
            kept.extend(block)
            continue

        kept.append(line)
        index += 1
    return "\n".join(kept).rstrip() + "\n\n"


def parse_toml_string(value: str) -> str:
    value = value.strip()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, str):
            return parsed
    except Exception:
        pass
    return value.strip('"')


def codex_default_provider_from_text(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("["):
            break
        if re.match(r'^\s*model_provider\s*=', line):
            return parse_toml_string(line.split("=", 1)[1])
    return ""


def choose_codex_default_provider(providers: list[dict[str, Any]], existing: str) -> str:
    names = {str(provider.get("name") or "").strip() for provider in providers if provider.get("name")}
    current = codex_default_provider_from_text(existing)
    if current in names:
        return current
    return str(providers[0].get("name") or "").strip() if providers else ""


def set_codex_default_model(text: str, provider_name: str, model: str) -> str:
    if not provider_name or not model:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    in_top_level = True
    for line in lines:
        if line.startswith("["):
            in_top_level = False
        if in_top_level and re.match(r'^\s*model(_provider)?\s*=', line):
            continue
        kept.append(line)
    prefix = [f'model = {toml_string(model)}', f'model_provider = {toml_string(provider_name)}', ""]
    return "\n".join(prefix + kept).rstrip() + "\n"


def cleanup_codex_profiles(active_names: set[str], stale_names: set[str] | None = None) -> dict[str, Any]:
    deleted: list[str] = []
    backups: list[str] = []
    catalog_deleted: list[dict[str, str]] = []
    stale = {name for name in (stale_names or set()) if name and name not in active_names}
    for name in sorted(stale):
        path = CODEX_DIR / f"{name}.config.toml"
        if path.exists():
            backup = backup_file(path)
            if backup:
                backups.append(str(backup))
            path.unlink()
            deleted.append(str(path))
        catalog_path = CODEX_MODEL_CATALOG_DIR / f"{safe_codex_filename(name)}.json"
        if catalog_path.exists():
            catalog_backup = backup_file(catalog_path)
            catalog_path.unlink()
            catalog_deleted.append({"path": str(catalog_path), "backup": str(catalog_backup) if catalog_backup else ""})
    return {"deleted": deleted, "backups": backups, "catalogDeleted": catalog_deleted}

def sync_codex_config(
    providers: list[dict[str, Any]],
    proxy_base: str,
    default_provider: str = "",
    stale_profile_names: set[str] | None = None,
    auto_compact_percent: int | None = None,
    keepalive_base: str = "",
    auto_compact_token_limit_value: int | None = None,
) -> dict[str, Any]:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_MODEL_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    backup = backup_file(CODEX_CONFIG)
    compact_percent = current_auto_compact_percent(auto_compact_percent)
    compact_token_limit = current_auto_compact_token_limit(
        auto_compact_token_limit_value
    )
    provider_names = {str(provider.get("name") or "").strip() for provider in providers if provider.get("name")}
    content = (
        strip_codex_generated_blocks(existing, provider_names, proxy_base)
        + generated_codex_provider_block(providers, proxy_base, keepalive_base)
    )
    default = next((provider for provider in providers if str(provider.get("name") or "") == default_provider), None)
    if default:
        content = set_codex_default_model(content, default_provider, first_model(default))
    CODEX_CONFIG.write_text(content, encoding="utf-8")

    written_profiles = []
    written_catalogs = []
    profile_backups = []
    for provider in providers:
        name = str(provider.get("name") or "").strip()
        model = first_model(provider)
        if not name or not model:
            continue
        models = provider_model_names(provider)
        model_meta = model_meta_for(provider, model)
        context = model_context_window(model_meta)
        effort = model_meta.get("reasoning_effort") or provider.get("reasoning_effort") or ""
        catalog_result = write_codex_model_catalog(provider)
        if catalog_result:
            written_catalogs.append(catalog_result)
        profile_lines = [f"model = {toml_string(model)}", f"model_provider = {toml_string(name)}"]
        profile_lines.append(f'model_context_window = {int(context)}')
        profile_lines.append(
            f'model_auto_compact_token_limit = '
            f'{auto_compact_token_limit(context, compact_percent, compact_token_limit)}'
        )
        if effort:
            profile_lines.append(f"model_reasoning_effort = {toml_string(effort)}")
        if catalog_result:
            profile_lines.append(f"model_catalog_json = {toml_string(catalog_result['path'])}")
        profile_lines.extend(["", "[tui.model_availability_nux]"])
        for item in models:
            profile_lines.append(f"{toml_string(item)} = 4")
        profile_lines.append("")
        profile_path = CODEX_DIR / f"{name}.config.toml"
        profile_backup = backup_file(profile_path)
        if profile_backup:
            profile_backups.append(str(profile_backup))
        profile_path.write_text("\n".join(profile_lines), encoding="utf-8")
        written_profiles.append(str(profile_path))
    profile_cleanup = cleanup_codex_profiles(provider_names, stale_profile_names)
    return {
        "target": str(CODEX_CONFIG),
        "backup": str(backup) if backup else "",
        "providers": len(providers),
        "profiles": written_profiles,
        "catalogs": written_catalogs,
        "profileBackups": profile_backups,
        "profileCleanup": profile_cleanup,
        "autoCompactPercent": compact_percent,
        "autoCompactTokenLimit": compact_token_limit,
    }

def is_proxy_generated_provider(item: Any, proxy_base: str) -> bool:
    if not isinstance(item, dict):
        return False
    base_url = str(item.get("base_url") or "").rstrip("/")
    proxy_prefix = proxy_base.rstrip("/") + "/" if proxy_base else ""
    return bool(proxy_prefix and base_url.startswith(proxy_prefix) and base_url.endswith("/v1"))



def sync_app_configs_for_proxy_base(
    providers: list[dict[str, Any]],
    proxy_base: str,
    stale_names: set[str] | None = None,
    auto_compact_percent: int | None = None,
    keepalive_base: str = "",
    auto_compact_token_limit_value: int | None = None,
) -> dict[str, Any]:
    enabled = [provider for provider in providers if api_checks.coerce_bool(provider.get("enabled"), True)]
    codex_existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    codex_default = choose_codex_default_provider(enabled, codex_existing)
    compact_percent = current_auto_compact_percent(auto_compact_percent)
    compact_token_limit = current_auto_compact_token_limit(
        auto_compact_token_limit_value
    )
    return {
        "needed": True,
        "proxyBase": proxy_base,
        "settings": {
            "autoCompactPercent": compact_percent,
            "autoCompactTokenLimit": compact_token_limit,
        },
        "codex": sync_codex_config(
            enabled,
            proxy_base,
            codex_default,
            stale_names,
            compact_percent,
            keepalive_base,
            compact_token_limit,
        ),
    }



def parse_codex_chains(proxy_base: str = "http://127.0.0.1:18006") -> list[dict[str, Any]]:
    if not CODEX_CONFIG.exists():
        return []
    text = CODEX_CONFIG.read_text(encoding="utf-8")
    chains = []
    current = ""
    base_url = ""
    for line in text.splitlines() + ["["]:
        if line.startswith('[model_providers.') or line == "[":
            if current and base_url:
                match = re.search(r"/([^/]+)/v1/?$", base_url.rstrip("/"))
                provider_id = match.group(1) if match else current
                chains.append({
                    "id": f"codex-{provider_id}",
                    "name": f"Codex -> {provider_id}",
                    "client": "codex",
                    "proxyId": "local-18006",
                    "providerId": provider_id,
                    "model": "",
                    "enabled": True,
                })
            current = ""
            base_url = ""
            m = re.search(r'\[model_providers\."?([^"\]]+)"?\]', line)
            if m:
                current = m.group(1)
            continue
        if current and line.strip().startswith("base_url"):
            base_url = line.split("=", 1)[1].strip().strip('"')
    return chains



def merge_discovered_chains(configured: list[dict[str, Any]], providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    provider_lookup = {str(p.get("name")): p for p in providers}
    merged = {str(chain.get("id")): dict(chain) for chain in configured if chain.get("id")}
    for chain in parse_codex_chains():
        provider = provider_lookup.get(str(chain.get("providerId")))
        if provider:
            chain["model"] = first_model(provider)
        merged.setdefault(str(chain.get("id")), chain)
    return list(merged.values())


def app_config_preview(providers: list[dict[str, Any]], proxy_base: str = "http://127.0.0.1:18006") -> dict[str, Any]:
    names = [str(provider.get("name") or "").strip() for provider in providers if provider.get("name")]
    return {
        "codex": {"target": str(CODEX_CONFIG), "exists": CODEX_CONFIG.exists(), "providers": len(names), "proxyBase": proxy_base},
        "settings": load_app_settings(),
        "providers": names,
        "discoveredChains": parse_codex_chains(),
    }


def app_sync_projection(providers: list[dict[str, Any]], proxy_base: str = DEFAULT_PROXY_BASE) -> list[dict[str, Any]]:
    projection = []
    for provider in providers:
        if not api_checks.coerce_bool(provider.get("enabled"), True):
            continue
        name = str(provider.get("name") or "").strip()
        if not name:
            continue
        projection.append({
            "name": name,
            "base_url": proxy_provider_url(name, provider_proxy_base(provider, proxy_base)),
            "api_mode": provider_api_mode(provider),
            "first_model": first_model(provider),
            "models": provider.get("models") or {},
            "reasoning_effort": provider.get("reasoning_effort") or "",
            "stream_max_retries": normalize_codex_retry(
                provider.get("stream_max_retries"),
                codex_stream_retry_default(),
                "stream_max_retries",
            ),
        })
    return projection


def app_sync_needed(before: list[dict[str, Any]], after: list[dict[str, Any]], proxy_base: str = DEFAULT_PROXY_BASE) -> bool:
    return app_sync_projection(before, proxy_base) != app_sync_projection(after, proxy_base)


def app_configs_need_proxy_sync(providers: list[dict[str, Any]], proxy_base: str) -> bool:
    enabled = [provider for provider in providers if api_checks.coerce_bool(provider.get("enabled"), True) and str(provider.get("name") or "").strip()]
    if not enabled:
        return False
    expected = {
        str(provider.get("name") or "").strip(): proxy_provider_url(
            str(provider.get("name") or "").strip(),
            provider_proxy_base(provider, proxy_base),
        )
        for provider in enabled
    }
    try:
        codex_items = {str(item.get("name") or "").strip(): str(item.get("base_url") or "").rstrip("/") for item in load_codex_custom_providers()}
        if any(codex_items.get(name) != url.rstrip("/") for name, url in expected.items()):
            return True
    except Exception:
        return True
    return False


def auto_sync_app_configs(before: list[dict[str, Any]], after: list[dict[str, Any]], proxy_base: str | None = None) -> dict[str, Any]:
    proxy_base = (proxy_base or current_aiproxy_proxy_base()).strip().rstrip("/")
    keepalive_base = current_keepalive_proxy_base().strip().rstrip("/")
    providers = [provider for provider in after if api_checks.coerce_bool(provider.get("enabled"), True)]
    if not app_sync_needed(before, after, proxy_base) and not app_configs_need_proxy_sync(providers, proxy_base):
        return {"needed": False, "reason": "no app config change", "proxyBase": proxy_base}
    before_names = {str(provider.get("name") or "").strip() for provider in before if provider.get("name")}
    after_names = {str(provider.get("name") or "").strip() for provider in providers if provider.get("name")}
    stale_names = before_names - after_names
    return sync_app_configs_for_proxy_base(
        providers,
        proxy_base,
        stale_names,
        keepalive_base=keepalive_base,
    )


def load_codex_custom_providers() -> list[dict[str, Any]]:
    if not CODEX_CONFIG.exists():
        return []
    text = CODEX_CONFIG.read_text(encoding="utf-8")
    providers: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines() + ["["]:
        if line.startswith("[model_providers.") or line == "[":
            if current:
                providers.append(current)
            current = None
            match = re.search(r'\[model_providers\."?([^"\]]+)"?\]', line)
            if match:
                current = {"name": match.group(1)}
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip().strip('"')
    return providers



def save_codex_custom_providers(items: list[dict[str, Any]]) -> Path | None:
    existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    backup = backup_file(CODEX_CONFIG)
    provider_blocks: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        provider_blocks.extend([
            f"[model_providers.{toml_string(name)}]",
            f"name = {toml_string(name)}",
            "base_url = " + toml_string(str(item.get("base_url") or "").strip()),
            "wire_api = " + toml_string(str(item.get("wire_api") or "responses").strip()),
            "",
        ])
    content = strip_codex_generated_blocks(existing) + "\n".join(provider_blocks).rstrip() + "\n"
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CODEX_CONFIG.write_text(content, encoding="utf-8")
    return backup


def load_app_custom_providers() -> dict[str, Any]:
    return {
        "codex": {"target": str(CODEX_CONFIG), "items": load_codex_custom_providers()},
    }


def normalize_service_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-.")
    if not normalized:
        raise ValueError("service id is required")
    return normalized


def service_name(service_id: str) -> str:
    return f"{AIPROXY_SERVICE_PREFIX}{normalize_service_id(service_id)}.service"


def load_aiproxy_instances() -> dict[str, Any]:
    data = read_json(AIPROXY_INSTANCES_FILE, {"items": []})
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def save_aiproxy_instances(items: list[dict[str, Any]]) -> dict[str, Any]:
    data = {"items": items, "updatedAt": now_iso()}
    write_json_atomic(AIPROXY_INSTANCES_FILE, data)
    return data


def run_systemctl(args: list[str]) -> tuple[int, str]:
    completed = subprocess.run(["systemctl", *args], text=True, capture_output=True, timeout=15)
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return completed.returncode, output


def read_systemd_unit(name: str) -> tuple[str, str] | None:
    path = SYSTEMD_DIR / name
    if path.exists():
        try:
            return path.read_text(encoding="utf-8"), str(path)
        except OSError:
            return None
    try:
        code, output = run_systemctl(["cat", name])
    except Exception:
        return None
    if code != 0 or not output.strip():
        return None
    return output, name


def aiproxy_unit_path(service_id: str) -> Path:
    return SYSTEMD_DIR / service_name(service_id)


def aiproxy_target_service(item: dict[str, Any] | None = None) -> str:
    return AIPROXY_SINGLE_SERVICE


def aiproxy_target_unit_path(item: dict[str, Any]) -> Path:
    return SYSTEMD_DIR / aiproxy_target_service(item)


def aiproxy_unit_content(item: dict[str, Any]) -> str:
    service_id = normalize_service_id(str(item.get("id") or "default"))
    listen = str(item.get("listen") or "127.0.0.1")
    port = int(item.get("port") or 18006)
    config = str(item.get("config") or active_config_file())
    verbose = " --verbose" if item.get("verbose") else ""
    scope_flag = str(item.get("scopeFlag") or "").strip()
    scope = f" {scope_flag}" if scope_flag else ""
    exec_start = f'{sys.executable} {BASE_DIR / "proxy.py"} --config {config} --listen {listen} --port {port}{scope}{verbose}'
    return "\n".join([
        "[Unit]",
        f"Description=AI API Proxy {service_id}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={BASE_DIR}",
        f"ExecStart={exec_start}",
        "Restart=always",
        "RestartSec=3",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def aiproxy_status(item: dict[str, Any]) -> dict[str, Any]:
    service_id = normalize_service_id(str(item.get("id") or "default"))
    name = str(item.get("service") or service_name(service_id))
    active_code, active = run_systemctl(["is-active", name])
    enabled_code, enabled = run_systemctl(["is-enabled", name])
    unit_path = str(item.get("unitPath") or (SYSTEMD_DIR / name))
    status = {**item, "id": service_id, "service": name, "unitPath": unit_path, "active": active.strip(), "enabledState": enabled.strip(), "activeOk": active_code == 0, "enabledOk": enabled_code == 0}
    status.update(aiproxy_http_status(status))
    if status["activeOk"] and status.get("httpAlive"):
        status["health"] = "healthy"
    elif not status["activeOk"]:
        status["health"] = "stopped"
    elif status["activeOk"] and status.get("httpTransient"):
        status["health"] = "starting"
    elif status["activeOk"] and not status.get("httpAlive"):
        status["health"] = "unhealthy"
    else:
        status["health"] = "unknown"
    return status


def normalize_aiproxy_url(value: Any, port: int) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    with_scheme = text if re.match(r"^https?://", text, re.IGNORECASE) else f"http://{text}"
    try:
        parsed = urlsplit(with_scheme)
        host = parsed.hostname or ""
        explicit_port = parsed.port
    except ValueError:
        return text
    if parsed.scheme.lower() != "http" or not host:
        return text
    normalized_host = f"[{host}]" if ":" in host else host
    return f"http://{normalized_host}:{explicit_port or port}"


def default_aiproxy_item(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    source = overrides or {}
    port = int(source.get("port") or 18006)
    return {
        **source,
        "id": AIPROXY_SINGLE_ID,
        "name": "AIProxy-主通道",
        "service": AIPROXY_SINGLE_SERVICE,
        "listen": str(source.get("listen") or "127.0.0.1"),
        "port": port,
        "url": normalize_aiproxy_url(source.get("url") or source.get("publicUrl"), port),
        "config": str(source.get("config") or CONFIG_YAML_FILE),
        "verbose": api_checks.coerce_bool(source.get("verbose"), False),
        "scopeFlag": "--exclude-keepalive",
    }


def keepalive_aiproxy_item(
    main_item: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    main = default_aiproxy_item(main_item)
    source = overrides or {}
    port = int(source.get("port") or (int(main.get("port") or 18006) + 1))
    default_url = keepalive_proxy_base(str(main.get("url") or aiproxy_probe_url(main)))
    return {
        **source,
        "id": AIPROXY_KEEPALIVE_ID,
        "name": "AIProxy-抢通保活通道",
        "service": AIPROXY_KEEPALIVE_SERVICE,
        "listen": str(source.get("listen") or main.get("listen") or "127.0.0.1"),
        "port": port,
        "url": normalize_aiproxy_url(source.get("url") or source.get("publicUrl") or default_url, port),
        "config": str(source.get("config") or main.get("config") or CONFIG_YAML_FILE),
        "verbose": api_checks.coerce_bool(source.get("verbose"), api_checks.coerce_bool(main.get("verbose"), False)),
        "scopeFlag": "--keepalive-only",
    }


def ensure_single_aiproxy_service() -> dict[str, Any]:
    item = preferred_aiproxy_item() or default_aiproxy_item()
    item = default_aiproxy_item(item)
    required = (
        (SYSTEMD_DIR / AIPROXY_SINGLE_SERVICE, item),
        (SYSTEMD_DIR / AIPROXY_KEEPALIVE_SERVICE, keepalive_aiproxy_item(item)),
    )
    for path, service_item in required:
        if not path.exists():
            write_aiproxy_service(service_item, restart=True)
    for service in AIPROXY_SYSTEMD_SERVICES:
        enabled_code, _enabled = run_systemctl(["is-enabled", service])
        if enabled_code != 0:
            run_systemctl(["enable", service])
    status = aiproxy_status(item)
    status["ensured"] = True
    return status


def write_aiproxy_service(item: dict[str, Any], restart: bool = True) -> dict[str, Any]:
    requested_id = normalize_service_id(str(item.get("id") or AIPROXY_SINGLE_ID))
    requested_service = str(item.get("service") or "")
    is_keepalive = requested_id == AIPROXY_KEEPALIVE_ID or requested_service == AIPROXY_KEEPALIVE_SERVICE
    if requested_id and requested_id not in {AIPROXY_SINGLE_ID, AIPROXY_KEEPALIVE_ID}:
        raise ValueError("AIProxy service not found")
    if requested_service and requested_service not in AIPROXY_SYSTEMD_SERVICES:
        raise ValueError("AIProxy service not found")

    existing = merged_aiproxy_service_items()
    existing_main = next(
        (candidate for candidate in existing if str(candidate.get("service") or "") == AIPROXY_SINGLE_SERVICE),
        default_aiproxy_item(),
    )
    existing_keepalive = next(
        (candidate for candidate in existing if str(candidate.get("service") or "") == AIPROXY_KEEPALIVE_SERVICE),
        keepalive_aiproxy_item(existing_main),
    )
    if is_keepalive:
        main_item = default_aiproxy_item(existing_main)
        target_item = keepalive_aiproxy_item(main_item, {**existing_keepalive, **item})
        keepalive_item = target_item
    else:
        target_item = default_aiproxy_item({**existing_main, **item})
        main_item = target_item
        keepalive_item = keepalive_aiproxy_item(main_item, existing_keepalive)

    target_service = str(target_item["service"])
    path = SYSTEMD_DIR / target_service
    backup = backup_file(path)
    path.write_text(aiproxy_unit_content(target_item), encoding="utf-8")
    reload_code, reload_output = run_systemctl(["daemon-reload"])
    action = "restart" if restart else "start"
    enable_code, enable_output = run_systemctl(["enable", target_service])
    action_code, action_output = run_systemctl([action, target_service])

    save_aiproxy_instances([main_item, keepalive_item])

    status = aiproxy_status(target_item)
    status["backup"] = str(backup) if backup else ""
    status["daemonReload"] = {"returnCode": reload_code, "output": reload_output}
    status["enable"] = {"returnCode": enable_code, "output": enable_output}
    status[action] = {"returnCode": action_code, "output": action_output}
    status["returnCode"] = max(reload_code, enable_code, action_code)
    status["output"] = "\n".join(part for part in (enable_output, action_output) if part)
    providers = load_provider_list()
    proxy_base = aiproxy_probe_url(main_item) or DEFAULT_PROXY_BASE
    keepalive_base = aiproxy_probe_url(keepalive_item) or DEFAULT_KEEPALIVE_PROXY_BASE
    try:
        status["appSync"] = sync_app_configs_for_proxy_base(
            providers,
            proxy_base,
            keepalive_base=keepalive_base,
        )
    except Exception as exc:
        status["appSync"] = {"error": f"{type(exc).__name__}: {exc}", "proxyBase": proxy_base}
    try:
        status["claudeFunctions"] = sync_claude_code_functions(providers, proxy_base, keepalive_base)
    except Exception as exc:
        status["claudeFunctions"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "proxyBase": proxy_base}
    return status

def control_aiproxy_service(service_id: str, action: str) -> dict[str, Any]:
    if action not in {"start", "stop", "restart", "enable", "disable"}:
        raise ValueError("unsupported action")
    item = find_aiproxy_service_item(service_id)
    if item is None:
        raise ValueError("AIProxy service not found")
    name = str(item.get("service") or AIPROXY_SINGLE_SERVICE)
    code, output = run_systemctl([action, name])
    status = aiproxy_status(item)
    status["returnCode"] = code
    status["output"] = output
    return status

def delete_aiproxy_service(service_id: str) -> dict[str, Any]:
    raise ValueError("AIProxy is a required single service and cannot be deleted")

def parse_cli_flag(args: list[str], name: str) -> str:
    prefix = f"{name}="
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return ""


def parse_aiproxy_scope_flag(args: list[str]) -> str:
    if "--keepalive-only" in args:
        return "--keepalive-only"
    if "--exclude-keepalive" in args:
        return "--exclude-keepalive"
    return ""


def aiproxy_probe_url(item: dict[str, Any]) -> str:
    explicit_url = str(item.get("url") or "").strip().rstrip("/")
    if explicit_url:
        return explicit_url
    port_text = str(item.get("port") or "").strip()
    listen = str(item.get("listen") or "").strip() or "127.0.0.1"
    if not port_text:
        return DEFAULT_PROXY_BASE
    try:
        port = int(port_text)
    except ValueError:
        return ""
    host = listen
    if host in {"0.0.0.0", "::", "[::]", "*"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("[") and not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def aiproxy_http_status(item: dict[str, Any]) -> dict[str, Any]:
    url = aiproxy_probe_url(item)
    service_key = str(item.get("service") or item.get("id") or url or "aiproxy")
    result: dict[str, Any] = {
        "url": url,
        "httpAlive": False,
        "httpStatus": "",
        "httpLatencyMs": None,
        "httpDetail": "",
        "httpCheckedAt": now_iso(),
        "httpTransient": False,
        "httpFailureCount": 0,
    }
    if not url:
        result["httpDetail"] = "缺少或无法解析监听地址"
        return result
    started = time.time()
    probe_path = "/__dashboard_probe__/v1/models"
    try:
        response = requests.get(url.rstrip("/") + probe_path, timeout=(1.5, 3))
        result["httpLatencyMs"] = int((time.time() - started) * 1000)
        result["httpStatus"] = f"HTTP {response.status_code}"
        # A 404 from an unknown provider still proves that AIProxy accepted and handled HTTP.
        result["httpAlive"] = response.status_code < 500
        result["httpDetail"] = f"{probe_path} -> HTTP {response.status_code}"
        with aiproxy_http_probe_lock:
            if result["httpAlive"]:
                aiproxy_http_probe_state[service_key] = {"lastSuccess": time.time(), "failures": 0}
            else:
                state = aiproxy_http_probe_state.setdefault(service_key, {})
                state["failures"] = int(state.get("failures") or 0) + 1
                result["httpFailureCount"] = state["failures"]
    except Exception as exc:
        now = time.time()
        result["httpLatencyMs"] = int((now - started) * 1000)
        raw_detail = api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 120)
        with aiproxy_http_probe_lock:
            state = aiproxy_http_probe_state.setdefault(service_key, {})
            failures = int(state.get("failures") or 0) + 1
            state["failures"] = failures
            last_success = float(state.get("lastSuccess") or 0)
        result["httpFailureCount"] = failures
        recent_success = bool(last_success and now - last_success <= AIPROXY_HTTP_TRANSIENT_SECONDS)
        # During a systemd restart or just after editing config, the process can be
        # active while the HTTP socket is not ready for a second or two. Treat the
        # first few active-service connection errors as transient to avoid false red
        # alarms; repeated failures still become unhealthy.
        if item.get("activeOk") and (recent_success or failures < AIPROXY_HTTP_FAILURE_THRESHOLD):
            result["httpTransient"] = True
            result["httpStatus"] = "探测重试中"
            result["httpDetail"] = f"AIProxy 正在启动/重启或端口尚未就绪；{raw_detail}"
        else:
            result["httpDetail"] = raw_detail
    return result


def path_matches(value: str, target: Path, cwd: Path | None = None) -> bool:
    if not value:
        return False
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (cwd or BASE_DIR) / candidate
    try:
        return candidate.resolve(strict=False) == target.resolve(strict=False)
    except OSError:
        return str(candidate) == str(target)


def aiproxy_item_from_unit(path: Path) -> dict[str, Any]:
    service_id = path.name[len(AIPROXY_SERVICE_PREFIX):-len(".service")]
    item: dict[str, Any] = {"id": service_id, "name": service_id, "listen": "", "port": "", "config": "", "service": service_name(service_id), "unitPath": str(path), "discovered": True}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return item
    match = re.search(r"^ExecStart=(.+)$", content, re.MULTILINE)
    if not match:
        return item
    exec_start = match.group(1).strip()
    if exec_start.startswith("-"):
        exec_start = exec_start[1:].lstrip()
    try:
        args = shlex.split(exec_start)
    except ValueError:
        return item
    item["config"] = parse_cli_flag(args, "--config")
    item["listen"] = parse_cli_flag(args, "--listen") or "127.0.0.1"
    item["port"] = parse_cli_flag(args, "--port") or 18006
    item["verbose"] = "--verbose" in args
    item["scopeFlag"] = parse_aiproxy_scope_flag(args)
    return item


def named_aiproxy_service_item(name: str) -> dict[str, Any] | None:
    unit = read_systemd_unit(name)
    if not unit:
        return None
    service_id = name[:-len(".service")] if name.endswith(".service") else name
    content, unit_path = unit
    item: dict[str, Any] = {"id": service_id, "name": service_id, "listen": "", "port": "", "config": "", "service": name, "unitPath": unit_path, "discovered": True}
    match = re.search(r"^ExecStart=(.+)$", content, re.MULTILINE)
    if not match:
        return item
    exec_start = match.group(1).strip()
    if exec_start.startswith("-"):
        exec_start = exec_start[1:].lstrip()
    try:
        args = shlex.split(exec_start)
    except ValueError:
        return item
    item["config"] = parse_cli_flag(args, "--config")
    item["listen"] = parse_cli_flag(args, "--listen") or "127.0.0.1"
    item["port"] = parse_cli_flag(args, "--port") or 18006
    item["verbose"] = "--verbose" in args
    item["scopeFlag"] = parse_aiproxy_scope_flag(args)
    return item


def discover_aiproxy_unit_items() -> list[dict[str, Any]]:
    items = []
    for path in sorted(SYSTEMD_DIR.glob(f"{AIPROXY_SERVICE_PREFIX}*.service")):
        items.append(aiproxy_item_from_unit(path))
    return items


def merged_aiproxy_service_items() -> list[dict[str, Any]]:
    configured_main = None
    configured_keepalive = None
    data = load_aiproxy_instances()
    for item in data.get("items", []):
        if str(item.get("service") or "") == AIPROXY_SINGLE_SERVICE or normalize_service_id(str(item.get("id") or "")) == AIPROXY_SINGLE_ID:
            configured_main = item
        elif str(item.get("service") or "") == AIPROXY_KEEPALIVE_SERVICE:
            configured_keepalive = item
    discovered_main = named_aiproxy_service_item(AIPROXY_SINGLE_SERVICE)
    main = default_aiproxy_item({**(discovered_main or {}), **(configured_main or {})})
    discovered_keepalive = named_aiproxy_service_item(AIPROXY_KEEPALIVE_SERVICE)
    keepalive = keepalive_aiproxy_item(
        main,
        {
            **(discovered_keepalive or {}),
            **(configured_keepalive or {}),
        },
    )
    return [main, keepalive]



def find_aiproxy_service_item(service_id: str) -> dict[str, Any] | None:
    target = normalize_service_id(service_id)
    for item in merged_aiproxy_service_items():
        item_id = normalize_service_id(str(item.get("id") or ""))
        service = str(item.get("service") or "")
        if target == item_id or target == normalize_service_id(service.removesuffix(".service")):
            return item
    return None



def aiproxy_config_files() -> list[dict[str, Any]]:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for path in [CONFIG_YAML_FILE, *sorted(BASE_DIR.glob("*.yaml")), *sorted(BASE_DIR.glob("*.yml"))]:
        if not path.exists() or not path.is_file():
            continue
        value = str(path.resolve(strict=False))
        if value in seen:
            continue
        seen.add(value)
        items.append({"name": path.name, "path": value})
    return items


def list_aiproxy_services() -> dict[str, Any]:
    data = load_aiproxy_instances()
    ensure_single_aiproxy_service()
    items = [aiproxy_status(item) for item in merged_aiproxy_service_items()]
    return {
        "items": items,
        "updatedAt": data.get("updatedAt", ""),
        "defaultConfig": str(CONFIG_YAML_FILE),
        "configFiles": aiproxy_config_files(),
        "summary": {
            "total": len(items),
            "running": sum(1 for item in items if item.get("activeOk")),
            "healthy": sum(1 for item in items if item.get("health") == "healthy"),
        },
    }



def preferred_aiproxy_item(items: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    candidates = items if items is not None else merged_aiproxy_service_items()
    if not candidates:
        return None
    return next((item for item in candidates if str(item.get("service") or "") == "aiproxy.service"), candidates[0])


def current_aiproxy_proxy_base(fallback_item: dict[str, Any] | None = None) -> str:
    item = fallback_item or preferred_aiproxy_item()
    return (aiproxy_probe_url(item or {}) or DEFAULT_PROXY_BASE).rstrip("/")


def current_keepalive_proxy_base() -> str:
    item = next(
        (candidate for candidate in merged_aiproxy_service_items() if str(candidate.get("service") or "") == AIPROXY_KEEPALIVE_SERVICE),
        None,
    )
    return (aiproxy_probe_url(item or {}) or DEFAULT_KEEPALIVE_PROXY_BASE).rstrip("/")


def keepalive_status_from_proxy(proxy_base: str = "") -> dict[str, Any]:
    """向 aiproxy 进程要抢通/保活状态。保活线程活在 proxy 里，dashboard 只是转发。"""
    base = (proxy_base or current_keepalive_proxy_base()).rstrip("/")
    if not base:
        return {"ok": False, "error": "aiproxy address unknown", "providers": {}}
    try:
        response = requests.get(f"{base}/_keepalive", timeout=(3, 5))
    except Exception as exc:
        return {"ok": False, "error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 400), "providers": {}, "proxyBase": base}
    if response.status_code != 200:
        return {"ok": False, "error": f"HTTP {response.status_code}", "providers": {}, "proxyBase": base}
    try:
        payload = response.json()
    except Exception:
        return {"ok": False, "error": "non-JSON reply from aiproxy", "providers": {}, "proxyBase": base}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "unexpected reply from aiproxy", "providers": {}, "proxyBase": base}
    payload["ok"] = True
    payload["proxyBase"] = base
    payload.setdefault("providers", {})
    return payload


def stats_connect() -> sqlite3.Connection | None:
    if not STATS_DB.exists():
        return None
    conn = sqlite3.connect(STATS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def stats_date_clause(query: dict[str, list[str]] | None) -> tuple[str, list[float]]:
    """Build a Shanghai-natural-day filter for request statistics."""
    value = str(((query or {}).get("date") or [""])[0]).strip()
    if not value:
        return "", []
    try:
        selected = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("date must use YYYY-MM-DD format") from None
    start = datetime.combine(selected, datetime.min.time(), tzinfo=STATS_TIMEZONE)
    end = start + timedelta(days=1)
    return "created_at >= ? AND created_at < ?", [start.timestamp(), end.timestamp()]


def stats_filter_options(query: dict[str, list[str]] | None = None) -> dict[str, list[dict[str, str]]]:
    """Return Provider names seen in the selected day."""
    date_clause, date_params = stats_date_clause(query)
    if not date_clause:
        return {
            "providers": [
                {"name": str(provider.get("name") or "")}
                for provider in load_provider_list()
                if str(provider.get("name") or "").strip()
            ],
        }
    conn = stats_connect()
    if conn is None:
        return {"providers": []}
    try:
        provider_rows = conn.execute(
            f"""
            SELECT DISTINCT provider_name AS name
            FROM request_stats
            WHERE {date_clause} AND TRIM(provider_name) <> ''
            ORDER BY name
            """,
            date_params,
        ).fetchall()
        return {"providers": [{"name": str(row["name"])} for row in provider_rows]}
    finally:
        conn.close()


def stats_summary(query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    date_clause, date_params = stats_date_clause(query)
    where = f" WHERE {date_clause}" if date_clause else ""
    conn = stats_connect()
    if conn is None:
        return {
            "requests": 0,
            "successes": 0,
            "successRate": 0,
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheReadTokens": 0,
            "cacheCreationTokens": 0,
            "averageFirstTokenMs": None,
            "averageDurationMs": None,
            "estimatedCost": 0,
        }
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(ok), 0) AS successes,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                AVG(first_token_ms) AS average_first_token_ms,
                AVG(duration_ms) AS average_duration_ms,
                SUM(estimated_cost) AS estimated_cost
            FROM request_stats
            """
            + where,
            date_params,
        ).fetchone()
        requests_count = int(row["requests"] or 0)
        successes = int(row["successes"] or 0)
        return {
            "requests": requests_count,
            "successes": successes,
            "successRate": round(successes * 100 / requests_count, 2) if requests_count else 0,
            "inputTokens": int(row["input_tokens"] or 0),
            "outputTokens": int(row["output_tokens"] or 0),
            "cacheReadTokens": int(row["cache_read_tokens"] or 0),
            "cacheCreationTokens": int(row["cache_creation_tokens"] or 0),
            "averageFirstTokenMs": row["average_first_token_ms"],
            "averageDurationMs": row["average_duration_ms"],
            "estimatedCost": (
                float(row["estimated_cost"])
                if row["estimated_cost"] is not None
                else None
            ),
        }
    finally:
        conn.close()


def stats_grouped(
    column: str,
    query: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    if column not in {"provider_name", "model"}:
        raise ValueError("invalid stats group")
    date_clause, date_params = stats_date_clause(query)
    where = f" WHERE {date_clause}" if date_clause else ""
    conn = stats_connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT
                {column} AS name,
                COUNT(*) AS requests,
                COALESCE(SUM(ok), 0) AS successes,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                AVG(headers_ms) AS average_headers_ms,
                AVG(first_token_ms) AS average_first_token_ms,
                AVG(duration_ms) AS average_duration_ms,
                SUM(estimated_cost) AS estimated_cost
            FROM request_stats
            {where}
            GROUP BY {column}
            ORDER BY requests DESC, name
            """,
            date_params,
        ).fetchall()
        result = []
        for row in rows:
            requests_count = int(row["requests"] or 0)
            successes = int(row["successes"] or 0)
            result.append(
                {
                    "name": str(row["name"] or ""),
                    "requests": requests_count,
                    "successes": successes,
                    "successRate": round(successes * 100 / requests_count, 2) if requests_count else 0,
                    "inputTokens": int(row["input_tokens"] or 0),
                    "outputTokens": int(row["output_tokens"] or 0),
                    "cacheReadTokens": int(row["cache_read_tokens"] or 0),
                    "cacheCreationTokens": int(row["cache_creation_tokens"] or 0),
                    "averageHeadersMs": row["average_headers_ms"],
                    "averageFirstTokenMs": row["average_first_token_ms"],
                    "averageDurationMs": row["average_duration_ms"],
                    "estimatedCost": (
                        float(row["estimated_cost"])
                        if row["estimated_cost"] is not None
                        else None
                    ),
                }
            )
        return result
    finally:
        conn.close()


def stats_requests(query: dict[str, list[str]]) -> list[dict[str, Any]]:
    try:
        limit = min(max(int((query.get("limit") or ["200"])[0]), 1), 1000)
    except (TypeError, ValueError):
        limit = 200
    clauses: list[str] = []
    params: list[Any] = []
    date_clause, date_params = stats_date_clause(query)
    if date_clause:
        clauses.append(date_clause)
        params.extend(date_params)
    for query_name, column in (("provider", "provider_name"), ("model", "model")):
        value = str((query.get(query_name) or [""])[0]).strip()
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn = stats_connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, provider_name, model, status_code,
                   ok, headers_ms, first_token_ms, duration_ms, input_tokens,
                   output_tokens, cache_read_tokens, cache_creation_tokens,
                   estimated_cost, error, is_streaming
            FROM request_stats
            """
            + where
            + " ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "createdAt": datetime.fromtimestamp(
                    float(row["created_at"]),
                    timezone.utc,
                ).astimezone(STATS_TIMEZONE).isoformat(timespec="seconds"),
                "provider": str(row["provider_name"] or ""),
                "model": str(row["model"] or ""),
                "statusCode": row["status_code"],
                "ok": bool(row["ok"]),
                "headersMs": row["headers_ms"],
                "firstTokenMs": row["first_token_ms"],
                "durationMs": row["duration_ms"],
                "inputTokens": int(row["input_tokens"] or 0),
                "outputTokens": int(row["output_tokens"] or 0),
                "cacheReadTokens": int(row["cache_read_tokens"] or 0),
                "cacheCreationTokens": int(row["cache_creation_tokens"] or 0),
                "estimatedCost": row["estimated_cost"],
                "error": str(row["error"] or ""),
                "streaming": bool(row["is_streaming"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def proc_cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def proc_cgroup(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def proxy_process_managed_by_aiproxy(pid: int) -> bool:
    cgroup = proc_cgroup(pid)
    if AIPROXY_SERVICE_PREFIX in cgroup:
        return True
    return any(name in cgroup for name in AIPROXY_SYSTEMD_SERVICES)


def proxy_script_in_cmd(args: list[str], cwd: Path | None) -> bool:
    proxy_script = BASE_DIR / "proxy.py"
    for arg in args:
        candidate = Path(arg)
        if candidate.name != "proxy.py":
            continue
        if not candidate.is_absolute():
            candidate = (cwd or BASE_DIR) / candidate
        if path_matches(str(candidate), proxy_script):
            return True
    return False


def proxy_cmd_config(args: list[str]) -> str:
    return parse_cli_flag(args, "--config") or str(CONFIG_YAML_FILE)


def iter_matching_manual_proxy_processes(config_path: Path) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return matches
    current_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == current_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [part.decode("utf-8", errors="ignore") for part in raw.split(b"\0") if part]
        if not args:
            continue
        cwd = proc_cwd(pid)
        if not proxy_script_in_cmd(args, cwd):
            continue
        if proxy_process_managed_by_aiproxy(pid):
            continue
        if not path_matches(proxy_cmd_config(args), config_path, cwd):
            continue
        matches.append({"pid": pid, "cmdline": args})
    return matches


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def manual_proxy_service_assignment(args: list[str]) -> str | None:
    scope = parse_aiproxy_scope_flag(args)
    if scope == "--keepalive-only":
        return AIPROXY_KEEPALIVE_SERVICE
    if scope == "--exclude-keepalive":
        return AIPROXY_SINGLE_SERVICE
    return None


def restart_manual_proxy_processes(
    config_path: Path,
    services: set[str] | None = None,
) -> dict[str, Any]:
    matches = []
    for item in iter_matching_manual_proxy_processes(config_path):
        assigned_service = manual_proxy_service_assignment(list(item.get("cmdline") or []))
        if services is not None and assigned_service is not None and assigned_service not in services:
            continue
        matches.append(item)
    result: dict[str, Any] = {"matched": len(matches), "stopped": [], "killed": [], "started": [], "log": str(PROXY_RESTART_LOG)}
    if not matches:
        return result
    commands: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for item in matches:
        command = list(item["cmdline"])
        key = tuple(command)
        if key not in seen:
            commands.append(command)
            seen.add(key)
        try:
            os.kill(int(item["pid"]), signal.SIGTERM)
            result["stopped"].append(int(item["pid"]))
        except ProcessLookupError:
            result["stopped"].append(int(item["pid"]))
        except OSError as exc:
            result.setdefault("errors", []).append({"pid": int(item["pid"]), "error": str(exc)})
    deadline = time.time() + 4
    while time.time() < deadline and any(pid_alive(int(item["pid"])) for item in matches):
        time.sleep(0.1)
    for item in matches:
        pid = int(item["pid"])
        if not pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            result["killed"].append(pid)
        except OSError as exc:
            result.setdefault("errors", []).append({"pid": pid, "error": str(exc)})
    PROXY_RESTART_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROXY_RESTART_LOG.open("ab") as log:
        for command in commands:
            try:
                process = subprocess.Popen(command, cwd=str(BASE_DIR), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                result["started"].append({"pid": process.pid, "cmd": " ".join(shlex.quote(part) for part in command)})
            except OSError as exc:
                result.setdefault("errors", []).append({"cmd": command, "error": str(exc)})
    return result


def restart_aiproxy_service_item(item: dict[str, Any], config_path: Path) -> dict[str, Any] | None:
    service_id = normalize_service_id(str(item.get("id") or ""))
    configured = str(item.get("config") or "")
    if configured and not path_matches(configured, config_path, BASE_DIR):
        return None
    name = str(item.get("service") or service_name(service_id))
    entry: dict[str, Any] = {"id": service_id, "service": name, "config": configured}
    try:
        active_code, active = run_systemctl(["is-active", name])
        entry["active"] = active.strip()
        if active_code != 0:
            entry["restarted"] = False
            entry["skipped"] = "inactive"
        else:
            code, output = run_systemctl(["restart", name])
            entry.update(aiproxy_status(item))
            entry["returnCode"] = code
            entry["output"] = output
            entry["restarted"] = code == 0
    except Exception as exc:
        entry["restarted"] = False
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


def provider_service_assignment(provider: dict[str, Any]) -> str:
    if api_checks.coerce_bool(provider.get("keepalive"), False):
        return AIPROXY_KEEPALIVE_SERVICE
    return AIPROXY_SINGLE_SERVICE


def canonical_provider_map(providers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    normalized = compact_provider_list(validate_provider_list(providers))
    result: dict[str, dict[str, Any]] = {}
    for provider in normalized:
        item = dict(provider)
        # pinned / pinned_at 只影响 dashboard 展示顺序，不改变任何 AIProxy 运行配置。
        item.pop("pinned", None)
        item.pop("pinned_at", None)
        result[str(item.get("name") or "").lower()] = item
    return result


def changed_aiproxy_services(
    before_providers: list[dict[str, Any]],
    after_providers: list[dict[str, Any]],
) -> set[str]:
    """Return only the proxy channels whose owned provider configs changed."""
    before = canonical_provider_map(before_providers)
    after = canonical_provider_map(after_providers)
    services: set[str] = set()
    for name in before.keys() | after.keys():
        old_provider = before.get(name)
        new_provider = after.get(name)
        if old_provider == new_provider:
            continue
        if old_provider is not None:
            services.add(provider_service_assignment(old_provider))
        if new_provider is not None:
            services.add(provider_service_assignment(new_provider))
    return services


def restart_aiproxy_services_for_config(
    config_path: Path,
    services: set[str] | None = None,
) -> dict[str, Any]:
    items = []
    seen_services: set[str] = set()
    candidates = merged_aiproxy_service_items()
    for name in AIPROXY_SYSTEMD_SERVICES:
        item = named_aiproxy_service_item(name)
        if item:
            candidates.append(item)
    for item in candidates:
        service = str(item.get("service") or service_name(normalize_service_id(str(item.get("id") or ""))))
        if service in seen_services:
            continue
        seen_services.add(service)
        if services is not None and service not in services:
            continue
        entry = restart_aiproxy_service_item(item, config_path)
        if entry is not None:
            items.append(entry)
    return {"matched": len(items), "items": items}


def check_proxy_config_before_restart(config_path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, str(BASE_DIR / "proxy.py"), "--config", str(config_path), "--check"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
        return {"ok": completed.returncode == 0, "returnCode": completed.returncode, "output": output[-2000:]}
    except Exception as exc:
        return {"ok": False, "error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}


def restart_after_config_write(
    before_providers: list[dict[str, Any]] | None = None,
    after_providers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config_path = active_config_file()
    result: dict[str, Any] = {"configPath": str(config_path)}
    changed_services: set[str] | None = None
    if before_providers is not None and after_providers is not None:
        changed_services = changed_aiproxy_services(before_providers, after_providers)
        result["changedServices"] = sorted(changed_services)
        if not changed_services:
            result["ok"] = True
            result["skipped"] = "no effective provider changes"
            return result
    result["configCheck"] = check_proxy_config_before_restart(config_path)
    if not result["configCheck"].get("ok"):
        result["ok"] = False
        result["skipped"] = "proxy config check failed; existing proxy processes were left unchanged"
        return result
    try:
        result["manualProxy"] = restart_manual_proxy_processes(config_path, changed_services)
    except Exception as exc:
        result["manualProxy"] = {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}
    try:
        result["aiproxyServices"] = restart_aiproxy_services_for_config(config_path, changed_services)
    except Exception as exc:
        result["aiproxyServices"] = {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}

    ok = True
    manual = result.get("manualProxy") if isinstance(result.get("manualProxy"), dict) else {}
    if manual.get("error") or manual.get("errors"):
        ok = False
    services = result.get("aiproxyServices") if isinstance(result.get("aiproxyServices"), dict) else {}
    if services.get("error"):
        ok = False
    for item in services.get("items") or []:
        if item.get("error"):
            ok = False
        elif item.get("skipped") == "inactive":
            continue
        elif item.get("restarted") is False:
            ok = False
    result["ok"] = ok
    return result


def is_cloudflare_challenge(response: requests.Response) -> bool:
    content_type = str(response.headers.get("content-type") or "").lower()
    body = " ".join((response.text or "").split()).lower()[:1000]
    return (
        response.status_code in {403, 429, 503}
        and ("text/html" in content_type or body.startswith("<!doctype html") or body.startswith("<html"))
        and ("just a moment" in body or "cloudflare" in body or "cf-browser-verification" in body)
    )


def cloudflare_retry_headers(headers: dict[str, str], attempt: int) -> dict[str, str]:
    retry_headers = dict(headers)
    if attempt >= 2 and not any(key.lower() == "user-agent" for key in retry_headers):
        retry_headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        })
    return retry_headers


def fetch_provider_models(provider: dict[str, Any]) -> dict[str, Any]:
    base_url = str(provider.get("base_url") or provider.get("url") or "").strip().rstrip("/")
    api_key = str(provider.get("api_key") or provider.get("key") or "").strip()
    headers = provider.get("headers") or {}
    remove_headers = provider.get("remove_headers") or []
    trust_env_proxy = api_checks.coerce_bool(provider.get("trust_env_proxy"), api_checks.DEFAULT_TRUST_ENV_PROXY)
    auth_mode = normalize_auth_mode(provider.get("auth_mode"), str(provider.get("api_mode") or ""), str(provider.get("custom_endpoint") or ""))
    anthropic_version = str(provider.get("anthropic_version") or "2023-06-01")
    if not base_url or not api_key:
        raise ValueError("Base URL 和 API Key 必填")

    request_headers = api_checks.build_headers(api_key, None, headers, remove_headers, auth_mode=auth_mode, anthropic_version=anthropic_version)
    # /models 是 OpenAI 兼容的目录端点：new-api 等中转认 Authorization: Bearer，
    # 而 anthropic 模式的 build_headers 只发 x-api-key，会被判为“未提供令牌”。
    # 两个头都带上，兼容 new-api（认 Bearer）与 Anthropic 官方（认 x-api-key）。
    if auth_mode == "anthropic" and not any(k.lower() == "authorization" for k in request_headers):
        request_headers["Authorization"] = f"Bearer {api_key}"
    started = time.time()
    response: requests.Response | None = None
    last_exc: Exception | None = None
    with requests.Session() as session:
        session.headers.clear()
        session.trust_env = trust_env_proxy
        for attempt in range(4):
            try:
                response = session.get(f"{base_url}/models", headers=cloudflare_retry_headers(request_headers, attempt), timeout=(10, 25))
                last_exc = None
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                response = None
                if attempt < 3:
                    time.sleep(0.6 + attempt * 0.4)
                    continue
                break
            if response.status_code == 200 or not is_cloudflare_challenge(response):
                break
            time.sleep(0.6 + attempt * 0.4)
    elapsed_ms = int((time.time() - started) * 1000)
    if response is None:
        if last_exc is not None:
            raise ValueError(f"/models {request_exception_detail(last_exc)}")
        raise ValueError("/models 599 请求未执行")
    if response.status_code != 200:
        if is_cloudflare_challenge(response):
            raise ValueError(f"/models HTTP {response.status_code}: Cloudflare/WAF 挑战，请重试")
        detail = api_checks.redact_sensitive(" ".join((response.text or "").split()), 160)
        raise ValueError(f"/models HTTP {response.status_code}: {detail}")
    payload = response.json()
    raw_models = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        raise ValueError("/models 返回格式不是模型列表")
    models = []
    for item in raw_models:
        if isinstance(item, str):
            model_id = item
            meta = {}
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
            meta = item
        else:
            continue
        if not model_id:
            continue
        context = meta.get("context_length") or meta.get("max_model_len") or meta.get("max_tokens") or meta.get("max_context_length")
        owned_by = meta.get("owned_by") or meta.get("owner") or ""
        models.append({"id": model_id, "context_length": context, "owned_by": owned_by})
    deduped = {model["id"]: model for model in models}
    return {"models": sorted(deduped.values(), key=lambda item: item["id"]), "count": len(deduped), "latencyMs": elapsed_ms}


def request_exception_detail(exc: Exception, limit: int = 160) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        prefix = "408 连接超时"
    elif isinstance(exc, requests.exceptions.ReadTimeout):
        prefix = "408 读超时"
    elif isinstance(exc, requests.exceptions.Timeout):
        prefix = "408 超时"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        prefix = "599 连接失败"
    else:
        prefix = f"599 {type(exc).__name__}"
    detail = api_checks.redact_sensitive(str(exc), limit).strip()
    return f"{prefix}：{detail}" if detail else prefix


DASHBOARD_PAGE_PATHS = {
    "/",
    "/dashboard.html",
    "/stats",
    "/stats.html",
    "/config",
    "/aiproxy",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AiApiDashboard/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or "0")
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        accept = self.headers.get("Accept") or ""
        wants_html = "text/html" in accept.lower()
        try:
            # Keep the legacy browser entry points working: a normal browser
            # navigation to /config or /aiproxy should open the dashboard,
            # while the frontend's fetch('/config') request (Accept: */*) must
            # continue to receive the JSON configuration API below.
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/config")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/dashboard.html" or (
                path in {"/config", "/aiproxy"} and wants_html
            ):
                self.send_text(200, DASHBOARD_HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                return
            if path == "/stats.html" or (path == "/stats" and wants_html):
                self.send_text(200, STATS_HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                return
            if path == "/config/export":
                self.send_text(200, provider_yaml_text(load_provider_list()), "application/x-yaml; charset=utf-8")
                return
            if path == "/config":
                providers = load_provider_list()
                self.send_json(200, {
                    "providers": providers,
                    "publicProviders": [provider_public(p) for p in providers],
                    "configPath": str(active_config_file()),
                    "configFormat": active_config_file().suffix.lstrip("."),
                    "settings": load_app_settings(),
                })
                return
            if path == "/api/stats/config":
                self.send_json(200, stats_filter_options(parse_qs(split.query)))
                return
            if path == "/api/stats/summary":
                self.send_json(200, stats_summary(parse_qs(split.query)))
                return
            if path == "/api/stats/providers":
                self.send_json(200, {"items": stats_grouped("provider_name", parse_qs(split.query))})
                return
            if path == "/api/stats/models":
                self.send_json(200, {"items": stats_grouped("model", parse_qs(split.query))})
                return
            if path == "/api/stats/requests":
                self.send_json(200, {"items": stats_requests(parse_qs(split.query))})
                return
            if path == "/checkins":
                self.send_json(200, default_checkins(load_provider_list()))
                return
            if path == "/backups":
                self.send_json(200, {"items": list_config_backups()})
                return
            if path == "/app-configs/preview":
                query = parse_qs(split.query)
                proxy_base = (query.get("proxyBase") or [current_aiproxy_proxy_base()])[0]
                self.send_json(200, app_config_preview(load_provider_list(), proxy_base))
                return
            if path == "/app-configs/custom-providers":
                self.send_json(200, load_app_custom_providers())
                return
            if path == "/aiproxy":
                self.send_json(200, list_aiproxy_services())
                return
            if path == "/keepalive":
                query = parse_qs(split.query)
                proxy_base = (query.get("proxyBase") or [""])[0]
                self.send_json(200, keepalive_status_from_proxy(proxy_base))
                return
            if path == "/proxy-config":
                cfg = load_proxy_config(active_config_file())
                self.send_json(200, {
                    "listen": cfg["listen"],
                    "port": cfg["port"],
                    "providers": sorted(cfg["providers"].keys()),
                    "connectTimeout": DEFAULT_CONNECT_TIMEOUT,
                    "readTimeout": DEFAULT_READ_TIMEOUT,
                })
                return
            self.send_json(404, {"error": "not found"})
        except Exception as exc:
            self.send_json(500, {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)})

    def do_POST(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        try:
            payload = self.read_body()
            if path == "/config/parse":
                content = payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("content must be a YAML string")
                providers = parse_provider_yaml_text(content)
                self.send_json(200, {"providers": providers, "warnings": provider_config_warnings(providers), "configFormat": "yaml"})
                return
            if path == "/config":
                providers = payload.get("providers")
                if not isinstance(providers, list):
                    raise ValueError("providers must be an array")
                before_providers = load_provider_list()
                backup, warnings = save_provider_list(providers, str(payload.get("format") or "yaml"))
                after_providers = load_provider_list()
                try:
                    claude_functions = sync_claude_code_functions(after_providers)
                except Exception as exc:
                    claude_functions = {"ok": False, "error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}
                    warnings.append(f"Claude Code 函数同步失败：{claude_functions['error']}")
                try:
                    app_sync = auto_sync_app_configs(before_providers, after_providers)
                except Exception as exc:
                    app_sync = {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}
                    warnings.append(f"Codex 同步失败：{app_sync['error']}")
                restart = restart_after_config_write(before_providers, after_providers)
                self.send_json(200, {
                    "ok": True,
                    "backup": str(backup) if backup else "",
                    "warnings": warnings,
                    "claudeFunctions": claude_functions,
                    "appSync": app_sync,
                    "restart": restart,
                })
                return
            if path == "/config/pin":
                provider_name = str(payload.get("name") or "").strip()
                pinned = api_checks.coerce_bool(payload.get("pinned"), False)
                backup, warnings = save_provider_pin(provider_name, pinned, payload.get("pinnedAt"))
                self.send_json(200, {
                    "ok": True,
                    "backup": str(backup) if backup else "",
                    "warnings": warnings,
                    "pinOnly": True,
                    "restart": {
                        "ok": True,
                        "changedServices": [],
                        "skipped": "pin-only change; AIProxy was not restarted",
                    },
                })
                return
            if path == "/config/keepalive":
                provider_name = str(payload.get("name") or "").strip()
                update_enabled = "enabled" in payload
                parameter_keys = ("concurrency", "retryInterval", "interval")
                update_parameters = any(key in payload for key in parameter_keys)
                if update_parameters and not all(key in payload for key in parameter_keys):
                    raise ValueError("concurrency, retryInterval and interval are required together")
                backup, warnings, before_providers, after_providers, keepalive = save_provider_keepalive(
                    provider_name,
                    enabled=payload.get("enabled"),
                    update_enabled=update_enabled,
                    concurrency=payload.get("concurrency"),
                    retry_interval=payload.get("retryInterval"),
                    interval=payload.get("interval"),
                    update_parameters=update_parameters,
                )
                before_provider = next(
                    (
                        item
                        for item in before_providers
                        if str(item.get("name") or "").strip().lower() == provider_name.lower()
                    ),
                    {},
                )
                before_keepalive = provider_keepalive_settings(before_provider)
                enabled_changed = before_keepalive["enabled"] != keepalive["enabled"]
                parameters_changed = any(
                    before_keepalive[key] != keepalive[key]
                    for key in ("concurrency", "retryInterval", "interval")
                )

                claude_functions: dict[str, Any] | None = None
                app_sync: dict[str, Any] | None = None
                if enabled_changed:
                    try:
                        claude_functions = sync_claude_code_functions(after_providers)
                    except Exception as exc:
                        claude_functions = {
                            "ok": False,
                            "error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000),
                        }
                        warnings.append(f"Claude Code 函数同步失败：{claude_functions['error']}")
                    try:
                        app_sync = auto_sync_app_configs(before_providers, after_providers)
                    except Exception as exc:
                        app_sync = {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}
                        warnings.append(f"Codex 同步失败：{app_sync['error']}")

                if enabled_changed or (keepalive["enabled"] and parameters_changed):
                    restart = restart_after_config_write(before_providers, after_providers)
                elif parameters_changed:
                    restart = {
                        "ok": True,
                        "changedServices": [],
                        "skipped": "keepalive is disabled; parameters saved without restart",
                    }
                else:
                    restart = {
                        "ok": True,
                        "changedServices": [],
                        "skipped": "no effective keepalive changes",
                    }
                self.send_json(200, {
                    "ok": True,
                    "backup": str(backup) if backup else "",
                    "warnings": warnings,
                    "keepalive": keepalive,
                    "enabledChanged": enabled_changed,
                    "parametersChanged": parameters_changed,
                    "claudeFunctions": claude_functions,
                    "appSync": app_sync,
                    "restart": restart,
                })
                return
            if path == "/checkins":
                items = payload.get("items")
                if not isinstance(items, list):
                    raise ValueError("items must be an array")
                data = {"items": items, "updatedAt": now_iso()}
                with write_lock:
                    write_json_atomic(CHECKINS_FILE, data)
                self.send_json(200, data)
                return
            if path == "/checkins/confirm":
                provider_id = str(payload.get("providerId") or "").strip()
                if not provider_id:
                    raise ValueError("providerId is required")
                data = default_checkins(load_provider_list())
                for item in data["items"]:
                    if str(item.get("providerId")) == provider_id:
                        item["lastConfirmedAt"] = now_iso()
                data["updatedAt"] = now_iso()
                with write_lock:
                    write_json_atomic(CHECKINS_FILE, data)
                self.send_json(200, data)
                return
            if path == "/providers/models":
                provider = payload.get("provider")
                if not isinstance(provider, dict):
                    raise ValueError("provider is required")
                self.send_json(200, fetch_provider_models(provider))
                return
            if path == "/backups/restore":
                name = str(payload.get("name") or "").strip()
                backup = restore_config_backup(name)
                restart = restart_after_config_write()
                self.send_json(200, {"ok": True, "backup": str(backup) if backup else "", "restart": restart})
                return
            if path == "/app-configs/compact-percent":
                current_settings = load_app_settings()
                percent = normalize_auto_compact_percent(
                    payload.get(
                        "autoCompactPercent",
                        current_settings["autoCompactPercent"],
                    )
                )
                token_limit = normalize_auto_compact_token_limit(
                    payload.get(
                        "autoCompactTokenLimit",
                        current_settings["autoCompactTokenLimit"],
                    )
                )
                settings = save_app_settings(
                    {
                        "autoCompactPercent": percent,
                        "autoCompactTokenLimit": token_limit,
                    }
                )
                providers = [provider for provider in load_provider_list() if api_checks.coerce_bool(provider.get("enabled"), True)]
                proxy_base = str(payload.get("proxyBase") or current_aiproxy_proxy_base()).strip().rstrip("/")
                keepalive_base = current_keepalive_proxy_base().strip().rstrip("/")
                codex_existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
                codex_default = choose_codex_default_provider(providers, codex_existing)
                result = {
                    "proxyBase": proxy_base,
                    "settings": settings,
                    "codex": sync_codex_config(
                        providers,
                        proxy_base,
                        codex_default,
                        auto_compact_percent=percent,
                        keepalive_base=keepalive_base,
                        auto_compact_token_limit_value=token_limit,
                    ),
                    "claudeCode": sync_claude_code_functions(
                        providers,
                        proxy_base,
                        keepalive_base,
                        auto_compact_percent=percent,
                        auto_compact_token_limit_value=token_limit,
                    ),
                }
                self.send_json(200, {"ok": True, "settings": settings, "result": result})
                return
            if path == "/app-configs/sync":
                providers = [provider for provider in load_provider_list() if api_checks.coerce_bool(provider.get("enabled"), True)]
                proxy_base = str(payload.get("proxyBase") or current_aiproxy_proxy_base()).strip().rstrip("/")
                default_provider = str(payload.get("defaultProvider") or "").strip()
                targets = payload.get("targets") or ["codex"]
                if not isinstance(targets, list):
                    raise ValueError("targets must be an array")
                unsupported_targets = [str(target) for target in targets if str(target) != "codex"]
                if unsupported_targets:
                    raise ValueError("target must be codex")
                compact_percent = current_auto_compact_percent()
                compact_token_limit = current_auto_compact_token_limit()
                result: dict[str, Any] = {
                    "proxyBase": proxy_base,
                    "settings": {
                        "autoCompactPercent": compact_percent,
                        "autoCompactTokenLimit": compact_token_limit,
                    },
                }
                if "codex" in targets:
                    result["codex"] = sync_codex_config(
                        providers,
                        proxy_base,
                        default_provider,
                        auto_compact_percent=compact_percent,
                        auto_compact_token_limit_value=compact_token_limit,
                    )
                restart = restart_after_config_write()
                self.send_json(200, {"ok": True, "result": result, "restart": restart})
                return
            if path == "/app-configs/custom-providers":
                target = str(payload.get("target") or "").strip()
                items = payload.get("items")
                if not isinstance(items, list):
                    raise ValueError("items must be an array")
                if target == "codex":
                    backup = save_codex_custom_providers(items)
                else:
                    raise ValueError("target must be codex")
                self.send_json(200, {"ok": True, "backup": str(backup) if backup else "", "data": load_app_custom_providers()})
                return
            if path == "/aiproxy":
                item = payload.get("item")
                if not isinstance(item, dict):
                    raise ValueError("item is required")
                self.send_json(200, write_aiproxy_service(item))
                return
            if path == "/aiproxy/control":
                self.send_json(200, control_aiproxy_service(str(payload.get("id") or ""), str(payload.get("action") or "")))
                return
            if path == "/aiproxy/delete":
                self.send_json(200, delete_aiproxy_service(str(payload.get("id") or "")))
                return
            self.send_json(404, {"error": "not found"})
        except Exception as exc:
            self.send_json(400, {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)})


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Local dashboard for ai-api")
    parser.add_argument("--host", default=DEFAULT_LISTEN)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--public-host", default=DEFAULT_PUBLIC_HOST, help="Host name or IP shown for browser access")
    parser.add_argument("--open", action="store_true", help="open browser")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    listen_url = f"http://{args.host}:{args.port}/"
    access_url = f"http://{args.public_host}:{args.port}/"
    print(f"dashboard listening on {listen_url}")
    print(f"dashboard access URL: {access_url}")
    print(f"config: {active_config_file()}")
    sys.stdout.flush()
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(access_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
