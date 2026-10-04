"""Incremental Chat/Responses SSE -> Anthropic Messages events.

Compared with cc-switch 372b1698968ced988ce046d705732cb799000533,
providers/{streaming,streaming_responses,reasoning_bridge}.rs. Deliberate
differences: no whitespace/size cutoff, fabricated tool identities, hosted
tools, raw upstream error messages, or successful termination without a finish
signal. Chat requires a valid finish_reason and complete tools, then waits for
[DONE] or normal EOF, collecting late usage/arguments and rejecting errors.
Responses requires a terminal event; EOF without one remains an error.
Terminal snapshots may extend an emitted prefix, never replace or replay it.

HTTP, retries and transport exceptions belong to the caller. Its line iterator
should translate transport failures into an SSE ``event: error`` frame.
Only JSON fallback and complete reasoning items use the JSON bridge; imports
are lazy so this module can be loaded independently of the HTTP/JSON adapters.
"""

import copy
import json


class _Failure(ValueError):
    def __init__(self, message, code="invalid_upstream_stream"):
        super().__init__(message)
        self.code = code


def _fail(message="Malformed or unsupported upstream stream."):
    raise _Failure(message)


def _event(kind, **fields):
    return kind, {"type": kind, **fields}


def _error(message, code="invalid_upstream_stream"):
    return _event("error", error={
        "type": "api_error", "code": code, "message": message,
    })


def _object(value):
    if not isinstance(value, dict):
        _fail()
    return value


def _array(value):
    if not isinstance(value, list):
        _fail()
    return value


def _string(value):
    if not isinstance(value, str):
        _fail()
    return value


def _index(value):
    if type(value) is not int or value < 0:
        _fail("Invalid upstream content or tool index.")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("Duplicate upstream JSON field.")
        result[key] = value
    return result


def _constant(value):
    _fail("Invalid upstream JSON constant.")


def _loads(text):
    try:
        return json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=_constant)
    except (ValueError, RecursionError):
        _fail("Invalid JSON in upstream stream.")


def _dump(value, *, sort_keys=False):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False, sort_keys=sort_keys)
    except (ValueError, TypeError, RecursionError):
        _fail("Invalid JSON in upstream content.")


def _frames(lines):
    """Parse iter_lines, including multi-data frames and an unterminated tail."""
    event, data, raw = "", [], []
    kind = None
    first = True

    def frame():
        if event in ("error", "response.failed", "response.error"):
            # Error bodies can be plain text, HTML, or contain credentials.
            return "error", {}
        if not data:
            return None
        text = "\n".join(data)
        if text.strip() == "[DONE]":
            return "[DONE]", {}
        return event, _object(_loads(text))

    for line in lines:
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8")
            except UnicodeError:
                _fail("Invalid UTF-8 in upstream stream.")
        line = _string(line).rstrip("\r\n")
        if first:
            line = line.lstrip("\ufeff")
            first = False
        if kind is None:
            if not line.strip():
                continue
            kind = "json" if line.lstrip().startswith(("{", "[")) else "sse"
        if kind == "json":
            raw.append(line)
            continue
        if not line:
            value = frame()
            if value is not None:
                yield value
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        if line.lstrip().startswith(("{", "[")):
            _fail("Cannot switch to JSON after an SSE stream has begun.")
        field, colon, value = line.partition(":")
        if colon and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        # id/retry and future SSE transport fields carry no model output.
    if kind == "json":
        yield "json", _object(_loads("\n".join(raw)))
    else:
        value = frame()
        if value is not None:
            yield value


def _merge_usage(target, update):
    for key, value in _object(update).items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_usage(target[key], value)
        elif value is not None:
            target[key] = copy.deepcopy(value)


class _Block:
    def __init__(self, kind, **metadata):
        self.kind = kind
        self.metadata = metadata
        self.index = None
        self.closed = False
        self.chunks = []
        self.sent = 0
        self.signature = None

    @property
    def text(self):
        return "".join(self.chunks)

    def append(self, text, snapshot=False):
        text = _string(text)
        if snapshot:
            previous = self.text
            if not text.startswith(previous):
                # Equivalent JSON formatting is not a change in tool input.
                # Canonical JSON, unlike Python equality, distinguishes true/1
                # and false/0 even inside nested arrays and objects.
                if self.kind == "tool_use" and previous.strip() and text.strip():
                    if (_dump(_loads(previous), sort_keys=True)
                            == _dump(_loads(text), sort_keys=True)):
                        return
                _fail("Upstream snapshot conflicts with already streamed content.")
            text = text[len(previous):]
        if text:
            if self.closed:
                _fail("Upstream changed an already closed content block.")
            self.chunks.append(text)

    def identify(self, call_id=None, name=None):
        for key, value in (("id", call_id), ("name", name)):
            if value is None or value == "":
                continue
            value = _string(value)
            previous = self.metadata.get(key)
            if previous and previous != value:
                _fail("Upstream changed a tool identity.")
            self.metadata[key] = value


class _Lifecycle:
    def __init__(self, model=""):
        self.id = ""
        self.model = model
        self.override_model = bool(model)
        self.started = False
        self.blocks = []

    def metadata(self, value):
        for key in ("id", "model"):
            if value.get(key) is not None:
                text = _string(value[key])
                if key == "model" and not self.override_model and not self.started:
                    self.model = text
                elif key == "id" and not self.started:
                    self.id = text

    def start(self, usage=None):
        if self.started:
            return []
        self.started = True
        usage = {"input_tokens": 0, **(usage or {}), "output_tokens": 0}
        return [_event("message_start", message={
            "id": self.id, "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": usage,
        })]

    def emit(self, block):
        if block.closed:
            return []
        if block.kind == "tool_use" and not all(
                block.metadata.get(key) for key in ("id", "name")):
            return []
        events = self.start()
        if block.index is None:
            block.index = len(self.blocks)
            self.blocks.append(block)
            content = {"type": block.kind, **block.metadata}
            if block.kind in ("text", "thinking"):
                content[block.kind] = ""
            elif block.kind == "tool_use":
                content["input"] = {}
            events.append(_event("content_block_start", index=block.index,
                                 content_block=content))
        field, delta_type = {
            "text": ("text", "text_delta"),
            "thinking": ("thinking", "thinking_delta"),
            "tool_use": ("partial_json", "input_json_delta"),
            "redacted_thinking": (None, None),
        }[block.kind]
        for text in block.chunks[block.sent:]:
            events.append(_event("content_block_delta", index=block.index,
                                 delta={"type": delta_type, field: text}))
        block.sent = len(block.chunks)
        return events

    def signature(self, block, signature):
        signature = _string(signature)
        if block.signature is not None:
            if block.signature != signature:
                _fail("Upstream changed an already emitted reasoning signature.")
            return []
        events = self.emit(block)
        if signature:
            events.append(_event("content_block_delta", index=block.index,
                                 delta={"type": "signature_delta",
                                        "signature": signature}))
        block.signature = signature
        return events

    def close(self, block):
        if block.index is None or block.closed:
            return []
        block.closed = True
        return [_event("content_block_stop", index=block.index)]

    def finish(self, stop_reason, usage, stop_sequence=None):
        events = self.start(usage)
        for block in self.blocks:
            events.extend(self.close(block))
        events.append(_event("message_delta", delta={
            "stop_reason": stop_reason, "stop_sequence": stop_sequence,
        }, usage=usage))
        events.append(_event("message_stop"))
        return events


def _message_events(message):
    message = _object(message)
    life = _Lifecycle(_string(message.get("model", "")))
    life.metadata(message)
    usage = _object(message.get("usage", {"input_tokens": 0, "output_tokens": 0}))
    content = _array(message.get("content"))
    if message.get("type", "message") != "message" or message.get("role", "assistant") != "assistant":
        _fail("Expected an Anthropic assistant message.")
    stop = message.get("stop_reason")
    if stop not in ("end_turn", "max_tokens", "tool_use", "stop_sequence",
                    "pause_turn", "refusal", "model_context_window_exceeded"):
        _fail("Expected a terminal Anthropic message.")
    yield from life.start(usage)
    for item in content:
        item = _object(item)
        kind = item.get("type")
        if kind in ("text", "thinking"):
            block = _Block(kind)
            block.append(item.get(kind, ""))
        elif kind == "tool_use":
            block = _Block(kind)
            block.identify(item.get("id"), item.get("name"))
            if not all(block.metadata.get(key) for key in ("id", "name")):
                _fail("Upstream tool call is missing its identity.")
            block.append(_dump(_object(item.get("input", {}))))
        elif kind == "redacted_thinking":
            block = _Block(kind, data=_string(item.get("data")))
        else:
            _fail("Unsupported Anthropic content block.")
        yield from life.emit(block)
        if kind == "thinking" and item.get("signature"):
            yield from life.signature(block, item["signature"])
        yield from life.close(block)
    yield from life.finish(stop, usage, message.get("stop_sequence"))


def message_to_events(message):
    """Turn an already-converted Anthropic JSON message into one SSE lifecycle."""
    try:
        yield from _message_events(message)
    except _Failure as exc:
        yield _error(str(exc), exc.code)


class _InlineThinking:
    """The baseline's leading-tag splitter; buffer only undecidable suffixes."""
    def __init__(self):
        self.mode = "detect"
        self.pending = ""
        self.separators = False

    def push(self, text):
        if self.mode == "text":
            if self.separators:
                text = text.lstrip("\r\n")
                self.separators = not bool(text)
            return [("text", text)] if text else []
        self.pending += text
        if self.mode == "detect":
            stripped = self.pending.lstrip()
            openings = ("<think>", "<thinking>")
            opening = next((tag for tag in openings if stripped.startswith(tag)), None)
            if opening:
                self.pending = stripped[len(opening):]
                self.mode, self.separators = "thinking", True
            elif not stripped or any(tag.startswith(stripped) for tag in openings):
                return []
            else:
                self.mode = "text"
                text, self.pending = self.pending, ""
                return [("text", text)]
        if self.separators:
            self.pending = self.pending.lstrip("\r\n")
            self.separators = not bool(self.pending)
        closes = ("</think>", "</thinking>")
        matches = [(self.pending.index(tag), tag) for tag in closes if tag in self.pending]
        if matches:
            pos, tag = min(matches)
            thinking = self.pending[:pos].rstrip("\r\n")
            text = self.pending[pos + len(tag):]
            self.pending, self.mode, self.separators = "", "text", True
            return ([("thinking", thinking)] if thinking else []) + self.push(text)
        held = max([0] + [size for tag in closes for size in range(1, len(tag))
                          if self.pending.endswith(tag[:size])])
        boundary = len(self.pending) - held
        boundary = len(self.pending[:boundary].rstrip("\r\n"))
        thinking, self.pending = self.pending[:boundary], self.pending[boundary:]
        return [("thinking", thinking)] if thinking else []

    def flush(self):
        text, self.pending = self.pending, ""
        kind = "thinking" if self.mode == "thinking" else "text"
        self.mode, self.separators = "text", False
        return [(kind, text)] if text else []


class _Chat:
    def __init__(self, model, scope, usage_converter):
        self.life = _Lifecycle(model)
        self.usage_converter = usage_converter
        self.usage = {}
        self.finish_reason = None
        self.current = None
        self.tools = {}
        self.inline = _InlineThinking()

    def visible(self, parts):
        events = []
        for kind, text in parts:
            if not text:
                continue
            if self.current is None or self.current.kind != kind:
                if self.current:
                    events.extend(self.life.close(self.current))
                self.current = _Block(kind)
            self.current.append(text)
            events.extend(self.life.emit(self.current))
        return events

    def flush(self):
        return self.visible(self.inline.flush())

    def feed(self, event, data):
        self.life.metadata(data)
        if data.get("usage") is not None:
            _merge_usage(self.usage, data["usage"])
        usage = self.usage_converter(self.usage, "chat")
        choices = _array(data.get("choices", []))
        if not choices:
            return []
        if len(choices) != 1:
            _fail("Expected exactly one streaming Chat choice.")
        choice = _object(choices[0])
        if choice.get("index", 0) != 0:
            _fail("Unsupported streaming Chat choice index.")
        delta = _object(choice.get("delta", {}))
        if delta.get("role", "assistant") != "assistant":
            _fail()
        if delta.get("error") is not None or choice.get("error") is not None:
            _fail("Upstream returned a stream error.")
        if any(delta.get(key) is not None for key in ("audio", "images")):
            _fail("Unsupported upstream Chat content.")
        if any(delta.get(key) is not None for key in (
                "reasoning_details", "encrypted_content", "signature",
                "thinking", "redacted_thinking")):
            _fail("Unsupported upstream reasoning state.")
        # Preserve the baseline's visible reasoning string alias, but never
        # discard structured state hidden behind a reasoning_content delta.
        if delta.get("reasoning") is not None and not isinstance(delta["reasoning"], str):
            _fail("Unsupported upstream reasoning state.")
        events = self.life.start(usage)
        reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("reasoning")
        if reasoning is not None:
            events.extend(self.visible([("thinking", _string(reasoning))]))
        if delta.get("content") is not None:
            events.extend(self.visible(self.inline.push(_string(delta["content"]))))
        if delta.get("refusal") is not None:
            events.extend(self.flush())
            events.extend(self.visible([("text", _string(delta["refusal"]))]))
        calls = _array(delta.get("tool_calls") or [])
        if delta.get("function_call") is not None:
            if calls:
                _fail("Conflicting upstream tool call formats.")
            legacy = _object(delta["function_call"])
            calls = [{"index": 0, "id": legacy.get("id"), "function": legacy}]
        if calls:
            events.extend(self.flush())
            if self.current:
                events.extend(self.life.close(self.current))
                self.current = None
            for call in calls:
                call = _object(call)
                if call.get("type", "function") != "function":
                    _fail("Unsupported upstream tool call.")
                index = _index(call.get("index"))
                block = self.tools.setdefault(index, _Block("tool_use"))
                function = _object(call.get("function") or {})
                block.identify(call.get("id"), function.get("name"))
                if function.get("arguments") is not None:
                    block.append(function["arguments"])
                events.extend(self.life.emit(block))
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish not in ("stop", "length", "tool_calls", "function_call", "content_filter"):
                _fail("Unsupported upstream Chat finish reason.")
            if self.finish_reason is None:
                self.finish_reason = finish
            events.extend(self.flush())
        return events

    def finish(self):
        if self.finish_reason is None:
            _fail("Chat stream ended without a finish reason.")
        events = self.flush()
        call_ids = set()
        for block in self.tools.values():
            _validate_tool(block)
            if block.metadata["id"] in call_ids:
                _fail("Upstream reused a tool call identity.")
            call_ids.add(block.metadata["id"])
            events.extend(self.life.emit(block))
        if self.finish_reason in ("tool_calls", "function_call") and not self.tools:
            _fail("Upstream tool finish reason has no tool call.")
        if not self.life.blocks and self.finish_reason not in ("length", "content_filter"):
            _fail("Upstream Chat stream has no supported content.")
        stop = ("max_tokens" if self.finish_reason == "length" else
                "tool_use" if self.tools else "end_turn")
        return events + self.life.finish(stop, self.usage_converter(self.usage, "chat"))


def _validate_tool(block):
    if not all(block.metadata.get(key) for key in ("id", "name")):
        _fail("Upstream tool call is missing its identity.")
    # Never substitute {} for missing/broken streamed arguments.
    if not block.text.strip():
        _fail("Upstream tool call is missing its arguments.")
    _object(_loads(block.text))


class _Item:
    def __init__(self, kind):
        self.kind = kind
        self.keys = set()
        self.parts = {}
        self.block = _Block("tool_use" if kind == "function_call" else "thinking")
        self.summary = {}
        self.last_summary = -1
        self.signed_item = None


class _Responses:
    def __init__(self, model, scope, usage_converter, converter):
        self.life = _Lifecycle(model)
        self.scope = scope
        self.usage_converter = usage_converter
        self.converter = converter
        self.usage = {}
        self.items = []
        self.by_key = {}

    def item(self, data, kind, item=None):
        item = item or {}
        keys = []
        if "output_index" in data:
            keys.append(("index", _index(data["output_index"])))
        for value in (data.get("item_id"), item.get("id")):
            if value is not None:
                value = _string(value)
                if value:
                    keys.append(("id", value))
        for value in (data.get("call_id"), item.get("call_id")):
            if value is not None:
                value = _string(value)
                if value:
                    keys.append(("call", value))
        matches = {self.by_key[key] for key in keys if key in self.by_key}
        if len(matches) > 1:
            _fail("Ambiguous upstream output item identity.")
        if matches:
            result = matches.pop()
        else:
            candidates = [entry for entry in self.items if entry.kind == kind
                          and (not keys or not entry.keys)]
            if len(candidates) > 1:
                _fail("Ambiguous unkeyed upstream output item.")
            result = candidates[0] if candidates else _Item(kind)
        if result.kind != kind:
            _fail("Upstream changed its output item type.")
        # Each item has at most one identity in each namespace. An alias hit
        # may fill a previously unknown identity, never replace an existing one
        # (notably, a reused call_id must not merge two id/output_index pairs).
        # This also checks agreement between event-level and nested item IDs.
        identities = dict(result.keys)
        for namespace, value in keys:
            if namespace in identities and identities[namespace] != value:
                _fail("Conflicting upstream output item identity.")
            identities[namespace] = value
        if result not in self.items:
            self.items.append(result)
        for key in keys:
            self.by_key[key] = result
        result.keys.update(keys)
        return result

    def text(self, data, value, snapshot=False):
        item = self.item(data, "message")
        index = _index(data.get("content_index", 0))
        block = item.parts.setdefault(index, _Block("text"))
        block.append(value, snapshot)
        return self.life.emit(block)

    def reasoning(self, data, value, snapshot=False):
        item = self.item(data, "reasoning")
        index = _index(data.get("summary_index", data.get("content_index", 0)))
        part = item.summary.setdefault(index, _Block("thinking"))
        before = len(part.chunks)
        part.append(value, snapshot)
        if len(part.chunks) == before:
            return []
        if index < item.last_summary:
            _fail("Upstream interleaved reasoning summary parts.")
        item.last_summary = index
        item.block.append(part.chunks[-1])
        return self.life.emit(item.block)

    def part(self, data, part):
        part = _object(part)
        kind = part.get("type")
        if kind not in ("output_text", "text", "refusal"):
            _fail("Unsupported upstream Responses content part.")
        field = "refusal" if kind == "refusal" else "text"
        return self.text(data, part.get(field, ""), snapshot=True)

    def snapshot(self, data, value, complete):
        value = _object(value)
        if value.get("error") is not None:
            _fail("Upstream returned an error output item.")
        kind = value.get("type")
        if kind not in ("message", "function_call", "reasoning"):
            _fail("Unsupported upstream Responses output item.")
        if complete and value.get("status", "completed") not in ("completed", "incomplete"):
            _fail("Upstream output item is failed or nonterminal.")
        item = self.item(data, kind, value)
        if kind == "message":
            if value.get("role", "assistant") != "assistant":
                _fail()
            events = []
            for index, part in enumerate(_array(value.get("content", []))):
                events.extend(self.part({**data, "item_id": value.get("id", data.get("item_id")),
                                         "content_index": index}, part))
            return events
        if kind == "function_call":
            block = item.block
            block.identify(value.get("call_id"), value.get("name"))
            if value.get("arguments") is not None:
                arguments = _string(value["arguments"])
                if arguments:
                    block.append(arguments, snapshot=True)
            # Empty added snapshots carry metadata, not final arguments.
            return self.life.emit(block)
        if not complete:
            # Only a complete original item may be signed by the JSON bridge.
            events = []
            for index, part in enumerate(_array(value.get("summary", []))):
                part = _object(part)
                if part.get("type") not in ("summary_text", "reasoning_text"):
                    _fail("Unsupported upstream reasoning summary.")
                events.extend(self.reasoning(
                    {**data, "item_id": value.get("id", data.get("item_id")),
                     "summary_index": index}, part.get("text", ""), snapshot=True))
            return events
        if item.signed_item is not None:
            # Include unknown/nested fields without Python's true == 1 coercion.
            if (_dump(item.signed_item, sort_keys=True)
                    != _dump(value, sort_keys=True)):
                _fail("Upstream changed an already signed reasoning item.")
            return []
        message = self.converter({
            "id": self.life.id, "model": self.life.model,
            "status": "completed", "output": [value],
        }, "responses", model=self.life.model, state_scope=self.scope)
        converted = _array(_object(message).get("content"))
        if len(converted) != 1:
            _fail("Invalid reasoning conversion result.")
        converted = _object(converted[0])
        block = item.block
        if converted.get("type") == "thinking":
            block.append(converted["thinking"], snapshot=True)
            events = self.life.emit(block)
            events.extend(self.life.signature(block, converted["signature"]))
        elif converted.get("type") == "redacted_thinking":
            if block.text or block.index is not None:
                _fail("Upstream reasoning snapshot lost streamed summary text.")
            block.kind = "redacted_thinking"
            block.metadata = {"data": _string(converted["data"])}
            events = self.life.emit(block)
        else:
            _fail("Invalid reasoning conversion result.")
        item.signed_item = copy.deepcopy(value)
        events.extend(self.life.close(block))
        return events

    def feed(self, event, data):
        name = event or data.get("type", "")
        if name in ("response.created", "response.in_progress", "response.queued"):
            response = _object(data.get("response", data))
            self.life.metadata(response)
            if response.get("error") is not None or response.get("status") in ("failed", "cancelled"):
                _fail("Upstream returned a failed response.")
            if response.get("usage") is not None:
                _merge_usage(self.usage, response["usage"])
            return self.life.start(self.usage_converter(self.usage, "responses")), False
        if name in ("response.completed", "response.incomplete"):
            return self.finish(name, _object(data.get("response", data))), True
        if name in ("response.failed", "response.error", "response.cancelled", "error"):
            _fail("Upstream returned a stream error.")
        if name in ("ping", "response.output_text.annotation.added"):
            return [], False
        if name in ("response.output_item.added", "response.output_item.done"):
            return self.snapshot(data, data.get("item"), name.endswith(".done")), False
        if name in ("response.content_part.added", "response.content_part.done"):
            return self.part(data, data.get("part")), False
        if name in ("response.output_text.delta", "response.refusal.delta"):
            return self.text(data, data.get("delta")), False
        if name in ("response.output_text.done", "response.refusal.done"):
            field = "refusal" if "refusal" in name else "text"
            return self.text(data, data.get(field, ""), snapshot=True), False
        if name in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            if name.endswith(".done") and data.get("item") is not None:
                value = _object(data["item"])
                if value.get("type") != "function_call":
                    _fail("Unsupported upstream tool argument snapshot.")
                return self.snapshot(data, value, True), False
            item = self.item(data, "function_call")
            item.block.identify(data.get("call_id"), data.get("name"))
            field = "arguments" if name.endswith(".done") else "delta"
            if data.get(field) is not None:
                arguments = _string(data[field])
                if arguments:
                    item.block.append(arguments, snapshot=name.endswith(".done"))
            return self.life.emit(item.block), False
        if name in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta",
                    "response.reasoning.delta"):
            return self.reasoning(data, data.get("delta", data.get("text"))), False
        if name in ("response.reasoning_summary_text.done", "response.reasoning_text.done"):
            return self.reasoning(data, data.get("text", ""), snapshot=True), False
        if name in ("response.reasoning_summary_part.added", "response.reasoning_summary_part.done"):
            part = _object(data.get("part"))
            if part.get("type") not in ("summary_text", "reasoning_text"):
                _fail("Unsupported upstream reasoning summary.")
            return self.reasoning(data, part.get("text", ""), snapshot=True), False
        if name == "response.reasoning.done":
            if data.get("item") is not None:
                return self.snapshot(data, data["item"], True), False
            # A text-level done is not the full opaque reasoning item.
            return [], False
        _fail("Unsupported upstream Responses event.")

    def finish(self, event, response):
        self.life.metadata(response)
        status = response.get("status", "incomplete" if event.endswith(".incomplete") else "completed")
        if status not in ("completed", "incomplete") or response.get("error") is not None:
            _fail("Upstream returned a failed or nonterminal response.")
        if response.get("usage") is not None:
            _merge_usage(self.usage, response["usage"])
        events = []
        for index, value in enumerate(_array(response.get("output", []))):
            events.extend(self.snapshot({"output_index": index}, value, True))
        tools = [item.block for item in self.items if item.kind == "function_call"]
        for block in tools:
            _validate_tool(block)
            events.extend(self.life.emit(block))
        if any(item.kind == "reasoning" and item.signed_item is None for item in self.items):
            _fail("Responses stream ended without a complete reasoning item.")
        if status == "incomplete":
            details = _object(response.get("incomplete_details") or {})
            reason = details.get("reason")
            if reason is not None:
                _string(reason)
            stop = "max_tokens" if reason in (None, "max_tokens", "max_output_tokens") else "end_turn"
        else:
            if not self.life.blocks:
                _fail("Completed Responses stream has no supported content.")
            stop = "tool_use" if tools else "end_turn"
        return events + self.life.finish(stop, self.usage_converter(self.usage, "responses"))


def stream_to_messages(lines, mode, *, model="", state_scope=""):
    """Yield ``(event_name, payload)`` from bytes/str requests.iter_lines.

    SSE failures yield one sanitized error and stop, without successful terminal
    events or replay. A raw JSON document is accepted only before SSE begins.
    No network access, retries, truncation, or transport exception handling.
    """
    from proxy_claude import BridgeError, upstream_to_messages, usage_to_messages

    adapter = None
    try:
        if mode not in ("chat", "responses"):
            _fail("Bridge mode must be chat or responses.")
        _string(model)
        _string(state_scope)
        adapter = (_Chat(model, state_scope, usage_to_messages) if mode == "chat"
                   else _Responses(model, state_scope, usage_to_messages, upstream_to_messages))
        for event, data in _frames(lines):
            if event == "error" or data.get("error") is not None or data.get("type") == "error":
                if mode == "chat":
                    yield from adapter.flush()
                yield _error("Upstream returned a stream error.", "upstream_stream_error")
                return
            if event == "json":
                yield from message_to_events(upstream_to_messages(
                    data, mode, model=model, state_scope=state_scope))
                return
            if event == "[DONE]":
                if mode != "chat":
                    _fail("Responses stream ended without a terminal response event.")
                yield from adapter.finish()
                return
            if mode == "chat":
                # Some gateways use SSE framing around a single JSON completion.
                choices = data.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    if "message" in choices[0]:
                        if adapter.life.started:
                            _fail("Cannot replay JSON after streamed output.")
                        yield from message_to_events(upstream_to_messages(
                            data, mode, model=model, state_scope=state_scope))
                        return
                yield from adapter.feed(event, data)
            else:
                events, terminal = adapter.feed(event, data)
                yield from events
                if terminal:
                    return
        if mode == "chat":
            if adapter.finish_reason is not None:
                # Match streaming.rs:754-775: a pending valid finish may be
                # finalized at normal EOF, but never before late data/errors.
                yield from adapter.finish()
                return
            yield from adapter.flush()
        yield _error("Upstream stream ended before its terminal marker.", "upstream_stream_truncated")
    except _Failure as exc:
        if isinstance(adapter, _Chat):
            yield from adapter.flush()
        yield _error(str(exc), exc.code)
    except BridgeError as exc:
        # The JSON bridge's public error is explicitly safe and serializable.
        yield "error", exc.error
