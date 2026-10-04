"""Shared validation for explicit per-model Codex capabilities."""

from __future__ import annotations

from typing import Any


CODEX_MODEL_CAPABILITY_FLAGS = (
    "supports_parallel_tool_calls",
    "supports_image_detail_original",
    "supports_search_tool",
)
CODEX_REASONING_LEVEL_DESCRIPTIONS = {
    "none": "Disable Thinking",
    "minimal": "Minimal reasoning",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balanced speed and reasoning",
    "high": "Greater reasoning depth",
    "xhigh": "Extra high reasoning depth",
    "max": "Maximum reasoning depth",
    "ultra": "Ultra reasoning depth",
}


def resolve_capability_reasoning_effort(provider: dict, meta: dict) -> str:
    """Resolve only an explicitly bounded default, including legacy aliases."""
    def configured(value):
        effort = value.get("reasoning_effort") or value.get("reasoning")
        if isinstance(effort, dict):
            effort = effort.get("effort")
        return effort.strip() if isinstance(effort, str) else ""

    effort = configured(meta) or configured(provider) or "medium"
    levels = meta.get("capabilities", {}).get("supported_reasoning_levels", [])
    return effort if effort in levels or not levels else levels[0]


def normalize_model_capabilities(value: Any, label: str) -> dict[str, Any]:
    """Validate explicit overrides; preserve unknown metadata, never infer capabilities."""
    label = f"{label} capabilities"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    capabilities = dict(value)
    for field in CODEX_MODEL_CAPABILITY_FLAGS:
        if field in capabilities and not isinstance(capabilities[field], bool):
            raise ValueError(f"{label}.{field} must be a boolean (true or false)")
    for field, allowed in (
        ("input_modalities", ("text", "image")),
        ("supported_reasoning_levels", tuple(CODEX_REASONING_LEVEL_DESCRIPTIONS)),
    ):
        if field not in capabilities:
            continue
        items = capabilities[field]
        if not isinstance(items, list) or not items:
            raise ValueError(f"{label}.{field} must be a non-empty array of: {', '.join(allowed)}")
        normalized = []
        for index, item in enumerate(items):
            if not isinstance(item, str) or item.strip() not in allowed:
                raise ValueError(f"{label}.{field}[{index}] must be one of: {', '.join(allowed)}")
            item = item.strip()
            if item not in normalized:
                normalized.append(item)
        # Declaration order controls the fallback default; do not sort.
        capabilities[field] = normalized
    return capabilities
