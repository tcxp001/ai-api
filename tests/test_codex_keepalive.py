"""Codex backend regressions: fake PTYs/processes only, never a real upstream."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_keepalive as cli
import dashboard
import proxy


def config(**overrides):
    entry = {
        "name": "any-test",
        "base_url": "https://upstream.invalid/v1",
        "api_key": "sk-test-private-key",
        "api_mode": "codex_responses",
        "models": {"model-a": {"reasoning_effort": "low"}},
        "keepalive": True,
        "keepalive_backend": "codex_cli",
        "keepalive_timeout": 5,
        "keepalive_total_timeout": 5,
        "keepalive_concurrency": 3,
    }
    entry.update(overrides)
    return proxy.load_config_from_data([entry])


def provider(**overrides):
    return config(**overrides)["providers"]["any-test"]


class ConfigurationTest(unittest.TestCase):
    def test_selected_model_reasoning_wins_over_provider_and_probe_override(self):
        p = provider(
            models={"model-a": {"reasoning_effort": "low"}, "model-b": {"reasoning_effort": "xhigh"}},
            keepalive_model="model-b", reasoning_effort="high", keepalive_reasoning_effort="medium",
        )
        model = proxy.keepalive_model_for(p)
        effort = proxy.configured_reasoning_effort(p, {"model": model})
        parsed = tomllib.loads(cli.build_config(p, model, effort, Path("/tmp/synthetic-work")))
        self.assertEqual(parsed["model"], "model-b")
        self.assertEqual(parsed["model_reasoning_effort"], "xhigh")
        upstream = parsed["model_providers"]["keepalive_upstream"]
        self.assertEqual(upstream["base_url"], p["base_url"])
        self.assertEqual(upstream["env_key"], cli.API_KEY_ENV)
        self.assertEqual(upstream["wire_api"], "responses")
        self.assertNotIn(p["api_key"], json.dumps(parsed))
        self.assertNotIn("max_output_tokens", parsed)

    def test_provider_fallback_and_unset_effort_never_use_probe_override(self):
        for extra, expected in (
            ({"reasoning_effort": "high"}, "high"),
            ({"reasoning": {"effort": "low"}}, "low"),
            ({}, None),
        ):
            with self.subTest(extra=extra):
                p = provider(models={"model-a": {}}, keepalive_reasoning_effort="xhigh", **extra)
                effort = proxy.configured_reasoning_effort(p, {"model": "model-a"})
                parsed = tomllib.loads(cli.build_config(p, "model-a", effort, Path("/tmp/synthetic-work")))
                self.assertEqual(parsed.get("model_reasoning_effort"), expected)

    def test_environment_does_not_inherit_other_credentials_or_session(self):
        with mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "parent-secret",
            "CODEX_THREAD_ID": "parent-thread",
            "ORCA_AGENT_HOOK_TOKEN": "parent-hook-secret",
            "HTTP_PROXY": "http://proxy.invalid:3128",
            "CODEX_HOME": "/private/main-codex",
        }):
            p = provider()
            env = cli.build_environment(p, Path("/tmp/isolated-home"))
            self.assertEqual(env[cli.API_KEY_ENV], p["api_key"])
            self.assertEqual(env["CODEX_HOME"], "/tmp/isolated-home")
            self.assertEqual(env["HOME"], "/tmp/isolated-home")
            for key in ("OPENAI_API_KEY", "CODEX_THREAD_ID", "ORCA_AGENT_HOOK_TOKEN", "HTTP_PROXY"):
                self.assertNotIn(key, env)
            p["trust_env_proxy"] = True
            self.assertEqual(cli.build_environment(p, Path("/tmp/h"))["HTTP_PROXY"], "http://proxy.invalid:3128")

    def test_dashboard_roundtrip_keeps_backend_and_path_without_changing_defaults(self):
        raw = {
            "name": "any-test", "base_url": "https://upstream.invalid/v1",
            "api_mode": "codex_responses", "models": {"model-a": {"reasoning_effort": "high"}},
            "keepalive": True, "keepalive_backend": "codex_cli",
            "keepalive_codex_path": "/opt/codex/bin/codex",
        }
        with mock.patch.object(dashboard, "codex_stream_retry_default", return_value=5):
            saved = dashboard.compact_provider(dashboard.validate_provider(raw, 1))
            self.assertEqual(saved["keepalive_backend"], "codex_cli")
            self.assertEqual(saved["keepalive_codex_path"], "/opt/codex/bin/codex")
            self.assertEqual(saved["models"]["model-a"]["reasoning_effort"], "high")
            raw.pop("keepalive_backend")
            raw.pop("keepalive_codex_path")
            regular = dashboard.compact_provider(dashboard.validate_provider(raw, 1))
            self.assertNotIn("keepalive_backend", regular)
            self.assertNotIn("keepalive_codex_path", regular)

    def test_other_providers_keep_the_http_request_contract(self):
        p = provider(keepalive_backend="http", models={"model-a": {"reasoning_effort": "high"}})
        self.assertFalse(proxy.KeepAliveManager._uses_codex(p))
        payload = proxy.build_keepalive_payload("model-a", "Hi", "/responses", p)
        self.assertEqual(payload["reasoning"]["effort"], "medium")
        self.assertEqual(payload["max_output_tokens"], 32)

    def test_invalid_backend_is_rejected_and_unsupported_cli_config_is_explicit(self):
        with self.assertRaises(ValueError):
            provider(keepalive_backend="typo")
        for overrides in (
            {"api_mode": "messages"}, {"api_key": ""},
            {"remove_headers": ["user-agent"]},
        ):
            with self.subTest(overrides=overrides):
                p = provider(**overrides)
                with self.assertRaises(cli.CodexConfigurationError):
                    cli.build_config(p, "model-a", "low", Path("/tmp/work"))


class ReplyDetectionTest(unittest.TestCase):
    def test_role_aware_rules_and_working_fragments(self):
        for text, prompt, short, expected in (
            ("› Hi\n• Hi. What would you like me to work on?", "Hi", False, True),
            ("› Hi\nWorking (5s - esc to interrupt)", "Hi", False, False),
            ("› Hi\nRetrying in 11s · attempt 8/10", "Hi", False, False),
            ("› Hi\n› Use /skills to list available skills", "Hi", False, False),
            ("› Hi\n• OK", "Hi", False, False),
            ("› 在吗？短回\n• 收到", "在吗？短回", True, True),
            ("› 在吗？短回\n• orking(2s•esctointerrupt)", "在吗？短回", True, False),
            ("› Hi\n• Old response is long enough.\n› 还在线吗？短答\n› Write tests for @filename",
             "还在线吗？短答", True, False),
            ("\x1b[2J\x1b[H›\n在吗？短回\n\x1b[4;1H•\n收到", "在吗？短回", True, True),
        ):
            with self.subTest(text=text):
                self.assertEqual(cli.assistant_reply(cli.lines_after_prompt(text, prompt), prompt, short), expected)

    def test_previous_retry_is_not_part_of_the_current_turn(self):
        text = "› Hi\nRetrying in 11s · attempt 8/10\n› 还在线吗？短答\n• 收到"
        self.assertEqual(cli.retry_signal(cli.lines_after_prompt(text, "还在线吗？短答")), "")
        self.assertTrue(cli.retry_signal(cli.lines_after_prompt("› Hi\nReconnecting... 3s", "Hi")))

    def test_loading_model_and_input_echo(self):
        self.assertFalse(cli.terminal_ready("model: loading\n› Ask anything"))
        self.assertTrue(cli.terminal_ready("model: test\n› Ask anything"))
        self.assertTrue(cli.terminal_ready("model: test\n❯ Ask Codex to do anything"))
        self.assertTrue(cli.prompt_visible("›\nHi", "Hi"))
        self.assertTrue(cli.prompt_visible("❯ Hi", "Hi"))
        self.assertTrue(cli.prompt_visible(
            "\x1b[20;3H在\x1b[20;5H吗\x1b[20;7H？\x1b[20;9H短\x1b[20;11H回",
            "在吗？短回",
        ))
        self.assertFalse(cli.prompt_visible("• Hi", "Hi"))


class KeepaliveMetricsTest(unittest.TestCase):
    def test_first_text_is_measured_before_stream_completion(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["content-length"]))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.created"}\n\n')
                self.wfile.flush()
                time.sleep(0.05)
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n')
                self.wfile.flush()
                time.sleep(0.15)
                self.wfile.write(b'data: {"type":"response.completed"}\n\n')
                self.wfile.flush()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cfg = config(base_url=f"http://127.0.0.1:{server.server_port}/v1", keepalive_backend="http")
        pool = proxy.ProviderSessionPool()
        self.addCleanup(pool.close_all)
        manager = proxy.KeepAliveManager(cfg, pool)
        p = cfg["providers"]["any-test"]
        session = pool.fresh(p)
        self.addCleanup(session.close)
        outcome = manager._probe(p, session)
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertIsNotNone(outcome.first_token_ms)
        self.assertLess(outcome.first_token_ms, outcome.elapsed * 1000 - 80)

    def test_created_reasoning_and_empty_completed_are_not_first_text(self):
        import queue
        lines = queue.Queue()
        for event in (
            {"type": "response.created"},
            {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
            {"type": "response.function_call_arguments.delta", "delta": "{}"},
            {"type": "response.completed", "response": {"output": None}},
        ):
            lines.put(("line", "data: " + json.dumps(event)))
        lines.put(("eof", None))
        callback = mock.Mock()
        self.assertTrue(proxy._consume_keepalive_stream(lines, (), 1, 1, None, callback)[0])
        callback.assert_not_called()

    def test_started_at_does_not_change_after_success_or_loss(self):
        cfg = config()
        pool = proxy.ProviderSessionPool()
        self.addCleanup(pool.close_all)
        manager = proxy.KeepAliveManager(cfg, pool)
        self.addCleanup(manager.stop)
        with mock.patch.object(manager, "_run_codex_provider"), contextlib.redirect_stdout(io.StringIO()):
            manager.start()
        initial = manager.snapshot()["providers"]["any-test"]["startedAt"]
        manager._note_ok("any-test", "warm", "test success")
        manager._note_fail("any-test", "test failure")
        manager._update("any-test", state="lost")
        self.assertEqual(manager.snapshot()["providers"]["any-test"]["startedAt"], initial)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the dashboard rendering function")
    def test_dashboard_renders_seconds_and_requested_time_labels(self):
        html = (ROOT / "dashboard.html").read_text()
        start = html.index("    function renderKeepalivePage()")
        end = html.index("    function autoCompactPercentValue", start)
        script = """
const node = {innerHTML: ''};
const $ = () => node;
const esc = value => String(value);
const modelNames = p => Object.keys(p.models || {});
const keepaliveBadge = () => '';
const keepaliveTime = t => 'time-' + t;
const state = {
  providers: [{name: 'any-test', keepalive: true}],
  keepalive: {providers: {'any-test': {
    model: 'model-a', firstTokenMs: 1250, latencyMs: 9999,
    startedAt: 100, lastOkAt: 200, okCount: 1,
    backend: 'codex_cli', reasoningEffort: 'high', codexAlive: true, codexPid: 12345
  }}}
};
""" + html[start:end] + "\nrenderKeepalivePage(); console.log(node.innerHTML);"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        rendered = result.stdout
        for value in ("首字（s）", "1.25", "启动时间", "time-100", "成功时间", "time-200"):
            self.assertIn(value, rendered)
        for value in (
            "最近结果", "最后成功时间", "9999", "<span>延迟</span>",
            "保活方式", "推理深度", "获胜进程", "PID 12345",
        ):
            self.assertNotIn(value, rendered)


FAKE_CODEX = r'''
import json, os, signal, sys, time, tomllib, tty
from pathlib import Path
ROOT = Path(__ROOT__)
MODE = __MODE__
tty.setraw(0)
home = Path(os.environ["CODEX_HOME"])
config = tomllib.loads((home / "config.toml").read_text())
trace = ROOT / (str(os.getpid()) + ".jsonl")
def record(**data):
    with trace.open("a") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
record(pid=os.getpid(), home=str(home), model=config["model"],
       effort=config.get("model_reasoning_effort"))
if MODE == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if MODE == "error":
    os.write(1, ("Error: invalid_api_key " + os.environ["AIPROXY_KEEPALIVE_API_KEY"] + "\n").encode())
elif MODE == "config_error":
    os.write(1, b"Error loading config.toml: unknown variant\n")
os.write(1, b"model: test\n> Ask anything\n")
prompt = b""
count = 0
while True:
    b = os.read(0, 1)
    if not b:
        break
    if b == b"\x1b":
        escape = b
        while not escape.endswith(b"~"):
            escape += os.read(0, 1)
        continue
    if b == b"\r":
        count += 1
        record(prompt=prompt.decode("utf-8"), count=count)
        prompt = b""
        if MODE in {"hang", "stubborn"}:
            os.write(1, b"\nWorking (5s - esc to interrupt)\n")
        elif MODE == "fail" or (MODE == "fail_second" and count == 2):
            os.write(1, b"\nRetrying in 11s - attempt 8/10\n")
        elif MODE == "exit":
            os._exit(0)
        elif MODE == "short":
            os.write(1, "\n• OK\n".encode())
        else:
            os.write(1, "\n• Still alive. This reply is long enough.\n> Ask anything\n".encode())
    else:
        if not prompt:
            os.write(1, b"\n> ")
        prompt += b
        os.write(1, b)
'''


@unittest.skipUnless(os.name == "posix", "PTY backend requires POSIX")
class PtyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-keepalive-test-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.patch = mock.patch.object(cli, "INPUT_DELAY", 0.01)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.settle = mock.patch.object(cli, "SETTLE_DELAY", 0.03)
        self.settle.start()
        self.addCleanup(self.settle.stop)

    def executable(self, mode="success"):
        path = self.root / ("fake-codex-" + mode)
        source = FAKE_CODEX.replace("__ROOT__", repr(str(self.root))).replace("__MODE__", repr(mode))
        path.write_text("#!" + sys.executable + "\n" + source)
        path.chmod(0o700)
        return str(path)

    def session(self, mode="success"):
        p = provider(keepalive_codex_path=self.executable(mode))
        session = cli.CodexSession(p, "model-a", "low")
        self.addCleanup(session.stop)
        return session

    def traces(self):
        return {
            int(path.stem): [json.loads(line) for line in path.read_text().splitlines()]
            for path in self.root.glob("*.jsonl")
        }

    def assert_exited(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def manager(self, mode="success", **overrides):
        cfg = config(keepalive_codex_path=self.executable(mode), **overrides)
        pool = proxy.ProviderSessionPool()
        self.addCleanup(pool.close_all)
        manager = proxy.KeepAliveManager(cfg, pool)
        self.addCleanup(manager.stop)
        manager._states["any-test"] = {
            "state": "cold", "backend": "codex_cli", "codexSession": "", "okCount": 0,
        }
        return manager, cfg["providers"]["any-test"]


class InteractiveSessionTest(PtyTestCase):
    def test_same_process_and_conversation_are_reused_with_private_config(self):
        session = self.session()
        self.assertTrue(session.prompt("Hi", 2)[0])
        self.assertIsNotNone(session.first_token_ms)
        pid = session.pid
        self.assertTrue(session.prompt("在吗？短回", 2, short=True)[0])
        self.assertIsNotNone(session.first_token_ms)
        self.assertEqual(session.pid, pid)
        trace = self.traces()[pid]
        self.assertEqual([row["prompt"] for row in trace[1:]], ["Hi", "在吗？短回"])
        self.assertEqual(trace[0]["effort"], "low")
        home = Path(trace[0]["home"])
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual((home / "config.toml").stat().st_mode & 0o777, 0o600)
        self.assertFalse((home / "auth.json").exists())
        self.assertNotIn(session.provider["api_key"], (home / "config.toml").read_text())
        session.stop()
        self.assert_exited(pid)
        self.assertFalse(home.parent.exists())

    def test_timeout_and_stubborn_process_are_cleaned_up(self):
        session = self.session("stubborn")
        result = session.prompt("Hi", 0.3)
        self.assertFalse(result[0])
        self.assertEqual(result[1], "timeout")
        pid = session.pid
        home = Path(self.traces()[pid][0]["home"])
        session.stop()
        self.assert_exited(pid)
        self.assertFalse(home.parent.exists())

    def test_cancel_before_start_never_launches_a_process(self):
        session = self.session()
        session.cancel()
        self.assertEqual(session.prompt("Hi", 1)[1], "skipped")
        self.assertIsNone(session.pid)
        self.assertEqual(self.traces(), {})

    def test_retry_and_process_exit_do_not_count_as_success(self):
        for mode, kind in (("fail", "busy"), ("exit", "stale")):
            with self.subTest(mode=mode):
                session = self.session(mode)
                result = session.prompt("Hi", 2)
                self.assertFalse(result[0])
                self.assertEqual(result[1], kind)

    def test_short_reply_only_counts_during_keepalive(self):
        session = self.session("short")
        self.assertFalse(session.prompt("Hi", 0.3)[0])
        session.stop()
        warm = self.session("short")
        self.assertTrue(warm.prompt("在吗？短回", 2, short=True)[0])

    def test_explicit_error_does_not_leak_key(self):
        session = self.session("error")
        ok, kind, detail = session.prompt("Hi", 2)
        self.assertFalse(ok)
        self.assertEqual(kind, "error")
        self.assertIn("invalid_api_key", detail)
        self.assertNotIn(session.provider["api_key"], detail)

    def test_configuration_error_is_non_retryable(self):
        session = self.session("config_error")
        ok, kind, _ = session.prompt("Hi", 2)
        self.assertFalse(ok)
        self.assertEqual(kind, "permanent")


class ManagerTest(PtyTestCase):
    def test_race_retains_only_winner_and_warm_probe_uses_it(self):
        manager, p = self.manager()
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
            proxy.requests.Session, "post", side_effect=AssertionError("HTTP fallback forbidden"),
        ):
            self.assertTrue(manager._acquire("any-test", p, 3)[0])
            winner = manager._codex_winners["any-test"]
            pid = winner.pid
            self.assertTrue(manager._keepalive_once("any-test", p)[0])
        self.assertEqual(winner.pid, pid)
        self.assertEqual(manager._codex_active["any-test"], {winner})
        self.assertEqual(manager._pool._pools, {})
        snapshot = manager.snapshot()["providers"]["any-test"]
        self.assertTrue(snapshot["codexAlive"])
        self.assertEqual(snapshot["codexPid"], pid)
        for other_pid, trace in self.traces().items():
            if other_pid != pid:
                self.assert_exited(other_pid)
                self.assertFalse(Path(trace[0]["home"]).parent.exists())
        manager.stop()
        self.assert_exited(pid)
        self.assertFalse(manager.snapshot()["providers"]["any-test"]["codexAlive"])

    def test_warm_failure_closes_old_session_and_reacquires_serially(self):
        manager, p = self.manager("fail_second")
        concurrencies = []
        original_acquire = manager._acquire
        sleeps = []
        def acquire(name, provider, concurrency):
            concurrencies.append(concurrency)
            return original_acquire(name, provider, concurrency)
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                manager._stop.set()
                return False
            return True
        manager._acquire = acquire
        manager._sleep = sleep
        with contextlib.redirect_stdout(io.StringIO()):
            manager._run_codex_provider("any-test", p)
        self.assertEqual(concurrencies, [3, 1])
        self.assertEqual(manager.snapshot()["providers"]["any-test"]["okCount"], 2)
        self.assertEqual(manager._codex_winners, {})
        for pid, trace in self.traces().items():
            self.assert_exited(pid)
            self.assertFalse(Path(trace[0]["home"]).parent.exists())

    def test_repeated_transport_failures_never_widen_cli_reacquisition(self):
        manager, p = self.manager()
        calls = []
        def acquire(name, provider, concurrency):
            calls.append(concurrency)
            return len(calls) == 1, "ReadTimeout: synthetic failure"
        manager._acquire = acquire
        manager._keepalive_once = lambda *args: (False, "timeout", "ReadTimeout: synthetic failure")
        def sleep(seconds):
            if len(calls) >= 8:
                manager._stop.set()
                return False
            return True
        manager._sleep = sleep
        with contextlib.redirect_stdout(io.StringIO()):
            manager._run_codex_provider("any-test", p)
        self.assertEqual(calls, [3] + [1] * 7)

    def test_external_business_success_cannot_invent_a_retained_process(self):
        manager, _ = self.manager()
        manager._external_warm["any-test"] = threading.Event()
        manager.observe_client_success("any-test")
        self.assertEqual(manager.snapshot()["providers"]["any-test"]["state"], "cold")
        self.assertFalse(manager._external_warm["any-test"].is_set())

    def test_start_reports_inherited_effort_and_no_http_token_cap(self):
        manager, _ = self.manager(
            models={"model-a": {"reasoning_effort": "high"}},
            keepalive_reasoning_effort="low",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            manager.start()
            until = time.monotonic() + 5
            while time.monotonic() < until:
                status = manager.snapshot()["providers"]["any-test"]
                if status["state"] == "warm":
                    break
                time.sleep(0.02)
            self.assertEqual(status["state"], "warm")
            self.assertEqual(status["reasoningEffort"], "high")
            self.assertIsNone(status["maxOutputTokens"])
            self.assertEqual(status["backend"], "codex_cli")
            self.assertNotIn("sk-test-private-key", json.dumps(status))
            manager.stop()

    def test_missing_cli_stops_without_http_fallback(self):
        manager, p = self.manager()
        p["keepalive_codex_path"] = str(self.root / "missing-codex")
        with contextlib.redirect_stdout(io.StringIO()):
            result = manager._acquire("any-test", p, 1)
        self.assertFalse(result[0])
        self.assertEqual(result.kind, "permanent")
        self.assertEqual(self.traces(), {})

    def test_stop_cancels_running_candidates_and_waiting_global_slots(self):
        manager, p = self.manager("hang")
        manager._inflight = threading.BoundedSemaphore(1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            thread = threading.Thread(target=manager._acquire, args=("any-test", p, 3))
            thread.start()
            until = time.monotonic() + 3
            while not self.traces() and time.monotonic() < until:
                time.sleep(0.02)
            manager.stop()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(manager._inflight.acquire(blocking=False))
        manager._inflight.release()
        self.assertEqual(manager._codex_winners, {})
        for pid, trace in self.traces().items():
            self.assert_exited(pid)
            self.assertFalse(Path(trace[0]["home"]).parent.exists())


@unittest.skipUnless(
    os.environ.get("AIPROXY_TEST_REAL_CODEX") == "1" and shutil.which("codex"),
    "opt-in installed-Codex test; all requests go to a local fake upstream",
)
class InstalledCodexTest(unittest.TestCase):
    def test_real_tui_reuses_process_and_sends_configured_model_and_effort(self):
        requests_seen = []
        paths_seen = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = json.dumps({"object": "list", "data": [
                    {"id": "gpt-6-astra", "object": "model"},
                ]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["content-length"])))
                requests_seen.append(data)
                paths_seen.append(self.path)
                rid = "resp_local_" + str(len(requests_seen))
                mid = "msg_local_" + str(len(requests_seen))
                text = "Hello! I am ready to help."
                item = {
                    "id": mid, "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
                response = {
                    "id": rid, "object": "response", "created_at": int(time.time()),
                    "model": "gpt-6-astra", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
                }
                events = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {**item, "status": "in_progress", "content": []}},
                    {"type": "response.content_part.added", "item_id": mid, "output_index": 0,
                     "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "item_id": mid, "output_index": 0,
                     "content_index": 0, "delta": text},
                    {"type": "response.output_text.done", "item_id": mid, "output_index": 0,
                     "content_index": 0, "text": text},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                body = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                               for e in events).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        p = provider(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="synthetic-local-only-key", trust_env_proxy=True,
            models={"gpt-6-astra": {"reasoning_effort": "high"}},
        )
        session = cli.CodexSession(p, "gpt-6-astra", "high")
        self.addCleanup(session.stop)
        blocked = {key: "http://127.0.0.1:1" for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        )}
        blocked.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        with mock.patch.dict(os.environ, blocked):
            first = session.prompt("Hi", 20)
            self.assertTrue(first[0], first)
            pid = session.pid
            second = session.prompt("在吗？短回", 20, short=True)
            self.assertTrue(second[0], second)
        self.assertEqual(session.pid, pid)
        def last_user(data):
            texts = [
                "".join(block.get("text", "") for block in item.get("content", [])
                        if isinstance(block, dict))
                for item in data.get("input", []) if item.get("role") == "user"
            ]
            return texts[-1] if texts else ""
        # Installed Codex also sends independent title-generation requests.
        # Verify our two actual turns, not the CLI's internal request count.
        turns = [data for data in requests_seen if last_user(data) in {"Hi", "在吗？短回"}]
        self.assertEqual([last_user(data) for data in turns], ["Hi", "在吗？短回"])
        self.assertTrue(all(path == "/v1/responses" for path in paths_seen))
        for request in requests_seen:
            self.assertEqual(request["model"], "gpt-6-astra")
        for request in turns:
            self.assertEqual(request["reasoning"]["effort"], "high")
            self.assertFalse(request.get("tools"))
        self.assertGreater(len(turns[1]["input"]), len(turns[0]["input"]))
        self.assertEqual(turns[0]["prompt_cache_key"], turns[1]["prompt_cache_key"])
        self.assertIsNotNone(session.first_token_ms)


if __name__ == "__main__":
    unittest.main()
