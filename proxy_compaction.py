"""Pure, atomic Codex compaction helpers (no HTTP, streaming, or provider state).

Source comparison: cc-switch 372b1698968ced988ce046d705732cb799000533,
src-tauri/src/proxy/providers/{codex_compaction,transform_codex_chat,
transform_codex_anthropic}.rs. The prompt and replay prefix below are verbatim.
Unlike that bridge's opaque-state placeholder, unreadable history is an error.

Integration:
* Gate summary turns with is_compaction_request(original_body, route_path).
  Save the original downstream stream flag before preparing the request.
* Call prepare_compaction_request only for a detected summary turn, including
  /responses/compact without a trigger. Send its result to regular /responses
  (or the existing Chat/Messages converter), never to /responses/compact.
* For ordinary history, call replay_compaction_items instead. native=True is
  only for native Responses: foreign ciphertext/unknown packets stay intact.
* In a summary turn, build/pass an EMPTY conversion tool context: the existing
  converters can otherwise rediscover tools inside historical additional_tools
  or tool_search_output packets. Those history packets are not deleted here.
* Before converting a Chat/Messages reply, call validate_compaction_finish_reason
  with its original choice.finish_reason / stop_reason. Ordinary converters
  can lose abnormal stop reasons while marking a reply completed.
* After a full successful upstream response has been converted to Responses,
  call compact_response. Only then may the parent emit downstream JSON/SSE.
  ProtocolStateError maps to HTTP 400; CompactionError maps to HTTP 502.

The encrypted_content field is a protocol slot, NOT an encryption claim.
Its value is a version prefix plus unpadded base64url of the UTF-8 summary,
without JSON, signatures, account bindings, or provider/model scope.
"""

import base64
import copy
import re
import uuid

from proxy_state import ProtocolStateError


COMPACTION_PREFIX = "ai-api-compaction-v1:"
_COMPACTION_FAMILY_PREFIX = "ai-api-compaction-"
_COMPACTION_TYPES = ("compaction", "compaction_summary", "context_compaction")

COMPACT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work.
"""

SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary "
    "of its thinking process. You also have access to the state of the tools that "
    "were used by that language model. Use this to build on the work that has "
    "already been done and avoid duplicating work. Here is the summary produced "
    "by the other language model, use the information in this summary to assist "
    "with your own analysis:"
)


class CompactionError(ValueError):
    """Safe failure: do not let an unfinished summary replace conversation history."""

    status_code = 502
    code = "compaction_failed"
    message = (
        "Upstream did not return a complete, nonempty compaction summary. "
        "Conversation history must not be replaced."
    )

    def __init__(self):
        super().__init__(self.message)

    @property
    def error(self) -> dict:
        return {"type": "api_error", "code": self.code, "message": self.message}


class _CompactionStateError(ProtocolStateError):
    status_code = 400

    @property
    def message(self) -> str:
        return str(self)


def encode_compaction_summary(summary: str) -> str:
    """Encode portable text, not encrypted or authenticated provider state."""
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("A compaction summary must be nonempty text.")
    return COMPACTION_PREFIX + base64.urlsafe_b64encode(
        summary.encode("utf-8")
    ).decode("ascii").rstrip("=")


def decode_compaction_summary(value) -> str | None:
    """Return None for foreign state; reject malformed/unsupported own state."""
    if not isinstance(value, str) or not value.startswith(_COMPACTION_FAMILY_PREFIX):
        return None
    try:
        if not value.startswith(COMPACTION_PREFIX):
            raise ValueError()
        encoded = value[len(COMPACTION_PREFIX):]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
            raise ValueError()
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        # Reject noncanonical pad bits as well as invalid UTF-8/empty summaries.
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
            raise ValueError()
        summary = raw.decode("utf-8")
        if not summary.strip():
            raise ValueError()
    except (ValueError, UnicodeError):
        raise _CompactionStateError(
            "Invalid or unsupported saved compaction summary; resend readable history."
        ) from None
    return summary


def _is_trigger(item) -> bool:
    return isinstance(item, dict) and item.get("type") == "compaction_trigger"


def is_compaction_request(body, route_path: str = "/responses") -> bool:
    """Recognize only Responses routes; message text is never a trigger."""
    path = route_path.split("?", 1)[0].rstrip("/")
    if path == "/responses/compact":
        return True
    if path != "/responses" or not isinstance(body, dict):
        return False
    items = body.get("input")
    if isinstance(items, list):
        return any(_is_trigger(item) for item in items)
    return _is_trigger(items)


def _user_message(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def replay_compaction_items(body: dict, *, native: bool = False) -> dict:
    """Deep-copy a request and decode only our compaction input items.

    Other packets (including reasoning, tools and compaction_trigger) are not
    changed. Opaque/missing compaction payloads cannot cross a protocol bridge.
    native=True preserves them verbatim, but never bypasses own-state validation.
    This helper does not bind a readable summary to any provider or model.
    """
    result = copy.deepcopy(body)

    def replay(item):
        if not isinstance(item, dict) or item.get("type") not in _COMPACTION_TYPES:
            return item
        summary = decode_compaction_summary(item.get("encrypted_content"))
        if summary is not None:
            return _user_message(f"{SUMMARY_PREFIX}\n{summary}")
        if native:
            return item
        raise _CompactionStateError(
            "Saved compaction state cannot be read by this protocol bridge; "
            "use its native Responses provider or resend readable history."
        )

    items = result.get("input")
    if isinstance(items, list):
        result["input"] = [replay(item) for item in items]
    elif isinstance(items, dict):
        result["input"] = replay(items)
    return result


def prepare_compaction_request(body: dict, *, native: bool = False) -> dict:
    """Prepare one explicitly selected, atomic summary turn, without mutation.

    Call only after detection: a trigger-free body is valid on /responses/compact.
    Preserve all other settings and history, including caller-supplied token
    limits. All triggers are removed and ONE official prompt is appended last.
    """
    result = replay_compaction_items(body, native=native)
    items = result.get("input")
    if items is None:
        items = []
    elif isinstance(items, str):
        items = [_user_message(items)]
    elif isinstance(items, dict):
        items = [items]
    elif not isinstance(items, list):
        raise _CompactionStateError("Invalid compaction input; resend readable history.")
    result["input"] = [item for item in items if not _is_trigger(item)]
    result["input"].append(_user_message(COMPACT_PROMPT))
    for key in ("tools", "tool_choice", "parallel_tool_calls"):
        result.pop(key, None)
    result["stream"] = False
    return result


def validate_compaction_finish_reason(reason) -> None:
    """Require a normal bridge stop BEFORE its original reason is discarded.

    Native Responses instead uses its explicit completed status. Missing or
    unknown bridge stop reasons are not proof that the summary finished.
    """
    if not isinstance(reason, str) or reason.strip().lower() not in (
        "stop", "end_turn", "stop_sequence",
    ):
        raise CompactionError()


def validate_compaction_source(payload, mode: str) -> None:
    """Validate raw success markers before ordinary converters can erase them."""
    def text_blocks(content, *, anthropic=False):
        if not anthropic and isinstance(content, str):
            return
        if not isinstance(content, list):
            raise CompactionError()
        for block in content:
            if not isinstance(block, dict) or block.get("error") is not None:
                raise CompactionError()
            kind = block.get("type")
            if kind == "text" or (not anthropic and kind == "output_text"):
                if not isinstance(block.get("text"), str):
                    raise CompactionError()
            elif anthropic and kind == "thinking":
                if not isinstance(block.get("thinking"), str):
                    raise CompactionError()
            elif anthropic and kind == "redacted_thinking":
                if not isinstance(block.get("data"), str):
                    raise CompactionError()
            else:
                # Error/refusal/tool/media blocks are never a completed summary,
                # even if a permissive normal converter extracts their text.
                raise CompactionError()

    if not isinstance(payload, dict) or payload.get("error") is not None:
        raise CompactionError()
    if payload.get("type") == "error" or payload.get("status") not in (None, "completed"):
        raise CompactionError()
    if mode == "messages":
        validate_compaction_finish_reason(payload.get("stop_reason"))
        if payload.get("role", "assistant") != "assistant":
            raise CompactionError()
        text_blocks(payload.get("content"), anthropic=True)
    else:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise CompactionError()
        choice = choices[0]
        message = choice.get("message")
        if choice.get("error") is not None or not isinstance(message, dict) or message.get("error") is not None:
            raise CompactionError()
        if message.get("role", "assistant") != "assistant" or message.get("refusal"):
            raise CompactionError()
        text_blocks(message.get("content"))
        validate_compaction_finish_reason(choice.get("finish_reason"))


def _summary_from_completed_response(response) -> str:
    if (
        not isinstance(response, dict)
        or response.get("object") != "response"
        or response.get("status") != "completed"
        or response.get("error") is not None
        or response.get("incomplete_details") is not None
    ):
        raise CompactionError()
    for key in ("finish_reason", "stop_reason"):
        if key in response:
            validate_compaction_finish_reason(response[key])
    output = response.get("output")
    if not isinstance(output, list):
        raise CompactionError()
    parts = []
    for item in output:
        if not isinstance(item, dict) or item.get("status", "completed") != "completed":
            raise CompactionError()
        item_type = item.get("type")
        if item_type == "reasoning":
            continue
        # A tool call (even alongside text) is not a finished summary turn.
        if item_type != "message":
            raise CompactionError()
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise CompactionError()
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise CompactionError()
            text = part.get("text")
            if not isinstance(text, str):
                raise CompactionError()
            # Commentary/analysis is not final assistant text.
            if item.get("phase") in (None, "final_answer") and text.strip():
                parts.append(text.strip())
    summary = "\n\n".join(parts)
    if not summary:
        raise CompactionError()
    return summary


def compact_response(converted_response: dict, compact_endpoint: bool = False) -> dict:
    """Return exactly one compaction item, or fail without modifying the input.

    Accept a full, successful Responses object, not raw Chat/Messages or SSE
    deltas. Ignore output_text shortcuts: only final assistant output is trusted.
    Preserve supplied id/model/created_at/usage provenance without inventing it.
    """
    summary = _summary_from_completed_response(converted_response)
    try:
        encoded = encode_compaction_summary(summary)
    except (ValueError, UnicodeError):
        raise CompactionError() from None
    result = {
        key: copy.deepcopy(converted_response[key])
        for key in ("id", "model", "created_at", "usage")
        if key in converted_response
    }
    result["object"] = "response.compaction" if compact_endpoint else "response"
    if not compact_endpoint:
        result["status"] = "completed"
    result["output"] = [{
        "type": "compaction",
        "id": f"cmp_{uuid.uuid4().hex}",
        "encrypted_content": encoded,
    }]
    return result
