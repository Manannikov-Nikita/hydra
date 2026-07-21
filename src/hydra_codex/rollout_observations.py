"""Small in-memory normalizers for safe rollout observations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def fingerprint(value: Any, digest: Callable[[str, str], str]) -> str:
    if not isinstance(value, dict):
        return type(value).__name__
    shape = ",".join(f"{key}:{type(item).__name__}" for key, item in sorted(value.items()))
    return "shape/" + digest("diagnostic", shape)[:32]


def path_key(value: Any, project_root: Path, digest: Callable[[str, str], str]) -> str:
    if not isinstance(value, str):
        return "unknown"
    candidate = Path(value)
    if not candidate.is_absolute() and ".." not in candidate.parts:
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return "external/" + digest("path", value)[:20]


def usage(payload: dict[str, Any]) -> dict[str, int] | None:
    info = payload.get("info")
    values = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(values, dict):
        return None
    input_tokens = safe_int(values.get("input_tokens"))
    cached_input_tokens = safe_int(values.get("cached_input_tokens"))
    if (
        input_tokens is not None
        and cached_input_tokens is not None
        and cached_input_tokens > input_tokens
    ):
        return None
    return {
        "input": input_tokens, "cached": cached_input_tokens,
        "output": safe_int(values.get("output_tokens")), "reasoning": safe_int(values.get("reasoning_output_tokens")),
        "cache_write": safe_int(values.get("cache_write_input_tokens")), "vendor_total": safe_int(values.get("total_tokens")),
        "context_window": safe_int(info.get("model_context_window")),
        "complete": int(all(safe_int(values.get(field)) is not None for field in (
            "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
        ))),
    }


def parse_arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
