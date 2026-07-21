"""Resolve deterministic authority for privacy-safe persisted source identities."""

from __future__ import annotations

from typing import Any


SOURCE_AUTHORITY = {"unknown": 0, "otel": 1, "app_server": 2, "rollout": 3}


def source_family(connection: Any, source_digest: str | None) -> str:
    """Resolve a normalized source through existing registries, never ingest order."""
    if not isinstance(source_digest, str) or not source_digest:
        return "unknown"
    event = connection.execute(
        "SELECT source_format FROM codex_event_sources WHERE source_digest=?",
        (source_digest,),
    ).fetchone()
    if event is not None and event[0] in {"app_server", "otel"}:
        return str(event[0])
    rollout = connection.execute(
        "SELECT source_type,chain_digest FROM rollout_sources WHERE source_digest=?",
        (source_digest,),
    ).fetchone()
    if rollout is None:
        return "unknown"
    if rollout[0] == "jsonl":
        return "rollout"
    if rollout[1] is not None:
        event = connection.execute(
            "SELECT source_format FROM codex_event_sources WHERE source_digest=?",
            (rollout[1],),
        ).fetchone()
        if event is not None and event[0] in {"app_server", "otel"}:
            return str(event[0])
    return "unknown"


def source_rank(connection: Any, source_digest: str | None) -> tuple[int, str]:
    """Return explicit family precedence plus a stable same-family tie break."""
    digest = source_digest if isinstance(source_digest, str) else ""
    return SOURCE_AUTHORITY[source_family(connection, digest)], digest
