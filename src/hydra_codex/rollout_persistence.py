"""Small privacy-preserving persistence helpers for rollout ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .rollout_identity import opaque
from .rollout_observations import path_key, safe_int
from .rollout_privacy import canonical_timestamp, safe_diagnostic_kind
from .source_authority import (
    SOURCE_AUTHORITY,
    rollout_revision_identity,
    source_family,
)


def insert_diagnostic(
    connection: Any,
    source: str,
    line: int,
    kind: str,
    payload: Any,
    *,
    fingerprint: Callable[[Any], str],
    unsafe_value: Any = None,
) -> None:
    digest = fingerprint(payload)
    if unsafe_value is not None:
        digest = "value/" + opaque("diagnostic", repr(unsafe_value) + "/" + digest)[:32]
    connection.execute(
        """INSERT INTO rollout_diagnostics(source_digest,line_number,envelope_kind,fingerprint)
           VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
        (source, line, safe_diagnostic_kind(kind), digest),
    )


@dataclass(frozen=True)
class _FileCandidate:
    source_digest: str
    source_ordinal: int
    operation: str
    relative_path: str
    path_hash: str
    observed_at: str | None
    turn_key: str | None
    tool_name: str
    requires_success: bool
    evidence_kind: str


_FILE_CANDIDATE_COLUMNS = (
    "source_digest,source_ordinal,operation,relative_path,path_hash,observed_at,"
    "turn_key,tool_name,requires_success,evidence_kind"
)


def _file_candidates(
    connection: Any, *, session_key: str, call_key: str,
) -> tuple[_FileCandidate, ...]:
    return tuple(
        _FileCandidate(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
            bool(row[8]), row[9],
        )
        for row in connection.execute(
            f"SELECT {_FILE_CANDIDATE_COLUMNS} FROM file_observation_candidates "
            "WHERE session_key=? AND call_key=?",
            (session_key, call_key),
        )
    )


def _compatible_tool(candidate: str, canonical: str) -> bool:
    if candidate == "unknown" or canonical == "unknown":
        return True
    if candidate == canonical:
        return True
    return {candidate, canonical} == {"apply_patch", "patch"}


def _has_structural_terminal(
    connection: Any, *, session_key: str, call_key: str, canonical_name: str,
) -> bool:
    """Require an observed end record without treating missing outcome as no run."""
    candidate_terminal = any(
        candidate_kind == "end" and (
            _compatible_tool(tool_name, canonical_name)
            or tool_name == "function"
        )
        for candidate_kind, tool_name in connection.execute(
            "SELECT candidate_kind,tool_name FROM tool_span_candidates "
            "WHERE session_key=? AND call_key=?",
            (session_key, call_key),
        )
    )
    if candidate_terminal:
        return True
    # Pre-candidate schemas already persisted a canonical complete span.  That
    # normalized terminal is sufficient migration evidence even when no
    # immutable end candidate existed at the time.
    legacy = connection.execute(
        "SELECT completeness FROM tool_spans WHERE session_key=? AND call_key=?",
        (session_key, call_key),
    ).fetchone()
    return legacy is not None and legacy[0] == "complete"


def _timestamp_value(value: str | None) -> tuple[datetime, str] | None:
    safe = canonical_timestamp(value)
    if safe.text is None:
        return None
    # datetime compares all six persisted fractional digits exactly.  SQLite's
    # julianday() rounds much more aggressively and can reverse sub-ms facts.
    parsed = datetime.fromisoformat(safe.text.replace("Z", "+00:00"))
    return parsed, safe.text


def _earliest(
    candidates: tuple[_FileCandidate, ...],
) -> tuple[str | None, str | None]:
    timestamped = tuple(
        (candidate, value)
        for candidate in candidates
        if (value := _timestamp_value(candidate.observed_at)) is not None
    )
    if timestamped:
        earliest = min(value[0] for _candidate, value in timestamped)
        tied = tuple(
            candidate for candidate, value in timestamped if value[0] == earliest
        )
        observed_at = min(
            value[1] for _candidate, value in timestamped if value[0] == earliest
        )
    else:
        tied = candidates
        observed_at = None
    turns = tuple(
        candidate.turn_key for candidate in tied if candidate.turn_key is not None
    )
    return observed_at, min(turns) if turns else None


def _proven_append_owner(
    connection: Any,
    *,
    session_key: str,
    call_key: str,
    canonical_source: str | None,
    owner_source: str,
    terminal_state: str,
    owner_candidates: tuple[_FileCandidate, ...],
) -> bool:
    canonical_revision = rollout_revision_identity(connection, canonical_source)
    owner_revision = rollout_revision_identity(connection, owner_source)
    if canonical_revision is None or owner_revision is None:
        return False
    canonical_logical, canonical_lines = canonical_revision
    owner_logical, owner_lines = owner_revision
    if canonical_logical != owner_logical or owner_lines <= canonical_lines:
        return False
    relation = connection.execute(
        "SELECT relation FROM rollout_sources WHERE source_digest=?",
        (owner_source,),
    ).fetchone()
    if relation is None or relation[0] != "append":
        return False
    if any(candidate.source_ordinal <= canonical_lines for candidate in owner_candidates):
        return False
    terminal = connection.execute(
        """SELECT 1 FROM tool_span_candidates
              WHERE session_key=? AND call_key=? AND source_digest=?
                AND candidate_kind='end' AND terminal_state=?
                AND source_ordinal>?
              LIMIT 1""",
        (
            session_key,
            call_key,
            owner_source,
            terminal_state,
            canonical_lines,
        ),
    ).fetchone()
    return terminal is not None


def materialize_file_observations(
    connection: Any, *, session_key: str, call_key: str,
    source_identity: Callable[[str, str], str | None] | None = None,
) -> None:
    """Rebuild one call's materialized file facts from immutable candidates."""
    candidates = _file_candidates(
        connection, session_key=session_key, call_key=call_key,
    )
    if not candidates:
        return
    tool = connection.execute(
        "SELECT tool_name,terminal_state,source_digest FROM tool_spans "
        "WHERE session_key=? AND call_key=?",
        (session_key, call_key),
    ).fetchone()
    canonical_name = "unknown" if tool is None else str(tool[0] or "unknown")
    terminal_state = "unknown" if tool is None else str(tool[1])
    canonical_source = None if tool is None else tool[2]

    materialized_source = (
        opaque("event", f"file-observation/{session_key}/{call_key}")
        if source_identity is None
        else source_identity(session_key, call_key)
    )
    migrated = tuple(
        candidate for candidate in candidates
        if candidate.evidence_kind in {"legacy", "quarantined"}
    )
    # Without the installation key migration cannot safely recreate a
    # synthetic materialized identity.  It may still remove disproven facts
    # and must quarantine unresolved legacy line-zero rows, but it leaves a
    # successful direct legacy observation intact until the next keyed ingest.
    canonical_authority = SOURCE_AUTHORITY[
        source_family(connection, canonical_source)
    ]
    removable = (
        migrated
        if materialized_source is not None or terminal_state == "failed"
        else tuple(
            candidate for candidate in migrated
            if candidate.evidence_kind == "quarantined"
            or SOURCE_AUTHORITY[
                source_family(connection, candidate.source_digest)
            ] < canonical_authority
        )
    )
    connection.executemany(
        """DELETE FROM file_observations
              WHERE source_digest=? AND line_number=? AND session_key=?
                AND operation=? AND path_hash=?""",
        (
            (
                candidate.source_digest, candidate.source_ordinal, session_key,
                candidate.operation, candidate.path_hash,
            )
            for candidate in removable
        ),
    )
    if materialized_source is not None:
        connection.execute(
            "DELETE FROM file_observations WHERE source_digest=? AND line_number=0",
            (materialized_source,),
        )
    if not _has_structural_terminal(
        connection, session_key=session_key, call_key=call_key,
        canonical_name=canonical_name,
    ) or terminal_state == "failed":
        return
    if materialized_source is None:
        return

    eligible = tuple(
        candidate for candidate in candidates
        if candidate.evidence_kind != "quarantined"
        if _compatible_tool(candidate.tool_name, canonical_name)
        and (not candidate.requires_success or terminal_state == "success")
    )
    if not eligible:
        return

    # The highest-authority adapter owns the fact set.  Lower adapters may
    # corroborate those exact facts (and provide an earlier timestamp), but
    # cannot add paths that the authoritative source did not report.
    ranked = tuple(
        (
            candidate,
            SOURCE_AUTHORITY[source_family(connection, candidate.source_digest)],
        )
        for candidate in eligible
    )
    canonical_candidates = tuple(
        candidate for candidate in eligible
        if canonical_source is not None
        and candidate.source_digest == canonical_source
    )
    if canonical_candidates:
        owners = canonical_candidates
    elif any(
        candidate.source_digest == canonical_source for candidate in candidates
    ):
        # The canonical adapter emitted file evidence, but none of it is
        # compatible with the canonical tool/outcome.  Do not substitute a
        # competing same-family source.
        return
    else:
        # Append-only rollout revisions can move a terminal file event into a
        # later source while the canonical span description still points at a
        # start event in the previous revision.  Preserve that case, but only
        # at the canonical source-family authority; lower adapters stay unable
        # to invent paths.
        authority = max(rank for _candidate, rank in ranked)
        canonical_authority = (
            SOURCE_AUTHORITY[source_family(connection, canonical_source)]
            if canonical_source is not None else None
        )
        if canonical_authority is not None and authority < canonical_authority:
            return
        owner_sources = {
            candidate.source_digest for candidate, rank in ranked
            if rank == authority
        }
        if len(owner_sources) != 1:
            # Multiple competing revisions without a canonical-source fact are
            # not an append proof.  Retain immutable candidates but do not
            # manufacture a union of their paths.
            return
        owners = tuple(
            candidate for candidate, rank in ranked
            if rank == authority and candidate.source_digest in owner_sources
        )
        owner_source = next(iter(owner_sources))
        if not _proven_append_owner(
            connection,
            session_key=session_key,
            call_key=call_key,
            canonical_source=canonical_source,
            owner_source=owner_source,
            terminal_state=terminal_state,
            owner_candidates=owners,
        ):
            return
    fact_keys = {
        (candidate.operation, candidate.relative_path)
        for candidate in owners
    }
    corroborating = tuple(
        candidate for candidate in eligible
        if (candidate.operation, candidate.relative_path) in fact_keys
    )
    for operation, relative_path in sorted(fact_keys):
        path_hash = min(
            candidate.path_hash for candidate in owners
            if (candidate.operation, candidate.relative_path)
            == (operation, relative_path)
        )
        matches = tuple(
            candidate for candidate in corroborating
            if (candidate.operation, candidate.relative_path)
            == (operation, relative_path)
        )
        observed_at, turn_key = _earliest(matches)
        connection.execute(
            """INSERT INTO file_observations(
                   source_digest,line_number,session_key,operation,relative_path,
                   path_hash,observed_at,turn_key)
               VALUES (?,0,?,?,?,?,?,?)""",
            (
                materialized_source, session_key, operation, relative_path,
                path_hash, observed_at, turn_key,
            ),
        )


def persist_file(
    connection: Any, source: str, line: int, session: str, operation: str, value: Any,
    project_root: Any, observed_at: str | None, turn_key: str | None,
    *, observation_call_key: str | None = None,
    observation_tool_name: str = "unknown", requires_success: bool = False,
) -> None:
    relative = path_key(value, project_root, opaque)
    if relative == "unknown":
        return
    call_key = observation_call_key or opaque(
        "call", f"file-observation/{source}/{line}",
    )
    canonical_at = canonical_timestamp(observed_at).text
    path_hash = opaque("path", relative)
    candidate_key = opaque(
        "event",
        "/".join((
            "file-candidate", session, call_key, source, str(line), operation,
            path_hash, canonical_at or "", turn_key or "", observation_tool_name,
            str(int(requires_success)),
        )),
    )
    connection.execute(
        """INSERT INTO file_observation_candidates(
               session_key,call_key,candidate_key,source_digest,source_ordinal,operation,
               relative_path,path_hash,observed_at,turn_key,tool_name,
               requires_success,evidence_kind)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'exact') ON CONFLICT DO NOTHING""",
        (
            session, call_key, candidate_key, source, line, operation, relative,
            path_hash, canonical_at,
            turn_key, observation_tool_name, int(requires_success),
        ),
    )
    materialize_file_observations(
        connection, session_key=session, call_key=call_key,
    )


def tool_end_state(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if result == "Ok" or (isinstance(result, dict) and "Ok" in result) or payload.get("success") is True:
        return "success"
    if result is not None or payload.get("success") is False:
        return "failed"
    return "unknown"


def duration_ms(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    seconds = safe_int(value.get("secs")) or 0
    nanos = safe_int(value.get("nanos")) or 0
    return seconds * 1000 + nanos // 1_000_000
