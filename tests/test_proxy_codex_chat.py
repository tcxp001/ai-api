import importlib.util
import io
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ai_api_proxy", ROOT / "proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(proxy)


def provider_config(base_url: str, api_mode: str = "chat_completions") -> dict:
    return {
        "base_url": base_url,
        "api_key": "",
        "api_mode": api_mode,
        "custom_endpoint": "",
        "headers": {},
        "remove_headers": set(),
        "auth_mode": "bearer",
        "anthropic_version": "2023-06-01",
        "trust_env_proxy": False,
        "pool_maxsize": 20,
        "connect_timeout": 5,
        "read_timeout": 5,
        "models": {},
        "reasoning_effort": "",
        "fallback_responses_to_chat": True,
    }


def responses_tool_payload(stream: bool = False) -> dict:
    return {
        "model": "deepseek-v4-pro",
        "stream": stream,
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
            {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": {"city": "Tokyo"}},
            {"type": "function_call_output", "call_id": "call_1", "output": {"temp": 21}},
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                "strict": True,
            }
        ],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "parallel_tool_calls": True,
    }


class CodexChatConversionTests(unittest.TestCase):
    def test_responses_payload_to_chat_maps_tool_history_and_specs(self):
        body = responses_tool_payload(stream=True)
        context = proxy.build_codex_tool_context_from_request(body)
        chat = proxy.responses_payload_to_chat(body, context)

        self.assertEqual(chat["messages"][0], {"role": "user", "content": "weather?"})
        tool_call = chat["messages"][1]["tool_calls"][0]
        self.assertEqual(tool_call["id"], "call_1")
        self.assertEqual(tool_call["function"]["name"], "get_weather")
        self.assertEqual(tool_call["function"]["arguments"], '{"city":"Tokyo"}')
        self.assertEqual(chat["messages"][2]["role"], "tool")
        self.assertEqual(chat["messages"][2]["tool_call_id"], "call_1")
        self.assertEqual(chat["tools"][0]["function"]["name"], "get_weather")
        self.assertTrue(chat["tools"][0]["function"]["strict"])
        self.assertEqual(chat["tool_choice"]["function"]["name"], "get_weather")
        self.assertTrue(chat["stream_options"]["include_usage"])

    def test_chat_payload_to_responses_restores_namespace_tool_call(self):
        body = {
            "model": "m",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_gmail",
                    "namespace": "mcp__codex_apps__gmail",
                    "name": "search_emails",
                    "arguments": {"query": "in:inbox"},
                }
            ],
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__codex_apps__gmail",
                    "tools": [{"type": "function", "name": "search_emails", "description": "Search", "parameters": {"type": "object"}}],
                }
            ],
        }
        context = proxy.build_codex_tool_context_from_request(body)
        chat = proxy.responses_payload_to_chat(body, context)
        chat_name = chat["messages"][0]["tool_calls"][0]["function"]["name"]
        response = proxy.chat_payload_to_responses(
            {
                "id": "chatcmpl_1",
                "model": "m",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_gmail",
                                    "type": "function",
                                    "function": {"name": chat_name, "arguments": '{"query":"in:inbox"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            model="m",
            tool_context=context,
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["name"], "search_emails")
        self.assertEqual(item["namespace"], "mcp__codex_apps__gmail")
        self.assertEqual(item["call_id"], "call_gmail")

    def test_chat_stream_to_responses_emits_tool_call_events(self):
        class FakeResp:
            status_code = 200
            headers = {"Content-Type": "text/event-stream"}

            def iter_lines(self, decode_unicode=False):
                for line in [
                    b'data: {"id":"chatcmpl_2","model":"gpt-5.4","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather"}}]}}]}',
                    b'data: {"id":"chatcmpl_2","model":"gpt-5.4","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\\"city\\\":\\\"Tokyo\\\"}"}}]},"finish_reason":"tool_calls"}]}',
                    b"data: [DONE]",
                ]:
                    yield line

        class FakeProxyHandler(proxy.ProxyHandler):
            def __init__(self):
                pass

            def send_response(self, status):
                self.status = status

            def send_header(self, key, value):
                pass

            def end_headers(self):
                pass

        handler = FakeProxyHandler()
        handler.command = "POST"
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        handler._send_chat_stream_as_responses(FakeResp(), model="gpt-5.4")
        output = handler.wfile.getvalue().decode()

        self.assertIn("event: response.function_call_arguments.delta", output)
        self.assertIn("event: response.function_call_arguments.done", output)
        self.assertIn('"type":"function_call"', output)
        self.assertIn('"call_id":"call_1"', output)
        self.assertIn("data: [DONE]", output)

    def test_proxy_chat_completions_endpoint_conversion(self):
        seen = []

        class FakeChatHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or "0")).decode())
                seen.append({"path": self.path, "body": body})
                self.server.testcase.assertEqual(self.path, "/v1/chat/completions")
                self.server.testcase.assertEqual(body["messages"][1]["tool_calls"][0]["function"]["name"], "get_weather")
                out = {
                    "id": "chatcmpl_nonstream",
                    "object": "chat.completion",
                    "created": 123,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_2",
                                        "type": "function",
                                        "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatHandler)
        fake.testcase = self
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        server = proxy.HeaderProxyServer(
            ("127.0.0.1", 0),
            proxy.ProxyHandler,
            {"listen": "127.0.0.1", "port": 0, "verbose": False, "providers": {"testchat": provider_config(f"http://127.0.0.1:{fake.server_address[1]}/v1")}},
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/testchat/v1/responses",
                data=json.dumps(responses_tool_payload(stream=False)).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = json.loads(r.read().decode())
            self.assertEqual([item["path"] for item in seen], ["/v1/chat/completions"])
            self.assertEqual(payload["output"][0]["type"], "function_call")
            self.assertEqual(payload["output"][0]["call_id"], "call_2")
            self.assertEqual(payload["usage"]["input_tokens"], 10)
        finally:
            server.shutdown(); server.server_close(); fake.shutdown(); fake.server_close()

    def test_codex_responses_stream_fallback_to_chat(self):
        seen = []

        class FallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or "0")).decode())
                seen.append(self.path)
                if self.path == "/v1/responses":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.server.testcase.assertEqual(self.path, "/v1/chat/completions")
                self.server.testcase.assertTrue(body["stream"])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [
                    {"id": "chatcmpl_fb", "model": body["model"], "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_fb", "type": "function", "function": {"name": "get_weather"}}]}}]},
                    {"id": "chatcmpl_fb", "model": body["model"], "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Tokyo"}'}}]}, "finish_reason": "tool_calls"}]},
                ]
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FallbackHandler)
        fake.testcase = self
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        server = proxy.HeaderProxyServer(
            ("127.0.0.1", 0),
            proxy.ProxyHandler,
            {"listen": "127.0.0.1", "port": 0, "verbose": False, "providers": {"fallback": provider_config(f"http://127.0.0.1:{fake.server_address[1]}/v1", api_mode="codex_responses")}},
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/fallback/v1/responses",
                data=json.dumps(responses_tool_payload(stream=True)).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                text = r.read().decode()
            self.assertEqual(seen, ["/v1/responses", "/v1/chat/completions"])
            self.assertIn("event: response.function_call_arguments.delta", text)
            self.assertIn("event: response.function_call_arguments.done", text)
            self.assertIn('"call_id":"call_fb"', text)
        finally:
            server.shutdown(); server.server_close(); fake.shutdown(); fake.server_close()


if __name__ == "__main__":
    unittest.main()
