"""Streaming source revision preflight and logical lineage decisions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import BinaryIO, Callable, Iterator

from .rollout_privacy import canonical_timestamp, nonempty_string


SOURCE_CHANGED_MESSAGE = "rollout source changed during ingest"


class SourceChanged(RuntimeError):
    """The source stopped matching the exact regular file being ingested."""


@dataclass(frozen=True)
class SourceStat:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class SourceScan:
    revision_digest: str
    line_fingerprints: tuple[str, ...]
    line_count: int
    byte_count: int
    chain_digest: str
    identity: str | None = field(repr=False)
    conversation: str | None = field(repr=False)
    cwd: str | None = field(repr=False)
    meta_timestamp: str | None
    segment_marker: str
    source_stat: SourceStat
    path: Path = field(repr=False)


def _regular_source_stat(details: os.stat_result) -> SourceStat:
    if not stat.S_ISREG(details.st_mode):
        raise SourceChanged(SOURCE_CHANGED_MESSAGE)
    return SourceStat(
        dev=int(details.st_dev),
        ino=int(details.st_ino),
        size=int(details.st_size),
        mtime_ns=int(details.st_mtime_ns),
        ctime_ns=int(details.st_ctime_ns),
    )


def source_stat(path: Path) -> SourceStat:
    """Return the exact no-follow metadata for one regular source file."""
    try:
        return _regular_source_stat(path.stat(follow_symlinks=False))
    except OSError as error:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error


@contextmanager
def open_source(
    path: Path, expected_stat: SourceStat | None = None,
) -> Iterator[BinaryIO]:
    """Open one exact regular file without following a replacement symlink."""
    expected = source_stat(path) if expected_stat is None else expected_stat
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        if _regular_source_stat(os.fstat(descriptor)) != expected:
            raise SourceChanged(SOURCE_CHANGED_MESSAGE)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            yield handle
            after_stream = _regular_source_stat(os.fstat(handle.fileno()))
        if after_stream != expected or source_stat(path) != expected:
            raise SourceChanged(SOURCE_CHANGED_MESSAGE)
    except SourceChanged:
        raise
    except OSError as error:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def line_fingerprint(value: bytes, key: bytes) -> str:
    """Fingerprint the exact bytes observed on disk, before any decoding."""
    return hmac.new(key, b"hydra/source-line/" + value, hashlib.sha256).hexdigest()


def scan_source(path: Path, key: bytes, pseudonymize: Callable[[str, str], str]) -> SourceScan:
    """Read a rollout incrementally, retaining only keyed hashes and safe header fields."""
    before_stat = source_stat(path)
    try:
        canonical_path = path.resolve(strict=True)
    except OSError as error:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error
    revision = hmac.new(key, b"hydra/source-revision/", hashlib.sha256)
    chain = hmac.new(key, b"hydra/source-chain/", hashlib.sha256)
    fingerprints: list[str] = []
    first_meta: tuple[str, str | None, str | None, str | None, str] | None = None
    matched_meta: tuple[int, tuple[str, str | None, str | None, str | None, str]] | None = None
    byte_count = 0
    with open_source(path, before_stat) as handle:
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
            if not isinstance(envelope, dict):
                continue
            if envelope.get("type") != "session_meta":
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
                envelope_time or fingerprint,
            )
            if first_meta is None:
                first_meta = candidate_meta
            suffix = f"{candidate}.jsonl"
            exact_suffix = path.name == suffix or path.name.endswith("-" + suffix)
            if exact_suffix and (matched_meta is None or len(candidate) > matched_meta[0]):
                matched_meta = (len(candidate), candidate_meta)
    selected_meta = matched_meta[1] if matched_meta is not None else first_meta
    identity, conversation, cwd, meta_timestamp, meta_marker = selected_meta or (None, None, None, None, "missing")
    first = fingerprints[0] if fingerprints else pseudonymize("source", "empty")
    marker = meta_marker if identity is not None else first
    return SourceScan(
        revision.hexdigest(), tuple(fingerprints), len(fingerprints), byte_count,
        chain.hexdigest(), identity, conversation, cwd, meta_timestamp, marker,
        before_stat, canonical_path,
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
        """SELECT logical.logical_source_key,logical.canonical_revision_digest
             FROM rollout_logical_sources AS logical
             JOIN rollout_sources AS revision
               ON revision.source_digest=logical.canonical_revision_digest
              AND revision.source_type='jsonl'
            WHERE logical.session_key=? AND logical.project_id=?
              AND logical.lineage_state='clean'
              AND logical.canonical_revision_digest IS NOT NULL""",
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
