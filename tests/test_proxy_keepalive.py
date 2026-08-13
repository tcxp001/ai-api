"""抢通 + 保温（keepalive）测试。全部打本地假上游，不碰真网络。"""

import importlib.util
import contextlib
import io
import json
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]

SPEC = importlib.util.spec_from_file_location("ai_api_proxy", ROOT / "proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(proxy)

DASH_SPEC = importlib.util.spec_from_file_location("ai_api_dashboard", ROOT / "dashboard.py")
dashboard = importlib.util.module_from_spec(DASH_SPEC)
assert DASH_SPEC.loader is not None
DASH_SPEC.loader.exec_module(dashboard)

PROMPTS_SPEC = importlib.util.spec_from_file_location("ai_api_prompts", ROOT / "prompts.py")
prompts = importlib.util.module_from_spec(PROMPTS_SPEC)
assert PROMPTS_SPEC.loader is not None
PROMPTS_SPEC.loader.exec_module(prompts)


def provider_entry(base_url: str, **overrides) -> dict:
    entry = {
        "name": "any",
        "base_url": base_url,
        "api_mode": "codex_responses",
        "models": {"gpt-5.6-sol": {}},
        "api_key": "sk-unit-test-key-0001",
        "keepalive": True,
        "keepalive_interval": 5,
        "keepalive_timeout": 5,
        "keepalive_concurrency": 3,
    }
    entry.update(overrides)
    return entry


def build_config(base_url: str, **overrides) -> dict:
    cfg = proxy.load_config_from_data([provider_entry(base_url, **overrides)])
    cfg["verbose"] = False
    return cfg


class FakeResponse:
    """够 validate_keepalive_response 用的最小 requests.Response 替身。"""

    def __init__(self, status: int = 200, body: str = "", content_type: str = "application/json", lines=None):
        self.status_code = status
        self.text = body
        self.headers = {"content-type": content_type}
        self._lines = lines or []

    def json(self):
        return json.loads(self.text)

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line if isinstance(line, bytes) else str(line).encode("utf-8")

    def close(self):
        pass


def sse(*events: dict) -> list[bytes]:
    return [b"data: " + json.dumps(event).encode("utf-8") for event in events]


SSE_COMPLETED = (
    b'data: {"type": "response.created"}\n\n'
    b'data: {"type": "response.output_text.delta", "delta": "\xe5\x9c\xa8"}\n\n'
    b'data: {"type": "response.completed"}\n\n'
)
JSON_THROTTLED = b'{"error": {"type": "rate_limit_error", "message": "rate limit reached"}}'


class FakeUpstream:
    """本地假上游。mode 决定哪些请求能拿到"真实回复"，其余一律 429。

    - ``single_winner``: 只有最先到达的那个请求返回 response.completed(抢通赛跑)
    - ``all_ok``:       每个请求都成功(保温)
    - ``all_fail``:     每个请求都 429
    - ``fail_first``:   第一个请求 429，之后都成功(保温立刻重试的场景)
    """

    def __init__(self, mode: str = "single_winner") -> None:
        self.mode = mode
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.headers_seen: list[dict] = []
        self.winner_taken = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D102 - 静音，别污染测试输出
                pass

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = {}
                with outer.lock:
                    outer.requests.append(payload)
                    outer.headers_seen.append(dict(self.headers))
                    win = (
                        outer.mode == "all_ok"
                        or (outer.mode == "fail_first" and len(outer.requests) > 1)
                        or (outer.mode == "single_winner" and not outer.winner_taken)
                    )
                    if win:
                        outer.winner_taken = True
                if win:
                    outer.reply(self, 200, "text/event-stream", SSE_COMPLETED)
                else:
                    outer.reply(self, 429, "application/json", JSON_THROTTLED)

        self._handler = Handler
        self._server: ThreadingHTTPServer | None = None

    @staticmethod
    def reply(handler: BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
        handler.send_response(status)
        handler.send_header("content-type", content_type)
        handler.send_header("content-length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def prompts_seen(self) -> list[str]:
        out = []
        with self.lock:
            for payload in self.requests:
                items = payload.get("input") or payload.get("messages") or []
                out.extend(str(item.get("content") or "") for item in items)
        return out


class RecordingPool(proxy.ProviderSessionPool):
    """记录 borrow/release/discard/fresh，用来断言"胜者进池、输家全关"。"""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[requests.Session] = []
        self.borrowed: list[requests.Session] = []
        self.released: list[requests.Session] = []
        self.discarded: list[requests.Session] = []

    def _new_session(self, provider):
        session = super()._new_session(provider)
        self.created.append(session)
        return session

    def borrow(self, provider_name, provider):
        session = super().borrow(provider_name, provider)
        self.borrowed.append(session)
        return session

    def release(self, provider_name, provider, session):
        self.released.append(session)
        super().release(provider_name, provider, session)

    def discard(self, session):
        self.discarded.append(session)
        super().discard(session)

    def pooled(self, provider_name: str) -> list[requests.Session]:
        pool = self._pools.get(provider_name)
        return list(pool.queue) if pool is not None else []


class DeadSession(requests.Session):
    """模拟池子里那条被上游按空闲超时掐掉的连接。"""

    def __init__(self, exc: BaseException | None = None) -> None:
        super().__init__()
        self.calls = 0
        self._exc = exc or requests.exceptions.ConnectionError(
            "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
        )

    def post(self, *args, **kwargs):
        self.calls += 1
        raise self._exc


def seeded_manager(cfg: dict, pool: proxy.ProviderSessionPool, name: str = "any") -> proxy.KeepAliveManager:
    """手工播一份状态（键与 start() 一致），不起真线程也能断言计数器。"""
    manager = proxy.KeepAliveManager(cfg, pool)
    manager._states[name] = {
        "state": proxy.KEEPALIVE_STATE_WARM,
        "since": time.time(),
        "note": "seeded by test",
        "attempts": 0,
        "okCount": 0,
        "failCount": 0,
        "recycled": 0,
        "lastOkAt": None,
        "lastErrorAt": None,
        "lastError": "",
        "latencyMs": None,
        "nextProbeAt": None,
    }
    return manager


def state_of(manager: proxy.KeepAliveManager, name: str = "any") -> dict:
    return manager.snapshot()["providers"][name]


class KeepAliveConfigTest(unittest.TestCase):
    def test_defaults_to_disabled_and_untouched_behaviour(self):
        cfg = proxy.load_config_from_data([
            {"name": "plain", "base_url": "https://plain.invalid/v1", "api_mode": "chat_completions", "models": ["m"]}
        ])
        provider = cfg["providers"]["plain"]
        self.assertFalse(provider["keepalive"])
        self.assertEqual(provider["keepalive_interval"], proxy.DEFAULT_KEEPALIVE_INTERVAL)
        self.assertEqual(provider["keepalive_interval"], 30.0)
        self.assertEqual(provider["keepalive_retry_interval"], proxy.DEFAULT_KEEPALIVE_RETRY_INTERVAL)
        self.assertEqual(provider["keepalive_retry_interval"], 5.0)
        self.assertEqual(provider["keepalive_concurrency"], proxy.DEFAULT_KEEPALIVE_CONCURRENCY)
        self.assertEqual(provider["keepalive_timeout"], proxy.DEFAULT_KEEPALIVE_TIMEOUT)
        self.assertEqual(provider["keepalive_model"], "")
        self.assertEqual(provider["keepalive_max_attempts"], 0)

    def test_explicit_values_and_clamping(self):
        cfg = proxy.load_config_from_data([{
            "name": "warm",
            "base_url": "https://warm.invalid/v1",
            "api_mode": "codex_responses",
            "models": {"a": {}, "b": {}},
            "keepalive": "yes",
            "keepalive_interval": 1,          # below the floor -> clamped to 5
            "keepalive_retry_interval": 9999, # above the ceiling -> clamped to 3600
            "keepalive_timeout": 9999,        # above the ceiling -> clamped to 600
            "keepalive_concurrency": 999,     # clamped to KEEPALIVE_CONCURRENCY_MAX
            "keepalive_model": "  b  ",
            "keepalive_max_attempts": -4,     # negative means "forever" (0)
        }])
        provider = cfg["providers"]["warm"]
        self.assertTrue(provider["keepalive"])
        self.assertEqual(provider["keepalive_interval"], 5.0)
        self.assertEqual(provider["keepalive_retry_interval"], 3600.0)
        self.assertEqual(provider["keepalive_timeout"], 600.0)
        self.assertEqual(provider["keepalive_concurrency"], proxy.KEEPALIVE_CONCURRENCY_MAX)
        self.assertEqual(provider["keepalive_model"], "b")
        self.assertEqual(provider["keepalive_max_attempts"], 0)

    def test_model_falls_back_to_first_configured_model(self):
        cfg = build_config("https://x.invalid/v1")
        self.assertEqual(proxy.keepalive_model_for(cfg["providers"]["any"]), "gpt-5.6-sol")
        cfg = build_config("https://x.invalid/v1", keepalive_model="pinned")
        self.assertEqual(proxy.keepalive_model_for(cfg["providers"]["any"]), "pinned")

    def test_endpoint_per_api_mode(self):
        cases = {
            "codex_responses": "/responses",
            "responses": "/responses",
            "chat_completions": "/chat/completions",
            "messages": "/messages",
        }
        for api_mode, expected in cases.items():
            provider = proxy.load_config_from_data([
                {"name": "p", "base_url": "https://p.invalid/v1", "api_mode": api_mode, "models": ["m"]}
            ])["providers"]["p"]
            self.assertEqual(proxy.keepalive_target_path(provider), expected, api_mode)
        custom = proxy.load_config_from_data([{
            "name": "p", "base_url": "https://p.invalid/v1", "api_mode": "custom_endpoint",
            "custom_endpoint": "/v1beta/chat", "models": ["m"],
        }])["providers"]["p"]
        self.assertEqual(proxy.keepalive_target_path(custom), "/v1beta/chat")

    def test_dashboard_drops_every_field_when_unchecked(self):
        entry = {"name": "any", "base_url": "https://x.invalid/v1", "api_mode": "codex_responses", "models": {"m": {}}}
        validated = dashboard.validate_provider(dict(entry), 1)
        self.assertFalse(validated["keepalive"])
        compacted = dashboard.compact_provider(validated)
        for field in dashboard.KEEPALIVE_FIELDS:
            self.assertNotIn(field, compacted)

    def test_dashboard_keeps_only_non_default_fields_when_checked(self):
        entry = {
            "name": "any", "base_url": "https://x.invalid/v1", "api_mode": "codex_responses",
            "models": {"m": {}}, "keepalive": True, "keepalive_interval": 120,
            "keepalive_retry_interval": 7,
        }
        compacted = dashboard.compact_provider(dashboard.validate_provider(entry, 1))
        self.assertTrue(compacted["keepalive"])
        self.assertEqual(compacted["keepalive_interval"], 120)
        self.assertEqual(compacted["keepalive_retry_interval"], 7)
        self.assertNotIn("keepalive_concurrency", compacted)
        self.assertNotIn("keepalive_timeout", compacted)
        self.assertNotIn("keepalive_max_attempts", compacted)
        self.assertNotIn("keepalive_model", compacted)

    def test_dashboard_rejects_out_of_range_values(self):
        base = {"name": "any", "base_url": "https://x.invalid/v1", "api_mode": "codex_responses", "models": {"m": {}}, "keepalive": True}
        for bad in (
            {"keepalive_interval": 2},
            {"keepalive_retry_interval": 0},
            {"keepalive_concurrency": 999},
            {"keepalive_timeout": "abc"},
            {"keepalive_max_attempts": -1},
        ):
            with self.assertRaises(ValueError):
                dashboard.validate_provider({**base, **bad}, 1)

    def test_proxy_process_scopes_keepalive_provider_to_one_instance(self):
        cfg = proxy.load_config_from_data([
            {"name": "warm", "base_url": "https://warm.invalid/v1", "models": ["m"], "keepalive": True},
            {"name": "plain", "base_url": "https://plain.invalid/v1", "models": ["m"]},
        ])
        dedicated = proxy.filter_config_providers({**cfg, "providers": dict(cfg["providers"])}, keepalive_only=True)
        regular = proxy.filter_config_providers({**cfg, "providers": dict(cfg["providers"])}, exclude_keepalive=True)
        self.assertEqual(set(dedicated["providers"]), {"warm"})
        self.assertEqual(set(regular["providers"]), {"plain"})

    def test_dashboard_routes_keepalive_provider_to_companion_port(self):
        providers = [
            {"name": "warm", "keepalive": True},
            {"name": "plain", "keepalive": False},
        ]
        block = dashboard.generated_codex_provider_block(providers, "http://192.168.2.10:18006")
        self.assertIn('base_url = "http://192.168.2.10:18007/warm/v1"', block)
        self.assertIn('base_url = "http://192.168.2.10:18006/plain/v1"', block)

    def test_systemd_units_split_regular_and_keepalive_traffic_and_always_restart(self):
        main = dashboard.default_aiproxy_item({"listen": "127.0.0.1", "port": 18006})
        main["scopeFlag"] = "--exclude-keepalive"
        dedicated = dashboard.keepalive_aiproxy_item(main)
        main_unit = dashboard.aiproxy_unit_content(main)
        dedicated_unit = dashboard.aiproxy_unit_content(dedicated)
        self.assertIn("--exclude-keepalive", main_unit)
        self.assertIn("--port 18006", main_unit)
        self.assertIn("Restart=always", main_unit)
        self.assertIn("--keepalive-only", dedicated_unit)
        self.assertIn("--port 18007", dedicated_unit)
        self.assertIn("Restart=always", dedicated_unit)

    def test_systemd_unit_discovery_preserves_proxy_scope(self):
        units = {
            dashboard.AIPROXY_SINGLE_SERVICE: (
                "[Service]\n"
                "ExecStart=/usr/bin/python3 /mnt/ai-api/proxy.py --config /mnt/ai-api/config.yaml "
                "--listen 127.0.0.1 --port 18006 --exclude-keepalive\n"
            ),
            dashboard.AIPROXY_KEEPALIVE_SERVICE: (
                "[Service]\n"
                "ExecStart=/usr/bin/python3 /mnt/ai-api/proxy.py --config /mnt/ai-api/config.yaml "
                "--listen 127.0.0.1 --port 18007 --keepalive-only\n"
            ),
        }
        with mock.patch.object(
            dashboard,
            "read_systemd_unit",
            side_effect=lambda name: (units[name], f"/etc/systemd/system/{name}"),
        ):
            main = dashboard.named_aiproxy_service_item(dashboard.AIPROXY_SINGLE_SERVICE)
            dedicated = dashboard.named_aiproxy_service_item(dashboard.AIPROXY_KEEPALIVE_SERVICE)
        self.assertEqual(main["scopeFlag"], "--exclude-keepalive")
        self.assertEqual(dedicated["scopeFlag"], "--keepalive-only")


class ExplodingStream(FakeResponse):
    """4xx/5xx 必须在读流之前就判失败，读一下就炸给我看。"""

    def iter_lines(self, decode_unicode=False):
        raise AssertionError("error status must fail immediately without reading the stream")

    def json(self):
        raise AssertionError("error status must fail immediately without parsing the body")


class KeepAliveJudgementTest(unittest.TestCase):
    """对应 atry 的 role-aware reply detection：只有真实回复算通。"""

    def test_success_shapes(self):
        cases = {
            "responses stream": (
                FakeResponse(200, content_type="text/event-stream", lines=sse({"type": "response.created"}, {"type": "response.completed"})),
                "/responses",
            ),
            "responses non-stream fallback": (
                FakeResponse(200, json.dumps({"status": "completed", "output": [{"type": "message"}]})),
                "/responses",
            ),
            "chat content": (
                FakeResponse(200, json.dumps({"choices": [{"message": {"content": "在"}}]})),
                "/chat/completions",
            ),
            "messages content": (
                FakeResponse(200, json.dumps({"content": [{"type": "text", "text": "在"}]})),
                "/messages",
            ),
            "messages stop_reason only": (
                FakeResponse(200, json.dumps({"content": [], "stop_reason": "end_turn"})),
                "/messages",
            ),
            # 线上实测：DS(deepseek 推理模型) 会把探活的 max_tokens 全花在 reasoning 上，
            # content 为空但这一轮确实跑完了 —— 属于真实回复，不能判失败。
            "chat reasoning only": (
                FakeResponse(200, json.dumps({"choices": [{"message": {"content": "", "reasoning_content": "用户在打招呼"}, "finish_reason": "length"}]})),
                "/chat/completions",
            ),
            "chat finish_reason only": (
                FakeResponse(200, json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})),
                "/chat/completions",
            ),
            "chat streaming-shaped delta": (
                FakeResponse(200, json.dumps({"choices": [{"delta": {"content": "在"}}]})),
                "/chat/completions",
            ),
        }
        for label, (response, path) in cases.items():
            ok, detail = proxy.validate_keepalive_response(response, path)
            self.assertTrue(ok, f"{label}: {detail}")
            self.assertEqual(detail, "", label)

    def test_failure_shapes(self):
        cases = {
            "error in 200 body": (FakeResponse(200, json.dumps({"error": {"message": "bad key"}})), "/responses", "error in 200 body"),
            "html": (FakeResponse(200, "<html>502 Bad Gateway</html>", content_type="text/html"), "/responses", "HTML"),
            "non-JSON": (FakeResponse(200, "upstream boom"), "/chat/completions", "non-JSON body"),
            "empty SSE": (FakeResponse(200, content_type="text/event-stream", lines=[]), "/responses", "empty SSE stream"),
            "only [DONE]": (FakeResponse(200, content_type="text/event-stream", lines=[b"data: [DONE]"]), "/responses", "empty SSE stream"),
            "response.failed": (
                FakeResponse(200, content_type="text/event-stream", lines=sse({"type": "response.created"}, {"type": "response.failed"})),
                "/responses",
                "response.failed",
            ),
            "response.incomplete": (
                FakeResponse(200, content_type="text/event-stream", lines=sse({"type": "response.incomplete"})),
                "/responses",
                "response.incomplete",
            ),
            "stream error event": (
                FakeResponse(200, content_type="text/event-stream", lines=sse({"type": "error", "error": "upstream gone"})),
                "/responses",
                "stream error",
            ),
            "stream without completed": (
                FakeResponse(200, content_type="text/event-stream", lines=sse({"type": "response.output_text.delta", "delta": "在"})),
                "/responses",
                "without response.completed",
            ),
            "throttle marker": (FakeResponse(200, json.dumps({"message": "Rate limit exceeded, retry later"})), "/chat/completions", "throttled"),
            "chat without choices": (FakeResponse(200, json.dumps({"id": "x"})), "/chat/completions", "without choices"),
            "chat empty content": (FakeResponse(200, json.dumps({"choices": [{"message": {"content": "  "}}]})), "/chat/completions", "without content"),
            "messages empty": (FakeResponse(200, json.dumps({"content": []})), "/messages", "without content"),
            "responses without output": (FakeResponse(200, json.dumps({"id": "resp_1"})), "/responses", "without output"),
        }
        for label, (response, path, needle) in cases.items():
            ok, detail = proxy.validate_keepalive_response(response, path)
            self.assertFalse(ok, label)
            self.assertIn(needle, detail, label)

    def test_error_status_fails_immediately(self):
        for status in (429, 500, 502, 401):
            ok, detail = proxy.validate_keepalive_response(ExplodingStream(status, content_type="text/event-stream"), "/responses")
            self.assertFalse(ok)
            self.assertIn(f"HTTP {status}", detail)

    def test_model_reply_mentioning_rate_limit_is_not_a_failure(self):
        body = json.dumps({"choices": [{"message": {"content": "rate limit 是限流的意思"}}]})
        ok, detail = proxy.validate_keepalive_response(FakeResponse(200, body), "/chat/completions")
        self.assertTrue(ok, detail)

    def test_stale_socket_classification(self):
        self.assertTrue(proxy._is_stale_connection_error(requests.exceptions.ConnectionError("reset by peer")))
        self.assertTrue(proxy._is_stale_connection_error(requests.exceptions.ChunkedEncodingError("truncated")))
        # ConnectTimeout 是 ConnectionError 的子类，但它意味着上游连不上，不是池里的连接坏了。
        self.assertFalse(proxy._is_stale_connection_error(requests.exceptions.ConnectTimeout("connect timeout")))
        self.assertFalse(proxy._is_stale_connection_error(requests.exceptions.ReadTimeout("read timeout")))

    def test_secrets_are_redacted_in_failure_details(self):
        redacted = proxy.keepalive_redact("Bearer sk-unit-test-key-0001 rejected")
        self.assertNotIn("sk-unit-test-key-0001", redacted)


class KeepAliveAcquireTest(unittest.TestCase):
    """抢通：并发 N 个独立 session 赛跑，赢家进池，输家全部关掉。"""

    def tearDown(self):
        self.upstream.stop()
        self.pool.close_all()

    def build(self, mode: str, concurrency: int = 10):
        self.upstream = FakeUpstream(mode)
        base_url = self.upstream.start()
        self.cfg = build_config(base_url, keepalive_concurrency=concurrency, keepalive_timeout=5)
        self.provider = self.cfg["providers"]["any"]
        self.pool = RecordingPool()
        return seeded_manager(self.cfg, self.pool)

    def test_single_winner_lands_in_the_pool_and_losers_are_closed(self):
        manager = self.build("single_winner", concurrency=10)
        ok, detail = manager._acquire("any", self.provider, 10)
        self.assertTrue(ok, detail)
        self.assertTrue(self.upstream.winner_taken)
        # 每个赛跑线程一条独立 session；池子里只留胜者那条。
        self.assertEqual(len(self.pool.created), 10)
        pooled = self.pool.pooled("any")
        self.assertEqual(len(pooled), 1)
        winner = pooled[0]
        self.assertEqual(self.pool.released, [winner])
        self.assertNotIn(winner, self.pool.discarded)
        self.assertEqual(len(self.pool.discarded), 9)
        self.assertEqual(set(self.pool.discarded) | {winner}, set(self.pool.created))
        self.assertIsNotNone(state_of(manager)["latencyMs"])

    def test_first_winner_returns_without_waiting_for_slow_losers(self):
        manager = self.build("all_ok", concurrency=3)
        original_probe = manager._probe
        order_lock = threading.Lock()
        next_index = 0

        def probe(provider, session):
            nonlocal next_index
            with order_lock:
                index = next_index
                next_index += 1
            if index:
                time.sleep(1.0)
            return original_probe(provider, session)

        manager._probe = probe
        started = time.monotonic()
        ok, detail = manager._acquire("any", self.provider, 3)
        elapsed = time.monotonic() - started

        self.assertTrue(ok, detail)
        self.assertLess(elapsed, 0.75, f"winner was ready but acquire waited {elapsed:.3f}s for losers")
        self.assertEqual(len(self.pool.pooled("any")), 1)
        self.assertEqual(len(self.pool.discarded), 2)

    def test_probe_looks_like_real_client_traffic(self):
        manager = self.build("single_winner", concurrency=1)
        self.assertTrue(manager._acquire("any", self.provider, 1)[0])
        payload = self.upstream.requests[0]
        headers = self.upstream.headers_seen[0]
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["max_output_tokens"], proxy.KEEPALIVE_PROBE_MAX_OUTPUT_TOKENS)
        self.assertIn(payload["input"][0]["content"], prompts.KEEPALIVE_PROMPTS)
        self.assertEqual(headers.get("Authorization"), "Bearer sk-unit-test-key-0001")
        self.assertIn("codex", (headers.get("User-Agent") or "").lower())

    def test_all_attempts_failed_leaves_nothing_in_the_pool(self):
        manager = self.build("all_fail", concurrency=3)
        ok, detail = manager._acquire("any", self.provider, 3)
        self.assertFalse(ok)
        self.assertIn("HTTP 429", detail)
        self.assertEqual(self.pool.pooled("any"), [])
        self.assertEqual(self.pool.released, [])
        self.assertEqual(len(self.pool.discarded), len(self.pool.created))

    def test_missing_model_fails_without_touching_the_network(self):
        manager = self.build("all_ok", concurrency=1)
        provider = dict(self.provider, models={}, keepalive_model="")
        ok, detail = manager._acquire("any", provider, 1)
        self.assertFalse(ok)
        self.assertIn("no model configured", detail)
        self.assertEqual(self.upstream.requests, [])


class KeepAliveWarmTest(unittest.TestCase):
    """保温：borrow 池中连接 → 探活 → release 回池，走真实请求同一条路径。"""

    def tearDown(self):
        self.upstream.stop()
        self.pool.close_all()

    def build(self, mode: str):
        self.upstream = FakeUpstream(mode)
        base_url = self.upstream.start()
        self.cfg = build_config(base_url, keepalive_timeout=5)
        self.provider = self.cfg["providers"]["any"]
        self.pool = RecordingPool()
        return seeded_manager(self.cfg, self.pool)

    def seed_pooled(self, session=None):
        session = session or self.pool.fresh(self.provider)
        self.pool.release("any", self.provider, session)
        self.pool.released.clear()
        return session

    def test_success_reuses_the_pooled_session(self):
        manager = self.build("all_ok")
        pooled = self.seed_pooled()
        ok, detail = manager._keepalive_once("any", self.provider)
        self.assertTrue(ok, detail)
        self.assertIs(self.pool.borrowed[-1], pooled)
        self.assertEqual(self.pool.released, [pooled])
        self.assertEqual(self.pool.pooled("any"), [pooled])
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.pool.discarded, [])
        self.assertEqual(state_of(manager)["recycled"], 0)

    def test_stale_pooled_connection_is_recycled_not_counted_as_failure(self):
        manager = self.build("all_ok")
        dead = self.seed_pooled(DeadSession())
        ok, detail = manager._keepalive_once("any", self.provider)
        self.assertTrue(ok, detail)
        self.assertEqual(dead.calls, 1)
        self.assertIn(dead, self.pool.discarded)
        # 换来的新连接进了池子，坏的那条没有。
        pooled = self.pool.pooled("any")
        self.assertEqual(len(pooled), 1)
        self.assertIsNot(pooled[0], dead)
        self.assertEqual(len(self.upstream.requests), 1)
        state = state_of(manager)
        self.assertEqual(state["recycled"], 1)
        self.assertEqual(state.get("failCount") or 0, 0)
        self.assertNotEqual(state["state"], proxy.KEEPALIVE_STATE_LOST)

    def test_stale_then_still_broken_reports_both_details(self):
        manager = self.build("all_fail")
        dead = self.seed_pooled(DeadSession())
        ok, detail = manager._keepalive_once("any", self.provider)
        self.assertFalse(ok)
        self.assertIn("ConnectionError", detail)
        self.assertIn("retry=", detail)
        self.assertIn("HTTP 429", detail)
        self.assertEqual(self.pool.pooled("any"), [])
        self.assertIn(dead, self.pool.discarded)
        self.assertEqual(state_of(manager)["recycled"], 0)

    def test_protocol_failure_retries_once_on_the_same_session(self):
        manager = self.build("all_fail")
        pooled = self.seed_pooled()
        ok, detail = manager._keepalive_once("any", self.provider)
        self.assertFalse(ok)
        self.assertIn("HTTP 429", detail)
        # 立刻重试一次；socket 没坏，所以连接还回池子。
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.pool.pooled("any"), [pooled])
        self.assertEqual(self.pool.discarded, [])

    def test_protocol_failure_recovers_on_the_immediate_retry(self):
        manager = self.build("fail_first")  # 第一发 429，重试那发成功
        pooled = self.seed_pooled()
        ok, detail = manager._keepalive_once("any", self.provider)
        self.assertTrue(ok, detail)
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.pool.pooled("any"), [pooled])
        self.assertEqual(state_of(manager).get("failCount") or 0, 0)

    def test_prompts_rotate_across_probes(self):
        manager = self.build("all_ok")
        self.seed_pooled()
        for _ in range(6):
            self.assertTrue(manager._keepalive_once("any", self.provider)[0])
        seen = self.upstream.prompts_seen()
        self.assertEqual(len(seen), 6)
        self.assertEqual(len(set(seen)), 6, seen)
        for prompt in seen:
            self.assertIn(prompt, prompts.KEEPALIVE_PROMPTS)


class ScriptedManager(proxy.KeepAliveManager):
    """脚本化 _acquire/_keepalive_once/_sleep，纯粹驱动状态机：不碰网络也不真睡。"""

    def __init__(self, cfg, acquire_results=(), keepalive_results=(), stop_after_sleeps=None):
        super().__init__(cfg, proxy.ProviderSessionPool())
        self.acquire_results = list(acquire_results)
        self.keepalive_results = list(keepalive_results)
        self.acquire_calls: list[int] = []
        self.keepalive_calls = 0
        self.sleeps: list[float] = []
        self.stop_after_sleeps = stop_after_sleeps
        self.logs: list[tuple[str, bool]] = []
        self._states["any"] = {"state": proxy.KEEPALIVE_STATE_COLD, "since": time.time(), "recycled": 0}

    def _acquire(self, name, provider, concurrency):
        self.acquire_calls.append(concurrency)
        ok = self.acquire_results.pop(0) if self.acquire_results else True
        return ok, "" if ok else "scripted acquire failure"

    def _keepalive_once(self, name, provider):
        self.keepalive_calls += 1
        ok = self.keepalive_results.pop(0) if self.keepalive_results else True
        return ok, "" if ok else "scripted keepalive failure"

    def _sleep(self, seconds):
        self.sleeps.append(round(float(seconds), 3))
        if self.stop_after_sleeps is not None and len(self.sleeps) >= self.stop_after_sleeps:
            self._stop.set()
            return False
        return True

    def _log(self, fmt, *args, always=False):
        self.logs.append((fmt % args if args else fmt, always))


class KeepAliveStateMachineTest(unittest.TestCase):
    """COLD ──抢通──→ WARM ──保温──→ WARM / LOST ──重抢(并发 1)──→ WARM"""

    def provider(self, **overrides):
        cfg = build_config("http://upstream.invalid/v1", keepalive_concurrency=10, keepalive_interval=60, **overrides)
        return cfg, cfg["providers"]["any"]

    def test_failed_acquire_retries_at_fixed_five_second_interval(self):
        cfg, provider = self.provider()
        manager = ScriptedManager(cfg, acquire_results=[False] * 9, stop_after_sleeps=9)
        manager._run_provider("any", provider, 0.0)
        self.assertEqual(manager.sleeps, [proxy.DEFAULT_KEEPALIVE_RETRY_INTERVAL] * 9)
        # 冷启动重来一轮仍然是满并发；降到 1 只发生在保温 miss 之后。
        self.assertEqual(manager.acquire_calls, [10] * 9)
        self.assertEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_STOPPED)
        self.assertEqual(state_of(manager)["failCount"], 9)

    def test_failed_acquire_uses_provider_retry_interval(self):
        cfg, provider = self.provider(keepalive_retry_interval=7)
        manager = ScriptedManager(cfg, acquire_results=[False] * 2, stop_after_sleeps=2)
        manager._run_provider("any", provider, 0.0)
        self.assertEqual(manager.sleeps, [7, 7])

    def test_keepalive_miss_reacquires_with_concurrency_one(self):
        cfg, provider = self.provider()
        manager = ScriptedManager(cfg, acquire_results=[True, True], keepalive_results=[False, True], stop_after_sleeps=3)
        manager._run_provider("any", provider, 0.0)
        # 一次保温 miss 不能扇出成又一批 10 个请求 —— 照抄 atry 的 keepAliveReacquireConfig。
        self.assertEqual(manager.acquire_calls, [10, 1])
        self.assertEqual(manager.sleeps, [60, 60, 60])
        self.assertEqual(manager.keepalive_calls, 2)
        self.assertEqual(state_of(manager)["failCount"], 1)
        self.assertGreaterEqual(state_of(manager)["okCount"], 3)

    def test_reacquire_also_uses_fixed_five_second_interval(self):
        cfg, provider = self.provider()
        manager = ScriptedManager(
            cfg,
            acquire_results=[True, False, False, True],
            keepalive_results=[False],
            stop_after_sleeps=5,
        )
        manager._run_provider("any", provider, 0.0)
        self.assertEqual(manager.acquire_calls, [10, 1, 1, 1])
        self.assertEqual(manager.sleeps, [60, 5, 5, 60, 60])

    def test_max_attempts_gives_up_and_marks_failed(self):
        cfg, provider = self.provider(keepalive_max_attempts=3)
        manager = ScriptedManager(cfg, acquire_results=[False] * 5)
        manager._run_provider("any", provider, 0.0)
        self.assertEqual(manager.acquire_calls, [10, 10, 10])
        self.assertEqual(manager.sleeps, [5, 5])
        state = state_of(manager)
        self.assertEqual(state["state"], proxy.KEEPALIVE_STATE_FAILED)
        self.assertIn("gave up after 3 attempts", state["note"])

    def test_zero_max_attempts_never_gives_up(self):
        cfg, provider = self.provider(keepalive_max_attempts=0)
        manager = ScriptedManager(cfg, acquire_results=[False] * 30, stop_after_sleeps=12)
        manager._run_provider("any", provider, 0.0)
        self.assertEqual(len(manager.acquire_calls), 12)
        self.assertNotEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_FAILED)

    def test_stop_before_first_probe(self):
        cfg, provider = self.provider()
        manager = ScriptedManager(cfg)
        manager._stop.set()
        manager._run_provider("any", provider, 0.0)
        self.assertEqual(manager.acquire_calls, [])
        self.assertEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_STOPPED)

    def test_stop_during_startup_stagger(self):
        cfg, provider = self.provider()
        manager = ScriptedManager(cfg, stop_after_sleeps=1)
        manager._run_provider("any", provider, 0.25)
        self.assertEqual(manager.sleeps, [0.25])
        self.assertEqual(manager.acquire_calls, [])
        self.assertIn("stopped before first probe", state_of(manager)["note"])

    def test_failures_are_logged_even_when_not_verbose(self):
        cfg, provider = self.provider()
        cfg["verbose"] = False
        manager = ScriptedManager(cfg, acquire_results=[False, True], keepalive_results=[False], stop_after_sleeps=3)
        manager._run_provider("any", provider, 0.0)
        always = [message for message, is_always in manager.logs if is_always]
        self.assertTrue(any("acquire failed" in message for message in always))
        self.assertTrue(any("keepalive lost" in message for message in always))

    def test_log_honours_verbose_flag(self):
        for verbose, expect_ok_line in ((False, False), (True, True)):
            cfg = build_config("http://upstream.invalid/v1")
            cfg["verbose"] = verbose
            manager = proxy.KeepAliveManager(cfg, proxy.ProviderSessionPool())
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                manager._log("any keepalive ok")
                manager._log("any keepalive lost", always=True)
            printed = buffer.getvalue()
            self.assertIn("keepalive lost", printed)
            self.assertEqual("keepalive ok" in printed, expect_ok_line)


class PromptRotationTest(unittest.TestCase):
    """探活提示词必须不断轮换，不能一直用"在吗"。"""

    def test_deck_covers_every_prompt_without_adjacent_repeats(self):
        deck = prompts.PromptDeck(rng=random.Random(1234))
        draws = [deck.next() for _ in range(32)]
        self.assertEqual(len(draws), 32)
        for previous, current in zip(draws, draws[1:]):
            self.assertNotEqual(previous, current, draws)
        self.assertEqual(set(draws), set(prompts.KEEPALIVE_PROMPTS))
        # 洗牌发牌：每 16 张恰好是一副完整的牌。
        self.assertEqual(set(draws[:16]), set(prompts.KEEPALIVE_PROMPTS))
        self.assertEqual(set(draws[16:]), set(prompts.KEEPALIVE_PROMPTS))

    def test_shuffled_order_differs_between_decks(self):
        first = [prompts.PromptDeck(rng=random.Random(1)).next() for _ in range(1)]
        orders = {tuple(prompts.PromptDeck(rng=random.Random(seed)).next() for _ in range(16)) for seed in range(6)}
        self.assertGreater(len(orders), 1, first)

    def test_shared_deck_is_thread_safe(self):
        deck = prompts.PromptDeck()
        collected: list[str] = []
        lock = threading.Lock()

        def draw():
            local = [deck.next() for _ in range(16)]
            with lock:
                collected.extend(local)

        workers = [threading.Thread(target=draw) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(collected), 128)
        # 8 副牌发完，每句恰好被取 8 次，一次不多一次不少。
        for prompt in prompts.KEEPALIVE_PROMPTS:
            self.assertEqual(collected.count(prompt), 8, prompt)

    def test_module_level_next_prompt_rotates(self):
        draws = [prompts.next_prompt() for _ in range(4)]
        self.assertEqual(len(set(draws)), 4, draws)
        self.assertNotEqual(draws, ["在吗？"] * 4)

    def test_empty_pool_is_rejected(self):
        with self.assertRaises(ValueError):
            prompts.PromptDeck(prompts=[" ", ""])


class KeepAliveEndpointTest(unittest.TestCase):
    """/_keepalive 状态端点 + dashboard 转发 + 真线程启停。"""

    def setUp(self):
        self.upstream = FakeUpstream("all_ok")
        base_url = self.upstream.start()
        self.cfg = build_config(base_url, keepalive_interval=5, keepalive_concurrency=2, keepalive_timeout=5)
        self.server = proxy.HeaderProxyServer(("127.0.0.1", 0), proxy.ProxyHandler, self.cfg)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.manager = None

    def tearDown(self):
        self.server.shutdown()
        with contextlib.redirect_stdout(io.StringIO()):
            self.server.server_close()  # 顺带覆盖 server_close() 里的 keepalive.stop()
        self.upstream.stop()

    def get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def start_manager(self) -> proxy.KeepAliveManager:
        manager = proxy.KeepAliveManager(self.cfg, self.server.session_pool)
        self.server.keepalive = manager
        self.manager = manager
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(manager.start(), 1)
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if manager.snapshot()["providers"]["any"]["state"] == proxy.KEEPALIVE_STATE_WARM:
                    return manager
                time.sleep(0.05)
        self.fail(f"provider never warmed up: {manager.snapshot()}")

    def test_status_is_empty_when_nothing_opted_in(self):
        status, body = self.get("/_keepalive")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["providers"], {})
        self.assertFalse(payload["enabled"])

    def test_status_reports_a_warm_provider_without_leaking_the_key(self):
        self.start_manager()
        status, body = self.get("/_keepalive")
        self.assertEqual(status, 200)
        self.assertNotIn("sk-unit-test-key-0001", body)
        payload = json.loads(body)
        self.assertTrue(payload["enabled"])
        entry = payload["providers"]["any"]
        self.assertEqual(entry["state"], proxy.KEEPALIVE_STATE_WARM)
        self.assertEqual(entry["endpoint"], "/responses")
        self.assertEqual(entry["model"], "gpt-5.6-sol")
        self.assertEqual(entry["interval"], 5.0)
        self.assertEqual(entry["concurrency"], 2)
        self.assertGreaterEqual(entry["okCount"], 1)
        self.assertIsNotNone(entry["latencyMs"])
        self.assertEqual(entry["lastError"], "")
        # 抢通的胜者留在池子里，真实流量 borrow() 到的就是这条。
        self.assertEqual(self.server.session_pool._pools["any"].qsize(), 1)

    def test_status_endpoint_rejects_writes(self):
        request = urllib.request.Request(f"{self.base}/_keepalive", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 405)

    def test_underscore_prefix_cannot_collide_with_a_provider_name(self):
        with self.assertRaises(ValueError):
            proxy.load_config_from_data([{
                "name": "_keepalive", "base_url": "https://x.invalid/v1", "api_mode": "responses", "models": ["m"],
            }])

    def test_threads_exit_after_stop(self):
        manager = self.start_manager()
        self.assertTrue(any(thread.is_alive() for thread in manager._threads))
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            manager.stop(timeout=3.0)
        self.assertLess(time.monotonic() - started, 3.0)  # 不等满一个 interval
        self.assertEqual(manager.snapshot()["providers"]["any"]["state"], proxy.KEEPALIVE_STATE_STOPPED)
        self.assertFalse([thread for thread in threading.enumerate() if thread.name.startswith("keepalive-any")])

    def test_dashboard_forwards_the_proxy_status(self):
        self.start_manager()
        payload = dashboard.keepalive_status_from_proxy(f"{self.base}/")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["proxyBase"], self.base)
        self.assertEqual(payload["providers"]["any"]["state"], proxy.KEEPALIVE_STATE_WARM)

    def test_dashboard_reports_an_unreachable_proxy(self):
        payload = dashboard.keepalive_status_from_proxy("http://127.0.0.1:9/")
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])
        self.assertEqual(payload["providers"], {})

    def test_dashboard_needs_a_proxy_address(self):
        original = dashboard.current_keepalive_proxy_base
        dashboard.current_keepalive_proxy_base = lambda: ""
        try:
            payload = dashboard.keepalive_status_from_proxy("")
        finally:
            dashboard.current_keepalive_proxy_base = original
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "aiproxy address unknown")
        self.assertEqual(payload["providers"], {})


CHECK_CONFIG = """\
- name: warm
  base_url: http://127.0.0.1:9/v1
  api_mode: codex_responses
  models: [gpt-5.6-sol]
  api_key: sk-check-mode-not-a-real-key
  keepalive: true
  keepalive_interval: 5
- name: plain
  base_url: http://127.0.0.1:9/v1
  api_mode: chat_completions
  models: [m]
  api_key: sk-check-mode-not-a-real-key
"""


class KeepAliveCliTest(unittest.TestCase):
    """--check 只校验配置，绝不能起保温线程去打上游。"""

    def test_check_lists_keepalive_providers_and_starts_no_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(CHECK_CONFIG, encoding="utf-8")
            started = time.monotonic()
            done = subprocess.run(
                [sys.executable, str(ROOT / "proxy.py"), "--config", str(cfg_path), "--check"],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(time.monotonic() - started, 20.0)  # 起了线程就会卡在 127.0.0.1:9 上退避重试
        self.assertIn("keepalive: warm", done.stdout)
        self.assertIn("providers: plain, warm", done.stdout)
        for noise in ("acquired upstream connection", "acquire failed", "started for", "keepalive ok"):
            self.assertNotIn(noise, done.stdout, noise)
        self.assertNotIn("sk-check-mode-not-a-real-key", done.stdout)

    def test_check_prints_none_when_nothing_opted_in(self):
        plain_only = CHECK_CONFIG.split("- name: plain", 1)[1]
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text("- name: plain" + plain_only, encoding="utf-8")
            done = subprocess.run(
                [sys.executable, str(ROOT / "proxy.py"), "--config", str(cfg_path), "--check"],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("keepalive: (none)", done.stdout)


if __name__ == "__main__":
    unittest.main()
