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
) -> None:
    relative = path_key(value, project_root, opaque)
    if relative == "unknown":
        return
    connection.execute(
        """INSERT INTO file_observations(
               source_digest,line_number,session_key,operation,relative_path,path_hash,observed_at,turn_key)
           VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
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
