#!/usr/bin/env python3
"""Multi-provider OpenAI-compatible header injection proxy.

Hermes can point custom providers at local URLs such as:

    http://127.0.0.1:18006/provider-a/v1

This proxy reads the real upstream provider list from config.yaml, injects
per-provider API keys/headers, synthesizes /models from config metadata, and
forwards OpenAI-compatible requests to the upstream base_url.
"""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import json
import queue
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
DEFAULT_POOL_MAXSIZE = 20
DEFAULT_CONNECT_TIMEOUT = 30
# Hermes' codex stream stale detector kills local requests after ~120s without
# stream bytes. Keep the proxy read timeout slightly lower so the proxy closes
# upstream sockets first, instead of letting Hermes disconnect and leaving long
# lived broken streams behind.
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
        custom_endpoint = str(entry.get("custom_endpoint") or entry.get("endpoint") or "").strip()
        if custom_endpoint and not custom_endpoint.startswith("/"):
            custom_endpoint = "/" + custom_endpoint
        normalized["providers"][name] = {
            "base_url": base_url,
            "api_key": str(entry.get("api_key") or entry.get("key") or "").strip(),
            "api_mode": str(entry.get("api_mode") or "").strip(),
            "custom_endpoint": custom_endpoint,
            "headers": {str(k): "" if v is None else str(v) for k, v in headers.items()},
            "remove_headers": {str(h).lower() for h in remove_headers},
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
    InvalidChunkLength, CLOSE-WAIT, and BrokenPipe errors in later Hermes turns.
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


def responses_content_to_chat(content: Any) -> Any:
    """Normalize Responses API message content for Chat Completions providers.

    Codex/Responses commonly sends text parts as {"type": "input_text", ...}.
    Some OpenAI-compatible gateways forward chat content blocks directly to
    Anthropic/Bedrock, where "input_text" is invalid and causes Bedrock
    ValidationException. Prefer plain strings for text-only content, and only
    keep a content-part list when non-text parts (for example images) are
    present.
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


def responses_input_item_to_chat_message(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        role = str(item.get("role") or "user").strip() or "user"
        if role == "developer":
            role = "system"
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        if "content" in item:
            content = responses_content_to_chat(item.get("content", ""))
        else:
            content = responses_content_to_chat(item)
        message: dict[str, Any] = {"role": role, "content": content}
        if role == "tool" and item.get("tool_call_id"):
            message["tool_call_id"] = item.get("tool_call_id")
        return message
    return {"role": "user", "content": responses_content_to_chat(item)}


def responses_payload_to_chat(body_json: dict[str, Any]) -> dict[str, Any]:
    chat: dict[str, Any] = {"model": body_json.get("model")}
    if body_json.get("max_output_tokens") is not None:
        chat["max_tokens"] = body_json.get("max_output_tokens")
    elif body_json.get("max_tokens") is not None:
        chat["max_tokens"] = body_json.get("max_tokens")

    for key in ("stream", "temperature", "top_p", "frequency_penalty", "presence_penalty", "stop", "seed", "user"):
        if key in body_json:
            chat[key] = body_json.get(key)

    messages = []
    instructions = body_json.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    input_value = body_json.get("input")
    if isinstance(input_value, list):
        for item in input_value:
            messages.append(responses_input_item_to_chat_message(item))
    elif input_value is not None:
        messages.append({"role": "user", "content": responses_content_to_chat(input_value)})
    else:
        messages.append({"role": "user", "content": ""})
    chat["messages"] = messages
    return {k: v for k, v in chat.items() if v is not None}


def chat_payload_text(chat_payload: dict[str, Any]) -> str:
    choice = None
    choices = chat_payload.get("choices") if isinstance(chat_payload, dict) else None
    if isinstance(choices, list) and choices:
        choice = choices[0]
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


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


def chat_payload_to_responses(chat_payload: dict[str, Any], model: str = "") -> dict[str, Any]:
    response_id = str(chat_payload.get("id") or f"resp_{int(time.time() * 1000)}")
    response_model = model or str(chat_payload.get("model") or "")
    return response_payload_from_text(response_id, response_model, chat_payload_text(chat_payload), chat_payload.get("usage"))


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
        super().__init__(server_address, RequestHandlerClass)
        self.config = config
        self.session_pool = ProviderSessionPool()

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

    def _write_response_stream_start(self, response_id: str, model: str) -> None:
        response = response_payload_from_text(response_id, model, "", status="in_progress")
        self._write_sse_event("response.created", {"type": "response.created", "response": response})
        self._write_sse_event("response.in_progress", {"type": "response.in_progress", "response": response})
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

    def _send_chat_payload_as_responses_stream(self, status: int, chat_payload: dict[str, Any], model: str = "") -> None:
        response_id = str(chat_payload.get("id") or f"resp_{int(time.time() * 1000)}")
        response_model = model or str(chat_payload.get("model") or "")
        text = chat_payload_text(chat_payload)
        self._send_response_stream_headers(status)
        if self.command == "HEAD":
            return
        self._write_response_stream_start(response_id, response_model)
        if text:
            self._write_sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": "msg_0",
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            })
        self._write_response_stream_done(response_id, response_model, text, chat_payload.get("usage"), emit_text=False)

    def _send_chat_stream_as_responses(self, resp: requests.Response, model: str = "") -> None:
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
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            ensure_started(chunk)
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if not isinstance(choices, list):
                continue
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
        if custom_endpoint and route_path in {"/responses", "/chat/completions"}:
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
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body, body_json = maybe_apply_reasoning(provider, proxied_path, body)
        convert_chat_response = False
        if (
            self.command == "POST"
            and route_path == "/responses"
            and str(provider.get("api_mode") or "").strip().lower() == "chat_completions"
            and isinstance(body_json, dict)
        ):
            query = "?" + proxied_path.split("?", 1)[1] if "?" in proxied_path else ""
            upstream_path = "/chat/completions" + query
            upstream_route_path = "/chat/completions"
            url = base_url + upstream_path
            body_json = responses_payload_to_chat(body_json)
            body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
            convert_chat_response = True
        if body_json is not None:
            headers["Content-Type"] = "application/json"

        session = self.server.session_pool.borrow(provider_name, provider)

        started = time.time()

        def send_upstream(active_session: requests.Session) -> tuple[requests.Response, bool]:
            active_convert_chat_response = convert_chat_response
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
                and not body_json.get("stream")
            ):
                active_resp.close()
                fallback_body_json = responses_payload_to_chat(body_json)
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
                active_convert_chat_response = True
            return active_resp, active_convert_chat_response

        try:
            try:
                resp, convert_chat_response = send_upstream(session)
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
                resp, convert_chat_response = send_upstream(session)
        except Exception as exc:
            self.server.log_error_always(self, "%s %s -> %s proxy_error=%s", provider_name, self.command, url, repr(exc))
            self._send_text(502, f"proxy error: {type(exc).__name__}: {exc}\n")
            self.server.session_pool.discard(session)
            return

        elapsed = time.time() - started
        self.server.log_if_verbose(self, "%s %s %s -> HTTP %s %.2fs", provider_name, self.command, proxied_path, resp.status_code, elapsed)

        if convert_chat_response and resp.status_code < 400:
            try:
                response_model = str((body_json or {}).get("model") or "")
                if isinstance(body_json, dict) and body_json.get("stream"):
                    if "text/event-stream" in str(resp.headers.get("Content-Type") or "").lower():
                        self._send_chat_stream_as_responses(resp, model=response_model)
                    else:
                        self._send_chat_payload_as_responses_stream(resp.status_code, resp.json(), model=response_model)
                else:
                    chat_payload = resp.json()
                    converted = chat_payload_to_responses(chat_payload, model=response_model)
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
    parser = argparse.ArgumentParser(description="Multi-provider header injection proxy for Hermes/OpenAI-compatible APIs")
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

    print(f"Hermes header proxy listening on http://{cfg['listen']}:{cfg['port']} verbose={cfg.get('verbose', False)}")
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
