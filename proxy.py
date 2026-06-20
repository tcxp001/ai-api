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

    Codex/Responses commonly sends text parts as {"type": "input_text", ...}.
    Prefer plain strings for text-only content, and only keep a content-part
    list when non-text parts (for example images) are present.
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

        if part_type == "input_image":
            image_url = part.get("image_url") or part.get("url")
            if isinstance(image_url, dict):
                return {"type": "image_url", "image_url": image_url}, None, False
            if image_url:
                return {"type": "image_url", "image_url": {"url": str(image_url)}}, None, False
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
            return "".join(text_parts)
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

    def flush_pending_tool_calls() -> None:
        if not pending_tool_calls:
            return
        messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_tool_calls)})
        pending_tool_calls.clear()

    def append_item(item: Any) -> None:
        if not isinstance(item, dict):
            flush_pending_tool_calls()
            messages.append({"role": "user", "content": responses_content_to_chat(item)})
            return

        item_type = str(item.get("type") or "")
        if item_type == "function_call":
            pending_tool_calls.append(responses_function_call_to_chat_tool_call(item, tool_context))
            return
        if item_type == "custom_tool_call":
            pending_tool_calls.append(responses_custom_tool_call_to_chat_tool_call(item))
            return
        if item_type == "tool_search_call":
            pending_tool_calls.append(responses_tool_search_call_to_chat_tool_call(item))
            return
        if item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            flush_pending_tool_calls()
            call_id = str(item.get("call_id") or item.get("id") or "")
            messages.append({"role": "tool", "tool_call_id": call_id, "content": responses_tool_output_to_chat_content(item)})
            return
        if item_type == "reasoning":
            # Chat-only upstreams cannot consume Responses reasoning items as
            # first-class blocks. Drop them instead of creating invalid roles.
            return

        flush_pending_tool_calls()
        if item_type in {"input_text", "input_image", "input_file", "input_audio"}:
            role = responses_role_to_chat_role(str(item.get("role") or "user"))
            messages.append({"role": role, "content": responses_content_to_chat([item])})
            return
        if item.get("role") is not None or item.get("content") is not None:
            messages.append(responses_input_item_to_chat_message(item))

    if isinstance(input_value, list):
        for input_item in input_value:
            append_item(input_item)
    elif input_value is not None:
        append_item(input_value)
    flush_pending_tool_calls()


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

    chat: dict[str, Any] = {"model": body_json.get("model")}
    if body_json.get("max_output_tokens") is not None:
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

    messages: list[dict[str, Any]] = []
    instructions = instruction_text(body_json.get("instructions")) if body_json.get("instructions") is not None else ""
    if instructions:
        messages.append({"role": "system", "content": instructions})

    if "input" in body_json:
        append_responses_input_as_chat_messages(body_json.get("input"), messages, tool_context)
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


def responses_payload_to_anthropic_messages(body_json: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": body_json.get("model")}
    if body_json.get("max_output_tokens") is not None:
        payload["max_tokens"] = body_json.get("max_output_tokens")
    elif body_json.get("max_tokens") is not None:
        payload["max_tokens"] = body_json.get("max_tokens")
    else:
        payload["max_tokens"] = 1024

    for key in ("stream", "temperature", "top_p", "stop"):
        if key in body_json:
            payload[key] = body_json.get(key)

    system_parts: list[str] = []
    instructions = body_json.get("instructions")
    if instructions:
        system_parts.append(str(instructions))

    messages: list[dict[str, Any]] = []
    input_value = body_json.get("input")
    if isinstance(input_value, list):
        for item in input_value:
            message = responses_input_item_to_chat_message(item)
            role = str(message.get("role") or "user")
            content = message.get("content", "")
            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue
            if role not in {"user", "assistant"}:
                role = "user"
            messages.append({"role": role, "content": normalize_anthropic_content(content)})
    elif input_value is not None:
        messages.append({"role": "user", "content": normalize_anthropic_content(responses_content_to_chat(input_value))})
    else:
        messages.append({"role": "user", "content": ""})

    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    payload["messages"] = messages or [{"role": "user", "content": ""}]
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


def anthropic_payload_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in {"text", "output_text"} or "text" in part:
                    parts.append(str(part.get("text") or ""))
            elif part is not None:
                parts.append(str(part))
        return "".join(parts)
    return ""



def chat_usage_to_responses_usage(usage: Any) -> Any:
    if not isinstance(usage, dict):
        return usage
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    reasoning_tokens = int(
        output_details.get("reasoning_tokens")
        or completion_details.get("reasoning_tokens")
        or usage.get("reasoning_tokens")
        or 0
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }


def messages_payload_to_responses(payload: dict[str, Any], model: str = "", tool_context: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(payload.get("choices"), list):
        return chat_payload_to_responses(payload, model=model, tool_context=tool_context)
    response_id = str(payload.get("id") or f"resp_{int(time.time() * 1000)}")
    response_model = model or str(payload.get("model") or "")
    usage = chat_usage_to_responses_usage(payload.get("usage"))
    return response_payload_from_text(response_id, response_model, anthropic_payload_text(payload), usage)


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


def chat_message_to_response_output_item(message: dict[str, Any], item_id: str = "msg_0") -> tuple[dict[str, Any] | None, str]:
    content = message.get("content", "") if isinstance(message, dict) else ""
    response_parts: list[dict[str, Any]] = []
    text_parts: list[str] = []
    if isinstance(content, str):
        if content:
            response_parts.append({"type": "output_text", "text": content, "annotations": []})
            text_parts.append(content)
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
) -> dict[str, Any]:
    spec = tool_context_lookup(tool_context, chat_name)
    if spec and spec.get("kind") == "custom":
        return {
            "id": item_id,
            "type": "custom_tool_call",
            "status": status,
            "call_id": call_id,
            "name": str(spec.get("name") or chat_name),
            "input": custom_tool_input_from_chat_arguments(arguments),
        }
    if spec and spec.get("kind") == "tool_search":
        return {
            "type": "tool_search_call",
            "call_id": call_id,
            "status": status,
            "execution": "client",
            "arguments": parse_tool_arguments_object(arguments),
        }
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
    return item


def chat_tool_call_to_response_item(
    tool_call: dict[str, Any],
    index: int,
    tool_context: dict[str, Any] | None = None,
    status: str = "completed",
) -> dict[str, Any] | None:
    call_id = str(tool_call.get("id") or f"call_{index}")
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    chat_name = str(function.get("name") or "")
    if not chat_name:
        return None
    arguments = canonicalize_tool_arguments(function.get("arguments"))
    item_id = response_tool_call_item_id_from_chat_name(call_id, chat_name, tool_context)
    return response_tool_call_item_from_chat_name(item_id, status, call_id, chat_name, arguments, tool_context)


def chat_legacy_function_call_to_response_item(
    function_call: dict[str, Any],
    tool_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    name = str(function_call.get("name") or "")
    if not name:
        return None
    call_id = str(function_call.get("id") or "call_0")
    arguments = canonicalize_tool_arguments(function_call.get("arguments"))
    item_id = response_tool_call_item_id_from_chat_name(call_id, name, tool_context)
    return response_tool_call_item_from_chat_name(item_id, "completed", call_id, name, arguments, tool_context)


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
    message_item, output_text = chat_message_to_response_output_item(message, item_id="msg_0")
    if message_item is not None:
        output.append(message_item)

    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            item = chat_tool_call_to_response_item(tool_call, index, tool_context=tool_context, status="completed")
            if item is not None:
                output.append(item)
    elif isinstance(message, dict) and isinstance(message.get("function_call"), dict):
        item = chat_legacy_function_call_to_response_item(message["function_call"], tool_context=tool_context)
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
        started = False
        text_started = False
        text_output_index = -1
        next_output_index = 0
        usage: Any = None
        tool_states: dict[int, dict[str, Any]] = {}

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

        def ensure_text_item() -> None:
            nonlocal text_started, text_output_index, next_output_index
            if text_started:
                return
            text_output_index = next_output_index
            next_output_index += 1
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

        def state_for_tool_call(raw_tool_call: dict[str, Any]) -> dict[str, Any]:
            nonlocal next_output_index
            try:
                tool_index = int(raw_tool_call.get("index") or 0)
            except Exception:
                tool_index = 0
            state = tool_states.get(tool_index)
            if state is None:
                state = {
                    "index": tool_index,
                    "output_index": next_output_index,
                    "call_id": str(raw_tool_call.get("id") or f"call_{tool_index}"),
                    "chat_name": "",
                    "arguments_parts": [],
                    "added": False,
                }
                next_output_index += 1
                tool_states[tool_index] = state
            if raw_tool_call.get("id"):
                state["call_id"] = str(raw_tool_call.get("id"))
            function = raw_tool_call.get("function") if isinstance(raw_tool_call.get("function"), dict) else {}
            if function.get("name"):
                state["chat_name"] = str(function.get("name") or "")
            return state

        def ensure_tool_added(state: dict[str, Any]) -> None:
            if state.get("added"):
                return
            chat_name = str(state.get("chat_name") or "")
            if not chat_name:
                return
            call_id = str(state.get("call_id") or f"call_{state.get('index', 0)}")
            item_id = response_tool_call_item_id_from_chat_name(call_id, chat_name, tool_context)
            item = response_tool_call_item_from_chat_name(item_id, "in_progress", call_id, chat_name, "", tool_context)
            self._write_sse_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": int(state["output_index"]),
                "item": item,
            })
            state["item_id"] = item_id
            state["stream_item_type"] = item.get("type")
            state["added"] = True

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
            ensure_started(chunk if isinstance(chunk, dict) else None)
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta", {}) if isinstance(choice.get("delta"), dict) else {}
                content = delta.get("content")
                if content:
                    ensure_text_item()
                    piece = str(content)
                    text_parts.append(piece)
                    self._write_sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": "msg_0",
                        "output_index": text_output_index,
                        "content_index": 0,
                        "delta": piece,
                    })
                raw_tool_calls = delta.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    for raw_tool_call in raw_tool_calls:
                        if not isinstance(raw_tool_call, dict):
                            continue
                        state = state_for_tool_call(raw_tool_call)
                        function = raw_tool_call.get("function") if isinstance(raw_tool_call.get("function"), dict) else {}
                        arg_delta = function.get("arguments")
                        ensure_tool_added(state)
                        if arg_delta is not None:
                            piece = str(arg_delta)
                            state["arguments_parts"].append(piece)
                            ensure_tool_added(state)
                            if state.get("added") and state.get("stream_item_type") == "function_call":
                                self._write_sse_event("response.function_call_arguments.delta", {
                                    "type": "response.function_call_arguments.delta",
                                    "item_id": str(state.get("item_id") or f"fc_{state.get('call_id', '')}"),
                                    "output_index": int(state["output_index"]),
                                    "delta": piece,
                                })

        ensure_started(None)
        final_output_pairs: list[tuple[int, dict[str, Any]]] = []
        final_text = "".join(text_parts)
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

        for _tool_index, state in sorted(tool_states.items(), key=lambda kv: int(kv[1].get("output_index", kv[0]))):
            chat_name = str(state.get("chat_name") or "")
            if not chat_name:
                continue
            ensure_tool_added(state)
            call_id = str(state.get("call_id") or f"call_{state.get('index', 0)}")
            arguments = canonicalize_tool_arguments("".join(state.get("arguments_parts") or []))
            item_id = str(state.get("item_id") or response_tool_call_item_id_from_chat_name(call_id, chat_name, tool_context))
            final_item = response_tool_call_item_from_chat_name(item_id, "completed", call_id, chat_name, arguments, tool_context)
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
        completed = {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": response_model,
            "output_text": final_text,
            "output": final_output,
            "usage": chat_usage_to_responses_usage(usage),
        }
        self._write_sse_event("response.completed", {"type": "response.completed", "response": completed})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_messages_stream_as_responses(self, resp: requests.Response, model: str = "") -> None:
        response_id = f"resp_{int(time.time() * 1000)}"
        response_model = model
        text_parts: list[str] = []
        started = False

        self._send_response_stream_headers(resp.status_code)
        if self.command == "HEAD":
            return

        def ensure_started(chunk: dict[str, Any] | None = None) -> None:
            nonlocal started, response_id, response_model
            if chunk:
                response_id = str(chunk.get("id") or response_id)
                response_model = str(chunk.get("model") or response_model)
            if not started:
                self._write_response_stream_start(response_id, response_model)
                started = True

        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace")
            data = line[5:].strip() if line.startswith("data:") else ""
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            ensure_started(chunk.get("message") if isinstance(chunk.get("message"), dict) else chunk)

            # OpenAI Chat Completions stream shape.
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if isinstance(choices, list):
                for choice in choices:
                    delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                    content = delta.get("content") if isinstance(delta, dict) else ""
                    if content:
                        piece = str(content)
                        text_parts.append(piece)
                        self._write_sse_event("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "item_id": "msg_0",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": piece,
                        })
                continue

            # Anthropic Messages stream shape.
            event_type = str(chunk.get("type") or "") if isinstance(chunk, dict) else ""
            piece = ""
            if event_type == "content_block_start":
                block = chunk.get("content_block") if isinstance(chunk.get("content_block"), dict) else {}
                if block.get("type") == "text":
                    piece = str(block.get("text") or "")
            elif event_type == "content_block_delta":
                delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                if delta.get("type") == "text_delta":
                    piece = str(delta.get("text") or "")
            elif event_type == "message_start" and isinstance(chunk.get("message"), dict):
                message = chunk["message"]
                response_id = str(message.get("id") or response_id)
                response_model = str(message.get("model") or response_model)
            if piece:
                text_parts.append(piece)
                self._write_sse_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": "msg_0",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": piece,
                })

        ensure_started(None)
        self._write_response_stream_done(response_id, response_model, "".join(text_parts), emit_text=False)

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
                body_json = responses_payload_to_anthropic_messages(body_json)
                convert_response_from = "messages"
            else:
                conversion_tool_context = build_codex_tool_context_from_request(body_json)
                body_json = responses_payload_to_chat(body_json, conversion_tool_context)
                convert_response_from = "messages" if provider_api_mode == "messages" else "chat"
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

        if convert_response_from and resp.status_code < 400:
            try:
                response_model = str((body_json or {}).get("model") or "")
                if isinstance(body_json, dict) and body_json.get("stream"):
                    if "text/event-stream" in str(resp.headers.get("Content-Type") or "").lower():
                        if convert_response_from == "chat":
                            self._send_chat_stream_as_responses(resp, model=response_model, tool_context=conversion_tool_context)
                        else:
                            self._send_messages_stream_as_responses(resp, model=response_model)
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
