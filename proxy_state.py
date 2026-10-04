"""Versioned protocol-state envelopes. Base64 is transport encoding, not encryption."""
import base64
import copy
import hashlib
import json


ANTHROPIC_THINKING_PREFIX = "ai-api-anthropic-thinking-v1:"
ANTHROPIC_THINKING_FAMILY = "ai-api-anthropic-thinking-"


class ProtocolStateError(ValueError):
    code = "incompatible_conversation_state"


def anthropic_state_scope(provider: dict, model: str, headers: dict | None = None) -> str:
    """Prevent accidental replay into a different upstream/account/model.

    The fingerprint exposes no URL or credential. It is provenance, not a
    cryptographic authenticity claim; the upstream validates its own signature.
    """
    if headers is None:
        headers = {str(key).lower(): str(value) for key, value in provider.get("headers", {}).items()}
        key = str(provider.get("api_key") or "")
        mode = provider.get("auth_mode", "bearer")
        if key:
            headers.pop("authorization", None)
            headers.pop("x-api-key", None)
            headers["x-api-key" if mode == "anthropic" else "authorization"] = key if mode == "anthropic" else f"Bearer {key}"
        elif mode == "anthropic":
            headers.pop("authorization", None)
    # requests prepares headers case-insensitively, with the last spelling
    # winning. Fingerprint that effective view, not a sorted multiset.
    effective = {str(key).lower(): str(value) for key, value in headers.items()}
    identity = sorted(
        (key, value) for key, value in effective.items()
        if key in {
            "authorization", "x-api-key", "api-key", "cookie",
            "openai-organization", "openai-project", "chatgpt-account-id",
        }
    )
    configured_names = {str(key).lower() for key in provider.get("headers", {})}
    configured_identity = sorted((key, effective[key]) for key in configured_names if key in effective)
    fields = [provider.get("base_url", ""), provider.get("custom_endpoint", ""),
              provider.get("auth_mode", ""), model, identity, configured_identity]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def _valid_thinking(block) -> bool:
    return isinstance(block, dict) and (
        (block.get("type") == "thinking" and isinstance(block.get("thinking"), str)
         and isinstance(block.get("signature"), str) and bool(block["signature"]))
        or (block.get("type") == "redacted_thinking"
            and isinstance(block.get("data"), str) and bool(block["data"]))
    )


def encode_anthropic_thinking(block: dict, scope: str = "") -> str | None:
    if not _valid_thinking(block):
        return None
    raw = json.dumps({"scope": scope, "block": block}, ensure_ascii=False,
                     separators=(",", ":")).encode()
    return ANTHROPIC_THINKING_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_anthropic_thinking(value, scope: str = "") -> dict | None:
    if not isinstance(value, str) or not value.startswith(ANTHROPIC_THINKING_FAMILY):
        return None
    if not value.startswith(ANTHROPIC_THINKING_PREFIX):
        raise ProtocolStateError("Unsupported saved Anthropic thinking version; resend readable history.")
    encoded = value[len(ANTHROPIC_THINKING_PREFIX):]
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or not _valid_thinking(envelope.get("block")):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ProtocolStateError("Invalid saved Anthropic thinking state; start a new turn without it.") from None
    if envelope.get("scope") != scope:
        raise ProtocolStateError("Saved thinking belongs to a different upstream, account, or model.")
    return copy.deepcopy(envelope["block"])


def reasoning_item_from_anthropic(block: dict, item_id: str, scope: str = "") -> dict:
    thinking = block.get("thinking", "") if block.get("type") == "thinking" else ""
    item = {
        "id": item_id, "type": "reasoning", "status": "completed",
        "summary": [{"type": "summary_text", "text": thinking}] if thinking else [],
    }
    encoded = encode_anthropic_thinking(block, scope)
    if encoded:
        item["encrypted_content"] = encoded
    return item
