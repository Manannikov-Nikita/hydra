"""Order-independent persistence for normalized, privacy-safe tool spans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .rollout_privacy import canonical_timestamp
from .source_authority import rollout_revision_identity, source_rank


_CATEGORIES = frozenset({"instrumentation", "opaque_exec", "tool", "web"})
_TOOL_NAMES = frozenset({
    "apply_patch", "collaboration", "custom_exec", "exec_command", "file_read",
    "file_write", "function", "hydra", "mcp", "nested_exec", "patch",
    "read_mcp_resource", "unknown", "view_image", "web",
})
_TERMINAL_STATES = frozenset({"unknown", "success", "failed"})
_PROVENANCE = frozenset({"exact", "lower_bound", "estimated"})
_KIND_PRIORITY = {
    "legacy_values": 0,
    "legacy_description": 1,
    "end": 2,
    "start": 3,
}


def _validate(
    category: str,
    tool_name: str,
    terminal_state: str = "unknown",
    provenance: str = "exact",
) -> None:
    if (
        category not in _CATEGORIES
        or tool_name not in _TOOL_NAMES
        or terminal_state not in _TERMINAL_STATES
        or provenance not in _PROVENANCE
    ):
        raise ValueError("unsupported normalized tool span value")


@dataclass(frozen=True)
class _Candidate:
    kind: str
    category: str
    terminal_state: str
    latency_ms: int | None
    tool_name: str
    started_at: str | None
    finished_at: str | None
    turn_key: str | None
    source_digest: str
    source_ordinal: int
    provenance: str


_CANDIDATE_COLUMNS = (
    "candidate_kind,category,terminal_state,latency_ms,tool_name,started_at,"
    "finished_at,turn_key,source_digest,source_ordinal,provenance"
)


def _candidates(
    connection: Any, *, session_key: str, call_key: str,
) -> tuple[_Candidate, ...]:
    return tuple(
        _Candidate(*tuple(row))
        for row in connection.execute(
            f"SELECT {_CANDIDATE_COLUMNS} FROM tool_span_candidates "
            "WHERE session_key=? AND call_key=?",
            (session_key, call_key),
        )
    )


def _ranked(
    connection: Any,
    candidates: tuple[_Candidate, ...],
    predicate: Callable[[_Candidate], bool],
) -> tuple[tuple[_Candidate, tuple[int, str]], ...]:
    eligible = tuple(
        (candidate, source_rank(connection, candidate.source_digest))
        for candidate in candidates
        if predicate(candidate)
    )
    if not eligible:
        return ()
    authority = max(rank for _candidate, rank in eligible)
    return tuple(item for item in eligible if item[1] == authority)


def _description(
    connection: Any, candidates: tuple[_Candidate, ...],
) -> _Candidate:
    ranked = tuple(
        (
            candidate,
            source_rank(connection, candidate.source_digest),
            rollout_revision_identity(connection, candidate.source_digest),
        )
        for candidate in candidates
    )
    authority = max(rank[0] for _candidate, rank, _revision in ranked)
    eligible = tuple(
        item for item in ranked if item[1][0] == authority
    )
    kind_priority = max(_KIND_PRIORITY[item[0].kind] for item in eligible)
    eligible = tuple(
        item for item in eligible
        if _KIND_PRIORITY[item[0].kind] == kind_priority
    )

    # Re-reading an append-only rollout emits the prefix start again alongside
    # a newly observed generic output. Prefer the longest revision of that one
    # logical source; do not let opaque HMAC digest ordering define semantics.
    revisions = tuple(item[2] for item in eligible)
    logicals = {revision[0] for revision in revisions if revision is not None}
    if len(logicals) == 1 and all(revision is not None for revision in revisions):
        latest = max(revision[1] for revision in revisions if revision is not None)
        eligible = tuple(item for item in eligible if item[2][1] == latest)

    # Separate conflicting starts retain their stable privacy-safe digest tie
    # break; only revisions within one proven append lineage use line count.
    return max(
        eligible,
        key=lambda item: (
            item[1][1],
            -item[0].source_ordinal,
            item[0].category,
            item[0].tool_name,
        ),
    )[0]


def _timestamp(
    connection: Any,
    candidates: tuple[_Candidate, ...],
    field: str,
) -> str | None:
    def valid(candidate: _Candidate) -> bool:
        value = getattr(candidate, field)
        return canonical_timestamp(value).epoch is not None

    ranked = _ranked(connection, candidates, valid)
    if not ranked:
        return None
    chosen = min(
        (candidate for candidate, _rank in ranked),
        key=lambda candidate: (
            canonical_timestamp(getattr(candidate, field)).epoch,
            candidate.source_ordinal,
            candidate.kind,
        ),
    )
    return canonical_timestamp(getattr(chosen, field)).text


def _terminal(
    connection: Any, candidates: tuple[_Candidate, ...],
) -> str:
    ranked = _ranked(
        connection, candidates,
        lambda candidate: candidate.terminal_state != "unknown",
    )
    if not ranked:
        return "unknown"
    return max(
        (candidate for candidate, _rank in ranked),
        key=lambda candidate: (
            candidate.source_ordinal,
            {"success": 1, "failed": 2}[candidate.terminal_state],
        ),
    ).terminal_state


def _latency(
    connection: Any, candidates: tuple[_Candidate, ...],
) -> int | None:
    ranked = _ranked(
        connection, candidates,
        lambda candidate: candidate.latency_ms is not None,
    )
    if not ranked:
        return None
    chosen = max(
        (candidate for candidate, _rank in ranked),
        key=lambda candidate: (candidate.source_ordinal, candidate.latency_ms),
    )
    return chosen.latency_ms


def _turn(
    connection: Any, candidates: tuple[_Candidate, ...],
) -> str | None:
    ranked = _ranked(
        connection, candidates,
        lambda candidate: candidate.turn_key is not None,
    )
    if not ranked:
        return None
    return max(
        (candidate for candidate, _rank in ranked),
        key=lambda candidate: (
            _KIND_PRIORITY[candidate.kind],
            -candidate.source_ordinal,
            candidate.turn_key,
        ),
    ).turn_key


def _materialize(
    connection: Any, *, session_key: str, call_key: str,
) -> None:
    candidates = _candidates(
        connection, session_key=session_key, call_key=call_key,
    )
    descriptor = _description(connection, candidates)
    started_at = _timestamp(connection, candidates, "started_at")
    finished_at = _timestamp(connection, candidates, "finished_at")
    connection.execute(
        """INSERT INTO tool_spans(
               session_key,call_key,category,terminal_state,latency_ms,tool_name,
               started_at,finished_at,turn_key,source_digest,source_ordinal,
               completeness,provenance)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(session_key,call_key) DO UPDATE SET
             category=excluded.category,
             terminal_state=excluded.terminal_state,
             latency_ms=excluded.latency_ms,
             tool_name=excluded.tool_name,
             started_at=excluded.started_at,
             finished_at=excluded.finished_at,
             turn_key=excluded.turn_key,
             source_digest=excluded.source_digest,
             source_ordinal=excluded.source_ordinal,
             completeness=excluded.completeness,
             provenance=excluded.provenance""",
        (
            session_key,
            call_key,
            descriptor.category,
            _terminal(connection, candidates),
            _latency(connection, candidates),
            descriptor.tool_name,
            started_at,
            finished_at,
            _turn(connection, candidates),
            descriptor.source_digest or None,
            descriptor.source_ordinal,
            "complete" if started_at is not None and finished_at is not None else "incomplete",
            descriptor.provenance,
        ),
    )


def _persist_candidate(
    connection: Any,
    *,
    session_key: str,
    call_key: str,
    kind: str,
    category: str,
    tool_name: str,
    started_at: str | None,
    finished_at: str | None,
    terminal_state: str,
    latency_ms: int | None,
    turn_key: str | None,
    source_digest: str,
    source_ordinal: int,
    provenance: str,
) -> None:
    connection.execute(
        """INSERT INTO tool_span_candidates(
               session_key,call_key,source_digest,source_ordinal,candidate_kind,
               category,terminal_state,latency_ms,tool_name,started_at,finished_at,
               turn_key,provenance)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
        (
            session_key, call_key, source_digest, source_ordinal, kind,
            category, terminal_state, latency_ms, tool_name, started_at,
            finished_at, turn_key, provenance,
        ),
    )
    _materialize(connection, session_key=session_key, call_key=call_key)
    # A higher-authority terminal outcome or tool identity can invalidate a
    # lower adapter's previously materialized file fact even when the new
    # source emits no file candidate of its own.
    from .rollout_persistence import materialize_file_observations
    materialize_file_observations(
        connection, session_key=session_key, call_key=call_key,
    )


def persist_tool_start(
    connection: Any,
    *,
    session_key: str,
    call_key: str,
    category: str,
    tool_name: str,
    started_at: str | None,
    turn_key: str | None,
    source_digest: str,
    source_ordinal: int,
    provenance: str = "exact",
) -> None:
    """Record a safe start candidate and rebuild its canonical span."""
    _validate(category, tool_name, provenance=provenance)
    _persist_candidate(
        connection,
        session_key=session_key,
        call_key=call_key,
        kind="start",
        category=category,
        tool_name=tool_name,
        started_at=started_at,
        finished_at=None,
        terminal_state="unknown",
        latency_ms=None,
        turn_key=turn_key,
        source_digest=source_digest,
        source_ordinal=source_ordinal,
        provenance=provenance,
    )


def persist_tool_end(
    connection: Any,
    *,
    session_key: str,
    call_key: str,
    category: str,
    tool_name: str,
    finished_at: str | None,
    terminal_state: str,
    latency_ms: int | None,
    turn_key: str | None,
    source_digest: str,
    source_ordinal: int,
    provenance: str = "exact",
) -> None:
    """Record a safe completion candidate and rebuild its canonical span."""
    _validate(category, tool_name, terminal_state, provenance)
    _persist_candidate(
        connection,
        session_key=session_key,
        call_key=call_key,
        kind="end",
        category=category,
        tool_name=tool_name,
        started_at=None,
        finished_at=finished_at,
        terminal_state=terminal_state,
        latency_ms=latency_ms,
        turn_key=turn_key,
        source_digest=source_digest,
        source_ordinal=source_ordinal,
        provenance=provenance,
    )
