import importlib.util
import io
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ai_api_proxy", ROOT / "proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(proxy)


def provider_config(base_url: str, api_mode: str = "chat_completions", auth_mode: str = "bearer") -> dict:
    return {
        "base_url": base_url,
        "api_key": "",
        "api_mode": api_mode,
        "custom_endpoint": "",
        "headers": {},
        "remove_headers": set(),
        "auth_mode": auth_mode,
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


    def test_responses_payload_to_anthropic_messages_maps_tool_history_and_specs(self):
        body = responses_tool_payload(stream=True)
        body["instructions"] = "system prompt"
        context = proxy.build_codex_tool_context_from_request(body)
        payload = proxy.responses_payload_to_anthropic_messages(body, context)

        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["system"], "system prompt")
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][0]["content"][0], {"type": "text", "text": "weather?"})
        tool_use = payload["messages"][1]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["id"], "call_1")
        self.assertEqual(tool_use["name"], "get_weather")
        self.assertEqual(tool_use["input"], {"city": "Tokyo"})
        tool_result = payload["messages"][2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "call_1")
        self.assertEqual(tool_result["content"], '{"temp":21}')
        self.assertEqual(payload["tools"][0]["name"], "get_weather")
        self.assertEqual(payload["tools"][0]["input_schema"]["properties"]["city"]["type"], "string")
        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "get_weather"})

    def test_messages_payload_to_responses_maps_anthropic_tool_use_and_thinking(self):
        converted = proxy.messages_payload_to_responses(
            {
                "id": "msg_1",
                "model": "claude-sonnet",
                "content": [
                    {"type": "thinking", "thinking": "checking"},
                    {"type": "text", "text": "Need weather."},
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Tokyo"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2},
            },
            model="claude-sonnet",
        )

        self.assertEqual(converted["output_text"], "Need weather.")
        self.assertEqual([item["type"] for item in converted["output"]], ["reasoning", "message", "function_call"])
        self.assertEqual(converted["output"][0]["summary"][0]["text"], "checking")
        self.assertEqual(converted["output"][2]["call_id"], "toolu_1")
        self.assertEqual(converted["output"][2]["arguments"], '{"city":"Tokyo"}')
        self.assertEqual(converted["usage"]["input_tokens_details"], {"cached_tokens": 2})

    def test_anthropic_messages_stream_to_responses_emits_tool_call_and_final_text(self):
        class FakeResp:
            status_code = 200
            headers = {"Content-Type": "text/event-stream"}

            def iter_lines(self, decode_unicode=False):
                chunks = [
                    {"type": "message_start", "message": {"id": "msg_stream", "model": "claude-sonnet", "usage": {"input_tokens": 4}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Need weather."}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}},
                    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"city":"Tokyo"}' }},
                    {"type": "content_block_stop", "index": 1},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
                    {"type": "message_stop"},
                ]
                for chunk in chunks:
                    yield ("data: " + json.dumps(chunk, separators=(",", ":"))).encode()

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
        handler._send_messages_stream_as_responses(FakeResp(), model="claude-sonnet")
        output = handler.wfile.getvalue().decode()

        self.assertIn("event: response.output_text.delta", output)
        self.assertIn("event: response.function_call_arguments.delta", output)
        self.assertIn("event: response.function_call_arguments.done", output)
        self.assertIn('"output_text":"Need weather."', output)
        self.assertIn('"call_id":"toolu_1"', output)
        self.assertIn('"input_tokens":4', output)
        self.assertIn('"output_tokens":5', output)

    def test_proxy_messages_mode_anthropic_receives_tool_use(self):
        seen = []

        class FakeAnthropicHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or "0")).decode())
                seen.append({"path": self.path, "body": body})
                self.server.testcase.assertEqual(self.path, "/v1/messages")
                self.server.testcase.assertEqual(body["messages"][1]["content"][0]["type"], "tool_use")
                self.server.testcase.assertEqual(body["messages"][2]["content"][0]["type"], "tool_result")
                out = {
                    "id": "msg_reply",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "tool_use", "id": "toolu_reply", "name": "get_weather", "input": {"city": "Tokyo"}}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 9, "output_tokens": 1},
                }
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropicHandler)
        fake.testcase = self
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        server = proxy.HeaderProxyServer(
            ("127.0.0.1", 0),
            proxy.ProxyHandler,
            {"listen": "127.0.0.1", "port": 0, "verbose": False, "providers": {"anth": provider_config(f"http://127.0.0.1:{fake.server_address[1]}/v1", api_mode="messages", auth_mode="anthropic")}},
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/anth/v1/responses",
                data=json.dumps(responses_tool_payload(stream=False)).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = json.loads(r.read().decode())
            self.assertEqual([item["path"] for item in seen], ["/v1/messages"])
            self.assertEqual(payload["output"][0]["type"], "function_call")
            self.assertEqual(payload["output"][0]["call_id"], "toolu_reply")
            self.assertEqual(payload["output"][0]["arguments"], '{"city":"Tokyo"}')
        finally:
            server.shutdown(); server.server_close(); fake.shutdown(); fake.server_close()


    def test_responses_payload_to_chat_collapses_system_messages_and_maps_file_audio(self):
        body = {
            "model": "gpt-4.1",
            "instructions": "root instructions",
            "input": [
                {"role": "developer", "content": [{"type": "input_text", "text": "developer instructions"}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": "file_1", "filename": "notes.txt"},
                        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
                    ],
                },
            ],
        }

        chat = proxy.responses_payload_to_chat(body)

        self.assertEqual(chat["messages"][0], {"role": "system", "content": "root instructions\n\ndeveloper instructions"})
        self.assertEqual([message["role"] for message in chat["messages"]], ["system", "user"])
        user_content = chat["messages"][1]["content"]
        self.assertEqual(user_content[0], {"type": "file", "file": {"file_id": "file_1", "filename": "notes.txt"}})
        self.assertEqual(user_content[1], {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}})

    def test_responses_payload_to_chat_preserves_reasoning_for_tool_call_history(self):
        body = {
            "model": "deepseek-reasoner",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Need to call weather tool."}]},
                {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": {"city": "Tokyo"}},
            ],
            "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
        }

        chat = proxy.responses_payload_to_chat(body)
        assistant = chat["messages"][1]

        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["reasoning_content"], "Need to call weather tool.")
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"city":"Tokyo"}')

    def test_chat_payload_to_responses_extracts_reasoning_and_attaches_to_tool_call(self):
        converted = proxy.chat_payload_to_responses({
            "id": "chatcmpl_reasoning",
            "model": "deepseek-reasoner",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think>Need to call weather tool.</think>\nCalling now.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        })

        self.assertEqual(converted["output_text"], "Calling now.")
        self.assertEqual([item["type"] for item in converted["output"]], ["reasoning", "message", "function_call"])
        self.assertEqual(converted["output"][0]["summary"][0]["text"], "Need to call weather tool.")
        self.assertEqual(converted["output"][2]["reasoning_content"], "Need to call weather tool.")

    def test_chat_payload_to_responses_extracts_reasoning_content_field(self):
        converted = proxy.chat_payload_to_responses({
            "id": "chatcmpl_reasoning_field",
            "model": "deepseek-reasoner",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Use the tool.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        })

        self.assertEqual([item["type"] for item in converted["output"]], ["reasoning", "function_call"])
        self.assertEqual(converted["output"][0]["summary"][0]["text"], "Use the tool.")
        self.assertEqual(converted["output"][1]["reasoning_content"], "Use the tool.")

    def test_chat_stream_to_responses_emits_reasoning_summary_events(self):
        class FakeResp:
            status_code = 200
            headers = {"Content-Type": "text/event-stream"}

            def iter_lines(self, decode_unicode=False):
                for line in [
                    b'data: {"id":"chatcmpl_rs","model":"deepseek-reasoner","choices":[{"delta":{"reasoning_content":"Need tool."}}]}',
                    b'data: {"id":"chatcmpl_rs","model":"deepseek-reasoner","choices":[{"delta":{"content":"Answer."},"finish_reason":"stop"}]}',
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
        handler._send_chat_stream_as_responses(FakeResp(), model="deepseek-reasoner")
        output = handler.wfile.getvalue().decode()

        self.assertIn("event: response.reasoning_summary_text.delta", output)
        self.assertIn("event: response.reasoning_summary_text.done", output)
        self.assertIn('"type":"reasoning"', output)
        self.assertIn('"output_text":"Answer."', output)

    def test_chat_stream_to_responses_extracts_inline_think_block(self):
        class FakeResp:
            status_code = 200
            headers = {"Content-Type": "text/event-stream"}

            def iter_lines(self, decode_unicode=False):
                for line in [
                    b'data: {"id":"chatcmpl_think","model":"deepseek-reasoner","choices":[{"delta":{"content":"<think>Need"}}]}',
                    b'data: {"id":"chatcmpl_think","model":"deepseek-reasoner","choices":[{"delta":{"content":" tool.</think>Answer."},"finish_reason":"stop"}]}',
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
        handler._send_chat_stream_as_responses(FakeResp(), model="deepseek-reasoner")
        output = handler.wfile.getvalue().decode()

        self.assertIn('"text":"Need tool."', output)
        self.assertIn('"output_text":"Answer."', output)
        self.assertNotIn('<think>', output)


    def test_converted_chat_error_response_is_responses_error_envelope(self):
        class ErrorChatHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                self.server.testcase.assertEqual(self.path, "/v1/chat/completions")
                self.rfile.read(int(self.headers.get("Content-Length") or "0"))
                data = b"invalid params, chat content has invalid message role: system"
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        fake = ThreadingHTTPServer(("127.0.0.1", 0), ErrorChatHandler)
        fake.testcase = self
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        server = proxy.HeaderProxyServer(
            ("127.0.0.1", 0),
            proxy.ProxyHandler,
            {"listen": "127.0.0.1", "port": 0, "verbose": False, "providers": {"errchat": provider_config(f"http://127.0.0.1:{fake.server_address[1]}/v1")}},
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/errchat/v1/responses",
                data=json.dumps(responses_tool_payload(stream=False)).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(req, timeout=10)
            err = raised.exception
            payload = json.loads(err.read().decode())
            self.assertEqual(err.code, 400)
            self.assertEqual(payload["error"]["type"], "invalid_request_error")
            self.assertEqual(payload["error"]["code"], "upstream_error")
            self.assertIn("upstream /chat/completions returned 400", payload["error"]["message"])
            self.assertIn("invalid message role", payload["error"]["message"])
        finally:
            server.shutdown(); server.server_close(); fake.shutdown(); fake.server_close()

    def test_converted_stream_chat_error_response_is_response_failed_sse(self):
        class ErrorChatHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                self.server.testcase.assertEqual(self.path, "/v1/chat/completions")
                self.rfile.read(int(self.headers.get("Content-Length") or "0"))
                data = json.dumps({"error": {"message": "provider exploded", "type": "server_error", "code": "boom"}}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        fake = ThreadingHTTPServer(("127.0.0.1", 0), ErrorChatHandler)
        fake.testcase = self
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        server = proxy.HeaderProxyServer(
            ("127.0.0.1", 0),
            proxy.ProxyHandler,
            {"listen": "127.0.0.1", "port": 0, "verbose": False, "providers": {"errstream": provider_config(f"http://127.0.0.1:{fake.server_address[1]}/v1")}},
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/errstream/v1/responses",
                data=json.dumps(responses_tool_payload(stream=True)).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(req, timeout=10)
            err = raised.exception
            output = err.read().decode()
            self.assertEqual(err.code, 500)
            self.assertIn("text/event-stream", err.headers.get("Content-Type"))
            self.assertIn("event: response.failed", output)
            self.assertIn('"status":"failed"', output)
            self.assertIn('"type":"server_error"', output)
            self.assertIn('"code":"boom"', output)
            self.assertIn("provider exploded", output)
            self.assertIn("data: [DONE]", output)
        finally:
            server.shutdown(); server.server_close(); fake.shutdown(); fake.server_close()



if __name__ == "__main__":
    unittest.main()
