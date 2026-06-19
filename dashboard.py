#!/usr/bin/env python3
"""Local web dashboard for provider config, manual check-ins and chain health."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shlex
import signal
import sys
sys.dont_write_bytecode = True
import subprocess
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
import yaml

import api as api_checks
from proxy import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT, load_config as load_proxy_config


BASE_DIR = Path(__file__).resolve().parent
CONFIG_YAML_FILE = BASE_DIR / "config.yaml"
CONFIG_JSON_FILE = BASE_DIR / "config.json"
CONFIG_FILE = CONFIG_JSON_FILE if CONFIG_JSON_FILE.exists() else CONFIG_YAML_FILE
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "log"
SETTINGS_FILE = DATA_DIR / "settings.json"
CHECKINS_FILE = DATA_DIR / "checkins.json"
MONITOR_FILE = DATA_DIR / "monitor.json"
STATUS_FILE = DATA_DIR / "runtime-status.json"
HISTORY_FILE = DATA_DIR / "runtime-history.json"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
CODEX_CONFIG = Path("/root/.codex/config.toml")
CODEX_DIR = Path("/root/.codex")
CODEX_MODEL_CATALOG_DIR = CODEX_DIR / "model-catalogs"
SYSTEMD_DIR = Path("/etc/systemd/system")
AIPROXY_SERVICE_PREFIX = "ai-api-proxy-"
AIPROXY_SYSTEMD_SERVICES = ("aiproxy.service",)
AIPROXY_SINGLE_SERVICE = "aiproxy.service"
AIPROXY_SINGLE_ID = "aiproxy"
AIPROXY_INSTANCES_FILE = DATA_DIR / "aiproxy-instances.json"
PROXY_RESTART_LOG = LOG_DIR / "proxy-restart.log"
DEFAULT_PROXY_BASE = "http://127.0.0.1:18006"
DEFAULT_MONITOR_PROMPT = "在吗？"
AIPROXY_HTTP_TRANSIENT_SECONDS = 12
AIPROXY_HTTP_FAILURE_THRESHOLD = 3

MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_LISTEN = "0.0.0.0"
DEFAULT_PUBLIC_HOST = "192.168.2.10"
DEFAULT_PORT = 18080
DEFAULT_DEGRADE_MS = 6000
MAX_HISTORY_ITEMS = 500
DEFAULT_AUTO_COMPACT_PERCENT = 70
MIN_AUTO_COMPACT_PERCENT = 1
MAX_AUTO_COMPACT_PERCENT = 95

write_lock = threading.Lock()
aiproxy_http_probe_lock = threading.Lock()
aiproxy_http_probe_state: dict[str, dict[str, Any]] = {}

PROVIDER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BACKUP_DIR = BASE_DIR / "backup"


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


def load_app_settings() -> dict[str, Any]:
    raw = read_json(SETTINGS_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    try:
        percent = normalize_auto_compact_percent(raw.get("autoCompactPercent"))
    except ValueError:
        percent = DEFAULT_AUTO_COMPACT_PERCENT
    return {"autoCompactPercent": percent}


def save_app_settings(settings: dict[str, Any]) -> dict[str, Any]:
    current = load_app_settings()
    if "autoCompactPercent" in settings:
        current["autoCompactPercent"] = normalize_auto_compact_percent(settings.get("autoCompactPercent"))
    write_json_atomic(SETTINGS_FILE, current)
    return current


def current_auto_compact_percent(value: Any = None) -> int:
    if value is not None:
        return normalize_auto_compact_percent(value)
    return int(load_app_settings().get("autoCompactPercent") or DEFAULT_AUTO_COMPACT_PERCENT)


def auto_compact_token_limit(context_window: int, percent: int) -> int:
    try:
        context = int(context_window)
    except (TypeError, ValueError):
        context = 0
    if context <= 0:
        context = 128000
    return max(1, int(context * normalize_auto_compact_percent(percent) / 100))


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
        target.write_bytes(backup.read_bytes())
    return current_backup or Path("")


def load_history() -> dict[str, Any]:
    return read_json(HISTORY_FILE, {"items": [], "updatedAt": ""})


def append_history(event: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with write_lock:
        history = load_history()
        existing = history.get("items", [])
        if not isinstance(existing, list):
            existing = []
        stamp = now_iso()
        for item in items:
            existing.insert(0, {"event": event, "at": stamp, "item": item})
        history = {"items": existing[:MAX_HISTORY_ITEMS], "updatedAt": stamp}
        write_json_atomic(HISTORY_FILE, history)


def clear_history() -> dict[str, Any]:
    data = {"items": [], "updatedAt": now_iso()}
    with write_lock:
        write_json_atomic(HISTORY_FILE, data)
    return data


def classify_health(alive: bool, latency_ms: int | None, threshold_ms: int = DEFAULT_DEGRADE_MS) -> str:
    if not alive:
        return "unavailable"
    if latency_ms is not None and latency_ms > threshold_ms:
        return "degraded"
    return "healthy"


def merge_failure_state(previous: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    failures = int(previous.get("consecutiveFailures") or 0) if isinstance(previous, dict) else 0
    if result.get("alive"):
        result["consecutiveFailures"] = 0
        result["lastSuccessAt"] = result.get("checkedAt")
        result["lastFailureAt"] = previous.get("lastFailureAt") if isinstance(previous, dict) else ""
    else:
        result["consecutiveFailures"] = failures + 1
        result["lastFailureAt"] = result.get("checkedAt")
        result["lastSuccessAt"] = previous.get("lastSuccessAt") if isinstance(previous, dict) else ""
    return result


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
    target = active_config_file()
    if fmt == "json":
        target = CONFIG_JSON_FILE
    elif fmt == "yaml":
        target = CONFIG_YAML_FILE
    with write_lock:
        backup = backup_file(target)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=BASE_DIR, delete=False) as f:
            tmp_name = f.name
            if target.suffix == ".json":
                json.dump(persisted, f, ensure_ascii=False, indent=2)
                f.write("\n")
            else:
                yaml.safe_dump(persisted, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_name, target)
    return backup, warnings


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


def default_monitor(providers: list[dict[str, Any]]) -> dict[str, Any]:
    existing = read_json(MONITOR_FILE, {})
    proxy = {"id": "local-aiproxy", "name": "AIProxy", "url": DEFAULT_PROXY_BASE, "enabled": True}
    try:
        preferred = preferred_aiproxy_item()
        if preferred:
            service = str(preferred.get("service") or "aiproxy.service")
            proxy = {
                "id": normalize_service_id(service[:-len(".service")] if service.endswith(".service") else service),
                "name": str(preferred.get("name") or service),
                "url": current_aiproxy_proxy_base(preferred),
                "enabled": True,
            }
    except Exception:
        pass
    chains: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for provider in providers:
        if not api_checks.coerce_bool(provider.get("enabled"), True):
            continue
        provider_id = str(provider.get("name") or "").strip()
        if not provider_id:
            continue
        models = provider_model_names(provider)
        if not models:
            continue
        provider_safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", provider_id).strip("-.") or "provider"
        for index, model in enumerate(models, start=1):
            model = str(model or "").strip()
            if not model:
                continue
            model_safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", model).strip("-.")[:80] or f"model-{index}"
            chain_id = f"provider-{provider_safe}-{model_safe}"
            if chain_id in seen_ids:
                chain_id = f"{chain_id}-{index}"
            seen_ids.add(chain_id)
            chains.append({
                "id": chain_id,
                "name": provider_id,
                "client": "",
                "proxyId": proxy["id"],
                "providerId": provider_id,
                "model": model,
                "enabled": True,
                "auto": True,
                "url": f"{proxy['url'].rstrip('/')}/{provider_id}/v1",
            })
    return {"proxies": [proxy], "chains": chains, "updatedAt": existing.get("updatedAt", ""), "mode": "auto"}


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


def provider_by_name() -> dict[str, dict[str, Any]]:
    return {str(provider.get("name")): provider for provider in load_provider_list()}


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


def generated_codex_provider_block(providers: list[dict[str, Any]], proxy_base: str) -> str:
    lines: list[str] = [CODEX_SYNC_START]
    for provider in providers:
        name = str(provider.get("name") or "").strip()
        if not name:
            continue
        lines.extend([
            f'[model_providers.{toml_string(name)}]',
            f'name = {toml_string(name)}',
            f'base_url = {toml_string(proxy_provider_url(name, proxy_base))}',
            f'wire_api = {toml_string(codex_wire_api(provider))}',
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
            while index < len(lines) and not lines[index].startswith("["):
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

def sync_codex_config(providers: list[dict[str, Any]], proxy_base: str, default_provider: str = "", stale_profile_names: set[str] | None = None, auto_compact_percent: int | None = None) -> dict[str, Any]:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_MODEL_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    backup = backup_file(CODEX_CONFIG)
    compact_percent = current_auto_compact_percent(auto_compact_percent)
    provider_names = {str(provider.get("name") or "").strip() for provider in providers if provider.get("name")}
    content = strip_codex_generated_blocks(existing, provider_names, proxy_base) + generated_codex_provider_block(providers, proxy_base)
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
        profile_lines.append(f'model_auto_compact_token_limit = {auto_compact_token_limit(context, compact_percent)}')
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
    }

def is_proxy_generated_provider(item: Any, proxy_base: str) -> bool:
    if not isinstance(item, dict):
        return False
    base_url = str(item.get("base_url") or "").rstrip("/")
    proxy_prefix = proxy_base.rstrip("/") + "/" if proxy_base else ""
    return bool(proxy_prefix and base_url.startswith(proxy_prefix) and base_url.endswith("/v1"))



def sync_app_configs_for_proxy_base(providers: list[dict[str, Any]], proxy_base: str, stale_names: set[str] | None = None, auto_compact_percent: int | None = None) -> dict[str, Any]:
    enabled = [provider for provider in providers if api_checks.coerce_bool(provider.get("enabled"), True)]
    codex_existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    codex_default = choose_codex_default_provider(enabled, codex_existing)
    compact_percent = current_auto_compact_percent(auto_compact_percent)
    return {
        "needed": True,
        "proxyBase": proxy_base,
        "settings": {"autoCompactPercent": compact_percent},
        "codex": sync_codex_config(enabled, proxy_base, codex_default, stale_names, compact_percent),
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
            "base_url": proxy_provider_url(name, proxy_base),
            "api_mode": provider_api_mode(provider),
            "first_model": first_model(provider),
            "models": provider.get("models") or {},
            "reasoning_effort": provider.get("reasoning_effort") or "",
        })
    return projection


def app_sync_needed(before: list[dict[str, Any]], after: list[dict[str, Any]], proxy_base: str = DEFAULT_PROXY_BASE) -> bool:
    return app_sync_projection(before, proxy_base) != app_sync_projection(after, proxy_base)


def app_configs_need_proxy_sync(providers: list[dict[str, Any]], proxy_base: str) -> bool:
    enabled = [provider for provider in providers if api_checks.coerce_bool(provider.get("enabled"), True) and str(provider.get("name") or "").strip()]
    if not enabled:
        return False
    expected = {str(provider.get("name") or "").strip(): proxy_provider_url(str(provider.get("name") or "").strip(), proxy_base) for provider in enabled}
    try:
        codex_items = {str(item.get("name") or "").strip(): str(item.get("base_url") or "").rstrip("/") for item in load_codex_custom_providers()}
        if any(codex_items.get(name) != url.rstrip("/") for name, url in expected.items()):
            return True
    except Exception:
        return True
    return False


def auto_sync_app_configs(before: list[dict[str, Any]], after: list[dict[str, Any]], proxy_base: str | None = None) -> dict[str, Any]:
    proxy_base = (proxy_base or current_aiproxy_proxy_base()).strip().rstrip("/")
    providers = [provider for provider in after if api_checks.coerce_bool(provider.get("enabled"), True)]
    if not app_sync_needed(before, after, proxy_base) and not app_configs_need_proxy_sync(providers, proxy_base):
        return {"needed": False, "reason": "no app config change", "proxyBase": proxy_base}
    before_names = {str(provider.get("name") or "").strip() for provider in before if provider.get("name")}
    after_names = {str(provider.get("name") or "").strip() for provider in providers if provider.get("name")}
    stale_names = before_names - after_names
    return sync_app_configs_for_proxy_base(providers, proxy_base, stale_names)


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
    exec_start = f'{sys.executable} {BASE_DIR / "proxy.py"} --config {config} --listen {listen} --port {port}{verbose}'
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
        "Restart=on-failure",
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


def default_aiproxy_item(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    source = overrides or {}
    port = int(source.get("port") or 18006)
    return {
        "id": AIPROXY_SINGLE_ID,
        "name": "AIProxy",
        "service": AIPROXY_SINGLE_SERVICE,
        "listen": str(source.get("listen") or "127.0.0.1"),
        "port": port,
        "url": str(source.get("url") or source.get("publicUrl") or "").strip().rstrip("/"),
        "config": str(source.get("config") or CONFIG_YAML_FILE),
        "verbose": api_checks.coerce_bool(source.get("verbose"), False),
    }


def ensure_single_aiproxy_service() -> dict[str, Any]:
    item = preferred_aiproxy_item() or default_aiproxy_item()
    item = default_aiproxy_item(item)
    unit_path = SYSTEMD_DIR / AIPROXY_SINGLE_SERVICE
    if not unit_path.exists():
        return write_aiproxy_service(item, restart=True)
    enabled_code, _enabled = run_systemctl(["is-enabled", AIPROXY_SINGLE_SERVICE])
    if enabled_code != 0:
        run_systemctl(["enable", AIPROXY_SINGLE_SERVICE])
    status = aiproxy_status(item)
    status["ensured"] = True
    return status


def write_aiproxy_service(item: dict[str, Any], restart: bool = True) -> dict[str, Any]:
    item = default_aiproxy_item(item)
    target_service = AIPROXY_SINGLE_SERVICE
    service_id = AIPROXY_SINGLE_ID

    path = SYSTEMD_DIR / target_service
    backup = backup_file(path)
    path.write_text(aiproxy_unit_content(item), encoding="utf-8")
    reload_code, reload_output = run_systemctl(["daemon-reload"])
    enable_code, enable_output = run_systemctl(["enable", target_service])
    if restart:
        restart_code, restart_output = run_systemctl(["restart", target_service])
    else:
        restart_code, restart_output = run_systemctl(["start", target_service])

    save_aiproxy_instances([item])

    status = aiproxy_status(item)
    status["backup"] = str(backup) if backup else ""
    status["daemonReload"] = {"returnCode": reload_code, "output": reload_output}
    status["enable"] = {"returnCode": enable_code, "output": enable_output}
    status["restart"] = {"returnCode": restart_code, "output": restart_output}
    status["returnCode"] = restart_code
    status["output"] = restart_output
    try:
        status["appSync"] = sync_app_configs_for_proxy_base(load_provider_list(), current_aiproxy_proxy_base(item))
    except Exception as exc:
        status["appSync"] = {"error": f"{type(exc).__name__}: {exc}", "proxyBase": current_aiproxy_proxy_base(item)}
    return status

def control_aiproxy_service(service_id: str, action: str) -> dict[str, Any]:
    if action not in {"start", "stop", "restart", "enable", "disable"}:
        raise ValueError("unsupported action")
    item = preferred_aiproxy_item() or default_aiproxy_item()
    item = default_aiproxy_item(item)
    code, output = run_systemctl([action, AIPROXY_SINGLE_SERVICE])
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
    return item


def discover_aiproxy_unit_items() -> list[dict[str, Any]]:
    items = []
    for path in sorted(SYSTEMD_DIR.glob(f"{AIPROXY_SERVICE_PREFIX}*.service")):
        items.append(aiproxy_item_from_unit(path))
    return items


def merged_aiproxy_service_items() -> list[dict[str, Any]]:
    configured = None
    data = load_aiproxy_instances()
    for item in data.get("items", []):
        if str(item.get("service") or "") == AIPROXY_SINGLE_SERVICE or normalize_service_id(str(item.get("id") or "")) == AIPROXY_SINGLE_ID:
            configured = item
            break
    discovered = named_aiproxy_service_item(AIPROXY_SINGLE_SERVICE)
    item = default_aiproxy_item({**(discovered or {}), **(configured or {})})
    return [item]



def find_aiproxy_service_item(service_id: str) -> dict[str, Any] | None:
    return merged_aiproxy_service_items()[0]



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
    item = ensure_single_aiproxy_service()
    items = [item]
    return {
        "items": items,
        "updatedAt": data.get("updatedAt", ""),
        "defaultConfig": str(CONFIG_YAML_FILE),
        "configFiles": aiproxy_config_files(),
        "summary": {
            "total": 1,
            "running": 1 if item.get("activeOk") else 0,
            "healthy": 1 if item.get("health") == "healthy" else 0,
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


def restart_manual_proxy_processes(config_path: Path) -> dict[str, Any]:
    matches = iter_matching_manual_proxy_processes(config_path)
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


def restart_aiproxy_services_for_config(config_path: Path) -> dict[str, Any]:
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


def restart_after_config_write() -> dict[str, Any]:
    config_path = active_config_file()
    result: dict[str, Any] = {"configPath": str(config_path)}
    result["configCheck"] = check_proxy_config_before_restart(config_path)
    if not result["configCheck"].get("ok"):
        result["ok"] = False
        result["skipped"] = "proxy config check failed; existing proxy processes were left unchanged"
        return result
    try:
        result["manualProxy"] = restart_manual_proxy_processes(config_path)
    except Exception as exc:
        result["manualProxy"] = {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}
    try:
        result["aiproxyServices"] = restart_aiproxy_services_for_config(config_path)
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


HEALTH_UA_PROFILE_IDS = [5, 6, 4, 2, 3]
HEALTH_UA_PROFILE_LABELS = {
    5: "Codex",
    6: "Claude Code",
    4: "Empty",
    2: "Curl",
    3: "Chrome",
}


def health_ua_profiles() -> list[tuple[str, Any]]:
    profiles: list[tuple[str, Any]] = []
    for profile_id in HEALTH_UA_PROFILE_IDS:
        profile = api_checks.UA_PROFILES.get(profile_id)
        if not profile:
            continue
        profiles.append((HEALTH_UA_PROFILE_LABELS[profile_id], profile[1]))
    return profiles


def header_value(headers: dict[str, Any], name: str) -> Any:
    target = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return value
    return None


def health_configured_ua_label(headers: dict[str, Any], remove_headers: list[Any]) -> str:
    originator = header_value(headers, "Originator")
    ua = header_value(headers, "User-Agent")
    if str(originator or "") == "codex_cli_rs":
        return "Codex"
    x_app = header_value(headers, "x-app")
    ua_text_lower = str(ua or "").lower()
    if str(x_app or "").lower() == "cli" or "claude-code/" in ua_text_lower or "claude-cli" in ua_text_lower:
        return "Claude Code"
    if ua is None:
        return "Default"

    ua_text = str(ua)
    if ua_text == "":
        return "Empty"
    if ua_text == "curl/8.0":
        return "Curl"
    if "Mozilla/5.0" in ua_text:
        return "Chrome"
    return "Custom"


HEALTH_ENDPOINT_CANDIDATES = [
    ("reasoning", "/responses", "reasoning"),
    ("responses", "/responses", "basic"),
    ("chat", "/chat/completions", "basic"),
    ("messages", "/messages", "basic"),
]


def health_endpoint_candidates(provider: dict[str, Any], model: str) -> list[tuple[str, str, str]]:
    endpoint = provider_endpoint(provider).lower()
    if endpoint == "/messages":
        return [("messages", "/messages", "basic")]
    return [item for item in HEALTH_ENDPOINT_CANDIDATES if item[1] != "/messages"]

def health_result_key(provider_id: str, model: str) -> str:
    return f"{provider_id}::{model}"


def run_provider_model_check(provider: dict[str, Any], model: str, all_ua: bool = False) -> dict[str, Any]:
    name = str(provider.get("name") or "")
    model = str(model or "").strip()
    base_url = str(provider.get("base_url") or provider.get("url") or "")
    api_key = str(provider.get("api_key") or provider.get("key") or "")
    headers = provider.get("headers") or {}
    remove_headers = provider.get("remove_headers") or []
    trust_env_proxy = api_checks.coerce_bool(provider.get("trust_env_proxy"), api_checks.DEFAULT_TRUST_ENV_PROXY)
    auth_mode = normalize_auth_mode(provider.get("auth_mode"), str(provider.get("api_mode") or ""), str(provider.get("custom_endpoint") or ""))
    anthropic_version = str(provider.get("anthropic_version") or "2023-06-01")
    ua_label = health_configured_ua_label(headers, remove_headers)
    started = time.time()
    result = {
        "resultKey": health_result_key(name, model),
        "providerId": name,
        "model": model,
        "checkedAt": now_iso(),
        "alive": False,
        "latencyMs": None,
        "status": "failed",
        "detail": "",
        "compact": "",
        "endpoint": "",
        "endpointLabel": "",
        "endpointVariant": "",
        "endpointTrace": [],
        "ua": ua_label,
        "uaMode": "configured",
        "uaResults": [],
    }

    def failed_ua_result(label: str, detail: str, endpoint_label: str = "") -> dict[str, Any]:
        compact = api_checks.compact_result("❌ 失败", detail, include_latency=True)
        return {
            "ua": label,
            "alive": False,
            "latencyMs": None,
            "status": "❌ 失败",
            "detail": detail,
            "compact": f"{endpoint_label} {compact}".strip() if endpoint_label else compact,
            "endpointLabel": endpoint_label,
        }

    if not base_url or not api_key or not model:
        detail = "缺 base_url/api_key/model"
        result.update({
            "status": "❌ 失败",
            "detail": detail,
            "compact": api_checks.compact_result("❌ 失败", detail, include_latency=True),
            "endpointLabel": "-",
            "uaResults": [failed_ua_result(ua_label, detail)],
        })
        return result

    profiles = health_ua_profiles() if all_ua else [(ua_label, None)]
    if all_ua and ua_label == "Custom":
        profiles = profiles + [("Custom", None)]
    inner_workers = api_checks.provider_inner_max_workers(name)

    def check_one(endpoint_label: str, endpoint: str, variant: str, ua_label: str, user_agent: Any) -> dict[str, Any]:
        ua_started = time.time()
        status, detail, compact = api_checks.check_endpoint_compact(
            base_url,
            api_key,
            model,
            endpoint,
            user_agent,
            variant=variant,
            provider_headers=headers,
            remove_headers=remove_headers,
            trust_env_proxy=trust_env_proxy,
            auth_mode=auth_mode,
            anthropic_version=anthropic_version,
        )
        return {
            "ua": ua_label,
            "alive": api_checks.is_success_result((status, detail)),
            "latencyMs": int((time.time() - ua_started) * 1000),
            "status": status,
            "detail": detail,
            "compact": compact,
            "endpoint": endpoint,
            "endpointLabel": endpoint_label,
            "endpointVariant": variant,
        }

    endpoint_trace: list[dict[str, Any]] = []
    last_attempt: dict[str, Any] | None = None
    for endpoint_label, endpoint, variant in health_endpoint_candidates(provider, model):
        if all_ua:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(inner_workers, len(profiles)) or 1) as executor:
                ua_results = list(executor.map(lambda item: check_one(endpoint_label, endpoint, variant, item[0], item[1]), profiles))
        else:
            ua_results = [check_one(endpoint_label, endpoint, variant, profiles[0][0], profiles[0][1])]

        successful = [item for item in ua_results if item["alive"]]
        attempt = {
            "endpointLabel": endpoint_label,
            "endpoint": endpoint,
            "endpointVariant": variant,
            "alive": bool(successful),
            "uaResults": ua_results,
        }
        endpoint_trace.append(attempt)
        last_attempt = attempt
        if successful:
            primary = successful[0]
            result.update({
                "alive": True,
                "latencyMs": int((time.time() - started) * 1000),
                "status": primary["status"],
                "detail": " | ".join(f"{item['ua']}: {item['detail']}" for item in ua_results),
                "compact": " | ".join(f"{item['ua']} {item['compact']}" for item in ua_results),
                "endpoint": endpoint,
                "endpointLabel": endpoint_label,
                "endpointVariant": variant,
                "endpointTrace": endpoint_trace,
                "ua": primary["ua"],
                "uaMode": "all" if all_ua else "configured",
                "uaResults": ua_results,
            })
            return result

    final_results = list((last_attempt or {}).get("uaResults") or [])
    primary = final_results[0] if final_results else failed_ua_result(ua_label, "测活失败")
    result.update({
        "alive": False,
        "latencyMs": int((time.time() - started) * 1000),
        "status": primary.get("status") or "❌ 失败",
        "detail": " | ".join(
            f"{attempt['endpointLabel']}: " + "; ".join(f"{item['ua']} {item['detail']}" for item in attempt.get("uaResults", []))
            for attempt in endpoint_trace
        ),
        "compact": primary.get("compact") or "❌ 失败",
        "endpoint": (last_attempt or {}).get("endpoint") or "",
        "endpointLabel": (last_attempt or {}).get("endpointLabel") or "",
        "endpointVariant": (last_attempt or {}).get("endpointVariant") or "",
        "endpointTrace": endpoint_trace,
        "ua": primary.get("ua") or ua_label,
        "uaMode": "all" if all_ua else "configured",
        "uaResults": final_results,
    })
    return result


def run_provider_check(provider: dict[str, Any], all_ua: bool = False, models: list[str] | None = None) -> list[dict[str, Any]]:
    model_names = [str(model or "").strip() for model in (models if models is not None else provider_model_names(provider))]
    model_names = [model for model in model_names if model]
    if not model_names:
        model_names = [""]
    return [run_provider_model_check(provider, model, all_ua) for model in model_names]

def check_proxy(proxy: dict[str, Any]) -> dict[str, Any]:
    url = str(proxy.get("url") or "").rstrip("/")
    started = time.time()
    result = {
        "proxyId": proxy.get("id"),
        "name": proxy.get("name") or proxy.get("id"),
        "url": url,
        "checkedAt": now_iso(),
        "alive": False,
        "latencyMs": None,
        "detail": "",
    }
    if not url:
        result["detail"] = "缺 url"
        return result
    try:
        resp = requests.get(url + "/", timeout=(2, 3))
        result["latencyMs"] = int((time.time() - started) * 1000)
        result["alive"] = resp.status_code in {200, 404}
        result["detail"] = f"HTTP {resp.status_code}"
    except Exception as exc:
        result["latencyMs"] = int((time.time() - started) * 1000)
        result["detail"] = api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 120)
    return result


def response_error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error") or payload.get("last_error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or error.get("code") or "").strip()
    if error:
        return str(error).strip()
    response = payload.get("response")
    if isinstance(response, dict):
        return response_error_message(response)
    return ""



def http_error_detail(response: requests.Response) -> str:
    base = api_checks.format_http_error(response.status_code, response)
    extra = ""
    try:
        payload = response.json()
        extra = response_error_message(payload)
        if not extra and isinstance(payload, dict):
            extra = str(payload.get("message") or payload.get("detail") or "").strip()
    except Exception:
        extra = api_checks.response_text(response)[:160]
    extra = api_checks.redact_sensitive(extra, 160).strip()
    if extra and extra not in base:
        return f"{base}：{extra}"
    return base


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


def check_responses_completed_stream(url: str, payload: dict[str, Any]) -> tuple[bool, str, int | None]:
    started = time.time()
    event_name = ""
    last_event = ""
    last_detail = ""
    event_count = 0
    completed = False
    response_status = ""
    try:
        with requests.post(url, json={**payload, "stream": True}, timeout=(5, 40), stream=True) as resp:
            if resp.status_code != 200:
                return False, http_error_detail(resp), int((time.time() - started) * 1000)
            content_type = str(resp.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                try:
                    body = resp.json()
                except Exception:
                    return False, "200 responses 未完成：非 SSE/非 JSON", int((time.time() - started) * 1000)
                response_status = str(body.get("status") or "") if isinstance(body, dict) else ""
                if isinstance(body, dict) and response_status == "completed":
                    return True, "200 response.completed", int((time.time() - started) * 1000)
                detail = response_error_message(body) or response_status or "未收到 response.completed"
                return False, f"200 responses 未完成：{detail}", int((time.time() - started) * 1000)

            for raw_line in resp.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = str(raw_line).strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                    last_event = event_name or last_event
                    continue
                if not line.startswith("data:"):
                    continue
                data = line.split(":", 1)[1].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                event_count += 1
                try:
                    item = json.loads(data)
                except Exception:
                    last_detail = api_checks.redact_sensitive(data, 120)
                    continue
                item_type = str(item.get("type") or event_name or "") if isinstance(item, dict) else event_name
                last_event = item_type or last_event
                if item_type == "response.completed":
                    completed = True
                    break
                if item_type in {"response.failed", "response.incomplete"}:
                    detail = response_error_message(item) or item_type
                    return False, f"200 responses 失败：{detail}", int((time.time() - started) * 1000)
                detail = response_error_message(item)
                if detail:
                    last_detail = detail
    except Exception as exc:
        return False, request_exception_detail(exc), int((time.time() - started) * 1000)

    elapsed = int((time.time() - started) * 1000)
    if completed:
        return True, "200 response.completed", elapsed
    if not event_count:
        return False, "200 responses 未完成：无流式事件", elapsed
    suffix = last_detail or last_event or "未收到 response.completed"
    return False, f"200 responses 未完成：{suffix}", elapsed


def check_chat_completed(url: str, payload: dict[str, Any]) -> tuple[bool, str, int | None]:
    started = time.time()
    try:
        resp = requests.post(url, json=payload, timeout=(5, 20))
        elapsed = int((time.time() - started) * 1000)
        if resp.status_code != 200:
            return False, http_error_detail(resp), elapsed
        try:
            body = resp.json()
        except Exception:
            return False, "200 chat 未完成：非 JSON", elapsed
        choices = body.get("choices") if isinstance(body, dict) else None
        if isinstance(choices, list) and choices:
            finish = str((choices[0] or {}).get("finish_reason") or "") if isinstance(choices[0], dict) else ""
            if finish and finish not in {"stop", "length"}:
                return False, f"200 chat 未完成：finish_reason={finish}", elapsed
            return True, "200 chat completed", elapsed
        detail = response_error_message(body) or "缺少 choices"
        return False, f"200 chat 未完成：{detail}", elapsed
    except Exception as exc:
        return False, request_exception_detail(exc), int((time.time() - started) * 1000)


def check_chain(chain: dict[str, Any], proxies: dict[str, dict[str, Any]], prompt: str = DEFAULT_MONITOR_PROMPT) -> dict[str, Any]:
    proxy = proxies.get(str(chain.get("proxyId")))
    proxy_url = str((proxy or {}).get("url") or "").rstrip("/")
    provider_id = str(chain.get("providerId") or "")
    model = str(chain.get("model") or "")
    started = time.time()
    result = {
        "chainId": chain.get("id"),
        "name": chain.get("name") or chain.get("id"),
        "client": chain.get("client") or "",
        "proxyId": chain.get("proxyId"),
        "providerId": provider_id,
        "model": model,
        "checkedAt": now_iso(),
        "alive": False,
        "latencyMs": None,
        "detail": "",
    }
    if not proxy_url or not provider_id or not model:
        result["detail"] = "400 缺 proxy/provider/model"
        return result
    prompt = str(prompt or DEFAULT_MONITOR_PROMPT).strip() or DEFAULT_MONITOR_PROMPT
    if len(prompt) > 1000:
        prompt = prompt[:1000]
    result["prompt"] = prompt
    responses_url = f"{proxy_url}/{provider_id}/v1/responses"
    responses_payload = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "store": False,
        "max_output_tokens": 8,
    }
    alive, detail, latency = check_responses_completed_stream(responses_url, responses_payload)
    endpoint = "responses"
    if not alive and (detail.startswith("404") or detail.startswith("405") or detail.startswith("501")):
        chat_url = f"{proxy_url}/{provider_id}/v1/chat/completions"
        chat_payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8}
        alive, detail, latency = check_chat_completed(chat_url, chat_payload)
        endpoint = "chat"
    result["latencyMs"] = latency if latency is not None else int((time.time() - started) * 1000)
    result["alive"] = bool(alive)
    result["endpoint"] = endpoint
    result["detail"] = detail if alive else api_checks.redact_sensitive(detail, 200)
    result["health"] = classify_health(bool(result.get("alive")), result.get("latencyMs"))
    return result


def load_status() -> dict[str, Any]:
    return read_json(STATUS_FILE, {"providers": {}, "proxies": {}, "chains": {}, "updatedAt": ""})


DASHBOARD_PAGE_PATHS = {"/", "/dashboard.html", "/config", "/health", "/aiproxy"}


def merge_status(kind: str, results: list[dict[str, Any]], key_name: str) -> dict[str, Any]:
    with write_lock:
        status = load_status()
        bucket = status.setdefault(kind, {})
        for result in results:
            item_key = str(result.get(key_name))
            previous = bucket.get(item_key, {}) if isinstance(bucket, dict) else {}
            result.setdefault("health", classify_health(bool(result.get("alive")), result.get("latencyMs")))
            bucket[item_key] = merge_failure_state(previous, result)
        status["updatedAt"] = now_iso()
        write_json_atomic(STATUS_FILE, status)
    append_history(kind, results)
    return status


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
            if path in {"/", "/dashboard.html"} or (path in DASHBOARD_PAGE_PATHS and wants_html):
                self.send_text(200, DASHBOARD_HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                return
            if path == "/config/export":
                self.send_text(200, provider_yaml_text(load_provider_list()), "application/x-yaml; charset=utf-8")
                return
            if path == "/config":
                providers = load_provider_list()
                self.send_json(200, {"providers": providers, "publicProviders": [provider_public(p) for p in providers], "configPath": str(active_config_file()), "configFormat": active_config_file().suffix.lstrip("."), "settings": load_app_settings()})
                return
            if path == "/checkins":
                self.send_json(200, default_checkins(load_provider_list()))
                return
            if path == "/monitor":
                self.send_json(200, default_monitor(load_provider_list()))
                return
            if path == "/status":
                self.send_json(200, load_status())
                return
            if path == "/history":
                self.send_json(200, load_history())
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
                    app_sync = auto_sync_app_configs(before_providers, after_providers)
                except Exception as exc:
                    app_sync = {"error": api_checks.redact_sensitive(f"{type(exc).__name__}: {exc}", 2000)}
                    warnings.append(f"Codex 同步失败：{app_sync['error']}")
                restart = restart_after_config_write()
                self.send_json(200, {"ok": True, "backup": str(backup) if backup else "", "warnings": warnings, "appSync": app_sync, "restart": restart})
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
            if path == "/monitor":
                proxies = payload.get("proxies")
                chains = payload.get("chains")
                if not isinstance(proxies, list) or not isinstance(chains, list):
                    raise ValueError("proxies and chains must be arrays")
                data = {"proxies": proxies, "chains": chains, "updatedAt": now_iso()}
                with write_lock:
                    write_json_atomic(MONITOR_FILE, data)
                self.send_json(200, data)
                return
            if path == "/health":
                providers = provider_by_name()
                names = payload.get("providers")
                if names is None:
                    names = list(providers.keys())
                if not isinstance(names, list):
                    raise ValueError("providers must be an array")
                all_ua = api_checks.coerce_bool(payload.get("allUa"), False)
                requested_models = payload.get("models")
                selected = [providers[name] for name in names if name in providers and api_checks.coerce_bool(providers[name].get("enabled"), True)]

                def models_for(provider: dict[str, Any]) -> list[str] | None:
                    if not isinstance(requested_models, dict):
                        return None
                    provider_name = str(provider.get("name") or "")
                    raw_models = requested_models.get(provider_name)
                    if raw_models is None:
                        return None
                    if isinstance(raw_models, str):
                        return [raw_models]
                    if isinstance(raw_models, list):
                        return [str(item) for item in raw_models]
                    return None

                max_workers = min(8, max(1, len(selected)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(run_provider_check, provider, all_ua, models_for(provider)) for provider in selected]
                    result_groups = [future.result() for future in futures]
                results = [result for group in result_groups for result in group]
                status = merge_status("providers", results, "resultKey") if results else load_status()
                self.send_json(200, {"results": results, "status": status, "allUa": all_ua})
                return
            if path == "/providers/models":
                provider = payload.get("provider")
                if not isinstance(provider, dict):
                    raise ValueError("provider is required")
                self.send_json(200, fetch_provider_models(provider))
                return
            if path == "/monitor/check":
                monitor = default_monitor(load_provider_list())
                requested_chain_ids = payload.get("chainIds")
                chain_id_filter: set[str] | None = None
                if requested_chain_ids is not None:
                    if not isinstance(requested_chain_ids, list):
                        raise ValueError("chainIds must be an array")
                    chain_id_filter = {str(item) for item in requested_chain_ids if str(item).strip()}
                chains = [c for c in monitor["chains"] if c.get("enabled", True) and (chain_id_filter is None or str(c.get("id")) in chain_id_filter)]
                prompt_default = str(payload.get("prompt") or DEFAULT_MONITOR_PROMPT)
                prompt_map_raw = payload.get("prompts") or {}
                prompt_map = prompt_map_raw if isinstance(prompt_map_raw, dict) else {}
                proxy_map = {str(p.get("id")): p for p in monitor["proxies"]}
                proxy_ids = {str(c.get("proxyId")) for c in chains if c.get("proxyId")}
                proxies = [p for p in monitor["proxies"] if p.get("enabled", True) and (chain_id_filter is None or str(p.get("id")) in proxy_ids)]
                proxy_results = [check_proxy(proxy) for proxy in proxies]
                chain_results = [check_chain(chain, proxy_map, str(prompt_map.get(str(chain.get("id"))) or prompt_default)) for chain in chains]
                merge_status("proxies", proxy_results, "proxyId")
                status = merge_status("chains", chain_results, "chainId")
                self.send_json(200, {"proxyResults": proxy_results, "chainResults": chain_results, "status": status, "requestedChainIds": sorted(chain_id_filter) if chain_id_filter is not None else None})
                return
            if path == "/backups/restore":
                name = str(payload.get("name") or "").strip()
                backup = restore_config_backup(name)
                restart = restart_after_config_write()
                self.send_json(200, {"ok": True, "backup": str(backup) if backup else "", "restart": restart})
                return
            if path == "/history/clear":
                self.send_json(200, clear_history())
                return
            if path == "/app-configs/compact-percent":
                percent = normalize_auto_compact_percent(payload.get("autoCompactPercent"))
                settings = save_app_settings({"autoCompactPercent": percent})
                providers = [provider for provider in load_provider_list() if api_checks.coerce_bool(provider.get("enabled"), True)]
                proxy_base = str(payload.get("proxyBase") or current_aiproxy_proxy_base()).strip().rstrip("/")
                codex_existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
                codex_default = choose_codex_default_provider(providers, codex_existing)
                result = {
                    "proxyBase": proxy_base,
                    "settings": settings,
                    "codex": sync_codex_config(providers, proxy_base, codex_default, auto_compact_percent=percent),
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
                result: dict[str, Any] = {"proxyBase": proxy_base, "settings": {"autoCompactPercent": compact_percent}}
                if "codex" in targets:
                    result["codex"] = sync_codex_config(providers, proxy_base, default_provider, auto_compact_percent=compact_percent)
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
