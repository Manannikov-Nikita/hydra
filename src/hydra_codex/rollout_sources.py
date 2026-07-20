"""Streaming source revision preflight and logical lineage decisions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .rollout_privacy import canonical_timestamp, nonempty_string


@dataclass(frozen=True)
class SourceScan:
    revision_digest: str
    line_fingerprints: tuple[str, ...]
    line_count: int
    byte_count: int
    chain_digest: str
    identity: str | None
    conversation: str | None
    cwd: str | None
    meta_timestamp: str | None
    segment_marker: str


def line_fingerprint(value: bytes, key: bytes) -> str:
    """Fingerprint the exact bytes observed on disk, before any decoding."""
    return hmac.new(key, b"hydra/source-line/" + value, hashlib.sha256).hexdigest()


def scan_source(path: Path, key: bytes, pseudonymize: Callable[[str, str], str]) -> SourceScan:
    """Read a rollout incrementally, retaining only keyed hashes and safe header fields."""
    revision = hmac.new(key, b"hydra/source-revision/", hashlib.sha256)
    chain = hmac.new(key, b"hydra/source-chain/", hashlib.sha256)
    fingerprints: list[str] = []
    first_meta: tuple[str, str | None, str | None, str | None, str] | None = None
    matched_meta: tuple[str, str | None, str | None, str | None, str] | None = None
    byte_count = 0
    with path.open("rb") as handle:
        for raw_line in handle:
            byte_count += len(raw_line)
            revision.update(raw_line)
            decoded = raw_line.decode("utf-8", errors="replace")
            fingerprint = line_fingerprint(raw_line, key)
            fingerprints.append(fingerprint)
            chain.update(bytes.fromhex(fingerprint))
            try:
                envelope = json.loads(decoded)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(envelope, dict) or envelope.get("type") != "session_meta":
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                continue
            candidate = nonempty_string(payload.get("id"), payload.get("session_id"))
            if candidate is None:
                continue
            payload_time = canonical_timestamp(payload.get("timestamp")).text
            envelope_time = canonical_timestamp(envelope.get("timestamp")).text
            candidate_meta = (
                candidate,
                nonempty_string(payload.get("session_id"), candidate),
                payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
                payload_time or envelope_time,
                f"{envelope_time or fingerprint}/{path.stem}",
            )
            if first_meta is None:
                first_meta = candidate_meta
            if candidate in path.name:
                matched_meta = candidate_meta
    identity, conversation, cwd, meta_timestamp, meta_marker = matched_meta or first_meta or (None, None, None, None, "missing")
    first = fingerprints[0] if fingerprints else pseudonymize("source", "empty")
    marker = meta_marker if identity is not None else first
    return SourceScan(
        revision.hexdigest(), tuple(fingerprints), len(fingerprints), byte_count,
        chain.hexdigest(), identity, conversation, cwd, meta_timestamp, marker,
    )


def revision_lines(connection: sqlite3.Connection, digest: str) -> tuple[str, ...]:
    return tuple(row[0] for row in connection.execute(
        "SELECT line_fingerprint FROM rollout_revision_lines WHERE revision_digest=? ORDER BY line_number",
        (digest,),
    ))


def relation_to(canonical: tuple[str, ...], current: tuple[str, ...]) -> str:
    if canonical == current:
        return "exact"
    shared = min(len(canonical), len(current))
    if canonical[:shared] != current[:shared]:
        return "rewrite"
    return "append" if len(current) > len(canonical) else "truncate"


def prefix_lineage(
    connection: sqlite3.Connection, session_key: str, project_id: str, current: tuple[str, ...],
) -> str | None:
    """Find the strongest clean prefix relation for a source observed at a new location."""
    matches: list[tuple[int, str]] = []
    rows = connection.execute(
        """SELECT logical_source_key,canonical_revision_digest
             FROM rollout_logical_sources
            WHERE session_key=? AND project_id=? AND lineage_state='clean'
              AND canonical_revision_digest IS NOT NULL""",
        (session_key, project_id),
    )
    for logical, revision in rows:
        canonical = revision_lines(connection, revision)
        if relation_to(canonical, current) != "rewrite":
            matches.append((min(len(canonical), len(current)), logical))
    return max(matches, default=(0, None), key=lambda item: (item[0], item[1]))[1]


def located_lineage(connection: sqlite3.Connection, location_key: str, project_id: str) -> str | None:
    row = connection.execute(
        """SELECT locations.logical_source_key
             FROM rollout_source_locations AS locations
             JOIN rollout_logical_sources AS sources
               ON sources.logical_source_key=locations.logical_source_key
            WHERE locations.location_key=? AND sources.project_id=?""",
        (location_key, project_id),
    ).fetchone()
    return None if row is None else row[0]
