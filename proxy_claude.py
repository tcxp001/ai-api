"""Bounded Anthropic Messages <-> Chat/Responses JSON conversion.

The request/schema/billing/usage conventions were compared against cc-switch
372b1698968ced988ce046d705732cb799000533, providers/{claude,transform,
transform_responses,reasoning_bridge}.rs. This is NOT an OAuth, hosted-tool or
SSE adapter. Model routing and HTTP error handling belong to the caller.

Deliberate safety differences from that baseline:
* Never manufacture thinking, replace broken tool arguments, or drop opaque
  state. Unsupported content fails closed, without echoing the input.
* Every Responses reasoning item is transported intact in our own versioned,
  scope-bound envelope, including unknown fields. Base64 is NOT encryption or
  authentication: callers must supply the same trusted upstream/account/model
  state_scope on both legs. Foreign visible Anthropic thinking is only a plain
  summary (Chat uses reasoning_content); it is never OpenAI encrypted_content.
* Explicit parallel-tool settings work in both modes; token budgets and content
  have no artificial floors, caps or truncation.

Responses does not support stop_sequences (omitted, as in the baseline).
Only ordinary function tools, text and images are in scope. Anthropic cache
control hints have no equivalent here and are omitted at protocol boundaries.
"""

import base64
import copy
import json
import re

from proxy_reasoning import apply_chat_reasoning


REASONING_PREFIX = "ai-api-responses-reasoning-v1:"
TOOL_RESULT_ERROR_MARKER = "[cc-switch:tool-result-error]"
TOOL_RESULT_MEDIA_MARKER = (
    "[cc-switch: tool result media moved to the following user message]"
)


class BridgeError(ValueError):
    """A safe, serializable Anthropic error; never contains upstream data."""

    def __init__(self, message, *, status_code=400, code="invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.error = {
            "type": "error",
            "error": {
                "type": "api_error" if status_code >= 500 else "invalid_request_error",
                "code": code,
                "message": message,
            },
        }


def _invalid(message="Invalid Messages request."):
    raise BridgeError(message)


def _upstream(message="Malformed or unsupported upstream JSON response."):
    raise BridgeError(message, status_code=502, code="invalid_upstream_response")


def _state():
    raise BridgeError(
        "Conversation reasoning state is malformed, incompatible, or belongs to "
        "another upstream/account/model; start a new conversation without that state.",
        code="incompatible_conversation_state",
    )


def _mode(mode):
    if mode not in ("chat", "responses"):
        _invalid("Bridge mode must be chat or responses.")


def _scope(scope):
    if not isinstance(scope, str):
        _invalid("state_scope must be a string.")
    try:
        scope.encode("utf-8")
    except UnicodeError:
        _invalid("state_scope must contain valid Unicode.")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _copy_json(value, fail):
    try:
        _json(value).encode("utf-8")
        return copy.deepcopy(value)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        fail()


def _object(value, fail):
    if not isinstance(value, dict):
        fail()
    return value


def _string(value, fail, *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        fail()
    return value


def _array(value, fail):
    if not isinstance(value, list):
        fail()
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _loads(value, fail):
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
        # JSON's 1e999 is accepted as infinity by Python's decoder, but is not
        # usable JSON tool input. Also reject unpaired Unicode surrogates.
        _json(parsed).encode("utf-8")
        return parsed
    except (ValueError, TypeError, RecursionError, UnicodeError):
        fail()


def _strip_billing(text):
    """Exactly one leading attribution line and at most one following newline."""
    if not text.startswith("x-anthropic-billing-header:"):
        return text
    match = re.search(r"\r\n|\r|\n", text)
    if match is None:
        return ""
    rest = text[match.end():]
    for newline in ("\r\n", "\n", "\r"):
        if rest.startswith(newline):
            return rest[len(newline):]
    return rest


def _system(value, mode):
    if isinstance(value, str):
        return _strip_billing(value)
    parts = []
    for block in _array(value, _invalid):
        _object(block, _invalid)
        if block.get("type", "text") != "text":
            _invalid("Only text system blocks can be bridged.")
        part = _strip_billing(_string(block.get("text"), _invalid))
        if part:
            parts.append(part)
    return ("\n" if mode == "chat" else "\n\n").join(parts)


def _clean_schema(schema, root=True):
    """Baseline schema normalization, not a generic recursive key scrubber."""
    if not isinstance(schema, dict):
        if root:
            _invalid("Tool input_schema must be an object.")
        return schema
    if root and "type" not in schema:
        schema["type"] = "object"
        schema.setdefault("properties", {})
    if schema.get("format") == "uri":
        del schema["format"]
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, value in properties.items():
            properties[key] = _clean_schema(value, False)
    if "items" in schema:
        schema["items"] = _clean_schema(schema["items"], False)
    return schema


def _tools(body, result, mode):
    if "tools" in body:
        tools = []
        for tool in _array(body["tools"], _invalid):
            _object(tool, _invalid)
            if tool.get("type") == "BatchTool":
                continue  # Legacy client-only aggregator, as in the source.
            if tool.get("type") not in (None, "custom"):
                _invalid("Only ordinary function tools can be bridged.")
            function = {
                "name": _string(tool.get("name"), _invalid, nonempty=True),
                "parameters": _clean_schema(tool.get("input_schema", {})),
            }
            if tool.get("description") is not None:
                function["description"] = _string(tool["description"], _invalid)
            tools.append(
                {"type": "function", "function": function} if mode == "chat"
                else {"type": "function", **function}
            )
        if tools:
            result["tools"] = tools
    if "tool_choice" not in body:
        return
    choice = body["tool_choice"]
    kind = choice if isinstance(choice, str) else _object(choice, _invalid).get("type")
    if kind in ("auto", "none", "any"):
        result["tool_choice"] = "required" if kind == "any" else kind
    elif kind == "tool" and isinstance(choice, dict):
        name = _string(choice.get("name"), _invalid, nonempty=True)
        result["tool_choice"] = (
            {"type": "function", "function": {"name": name}} if mode == "chat"
            else {"type": "function", "name": name}
        )
    else:
        _invalid("Unsupported tool_choice.")
    if isinstance(choice, dict) and "disable_parallel_tool_use" in choice:
        disabled = choice["disable_parallel_tool_use"]
        if not isinstance(disabled, bool):
            _invalid("disable_parallel_tool_use must be boolean.")
        result["parallel_tool_calls"] = not disabled


def _supports_effort(model):
    model = model.lower()
    if re.match(r"o[0-9]|gpt-[5-9]|grok-build-", model):
        return True
    match = re.match(r"grok-4\.([0-9]+)", model)
    minor = match.group(1).lstrip("0") if match else ""
    return len(minor) > 1 or (len(minor) == 1 and minor >= "5")


def _effort(body, configured):
    """Explicit request effort > request thinking > configured fallback."""
    output_config = _object(body.get("output_config", {}), _invalid)
    if "effort" in output_config:
        effort = _string(output_config["effort"], _invalid)
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            return ""
        max_models = (
            "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
            "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
        )
        if effort == "max" and body.get("model", "").lower() not in max_models:
            return "xhigh"
        return effort
    if "thinking" in body:
        thinking = _object(body["thinking"], _invalid)
        kind = thinking.get("type")
        if kind == "disabled":
            return "none"
        if kind == "adaptive":
            return "xhigh"
        if kind != "enabled":
            _invalid("Unsupported thinking configuration.")
        budget = thinking.get("budget_tokens")
        if budget is None:
            return "high"
        if type(budget) is not int or budget < 0:
            _invalid("thinking.budget_tokens must be a nonnegative integer.")
        return "low" if budget < 4000 else "medium" if budget < 16000 else "high"
    return configured


def _image(block, mode):
    source = _object(block.get("source"), _invalid)
    if source.get("type") == "url":
        url = _string(source.get("url"), _invalid, nonempty=True)
        if not url.lower().startswith(("https://", "http://")):
            _invalid("Image URL must use HTTP or HTTPS.")
    elif source.get("type") in ("base64", None):
        data = _string(source.get("data"), _invalid, nonempty=True)
        media_type = _string(source.get("media_type", "image/png"), _invalid)
        if not media_type.startswith("image/") or any(c in media_type for c in ",;\r\n"):
            _invalid("Invalid image media type.")
        url = f"data:{media_type};base64,{data}"
    else:
        _invalid("Unsupported image source.")
    return (
        {"type": "image_url", "image_url": {"url": url}} if mode == "chat"
        else {"type": "input_image", "image_url": url}
    )


def _reasoning_text(item, fail):
    if item.get("type") != "reasoning":
        fail()
    summary = _array(item.get("summary", []), fail)
    parts = []
    for part in summary:
        _object(part, fail)
        if part.get("type") not in ("summary_text", "reasoning_text"):
            fail()
        parts.append(_string(part.get("text"), fail))
    if "encrypted_content" in item and item["encrypted_content"] is not None:
        _string(item["encrypted_content"], fail)
    if "id" in item:
        _string(item["id"], fail, nonempty=True)
    if "status" in item and item["status"] not in ("completed", "incomplete"):
        fail()
    return "".join(parts)


def _encode_reasoning(item, scope):
    raw = _json({"version": 1, "state_scope": scope, "item": item}).encode("utf-8")
    return REASONING_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_reasoning(signature, scope):
    encoded = signature[len(REASONING_PREFIX):]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        _state()
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                              altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
            _state()
        envelope = _loads(raw.decode("utf-8"), _state)
    except (ValueError, UnicodeError):
        _state()
    _object(envelope, _state)
    if (set(envelope) != {"version", "state_scope", "item"}
            or type(envelope["version"]) is not int or envelope["version"] != 1
            or envelope["state_scope"] != scope):
        _state()
    item = _object(envelope["item"], _state)
    _reasoning_text(item, _state)
    return item


def _thinking(block, mode, scope):
    if any(key in block for key in ("encrypted_content", "reasoning_details")):
        _state()
    redacted = block["type"] == "redacted_thinking"
    if ("signature" in block if redacted else "data" in block):
        _state()
    visible = "" if redacted else _string(block.get("thinking"), _state)
    signature = block.get("data" if redacted else "signature")
    if signature is not None:
        _string(signature, _state, nonempty=True)
        if signature.startswith(REASONING_PREFIX):
            item = _decode_reasoning(signature, scope)
            if mode != "responses" or _reasoning_text(item, _state) != visible:
                _state()
            return item
        # Other bridge/version envelopes are not native Anthropic signatures.
        if signature.startswith(("ai-api-", "ccswitch-openai-reasoning-")):
            _state()
    if redacted or (signature is not None and not visible):
        _state()
    if not visible:
        return None
    return (visible if mode == "chat" else
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": visible}]})


def _tool_result(block, mode):
    """Return the tool payload and (Chat-only) associated user image parts."""
    error = block.get("is_error", False)
    if not isinstance(error, bool):
        _invalid("tool_result.is_error must be boolean.")
    content = block.get("content", "")
    if isinstance(content, str):
        if mode == "chat":
            return ((TOOL_RESULT_ERROR_MARKER + "\n" + content) if error else content), []
        if not error:
            return content, []
        return [{"type": "input_text", "text": TOOL_RESULT_ERROR_MARKER},
                {"type": "input_text", "text": content}], []
    parts = _array(content, _invalid)
    output, media = [], []
    for part in parts:
        _object(part, _invalid)
        if part.get("type") == "text":
            value = _string(part.get("text"), _invalid)
            output.append({"type": "text" if mode == "chat" else "input_text", "text": value})
        elif part.get("type") == "image":
            image = _image(part, mode)
            if mode == "chat":
                media.append(image)
                output.append({"type": "text", "text": TOOL_RESULT_MEDIA_MARKER})
            else:
                output.append(image)
        else:
            _invalid("Only text and image tool results can be bridged.")
    if mode == "chat":
        value = _json(output)
        return ((TOOL_RESULT_ERROR_MARKER + "\n" + value) if error else value), media
    if error:
        output.insert(0, {"type": "input_text", "text": TOOL_RESULT_ERROR_MARKER})
    return output, []


def _blocks(message):
    _object(message, _invalid)
    role = message.get("role")
    if role not in ("user", "assistant", "system"):
        _invalid("Unsupported Messages role.")
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks = _array(content, _invalid)
    for block in blocks:
        _object(block, _invalid)
        kind = block.get("type")
        if kind not in ("text", "image", "tool_use", "tool_result", "thinking", "redacted_thinking"):
            _invalid("Unsupported Messages content block.")
        if kind in ("tool_use", "thinking", "redacted_thinking") and role != "assistant":
            _invalid("Thinking and tool_use blocks require an assistant message.")
        if kind in ("image", "tool_result") and role != "user":
            _invalid("Images and tool_result blocks require a user message.")
    return role, blocks


def _call_input(block):
    call_id = _string(block.get("id"), _invalid, nonempty=True)
    name = _string(block.get("name"), _invalid, nonempty=True)
    arguments = _object(block.get("input", {}), _invalid)
    return call_id, name, _json(arguments)


def _chat_message(message, scope):
    role, blocks = _blocks(message)
    result, content, calls, reasoning, media = [], [], [], [], []
    for block in blocks:
        kind = block["type"]
        if kind == "text":
            content.append({"type": "text", "text": _string(block.get("text"), _invalid)})
        elif kind == "image":
            content.append(_image(block, "chat"))
        elif kind == "tool_use":
            call_id, name, arguments = _call_input(block)
            calls.append({"id": call_id, "type": "function",
                          "function": {"name": name, "arguments": arguments}})
        elif kind == "tool_result":
            call_id = _string(block.get("tool_use_id"), _invalid, nonempty=True)
            output, images = _tool_result(block, "chat")
            result.append({"role": "tool", "tool_call_id": call_id, "content": output})
            if images:
                media.append({"type": "text", "text": f"Tool result media for {call_id}:"})
                media.extend(images)
        else:
            thinking = _thinking(block, "chat", scope)
            if thinking is not None:
                reasoning.append(thinking)
    # Keep parallel results adjacent; tool-role messages cannot contain images.
    if media:
        result.append({"role": "user", "content": media})
    if content or calls or reasoning:
        value = (content[0]["text"] if len(content) == 1 and content[0]["type"] == "text"
                 else content or None)
        converted = {"role": role, "content": value}
        if calls:
            converted["tool_calls"] = calls
        if reasoning:
            converted["reasoning_content"] = "\n".join(reasoning)
        result.append(converted)
    return result


def _responses_message(message, scope):
    role, blocks = _blocks(message)
    result, content = [], []

    def flush():
        if content:
            result.append({"role": role, "content": list(content)})
            content.clear()

    for block in blocks:
        kind = block["type"]
        if kind == "text":
            content.append({"type": "output_text" if role == "assistant" else "input_text",
                            "text": _string(block.get("text"), _invalid)})
        elif kind == "image":
            content.append(_image(block, "responses"))
        else:
            flush()
            if kind == "tool_use":
                call_id, name, arguments = _call_input(block)
                result.append({"type": "function_call", "call_id": call_id,
                               "name": name, "arguments": arguments})
            elif kind == "tool_result":
                call_id = _string(block.get("tool_use_id"), _invalid, nonempty=True)
                output, _ = _tool_result(block, "responses")
                result.append({"type": "function_call_output", "call_id": call_id, "output": output})
            else:
                thinking = _thinking(block, "responses", scope)
                if thinking is not None:
                    result.append(thinking)
    flush()
    # Do not silently remove a trailing reasoning item as the source does.
    # Responses requires its following assistant message/function call.
    follower = False
    for item in reversed(result):
        if item.get("type") == "reasoning":
            if not follower:
                _state()
        elif item.get("role") == "assistant" or item.get("type") == "function_call":
            follower = True
    return result


def messages_to_upstream(body, mode, *, state_scope="", reasoning_config=None,
                         configured_effort="") -> dict:
    """Convert a Messages request without mutation.

    reasoning_config uses proxy_reasoning's Chat settings; Responses has its
    native reasoning.effort. configured_effort is a fallback when the request
    supplies neither output_config.effort nor thinking. stream is passed through
    (Chat streaming requests ask for usage); this module does not convert SSE.
    """
    _mode(mode)
    _scope(state_scope)
    _string(configured_effort, _invalid)
    body = _object(_copy_json(body, _invalid), _invalid)
    model = _string(body.get("model", ""), _invalid)
    result = {}
    if "model" in body:
        result["model"] = model
    converted = []
    if "system" in body:
        system = _system(body["system"], mode)
        if system:
            if mode == "chat":
                converted.append({"role": "system", "content": system})
            else:
                result["instructions"] = system
    convert = _chat_message if mode == "chat" else _responses_message
    for message in _array(body.get("messages"), _invalid):
        converted.extend(convert(message, state_scope))
    result["messages" if mode == "chat" else "input"] = converted
    if mode == "responses":
        # This bridge replays full history rather than previous_response_id.
        # Ask for portable reasoning even when upstream storage defaults to on.
        # Standard Responses include, not an OAuth/provider-specific setting.
        result["include"] = ["reasoning.encrypted_content"]
    if "max_tokens" in body:
        tokens = body["max_tokens"]
        if type(tokens) is not int or tokens <= 0:
            _invalid("max_tokens must be a positive integer.")
        key = ("max_output_tokens" if mode == "responses" else
               "max_completion_tokens" if re.match(r"o[0-9]", model) else "max_tokens")
        result[key] = tokens
    for key in ("temperature", "top_p"):
        if key in body:
            if type(body[key]) not in (int, float):
                _invalid("Sampling parameters must be numeric.")
            result[key] = body[key]
    if "stream" in body:
        if not isinstance(body["stream"], bool):
            _invalid("stream must be boolean.")
        result["stream"] = body["stream"]
    if mode == "chat" and body.get("stream"):
        result["stream_options"] = {"include_usage": True}
    if "stop_sequences" in body:
        stops = _array(body["stop_sequences"], _invalid)
        for stop in stops:
            _string(stop, _invalid)
        if mode == "chat":
            result["stop"] = stops
    _tools(body, result, mode)
    effort = _effort(body, configured_effort)
    if mode == "chat":
        try:
            apply_chat_reasoning(result, effort, reasoning_config,
                                 default_supports_effort=_supports_effort(model))
        except (ValueError, TypeError):
            _invalid("Invalid Chat reasoning configuration.")
    elif effort and effort.lower() not in ("none", "off", "disabled"):
        if _supports_effort(model) or configured_effort:
            result["reasoning"] = {"effort": effort}
    return result


def usage_to_messages(usage, mode) -> dict:
    """Convert inclusive upstream input into disjoint fresh/read/write counts."""
    _mode(mode)
    if usage is None:
        usage = {}
    usage = _object(_copy_json(usage, _upstream), _upstream)

    def count(*paths):
        for path in paths:
            value = usage
            for key in path.split("."):
                if value is None:
                    break
                _object(value, _upstream)
                value = value.get(key)
            if value is not None:
                if type(value) is not int or value < 0:
                    _upstream("Invalid upstream usage counts.")
                return value
        return None

    if mode == "chat":
        input_keys, output_keys = ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")
        details = ("prompt_tokens_details", "input_tokens_details")
    else:
        input_keys, output_keys = ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")
        details = ("input_tokens_details", "prompt_tokens_details")
    total, output = count(*input_keys) or 0, count(*output_keys) or 0
    read = count("cache_read_input_tokens", *(f"{key}.cached_tokens" for key in details))
    write = count("cache_creation_input_tokens", *(f"{key}.cache_write_tokens" for key in details))
    result = {"input_tokens": max(0, total - (read or 0) - (write or 0)), "output_tokens": output}
    for key, value in (("cache_read_input_tokens", read), ("cache_creation_input_tokens", write)):
        if value is not None:
            result[key] = value
    if "cache_creation" in usage:
        creation = _object(usage["cache_creation"], _upstream)
        if any(type(value) is not int or value < 0 for value in creation.values()):
            _upstream("Invalid upstream cache creation counts.")
        result["cache_creation"] = creation
    return result


def _arguments(value):
    if isinstance(value, str):
        value = {} if not value.strip() else _loads(value, _upstream)
    return _object(value, _upstream)


def _reply_call(call_id, function):
    _object(function, _upstream)
    if "arguments" not in function:
        _upstream("Upstream tool call is missing arguments.")
    return {
        "type": "tool_use",
        "id": _string(call_id, _upstream, nonempty=True),
        "name": _string(function.get("name"), _upstream, nonempty=True),
        "input": _arguments(function["arguments"]),
    }


def _split_thinking(value):
    after_ws = value.lstrip()
    for opening in ("<think>", "<thinking>"):
        if after_ws.startswith(opening):
            body = after_ws[len(opening):]
            closing = re.search(r"</think>|</thinking>", body)
            if closing is not None:
                return body[:closing.start()].strip("\r\n"), body[closing.end():].lstrip("\r\n")
    return None


def _reply_text_parts(value, *, chat_mode=False):
    """Map visible text/refusals; reject unknown nonrepresentable output."""
    if isinstance(value, str) and chat_mode:
        value = [{"type": "text", "text": value}]
    parts = _array(value, _upstream)
    result, first_text = [], True
    for part in parts:
        _object(part, _upstream)
        kind = part.get("type")
        if kind in ("text", "output_text"):
            text = _string(part.get("text"), _upstream)
            split = _split_thinking(text) if chat_mode and first_text else None
            first_text = False
            if split is not None:
                thinking, answer = split
                if thinking:
                    result.append({"type": "thinking", "thinking": thinking})
                if answer:
                    result.append({"type": "text", "text": answer})
            else:
                result.append({"type": "text", "text": text})
        elif kind == "refusal":
            result.append({"type": "text", "text": _string(part.get("refusal"), _upstream)})
        else:
            _upstream("Unsupported upstream response content.")
    return result


def _chat_reply(payload):
    choices = _array(payload.get("choices"), _upstream)
    if len(choices) != 1:
        _upstream("Expected exactly one upstream Chat completion.")
    choice = _object(choices[0], _upstream)
    if choice.get("error") is not None:
        _upstream("Upstream returned an error response.")
    message = _object(choice.get("message"), _upstream)
    if message.get("role", "assistant") != "assistant" or message.get("error") is not None:
        _upstream()
    # These are not Chat's visible reasoning_content. Silently ignoring an
    # extension containing signed/encrypted state would destroy the next turn.
    for key in ("reasoning_details", "encrypted_content", "signature", "thinking",
                "redacted_thinking", "reasoning"):
        if message.get(key) is not None:
            _upstream("Unsupported upstream reasoning state.")
    finish = choice.get("finish_reason")
    if finish not in ("stop", "length", "tool_calls", "function_call", "content_filter"):
        _upstream("Upstream Chat completion is not terminal.")
    content = []
    if message.get("reasoning_content") is not None:
        thinking = _string(message["reasoning_content"], _upstream)
        if thinking:
            content.append({"type": "thinking", "thinking": thinking})
    if message.get("content") is not None:
        content.extend(_reply_text_parts(message["content"], chat_mode=True))
    if message.get("refusal") is not None:
        content.append({"type": "text", "text": _string(message["refusal"], _upstream)})
    calls = message.get("tool_calls")
    calls = [] if calls is None else _array(calls, _upstream)
    for tool in calls:
        _object(tool, _upstream)
        if tool.get("type", "function") != "function":
            _upstream("Unsupported upstream tool call.")
        content.append(_reply_call(tool.get("id"), tool.get("function")))
    legacy = message.get("function_call")
    if legacy is not None:
        if calls:
            _upstream("Conflicting upstream tool call formats.")
        _object(legacy, _upstream)
        content.append(_reply_call(legacy.get("id"), legacy))
    has_calls = any(block["type"] == "tool_use" for block in content)
    if finish in ("tool_calls", "function_call") and not has_calls:
        _upstream("Upstream tool finish reason has no tool call.")
    if not content and finish not in ("length", "content_filter"):
        _upstream("Upstream Chat response has no supported content.")
    stop = {
        "stop": "end_turn", "length": "max_tokens", "content_filter": "end_turn",
        "tool_calls": "tool_use", "function_call": "tool_use",
    }[finish]
    return content, stop


def _responses_reply(payload, scope):
    status = payload.get("status")
    if status not in ("completed", "incomplete"):
        _upstream("Upstream Responses object is failed or nonterminal.")
    output = _array(payload.get("output"), _upstream)
    content = []
    for item in output:
        _object(item, _upstream)
        if item.get("error") is not None:
            _upstream("Upstream returned an error output item.")
        if item.get("status", "completed") not in ("completed", "incomplete"):
            _upstream("Upstream output item is failed or nonterminal.")
        kind = item.get("type")
        if kind == "message":
            if item.get("role", "assistant") != "assistant":
                _upstream()
            content.extend(_reply_text_parts(item.get("content")))
        elif kind == "function_call":
            content.append(_reply_call(item.get("call_id"), item))
        elif kind == "reasoning":
            thinking = _reasoning_text(item, _upstream)
            envelope = _encode_reasoning(item, scope)
            content.append(
                {"type": "thinking", "thinking": thinking, "signature": envelope} if thinking
                else {"type": "redacted_thinking", "data": envelope}
            )
        else:
            _upstream("Unsupported upstream Responses output item.")
    if status == "incomplete":
        details = payload.get("incomplete_details")
        reason = None if details is None else _object(details, _upstream).get("reason")
        if reason is not None:
            _string(reason, _upstream)
        stop = "max_tokens" if reason in (None, "max_tokens", "max_output_tokens") else "end_turn"
    else:
        if not content:
            _upstream("Completed upstream response has no supported content.")
        stop = "tool_use" if any(block["type"] == "tool_use" for block in content) else "end_turn"
    return content, stop


def upstream_to_messages(payload, mode, *, model="", state_scope="") -> dict:
    """Validate one nonstream upstream JSON reply and produce an Anthropic reply.

    A nonempty model overrides the upstream name (the caller's routed alias).
    Malformed/failed replies raise a safe 502; no raw upstream error is exposed.
    """
    _mode(mode)
    _scope(state_scope)
    _string(model, _invalid)
    payload = _object(_copy_json(payload, _upstream), _upstream)
    if payload.get("error") is not None:
        _upstream("Upstream returned an error response.")
    if mode == "chat" and payload.get("status") not in (None, "completed", "incomplete"):
        _upstream("Upstream Chat response is failed or nonterminal.")
    content, stop = (_chat_reply(payload) if mode == "chat"
                     else _responses_reply(payload, state_scope))
    call_ids = [block["id"] for block in content if block["type"] == "tool_use"]
    if len(set(call_ids)) != len(call_ids):
        _upstream("Upstream returned duplicate tool call identifiers.")
    return {
        "id": _string(payload.get("id", ""), _upstream),
        "type": "message", "role": "assistant",
        "content": content,
        "model": model or _string(payload.get("model", ""), _upstream),
        "stop_reason": stop, "stop_sequence": None,
        "usage": usage_to_messages(payload.get("usage"), mode),
    }
