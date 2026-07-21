"""Fail-closed privacy primitives for the versioned rollout adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


LOCATION_TYPES = frozenset({"active", "archived", "explicit"})
EXTRACTED_ENVELOPES = frozenset({"session_meta", "turn_context", "event_msg", "response_item"})
IGNORED_ENVELOPES = frozenset({"world_state"})
KNOWN_ENVELOPES = EXTRACTED_ENVELOPES | IGNORED_ENVELOPES

EXTRACTED_EVENT_TYPES = frozenset({
    "token_count", "sub_agent_activity", "task_started", "task_complete", "turn_aborted",
    "mcp_tool_call_end", "patch_apply_end", "web_search_end",
})
IGNORED_EVENT_TYPES = frozenset({"agent_reasoning", "agent_message", "user_message"})
KNOWN_EVENT_TYPES = EXTRACTED_EVENT_TYPES | IGNORED_EVENT_TYPES

EXTRACTED_RESPONSE_TYPES = frozenset({
    "custom_tool_call", "custom_tool_call_output", "function_call", "function_call_output",
})
IGNORED_RESPONSE_TYPES = frozenset({"reasoning", "message"})
KNOWN_RESPONSE_TYPES = EXTRACTED_RESPONSE_TYPES | IGNORED_RESPONSE_TYPES
DIAGNOSTIC_KINDS = frozenset({
    "malformed", "unknown_envelope", "unknown_event_type", "unknown_response_type",
    "invalid_timestamp", "session_meta", "unresolved_project", "unrelated_project",
    "multiple_sessions", "token_count", "counter_reset", "sub_agent_activity",
    "turn_event", "out_of_order", "function_call", "source_truncate", "source_rewrite",
    "source_changed", "duplicate_turn_start", "invalid_turn_interval",
    "custom_exec_conditional", "custom_exec_dynamic", "custom_exec_dead_code",
    "custom_exec_unsupported", "custom_exec_unbalanced",
})


@dataclass(frozen=True)
class SafeTimestamp:
    text: str | None
    epoch: float | None
    quality: str


def nonempty_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def canonical_timestamp(value: Any) -> SafeTimestamp:
    if value is None:
        return SafeTimestamp(None, None, "missing")
    if not isinstance(value, str):
        return SafeTimestamp(None, None, "invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return SafeTimestamp(None, None, "invalid")
    if parsed.tzinfo is None:
        return SafeTimestamp(None, None, "invalid")
    utc = parsed.astimezone(timezone.utc)
    text = utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if text.endswith(".000000Z"):
        text = text.replace(".000000Z", "Z")
    return SafeTimestamp(text, utc.timestamp(), "valid")


def safe_diagnostic_kind(value: str) -> str:
    return value if value in DIAGNOSTIC_KINDS else "unknown_envelope"


def safe_envelope_kind(value: Any) -> str:
    return value if isinstance(value, str) and value in KNOWN_ENVELOPES else "unknown_envelope"
