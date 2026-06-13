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
CHECKINS_FILE = BASE_DIR / "checkins.json"
MONITOR_FILE = BASE_DIR / "monitor.json"
STATUS_FILE = BASE_DIR / "runtime-status.json"
HISTORY_FILE = BASE_DIR / "runtime-history.json"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
CODEX_CONFIG = Path("/root/.codex/config.toml")
CODEX_DIR = Path("/root/.codex")
CODEX_MODEL_CATALOG_DIR = CODEX_DIR / "model-catalogs"
HERMES_CONFIG = Path("/root/.hermes/config.yaml")
SYSTEMD_DIR = Path("/etc/systemd/system")
AIPROXY_SERVICE_PREFIX = "ai-api-proxy-"
AIPROXY_SYSTEMD_SERVICES = ("aiproxy.service",)
AIPROXY_INSTANCES_FILE = BASE_DIR / "aiproxy-instances.json"
PROXY_RESTART_LOG = BASE_DIR / "proxy-restart.log"
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

write_lock = threading.Lock()
aiproxy_http_probe_lock = threading.Lock()
aiproxy_http_probe_state: dict[str, dict[str, Any]] = {}

PROVIDER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def active_config_file() -> Path:
    return CONFIG_JSON_FILE if CONFIG_JSON_FILE.exists() else CONFIG_YAML_FILE


def list_config_backups() -> list[dict[str, Any]]:
    backups = []
    patterns = ["config.yaml.bak-*", "config.json.bak-*"]
    paths = []
    for pattern in patterns:
        paths.extend(BASE_DIR.glob(pattern))
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        backups.append({
            "name": path.name,
            "path": str(path),
            "size": stat.st_size,
            "createdAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds"),
        })
    return backups


def restore_config_backup(name: str) -> Path:
    safe_name = Path(name).name
    backup = BASE_DIR / safe_name
    if not (safe_name.startswith("config.yaml.bak-") or safe_name.startswith("config.json.bak-")) or not backup.exists():
        raise ValueError("backup not found")
    target = CONFIG_JSON_FILE if safe_name.startswith("config.json") else CONFIG_YAML_FILE
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
    mode = str(provider.get("api_mode") or "").strip() or "codex_responses"
    mode = {"responses": "codex_responses", "chat": "chat_completions"}.get(mode, mode)
    if mode not in {"codex_responses", "chat_completions", "custom_endpoint"}:
        raise ValueError(f"{label} api_mode must be codex_responses, chat_completions or custom_endpoint")
    provider["api_mode"] = mode
    custom_endpoint = str(provider.get("custom_endpoint") or provider.get("endpoint") or "").strip()
    if provider["api_mode"] == "custom_endpoint":
        if not custom_endpoint:
            raise ValueError(f"{label} custom_endpoint is required")
        if not custom_endpoint.startswith("/"):
            custom_endpoint = "/" + custom_endpoint
        provider["custom_endpoint"] = custom_endpoint
    else:
        provider.pop("custom_endpoint", None)
    provider.pop("endpoint", None)
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
        provider["models"] = {str(name).strip(): (meta if isinstance(meta, dict) else {}) for name, meta in models.items() if str(name).strip()}
    else:
        provider["models"] = {}
    return provider


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


def provider_config_warnings(providers: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for provider in providers:
        name = str(provider.get("name") or "未命名")
        if not str(provider.get("api_key") or provider.get("key") or "").strip():
            warnings.append(f"{name}: api_key 为空；仅在上游不需要鉴权时可忽略")
        models = provider.get("models") or {}
        if not isinstance(models, dict) or not models:
            warnings.append(f"{name}: models 为空；Codex/Hermes 可能无法选择模型")
    return warnings


def save_provider_list(providers: list[Any], fmt: str = "auto") -> tuple[Path | None, list[str]]:
    normalized = validate_provider_list(providers)
    warnings = provider_config_warnings(normalized)
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
                json.dump(normalized, f, ensure_ascii=False, indent=2)
                f.write("\n")
            else:
                yaml.safe_dump(normalized, f, allow_unicode=True, sort_keys=False)
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
        return endpoint or "/responses"
    if mode == "chat_completions":
        return "/chat/completions"
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


def write_codex_model_catalog(provider: dict[str, Any]) -> Path | None:
    name = str(provider.get("name") or "").strip()
    models = provider_model_names(provider)
    if not name or not models:
        return None
    catalog_path = CODEX_MODEL_CATALOG_DIR / f"{safe_codex_filename(name)}.json"
    payload = {"models": [codex_model_catalog_entry(provider, model, index) for index, model in enumerate(models)]}
    write_json_atomic(catalog_path, payload)
    return catalog_path


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
    catalog_deleted: list[str] = []
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
            catalog_path.unlink()
            catalog_deleted.append(str(catalog_path))
    return {"deleted": deleted, "backups": backups, "catalogDeleted": catalog_deleted}

def sync_codex_config(providers: list[dict[str, Any]], proxy_base: str, default_provider: str = "", stale_profile_names: set[str] | None = None) -> dict[str, Any]:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_MODEL_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    backup = backup_file(CODEX_CONFIG)
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
        context = model_meta.get("context_length") or model_meta.get("max_model_len") or model_meta.get("max_tokens")
        effort = model_meta.get("reasoning_effort") or provider.get("reasoning_effort") or ""
        catalog_path = write_codex_model_catalog(provider)
        if catalog_path:
            written_catalogs.append(str(catalog_path))
        profile_lines = [f"model = {toml_string(model)}", f"model_provider = {toml_string(name)}"]
        if context:
            profile_lines.append(f'model_context_window = {int(context)}')
        if effort:
            profile_lines.append(f"model_reasoning_effort = {toml_string(effort)}")
        if catalog_path:
            profile_lines.append(f"model_catalog_json = {toml_string(str(catalog_path))}")
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
    }

def is_proxy_generated_provider(item: Any, proxy_base: str) -> bool:
    if not isinstance(item, dict):
        return False
    base_url = str(item.get("base_url") or "").rstrip("/")
    proxy_prefix = proxy_base.rstrip("/") + "/" if proxy_base else ""
    return bool(proxy_prefix and base_url.startswith(proxy_prefix) and base_url.endswith("/v1"))


def hermes_custom_provider(provider: dict[str, Any], proxy_base: str) -> dict[str, Any]:
    name = str(provider.get("name") or "").strip()
    item: dict[str, Any] = {
        "name": name,
        "base_url": proxy_provider_url(name, proxy_base),
        "api_mode": provider_api_mode(provider),
    }
    models = provider.get("models")
    if models:
        item["models"] = models
    return item


def choose_hermes_default_provider(cfg: dict[str, Any], providers: list[dict[str, Any]]) -> str:
    names = {str(provider.get("name") or "").strip() for provider in providers if provider.get("name")}
    model = cfg.get("model") if isinstance(cfg, dict) else {}
    current = str((model or {}).get("provider") or "") if isinstance(model, dict) else ""
    if current in names:
        return current
    return str(providers[0].get("name") or "").strip() if providers else ""


def sync_app_configs_for_proxy_base(providers: list[dict[str, Any]], proxy_base: str, stale_names: set[str] | None = None) -> dict[str, Any]:
    enabled = [provider for provider in providers if api_checks.coerce_bool(provider.get("enabled"), True)]
    codex_existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    codex_default = choose_codex_default_provider(enabled, codex_existing)
    hermes_cfg: dict[str, Any] = {}
    if HERMES_CONFIG.exists():
        loaded = yaml.safe_load(HERMES_CONFIG.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            hermes_cfg = loaded
    hermes_default = choose_hermes_default_provider(hermes_cfg, enabled)
    return {
        "needed": True,
        "proxyBase": proxy_base,
        "codex": sync_codex_config(enabled, proxy_base, codex_default, stale_names),
        "hermes": sync_hermes_config(enabled, proxy_base, hermes_default),
    }


def sync_hermes_config(providers: list[dict[str, Any]], proxy_base: str, default_provider: str = "") -> dict[str, Any]:
    cfg: dict[str, Any]
    if HERMES_CONFIG.exists():
        with HERMES_CONFIG.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError("Hermes config root must be an object")
    backup = backup_file(HERMES_CONFIG)
    ai_names = {str(provider.get("name") or "").strip() for provider in providers if provider.get("name")}
    existing = cfg.get("custom_providers") or []
    if not isinstance(existing, list):
        existing = []
    preserved = [item for item in existing if not (isinstance(item, dict) and (str(item.get("name") or "") in ai_names or is_proxy_generated_provider(item, proxy_base)))]
    generated = [hermes_custom_provider(provider, proxy_base) for provider in providers if str(provider.get("name") or "").strip()]
    cfg["custom_providers"] = preserved + generated
    if default_provider:
        cfg.setdefault("model", {})
        if isinstance(cfg["model"], dict):
            cfg["model"]["provider"] = default_provider
            provider = next((p for p in providers if str(p.get("name")) == default_provider), None)
            if provider and first_model(provider):
                cfg["model"]["default"] = first_model(provider)
    HERMES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=HERMES_CONFIG.parent, delete=False) as f:
        tmp_name = f.name
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_name, HERMES_CONFIG)
    return {"target": str(HERMES_CONFIG), "backup": str(backup) if backup else "", "providers": len(generated), "preserved": len(preserved)}


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


def parse_hermes_chains() -> list[dict[str, Any]]:
    if not HERMES_CONFIG.exists():
        return []
    with HERMES_CONFIG.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    chains = []
    for item in cfg.get("custom_providers") or []:
        if not isinstance(item, dict):
            continue
        base_url = str(item.get("base_url") or "")
        match = re.search(r"/([^/]+)/v1/?$", base_url.rstrip("/"))
        if not match:
            continue
        provider_id = match.group(1)
        chains.append({
            "id": f"hermes-{provider_id}",
            "name": f"Hermes -> {provider_id}",
            "client": "hermes",
            "proxyId": "local-18006",
            "providerId": provider_id,
            "model": "",
            "enabled": True,
        })
    return chains


def merge_discovered_chains(configured: list[dict[str, Any]], providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    provider_lookup = {str(p.get("name")): p for p in providers}
    merged = {str(chain.get("id")): dict(chain) for chain in configured if chain.get("id")}
    for chain in parse_codex_chains() + parse_hermes_chains():
        provider = provider_lookup.get(str(chain.get("providerId")))
        if provider:
            chain["model"] = first_model(provider)
        merged.setdefault(str(chain.get("id")), chain)
    return list(merged.values())


def app_config_preview(providers: list[dict[str, Any]], proxy_base: str = "http://127.0.0.1:18006") -> dict[str, Any]:
    names = [str(provider.get("name") or "").strip() for provider in providers if provider.get("name")]
    return {
        "codex": {"target": str(CODEX_CONFIG), "exists": CODEX_CONFIG.exists(), "providers": len(names), "proxyBase": proxy_base},
        "hermes": {"target": str(HERMES_CONFIG), "exists": HERMES_CONFIG.exists(), "providers": len(names), "proxyBase": proxy_base},
        "providers": names,
        "discoveredChains": parse_codex_chains() + parse_hermes_chains(),
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
    try:
        hermes_items = {str(item.get("name") or "").strip(): str(item.get("base_url") or "").rstrip("/") for item in load_hermes_custom_providers()}
        if any(hermes_items.get(name) != url.rstrip("/") for name, url in expected.items()):
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


def load_hermes_config_data() -> dict[str, Any]:
    if not HERMES_CONFIG.exists():
        return {}
    with HERMES_CONFIG.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("Hermes config root must be an object")
    return cfg


def load_hermes_custom_providers() -> list[dict[str, Any]]:
    cfg = load_hermes_config_data()
    items = cfg.get("custom_providers") or []
    return [item for item in items if isinstance(item, dict)]


def save_hermes_custom_providers(items: list[dict[str, Any]]) -> Path | None:
    cfg = load_hermes_config_data()
    backup = backup_file(HERMES_CONFIG)
    cfg["custom_providers"] = items
    HERMES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=HERMES_CONFIG.parent, delete=False) as f:
        tmp_name = f.name
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_name, HERMES_CONFIG)
    return backup


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
        "hermes": {"target": str(HERMES_CONFIG), "items": load_hermes_custom_providers()},
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


def aiproxy_target_service(item: dict[str, Any]) -> str:
    requested = str(item.get("service") or item.get("id") or "aiproxy").strip()
    if requested.endswith(".service"):
        requested = requested[:-len(".service")]
    service_base = normalize_service_id(requested or "aiproxy")
    return f"{service_base}.service"


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


def write_aiproxy_service(item: dict[str, Any]) -> dict[str, Any]:
    target_service = aiproxy_target_service(item)
    service_id = normalize_service_id(target_service[:-len(".service")])
    old_service = str(item.get("oldService") or item.get("previousService") or "").strip()
    if old_service:
        if old_service.endswith(".service"):
            old_service = old_service[:-len(".service")]
        old_service = f"{normalize_service_id(old_service)}.service"

    item = {**item, "id": service_id, "name": str(item.get("name") or service_id), "service": target_service}
    result_ops: dict[str, Any] = {}

    if old_service and old_service != target_service:
        stop_code, stop_output = run_systemctl(["stop", old_service])
        disable_code, disable_output = run_systemctl(["disable", old_service])
        old_path = SYSTEMD_DIR / old_service
        old_backup = backup_file(old_path)
        if old_path.exists():
            old_path.unlink()
        result_ops["oldService"] = {
            "service": old_service,
            "stopped": stop_code == 0,
            "stopOutput": stop_output,
            "disabled": disable_code == 0,
            "disableOutput": disable_output,
            "backup": str(old_backup) if old_backup else "",
            "removedUnit": not old_path.exists(),
        }

    path = aiproxy_target_unit_path(item)
    backup = backup_file(path)
    path.write_text(aiproxy_unit_content(item), encoding="utf-8")
    reload_code, reload_output = run_systemctl(["daemon-reload"])
    enable_code, enable_output = run_systemctl(["enable", target_service])
    restart_code, restart_output = run_systemctl(["restart", target_service])

    data = load_aiproxy_instances()
    items = [
        entry for entry in data.get("items", [])
        if str(entry.get("service") or "") not in {target_service, old_service}
        and normalize_service_id(str(entry.get("id") or "")) != service_id
    ]
    items.append(item)
    save_aiproxy_instances(items)

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
    status.update(result_ops)
    return status


def control_aiproxy_service(service_id: str, action: str) -> dict[str, Any]:
    service_id = normalize_service_id(service_id)
    if action not in {"start", "stop", "restart", "enable", "disable"}:
        raise ValueError("unsupported action")
    item = find_aiproxy_service_item(service_id) or {"id": service_id, "service": f"{service_id}.service"}
    name = str(item.get("service") or f"{service_id}.service")
    code, output = run_systemctl([action, name])
    status = aiproxy_status(item)
    status["returnCode"] = code
    status["output"] = output
    return status


def delete_aiproxy_service(service_id: str) -> dict[str, Any]:
    service_id = normalize_service_id(service_id)
    run_systemctl(["stop", service_name(service_id)])
    run_systemctl(["disable", service_name(service_id)])
    path = aiproxy_unit_path(service_id)
    backup = backup_file(path)
    if path.exists():
        path.unlink()
    run_systemctl(["daemon-reload"])
    data = load_aiproxy_instances()
    items = [entry for entry in data.get("items", []) if normalize_service_id(str(entry.get("id") or "")) != service_id]
    save_aiproxy_instances(items)
    return {"ok": True, "id": service_id, "backup": str(backup) if backup else ""}


def parse_cli_flag(args: list[str], name: str) -> str:
    prefix = f"{name}="
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return ""


def aiproxy_probe_url(item: dict[str, Any]) -> str:
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
        raw_detail = f"{type(exc).__name__}: {str(exc)[:120]}"
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
    data = load_aiproxy_instances()
    by_id: dict[str, dict[str, Any]] = {}
    for item in discover_aiproxy_unit_items():
        by_id[normalize_service_id(str(item.get("id") or ""))] = item
    for name in AIPROXY_SYSTEMD_SERVICES:
        item = named_aiproxy_service_item(name)
        if item:
            by_id[normalize_service_id(str(item.get("id") or ""))] = item
    for item in data.get("items", []):
        service_id = normalize_service_id(str(item.get("id") or ""))
        by_id[service_id] = {**by_id.get(service_id, {}), **item}
    return list(by_id.values())


def find_aiproxy_service_item(service_id: str) -> dict[str, Any] | None:
    raw = str(service_id or "").strip()
    normalized = normalize_service_id(raw)
    for item in merged_aiproxy_service_items():
        item_id = normalize_service_id(str(item.get("id") or ""))
        service = str(item.get("service") or "")
        if item_id == normalized or service == raw or service == f"{raw}.service":
            return item
    return None


def list_aiproxy_services() -> dict[str, Any]:
    data = load_aiproxy_instances()
    items = [aiproxy_status(item) for item in merged_aiproxy_service_items()]
    return {
        "items": items,
        "updatedAt": data.get("updatedAt", ""),
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


def summarize_restart_after_config_write(result: dict[str, Any]) -> dict[str, Any]:
    manual = result.get("manualProxy") if isinstance(result.get("manualProxy"), dict) else {}
    services = result.get("aiproxyServices") if isinstance(result.get("aiproxyServices"), dict) else {}
    matched = 0
    restarted = 0
    skipped = 0
    errors: list[str] = []

    if manual.get("error"):
        errors.append(str(manual.get("error")))
    matched += int(manual.get("matched") or 0)
    restarted += len(manual.get("started") or [])
    for item in manual.get("errors") or []:
        errors.append(str(item.get("error") or item))

    if services.get("error"):
        errors.append(str(services.get("error")))
    matched += int(services.get("matched") or 0)
    for item in services.get("items") or []:
        if not isinstance(item, dict):
            continue
        service = str(item.get("service") or item.get("id") or "AIProxy")
        if item.get("error"):
            errors.append(f"{service}: {item.get('error')}")
        elif item.get("restarted") is True:
            restarted += 1
        elif item.get("skipped"):
            skipped += 1
        elif item.get("returnCode") not in (None, 0):
            errors.append(f"{service}: systemctl restart 返回 {item.get('returnCode')}")

    ok = not errors
    applied = restarted > 0
    if errors:
        message = "AIProxy 自动重启失败，请到 AIProxy 页查看"
    elif applied:
        message = "AIProxy 已自动重启，配置已生效"
    elif matched:
        message = "AIProxy 当前未运行，配置已保存，启动后生效" if skipped else "未执行 AIProxy 重启，请到 AIProxy 页确认状态"
    else:
        message = "未发现正在运行的 AIProxy，配置已保存，启动后生效"

    result.update({
        "ok": ok,
        "applied": applied,
        "matched": matched,
        "restarted": restarted,
        "skipped": skipped,
        "errors": errors,
        "message": message,
    })
    return result


def restart_after_config_write() -> dict[str, Any]:
    config_path = active_config_file()
    result: dict[str, Any] = {"configPath": str(config_path)}
    try:
        result["manualProxy"] = restart_manual_proxy_processes(config_path)
    except Exception as exc:
        result["manualProxy"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        result["aiproxyServices"] = restart_aiproxy_services_for_config(config_path)
    except Exception as exc:
        result["aiproxyServices"] = {"error": f"{type(exc).__name__}: {exc}"}
    return summarize_restart_after_config_write(result)


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
    if not base_url or not api_key:
        raise ValueError("Base URL 和 API Key 必填")

    request_headers = api_checks.build_headers(api_key, None, headers, remove_headers)
    started = time.time()
    response: requests.Response | None = None
    with requests.Session() as session:
        session.headers.clear()
        session.trust_env = trust_env_proxy
        for attempt in range(4):
            response = session.get(f"{base_url}/models", headers=cloudflare_retry_headers(request_headers, attempt), timeout=(10, 25))
            if response.status_code == 200 or not is_cloudflare_challenge(response):
                break
            time.sleep(0.6 + attempt * 0.4)
    elapsed_ms = int((time.time() - started) * 1000)
    if response is None:
        raise ValueError("/models 请求未执行")
    if response.status_code != 200:
        if is_cloudflare_challenge(response):
            raise ValueError(f"/models HTTP {response.status_code}: Cloudflare/WAF 挑战，请重试")
        detail = " ".join((response.text or "").split())[:160]
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


HEALTH_UA_LABELS = {
    "空字符串": "空UA",
    "Chrome电脑": "Chrome",
}
HEALTH_UA_ORDER = ["Codex", "空UA", "Curl", "Chrome"]


def health_ua_profiles() -> list[tuple[str, Any]]:
    profiles = [(HEALTH_UA_LABELS.get(name, name), value) for name, value in api_checks.UA_PROFILES.values()]
    order = {label: index for index, label in enumerate(HEALTH_UA_ORDER)}
    return sorted((item for item in profiles if item[0] in order), key=lambda item: order[item[0]])


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
    if ua is None:
        return "自定义"

    ua_text = str(ua)
    if ua_text == "":
        return "空UA"
    if ua_text == "curl/8.0":
        return "Curl"
    if "Mozilla/5.0" in ua_text:
        return "Chrome"
    return "自定义"


HEALTH_ENDPOINT_CANDIDATES = [
    ("reasoning", "/responses", "reasoning"),
    ("responses", "/responses", "basic"),
    ("chat", "/chat/completions", "basic"),
]


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
    for endpoint_label, endpoint, variant in HEALTH_ENDPOINT_CANDIDATES:
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
        result["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return result


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
        result["detail"] = "缺 proxy/provider/model"
        return result
    prompt = str(prompt or DEFAULT_MONITOR_PROMPT).strip() or DEFAULT_MONITOR_PROMPT
    if len(prompt) > 1000:
        prompt = prompt[:1000]
    result["prompt"] = prompt
    url = f"{proxy_url}/{provider_id}/v1/responses"
    payload = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "store": False,
        "max_output_tokens": 8,
    }
    try:
        resp = requests.post(url, json=payload, timeout=(5, 20))
        if resp.status_code in {404, 405, 501}:
            url = f"{proxy_url}/{provider_id}/v1/chat/completions"
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8}
            resp = requests.post(url, json=payload, timeout=(5, 20))
        result["latencyMs"] = int((time.time() - started) * 1000)
        result["alive"] = resp.status_code == 200
        result["detail"] = f"HTTP {resp.status_code}" if resp.status_code == 200 else f"HTTP {resp.status_code}: {' '.join((resp.text or '').split())[:160]}"
    except Exception as exc:
        result["latencyMs"] = int((time.time() - started) * 1000)
        result["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    result["health"] = classify_health(bool(result.get("alive")), result.get("latencyMs"))
    return result


def load_status() -> dict[str, Any]:
    return read_json(STATUS_FILE, {"providers": {}, "proxies": {}, "chains": {}, "updatedAt": ""})


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
        try:
            if split.path in {"/", "/dashboard.html"}:
                self.send_text(200, DASHBOARD_HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                return
            if split.path == "/api/config":
                providers = load_provider_list()
                self.send_json(200, {"providers": providers, "publicProviders": [provider_public(p) for p in providers], "configPath": str(active_config_file()), "configFormat": active_config_file().suffix.lstrip(".")})
                return
            if split.path == "/api/checkins":
                self.send_json(200, default_checkins(load_provider_list()))
                return
            if split.path == "/api/monitor":
                self.send_json(200, default_monitor(load_provider_list()))
                return
            if split.path == "/api/status":
                self.send_json(200, load_status())
                return
            if split.path == "/api/history":
                self.send_json(200, load_history())
                return
            if split.path == "/api/backups":
                self.send_json(200, {"items": list_config_backups()})
                return
            if split.path == "/api/app-configs/preview":
                query = parse_qs(split.query)
                proxy_base = (query.get("proxyBase") or [current_aiproxy_proxy_base()])[0]
                self.send_json(200, app_config_preview(load_provider_list(), proxy_base))
                return
            if split.path == "/api/app-configs/custom-providers":
                self.send_json(200, load_app_custom_providers())
                return
            if split.path == "/api/aiproxy/services":
                self.send_json(200, list_aiproxy_services())
                return
            if split.path == "/api/proxy-config":
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
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        split = urlsplit(self.path)
        try:
            payload = self.read_body()
            if split.path == "/api/config":
                providers = payload.get("providers")
                if not isinstance(providers, list):
                    raise ValueError("providers must be an array")
                before_providers = load_provider_list()
                backup, warnings = save_provider_list(providers, str(payload.get("format") or "auto"))
                after_providers = load_provider_list()
                try:
                    app_sync = auto_sync_app_configs(before_providers, after_providers)
                    app_sync.setdefault("ok", True)
                except Exception as exc:
                    app_sync = {"ok": False, "needed": True, "error": f"{type(exc).__name__}: {exc}"}
                restart = restart_after_config_write()
                self.send_json(200, {"ok": True, "backup": str(backup) if backup else "", "warnings": warnings, "appSync": app_sync, "restart": restart})
                return
            if split.path == "/api/checkins":
                items = payload.get("items")
                if not isinstance(items, list):
                    raise ValueError("items must be an array")
                data = {"items": items, "updatedAt": now_iso()}
                with write_lock:
                    write_json_atomic(CHECKINS_FILE, data)
                self.send_json(200, data)
                return
            if split.path == "/api/checkins/confirm":
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
            if split.path == "/api/monitor":
                proxies = payload.get("proxies")
                chains = payload.get("chains")
                if not isinstance(proxies, list) or not isinstance(chains, list):
                    raise ValueError("proxies and chains must be arrays")
                data = {"proxies": proxies, "chains": chains, "updatedAt": now_iso()}
                with write_lock:
                    write_json_atomic(MONITOR_FILE, data)
                self.send_json(200, data)
                return
            if split.path == "/api/health/check":
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
            if split.path == "/api/providers/models":
                provider = payload.get("provider")
                if not isinstance(provider, dict):
                    raise ValueError("provider is required")
                self.send_json(200, fetch_provider_models(provider))
                return
            if split.path == "/api/monitor/check":
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
            if split.path == "/api/backups/restore":
                name = str(payload.get("name") or "").strip()
                backup = restore_config_backup(name)
                restart = restart_after_config_write()
                self.send_json(200, {"ok": True, "backup": str(backup) if backup else "", "restart": restart})
                return
            if split.path == "/api/history/clear":
                self.send_json(200, clear_history())
                return
            if split.path == "/api/app-configs/sync":
                providers = [provider for provider in load_provider_list() if api_checks.coerce_bool(provider.get("enabled"), True)]
                proxy_base = str(payload.get("proxyBase") or current_aiproxy_proxy_base()).strip().rstrip("/")
                default_provider = str(payload.get("defaultProvider") or "").strip()
                targets = payload.get("targets") or ["codex", "hermes"]
                result: dict[str, Any] = {"proxyBase": proxy_base}
                if "codex" in targets:
                    result["codex"] = sync_codex_config(providers, proxy_base, default_provider)
                if "hermes" in targets:
                    result["hermes"] = sync_hermes_config(providers, proxy_base, default_provider)
                restart = restart_after_config_write()
                self.send_json(200, {"ok": True, "result": result, "restart": restart})
                return
            if split.path == "/api/app-configs/custom-providers":
                target = str(payload.get("target") or "").strip()
                items = payload.get("items")
                if not isinstance(items, list):
                    raise ValueError("items must be an array")
                if target == "codex":
                    backup = save_codex_custom_providers(items)
                elif target == "hermes":
                    backup = save_hermes_custom_providers(items)
                else:
                    raise ValueError("target must be codex or hermes")
                self.send_json(200, {"ok": True, "backup": str(backup) if backup else "", "data": load_app_custom_providers()})
                return
            if split.path == "/api/aiproxy/services":
                item = payload.get("item")
                if not isinstance(item, dict):
                    raise ValueError("item is required")
                self.send_json(200, write_aiproxy_service(item))
                return
            if split.path == "/api/aiproxy/services/control":
                self.send_json(200, control_aiproxy_service(str(payload.get("id") or ""), str(payload.get("action") or "")))
                return
            if split.path == "/api/aiproxy/services/delete":
                self.send_json(200, delete_aiproxy_service(str(payload.get("id") or "")))
                return
            self.send_json(404, {"error": "not found"})
        except Exception as exc:
            self.send_json(400, {"error": f"{type(exc).__name__}: {exc}"})


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
