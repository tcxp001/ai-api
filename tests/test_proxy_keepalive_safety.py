"""Keepalive-only stopping policy and probe deadline regressions."""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dashboard
import proxy


def build_config(base_url, **overrides):
    entry = {
        "name": "any",
        "base_url": base_url,
        "api_mode": "codex_responses",
        "models": {"gpt-5.6-sol": {}},
        "api_key": "sk-unit-test-key-0001",
        "keepalive": True,
        "keepalive_backend": "http",
        "keepalive_interval": 5,
        "keepalive_timeout": 5,
        "keepalive_concurrency": 3,
    }
    entry.update(overrides)
    cfg = proxy.load_config_from_data([entry])
    cfg["verbose"] = False
    return cfg


class FakeResponse:
    def __init__(self, status=200, body="", content_type="application/json", lines=None):
        self.status_code = status
        self.text = body
        self.headers = {"content-type": content_type}
        self._lines = lines or []

    def json(self):
        return json.loads(self.text)

    def iter_lines(self, decode_unicode=False, chunk_size=512):
        for line in self._lines:
            yield line if isinstance(line, bytes) else str(line).encode("utf-8")

    def close(self):
        pass


def sse(*events):
    return [b"data: " + json.dumps(event).encode("utf-8") for event in events]


SSE_COMPLETED = (
    b'data: {"type": "response.created"}\n\n'
    b'data: {"type": "response.output_text.delta", "delta": "\xe5\x9c\xa8"}\n\n'
    b'data: {"type": "response.completed"}\n\n'
)


class RecordingPool(proxy.ProviderSessionPool):
    def __init__(self):
        super().__init__()
        self.discarded = []

    def discard(self, session):
        self.discarded.append(session)
        super().discard(session)

    def pooled(self, name):
        pool = self._pools.get(name)
        return list(pool.queue) if pool is not None else []


def seeded_manager(cfg, pool):
    manager = proxy.KeepAliveManager(cfg, pool)
    manager._states["any"] = {
        "state": proxy.KEEPALIVE_STATE_WARM,
        "since": time.time(),
        "attempts": 0,
        "okCount": 0,
        "failCount": 0,
        "lastSuccessFailCount": 0,
        "failureStreak": 0,
        "recycled": 0,
        "lastOkAt": None,
        "lastErrorAt": None,
        "lastError": "",
        "latencyMs": None,
        "nextProbeAt": None,
    }
    return manager


def state_of(manager):
    return manager.snapshot()["providers"]["any"]


class ScriptedManager(proxy.KeepAliveManager):
    """Drive the state machine without live upstreams or real retry sleeps."""

    def __init__(self, cfg, acquire_results=(), keepalive_results=(), stop_after_sleeps=None):
        super().__init__(cfg, proxy.ProviderSessionPool())
        self.acquire_results = list(acquire_results)
        self.keepalive_results = list(keepalive_results)
        self.acquire_calls = []
        self.sleeps = []
        self.stop_after_sleeps = stop_after_sleeps
        self._states["any"] = {"state": proxy.KEEPALIVE_STATE_COLD, "since": time.time(), "recycled": 0}

    def _acquire(self, name, provider, concurrency):
        self.acquire_calls.append(concurrency)
        ok = self.acquire_results.pop(0) if self.acquire_results else True
        return ok, "" if ok else "scripted acquire failure"

    def _keepalive_once(self, name, provider):
        result = self.keepalive_results.pop(0) if self.keepalive_results else True
        if isinstance(result, tuple):
            ok, kind = result
        else:
            ok, kind = bool(result), "ok" if result else "protocol"
        return ok, kind, "" if ok else "scripted keepalive failure"

    def _sleep(self, seconds):
        self.sleeps.append(round(float(seconds), 3))
        if self.stop_after_sleeps is not None and len(self.sleeps) >= self.stop_after_sleeps:
            self._stop.set()
            return False
        return True

    def _log(self, fmt, *args, always=False):
        pass


class ProbeDeadlineUpstream:
    """Finite trickle responses: regressions fail promptly even without a deadline."""

    def __init__(self, mode):
        self.mode = mode
        self.disconnected = threading.Event()
        self.finished = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", 0)))
                if outer.mode == "ok":
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("content-length", str(len(SSE_COMPLETED)))
                    self.end_headers()
                    self.wfile.write(SSE_COMPLETED)
                    self.wfile.flush()
                    return
                try:
                    if outer.mode == "headers":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Trickle: ")
                        self.wfile.flush()
                    else:
                        self.send_response(429 if outer.mode == "error" else 200)
                        self.send_header(
                            "content-type",
                            "text/event-stream" if outer.mode in {"sse", "completed"} else "application/json",
                        )
                        self.send_header("connection", "close")
                        self.end_headers()
                        if outer.mode == "completed":
                            self.wfile.write(SSE_COMPLETED)
                            self.wfile.flush()
                    until = time.monotonic() + 0.7
                    while time.monotonic() < until:
                        chunk = b": heartbeat\n\n" if outer.mode in {"sse", "completed"} else b" "
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        time.sleep(0.02)
                    if outer.mode == "headers":
                        self.wfile.write(b"\r\nContent-Length: 0\r\n\r\n")
                    elif outer.mode in {"sse", "completed"}:
                        self.wfile.write(SSE_COMPLETED)
                    else:
                        self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')
                    self.wfile.flush()
                except (OSError, ValueError):
                    outer.disconnected.set()
                finally:
                    self.close_connection = True
                    outer.finished.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class KeepAlivePermanentFailureTest(unittest.TestCase):
    def test_test_manager_does_not_publish_to_runtime_event_queue(self):
        cfg = build_config("http://127.0.0.1:1")
        manager = proxy.KeepAliveManager(cfg, proxy.ProviderSessionPool())
        with mock.patch.object(proxy, "publish_keepalive_event") as publish:
            manager._emit_event("a", "start", "开始抢通")
        publish.assert_not_called()

    def test_error_classification_does_not_stop_transient_failures(self):
        cases = [
            (401, {}, "auth"),
            (403, {"error": {"code": "invalid_api_key"}}, "auth"),
            (429, {"error": {"code": "insufficient_quota"}}, "permanent"),
            (402, {"error": {"message": "用户额度不足"}}, "permanent"),
            (404, {"error": {"code": "model_not_found"}}, "permanent"),
            (400, {"error": {"code": "unsupported_parameter"}}, "permanent"),
            (400, {"error": {"type": "invalid_request_error", "message": "invalid temperature"}}, "permanent"),
            (400, {"error": {"type": "invalid_request_error", "message": "no available channel"}}, "busy"),
            (400, {"error": {"type": "invalid_request_error", "message": "server busy; try again later"}}, "busy"),
            (429, {"error": {"code": "rate_limit_error"}}, "busy"),
            (503, {"error": {"code": "get_channel_failed"}}, "busy"),
            (403, {"error": {"message": "gateway access denied"}}, "protocol"),
            (404, {"error": {"message": "route unavailable"}}, "protocol"),
        ]
        for status, payload, expected in cases:
            with self.subTest(status=status, payload=payload):
                result = proxy.validate_keepalive_response(
                    FakeResponse(status, json.dumps(payload)), "/responses"
                )
                self.assertFalse(result[0])
                self.assertEqual(result.kind, expected)
        result = proxy.validate_keepalive_response(
            FakeResponse(403, "<html>invalid_api_key</html>", "text/html"), "/responses"
        )
        self.assertEqual(result.kind, "protocol")

    def test_permanent_errors_in_http_200_json_and_sse(self):
        cases = [
            FakeResponse(200, '{"error":{"code":"insufficient_quota"}}'),
            FakeResponse(200, content_type="text/event-stream", lines=sse(
                {"type": "response.created"},
                {"type": "response.failed", "response": {"error": {"code": "insufficient_quota"}}},
            )),
            FakeResponse(200, content_type="text/event-stream", lines=sse(
                {"type": "error", "error": {"code": "unsupported_parameter"}},
            )),
        ]
        for response in cases:
            with self.subTest(response=response):
                result = proxy.validate_keepalive_response(response, "/responses")
                self.assertFalse(result[0])
                self.assertEqual(result.kind, "permanent")

    def test_generated_text_is_not_classified_as_an_error(self):
        text = "invalid_api_key insufficient_quota 余额不足 model_not_found"
        response = FakeResponse(200, json.dumps({
            "choices": [{"message": {"content": text}}],
        }))
        self.assertTrue(proxy.validate_keepalive_response(response, "/chat/completions")[0])
        response = FakeResponse(200, content_type="text/event-stream", lines=sse(
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.completed"},
        ))
        self.assertTrue(proxy.validate_keepalive_response(response, "/responses")[0])

    def test_cold_acquire_stops_on_invalid_key(self):
        cfg = build_config("http://upstream.invalid/v1", keepalive_concurrency=1)
        pool = RecordingPool()
        self.addCleanup(pool.close_all)
        manager = ScriptedManager(cfg, stop_after_sleeps=2)
        self.addCleanup(manager._pool.close_all)
        manager._pool = pool
        manager._acquire = lambda name, provider, concurrency: proxy.KeepAliveManager._acquire(
            manager, name, provider, concurrency
        )
        response = FakeResponse(401, json.dumps({"error": {"code": "invalid_api_key"}}))
        with mock.patch.object(proxy.requests.Session, "post", return_value=response) as post:
            manager._run_provider("any", cfg["providers"]["any"])
        self.assertEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_FAILED)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(manager.sleeps, [])
        self.assertEqual(state_of(manager)["lastErrorKind"], "auth")

    def test_permanent_failure_stops_reacquire_after_a_warm_loss(self):
        cfg = build_config("http://upstream.invalid/v1", keepalive_concurrency=1)
        manager = ScriptedManager(cfg, keepalive_results=[False], stop_after_sleeps=3)
        self.addCleanup(manager._pool.close_all)
        calls = []

        def acquire(name, provider, concurrency):
            calls.append(concurrency)
            if len(calls) == 1:
                return True, ""
            return proxy.KeepAliveManager._acquire(manager, name, provider, concurrency)

        manager._acquire = acquire
        response = FakeResponse(429, '{"error":{"code":"insufficient_quota"}}')
        with mock.patch.object(proxy.requests.Session, "post", return_value=response) as post:
            manager._run_provider("any", cfg["providers"]["any"])
        self.assertEqual(calls, [1, 1])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_FAILED)
        self.assertEqual(state_of(manager)["lastErrorKind"], "permanent")
        self.assertIsNone(state_of(manager)["nextProbeAt"])
        self.assertEqual(manager.sleeps, [5])

    def test_warm_permanent_failure_does_not_retry_the_request(self):
        cfg = build_config("http://upstream.invalid/v1")
        pool = RecordingPool()
        self.addCleanup(pool.close_all)
        manager = seeded_manager(cfg, pool)
        pool.release("any", cfg["providers"]["any"], pool.fresh(cfg["providers"]["any"]))
        response = FakeResponse(200, '{"error":{"code":"invalid_api_key"}}')
        with mock.patch.object(proxy.requests.Session, "post", return_value=response) as post:
            ok, kind, detail = manager._keepalive_once("any", cfg["providers"]["any"])
        self.assertFalse(ok)
        self.assertEqual(kind, "auth")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(pool.pooled("any"), [])

    def test_warm_state_stops_on_permanent_failure(self):
        cfg = build_config("http://upstream.invalid/v1")
        manager = ScriptedManager(
            cfg, acquire_results=[True], keepalive_results=[(False, "permanent")],
            stop_after_sleeps=3,
        )
        self.addCleanup(manager._pool.close_all)
        manager._run_provider("any", cfg["providers"]["any"])
        self.assertEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_FAILED)
        self.assertEqual(manager.acquire_calls, [3])
        self.assertEqual(manager.sleeps, [5])

    def test_plain_403_and_busy_errors_keep_retrying(self):
        for status in (403, 404, 429, 503):
            with self.subTest(status=status):
                cfg = build_config("http://upstream.invalid/v1", keepalive_concurrency=1)
                manager = ScriptedManager(cfg, stop_after_sleeps=2)
                self.addCleanup(manager._pool.close_all)
                manager._acquire = lambda name, provider, concurrency: proxy.KeepAliveManager._acquire(
                    manager, name, provider, concurrency
                )
                response = FakeResponse(status, '{"error":{"message":"temporarily unavailable"}}')
                with mock.patch.object(proxy.requests.Session, "post", return_value=response) as post:
                    manager._run_provider("any", cfg["providers"]["any"])
                self.assertEqual(post.call_count, 2)
                self.assertEqual(manager.sleeps, [proxy.DEFAULT_KEEPALIVE_RETRY_INTERVAL] * 2)
                self.assertNotEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_FAILED)

    def test_cancelled_race_candidate_never_sends_a_late_request(self):
        cfg = build_config("http://upstream.invalid/v1")
        pool = RecordingPool()
        self.addCleanup(pool.close_all)
        manager = seeded_manager(cfg, pool)
        session = pool.fresh(cfg["providers"]["any"])
        self.addCleanup(session.close)
        session._keepalive_cancelled = True
        with mock.patch.object(session, "post") as post:
            outcome = manager._probe(cfg["providers"]["any"], session)
        self.assertEqual(outcome.kind, "skipped")
        post.assert_not_called()


class KeepAliveProbeDeadlineTest(unittest.TestCase):
    def test_heartbeat_probe_times_out_and_releases_resources(self):
        self.assert_probe_deadline("sse")

    def test_headers_are_covered_by_the_total_deadline(self):
        self.assert_probe_deadline("headers")

    def test_nonstream_json_is_covered_by_the_total_deadline(self):
        self.assert_probe_deadline("json", "chat_completions")

    def test_error_body_is_covered_by_the_total_deadline(self):
        self.assert_probe_deadline("error")

    def test_completed_event_without_eof_still_has_a_deadline(self):
        self.assert_probe_deadline("completed")

    def assert_probe_deadline(self, mode, api_mode="codex_responses"):
        existing_readers = {t.ident for t in threading.enumerate() if t.name == "keepalive-sse-reader"}
        with ProbeDeadlineUpstream(mode) as upstream:
            cfg = build_config(upstream.base_url)
            provider = cfg["providers"]["any"]
            provider["api_mode"] = api_mode
            provider["keepalive_total_timeout"] = 0.15
            pool = RecordingPool()
            self.addCleanup(pool.close_all)
            manager = seeded_manager(cfg, pool)
            manager._inflight = threading.BoundedSemaphore(1)
            session = pool.fresh(provider)
            self.addCleanup(session.close)
            started = time.monotonic()
            outcome = manager._probe(provider, session)
            elapsed = time.monotonic() - started
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.kind, "total_timeout")
            self.assertLess(elapsed, 0.5)
            self.assertTrue(manager._inflight.acquire(blocking=False))
            manager._inflight.release()
            self.assertTrue(upstream.disconnected.wait(0.4))
            self.assertFalse(
                [t for t in threading.enumerate() if t.name == "keepalive-sse-reader" and t.ident not in existing_readers]
            )
            self.assertFalse(manager._probe_deadlines)
            self.assertFalse([t for t in threading.enumerate() if t.name == "keepalive-deadline"])

    def test_waiting_for_a_global_slot_is_bounded(self):
        cfg = build_config("http://upstream.invalid/v1")
        provider = cfg["providers"]["any"]
        provider["keepalive_total_timeout"] = 0.05
        pool = RecordingPool()
        self.addCleanup(pool.close_all)
        manager = seeded_manager(cfg, pool)
        manager._inflight = threading.BoundedSemaphore(1)
        manager._inflight.acquire()
        session = pool.fresh(provider)
        self.addCleanup(session.close)
        with mock.patch.object(session, "post") as post:
            outcome = manager._probe(provider, session)
        self.assertEqual(outcome.kind, "total_timeout")
        post.assert_not_called()
        self.assertFalse(manager._inflight.acquire(blocking=False))
        manager._inflight.release()

    def test_timeout_discards_session_and_the_next_attempt_can_succeed(self):
        with ProbeDeadlineUpstream("sse") as upstream:
            cfg = build_config(upstream.base_url)
            provider = cfg["providers"]["any"]
            provider["keepalive_total_timeout"] = 0.15
            pool = RecordingPool()
            self.addCleanup(pool.close_all)
            manager = seeded_manager(cfg, pool)
            ok, kind, _ = manager._keepalive_once("any", provider)
            self.assertFalse(ok)
            self.assertEqual(kind, "total_timeout")
            self.assertEqual(len(pool.discarded), 1)
            self.assertEqual(pool.pooled("any"), [])
            self.assertTrue(upstream.finished.wait(0.5))
            upstream.mode = "ok"
            self.assertTrue(manager._keepalive_once("any", provider)[0])
            self.assertEqual(len(pool.pooled("any")), 1)

    def test_total_timeout_does_not_stop_the_provider_state_machine(self):
        cfg = build_config("http://upstream.invalid/v1")
        manager = ScriptedManager(
            cfg, acquire_results=[True, True], keepalive_results=[(False, "total_timeout")],
            stop_after_sleeps=2,
        )
        self.addCleanup(manager._pool.close_all)
        manager._run_provider("any", cfg["providers"]["any"])
        self.assertEqual(manager.acquire_calls, [3, 1])
        self.assertNotEqual(state_of(manager)["state"], proxy.KEEPALIVE_STATE_FAILED)

    def test_success_restores_pool_hooks_and_business_requests_have_no_probe_deadline(self):
        with ProbeDeadlineUpstream("ok") as upstream:
            cfg = build_config(upstream.base_url)
            provider = cfg["providers"]["any"]
            provider["keepalive_total_timeout"] = 0.15
            pool = RecordingPool()
            self.addCleanup(pool.close_all)
            manager = seeded_manager(cfg, pool)
            session = pool.fresh(provider)
            self.addCleanup(session.close)
            self.assertTrue(manager._probe(provider, session).ok)
            adapter = session.get_adapter(upstream.base_url)
            http_pool = adapter.get_connection(upstream.base_url)
            connection = next(c for c in http_pool.pool.queue if c is not None)
            first_socket = connection.sock
            self.assertTrue(manager._probe(provider, session).ok)
            self.assertIs(connection.sock, first_socket)
            self.assertNotIn("connect", vars(connection))
            self.assertNotIn("_get_conn", vars(http_pool))
            self.assertNotIn("get_connection", vars(adapter))
            upstream.mode = "json"
            started = time.monotonic()
            response = session.post(upstream.base_url + "/chat/completions", json={}, timeout=2)
            self.assertEqual(response.status_code, 200)
            self.assertGreater(time.monotonic() - started, 0.5)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "ok")


class KeepAliveTotalTimeoutConfigTest(unittest.TestCase):
    def test_default_and_bounds(self):
        for supplied, expected in ((None, 180), (1, 5), (9999, 1800), (240, 240)):
            with self.subTest(supplied=supplied):
                provider = build_config(
                    "http://upstream.invalid/v1", keepalive_total_timeout=supplied,
                )["providers"]["any"]
                self.assertEqual(provider["keepalive_total_timeout"], expected)

    def test_dashboard_roundtrip_and_validation(self):
        entry = {
            "name": "any", "base_url": "http://upstream.invalid/v1",
            "models": {"m": {}}, "keepalive": True, "keepalive_total_timeout": 240,
        }
        compacted = dashboard.compact_provider(dashboard.validate_provider(entry, 1))
        self.assertEqual(compacted["keepalive_total_timeout"], 240)
        for value in (4, 1801, "bad", True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dashboard.validate_provider({**entry, "keepalive_total_timeout": value}, 1)
        compacted = dashboard.compact_provider(
            dashboard.validate_provider({**entry, "keepalive_total_timeout": 180}, 1)
        )
        self.assertNotIn("keepalive_total_timeout", compacted)


if __name__ == "__main__":
    unittest.main()
