"""Small privacy-preserving persistence helpers for rollout ingestion."""

from __future__ import annotations

from typing import Any, Callable

from .rollout_identity import opaque
from .rollout_observations import path_key, safe_int
from .rollout_privacy import safe_diagnostic_kind


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


def persist_file(
    connection: Any, source: str, line: int, session: str, operation: str, value: Any,
    project_root: Any, observed_at: str | None, turn_key: str | None,
    *, observation_call_key: str | None = None,
) -> None:
    relative = path_key(value, project_root, opaque)
    if relative == "unknown":
        return
    # App Server and rollout JSONL can describe the same logical tool call.  A
    # stable, privacy-safe observation identity prevents those adapters from
    # inflating read/write counts.  Keep the earliest terminal observation so
    # ingestion order cannot move a fact past a task cutoff.
    if observation_call_key is not None:
        source = opaque(
            "event", f"file-observation/{session}/{observation_call_key}",
        )
        line = 0
    connection.execute(
        """INSERT INTO file_observations(
               source_digest,line_number,session_key,operation,relative_path,path_hash,observed_at,turn_key)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(source_digest,line_number,operation,relative_path) DO UPDATE SET
             observed_at=CASE
               WHEN file_observations.observed_at IS NULL THEN excluded.observed_at
               WHEN excluded.observed_at IS NULL THEN file_observations.observed_at
               WHEN julianday(excluded.observed_at) < julianday(file_observations.observed_at)
                 THEN excluded.observed_at
               ELSE file_observations.observed_at
             END,
             turn_key=CASE
               WHEN file_observations.observed_at IS NULL
                    AND excluded.observed_at IS NOT NULL
                 THEN COALESCE(excluded.turn_key,file_observations.turn_key)
               WHEN excluded.observed_at IS NOT NULL
                    AND julianday(excluded.observed_at) < julianday(file_observations.observed_at)
                 THEN COALESCE(excluded.turn_key,file_observations.turn_key)
               WHEN excluded.observed_at = file_observations.observed_at
                    OR (excluded.observed_at IS NULL AND file_observations.observed_at IS NULL)
                 THEN CASE
                   WHEN file_observations.turn_key IS NULL THEN excluded.turn_key
                   WHEN excluded.turn_key IS NULL THEN file_observations.turn_key
                   WHEN excluded.turn_key < file_observations.turn_key THEN excluded.turn_key
                   ELSE file_observations.turn_key
                 END
               ELSE COALESCE(file_observations.turn_key,excluded.turn_key)
             END""",
        (source, line, session, operation, relative, opaque("path", relative), observed_at, turn_key),
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
