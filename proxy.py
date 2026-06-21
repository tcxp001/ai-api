#!/usr/bin/env python3
"""Multi-provider OpenAI-compatible header injection proxy.

Clients can point custom providers at local URLs such as:

    http://127.0.0.1:18006/provider-a/v1

This proxy reads the real upstream provider list from config.yaml, injects
per-provider API keys/headers, synthesizes /models from config metadata, and
forwards OpenAI-compatible requests to the upstream base_url.
"""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import queue
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
import yaml

DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "api-key"}
RESPONSES_TO_CHAT_FALLBACK_STATUSES = {404, 405, 501}
PROVIDER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VALID_API_MODES = {"", "codex_responses", "responses", "chat_completions", "messages", "custom_endpoint"}
VALID_AUTH_MODES = {"bearer", "anthropic"}
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
TOOL_SEARCH_PROXY_NAME = "tool_search"
CUSTOM_TOOL_INPUT_FIELD = "input"
CHAT_TOOL_NAME_MAX_LEN = 64
CUSTOM_TOOL_INPUT_DESCRIPTION = (
    "Raw string input for the original custom tool. Preserve formatting exactly "
    "and follow the original tool definition embedded in the description."
)
CUSTOM_TOOL_PRESERVED_METADATA_HEADING = "Original tool definition:"


def provider_auth_mode_for_endpoint(api_mode: str, custom_endpoint: str = "") -> str:
    mode = str(api_mode or "").strip().lower()
    endpoint = str(custom_endpoint or "").strip().lower()
    if mode == "messages" or (mode == "custom_endpoint" and endpoint == "/messages"):
        return "anthropic"
    return "bearer"


def normalize_auth_mode(value: Any, api_mode: str, custom_endpoint: str = "") -> str:
    default = provider_auth_mode_for_endpoint(api_mode, custom_endpoint)
    mode = str(value or default).strip().lower()
    if mode not in VALID_AUTH_MODES:
        raise ValueError("auth_mode must be bearer or anthropic")
    return mode


DEFAULT_POOL_MAXSIZE = 20
DEFAULT_CONNECT_TIMEOUT = 30
# Some client stream stale detectors kill local requests after ~120s without
# stream bytes. Keep the proxy read timeout slightly lower so the proxy closes
# upstream sockets first instead of leaving long-lived broken streams behind.
DEFAULT_READ_TIMEOUT = 115
TRANSPORT_RETRY_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n", ""}:
            return False
    return default


def _coerce_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _coerce_positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_models(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize model metadata from config into {model_id: metadata}."""
    if isinstance(raw, str) and raw.strip():
        return {raw.strip(): {}}
    if isinstance(raw, list):
        return {str(item).strip(): {} for item in raw if str(item).strip()}
    if not isinstance(raw, dict):
        return {}

    models: dict[str, dict[str, Any]] = {}
    for model_id, meta in raw.items():
        name = str(model_id or "").strip()
        if not name:
            continue
        normalized: dict[str, Any] = {}
        if isinstance(meta, dict):
            for key in ("context_length", "max_model_len", "max_tokens", "max_completion_tokens"):
                value = _coerce_positive_int(meta.get(key))
                if value is not None:
                    normalized[key] = value
            effort = meta.get("reasoning_effort") or meta.get("reasoning")
            if isinstance(effort, dict):
                effort = effort.get("effort")
            if isinstance(effort, str) and effort.strip():
                normalized["reasoning_effort"] = effort.strip()
        models[name] = normalized
    return models


def load_config_from_data(cfg: Any, source: str | Path = "<memory>") -> dict[str, Any]:
    if isinstance(cfg, dict) and isinstance(cfg.get("providers"), list):
        cfg = cfg["providers"]
    if not isinstance(cfg, list) or not cfg:
        raise ValueError(f"config root must be a non-empty provider list: {source}")

    normalized: dict[str, Any] = {
        "listen": "127.0.0.1",
        "port": 18006,
        "trust_env_proxy": False,
        "verbose": False,
        "providers": {},
    }
    for idx, entry in enumerate(cfg, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"provider #{idx} must be a mapping")
        if not _coerce_bool(entry.get("enabled"), True):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"provider #{idx} missing required field: name")
        if not PROVIDER_NAME_PATTERN.match(name):
            raise ValueError(
                f"provider {name!r} name must be a URL-safe path segment: "
                "letters, numbers, dot, underscore or hyphen; it must start with a letter or number"
            )
        base_url = str(entry.get("base_url") or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"provider {name!r} base_url must be http(s) URL")
        headers = entry.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError(f"provider {name!r} headers must be a mapping")
        remove_headers = entry.get("remove_headers") or []
        if not isinstance(remove_headers, list):
            raise ValueError(f"provider {name!r} remove_headers must be a list")
        models = normalize_models(entry.get("models") or entry.get("model"))
        provider_reasoning = entry.get("reasoning_effort") or entry.get("reasoning")
        if isinstance(provider_reasoning, dict):
            provider_reasoning = provider_reasoning.get("effort")
        api_mode = str(entry.get("api_mode") or "").strip()
        if api_mode not in VALID_API_MODES:
            raise ValueError(f"provider {name!r} api_mode must be one of: {', '.join(sorted(VALID_API_MODES - {''}))}")
        custom_endpoint = str(entry.get("custom_endpoint") or entry.get("endpoint") or "").strip()
        if custom_endpoint and not custom_endpoint.startswith("/"):
            custom_endpoint = "/" + custom_endpoint
        if custom_endpoint == "/message":
            raise ValueError(f"provider {name!r} custom_endpoint must be /messages, not /message")
        if api_mode == "custom_endpoint" and not custom_endpoint:
            raise ValueError(f"provider {name!r} custom_endpoint is required when api_mode is custom_endpoint")
        auth_mode = normalize_auth_mode(entry.get("auth_mode"), api_mode, custom_endpoint)
        anthropic_version = str(entry.get("anthropic_version") or DEFAULT_ANTHROPIC_VERSION).strip() or DEFAULT_ANTHROPIC_VERSION
        key = name.lower()
        if key in {existing.lower() for existing in normalized["providers"]}:
            raise ValueError(f"provider {name!r} duplicates an earlier provider name")
        normalized["providers"][name] = {
            "base_url": base_url,
            "api_key": str(entry.get("api_key") or entry.get("key") or "").strip(),
            "api_mode": api_mode,
            "custom_endpoint": custom_endpoint,
            "headers": {str(k): "" if v is None else str(v) for k, v in headers.items()},
            "remove_headers": {str(h).lower() for h in remove_headers},
            "auth_mode": auth_mode,
            "anthropic_version": anthropic_version,
            "trust_env_proxy": _coerce_bool(entry.get("trust_env_proxy"), normalized["trust_env_proxy"]),
            "pool_maxsize": _coerce_positive_int(entry.get("pool_maxsize")) or DEFAULT_POOL_MAXSIZE,
            "connect_timeout": _coerce_positive_float(entry.get("connect_timeout")) or DEFAULT_CONNECT_TIMEOUT,
            "read_timeout": _coerce_positive_float(entry.get("read_timeout")) or DEFAULT_READ_TIMEOUT,
            "models": models,
            "reasoning_effort": provider_reasoning.strip() if isinstance(provider_reasoning, str) and provider_reasoning.strip() else "",
            "fallback_responses_to_chat": _coerce_bool(entry.get("fallback_responses_to_chat"), True),
        }
    return normalized


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or []
    return load_config_from_data(cfg, path)


def _header_contains_token(headers: Any, header_name: str, token: str) -> bool:
    token = token.lower()
    for key, value in getattr(headers, "items", lambda: [])():
        if str(key).lower() != header_name.lower():
            continue
        parts = [part.strip().lower() for part in str(value or "").split(",")]
        if token in parts:
            return True
    return False


def should_discard_upstream_response(resp: Any) -> bool:
    """Return True when the requests.Session should not be returned to pool.

    Provider gateways often close or poison keep-alive sockets after 5xx/Cloudflare
    errors. Reusing those sessions causes repeated RemoteDisconnected,
    InvalidChunkLength, CLOSE-WAIT, and BrokenPipe errors in later client turns.
    """
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status >= 500:
        return True
    if status == 408:
        return True
    headers = getattr(resp, "headers", {}) or {}
    return _header_contains_token(headers, "connection", "close")


def redact_header(name: str, value: str) -> str:
    if name.lower() in SENSITIVE_HEADERS:
        return "<redacted>"
    return value


def compact_body(text: str, limit: int = 500) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def build_models_payload(provider_name: str, provider: dict[str, Any]) -> dict[str, Any]:
    data = []
    for model_id, meta in sorted(provider.get("models", {}).items()):
        item: dict[str, Any] = {
            "id": model_id,
            "object": "model",
            "created": 0,
            "owned_by": provider_name,
        }
        for key in ("context_length", "max_model_len", "max_tokens", "max_completion_tokens"):
            if key in meta:
                item[key] = meta[key]
        if "context_length" in meta:
            item.setdefault("max_model_len", meta["context_length"])
        if meta.get("reasoning_effort"):
            item["reasoning_effort"] = meta["reasoning_effort"]
        data.append(item)
    return {"object": "list", "data": data}


def configured_reasoning_effort(provider: dict[str, Any], body_json: dict[str, Any] | None) -> str:
    model = ""
    if isinstance(body_json, dict):
        model = str(body_json.get("model") or "").strip()
    model_meta = provider.get("models", {}).get(model, {}) if model else {}
    effort = ""
    if isinstance(model_meta, dict):
        effort = str(model_meta.get("reasoning_effort") or "").strip()
    if not effort:
        effort = str(provider.get("reasoning_effort") or "").strip()
    return effort


def maybe_apply_reasoning(provider: dict[str, Any], proxied_path: str, body: bytes | None) -> tuple[bytes | None, dict[str, Any] | None]:
    if not body:
        return body, None
    try:
        body_json = json.loads(body.decode("utf-8"))
    except Exception:
        return body, None
    if not isinstance(body_json, dict):
        return body, None

    route_path = proxied_path.split("?", 1)[0].rstrip("/") or "/"
    if route_path == "/responses":
        effort = configured_reasoning_effort(provider, body_json)
        if effort:
            if effort.lower() == "none":
                body_json.pop("reasoning", None)
            else:
                body_json["reasoning"] = {"effort": effort, "summary": "auto"}
                body_json.setdefault("include", ["reasoning.encrypted_content"])
    return json.dumps(body_json, ensure_ascii=False).encode("utf-8"), body_json



def canonical_json_string(value: Any) -> str:
    """Stable compact JSON string used for tool-call arguments."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonicalize_json_string_if_parseable(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return value
    try:
        return canonical_json_string(json.loads(trimmed))
    except Exception:
        return value


def canonicalize_tool_arguments(value: Any) -> str:
    """Normalize Responses/Chat tool arguments into a valid JSON string."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        if not value.strip():
            return "{}"
        return canonicalize_json_string_if_parseable(value)
    return canonical_json_string(value)


def is_openai_o_series(model: str) -> bool:
    model = str(model or "")
    return len(model) > 1 and model[0] == "o" and model[1].isdigit()


def supports_reasoning_effort(model: str) -> bool:
    model = str(model or "").lower()
    if is_openai_o_series(model):
        return True
    if model.startswith("gpt-"):
        rest = model[4:]
        return bool(rest and rest[0].isdigit() and rest[0] >= "5")
    return False


def _extract_reasoning_detail_part_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if not isinstance(value, dict):
        return None
    for key in ("text", "content", "summary"):
        text = value.get(key)
        if isinstance(text, str) and text:
            return text
    parts = value.get("parts")
    if isinstance(parts, list):
        joined = "\n\n".join(
            part
            for part in (_extract_reasoning_detail_part_text(item) for item in parts)
            if part
        )
        return joined or None
    return None


def _extract_reasoning_details_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, list):
        joined = "\n\n".join(
            part
            for part in (_extract_reasoning_detail_part_text(item) for item in value)
            if part
        )
        return joined or None
    if isinstance(value, dict):
        return _extract_reasoning_detail_part_text(value)
    return None


def extract_reasoning_field_text(value: Any) -> str | None:
    """Best-effort extraction for OpenAI-compatible reasoning fields."""
    if not isinstance(value, dict):
        return None
    for key in ("reasoning_content", "reasoning"):
        text = value.get(key)
        if isinstance(text, str) and text:
            return text
    reasoning = value.get("reasoning")
    if isinstance(reasoning, dict):
        for key in ("content", "text", "summary"):
            text = reasoning.get(key)
            if isinstance(text, str) and text:
                return text
    details = value.get("reasoning_details")
    if details is not None:
        return _extract_reasoning_details_text(details)
    return None


def extract_reasoning_summary_text(value: Any) -> str | None:
    """Extract Responses reasoning item summary text."""
    if not isinstance(value, dict):
        return None
    for key in ("reasoning_content", "content", "text"):
        text = value.get(key)
        if isinstance(text, str) and text:
            return text
    summary = value.get("summary")
    if isinstance(summary, str):
        return summary if summary else None
    if isinstance(summary, list):
        parts: list[str] = []
        for part in summary:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text:
                    parts.append(str(text))
            elif isinstance(part, str) and part:
                parts.append(part)
        joined = "\n\n".join(parts)
        return joined or None
    return None


def append_reasoning_content(message: dict[str, Any], reasoning: str | None) -> bool:
    reasoning = str(reasoning or "").strip()
    if not reasoning:
        return False
    existing = message.get("reasoning_content")
    if isinstance(existing, str) and existing.strip():
        message["reasoning_content"] = existing + "\n\n" + reasoning
    else:
        message["reasoning_content"] = reasoning
    return True


def attach_reasoning_content_field(item: dict[str, Any], reasoning: str | None) -> None:
    reasoning = str(reasoning or "").strip()
    if reasoning:
        item["reasoning_content"] = reasoning


THINK_OPEN_TAG = "<think>"
THINK_CLOSE_TAG = "</think>"


def split_leading_think_block(text: str) -> tuple[str, str] | None:
    """Split a leading <think>...</think> block into (reasoning, answer)."""
    text = str(text or "")
    leading_ws_len = len(text) - len(text.lstrip())
    after_ws = text[leading_ws_len:]
    if not after_ws.startswith(THINK_OPEN_TAG):
        return None
    body_start = leading_ws_len + len(THINK_OPEN_TAG)
    close_start = text.find(THINK_CLOSE_TAG, body_start)
    if close_start < 0:
        return None
    answer_start = close_start + len(THINK_CLOSE_TAG)
    return text[body_start:close_start].strip(), text[answer_start:].lstrip()


def strip_leading_think_open_tag(text: str) -> str | None:
    text = str(text or "")
    leading_ws_len = len(text) - len(text.lstrip())
    after_ws = text[leading_ws_len:]
    if after_ws.startswith(THINK_OPEN_TAG):
        return after_ws[len(THINK_OPEN_TAG):].strip()
    return None


def short_sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def flatten_namespace_tool_name(namespace: str, name: str) -> str:
    full_name = f"{namespace}__{name}"
    if len(full_name.encode("utf-8")) <= CHAT_TOOL_NAME_MAX_LEN:
        return full_name
    suffix = f"__{short_sha256_hex(full_name)}"
    prefix_limit = max(0, CHAT_TOOL_NAME_MAX_LEN - len(suffix.encode("utf-8")))
    prefix = ""
    for ch in full_name:
        if len((prefix + ch).encode("utf-8")) > prefix_limit:
            break
        prefix += ch
    return prefix + suffix


def new_codex_tool_context() -> dict[str, Any]:
    return {
        "chat_tools": [],
        "seen_chat_names": set(),
        "chat_name_to_spec": {},
        "namespace_name_to_chat_name": {},
    }


def responses_tool_name(tool: Any) -> str | None:
    if isinstance(tool, str):
        name = tool.strip()
        return name or None
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    name = None
    if isinstance(function, dict):
        name = function.get("name")
    if not name:
        name = tool.get("name")
    name = str(name or "").strip()
    return name or None


def _tool_context_add_chat_tool(context: dict[str, Any], chat_name: str, spec: dict[str, Any], chat_tool: dict[str, Any]) -> None:
    chat_name = str(chat_name or "").strip()
    if not chat_name or chat_name in context["seen_chat_names"]:
        return
    context["seen_chat_names"].add(chat_name)
    namespace = spec.get("namespace")
    if namespace:
        context["namespace_name_to_chat_name"][(str(namespace), str(spec.get("name") or ""))] = chat_name
    context["chat_name_to_spec"][chat_name] = spec
    context["chat_tools"].append(chat_tool)


def chat_name_for_response_function(context: dict[str, Any] | None, name: str, namespace: str | None = None) -> str:
    name = str(name or "").strip()
    namespace = str(namespace or "").strip()
    if context and namespace:
        mapped = context.get("namespace_name_to_chat_name", {}).get((namespace, name))
        if mapped:
            return mapped
        return flatten_namespace_tool_name(namespace, name)
    return name


def responses_function_tool_to_chat_tool(tool: dict[str, Any], chat_name: str) -> dict[str, Any] | None:
    if tool.get("type") != "function":
        return None
    function = tool.get("function")
    if isinstance(function, dict):
        chat_function = dict(function)
        chat_function["name"] = chat_name
        if "strict" in tool and "strict" not in chat_function:
            chat_function["strict"] = tool.get("strict")
    else:
        chat_function = {
            "name": chat_name,
            "description": tool.get("description") or "",
            "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {},
        }
        if "strict" in tool:
            chat_function["strict"] = tool.get("strict")
    return {"type": "function", "function": chat_function}


def responses_custom_tool_description(tool: Any) -> str:
    return f"{CUSTOM_TOOL_PRESERVED_METADATA_HEADING}\n```json\n{canonical_json_string(tool)}\n```"


def _tool_context_add_function_tool(context: dict[str, Any], tool: dict[str, Any], namespace: str | None = None) -> None:
    original_name = responses_tool_name(tool)
    if not original_name:
        return
    chat_name = flatten_namespace_tool_name(namespace, original_name) if namespace else original_name
    chat_tool = responses_function_tool_to_chat_tool(tool, chat_name)
    if not chat_tool:
        return
    spec = {
        "kind": "namespace" if namespace else "function",
        "name": original_name,
        "namespace": namespace or "",
    }
    _tool_context_add_chat_tool(context, chat_name, spec, chat_tool)


def _tool_context_add_custom_tool(context: dict[str, Any], tool: Any) -> None:
    name = responses_tool_name(tool)
    if not name:
        return
    chat_tool = {
        "type": "function",
        "function": {
            "name": name,
            "description": responses_custom_tool_description(tool),
            "parameters": {
                "type": "object",
                "properties": {
                    CUSTOM_TOOL_INPUT_FIELD: {
                        "type": "string",
                        "description": CUSTOM_TOOL_INPUT_DESCRIPTION,
                    }
                },
                "required": [CUSTOM_TOOL_INPUT_FIELD],
            },
        },
    }
    _tool_context_add_chat_tool(context, name, {"kind": "custom", "name": name, "namespace": ""}, chat_tool)


def _tool_context_add_tool_search_tool(context: dict[str, Any]) -> None:
    chat_tool = {
        "type": "function",
        "function": {
            "name": TOOL_SEARCH_PROXY_NAME,
            "description": "Search and load Codex tools, plugins, connectors, and MCP namespaces for the current task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query for tools or connectors to load."},
                    "limit": {"type": "integer", "description": "Maximum number of tool groups to return."},
                },
                "required": ["query"],
            },
        },
    }
    _tool_context_add_chat_tool(
        context,
        TOOL_SEARCH_PROXY_NAME,
        {"kind": "tool_search", "name": TOOL_SEARCH_PROXY_NAME, "namespace": ""},
        chat_tool,
    )


def _tool_context_add_namespace_tool(context: dict[str, Any], tool: dict[str, Any]) -> None:
    namespace = str(tool.get("name") or "").strip()
    children = tool.get("tools") if isinstance(tool.get("tools"), list) else tool.get("children")
    if not namespace or not isinstance(children, list):
        return
    for child in children:
        if isinstance(child, dict) and child.get("type") == "function":
            _tool_context_add_function_tool(context, child, namespace=namespace)


def _tool_context_add_response_tool(context: dict[str, Any], tool: Any) -> None:
    if isinstance(tool, str):
        _tool_context_add_custom_tool(context, {"type": "custom", "name": tool})
        return
    if not isinstance(tool, dict):
        return
    tool_type = str(tool.get("type") or "")
    if tool_type == "function":
        _tool_context_add_function_tool(context, tool)
    elif tool_type == "custom":
        _tool_context_add_custom_tool(context, tool)
    elif tool_type == "tool_search":
        _tool_context_add_tool_search_tool(context)
    elif tool_type == "namespace":
        _tool_context_add_namespace_tool(context, tool)


def collect_tool_search_output_tools(value: Any, context: dict[str, Any]) -> None:
    if isinstance(value, list):
        for item in value:
            collect_tool_search_output_tools(item, context)
        return
    if not isinstance(value, dict):
        return
    tools = value.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            _tool_context_add_response_tool(context, tool)
    for child in value.values():
        if isinstance(child, (dict, list)):
            collect_tool_search_output_tools(child, context)


def build_codex_tool_context_from_request(body_json: dict[str, Any]) -> dict[str, Any]:
    context = new_codex_tool_context()
    tools = body_json.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            _tool_context_add_response_tool(context, tool)
    if "input" in body_json:
        collect_tool_search_output_tools(body_json.get("input"), context)
    return context


def responses_content_to_chat(content: Any) -> Any:
    """Normalize Responses API message content for Chat Completions providers.

    Text-only content is collapsed to a plain string. Multimodal parts are
    translated to OpenAI Chat content parts, preserving images, files and audio
    instead of forwarding Responses-only type names.
    """

    def normalize_part(part: Any) -> tuple[Any | None, str | None, bool]:
        if isinstance(part, str):
            return {"type": "text", "text": part}, part, True
        if not isinstance(part, dict):
            text = str(part)
            return {"type": "text", "text": text}, text, True

        part_type = str(part.get("type") or "").strip()
        if part_type in {"input_text", "output_text", "text"} or "text" in part:
            text = "" if part.get("text") is None else str(part.get("text"))
            return {"type": "text", "text": text}, text, True

        if part_type == "refusal":
            text = "" if part.get("refusal") is None else str(part.get("refusal"))
            return {"type": "text", "text": text}, text, True

        if part_type == "input_image":
            image_url = part.get("image_url") or part.get("url")
            if isinstance(image_url, dict):
                return {"type": "image_url", "image_url": image_url}, None, False
            if image_url:
                return {"type": "image_url", "image_url": {"url": str(image_url)}}, None, False
            return None, None, False

        if part_type == "input_file":
            file_obj: dict[str, Any] = {}
            for key in ("file_id", "file_data", "filename"):
                if part.get(key) is not None:
                    file_obj[key] = part.get(key)
            if file_obj and ("file_id" in file_obj or "file_data" in file_obj):
                return {"type": "file", "file": file_obj}, None, False
            return None, None, False

        if part_type == "input_audio":
            input_audio = part.get("input_audio")
            if input_audio is not None:
                return {"type": "input_audio", "input_audio": input_audio}, None, False
            return None, None, False

        # Unknown Responses-only blocks are more likely to break chat providers
        # than help. Drop them instead of forwarding invalid "type" values.
        return None, None, False

    if isinstance(content, list):
        parts: list[Any] = []
        text_parts: list[str] = []
        all_text = True
        for part in content:
            normalized, text, is_text = normalize_part(part)
            if normalized is not None:
                parts.append(normalized)
            if text is not None:
                text_parts.append(text)
            if not is_text:
                all_text = False
        if all_text:
            return "\n".join(text_parts)
        return parts if parts else ""

    if isinstance(content, dict):
        normalized, text, is_text = normalize_part(content)
        if is_text:
            return text or ""
        return [normalized] if normalized is not None else ""

    return content


def responses_role_to_chat_role(role: str) -> str:
    role = str(role or "user").strip() or "user"
    if role == "developer":
        return "system"
    if role not in {"system", "user", "assistant", "tool"}:
        return "user"
    return role


def responses_input_item_to_chat_message(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        role = responses_role_to_chat_role(str(item.get("role") or "user"))
        if "content" in item:
            content = responses_content_to_chat(item.get("content", ""))
        else:
            content = responses_content_to_chat(item)
        message: dict[str, Any] = {"role": role, "content": content}
        tool_call_id = item.get("tool_call_id") or item.get("call_id")
        if role == "tool" and tool_call_id:
            message["tool_call_id"] = str(tool_call_id)
        return message
    return {"role": "user", "content": responses_content_to_chat(item)}


def responses_function_call_to_chat_tool_call(item: dict[str, Any], tool_context: dict[str, Any] | None) -> dict[str, Any]:
    call_id = str(item.get("call_id") or item.get("id") or "call_0")
    name = str(item.get("name") or "")
    namespace = str(item.get("namespace") or "") or None
    chat_name = chat_name_for_response_function(tool_context, name, namespace)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": chat_name, "arguments": canonicalize_tool_arguments(item.get("arguments"))},
    }


def responses_custom_tool_call_to_chat_tool_call(item: dict[str, Any]) -> dict[str, Any]:
    call_id = str(item.get("call_id") or item.get("id") or "call_0")
    name = str(item.get("name") or "")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": canonical_json_string({CUSTOM_TOOL_INPUT_FIELD: item.get("input", "")}),
        },
    }


def responses_tool_search_call_to_chat_tool_call(item: dict[str, Any]) -> dict[str, Any]:
    call_id = str(item.get("call_id") or item.get("id") or "call_0")
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": TOOL_SEARCH_PROXY_NAME, "arguments": canonicalize_tool_arguments(item.get("arguments"))},
    }


def responses_tool_output_to_chat_content(item: dict[str, Any]) -> str:
    if item.get("type") in {"custom_tool_call_output", "tool_search_output"} and "output" not in item:
        return canonical_json_string(item)
    output = item.get("output") if "output" in item else item.get("content")
    if output is None:
        return ""
    if isinstance(output, str):
        return canonicalize_json_string_if_parseable(output)
    return canonical_json_string(output)


def append_responses_input_as_chat_messages(input_value: Any, messages: list[dict[str, Any]], tool_context: dict[str, Any] | None) -> None:
    pending_tool_calls: list[dict[str, Any]] = []
    pending_reasoning: list[str] = []
    last_assistant_index: int | None = None

    def append_pending_reasoning(reasoning: str | None, unique: bool = False) -> None:
        reasoning = str(reasoning or "").strip()
        if not reasoning:
            return
        if unique and any(reasoning in existing for existing in pending_reasoning):
            return
        pending_reasoning.append(reasoning)

    def take_pending_reasoning() -> str:
        text = "\n\n".join(part for part in pending_reasoning if str(part).strip()).strip()
        pending_reasoning.clear()
        return text

    def attach_pending_reasoning_to_assistant(message: dict[str, Any]) -> None:
        reasoning = take_pending_reasoning()
        if reasoning:
            append_reasoning_content(message, reasoning)

    def attach_reasoning_to_last_assistant(reasoning: str | None) -> bool:
        reasoning = str(reasoning or "").strip()
        if not reasoning:
            return True
        if last_assistant_index is None:
            return False
        if not (0 <= last_assistant_index < len(messages)):
            return False
        message = messages[last_assistant_index]
        if message.get("role") != "assistant":
            return False
        return append_reasoning_content(message, reasoning)

    def update_last_assistant_index(message: dict[str, Any]) -> None:
        nonlocal last_assistant_index
        role = message.get("role")
        if role == "assistant":
            last_assistant_index = len(messages)
        elif role == "tool":
            # Tool results belong to the preceding assistant tool-call turn; keep
            # the assistant index so a trailing reasoning item can still backfill.
            pass
        else:
            last_assistant_index = None

    def ensure_tool_call_reasoning_content(message: dict[str, Any]) -> None:
        if message.get("role") != "assistant":
            return
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            return
        existing = message.get("reasoning_content")
        if not (isinstance(existing, str) and existing.strip()):
            message["reasoning_content"] = "tool call"

    def flush_pending_tool_calls() -> None:
        nonlocal last_assistant_index
        if not pending_tool_calls:
            return
        message = {"role": "assistant", "content": None, "tool_calls": list(pending_tool_calls)}
        attach_pending_reasoning_to_assistant(message)
        last_assistant_index = len(messages)
        messages.append(message)
        pending_tool_calls.clear()

    def append_message(message: dict[str, Any]) -> None:
        if message.get("role") == "assistant":
            attach_pending_reasoning_to_assistant(message)
        elif pending_reasoning:
            # Reasoning items are assistant-side state. A user/system turn starts
            # a new segment, so do not leak stale reasoning across roles.
            pending_reasoning.clear()
        update_last_assistant_index(message)
        messages.append(message)

    def append_item(item: Any) -> None:
        if not isinstance(item, dict):
            flush_pending_tool_calls()
            append_message({"role": "user", "content": responses_content_to_chat(item)})
            return

        item_type = str(item.get("type") or "")
        if item_type == "function_call":
            append_pending_reasoning(extract_reasoning_field_text(item), unique=True)
            pending_tool_calls.append(responses_function_call_to_chat_tool_call(item, tool_context))
            return
        if item_type == "custom_tool_call":
            append_pending_reasoning(extract_reasoning_field_text(item), unique=True)
            pending_tool_calls.append(responses_custom_tool_call_to_chat_tool_call(item))
            return
        if item_type == "tool_search_call":
            append_pending_reasoning(extract_reasoning_field_text(item), unique=True)
            pending_tool_calls.append(responses_tool_search_call_to_chat_tool_call(item))
            return
        if item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            flush_pending_tool_calls()
            call_id = str(item.get("call_id") or item.get("id") or "")
            message = {"role": "tool", "tool_call_id": call_id, "content": responses_tool_output_to_chat_content(item)}
            update_last_assistant_index(message)
            messages.append(message)
            return
        if item_type == "reasoning":
            reasoning = extract_reasoning_summary_text(item)
            attached_to_previous = not pending_tool_calls and attach_reasoning_to_last_assistant(reasoning)
            if not attached_to_previous:
                append_pending_reasoning(reasoning)
            return

        flush_pending_tool_calls()
        if item_type in {"input_text", "input_image", "input_file", "input_audio"}:
            role = responses_role_to_chat_role(str(item.get("role") or "user"))
            append_message({"role": role, "content": responses_content_to_chat([item])})
            return
        if item.get("role") is not None or item.get("content") is not None:
            message = responses_input_item_to_chat_message(item)
            if message.get("role") == "assistant":
                append_pending_reasoning(extract_reasoning_field_text(item))
            append_message(message)

    if isinstance(input_value, list):
        for input_item in input_value:
            append_item(input_item)
    elif input_value is not None:
        append_item(input_value)
    flush_pending_tool_calls()
    for message in messages:
        ensure_tool_call_reasoning_content(message)


def collapse_chat_system_messages_to_head(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    system_chunks: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "system":
            rest.append(message)
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("text") is not None
            )
        if text.strip():
            system_chunks.append(text)
        elif content not in (None, "", []):
            rest.append(message)
    if not system_chunks:
        return rest
    return [{"role": "system", "content": "\n\n".join(system_chunks)}] + rest


def instruction_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, dict) and part.get("text") is not None:
                parts.append(str(part.get("text") or ""))
            elif part is not None:
                parts.append(str(part))
        return "\n".join(part for part in parts if part)
    return str(value or "")


def responses_tool_choice_to_chat(tool_choice: Any, tool_context: dict[str, Any] | None) -> Any:
    if isinstance(tool_choice, dict):
        tool_type = str(tool_choice.get("type") or "")
        if tool_type == "function":
            name = str(tool_choice.get("name") or "")
            namespace = str(tool_choice.get("namespace") or "") or None
            return {"type": "function", "function": {"name": chat_name_for_response_function(tool_context, name, namespace)}}
        if tool_type == "custom":
            return {"type": "function", "function": {"name": str(tool_choice.get("name") or "")}}
        if tool_type == "tool_search":
            return {"type": "function", "function": {"name": TOOL_SEARCH_PROXY_NAME}}
    return tool_choice


def inject_openai_stream_include_usage(chat: dict[str, Any]) -> None:
    if chat.get("stream") is not True:
        return
    stream_options = chat.get("stream_options") if isinstance(chat.get("stream_options"), dict) else {}
    stream_options = dict(stream_options)
    stream_options.setdefault("include_usage", True)
    chat["stream_options"] = stream_options


EXTRA_CHAT_PASSTHROUGH_FIELDS = (
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "metadata",
    "n",
    "parallel_tool_calls",
    "presence_penalty",
    "response_format",
    "seed",
    "service_tier",
    "stop",
    "stream_options",
    "top_logprobs",
    "user",
)


def responses_payload_to_chat(body_json: dict[str, Any], tool_context: dict[str, Any] | None = None) -> dict[str, Any]:
    if tool_context is None:
        tool_context = build_codex_tool_context_from_request(body_json)

    model_name = str(body_json.get("model") or "")
    chat: dict[str, Any] = {"model": body_json.get("model")}
    if body_json.get("max_output_tokens") is not None:
        if is_openai_o_series(model_name):
            chat["max_completion_tokens"] = body_json.get("max_output_tokens")
        else:
            chat["max_tokens"] = body_json.get("max_output_tokens")
    elif body_json.get("max_tokens") is not None:
        chat["max_tokens"] = body_json.get("max_tokens")
    if body_json.get("max_completion_tokens") is not None:
        chat["max_completion_tokens"] = body_json.get("max_completion_tokens")

    for key in ("stream", "temperature", "top_p"):
        if key in body_json:
            chat[key] = body_json.get(key)
    for key in EXTRA_CHAT_PASSTHROUGH_FIELDS:
        if key in body_json and key not in chat:
            chat[key] = body_json.get(key)

    reasoning = body_json.get("reasoning") if isinstance(body_json.get("reasoning"), dict) else None
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if isinstance(effort, str) and effort.strip() and supports_reasoning_effort(model_name):
        normalized_effort = effort.strip()
        if normalized_effort.lower() not in {"none", "off", "disabled"}:
            chat["reasoning_effort"] = normalized_effort

    messages: list[dict[str, Any]] = []
    instructions = instruction_text(body_json.get("instructions")) if body_json.get("instructions") is not None else ""
    if instructions:
        messages.append({"role": "system", "content": instructions})

    if "input" in body_json:
        append_responses_input_as_chat_messages(body_json.get("input"), messages, tool_context)
    messages = collapse_chat_system_messages_to_head(messages)
    if not messages:
        messages.append({"role": "user", "content": ""})
    chat["messages"] = messages

    tools = list(tool_context.get("chat_tools") or []) if isinstance(tool_context, dict) else []
    if tools:
        chat["tools"] = tools
    if "tool_choice" in body_json:
        chat["tool_choice"] = responses_tool_choice_to_chat(body_json.get("tool_choice"), tool_context)

    inject_openai_stream_include_usage(chat)

    has_tools = bool(chat.get("tools"))
    if not has_tools:
        chat.pop("tool_choice", None)
        chat.pop("parallel_tool_calls", None)

    return {k: v for k, v in chat.items() if v is not None}


def normalize_anthropic_content(value: Any) -> Any:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return value
    return str(value or "")


def normalize_anthropic_input_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    normalized = dict(schema)
    if normalized.get("type") == "object" and not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def anthropic_image_source_from_responses_image_url(image_url: Any) -> dict[str, Any] | None:
    if isinstance(image_url, dict):
        url = image_url.get("url") or image_url.get("image_url")
    else:
        url = image_url
    url = str(url or "").strip()
    if not url:
        return None
    if url.startswith("data:"):
        # data:<media_type>;base64,<data>
        try:
            meta, data = url[5:].split(",", 1)
            media_type, encoding = meta.split(";", 1)
        except ValueError:
            return None
        if encoding.lower() != "base64" or not media_type or not data:
            return None
        return {"type": "base64", "media_type": media_type, "data": data}
    if url.startswith(("http://", "https://")):
        # Newer Anthropic-compatible gateways may accept URL image sources.
        # Keeping this as a source block is more useful than silently dropping it.
        return {"type": "url", "url": url}
    return None


def responses_content_to_anthropic_blocks(content: Any, *, assistant: bool = False) -> list[dict[str, Any]]:
    """Convert Responses message content to Anthropic content blocks."""

    def convert_part(part: Any) -> list[dict[str, Any]]:
        if isinstance(part, str):
            return [{"type": "text", "text": part}]
        if not isinstance(part, dict):
            return [{"type": "text", "text": str(part)}]

        part_type = str(part.get("type") or "").strip()
        if part_type in {"input_text", "output_text", "text"} or "text" in part:
            return [{"type": "text", "text": "" if part.get("text") is None else str(part.get("text"))}]

        if not assistant and part_type == "input_image":
            source = anthropic_image_source_from_responses_image_url(part.get("image_url") or part.get("url"))
            return [{"type": "image", "source": source}] if source else []

        return []

    blocks: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            blocks.extend(convert_part(part))
    else:
        blocks.extend(convert_part(content))
    return blocks


def anthropic_blocks_to_content_value(blocks: list[dict[str, Any]]) -> Any:
    if not blocks:
        return ""
    return blocks


def anthropic_content_value_to_blocks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [dict(value)]
    if value is None:
        return []
    return [{"type": "text", "text": str(value)}]


def append_anthropic_message(messages: list[dict[str, Any]], role: str, content: Any) -> None:
    role = role if role in {"user", "assistant"} else "user"
    if messages and messages[-1].get("role") == role:
        combined = anthropic_content_value_to_blocks(messages[-1].get("content"))
        combined.extend(anthropic_content_value_to_blocks(content))
        messages[-1]["content"] = anthropic_blocks_to_content_value(combined)
        return
    messages.append({"role": role, "content": content})


def responses_content_to_system_text(content: Any) -> str:
    blocks = responses_content_to_anthropic_blocks(content)
    return "\n\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text" and block.get("text") is not None)


def parse_tool_arguments_object_or_wrapped(arguments: Any) -> dict[str, Any]:
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
            return {"input": parsed}
        except Exception:
            return {"input": arguments}
    return {"input": arguments}


def responses_tool_output_to_anthropic_content(item: dict[str, Any]) -> str:
    output = item.get("output") if "output" in item else item.get("content")
    if output is None:
        return "(empty)"
    if isinstance(output, str):
        return output if output else "(empty)"
    return canonical_json_string(output)


def responses_function_tool_to_anthropic_tool(tool: dict[str, Any], name: str) -> dict[str, Any] | None:
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    description = function.get("description") if function else tool.get("description")
    parameters = function.get("parameters") if function else tool.get("parameters")
    return {
        "name": name,
        "description": "" if description is None else str(description),
        "input_schema": normalize_anthropic_input_schema(parameters),
    }


def responses_custom_tool_to_anthropic_tool(tool: Any) -> dict[str, Any] | None:
    name = responses_tool_name(tool)
    if not name:
        return None
    return {
        "name": name,
        "description": responses_custom_tool_description(tool),
        "input_schema": {
            "type": "object",
            "properties": {
                CUSTOM_TOOL_INPUT_FIELD: {
                    "type": "string",
                    "description": CUSTOM_TOOL_INPUT_DESCRIPTION,
                }
            },
            "required": [CUSTOM_TOOL_INPUT_FIELD],
        },
    }


def responses_tool_search_to_anthropic_tool() -> dict[str, Any]:
    return {
        "name": TOOL_SEARCH_PROXY_NAME,
        "description": "Search and load Codex tools, plugins, connectors, and MCP namespaces for the current task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for tools or connectors to load."},
                "limit": {"type": "integer", "description": "Maximum number of tool groups to return."},
            },
            "required": ["query"],
        },
    }


def responses_tools_to_anthropic_tools(tools: Any, tool_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(tool: dict[str, Any] | None) -> None:
        if not isinstance(tool, dict):
            return
        name = str(tool.get("name") or "").strip()
        if not name or name in seen:
            return
        seen.add(name)
        out.append(tool)

    if not isinstance(tools, list):
        return out
    for tool in tools:
        if isinstance(tool, str):
            add(responses_custom_tool_to_anthropic_tool({"type": "custom", "name": tool}))
            continue
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type") or "")
        if tool_type == "function":
            name = responses_tool_name(tool)
            if name:
                add(responses_function_tool_to_anthropic_tool(tool, name))
        elif tool_type == "namespace":
            namespace = str(tool.get("name") or "").strip()
            children = tool.get("tools") if isinstance(tool.get("tools"), list) else tool.get("children")
            if namespace and isinstance(children, list):
                for child in children:
                    if isinstance(child, dict) and child.get("type") == "function":
                        child_name = responses_tool_name(child)
                        if child_name:
                            add(responses_function_tool_to_anthropic_tool(child, chat_name_for_response_function(tool_context, child_name, namespace)))
        elif tool_type == "custom":
            add(responses_custom_tool_to_anthropic_tool(tool))
        elif tool_type == "tool_search":
            add(responses_tool_search_to_anthropic_tool())
        elif tool_type in {"web_search", "web_search_preview", "web_search_20250305", "google_search"}:
            add({"type": "web_search_20250305", "name": "web_search"})
    return out


def responses_tool_choice_to_anthropic(tool_choice: Any, tool_context: dict[str, Any] | None = None) -> Any:
    if isinstance(tool_choice, str):
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice in {"auto", "none"}:
            return {"type": tool_choice}
        return tool_choice
    if isinstance(tool_choice, dict):
        tool_type = str(tool_choice.get("type") or "")
        if tool_type == "function":
            name = str(tool_choice.get("name") or "")
            namespace = str(tool_choice.get("namespace") or "") or None
            if not name and isinstance(tool_choice.get("function"), dict):
                name = str(tool_choice["function"].get("name") or "")
            if name:
                return {"type": "tool", "name": chat_name_for_response_function(tool_context, name, namespace)}
        if tool_type == "custom":
            name = str(tool_choice.get("name") or "")
            if name:
                return {"type": "tool", "name": name}
        if tool_type == "tool_search":
            return {"type": "tool", "name": TOOL_SEARCH_PROXY_NAME}
    return tool_choice


def responses_stop_to_anthropic_stop_sequences(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value] if value else None
    if isinstance(value, list):
        stops = [str(item) for item in value if str(item)]
        return stops or None
    return [str(value)] if str(value) else None


def anthropic_thinking_from_responses_reasoning(reasoning: Any, max_tokens: int) -> dict[str, Any] | None:
    if not isinstance(reasoning, dict):
        return None
    effort = str(reasoning.get("effort") or "").strip().lower()
    if not effort or effort == "low" or max_tokens <= 1024:
        return None
    default_budget = {"medium": 4096, "high": 10240, "xhigh": 32768, "max": 32768}.get(effort, 4096)
    budget = min(default_budget, max(1024, max_tokens // 2))
    if budget >= max_tokens:
        return None
    return {"type": "enabled", "budget_tokens": budget}


def responses_payload_to_anthropic_messages(body_json: dict[str, Any], tool_context: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": body_json.get("model")}
    if body_json.get("max_output_tokens") is not None:
        payload["max_tokens"] = body_json.get("max_output_tokens")
    elif body_json.get("max_tokens") is not None:
        payload["max_tokens"] = body_json.get("max_tokens")
    else:
        payload["max_tokens"] = 8192

    for key in ("stream", "temperature", "top_p"):
        if key in body_json:
            payload[key] = body_json.get(key)
    stop_sequences = responses_stop_to_anthropic_stop_sequences(body_json.get("stop"))
    if stop_sequences:
        payload["stop_sequences"] = stop_sequences
    if isinstance(body_json.get("metadata"), dict):
        payload["metadata"] = body_json.get("metadata")

    system_parts: list[str] = []
    instructions = body_json.get("instructions")
    if instructions:
        system_parts.append(instruction_text(instructions))

    messages: list[dict[str, Any]] = []
    input_value = body_json.get("input")

    def append_input_item(item: Any) -> None:
        if not isinstance(item, dict):
            append_anthropic_message(messages, "user", anthropic_blocks_to_content_value(responses_content_to_anthropic_blocks(item)))
            return

        item_type = str(item.get("type") or "")
        role = responses_role_to_chat_role(str(item.get("role") or "user"))
        if role == "system":
            text = responses_content_to_system_text(item.get("content", ""))
            if text:
                system_parts.append(text)
            return

        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "call_0")
            name = str(item.get("name") or "")
            namespace = str(item.get("namespace") or "") or None
            append_anthropic_message(messages, "assistant", [{
                "type": "tool_use",
                "id": call_id,
                "name": chat_name_for_response_function(tool_context, name, namespace),
                "input": parse_tool_arguments_object_or_wrapped(item.get("arguments")),
            }])
            return
        if item_type == "custom_tool_call":
            call_id = str(item.get("call_id") or item.get("id") or "call_0")
            append_anthropic_message(messages, "assistant", [{
                "type": "tool_use",
                "id": call_id,
                "name": str(item.get("name") or ""),
                "input": {CUSTOM_TOOL_INPUT_FIELD: str(item.get("input") or "")},
            }])
            return
        if item_type == "tool_search_call":
            call_id = str(item.get("call_id") or item.get("id") or "call_0")
            append_anthropic_message(messages, "assistant", [{
                "type": "tool_use",
                "id": call_id,
                "name": TOOL_SEARCH_PROXY_NAME,
                "input": parse_tool_arguments_object_or_wrapped(item.get("arguments")),
            }])
            return
        if item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            call_id = str(item.get("call_id") or item.get("id") or "")
            append_anthropic_message(messages, "user", [{
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": responses_tool_output_to_anthropic_content(item),
            }])
            return
        if item_type == "reasoning":
            return

        if item_type in {"input_text", "input_image", "input_file", "input_audio"}:
            append_anthropic_message(messages, role if role in {"user", "assistant"} else "user", anthropic_blocks_to_content_value(responses_content_to_anthropic_blocks([item], assistant=(role == "assistant"))))
            return

        content = item.get("content", item)
        append_anthropic_message(messages, role if role in {"user", "assistant"} else "user", anthropic_blocks_to_content_value(responses_content_to_anthropic_blocks(content, assistant=(role == "assistant"))))

    if isinstance(input_value, list):
        for item in input_value:
            append_input_item(item)
    elif input_value is not None:
        append_input_item(input_value)
    else:
        messages.append({"role": "user", "content": ""})

    if system_parts:
        payload["system"] = "\n\n".join(part for part in system_parts if part)
    payload["messages"] = messages or [{"role": "user", "content": ""}]

    anthropic_tools = responses_tools_to_anthropic_tools(body_json.get("tools"), tool_context)
    if anthropic_tools:
        payload["tools"] = anthropic_tools
    if "tool_choice" in body_json and anthropic_tools:
        payload["tool_choice"] = responses_tool_choice_to_anthropic(body_json.get("tool_choice"), tool_context)
    max_tokens_for_thinking = _coerce_positive_int(payload.get("max_tokens")) or 0
    thinking = anthropic_thinking_from_responses_reasoning(body_json.get("reasoning"), max_tokens_for_thinking)
    if thinking:
        payload["thinking"] = thinking
    return {k: v for k, v in payload.items() if v is not None}


def normalize_anthropic_messages_payload(body_json: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body_json)
    if payload.get("max_output_tokens") is not None and payload.get("max_tokens") is None:
        payload["max_tokens"] = payload.pop("max_output_tokens")
    payload.setdefault("max_tokens", 1024)
    system_parts: list[str] = []
    messages = payload.get("messages")
    if isinstance(messages, list):
        normalized_messages = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content", "")
            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue
            if role not in {"user", "assistant"}:
                role = "user"
            normalized_messages.append({"role": role, "content": normalize_anthropic_content(content)})
        payload["messages"] = normalized_messages or [{"role": "user", "content": ""}]
    if body_json.get("instructions"):
        system_parts.insert(0, str(body_json.get("instructions")))
        payload.pop("instructions", None)
    if system_parts:
        existing_system = payload.get("system")
        parts = [str(existing_system)] if existing_system else []
        parts.extend(system_parts)
        payload["system"] = "\n\n".join(part for part in parts if part)
    for key in ("store", "include", "reasoning", "frequency_penalty", "presence_penalty", "seed", "user"):
        payload.pop(key, None)
    return {k: v for k, v in payload.items() if v is not None}


def anthropic_content_blocks_from_value(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, dict):
        return [content]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def anthropic_payload_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in anthropic_content_blocks_from_value(payload.get("content")):
        if block.get("type") in {"text", "output_text"} or "text" in block:
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def default_error_type_for_status(status: int) -> str:
    if status in {400, 422}:
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 408:
        return "timeout_error"
    if status == 409:
        return "conflict_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "upstream_error"
    return "upstream_error"


def strip_html_error_text(text: str) -> str:
    text = str(text or "")
    if "<" not in text or ">" not in text:
        return text
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _error_field(value: Any, *keys: str) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if value.get(key) is not None:
            return value.get(key)
    return None


def extract_error_object_from_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = _error_field(error, "message", "detail", "error")
            error_type = _error_field(error, "type", "status")
            code = _error_field(error, "code")
            param = _error_field(error, "param")
            return {
                "message": str(message or ""),
                "type": str(error_type or "") if error_type is not None else "",
                "code": code,
                "param": param,
            }
        message = _error_field(payload, "message", "detail", "error")
        error_type = _error_field(payload, "type", "status")
        code = _error_field(payload, "code")
        param = _error_field(payload, "param")
        if message is not None or error_type is not None or code is not None:
            return {
                "message": str(message or ""),
                "type": str(error_type or "") if error_type is not None else "",
                "code": code,
                "param": param,
            }
    if isinstance(payload, str):
        return {"message": payload, "type": "", "code": None, "param": None}
    return {"message": "", "type": "", "code": None, "param": None}


def extract_error_from_sse_text(text: str) -> dict[str, Any] | None:
    data_parts: list[str] = []
    for line in str(text or "").splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                data_parts.append(data)
    for data in data_parts:
        try:
            parsed = json.loads(data)
        except Exception:
            continue
        extracted = extract_stream_error_object_from_chunk(parsed)
        if extracted.get("message") or extracted.get("type") or extracted.get("code") is not None:
            return extracted
    return None


def extract_stream_error_object_from_chunk(chunk: Any) -> dict[str, Any] | None:
    if not isinstance(chunk, dict):
        return None
    if isinstance(chunk.get("error"), (dict, str)):
        return extract_error_object_from_payload(chunk)
    if str(chunk.get("type") or "").strip().lower() == "error":
        return extract_error_object_from_payload(chunk)
    return None


def stream_error_to_openai_error(error_payload: Any, upstream_route_path: str = "") -> dict[str, Any]:
    extracted = extract_error_object_from_payload(error_payload)
    message = str(extracted.get("message") or "").strip()
    if not message:
        message = "stream error"
    message = compact_body(strip_html_error_text(message), limit=2000)
    source = upstream_route_path or "upstream"
    if not message.lower().startswith("upstream "):
        message = f"upstream {source} stream error: {message}"

    error_type = str(extracted.get("type") or "").strip() or "upstream_error"
    code = extracted.get("code")
    if code is None:
        code = "upstream_error"
    return {
        "message": message,
        "type": error_type,
        "param": extracted.get("param"),
        "code": code,
    }


def upstream_error_to_openai_error(resp: requests.Response, upstream_route_path: str = "") -> dict[str, Any]:
    status = int(getattr(resp, "status_code", 0) or 0)
    raw_text = ""
    try:
        raw_text = resp.text
    except Exception:
        try:
            raw_text = resp.content.decode("utf-8", errors="replace")
        except Exception:
            raw_text = ""

    extracted: dict[str, Any] | None = None
    content_type = str(resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" in content_type or "data:" in raw_text[:200]:
        extracted = extract_error_from_sse_text(raw_text)

    if extracted is None:
        try:
            extracted = extract_error_object_from_payload(resp.json())
        except Exception:
            extracted = extract_error_object_from_payload(strip_html_error_text(raw_text))

    message = str((extracted or {}).get("message") or "").strip()
    if not message:
        message = str(getattr(resp, "reason", "") or "").strip() or f"HTTP {status}"
    message = compact_body(strip_html_error_text(message), limit=2000)
    source = upstream_route_path or "upstream"
    if not message.lower().startswith("upstream "):
        message = f"upstream {source} returned {status}: {message}"

    error_type = str((extracted or {}).get("type") or "").strip() or default_error_type_for_status(status)
    code = (extracted or {}).get("code")
    if code is None:
        code = "upstream_error"
    param = (extracted or {}).get("param")
    return {
        "message": message,
        "type": error_type,
        "param": param,
        "code": code,
    }


def chat_usage_to_responses_usage(usage: Any) -> Any:
    if not isinstance(usage, dict):
        return usage
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    reasoning_tokens = int(
        output_details.get("reasoning_tokens")
        or completion_details.get("reasoning_tokens")
        or usage.get("reasoning_tokens")
        or 0
    )
    cached_tokens = int(
        input_details.get("cached_tokens")
        or prompt_details.get("cached_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cached_tokens")
        or 0
    )
    result: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }
    if cached_tokens:
        result["input_tokens_details"] = {"cached_tokens": cached_tokens}
    return result


def anthropic_stop_reason_to_responses_status(stop_reason: Any) -> str:
    if stop_reason == "max_tokens":
        return "incomplete"
    return "completed"


def messages_payload_to_responses(payload: dict[str, Any], model: str = "", tool_context: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(payload.get("choices"), list):
        return chat_payload_to_responses(payload, model=model, tool_context=tool_context)

    response_id = str(payload.get("id") or f"resp_{int(time.time() * 1000)}")
    response_model = model or str(payload.get("model") or "")
    created_at = int(payload.get("created_at") or payload.get("created") or time.time())
    status = anthropic_stop_reason_to_responses_status(payload.get("stop_reason"))
    usage = chat_usage_to_responses_usage(payload.get("usage"))

    output: list[dict[str, Any]] = []
    output_text_parts: list[str] = []
    pending_message_parts: list[dict[str, Any]] = []
    message_index = 0

    def flush_message() -> None:
        nonlocal message_index, pending_message_parts
        if not pending_message_parts:
            return
        output.append({
            "id": f"msg_{message_index}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": list(pending_message_parts),
        })
        message_index += 1
        pending_message_parts = []

    for block in anthropic_content_blocks_from_value(payload.get("content")):
        block_type = str(block.get("type") or "")
        if block_type in {"text", "output_text"} or "text" in block:
            text = str(block.get("text") or "")
            pending_message_parts.append({"type": "output_text", "text": text, "annotations": []})
            output_text_parts.append(text)
            continue
        if block_type == "thinking":
            flush_message()
            thinking = str(block.get("thinking") or block.get("text") or "")
            if thinking:
                output.append({
                    "id": f"rs_{len(output)}",
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": thinking}],
                })
            continue
        if block_type == "tool_use":
            flush_message()
            call_id = str(block.get("id") or f"call_{len(output)}")
            name = str(block.get("name") or "")
            arguments = canonicalize_tool_arguments(block.get("input"))
            item_id = response_tool_call_item_id_from_chat_name(call_id, name, tool_context)
            item = response_tool_call_item_from_chat_name(item_id, "completed", call_id, name, arguments, tool_context)
            output.append(item)
            continue

    flush_message()
    output_text = "".join(output_text_parts)
    if not output:
        output.append({
            "id": "msg_0",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        })

    response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": response_model,
        "output_text": output_text,
        "output": output,
        "usage": usage,
    }
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


def chat_payload_choice(chat_payload: dict[str, Any]) -> dict[str, Any]:
    choices = chat_payload.get("choices") if isinstance(chat_payload, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def chat_message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in {"text", "output_text"} or "text" in part:
                    parts.append(str(part.get("text") or ""))
                elif part.get("type") == "refusal":
                    parts.append(str(part.get("refusal") or ""))
            elif part is not None:
                parts.append(str(part))
        return "".join(parts)
    return str(content or "")


def chat_payload_text(chat_payload: dict[str, Any]) -> str:
    choice = chat_payload_choice(chat_payload)
    message = choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
    return chat_message_text(message)


def response_payload_from_text(response_id: str, model: str, text: str, usage: Any = None, status: str = "completed") -> dict[str, Any]:
    output = []
    if status == "completed":
        output = [
            {
                "id": "msg_0",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ]
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output_text": text if status == "completed" else "",
        "output": output,
        "usage": usage,
    }


def chat_reasoning_text(message: dict[str, Any]) -> str | None:
    reasoning = extract_reasoning_field_text(message)
    if reasoning:
        return reasoning
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        split = split_leading_think_block(content)
        if split and split[0]:
            return split[0]
    return None


def chat_reasoning_to_response_output_item(reasoning: str | None, response_id: str) -> dict[str, Any] | None:
    reasoning = str(reasoning or "").strip()
    if not reasoning:
        return None
    return {
        "id": f"rs_{response_id}",
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": reasoning}],
    }


def chat_message_to_response_output_item(message: dict[str, Any], item_id: str = "msg_0") -> tuple[dict[str, Any] | None, str]:
    content = message.get("content", "") if isinstance(message, dict) else ""
    response_parts: list[dict[str, Any]] = []
    text_parts: list[str] = []
    if isinstance(content, str):
        split = split_leading_think_block(content)
        text_content = split[1] if split else content
        if text_content:
            response_parts.append({"type": "output_text", "text": text_content, "annotations": []})
            text_parts.append(text_content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                part_type = str(part.get("type") or "")
                if part_type in {"text", "output_text"} or "text" in part:
                    text = str(part.get("text") or "")
                    if text:
                        response_parts.append({"type": "output_text", "text": text, "annotations": []})
                        text_parts.append(text)
                elif part_type == "refusal":
                    refusal = str(part.get("refusal") or "")
                    if refusal:
                        response_parts.append({"type": "refusal", "refusal": refusal})
            elif part is not None:
                text = str(part)
                if text:
                    response_parts.append({"type": "output_text", "text": text, "annotations": []})
                    text_parts.append(text)
    if isinstance(message, dict) and message.get("refusal"):
        response_parts.append({"type": "refusal", "refusal": str(message.get("refusal") or "")})
    if not response_parts:
        return None, ""
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": response_parts,
    }, "".join(text_parts)


def response_status_from_finish_reason(finish_reason: Any) -> str:
    if finish_reason == "length":
        return "incomplete"
    if finish_reason in {"content_filter", "error"}:
        return "failed"
    return "completed"


def tool_context_lookup(context: dict[str, Any] | None, chat_name: str) -> dict[str, Any] | None:
    if not context:
        return None
    spec = context.get("chat_name_to_spec", {}).get(chat_name)
    return spec if isinstance(spec, dict) else None


def parse_tool_arguments_object(arguments: str) -> dict[str, Any]:
    if not str(arguments or "").strip():
        return {}
    try:
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {"query": arguments}


def custom_tool_input_from_chat_arguments(arguments: str) -> str:
    if not str(arguments or "").strip():
        return ""
    try:
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            value = parsed.get(CUSTOM_TOOL_INPUT_FIELD)
            if isinstance(value, str):
                return value
    except Exception:
        pass
    return arguments


def response_tool_call_item_id_from_chat_name(call_id: str, chat_name: str, tool_context: dict[str, Any] | None = None) -> str:
    spec = tool_context_lookup(tool_context, chat_name)
    return f"ctc_{call_id}" if spec and spec.get("kind") == "custom" else f"fc_{call_id}"


def response_tool_call_item_from_chat_name(
    item_id: str,
    status: str,
    call_id: str,
    chat_name: str,
    arguments: str,
    tool_context: dict[str, Any] | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    spec = tool_context_lookup(tool_context, chat_name)
    if spec and spec.get("kind") == "custom":
        item = {
            "id": item_id,
            "type": "custom_tool_call",
            "status": status,
            "call_id": call_id,
            "name": str(spec.get("name") or chat_name),
            "input": custom_tool_input_from_chat_arguments(arguments),
        }
        attach_reasoning_content_field(item, reasoning)
        return item
    if spec and spec.get("kind") == "tool_search":
        item = {
            "type": "tool_search_call",
            "call_id": call_id,
            "status": status,
            "execution": "client",
            "arguments": parse_tool_arguments_object(arguments),
        }
        attach_reasoning_content_field(item, reasoning)
        return item
    item = {
        "id": item_id,
        "type": "function_call",
        "status": status,
        "call_id": call_id,
        "name": str((spec or {}).get("name") or chat_name),
        "arguments": arguments,
    }
    namespace = str((spec or {}).get("namespace") or "")
    if namespace:
        item["namespace"] = namespace
    attach_reasoning_content_field(item, reasoning)
    return item


def chat_tool_call_to_response_item(
    tool_call: dict[str, Any],
    index: int,
    tool_context: dict[str, Any] | None = None,
    status: str = "completed",
    reasoning: str | None = None,
) -> dict[str, Any] | None:
    call_id = str(tool_call.get("id") or f"call_{index}")
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    chat_name = str(function.get("name") or "")
    if not chat_name:
        return None
    arguments = canonicalize_tool_arguments(function.get("arguments"))
    item_id = response_tool_call_item_id_from_chat_name(call_id, chat_name, tool_context)
    return response_tool_call_item_from_chat_name(item_id, status, call_id, chat_name, arguments, tool_context, reasoning=reasoning)


def chat_legacy_function_call_to_response_item(
    function_call: dict[str, Any],
    tool_context: dict[str, Any] | None = None,
    reasoning: str | None = None,
) -> dict[str, Any] | None:
    name = str(function_call.get("name") or "")
    if not name:
        return None
    call_id = str(function_call.get("id") or "call_0")
    arguments = canonicalize_tool_arguments(function_call.get("arguments"))
    item_id = response_tool_call_item_id_from_chat_name(call_id, name, tool_context)
    return response_tool_call_item_from_chat_name(item_id, "completed", call_id, name, arguments, tool_context, reasoning=reasoning)


def chat_payload_to_responses(
    chat_payload: dict[str, Any],
    model: str = "",
    tool_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response_id = str(chat_payload.get("id") or f"resp_{int(time.time() * 1000)}")
    response_model = model or str(chat_payload.get("model") or "")
    created_at = int(chat_payload.get("created") or time.time())
    choice = chat_payload_choice(chat_payload)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    finish_reason = choice.get("finish_reason")
    status = response_status_from_finish_reason(finish_reason)

    output: list[dict[str, Any]] = []
    reasoning = chat_reasoning_text(message)
    reasoning_item = chat_reasoning_to_response_output_item(reasoning, response_id)
    if reasoning_item is not None:
        output.append(reasoning_item)

    message_item, output_text = chat_message_to_response_output_item(message, item_id="msg_0")
    if message_item is not None:
        output.append(message_item)

    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            item = chat_tool_call_to_response_item(
                tool_call,
                index,
                tool_context=tool_context,
                status="completed",
                reasoning=reasoning,
            )
            if item is not None:
                output.append(item)
    elif isinstance(message, dict) and isinstance(message.get("function_call"), dict):
        item = chat_legacy_function_call_to_response_item(
            message["function_call"],
            tool_context=tool_context,
            reasoning=reasoning,
        )
        if item is not None:
            output.append(item)

    response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": response_model,
        "output_text": output_text,
        "output": output,
        "usage": chat_usage_to_responses_usage(chat_payload.get("usage")),
    }
    if finish_reason == "length":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


class ProviderSessionPool:
    """Small per-provider Session pool so keep-alive survives across requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pools: dict[str, queue.LifoQueue[requests.Session]] = {}

    def _queue_for(self, provider_name: str, provider: dict[str, Any]) -> queue.LifoQueue[requests.Session]:
        with self._lock:
            if provider_name not in self._pools:
                maxsize = int(provider.get("pool_maxsize") or DEFAULT_POOL_MAXSIZE)
                self._pools[provider_name] = queue.LifoQueue(maxsize=maxsize)
            return self._pools[provider_name]

    def _new_session(self, provider: dict[str, Any]) -> requests.Session:
        maxsize = int(provider.get("pool_maxsize") or DEFAULT_POOL_MAXSIZE)
        adapter = HTTPAdapter(pool_connections=maxsize, pool_maxsize=maxsize, max_retries=0)
        session = requests.Session()
        session.headers.clear()
        session.trust_env = bool(provider.get("trust_env_proxy", False))
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def borrow(self, provider_name: str, provider: dict[str, Any]) -> requests.Session:
        pool = self._queue_for(provider_name, provider)
        try:
            return pool.get_nowait()
        except queue.Empty:
            return self._new_session(provider)

    def release(self, provider_name: str, provider: dict[str, Any], session: requests.Session) -> None:
        pool = self._queue_for(provider_name, provider)
        try:
            pool.put_nowait(session)
        except queue.Full:
            session.close()

    def discard(self, session: requests.Session) -> None:
        try:
            session.close()
        except Exception:
            pass

    def fresh(self, provider: dict[str, Any]) -> requests.Session:
        return self._new_session(provider)

    def close_all(self) -> None:
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            while True:
                try:
                    pool.get_nowait().close()
                except queue.Empty:
                    break


class HeaderProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler], config: dict[str, Any]):
        # Initialize fields before ThreadingHTTPServer.__init__ so server_close()
        # is safe even when bind() fails, for example during a port-conflict restart.
        self.config = config
        self.session_pool = ProviderSessionPool()
        super().__init__(server_address, RequestHandlerClass)

    def server_close(self) -> None:
        self.session_pool.close_all()
        super().server_close()

    def log_if_verbose(self, handler: BaseHTTPRequestHandler, fmt: str, *args: Any) -> None:
        if self.config.get("verbose"):
            handler.log_message(fmt, *args)

    def log_error_always(self, handler: BaseHTTPRequestHandler, fmt: str, *args: Any) -> None:
        handler.log_message(fmt, *args)


class ProxyHandler(BaseHTTPRequestHandler):
    server: HeaderProxyServer

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if self.server.config.get("verbose"):
            super().log_request(code, size)

    def _send_bytes(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()

    def _send_response_stream_headers(self, status: int = 200) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_sse_event(self, event: str, payload: dict[str, Any]) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(b"data: ")
        self.wfile.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.wfile.write(b"\n\n")
        self.wfile.flush()

    def _write_response_stream_start(self, response_id: str, model: str, start_text_item: bool = True) -> None:
        response = response_payload_from_text(response_id, model, "", status="in_progress")
        self._write_sse_event("response.created", {"type": "response.created", "response": response})
        self._write_sse_event("response.in_progress", {"type": "response.in_progress", "response": response})
        if not start_text_item:
            return
        self._write_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_0", "type": "message", "status": "in_progress", "role": "assistant", "content": []},
        })
        self._write_sse_event("response.content_part.added", {
            "type": "response.content_part.added",
            "item_id": "msg_0",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })

    def _write_response_stream_done(self, response_id: str, model: str, text: str, usage: Any = None, emit_text: bool = True) -> None:
        final_text = text if emit_text else ""
        part = {"type": "output_text", "text": final_text, "annotations": []}
        item = {"id": "msg_0", "type": "message", "status": "completed", "role": "assistant", "content": [part]}
        self._write_sse_event("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": "msg_0",
            "output_index": 0,
            "content_index": 0,
            "text": final_text,
        })
        self._write_sse_event("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": "msg_0",
            "output_index": 0,
            "content_index": 0,
            "part": part,
        })
        self._write_sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item})
        self._write_sse_event("response.completed", {
            "type": "response.completed",
            "response": response_payload_from_text(response_id, model, final_text, usage),
        })
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_responses_error_stream(self, status: int, error: dict[str, Any], model: str = "") -> None:
        self._send_response_stream_headers(status)
        if self.command == "HEAD":
            return
        self._write_responses_failed_stream(error, model=model)

    def _write_responses_failed_stream(self, error: dict[str, Any], model: str = "") -> None:
        response = {
            "id": f"resp_{int(time.time() * 1000)}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "failed",
            "model": model,
            "output": [],
            "output_text": "",
            "error": error,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        self._write_sse_event("response.failed", {"type": "response.failed", "response": response})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_chat_payload_as_responses_stream(
        self,
        status: int,
        chat_payload: dict[str, Any],
        model: str = "",
        tool_context: dict[str, Any] | None = None,
    ) -> None:
        converted = chat_payload_to_responses(chat_payload, model=model, tool_context=tool_context)
        self._send_responses_payload_as_stream(status, converted, model=model)

    def _emit_response_message_item_stream(self, output_index: int, item: dict[str, Any]) -> tuple[dict[str, Any], str]:
        item_id = str(item.get("id") or f"msg_{output_index}")
        final_parts: list[dict[str, Any]] = []
        text_parts: list[str] = []
        self._write_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
        })
        content = item.get("content") if isinstance(item.get("content"), list) else []
        content_index = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type not in {"output_text", "text"} and "text" not in part:
                final_parts.append(part)
                continue
            text = str(part.get("text") or "")
            stream_part = {"type": "output_text", "text": "", "annotations": part.get("annotations") or []}
            done_part = {"type": "output_text", "text": text, "annotations": part.get("annotations") or []}
            self._write_sse_event("response.content_part.added", {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": stream_part,
            })
            if text:
                self._write_sse_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": content_index,
                    "delta": text,
                })
            self._write_sse_event("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "text": text,
            })
            self._write_sse_event("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": done_part,
            })
            final_parts.append(done_part)
            text_parts.append(text)
            content_index += 1
        final_item = {"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": final_parts}
        self._write_sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": final_item,
        })
        return final_item, "".join(text_parts)

    def _emit_response_reasoning_item_stream(self, output_index: int, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item.get("id") or f"rs_{output_index}")
        summary = item.get("summary")
        if isinstance(summary, list):
            summary_text = "".join(
                str(part.get("text") or part.get("content") or "") if isinstance(part, dict) else str(part or "")
                for part in summary
            )
        else:
            summary_text = str(summary or item.get("text") or item.get("content") or "")
        self._write_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {"id": item_id, "type": "reasoning", "status": "in_progress", "summary": []},
        })
        if summary_text:
            self._write_sse_event("response.reasoning_summary_text.delta", {
                "type": "response.reasoning_summary_text.delta",
                "item_id": item_id,
                "output_index": output_index,
                "summary_index": 0,
                "delta": summary_text,
            })
        final_item = {
            "id": item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": summary_text}] if summary_text else [],
        }
        self._write_sse_event("response.reasoning_summary_text.done", {
            "type": "response.reasoning_summary_text.done",
            "item_id": item_id,
            "output_index": output_index,
            "summary_index": 0,
            "text": summary_text,
        })
        self._write_sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": final_item,
        })
        return final_item

    def _emit_response_tool_item_stream(self, output_index: int, item: dict[str, Any]) -> dict[str, Any]:
        final_item = dict(item)
        final_item["status"] = "completed"
        added_item = dict(final_item)
        added_item["status"] = "in_progress"
        if added_item.get("type") == "function_call":
            added_item["arguments"] = ""
        self._write_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": added_item,
        })
        if final_item.get("type") == "function_call":
            arguments = str(final_item.get("arguments") or "")
            if arguments:
                self._write_sse_event("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": str(final_item.get("id") or f"fc_{output_index}"),
                    "output_index": output_index,
                    "delta": arguments,
                })
            self._write_sse_event("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": str(final_item.get("id") or f"fc_{output_index}"),
                "output_index": output_index,
                "arguments": arguments,
            })
        self._write_sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": final_item,
        })
        return final_item

    def _send_responses_payload_as_stream(self, status: int, payload: dict[str, Any], model: str = "") -> None:
        response_id = str(payload.get("id") or f"resp_{int(time.time() * 1000)}")
        response_model = model or str(payload.get("model") or "")
        output = payload.get("output") if isinstance(payload.get("output"), list) else []
        if not output and payload.get("output_text"):
            output = [{
                "id": "msg_0",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": str(payload.get("output_text") or ""), "annotations": []}],
            }]

        self._send_response_stream_headers(status)
        if self.command == "HEAD":
            return
        self._write_response_stream_start(response_id, response_model, start_text_item=False)

        final_output: list[dict[str, Any]] = []
        output_text_parts: list[str] = []
        for output_index, item in enumerate(output):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                final_item, text = self._emit_response_message_item_stream(output_index, item)
                final_output.append(final_item)
                if text:
                    output_text_parts.append(text)
            elif item.get("type") == "reasoning":
                final_output.append(self._emit_response_reasoning_item_stream(output_index, item))
            else:
                final_output.append(self._emit_response_tool_item_stream(output_index, item))

        completed = dict(payload)
        completed["id"] = response_id
        completed["object"] = "response"
        completed["created_at"] = int(completed.get("created_at") or time.time())
        completed["status"] = completed.get("status") or "completed"
        completed["model"] = response_model
        completed["output"] = final_output
        completed["output_text"] = str(completed.get("output_text") or "".join(output_text_parts))
        self._write_sse_event("response.completed", {"type": "response.completed", "response": completed})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_chat_stream_as_responses(
        self,
        resp: requests.Response,
        model: str = "",
        tool_context: dict[str, Any] | None = None,
    ) -> None:
        response_id = f"resp_{int(time.time() * 1000)}"
        response_model = model
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        started = False
        text_started = False
        text_output_index = -1
        reasoning_started = False
        reasoning_done = False
        reasoning_output_index = -1
        reasoning_item_id = "rs_0"
        next_output_index = 0
        usage: Any = None
        finish_reason: Any = None
        tool_states: dict[int, dict[str, Any]] = {}
        inline_mode = "detecting"  # detecting | reasoning | text
        inline_buffer = ""

        self._send_response_stream_headers(resp.status_code)
        if self.command == "HEAD":
            return

        def ensure_started(chunk: dict[str, Any] | None = None) -> None:
            nonlocal started, response_id, response_model
            if chunk:
                response_id = str(chunk.get("id") or response_id)
                response_model = str(chunk.get("model") or response_model)
            if not started:
                self._write_response_stream_start(response_id, response_model, start_text_item=False)
                started = True

        def allocate_output_index() -> int:
            nonlocal next_output_index
            output_index = next_output_index
            next_output_index += 1
            return output_index

        def ensure_reasoning_item() -> None:
            nonlocal reasoning_started, reasoning_output_index, reasoning_item_id
            ensure_started(None)
            if reasoning_started:
                return
            reasoning_output_index = allocate_output_index()
            reasoning_item_id = f"rs_{reasoning_output_index}"
            self._write_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": reasoning_output_index,
                "item": {"id": reasoning_item_id, "type": "reasoning", "status": "in_progress", "summary": []},
            })
            reasoning_started = True

        def emit_reasoning_delta(piece: str) -> None:
            piece = str(piece or "")
            if not piece:
                return
            ensure_reasoning_item()
            reasoning_parts.append(piece)
            self._write_sse_event("response.reasoning_summary_text.delta", {
                "type": "response.reasoning_summary_text.delta",
                "item_id": reasoning_item_id,
                "output_index": reasoning_output_index,
                "summary_index": 0,
                "delta": piece,
            })

        def finalize_reasoning() -> None:
            nonlocal reasoning_done
            if not reasoning_started or reasoning_done:
                return
            summary_text = "".join(reasoning_parts)
            item = {
                "id": reasoning_item_id,
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": summary_text}] if summary_text else [],
            }
            self._write_sse_event("response.reasoning_summary_text.done", {
                "type": "response.reasoning_summary_text.done",
                "item_id": reasoning_item_id,
                "output_index": reasoning_output_index,
                "summary_index": 0,
                "text": summary_text,
            })
            self._write_sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": reasoning_output_index,
                "item": item,
            })
            reasoning_done = True

        def ensure_text_item() -> None:
            nonlocal text_started, text_output_index
            if text_started:
                return
            finalize_reasoning()
            ensure_started(None)
            text_output_index = allocate_output_index()
            self._write_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": text_output_index,
                "item": {"id": "msg_0", "type": "message", "status": "in_progress", "role": "assistant", "content": []},
            })
            self._write_sse_event("response.content_part.added", {
                "type": "response.content_part.added",
                "item_id": "msg_0",
                "output_index": text_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            text_started = True

        def emit_text_delta(piece: str) -> None:
            piece = str(piece or "")
            if not piece:
                return
            ensure_text_item()
            text_parts.append(piece)
            self._write_sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": "msg_0",
                "output_index": text_output_index,
                "content_index": 0,
                "delta": piece,
            })

        def leading_think_prefix_decision(buffer: str) -> str:
            trimmed = buffer.lstrip()
            if not trimmed:
                return "need_more"
            if trimmed.startswith(THINK_OPEN_TAG):
                return "reasoning"
            if THINK_OPEN_TAG.startswith(trimmed):
                return "need_more"
            return "text"

        def drain_complete_inline_think() -> None:
            nonlocal inline_mode, inline_buffer
            split = split_leading_think_block(inline_buffer)
            if not split:
                return
            reasoning, answer = split
            inline_mode = "text"
            inline_buffer = ""
            if reasoning:
                emit_reasoning_delta(reasoning)
                finalize_reasoning()
            if answer:
                emit_text_delta(answer)

        def flush_inline_at_boundary() -> None:
            nonlocal inline_mode, inline_buffer
            if inline_mode == "text":
                return
            buffered = inline_buffer
            inline_buffer = ""
            if inline_mode == "detecting":
                inline_mode = "text"
                if buffered:
                    finalize_reasoning()
                    emit_text_delta(buffered)
                return
            inline_mode = "text"
            split = split_leading_think_block(buffered)
            if split:
                reasoning, answer = split
                if reasoning:
                    emit_reasoning_delta(reasoning)
                    finalize_reasoning()
                if answer:
                    emit_text_delta(answer)
                return
            reasoning = strip_leading_think_open_tag(buffered)
            if reasoning is None:
                reasoning = buffered
            if reasoning:
                emit_reasoning_delta(reasoning)
                finalize_reasoning()

        def process_content_piece(piece: str) -> None:
            nonlocal inline_mode, inline_buffer
            piece = str(piece or "")
            if not piece:
                return
            if inline_mode == "text":
                finalize_reasoning()
                emit_text_delta(piece)
                return
            inline_buffer += piece
            if inline_mode == "detecting":
                decision = leading_think_prefix_decision(inline_buffer)
                if decision == "need_more":
                    return
                if decision == "reasoning":
                    inline_mode = "reasoning"
                    drain_complete_inline_think()
                    return
                inline_mode = "text"
                text = inline_buffer
                inline_buffer = ""
                finalize_reasoning()
                emit_text_delta(text)
                return
            if inline_mode == "reasoning":
                drain_complete_inline_think()

        def current_reasoning_text() -> str:
            return "".join(reasoning_parts).strip()

        def append_reasoning_to_active_tools(piece: str) -> None:
            piece = str(piece or "")
            if not piece.strip():
                return
            for state in tool_states.values():
                if state.get("done"):
                    continue
                if not state.get("reasoning_content"):
                    state["reasoning_content"] = piece.strip()
                else:
                    state["reasoning_content"] = str(state.get("reasoning_content") or "") + piece

        def state_for_tool_call(raw_tool_call: dict[str, Any]) -> dict[str, Any]:
            try:
                tool_index = int(raw_tool_call.get("index") or 0)
            except Exception:
                tool_index = 0
            state = tool_states.get(tool_index)
            if state is None:
                state = {
                    "index": tool_index,
                    "output_index": None,
                    "call_id": str(raw_tool_call.get("id") or f"call_{tool_index}"),
                    "chat_name": "",
                    "arguments_parts": [],
                    "streamed_arguments_len": 0,
                    "reasoning_content": current_reasoning_text(),
                    "added": False,
                    "done": False,
                }
                tool_states[tool_index] = state
            if raw_tool_call.get("id"):
                state["call_id"] = str(raw_tool_call.get("id"))
            function = raw_tool_call.get("function") if isinstance(raw_tool_call.get("function"), dict) else {}
            if function.get("name"):
                state["chat_name"] = str(function.get("name") or "")
            if not state.get("reasoning_content") and current_reasoning_text():
                state["reasoning_content"] = current_reasoning_text()
            return state

        def emit_pending_tool_argument_delta(state: dict[str, Any]) -> None:
            if not state.get("added") or state.get("stream_item_type") != "function_call":
                return
            arguments = "".join(state.get("arguments_parts") or [])
            streamed_len = int(state.get("streamed_arguments_len") or 0)
            if len(arguments) <= streamed_len:
                return
            piece = arguments[streamed_len:]
            state["streamed_arguments_len"] = len(arguments)
            self._write_sse_event("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "item_id": str(state.get("item_id") or f"fc_{state.get('call_id', '')}"),
                "output_index": int(state["output_index"]),
                "delta": piece,
            })

        def ensure_tool_added(state: dict[str, Any]) -> None:
            if state.get("added"):
                return
            chat_name = str(state.get("chat_name") or "")
            if not chat_name:
                return
            finalize_reasoning()
            ensure_started(None)
            output_index = allocate_output_index()
            state["output_index"] = output_index
            call_id = str(state.get("call_id") or f"call_{state.get('index', 0)}")
            item_id = response_tool_call_item_id_from_chat_name(call_id, chat_name, tool_context)
            item = response_tool_call_item_from_chat_name(
                item_id,
                "in_progress",
                call_id,
                chat_name,
                "",
                tool_context,
                reasoning=state.get("reasoning_content"),
            )
            self._write_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            })
            state["item_id"] = item_id
            state["stream_item_type"] = item.get("type")
            state["added"] = True
            emit_pending_tool_argument_delta(state)

        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace")
            data = line[5:].strip() if line.startswith("data:") else ""
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            if isinstance(chunk, dict) and chunk.get("usage") is not None:
                usage = chunk.get("usage")
            stream_error = extract_stream_error_object_from_chunk(chunk)
            if stream_error:
                response_model = str(chunk.get("model") or response_model) if isinstance(chunk, dict) else response_model
                error = stream_error_to_openai_error(stream_error, "/chat/completions")
                self._write_responses_failed_stream(error, model=response_model)
                return
            ensure_started(chunk if isinstance(chunk, dict) else None)
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta", {}) if isinstance(choice.get("delta"), dict) else {}
                reasoning_delta = extract_reasoning_field_text(delta)
                if reasoning_delta:
                    emit_reasoning_delta(reasoning_delta)
                    append_reasoning_to_active_tools(reasoning_delta)
                content = delta.get("content")
                if content:
                    process_content_piece(str(content))
                raw_tool_calls = delta.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    flush_inline_at_boundary()
                    if current_reasoning_text():
                        finalize_reasoning()
                    for raw_tool_call in raw_tool_calls:
                        if not isinstance(raw_tool_call, dict):
                            continue
                        state = state_for_tool_call(raw_tool_call)
                        function = raw_tool_call.get("function") if isinstance(raw_tool_call.get("function"), dict) else {}
                        arg_delta = function.get("arguments")
                        if arg_delta is not None:
                            state.setdefault("arguments_parts", []).append(str(arg_delta))
                        ensure_tool_added(state)
                        if arg_delta is not None:
                            emit_pending_tool_argument_delta(state)
                if choice.get("finish_reason") is not None:
                    finish_reason = choice.get("finish_reason")

        ensure_started(None)
        flush_inline_at_boundary()
        finalize_reasoning()
        final_output_pairs: list[tuple[int, dict[str, Any]]] = []
        final_text = "".join(text_parts)
        if reasoning_started:
            summary_text = "".join(reasoning_parts)
            final_output_pairs.append((reasoning_output_index, {
                "id": reasoning_item_id,
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": summary_text}] if summary_text else [],
            }))
        if text_started:
            part = {"type": "output_text", "text": final_text, "annotations": []}
            item = {"id": "msg_0", "type": "message", "status": "completed", "role": "assistant", "content": [part]}
            self._write_sse_event("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": "msg_0",
                "output_index": text_output_index,
                "content_index": 0,
                "text": final_text,
            })
            self._write_sse_event("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": "msg_0",
                "output_index": text_output_index,
                "content_index": 0,
                "part": part,
            })
            self._write_sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": text_output_index,
                "item": item,
            })
            final_output_pairs.append((text_output_index, item))

        for _tool_index, state in sorted(tool_states.items(), key=lambda kv: int(kv[1].get("output_index") if kv[1].get("output_index") is not None else kv[0])):
            chat_name = str(state.get("chat_name") or "")
            if not chat_name:
                continue
            ensure_tool_added(state)
            if state.get("output_index") is None:
                continue
            call_id = str(state.get("call_id") or f"call_{state.get('index', 0)}")
            arguments = canonicalize_tool_arguments("".join(state.get("arguments_parts") or []))
            item_id = str(state.get("item_id") or response_tool_call_item_id_from_chat_name(call_id, chat_name, tool_context))
            final_item = response_tool_call_item_from_chat_name(
                item_id,
                "completed",
                call_id,
                chat_name,
                arguments,
                tool_context,
                reasoning=state.get("reasoning_content"),
            )
            state["done"] = True
            if final_item.get("type") == "function_call":
                self._write_sse_event("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": item_id,
                    "output_index": int(state["output_index"]),
                    "arguments": arguments,
                })
            self._write_sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": int(state["output_index"]),
                "item": final_item,
            })
            final_output_pairs.append((int(state["output_index"]), final_item))

        final_output = [item for _output_index, item in sorted(final_output_pairs, key=lambda pair: pair[0])]
        status = response_status_from_finish_reason(finish_reason)
        completed = {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": response_model,
            "output_text": final_text,
            "output": final_output,
            "usage": chat_usage_to_responses_usage(usage),
        }
        if status == "incomplete":
            completed["incomplete_details"] = {"reason": "max_output_tokens"}
        self._write_sse_event("response.completed", {"type": "response.completed", "response": completed})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_messages_stream_as_responses(
        self,
        resp: requests.Response,
        model: str = "",
        tool_context: dict[str, Any] | None = None,
    ) -> None:
        response_id = f"resp_{int(time.time() * 1000)}"
        response_model = model
        started = False
        completed_sent = False
        next_output_index = 0
        block_states: dict[Any, dict[str, Any]] = {}
        final_output_pairs: list[tuple[int, dict[str, Any]]] = []
        output_text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason = ""

        self._send_response_stream_headers(resp.status_code)
        if self.command == "HEAD":
            return

        def merge_usage(value: Any) -> None:
            if not isinstance(value, dict):
                return
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            ):
                if value.get(key) is not None:
                    usage[key] = value.get(key)

        def ensure_started(chunk: dict[str, Any] | None = None) -> None:
            nonlocal started, response_id, response_model
            if isinstance(chunk, dict):
                response_id = str(chunk.get("id") or response_id)
                response_model = str(chunk.get("model") or response_model)
                merge_usage(chunk.get("usage"))
            if not started:
                self._write_response_stream_start(response_id, response_model, start_text_item=False)
                started = True

        def allocate_output_index() -> int:
            nonlocal next_output_index
            output_index = next_output_index
            next_output_index += 1
            return output_index

        def emit_text_delta(state: dict[str, Any], piece: str) -> None:
            if not piece:
                return
            state.setdefault("text_parts", []).append(piece)
            self._write_sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": str(state["item_id"]),
                "output_index": int(state["output_index"]),
                "content_index": 0,
                "delta": piece,
            })

        def emit_reasoning_delta(state: dict[str, Any], piece: str) -> None:
            if not piece:
                return
            state.setdefault("summary_parts", []).append(piece)
            self._write_sse_event("response.reasoning_summary_text.delta", {
                "type": "response.reasoning_summary_text.delta",
                "item_id": str(state["item_id"]),
                "output_index": int(state["output_index"]),
                "summary_index": 0,
                "delta": piece,
            })

        def start_message_block(key: Any, initial_text: str = "") -> dict[str, Any]:
            ensure_started(None)
            output_index = allocate_output_index()
            item_id = f"msg_{output_index}"
            state = {"kind": "message", "output_index": output_index, "item_id": item_id, "text_parts": [], "done": False}
            block_states[key] = state
            self._write_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
            })
            self._write_sse_event("response.content_part.added", {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            emit_text_delta(state, initial_text)
            return state

        def start_reasoning_block(key: Any, initial_text: str = "") -> dict[str, Any]:
            ensure_started(None)
            output_index = allocate_output_index()
            item_id = f"rs_{output_index}"
            state = {"kind": "reasoning", "output_index": output_index, "item_id": item_id, "summary_parts": [], "done": False}
            block_states[key] = state
            self._write_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {"id": item_id, "type": "reasoning", "status": "in_progress", "summary": []},
            })
            emit_reasoning_delta(state, initial_text)
            return state

        def start_tool_block(key: Any, block: dict[str, Any]) -> dict[str, Any]:
            ensure_started(None)
            output_index = allocate_output_index()
            call_id = str(block.get("id") or f"call_{output_index}")
            name = str(block.get("name") or "")
            item_id = response_tool_call_item_id_from_chat_name(call_id, name, tool_context)
            item = response_tool_call_item_from_chat_name(item_id, "in_progress", call_id, name, "", tool_context)
            state = {
                "kind": "function_call",
                "output_index": output_index,
                "item_id": item_id,
                "call_id": call_id,
                "name": name,
                "arguments_parts": [],
                "stream_item_type": item.get("type"),
                "done": False,
            }
            block_states[key] = state
            self._write_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            })
            initial_input = block.get("input")
            if initial_input not in (None, {}, ""):
                piece = canonicalize_tool_arguments(initial_input)
                state["arguments_parts"].append(piece)
                if state.get("stream_item_type") == "function_call":
                    self._write_sse_event("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item_id,
                        "output_index": output_index,
                        "delta": piece,
                    })
            return state

        def start_block(key: Any, block: dict[str, Any]) -> dict[str, Any] | None:
            block_type = str(block.get("type") or "")
            if block_type == "text":
                return start_message_block(key, str(block.get("text") or ""))
            if block_type == "thinking":
                return start_reasoning_block(key, str(block.get("thinking") or ""))
            if block_type == "tool_use":
                return start_tool_block(key, block)
            return None

        def finalize_state(state: dict[str, Any]) -> None:
            if state.get("done"):
                return
            state["done"] = True
            output_index = int(state["output_index"])
            item_id = str(state["item_id"])
            kind = str(state.get("kind") or "")
            if kind == "message":
                text = "".join(state.get("text_parts") or [])
                part = {"type": "output_text", "text": text, "annotations": []}
                item = {"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": [part]}
                self._write_sse_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "text": text,
                })
                self._write_sse_event("response.content_part.done", {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "part": part,
                })
                self._write_sse_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                })
                final_output_pairs.append((output_index, item))
                output_text_parts.append(text)
                return
            if kind == "reasoning":
                summary_text = "".join(state.get("summary_parts") or [])
                item = {
                    "id": item_id,
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": summary_text}] if summary_text else [],
                }
                self._write_sse_event("response.reasoning_summary_text.done", {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "summary_index": 0,
                    "text": summary_text,
                })
                self._write_sse_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                })
                final_output_pairs.append((output_index, item))
                return
            if kind == "function_call":
                arguments = canonicalize_tool_arguments("".join(state.get("arguments_parts") or []))
                call_id = str(state.get("call_id") or f"call_{output_index}")
                name = str(state.get("name") or "")
                final_item = response_tool_call_item_from_chat_name(item_id, "completed", call_id, name, arguments, tool_context)
                if final_item.get("type") == "function_call":
                    self._write_sse_event("response.function_call_arguments.done", {
                        "type": "response.function_call_arguments.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "arguments": arguments,
                    })
                self._write_sse_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": final_item,
                })
                final_output_pairs.append((output_index, final_item))

        def finalize_all_open() -> None:
            for state in sorted(block_states.values(), key=lambda item: int(item.get("output_index", 0))):
                finalize_state(state)

        def send_completed() -> None:
            nonlocal completed_sent
            if completed_sent:
                return
            ensure_started(None)
            finalize_all_open()
            final_output = [item for _output_index, item in sorted(final_output_pairs, key=lambda pair: pair[0])]
            final_text = "".join(output_text_parts)
            status = anthropic_stop_reason_to_responses_status(stop_reason)
            completed: dict[str, Any] = {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": status,
                "model": response_model,
                "output_text": final_text,
                "output": final_output,
                "usage": chat_usage_to_responses_usage(usage),
            }
            if status == "incomplete":
                completed["incomplete_details"] = {"reason": "max_output_tokens"}
            self._write_sse_event("response.completed", {"type": "response.completed", "response": completed})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            completed_sent = True

        def block_key_from_chunk(chunk: dict[str, Any]) -> Any:
            index = chunk.get("index")
            if index is None:
                return f"auto_{len(block_states)}"
            try:
                return int(index)
            except Exception:
                return str(index)

        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace")
            data = line[5:].strip() if line.startswith("data:") else ""
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            if not isinstance(chunk, dict):
                continue
            stream_error = extract_stream_error_object_from_chunk(chunk)
            if stream_error:
                response_model = str(chunk.get("model") or response_model)
                error = stream_error_to_openai_error(stream_error, "/messages")
                self._write_responses_failed_stream(error, model=response_model)
                return

            # Some OpenAI-compatible gateways return Chat Completions SSE even
            # on a /messages-shaped route. Preserve the previous text behavior.
            choices = chunk.get("choices")
            if isinstance(choices, list):
                ensure_started(chunk)
                merge_usage(chunk.get("usage"))
                chat_state = block_states.get("chat_text")
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                    content = delta.get("content")
                    if content:
                        if chat_state is None or chat_state.get("done"):
                            chat_state = start_message_block("chat_text")
                        emit_text_delta(chat_state, str(content))
                continue

            event_type = str(chunk.get("type") or "")
            if event_type == "message_start":
                message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
                ensure_started(message)
                continue
            if event_type == "content_block_start":
                block = chunk.get("content_block") if isinstance(chunk.get("content_block"), dict) else {}
                key = block_key_from_chunk(chunk)
                start_block(key, block)
                continue
            if event_type == "content_block_delta":
                key = block_key_from_chunk(chunk)
                delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                delta_type = str(delta.get("type") or "")
                state = block_states.get(key)
                if state is None:
                    if delta_type == "text_delta":
                        state = start_message_block(key)
                    elif delta_type == "thinking_delta":
                        state = start_reasoning_block(key)
                if state is None:
                    continue
                if delta_type == "text_delta":
                    emit_text_delta(state, str(delta.get("text") or ""))
                elif delta_type == "thinking_delta":
                    emit_reasoning_delta(state, str(delta.get("thinking") or ""))
                elif delta_type == "input_json_delta":
                    piece = str(delta.get("partial_json") or "")
                    if piece:
                        state.setdefault("arguments_parts", []).append(piece)
                        if state.get("stream_item_type") == "function_call":
                            self._write_sse_event("response.function_call_arguments.delta", {
                                "type": "response.function_call_arguments.delta",
                                "item_id": str(state.get("item_id") or ""),
                                "output_index": int(state.get("output_index") or 0),
                                "delta": piece,
                            })
                continue
            if event_type == "content_block_stop":
                key = block_key_from_chunk(chunk)
                state = block_states.get(key)
                if state is not None:
                    finalize_state(state)
                continue
            if event_type == "message_delta":
                delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta.get("stop_reason") or "")
                merge_usage(chunk.get("usage"))
                continue
            if event_type == "message_stop":
                send_completed()
                break

        if not completed_sent:
            send_completed()

    def _send_text(self, status: int, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _route(self) -> tuple[str | None, dict[str, Any] | None, str]:
        split = urlsplit(self.path)
        parts = split.path.split("/")
        # Expect /<provider>/v1/<upstream-path>. The upstream base_url already includes /v1.
        if len(parts) < 3 or not parts[1]:
            return None, None, split.path
        provider_name = parts[1]
        provider = self.server.config["providers"].get(provider_name)
        if provider is None:
            names = ", ".join(sorted(self.server.config["providers"].keys()))
            self._send_text(404, f"unknown provider path. Use /<provider>/v1/... ; providers: {names}\n")
            return None, None, split.path
        if len(parts) >= 3 and parts[2] == "v1":
            rest_parts = parts[3:]
        else:
            rest_parts = parts[2:]
        route_path = "/" + "/".join(rest_parts)
        if route_path == "/":
            route_path = ""
        if split.query:
            route_path += "?" + split.query
        return provider_name, provider, route_path

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _handle(self) -> None:
        provider_name, provider, proxied_path = self._route()
        if not provider_name or provider is None:
            return

        route_path = proxied_path.split("?", 1)[0].rstrip("/") or "/"
        if self.command in {"GET", "HEAD"} and route_path == "/models":
            self._send_json(200, build_models_payload(provider_name, provider))
            return

        custom_endpoint = str(provider.get("custom_endpoint") or "").strip()
        upstream_path = proxied_path
        if custom_endpoint and route_path in {"/responses", "/chat/completions", "/messages"}:
            query = ""
            if "?" in proxied_path:
                query = "?" + proxied_path.split("?", 1)[1]
            upstream_path = custom_endpoint + query
        upstream_route_path = upstream_path.split("?", 1)[0].rstrip("/") or "/"

        base_url = str(provider.get("base_url") or "").rstrip("/")
        url = base_url + (upstream_path or "")
        body = self._read_body()

        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS:
                continue
            headers[key] = value

        for key, value in provider.get("headers", {}).items():
            headers[str(key)] = "" if value is None else str(value)

        for key in provider.get("remove_headers", set()):
            for existing in list(headers.keys()):
                if existing.lower() == key:
                    headers.pop(existing, None)

        api_key = str(provider.get("api_key") or "").strip()
        auth_mode = str(provider.get("auth_mode") or "bearer").strip().lower()
        if auth_mode == "anthropic":
            for existing in list(headers.keys()):
                if existing.lower() == "authorization":
                    headers.pop(existing, None)
            if api_key:
                headers["x-api-key"] = api_key
            headers["anthropic-version"] = str(provider.get("anthropic_version") or DEFAULT_ANTHROPIC_VERSION)
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body, body_json = maybe_apply_reasoning(provider, proxied_path, body)
        convert_response_from = ""
        conversion_tool_context: dict[str, Any] | None = None
        provider_api_mode = str(provider.get("api_mode") or "").strip().lower()
        if (
            self.command == "POST"
            and route_path == "/responses"
            and provider_api_mode in {"chat_completions", "messages"}
            and isinstance(body_json, dict)
        ):
            query = "?" + proxied_path.split("?", 1)[1] if "?" in proxied_path else ""
            upstream_path = ("/messages" if provider_api_mode == "messages" else "/chat/completions") + query
            upstream_route_path = "/messages" if provider_api_mode == "messages" else "/chat/completions"
            url = base_url + upstream_path
            if auth_mode == "anthropic" and provider_api_mode == "messages":
                conversion_tool_context = build_codex_tool_context_from_request(body_json)
                body_json = responses_payload_to_anthropic_messages(body_json, conversion_tool_context)
                convert_response_from = "messages"
            else:
                conversion_tool_context = build_codex_tool_context_from_request(body_json)
                body_json = responses_payload_to_chat(body_json, conversion_tool_context)
                convert_response_from = "chat"
            body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        elif self.command == "POST" and upstream_route_path == "/messages" and auth_mode == "anthropic" and isinstance(body_json, dict):
            body_json = normalize_anthropic_messages_payload(body_json)
            body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        if body_json is not None:
            headers["Content-Type"] = "application/json"

        session = self.server.session_pool.borrow(provider_name, provider)

        started = time.time()

        def send_upstream(active_session: requests.Session) -> tuple[requests.Response, str]:
            nonlocal conversion_tool_context
            active_convert_response_from = convert_response_from
            timeout = (
                float(provider.get("connect_timeout") or DEFAULT_CONNECT_TIMEOUT),
                float(provider.get("read_timeout") or DEFAULT_READ_TIMEOUT),
            )
            active_resp = active_session.request(
                method=self.command,
                url=url,
                headers=headers,
                data=body,
                stream=True,
                timeout=timeout,
            )
            if (
                self.command == "POST"
                and upstream_route_path == "/responses"
                and provider.get("fallback_responses_to_chat")
                and active_resp.status_code in RESPONSES_TO_CHAT_FALLBACK_STATUSES
                and isinstance(body_json, dict)
            ):
                active_resp.close()
                conversion_tool_context = build_codex_tool_context_from_request(body_json)
                fallback_body_json = responses_payload_to_chat(body_json, conversion_tool_context)
                fallback_body = json.dumps(fallback_body_json, ensure_ascii=False).encode("utf-8")
                fallback_url = base_url + "/chat/completions"
                active_resp = active_session.request(
                    method="POST",
                    url=fallback_url,
                    headers=headers,
                    data=fallback_body,
                    stream=True,
                    timeout=timeout,
                )
                active_convert_response_from = "chat"
            return active_resp, active_convert_response_from

        try:
            try:
                resp, convert_response_from = send_upstream(session)
            except TRANSPORT_RETRY_EXCEPTIONS as exc:
                self.server.session_pool.discard(session)
                if self.command not in {"GET", "POST"}:
                    raise
                self.server.log_error_always(
                    self,
                    "%s %s -> %s proxy_transport_error=%s retrying_with_fresh_session=1",
                    provider_name,
                    self.command,
                    url,
                    repr(exc),
                )
                session = self.server.session_pool.fresh(provider)
                resp, convert_response_from = send_upstream(session)
        except Exception as exc:
            self.server.log_error_always(self, "%s %s -> %s proxy_error=%s", provider_name, self.command, url, repr(exc))
            self._send_text(502, f"proxy error: {type(exc).__name__}: {exc}\n")
            self.server.session_pool.discard(session)
            return

        elapsed = time.time() - started
        self.server.log_if_verbose(self, "%s %s %s -> HTTP %s %.2fs", provider_name, self.command, proxied_path, resp.status_code, elapsed)

        if convert_response_from and resp.status_code >= 400:
            try:
                response_model = str((body_json or {}).get("model") or "")
                error_source = "/messages" if convert_response_from == "messages" else "/chat/completions"
                error = upstream_error_to_openai_error(resp, error_source)
                if isinstance(body_json, dict) and body_json.get("stream"):
                    self._send_responses_error_stream(resp.status_code, error, model=response_model)
                else:
                    self._send_json(resp.status_code, {"error": error})
                return
            finally:
                resp.close()
                if should_discard_upstream_response(resp):
                    self.server.session_pool.discard(session)
                else:
                    self.server.session_pool.release(provider_name, provider, session)

        if convert_response_from and resp.status_code < 400:
            try:
                response_model = str((body_json or {}).get("model") or "")
                if isinstance(body_json, dict) and body_json.get("stream"):
                    if "text/event-stream" in str(resp.headers.get("Content-Type") or "").lower():
                        if convert_response_from == "chat":
                            self._send_chat_stream_as_responses(resp, model=response_model, tool_context=conversion_tool_context)
                        else:
                            self._send_messages_stream_as_responses(resp, model=response_model, tool_context=conversion_tool_context)
                    else:
                        payload = resp.json()
                        converted = (
                            messages_payload_to_responses(payload, model=response_model, tool_context=conversion_tool_context)
                            if convert_response_from == "messages"
                            else chat_payload_to_responses(payload, model=response_model, tool_context=conversion_tool_context)
                        )
                        self._send_responses_payload_as_stream(resp.status_code, converted, model=response_model)
                else:
                    payload = resp.json()
                    converted = (
                        messages_payload_to_responses(payload, model=response_model, tool_context=conversion_tool_context)
                        if convert_response_from == "messages"
                        else chat_payload_to_responses(payload, model=response_model, tool_context=conversion_tool_context)
                    )
                    self._send_json(resp.status_code, converted)
                return
            finally:
                resp.close()
                if should_discard_upstream_response(resp):
                    self.server.session_pool.discard(session)
                else:
                    self.server.session_pool.release(provider_name, provider, session)

        self.close_connection = True
        self.send_response(resp.status_code)
        for key, value in resp.headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()

        discard_session = should_discard_upstream_response(resp)
        chunks_sent = 0
        try:
            # 原样转发压缩字节。不要用 iter_content()，它会自动解码 gzip/br，
            # 但响应头里的 Content-Encoding 仍会保留，客户端会二次解压并误报失败。
            resp.raw.decode_content = False
            if self.command != "HEAD":
                for chunk in resp.raw.stream(8192, decode_content=False):
                    if chunk:
                        chunks_sent += 1
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except Exception as exc:
            discard_session = True
            self.close_connection = True
            self.server.log_error_always(
                self,
                "%s %s %s upstream_status=%s chunks_sent=%s elapsed=%.2fs stream_error=%s closing_downstream=1",
                provider_name,
                self.command,
                proxied_path,
                resp.status_code,
                chunks_sent,
                time.time() - started,
                repr(exc),
            )
            return
        finally:
            resp.close()
            if discard_session:
                self.server.session_pool.discard(session)
            else:
                self.server.session_pool.release(provider_name, provider, session)

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self.close_connection = True
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-provider header injection proxy for OpenAI-compatible APIs")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to YAML/JSON config")
    parser.add_argument("--listen", help="Override listen host")
    parser.add_argument("--port", type=int, help="Override listen port")
    parser.add_argument("--check", action="store_true", help="Validate config and exit")
    parser.add_argument("--verbose", action="store_true", help="Log every proxied request; errors are always logged")
    args = parser.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    cfg = load_config(cfg_path)
    if args.listen:
        cfg["listen"] = args.listen
    if args.port:
        cfg["port"] = int(args.port)
    cfg["verbose"] = bool(args.verbose)
    if args.check:
        print(f"OK config: {cfg_path}")
        print(f"listen: {cfg['listen']}:{cfg['port']}")
        print("providers: " + ", ".join(sorted(cfg["providers"].keys())))
        return 0

    server = HeaderProxyServer((cfg["listen"], cfg["port"]), ProxyHandler, cfg)

    def stop(_signum: int | None = None, _frame: Any = None) -> None:
        print("\nshutting down")
        sys.stdout.flush()
        # shutdown() must not run in the signal handler/main serve_forever thread;
        # doing so can deadlock and make systemd wait until SIGKILL.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"Header proxy listening on http://{cfg['listen']}:{cfg['port']} verbose={cfg.get('verbose', False)}")
    print("providers:")
    for name, provider in sorted(cfg["providers"].items()):
        h = {k: redact_header(k, v) for k, v in provider["headers"].items()}
        print(
            f"  /{name}/ -> {provider['base_url']} "
            f"headers={json.dumps(h, ensure_ascii=False)} remove={sorted(provider['remove_headers'])} "
            f"trust_env_proxy={provider.get('trust_env_proxy', False)} pool_maxsize={provider.get('pool_maxsize', DEFAULT_POOL_MAXSIZE)} "
            f"timeout=({provider.get('connect_timeout', DEFAULT_CONNECT_TIMEOUT)}, {provider.get('read_timeout', DEFAULT_READ_TIMEOUT)})"
        )
    sys.stdout.flush()

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
