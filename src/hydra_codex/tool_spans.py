"""Storage adapter for normalized rollout tool spans."""

from __future__ import annotations

from typing import Any

from .tool_normalization import JoinedToolSpan, ToolSpanJoin

_CATEGORIES = frozenset({"instrumentation", "opaque_exec", "tool", "web"})
_TOOL_NAMES = frozenset({
    "apply_patch", "custom_exec", "exec_command", "function", "hydra", "mcp",
    "nested_exec", "patch", "view_image", "web",
})
_TERMINAL_STATES = frozenset({"unknown", "success", "failed"})
_PROVENANCE = frozenset({"exact", "lower_bound"})


def _validate(category: str, tool_name: str, terminal_state: str = "unknown", provenance: str = "exact") -> None:
    if category not in _CATEGORIES or tool_name not in _TOOL_NAMES or terminal_state not in _TERMINAL_STATES or provenance not in _PROVENANCE:
        raise ValueError("unsupported normalized tool span value")


def persist_tool_start(
    connection: Any, *, session_key: str, call_key: str, category: str, tool_name: str,
    started_at: str | None, turn_key: str | None, source_digest: str, source_ordinal: int, provenance: str = "exact",
) -> None:
    """Record a safe start fact without overwriting a terminal result."""
    _validate(category, tool_name, provenance=provenance)
    row = connection.execute(
        "SELECT tool_name, started_at, finished_at, terminal_state, completeness FROM tool_spans WHERE session_key = ? AND call_key = ?",
        (session_key, call_key),
    ).fetchone()
    if row is None:
        connection.execute(
            """INSERT INTO tool_spans(session_key, call_key, category, terminal_state, latency_ms, tool_name,
               started_at, finished_at, turn_key, source_digest, source_ordinal, completeness, provenance)
               VALUES (?, ?, ?, 'unknown', NULL, ?, ?, NULL, ?, ?, ?, 'incomplete', ?)""",
            (session_key, call_key, category, tool_name, started_at, turn_key, source_digest, source_ordinal, provenance),
        )
        return
    span = JoinedToolSpan(call_key, row[0], row[1], row[2], row[3], row[4])
    if span.finished_at is not None:
        span = ToolSpanJoin().start_after_end(span, tool_name, started_at or "")
    connection.execute(
        """UPDATE tool_spans SET category = ?, tool_name = ?, started_at = COALESCE(started_at, ?),
           turn_key = COALESCE(turn_key, ?), completeness = CASE WHEN finished_at IS NULL THEN completeness ELSE 'complete' END
           WHERE session_key = ? AND call_key = ?""",
        (category, span.name or tool_name, span.started_at or started_at, turn_key, session_key, call_key),
    )


def persist_tool_end(
    connection: Any, *, session_key: str, call_key: str, category: str, tool_name: str,
    finished_at: str | None, terminal_state: str, latency_ms: int | None, turn_key: str | None,
    source_digest: str, source_ordinal: int, provenance: str = "exact",
) -> None:
    """Record a completion fact, making an end-only span explicit and monotonic."""
    _validate(category, tool_name, terminal_state, provenance)
    row = connection.execute(
        "SELECT tool_name, started_at, finished_at, terminal_state, completeness FROM tool_spans WHERE session_key = ? AND call_key = ?",
        (session_key, call_key),
    ).fetchone()
    if row is None:
        span = ToolSpanJoin().end(call_key, finished_at or "", terminal_state)
        connection.execute(
            """INSERT INTO tool_spans(session_key, call_key, category, terminal_state, latency_ms, tool_name,
               started_at, finished_at, turn_key, source_digest, source_ordinal, completeness, provenance)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
            (session_key, call_key, category, span.terminal_state, latency_ms, tool_name, finished_at,
             turn_key, source_digest, source_ordinal, span.completeness, provenance),
        )
        return
    span = JoinedToolSpan(call_key, row[0], row[1], row[2], row[3], row[4])
    terminal = terminal_state if span.terminal_state == "unknown" else span.terminal_state
    connection.execute(
        """UPDATE tool_spans SET terminal_state = ?, latency_ms = COALESCE(latency_ms, ?),
           finished_at = COALESCE(finished_at, ?), turn_key = COALESCE(turn_key, ?),
           completeness = CASE WHEN started_at IS NULL THEN 'incomplete' ELSE 'complete' END
           WHERE session_key = ? AND call_key = ?""",
        (terminal, latency_ms, finished_at, turn_key, session_key, call_key),
    )
