"""Explicit, opt-in reasoning parameter mapping for Chat-compatible upstreams."""

THINKING_PARAMS = {"none", "thinking", "enable_thinking"}
EFFORT_PARAMS = {"none", "reasoning_effort", "reasoning.effort"}
DISABLED_EFFORTS = {"none", "off", "disabled"}


def normalize_chat_reasoning(value, label="chat_reasoning"):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) - {"thinking_param", "effort_param", "effort_map"}:
        raise ValueError(f"{label} contains an unsupported setting")
    result = dict(value)
    for key, allowed in (("thinking_param", THINKING_PARAMS), ("effort_param", EFFORT_PARAMS)):
        if key in result and (not isinstance(result[key], str) or result[key] not in allowed):
            raise ValueError(f"{label}.{key} must be one of: {', '.join(sorted(allowed))}")
    if "effort_map" in result:
        mapping = result["effort_map"]
        if not isinstance(mapping, dict) or any(
            not isinstance(key, str) or not key.strip()
            or not isinstance(item, str) or not item.strip()
            for key, item in mapping.items()
        ):
            raise ValueError(f"{label}.effort_map must map effort names to non-empty strings")
        result["effort_map"] = {key.strip().lower(): item.strip() for key, item in mapping.items()}
    return result


def apply_chat_reasoning(chat, effort, config=None, *, default_supports_effort=False):
    """No config preserves the existing model-gated reasoning_effort behavior."""
    config = normalize_chat_reasoning(config)
    if not isinstance(effort, str) or not effort.strip():
        return
    level = effort.strip()
    enabled = level.lower() not in DISABLED_EFFORTS
    thinking_param = config.get("thinking_param", "none")
    if thinking_param == "thinking":
        chat["thinking"] = {"type": "enabled" if enabled else "disabled"}
    elif thinking_param == "enable_thinking":
        chat["enable_thinking"] = enabled
    effort_param = config.get(
        "effort_param", "reasoning_effort" if default_supports_effort else "none",
    )
    if effort_param == "none":
        return
    # Preserve legacy omission of disabled effort unless explicitly mapped.
    mapping = config.get("effort_map", {})
    if not enabled and level.lower() not in mapping:
        return
    level = mapping.get(level.lower(), level)
    if effort_param == "reasoning_effort":
        chat["reasoning_effort"] = level
    else:
        chat["reasoning"] = {"effort": level}
