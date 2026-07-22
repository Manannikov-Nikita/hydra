"""Single-pass, privacy-safe preparation and trusted attribution of Codex events."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sqlite3
import stat
from typing import Mapping, TYPE_CHECKING

from .codex_events import CodexEventBatch, read_codex_event_stream
from .rollout_identity import Pseudonymizer
from .rollout_sources import (
    SOURCE_CHANGED_MESSAGE,
    SourceChanged,
    SourceStat,
    open_source,
    source_stat,
)

if TYPE_CHECKING:
    from .codex_event_ingest import CodexEventSource


_ATTRIBUTION_CODES = frozenset({
    "event_attribution_unavailable",
    "event_attribution_ambiguous",
})


@dataclass(frozen=True)
class PreparedCodexEventSource:
    """One exact event stream, normalized without retaining raw content."""

    schema: str
    line_count: int
    byte_count: int
    path: Path = field(repr=False)
    source_stat: SourceStat = field(repr=False)
    raw_digest: str = field(repr=False)
    location_key: str = field(repr=False)
    batch: CodexEventBatch = field(repr=False)
    thread_keys: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.line_count, self.byte_count):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("event source counts must be non-negative integers")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("prepared event path must be absolute")
        if not isinstance(self.raw_digest, str) or len(self.raw_digest) != 64:
            raise ValueError("raw event digest must be sha256 hex")
        if not isinstance(self.location_key, str) or not self.location_key:
            raise ValueError("event location key must be non-empty")
        if self.batch.schema != self.schema:
            raise ValueError("prepared event schema must match its batch")
        if tuple(sorted(set(self.thread_keys))) != self.thread_keys:
            raise ValueError("prepared thread keys must be unique and sorted")


@dataclass(frozen=True)
class _ProjectRootIdentity:
    dev: int
    ino: int
    mode: int


@dataclass(frozen=True)
class PreparedEventAttribution:
    """One exact trusted project/worktree/root binding, hidden from repr."""

    project_id: str = field(repr=False)
    worktree_path: str = field(repr=False)
    project_root: Path = field(repr=False)
    root_identity: _ProjectRootIdentity = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise ValueError("attributed project id must be non-empty")
        if not isinstance(self.worktree_path, str) or not self.worktree_path:
            raise ValueError("attributed worktree path must be non-empty")
        if not isinstance(self.project_root, Path) or not self.project_root.is_absolute():
            raise ValueError("attributed project root must be an absolute path")
        if not isinstance(self.root_identity, _ProjectRootIdentity):
            raise ValueError("attributed project root identity is required")


@dataclass(frozen=True)
class EventAttributionDiagnostic:
    """Categorical public failure without source or project identifiers."""

    code: str

    def __post_init__(self) -> None:
        if self.code not in _ATTRIBUTION_CODES:
            raise ValueError("unsupported event attribution diagnostic")


def prepare_codex_event_source(
    source: CodexEventSource, *, hash_key: bytes,
) -> PreparedCodexEventSource:
    """Hash and parse one stat-bound physical stream exactly once."""
    hasher = Pseudonymizer(hash_key)
    requested = Path(source.path).expanduser().absolute()
    expected_stat = source_stat(requested)
    try:
        canonical = requested.resolve(strict=True)
    except OSError as error:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error
    if source_stat(requested) != expected_stat or source_stat(canonical) != expected_stat:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE)
    raw_digest = hashlib.sha256()
    line_count = 0
    byte_count = 0

    def measured_lines(handle):
        nonlocal line_count, byte_count
        for raw_line in handle:
            line_count += 1
            byte_count += len(raw_line)
            raw_digest.update(raw_line)
            yield raw_line

    with open_source(requested, expected_stat) as handle:
        batch = read_codex_event_stream(
            measured_lines(handle), schema=source.schema, privacy_key=hash_key,
        )
    return PreparedCodexEventSource(
        schema=source.schema,
        line_count=line_count,
        byte_count=byte_count,
        path=canonical,
        source_stat=expected_stat,
        raw_digest=raw_digest.hexdigest(),
        location_key=hasher.digest("path", str(canonical)),
        batch=batch,
        thread_keys=tuple(sorted({
            event.thread_key
            for event in batch.events
            if event.thread_key is not None
        })),
    )


def _current_project_root(
    value: object,
) -> tuple[Path, _ProjectRootIdentity] | None:
    if not isinstance(value, Path) or not value.is_absolute():
        return None
    try:
        before = value.stat(follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            return None
        canonical = value.resolve(strict=True)
        after = value.stat(follow_symlinks=False)
    except OSError:
        return None
    identity = lambda details: _ProjectRootIdentity(
        int(details.st_dev), int(details.st_ino), int(details.st_mode),
    )
    if canonical != value or identity(before) != identity(after):
        return None
    return value, identity(before)


def revalidate_prepared_event_attribution(
    attribution: PreparedEventAttribution,
) -> None:
    """Reject a project root that no longer has the attributed identity."""
    current = _current_project_root(attribution.project_root)
    if current is None or current[1] != attribution.root_identity:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE)


def attribute_prepared_codex_event_source(
    connection: sqlite3.Connection,
    prepared: PreparedCodexEventSource,
    worktrees: Mapping[tuple[str, str], tuple[Path, ...]],
) -> PreparedEventAttribution | EventAttributionDiagnostic:
    """Bind every event thread through trusted stored session capabilities only."""
    thread_keys = prepared.thread_keys
    if not thread_keys:
        return EventAttributionDiagnostic("event_attribution_unavailable")
    placeholders = ",".join("?" for _key in thread_keys)
    rows = connection.execute(
        f"""SELECT DISTINCT
                   sessions.session_id,sessions.project_id,sessions.worktree_path
              FROM sessions
             WHERE sessions.session_id IN ({placeholders})
               AND EXISTS (
                   SELECT 1 FROM trusted_turn_bindings
                    WHERE trusted_turn_bindings.session_key=sessions.session_id
                      AND trusted_turn_bindings.project_id=sessions.project_id
               )""",
        thread_keys,
    ).fetchall()
    if {str(row[0]) for row in rows} != set(thread_keys):
        return EventAttributionDiagnostic("event_attribution_unavailable")
    bindings = {(str(row[1]), str(row[2])) for row in rows}
    if len(bindings) != 1:
        return EventAttributionDiagnostic("event_attribution_ambiguous")
    project_id, worktree_path = next(iter(bindings))
    configured_roots = worktrees.get((project_id, worktree_path), ())
    if not isinstance(configured_roots, tuple):
        return EventAttributionDiagnostic("event_attribution_unavailable")
    roots = tuple(_current_project_root(root) for root in configured_roots)
    if any(root is None for root in roots):
        return EventAttributionDiagnostic("event_attribution_unavailable")
    if not roots:
        return EventAttributionDiagnostic("event_attribution_unavailable")
    if len(roots) != 1:
        return EventAttributionDiagnostic("event_attribution_ambiguous")
    root = roots[0]
    assert root is not None
    project_root, root_identity = root
    return PreparedEventAttribution(
        project_id, worktree_path, project_root, root_identity,
    )
