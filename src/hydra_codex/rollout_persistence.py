"""Small privacy-preserving persistence helpers for rollout ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .rollout_identity import opaque
from .rollout_observations import path_key, safe_int
from .rollout_privacy import canonical_timestamp, safe_diagnostic_kind
from .source_authority import SOURCE_AUTHORITY, source_family


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


def materialize_file_observations(
    connection: Any, *, session_key: str, call_key: str,
) -> None:
    """Rebuild one call's materialized file facts from immutable candidates."""
    candidates = _file_candidates(
        connection, session_key=session_key, call_key=call_key,
    )
    if not candidates:
        return
    materialized_source = opaque(
        "event", f"file-observation/{session_key}/{call_key}",
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
            for candidate in candidates
            if candidate.evidence_kind == "legacy"
        ),
    )
    connection.execute(
        "DELETE FROM file_observations WHERE source_digest=? AND line_number=0",
        (materialized_source,),
    )

    tool = connection.execute(
        "SELECT tool_name,terminal_state,source_digest FROM tool_spans "
        "WHERE session_key=? AND call_key=?",
        (session_key, call_key),
    ).fetchone()
    canonical_name = "unknown" if tool is None else str(tool[0] or "unknown")
    terminal_state = "unknown" if tool is None else str(tool[1])
    if terminal_state == "failed":
        return

    eligible = tuple(
        candidate for candidate in candidates
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
    authority = max(rank for _candidate, rank in ranked)
    canonical_authority = (
        SOURCE_AUTHORITY[source_family(connection, tool[2])]
        if tool is not None else None
    )
    if canonical_authority is not None and authority < canonical_authority:
        return
    fact_keys = {
        (candidate.operation, candidate.relative_path)
        for candidate, rank in ranked if rank == authority
    }
    corroborating = tuple(
        candidate for candidate in eligible
        if (candidate.operation, candidate.relative_path) in fact_keys
    )
    for operation, relative_path in sorted(fact_keys):
        path_hash = min(
            candidate.path_hash for candidate, rank in ranked
            if rank == authority
            and (candidate.operation, candidate.relative_path)
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
