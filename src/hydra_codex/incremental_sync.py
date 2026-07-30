"""Durable, queue-driven JSONL tailing without global rollout discovery.

This module intentionally has no dependency on :func:`discover_trusted_rollouts`.
Normal sync only resolves a source that a trusted hook or repair job previously
registered.  Materialising a line is injected because the legacy full-file
parser carries transient state; callers can migrate to this transaction seam
without reintroducing a full-file scan.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid
from typing import BinaryIO, Iterator

from .lineage import assert_session_project
from .project import ProjectNotFound, resolve_project
from .reconcile_engine import reconcile_project
from .rollout import ingest_rollouts
from .rollout_identity import Pseudonymizer, RolloutRoot
from .rollout_observations import safe_int, usage as parse_usage
from .rollout_privacy import canonical_timestamp, nonempty_string
from .rollout_reconcile import reconcile_fork_baselines, reconcile_token_epochs, reconcile_turn_attempts
from .rollout_sources import SOURCE_CHANGED_MESSAGE, SourceChanged, SourceStat, line_fingerprint, open_source, scan_source, source_stat
from .storage import HydraStore
from .sync_state import (
    DirtyRoot,
    QueueItem,
    SourceCheckpoint,
    SyncStateRepository,
    _epoch_nanoseconds,
    validate_root_relative_locator,
)
from .test_evidence import materialize_test_evidence, reconcile_test_retries
from .token_selection import refresh_token_session_selection


_PREFIX_BYTES = 256
_BOUNDARY_BYTES = 256


class RepairRequired(RuntimeError):
    """One registered source no longer has an append-only relationship."""


class _MaterializationRejected(RuntimeError):
    """A deterministic materializer validation rejected one source."""


@dataclass(frozen=True)
class TrustedSourceRoots:
    sessions: Path
    archived_sessions: Path

    def _root_with_identity(
        self, root_kind: str,
    ) -> tuple[Path, tuple[int, int]]:
        try:
            value = {"sessions": self.sessions, "archived_sessions": self.archived_sessions}[root_kind]
        except KeyError as error:
            raise ValueError("untrusted source root") from error
        try:
            details = value.lstat()
        except OSError as error:
            raise RepairRequired("trusted source root is unavailable") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise RepairRequired("trusted source root is unavailable")
        return value, (int(details.st_dev), int(details.st_ino))

    def root_for(self, root_kind: str) -> Path:
        return self._root_with_identity(root_kind)[0]

    @contextmanager
    def open_directory(self, root_kind: str, locator: str) -> Iterator[int]:
        """Hold one trusted directory by descriptor-relative, no-follow opens."""
        root, expected_root = self._root_with_identity(root_kind)
        directory: int | None = None
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            directory = os.open(root, flags)
            opened_root = os.fstat(directory)
            if (
                not stat.S_ISDIR(opened_root.st_mode)
                or (int(opened_root.st_dev), int(opened_root.st_ino))
                != expected_root
            ):
                raise RepairRequired("trusted source root changed during open")
            if locator != "@root":
                for component in validate_root_relative_locator(locator).split("/"):
                    child = os.open(component, flags, dir_fd=directory)
                    if not stat.S_ISDIR(os.fstat(child).st_mode):
                        os.close(child)
                        raise RepairRequired(
                            "repair frontier is no longer a directory",
                        )
                    os.close(directory)
                    directory = child
            yield directory
        except RepairRequired:
            raise
        except OSError as error:
            raise RepairRequired("repair frontier is unavailable") from error
        finally:
            if directory is not None:
                os.close(directory)

    @contextmanager
    def open_held(self, root_kind: str, locator: str) -> Iterator[tuple[BinaryIO, SourceStat]]:
        """Open a regular source by descriptor-relative traversal, fail closed.

        Each component is opened from the directory descriptor that named it;
        a parent swap to a symlink after validation therefore cannot redirect
        the final open. The returned descriptor remains held while its caller
        validates anchors and reads the append.
        """
        relative = validate_root_relative_locator(locator)
        root, expected_root = self._root_with_identity(root_kind)
        directory: int | None = None
        source: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory = os.open(root, flags)
            opened_root = os.fstat(directory)
            if (
                not stat.S_ISDIR(opened_root.st_mode)
                or (int(opened_root.st_dev), int(opened_root.st_ino))
                != expected_root
            ):
                raise RepairRequired("trusted source root is unavailable")
            parts = relative.split("/")
            for component in parts[:-1]:
                child = os.open(component, flags, dir_fd=directory)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    os.close(child)
                    raise RepairRequired("source locator is unavailable")
                os.close(directory)
                directory = child
            source = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            details = os.fstat(source)
            if not stat.S_ISREG(details.st_mode):
                raise RepairRequired("source locator is not a regular file")
            stat_value = SourceStat(int(details.st_dev), int(details.st_ino), int(details.st_size),
                                    int(details.st_mtime_ns), int(details.st_ctime_ns))
            with os.fdopen(source, "rb", closefd=True) as handle:
                source = None
                yield handle, stat_value
                after = os.fstat(handle.fileno())
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    stat_value.dev, stat_value.ino, stat_value.size, stat_value.mtime_ns, stat_value.ctime_ns,
                ):
                    raise RepairRequired("source changed while tailing")
        except RepairRequired:
            raise
        except OSError as error:
            raise RepairRequired("source locator is unavailable") from error
        finally:
            if source is not None:
                os.close(source)
            if directory is not None:
                os.close(directory)

    def source_opener(self, root_kind: str, locator: str, *, byte_limit: int | None = None):
        """Build a fd-relative opener for legacy scan/parser seams.

        The passed path is intentionally ignored.  It is a private lexical
        label only; every actual open starts at a fresh trusted root descriptor.
        """
        validate_root_relative_locator(locator)
        if byte_limit is not None and byte_limit < 0:
            raise ValueError("source read limit is invalid")

        @contextmanager
        def opener(_path: Path, expected: SourceStat | None = None) -> Iterator[BinaryIO]:
            try:
                with self.open_held(root_kind, locator) as (handle, actual):
                    if expected is not None and actual != expected:
                        raise SourceChanged(SOURCE_CHANGED_MESSAGE)
                    yield handle if byte_limit is None else _BoundedSourceRead(handle, byte_limit)
            except RepairRequired as error:
                raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error

        return opener

    def resolve(self, root_kind: str, locator: str) -> Path:
        """Resolve a private locator without accepting symlinks or escapes."""
        relative = validate_root_relative_locator(locator)
        root = self.root_for(root_kind)
        candidate = root
        try:
            for component in relative.split("/"):
                candidate = candidate / component
                details = candidate.lstat()
                if stat.S_ISLNK(details.st_mode):
                    raise RepairRequired("source locator traverses a symlink")
            if not stat.S_ISREG(details.st_mode):
                raise RepairRequired("source locator is not a regular file")
        except RepairRequired:
            raise
        except OSError as error:
            raise RepairRequired("source locator is unavailable") from error
        # ``candidate`` is constructed from validated lexical components.  The
        # per-component lstat above is what prevents resolve() from crossing a
        # symlink; no absolute locator is persisted or returned through an API.
        return candidate


@dataclass(frozen=True)
class TailLine:
    ordinal: int
    value: bytes


class _BoundedSourceRead:
    """A non-owning byte cap over a held trusted descriptor.

    It deliberately exposes only the binary reads/iteration used by legacy
    scan and parser code plus ``fileno`` for fstat verification.  No temporary
    transcript copy or pathname re-open is involved.
    """

    def __init__(self, handle: BinaryIO, byte_limit: int) -> None:
        self._handle = handle
        self._remaining = byte_limit

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        value = self._handle.read(requested)
        self._remaining -= len(value)
        return value

    def readline(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        value = self._handle.readline(requested)
        self._remaining -= len(value)
        return value

    def __iter__(self) -> "_BoundedSourceRead":
        return self

    def __next__(self) -> bytes:
        value = self.readline()
        if not value:
            raise StopIteration
        return value


@dataclass(frozen=True)
class TailRead:
    lines: tuple[TailLine, ...]
    checkpoint: SourceCheckpoint
    bytes_read: int
    # ``has_complete_work`` deliberately excludes an EOF partial line.  The
    # worker can immediately requeue a bounded complete suffix, while an
    # incomplete final record waits for its producer instead of hot-looping.
    has_complete_work: bool = False
    partial_line: bool = False


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _prefix_anchor(handle: BinaryIO, size: int) -> str | None:
    if size < _PREFIX_BYTES:
        return None
    handle.seek(0)
    return _digest(handle.read(_PREFIX_BYTES))


def _boundary_anchor(handle: BinaryIO, offset: int) -> str | None:
    if offset <= 0:
        return None
    width = min(_BOUNDARY_BYTES, offset)
    handle.seek(offset - width)
    return _digest(handle.read(width))


def _checkpoint_for(
    handle: BinaryIO, details: SourceStat, offset: int, line_number: int,
) -> SourceCheckpoint:
    return SourceCheckpoint(
        byte_offset=offset,
        line_number=line_number,
        prefix_anchor=_prefix_anchor(handle, details.size),
        revision_anchor=_boundary_anchor(handle, offset),
        file_size=details.size,
        device_id=details.dev,
        inode=details.ino,
    )


def _full_scan_checkpoint(
    roots: TrustedSourceRoots, root_kind: str, locator: str, scan,
) -> SourceCheckpoint:
    """Checkpoint a completed legacy scan without applying tail claim limits.

    ``scan_source`` already streamed every record for the explicit repair.
    Re-reading it through the bounded live-tail reader would silently leave a
    large repair at 1 MiB/10k lines.  Re-open the same descriptor-relative
    source only to verify the scan's exact stat and hash the small anchors.
    """
    with roots.open_held(root_kind, locator) as (handle, details):
        if details != scan.source_stat or details.size != scan.byte_count:
            raise RepairRequired("source changed during full repair")
        # Old callers can still construct SourceScan positionally; their
        # legacy counts remain the safest available fallback. New scans retain
        # a final unterminated fragment for the incremental append reader.
        complete_bytes = scan.byte_count if scan.complete_byte_count is None else scan.complete_byte_count
        complete_lines = scan.line_count if scan.complete_line_count is None else scan.complete_line_count
        return _checkpoint_for(handle, details, complete_bytes, complete_lines)


def read_incremental_source(
    roots: TrustedSourceRoots, root_kind: str, source_locator: str,
    checkpoint: SourceCheckpoint | None = None, *, max_bytes: int = 1024 * 1024,
    max_lines: int = 10_000,
) -> TailRead:
    """Read only new, newline-complete bytes from one trusted source.

    A changed inode, a smaller size, a changed stable prefix, or a changed
    boundary before the durable offset is a repair condition for *this source*
    only.  A partial final line is deliberately not included in the checkpoint.
    An unchanged-size rewrite strictly in the unchecked middle of an already
    consumed prefix is intentionally not reread: it is outside the bounded
    tailing contract and requires explicit repair history.
    """
    if not 1 <= max_bytes <= 16 * 1024 * 1024 or not 1 <= max_lines <= 100_000:
        raise ValueError("tail claim limits are invalid")
    prior = checkpoint or SourceCheckpoint(0, 0, None, None, 0, None, None)
    try:
        with roots.open_held(root_kind, source_locator) as (handle, before):
            if prior.byte_offset > before.size:
                raise RepairRequired("source was truncated")
            if prior.device_id is not None and (prior.device_id, prior.inode) != (before.dev, before.ino):
                raise RepairRequired("source inode changed")
            if prior.prefix_anchor is not None and _prefix_anchor(handle, before.size) != prior.prefix_anchor:
                raise RepairRequired("source prefix changed")
            if prior.revision_anchor is not None and _boundary_anchor(handle, prior.byte_offset) != prior.revision_anchor:
                raise RepairRequired("source boundary changed")
            handle.seek(prior.byte_offset)
            lines: list[TailLine] = []
            buffered = b""
            consumed = 0
            read_total = 0
            while read_total < max_bytes and len(lines) < max_lines:
                chunk = handle.read(min(64 * 1024, max_bytes - read_total))
                if not chunk:
                    break
                read_total += len(chunk)
                buffered += chunk
                while len(lines) < max_lines:
                    ending = buffered.find(b"\n")
                    if ending < 0:
                        break
                    raw = buffered[:ending + 1]
                    buffered = buffered[ending + 1:]
                    lines.append(TailLine(prior.line_number + len(lines) + 1, raw))
                    consumed += len(raw)
            offset = prior.byte_offset + consumed
            next_checkpoint = _checkpoint_for(handle, before, offset, prior.line_number + len(lines))
            unread_after_buffer = before.size - (prior.byte_offset + read_total)
            has_complete_work = False
            partial_line = False
            if len(lines) == max_lines:
                # The line cap leaves a complete suffix when it is already in
                # the bounded buffer or when bytes remain unread.  If the
                # buffer is the true EOF suffix without a newline, it is only
                # a partial producer record and must not be requeued hot.
                has_complete_work = b"\n" in buffered or unread_after_buffer > 0
                partial_line = not has_complete_work and bool(buffered)
            elif read_total == max_bytes:
                if buffered:
                    if unread_after_buffer > 0:
                        # No newline fitted into a whole claim and the record
                        # still continues.  Retrying would make zero progress.
                        raise RepairRequired("source line exceeds bounded tail limit")
                    partial_line = True
                else:
                    has_complete_work = unread_after_buffer > 0
            else:
                partial_line = bool(buffered)
        return TailRead(tuple(lines), next_checkpoint, consumed, has_complete_work, partial_line)
    except RepairRequired as error:
        if str(error) == "source changed while tailing":
            raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error
        raise
    except SourceChanged as error:
        raise RepairRequired("source changed while tailing") from error


@dataclass(frozen=True)
class MaterializedSource:
    """Safe materializer result; only opaque private identifiers are accepted."""

    project_id: str | None = None
    dirty_root_key: str | None = None
    dirty_root_kind: str = "project"
    session_key: str | None = None
    logical_source_key: str | None = None

    def __post_init__(self) -> None:
        if self.project_id is None and self.dirty_root_key is not None:
            raise ValueError("dirty root requires a project")
        if self.dirty_root_kind not in {"project", "task"}:
            raise ValueError("dirty root kind is invalid")


@dataclass(frozen=True)
class SyncRun:
    claimed: int
    completed: int
    repair_required: int
    bytes_processed: int
    lease_acquired: bool = False


Materializer = Callable[[QueueItem, TailRead, object], MaterializedSource]
Reconciler = Callable[[str, tuple[DirtyRoot, ...]], None]


def _utc(value: str) -> str:
    # Repository validation owns canonical timestamp verification.  This helper
    # creates a stable timestamp for callers which omit a clock in production.
    if value:
        return value
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _precise_utc(value: datetime) -> str:
    """Render a canonical UTC timestamp without discarding a short lease."""
    value = value.astimezone(timezone.utc)
    fraction = f"{value.microsecond:06d}".rstrip("0")
    return value.strftime("%Y-%m-%dT%H:%M:%S") + (f".{fraction}" if fraction else "") + "Z"


def rollout_tail_materializer(store: HydraStore) -> Materializer:
    """Build the production safe-facts materializer for normal queued sync.

    It intentionally materializes only normalized lifecycle and token facts.
    Function-call arguments, tool output and prompts never cross this boundary.
    The latest session and turn context are recovered from safe durable keys, so
    an append that does not repeat ``session_meta`` is still attributable.
    """
    hasher = Pseudonymizer.installation(store.database_path.parent)

    def materialize(item: QueueItem, tail: TailRead, connection: object) -> MaterializedSource:
        if item.project_id is None:
            # A hook/backfill may discover a source before it knows a project.
            # Keep its safe checkpoint, but never guess a project from raw cwd.
            return MaterializedSource()
        # Backfill/repair records the legacy logical key. For a hook-created
        # source without one, retain a stable opaque placeholder until a full
        # repair has established the canonical legacy lineage.
        logical = item.logical_source_key or hasher.digest("source", f"pending/{item.root_kind}/{item.source_locator}")
        canonical = connection.execute(
            """SELECT canonical_revision_digest,project_id,session_key
                 FROM rollout_logical_sources WHERE logical_source_key=?""",
            (logical,),
        ).fetchone()
        if canonical is not None and canonical[1] != item.project_id:
            raise ValueError("canonical logical source belongs to another project")
        source_digest = str(canonical[0]) if canonical is not None and canonical[0] is not None else hasher.digest(
            "source", f"incremental/{item.root_kind}/{item.source_locator}",
        )
        session_key = item.session_key
        canonical_session = (
            None
            if canonical is None or canonical[2] is None
            else str(canonical[2])
        )
        if (
            session_key is not None
            and canonical_session is not None
            and session_key != canonical_session
            and canonical[0] is None
        ):
            raise ValueError(
                "provisional logical source belongs to another session"
            )
        if canonical_session is not None and (
            session_key is None or canonical[0] is not None
        ):
            # A canonical legacy lineage is stronger than a source-registry
            # hint. Older hook runtimes wrote their own session namespace into
            # the registry and could poison an otherwise valid source binding.
            # Completing this tail heals that hint atomically at commit.
            session_key = canonical_session
        if session_key is not None:
            # Hooks can bind a source to a safe opaque session before the
            # incremental byte range contains (or still contains) session_meta.
            # Establish the foreign-key parent from that trusted binding so an
            # appended token/lifecycle fact never requires a full-history read.
            inserted_session = connection.execute(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,
                       conversation_key)
                   VALUES (?,?,'incremental',1,?)
                   ON CONFLICT(session_key) DO NOTHING""",
                (session_key, item.project_id, session_key),
            )
            if inserted_session.rowcount:
                connection.execute(
                    """INSERT INTO incremental_session_placeholders(session_key)
                       VALUES (?) ON CONFLICT DO NOTHING""",
                    (session_key,),
                )
            assert_session_project(
                connection,
                session_key=session_key,
                project_id=item.project_id,
            )
        connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
               VALUES (?,?,?,NULL,'clean') ON CONFLICT(logical_source_key) DO UPDATE SET
                 project_id=excluded.project_id,
                 session_key=COALESCE(
                   rollout_logical_sources.session_key,
                   excluded.session_key)""",
            (logical, item.project_id, session_key),
        )
        if session_key is not None:
            connection.execute(
                """INSERT INTO rollout_session_segments(
                       session_key,logical_source_key)
                   VALUES (?,?) ON CONFLICT DO NOTHING""",
                (session_key, logical),
            )
            connection.execute(
                """UPDATE rollout_sessions SET resume_segments=(
                       SELECT COUNT(*) FROM rollout_session_segments
                        WHERE session_key=?)
                     WHERE session_key=?""",
                (session_key, session_key),
            )
        connection.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key,relation,line_count,byte_count,chain_digest,materialized)
               VALUES (?,'jsonl',?,'append',0,0,'',1) ON CONFLICT(source_digest) DO NOTHING""",
            (source_digest, logical),
        )
        location_type = "active" if item.root_kind == "sessions" else "archived"
        connection.execute(
            """INSERT INTO rollout_source_locations(logical_source_key,location_key,location_type,revision_digest)
               VALUES (?,?,?,?) ON CONFLICT(logical_source_key,location_key) DO UPDATE SET
                 revision_digest=excluded.revision_digest""",
            (logical, hasher.digest("source", f"location/{item.root_kind}/{item.source_locator}"), location_type, source_digest),
        )
        turn_row = connection.execute(
            """SELECT fingerprint FROM rollout_events WHERE logical_source_key=? AND envelope_kind='turn_context'
                 ORDER BY source_ordinal DESC LIMIT 1""", (logical,),
        ).fetchone()
        current_turn = None if turn_row is None else str(turn_row[0])
        for line in tail.lines:
            try:
                envelope = json.loads(line.value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
                continue
            kind = envelope.get("type")
            payload = envelope["payload"]
            observed = canonical_timestamp(envelope.get("timestamp"))
            source_fingerprint = line_fingerprint(line.value, hasher.key)
            fingerprint = source_fingerprint
            # This is byte-for-byte the legacy _parse_source event-key recipe.
            event_key = hasher.digest("event", f"{logical}/{line.ordinal}/{source_fingerprint}")
            if kind == "session_meta":
                identity = nonempty_string(payload.get("id"), payload.get("session_id"))
                if identity is not None:
                    parsed_session_key = hasher.digest("identity", identity)
                    if (
                        session_key is not None
                        and parsed_session_key != session_key
                    ):
                        raise ValueError(
                            "session metadata does not match trusted binding"
                        )
                    session_key = parsed_session_key
                    conversation = nonempty_string(payload.get("session_id"), identity)
                    inserted_session = connection.execute(
                        """INSERT INTO rollout_sessions(
                               session_key,project_id,path_key,resume_segments,conversation_key,started_at,last_activity_at)
                           VALUES (?,?, 'incremental',1,?,?,?)
                           ON CONFLICT(session_key) DO NOTHING""",
                        (session_key, item.project_id, hasher.digest("conversation", conversation) if conversation else session_key,
                         observed.text, observed.text),
                    )
                    if inserted_session.rowcount:
                        connection.execute(
                            """INSERT INTO incremental_session_placeholders(
                                   session_key)
                               VALUES (?) ON CONFLICT DO NOTHING""",
                            (session_key,),
                        )
                    assert_session_project(
                        connection,
                        session_key=session_key,
                        project_id=item.project_id,
                    )
                    conversation_key = (
                        hasher.digest("conversation", conversation)
                        if conversation else session_key
                    )
                    connection.execute(
                        """UPDATE rollout_sessions SET
                             conversation_key=CASE WHEN EXISTS (
                               SELECT 1
                                 FROM incremental_session_placeholders marker
                                WHERE marker.session_key=
                                      rollout_sessions.session_key)
                               THEN ? ELSE conversation_key END,
                             started_at=CASE
                               WHEN started_at IS NULL
                                    OR julianday(?) < julianday(started_at)
                               THEN ? ELSE started_at END,
                             last_activity_at=CASE
                               WHEN last_activity_at IS NULL
                                    OR julianday(?) > julianday(last_activity_at)
                               THEN ? ELSE last_activity_at END
                           WHERE session_key=?""",
                        (
                            conversation_key,
                            observed.text, observed.text,
                            observed.text, observed.text,
                            session_key,
                        ),
                    )
                    connection.execute(
                        """UPDATE rollout_logical_sources
                              SET session_key=?,project_id=?
                            WHERE logical_source_key=?""",
                        (session_key, item.project_id, logical),
                    )
                    connection.execute(
                        """INSERT INTO rollout_session_segments(session_key,logical_source_key)
                           VALUES (?,?) ON CONFLICT DO NOTHING""", (session_key, logical),
                    )
                    connection.execute(
                        """UPDATE rollout_sessions SET resume_segments=(
                               SELECT COUNT(*) FROM rollout_session_segments
                                WHERE session_key=?)
                             WHERE session_key=?""",
                        (session_key, session_key),
                    )
            if kind == "turn_context" and isinstance(payload.get("turn_id"), str):
                current_turn = hasher.digest("turn", payload["turn_id"])
                fingerprint = current_turn
            connection.execute(
                """INSERT INTO rollout_event_keys(event_key,source_digest,source_ordinal)
                   VALUES (?,?,?) ON CONFLICT DO NOTHING""", (event_key, source_digest, line.ordinal),
            )
            connection.execute(
                """INSERT INTO rollout_events(
                       event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,timestamp_quality,fingerprint)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (event_key, logical, line.ordinal, kind if isinstance(kind, str) else "unknown",
                 observed.text, observed.quality, fingerprint),
            )
            connection.execute(
                """INSERT INTO rollout_revision_events(revision_digest,event_key,source_ordinal)
                   VALUES (?,?,?) ON CONFLICT DO NOTHING""", (source_digest, event_key, line.ordinal),
            )
            if kind != "event_msg" or session_key is None:
                continue
            event_type = payload.get("type")
            if event_type == "token_count":
                values = parse_usage(payload)
                if values is not None:
                    connection.execute(
                        """INSERT INTO token_snapshots(
                               source_digest,line_number,session_key,project_id,epoch,input_tokens,cached_input_tokens,
                               output_tokens,reasoning_tokens,cache_write_tokens,vendor_total,context_window,completeness,
                               turn_key,observed_at)
                           VALUES (?,?,?,?,0,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                        (source_digest, line.ordinal, session_key, item.project_id, values["input"], values["cached"],
                         values["output"], values["reasoning"], values["cache_write"], values["vendor_total"],
                         values["context_window"], "complete" if values["complete"] else "partial", current_turn, observed.text),
                    )
            elif event_type in {"task_started", "task_complete", "turn_aborted"} and isinstance(payload.get("turn_id"), str):
                turn_key = hasher.digest("turn", payload["turn_id"])
                connection.execute(
                    """INSERT INTO turn_lifecycle_events(
                           event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,emitted_duration_ms,
                           source_digest,logical_source_key,source_ordinal)
                       VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (event_key, session_key, turn_key,
                     {"task_started": "started", "task_complete": "completed", "turn_aborted": "aborted"}[event_type],
                     observed.text, observed.epoch, safe_int(payload.get("duration_ms")), source_digest, logical, line.ordinal),
                )
        return MaterializedSource(item.project_id, session_key=session_key, logical_source_key=logical)

    return materialize


class IncrementalSyncWorker:
    """Lease a small queue batch and tail only the claimed registered source."""

    def __init__(
        self, store: HydraStore, roots: TrustedSourceRoots, *, materialize: Materializer | None = None,
        reconcile: Reconciler | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not callable(clock):
            raise TypeError("incremental sync clock must be callable")
        self.store = store
        self.roots = roots
        self.repository = SyncStateRepository(store)
        self.materialize = materialize or rollout_tail_materializer(store)
        self.reconcile = reconcile
        self.clock = clock

    @contextmanager
    def _lease_heartbeat(
        self, owner_key: str, lease_expires_at: str, item: QueueItem | None = None,
        *, dirty_roots: tuple[DirtyRoot, ...] = (),
        interval_seconds: float | None = None,
    ) -> Iterator[threading.Event]:
        """Renew a held lease while arbitrary parser/reconciler code runs.

        The heartbeat owns a separate SQLite connection so a slow materializer
        cannot starve the renewal behind its transaction. Queue commits still
        require a live lease; dirty-root acknowledgements are instead fenced
        by their immutable per-claim token after a successful reconciliation.
        """
        if item is not None and dirty_roots:
            raise ValueError("heartbeat cannot renew queue and dirty claims together")
        try:
            deadline = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00"))
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            remaining = 0
        # Historical callers supply logical timestamps.  They still retain the
        # old boundary semantics; real short leases get a genuine heartbeat.
        if remaining <= 0:
            yield threading.Event()
            return
        stop = threading.Event()
        lost = threading.Event()
        try:
            reopen = self.store.validated_reopener()
        except Exception:
            lost.set()
            yield lost
            return
        interval = max(0.01, interval_seconds if interval_seconds is not None else remaining / 3)
        def beat() -> None:
            # SQLite connections are thread-affine.  Construct this auxiliary
            # store inside the heartbeat thread, rather than accidentally
            # catching ProgrammingError forever on a connection opened by the
            # invoking MCP/dashboard thread.
            auxiliary: HydraStore | None = None
            try:
                auxiliary = reopen()
                # A materializer may hold SQLite's single writer lock.  A
                # heartbeat must retry on the next cadence, not wait through
                # the normal five-second contention timeout.
                auxiliary.connection.execute("PRAGMA busy_timeout = 25")
                repository = SyncStateRepository(auxiliary)
                while not stop.wait(interval):
                    observed_now = datetime.now(timezone.utc)
                    observed = _precise_utc(observed_now)
                    renewed_expiry = _precise_utc(
                        observed_now + timedelta(seconds=remaining),
                    )
                    try:
                        if item is not None:
                            held = repository.renew_claim(
                                owner_key, item.root_kind, item.source_locator,
                                observed, renewed_expiry,
                            )
                        elif dirty_roots:
                            held = repository.renew_dirty_claims(
                                owner_key, dirty_roots,
                                observed, renewed_expiry,
                            )
                        else:
                            held = repository.acquire_lease(
                                owner_key, observed, renewed_expiry,
                            )
                    except (OSError, sqlite3.Error):
                        # Busy means our own in-flight atomic materialization
                        # owns the writer lock; retry on the next heartbeat.
                        continue
                    except ValueError:
                        held = False
                    if not held:
                        if dirty_roots:
                            lost.set()
                            return
                        # A false claim renewal can race the successful queue
                        # ack. Only a read-confirmed owner change is terminal.
                        try:
                            still_owned = repository.lease_owned(owner_key, observed)
                        except sqlite3.Error:
                            continue
                        if not still_owned:
                            lost.set()
                            return
            except Exception:
                lost.set()
            finally:
                if auxiliary is not None:
                    auxiliary.close()

        thread = threading.Thread(target=beat, name="hydra-sync-lease", daemon=True)
        thread.start()
        try:
            yield lost
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval * 2))

    def reconcile_dirty(
        self,
        owner_key: str,
        observed_at: str,
        lease_expires_at: str,
        *,
        current_time: Callable[[], str] | None = None,
    ) -> int:
        """Reconcile only roots claimed by this worker, never the full catalog.

        The current conservative boundary is project-level: task dirty markers
        are grouped into their project because the legacy reconciler rebuilds a
        project atomically. Claimed markers remain for retry if reconciliation
        raises, so an outage cannot silently make data appear fresh.
        """
        if self.reconcile is None:
            return 0
        roots = self.repository.claim_dirty_roots(owner_key, observed_at, lease_expires_at)
        if not roots:
            return 0
        completed = 0
        fresh = current_time or (lambda: observed_at)
        for project_id in sorted({root.project_id for root in roots}):
            project_roots = tuple(root for root in roots if root.project_id == project_id)
            with self._lease_heartbeat(
                owner_key, lease_expires_at,
                dirty_roots=project_roots,
            ) as _lost:
                self.reconcile(project_id, project_roots)
            acknowledged_at = fresh()
            completed += self.repository.acknowledge_dirty_roots(
                owner_key, project_roots, acknowledged_at,
            )
        return completed

    def _commit(
        self, item: QueueItem, tail: TailRead, result: MaterializedSource,
        owner_key: str, current_time: Callable[[], str],
        ownership_lost: threading.Event | None = None,
        job_id: str | None = None,
    ) -> bool:
        """Commit parser facts, checkpoint, dirty marker and queue ack together."""
        with self.store.rollout_transaction() as connection:
            observed_at = current_time()
            # A stale/dead worker cannot commit over the next lease holder.
            owned = connection.execute(
                """SELECT requeue_pending FROM sync_ingest_queue WHERE root_kind=? AND source_locator=?
                     AND queue_state='claimed' AND claimed_by=?
                     AND hydra_rfc3339_micros(claim_expires_at)
                         >hydra_rfc3339_micros(?)""",
                (item.root_kind, item.source_locator, owner_key, observed_at),
            ).fetchone()
            if owned is None:
                raise RepairRequired("queue claim expired")
            try:
                result = self.materialize(item, tail, connection)
            except ValueError as error:
                raise _MaterializationRejected from error
            if ownership_lost is not None and ownership_lost.is_set():
                # The materializer's writes are still inside this transaction;
                # raising here rolls them back before a successor can observe
                # any facts or a durable checkpoint.
                raise RepairRequired("worker lease lost")
            observed_at = current_time()
            owned = connection.execute(
                """SELECT requeue_pending FROM sync_ingest_queue
                     WHERE root_kind=? AND source_locator=?
                       AND queue_state='claimed' AND claimed_by=?
                       AND hydra_rfc3339_micros(claim_expires_at)
                           >hydra_rfc3339_micros(?)""",
                (
                    item.root_kind, item.source_locator,
                    owner_key, observed_at,
                ),
            ).fetchone()
            if owned is None:
                raise RepairRequired("queue claim expired")
            if result.project_id is not None:
                # Keep incremental facts in the same derived-state shape as a
                # legacy ingest batch before exposing the project as dirty.
                materialize_test_evidence(connection, result.project_id)
                reconcile_test_retries(connection, result.project_id)
                if result.session_key is not None:
                    refresh_token_session_selection(
                        connection,
                        result.project_id,
                        result.session_key,
                    )
                diagnose = lambda _source, _ordinal, _kind: None
                reconcile_token_epochs(connection, result.project_id, diagnose)
                reconcile_fork_baselines(connection, result.project_id)
                reconcile_turn_attempts(
                    connection,
                    diagnose,
                    project_id=result.project_id,
                )
            if ownership_lost is not None and ownership_lost.is_set():
                raise RepairRequired("worker lease lost")
            checkpoint = tail.checkpoint
            connection.execute(
                """INSERT INTO sync_source_checkpoints(
                       root_kind,source_locator,device_id,inode,file_size,byte_offset,line_number,
                       prefix_anchor,revision_anchor,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(root_kind,source_locator) DO UPDATE SET
                     device_id=excluded.device_id,inode=excluded.inode,file_size=excluded.file_size,
                     byte_offset=excluded.byte_offset,line_number=excluded.line_number,
                     prefix_anchor=excluded.prefix_anchor,revision_anchor=excluded.revision_anchor,
                     updated_at=excluded.updated_at""",
                (item.root_kind, item.source_locator, checkpoint.device_id, checkpoint.inode,
                 checkpoint.file_size, checkpoint.byte_offset, checkpoint.line_number,
                 checkpoint.prefix_anchor, checkpoint.revision_anchor, observed_at),
            )
            if result.project_id is not None:
                connection.execute(
                    """UPDATE sync_source_registry SET project_id=COALESCE(project_id,?),
                           session_key=COALESCE(?,session_key),logical_source_key=COALESCE(?,logical_source_key),last_seen_at=?
                         WHERE root_kind=? AND source_locator=?""",
                    (result.project_id, result.session_key, result.logical_source_key,
                     observed_at, item.root_kind, item.source_locator),
                )
                root_key = result.dirty_root_key or result.project_id
                connection.execute(
                    """INSERT INTO sync_dirty_roots(project_id,root_key,root_kind,observed_at,claim_owner,claim_expires_at,eligible_epoch_ns)
                       VALUES (?,?,?,?,NULL,NULL,?)
                       ON CONFLICT(project_id,root_key,root_kind) DO UPDATE SET
                         observed_at=excluded.observed_at,claim_owner=NULL,
                         claim_expires_at=NULL,
                         claim_token=NULL,
                         eligible_epoch_ns=excluded.eligible_epoch_ns""",
                    (
                        result.project_id, root_key, result.dirty_root_kind,
                        observed_at, _epoch_nanoseconds(observed_at),
                    ),
                )
            requeued = tail.has_complete_work or bool(owned[0])
            if requeued:
                connection.execute(
                    """UPDATE sync_ingest_queue SET queue_state='queued',available_at=?,claimed_by=NULL,
                           claimed_at=NULL,claim_expires_at=NULL,requeue_pending=0,
                           reason_code=NULL,eligible_epoch_ns=?
                         WHERE root_kind=? AND source_locator=?""",
                    (
                        observed_at, _epoch_nanoseconds(observed_at),
                        item.root_kind, item.source_locator,
                    ),
                )
            else:
                connection.execute(
                    """DELETE FROM sync_ingest_queue WHERE root_kind=? AND source_locator=?
                         AND queue_state='claimed' AND claimed_by=?""",
                    (item.root_kind, item.source_locator, owner_key),
                )
            if job_id is not None:
                remaining_sources = int(connection.execute(
                    """SELECT ingest_total FROM sync_work_summary
                        WHERE singleton=1""",
                ).fetchone()[0])
                self.repository.advance_job(
                    job_id,
                    sources_completed_delta=int(not requeued),
                    bytes_processed_delta=tail.bytes_read,
                    repair_required_delta=0,
                    remaining_sources=remaining_sources,
                    updated_at=observed_at,
                    owner_key=owner_key,
                )
            self.repository._bump_revision(connection, observed_at)
            return not requeued

    def sync_once(
        self, owner_key: str, observed_at: str, lease_expires_at: str, *,
        maximum_sources: int = 100, crash_after_materialize: bool = False,
        crash_after_outbox_consume: bool = False,
        release_lease: bool = True,
        job_id: str | None = None,
    ) -> SyncRun:
        if not 1 <= maximum_sources <= 1000:
            raise ValueError("maximum_sources must be between 1 and 1000")
        if not isinstance(release_lease, bool):
            raise TypeError("release_lease must be a boolean")
        if job_id is not None:
            job = self.repository.get_job(job_id)
            if (
                job is None
                or job.job_kind != "sync"
                or job.state != "running"
            ):
                raise ValueError("normal sync progress requires a running sync job")
        now = _utc(observed_at)
        requested_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
        requested_deadline = datetime.fromisoformat(
            lease_expires_at.replace("Z", "+00:00"),
        )
        lease_seconds = max(
            0.01, (requested_deadline - requested_at).total_seconds(),
        )
        use_wall_clock = requested_deadline > self.clock()

        def lease_window() -> tuple[str, str]:
            if not use_wall_clock:
                return now, lease_expires_at
            observed = self.clock()
            return (
                _precise_utc(observed),
                _precise_utc(
                    observed + timedelta(seconds=lease_seconds),
                ),
            )

        def current_time() -> str:
            return lease_window()[0]

        def quarantine_for_repair(item: QueueItem) -> bool:
            repair_at, _ = lease_window()
            with self.store.rollout_transaction() as connection:
                quarantined = self.repository.quarantine_claim(
                    owner_key,
                    item.root_kind,
                    item.source_locator,
                    repair_at,
                )
                if not quarantined:
                    return False
                if job_id is not None:
                    remaining_sources = int(connection.execute(
                        """SELECT ingest_total FROM sync_work_summary
                            WHERE singleton=1""",
                    ).fetchone()[0])
                    self.repository.advance_job(
                        job_id,
                        sources_completed_delta=0,
                        bytes_processed_delta=0,
                        repair_required_delta=1,
                        remaining_sources=remaining_sources,
                        updated_at=repair_at,
                        owner_key=owner_key,
                    )
                return True

        lease_now, active_expiry = lease_window()
        if not self.repository.acquire_lease(
            owner_key, lease_now, active_expiry,
        ):
            return SyncRun(0, 0, 0, 0, False)
        claimed = completed = repairs = processed = 0
        for _ in range(maximum_sources):
            claim_now, claim_expiry = lease_window()
            if not self.repository.acquire_lease(
                owner_key, claim_now, claim_expiry,
            ):
                break
            item = self.repository.claim_next(
                owner_key, claim_now, claim_expiry,
            )
            if item is None:
                break
            claimed += 1
            try:
                tail = read_incremental_source(
                    self.roots, item.root_kind, item.source_locator,
                    self.repository.checkpoint_for(item.root_kind, item.source_locator),
                )
            except SourceChanged:
                retry_at, _ = lease_window()
                self.repository.retry_claim(
                    owner_key, item.root_kind, item.source_locator,
                    reason_code="source_changed",
                    available_at=retry_at,
                    observed_at=retry_at,
                )
                continue
            except RepairRequired:
                repairs += int(quarantine_for_repair(item))
                continue
            if item.project_id is None:
                # Without a trusted project binding, advancing the durable
                # offset would irreversibly discard facts we cannot attribute.
                self.repository.retry_claim(
                    owner_key, item.root_kind, item.source_locator,
                    reason_code="unattributed",
                    available_at=claim_expiry,
                    observed_at=claim_now,
                )
                continue
            # SQLite permits only one writer.  Give a materialization
            # transaction a conservative initial lease window, then keep
            # heartbeating from a separate connection whenever the write lock
            # is free.  Without this extension an otherwise healthy, slow
            # parser could block the heartbeat behind its own transaction.
            live_lease = use_wall_clock
            protected_expiry = (
                _precise_utc(max(
                    datetime.fromisoformat(
                        claim_expiry.replace("Z", "+00:00"),
                    ),
                    self.clock() + timedelta(seconds=60),
                ))
                if live_lease else claim_expiry
            )
            if not self.repository.renew_claim(
                owner_key, item.root_kind, item.source_locator,
                claim_now, protected_expiry,
            ):
                # A competing owner or expired lease won before materialization;
                # leave the queue item for normal expiry/reclaim semantics.
                continue
            if crash_after_materialize:
                # Test-only fault point: the queue claim survives and its
                # expiry causes an idempotent replay after a process restart.
                with self.store.rollout_transaction() as connection:
                    self.materialize(item, tail, connection)
                raise RuntimeError("injected crash after materialize")
            try:
                requested_seconds = lease_seconds
                heartbeat = (
                    self._lease_heartbeat(
                        owner_key, protected_expiry, item,
                        interval_seconds=requested_seconds / 3,
                    ) if live_lease else nullcontext(threading.Event())
                )
                with heartbeat as lost:
                    if lost.is_set():
                        raise RepairRequired("worker lease lost")
                    drained = self._commit(
                        item, tail, MaterializedSource(),
                        owner_key, current_time, lost, job_id,
                    )
            except _MaterializationRejected:
                # Trusted identity/lineage validation is deterministic for this
                # binding. Retrying the same bytes would hot-loop, so quarantine
                # only this source and leave explicit repair to re-establish it.
                repairs += int(quarantine_for_repair(item))
                continue
            except RepairRequired:
                retry_at, retry_expiry = lease_window()
                self.repository.retry_claim(
                    owner_key, item.root_kind, item.source_locator,
                    reason_code="lease_lost",
                    available_at=retry_expiry,
                    observed_at=retry_at,
                )
                continue
            except (OSError, sqlite3.OperationalError, SourceChanged, RuntimeError):
                # The materializer transaction rolled back; release this claim
                # for a bounded retry rather than relying on process death.
                retry_at, retry_expiry = lease_window()
                self.repository.retry_claim(
                    owner_key, item.root_kind, item.source_locator,
                    reason_code="transient_failure",
                    available_at=retry_expiry,
                    observed_at=retry_at,
                )
                continue
            completed += int(drained)
            processed += tail.bytes_read
        hook_now, hook_expiry = lease_window()
        self.repository.acquire_lease(
            owner_key, hook_now, hook_expiry,
        )
        hook_events = self.repository.claim_hook_events(
            owner_key, hook_now, hook_expiry,
        )
        if crash_after_outbox_consume:
            # Test-only fault point.  The safe facts have been projected into
            # durable dirty roots, but remain unacknowledged for an idempotent
            # replay once this lease expires.
            raise RuntimeError("injected crash after outbox consume")
        if hook_events:
            acknowledged_at, _ = lease_window()
            self.repository.acknowledge_hook_events(
                owner_key,
                tuple(event.event_key for event in hook_events),
                acknowledged_at,
            )
        reconcile_now, reconcile_expiry = lease_window()
        self.repository.acquire_lease(
            owner_key, reconcile_now, reconcile_expiry,
        )
        self.reconcile_dirty(
            owner_key, reconcile_now, reconcile_expiry,
            current_time=current_time,
        )
        if release_lease:
            self.repository.release_lease(owner_key, current_time())
        return SyncRun(claimed, completed, repairs, processed, True)


@dataclass(frozen=True)
class RepairRun:
    discovered: int
    directories_scanned: int
    completed: bool
    lease_acquired: bool = True


class ResumableRepair:
    """Explicit bounded repair/backfill; this is the only directory walker."""

    _ROOT_MARKER = "@root"
    _FILE_PREFIX = "@file/"

    def __init__(
        self,
        store: HydraStore,
        roots: TrustedSourceRoots,
        *,
        lease_ttl_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not 1 <= lease_ttl_seconds <= 3600:
            raise ValueError("repair lease TTL must be between 1 and 3600 seconds")
        if not callable(clock):
            raise TypeError("repair clock must be callable")
        self.store = store
        self.roots = roots
        self.repository = SyncStateRepository(store)
        self.lease_ttl_seconds = lease_ttl_seconds
        self.clock = clock

    @contextmanager
    def _lease_heartbeat(self, owner_key: str, lease_expires_at: str) -> Iterator[threading.Event]:
        """Use the generic separate-connection heartbeat for repair I/O.

        Repair does directory enumeration, full legacy materialization and
        reconciliation outside one SQLite transaction.  Reusing the no-queue
        heartbeat keeps the singleton owner alive through each slow phase.
        """
        worker = IncrementalSyncWorker(self.store, self.roots)
        with worker._lease_heartbeat(owner_key, lease_expires_at) as lost:
            yield lost

    def start_backfill(self, observed_at: str, *, job_kind: str = "backfill") -> str:
        """Start or resume the explicit full-history path, never normal sync."""
        if job_kind not in {"backfill", "repair"}:
            raise ValueError("backfill job kind is invalid")
        job_id, _reused = self.repository.get_or_create_active_job(
            job_kind, observed_at,
        )
        return job_id

    def start(self, observed_at: str) -> str:
        """Compatibility spelling for an explicit repair-history operation."""
        return self.start_backfill(observed_at, job_kind="repair")

    def _full_materialize(self, root_kind: str, locator: str, observed_at: str) -> bool:
        """Legacy-equivalent full ingest for one explicit repair/backfill file."""
        validate_root_relative_locator(locator)
        # This path is an opaque, root-relative label passed through legacy
        # APIs.  All byte access below goes via ``source_opener``.
        path = self.roots.root_for(root_kind) / locator
        opener = self.roots.source_opener(root_kind, locator)
        hasher = Pseudonymizer.installation(self.store.database_path.parent)
        scan = scan_source(path, hasher.key, hasher.digest, opener=opener)
        materialized_scan = scan.complete_prefix()
        if materialized_scan.identity is None or materialized_scan.cwd is None:
            self.repository.register_source(
                root_kind=root_kind, source_locator=locator, observed_at=observed_at,
            )
            self.repository.mark_repair_required(
                root_kind, locator, observed_at, discard_queue=True,
            )
            return False
        try:
            project = resolve_project(materialized_scan.cwd)
        except (ProjectNotFound, OSError, TypeError, ValueError):
            self.repository.register_source(
                root_kind=root_kind, source_locator=locator, observed_at=observed_at,
            )
            self.repository.mark_repair_required(
                root_kind, locator, observed_at, discard_queue=True,
            )
            return False
        self.repository.observe_project(
            project_id=project.project_id,
            display_name=project.display_name,
            display_name_provenance=project.display_name_provenance,
            observed_at=observed_at,
        )
        label = "active" if root_kind == "sessions" else "archived"
        # ``prepared_scans`` makes legacy ingest use exactly this validated
        # source prefix; no directory discovery, raw transcript copy or global
        # walk is reachable here. The opener authenticates the full descriptor
        # but caps parser bytes at the newline-complete scan boundary.
        ingest_rollouts(
            self.store, (RolloutRoot(path, label),), project.project_root, project.project_id,
            hash_key=hasher.key, prepared_scans={path: materialized_scan},
            source_opener=self.roots.source_opener(
                root_kind, locator, byte_limit=materialized_scan.byte_count,
            ),
        )
        current_digest = hasher.digest(
            "source", f"revision/{project.project_id}/{materialized_scan.revision_digest}",
        )
        current = self.store.connection.execute(
            """SELECT r.materialized,l.lineage_state,l.canonical_revision_digest
                 FROM rollout_sources AS r JOIN rollout_logical_sources AS l
                   ON l.logical_source_key=r.logical_source_key
                WHERE r.source_digest=? AND l.project_id=?""",
            (current_digest, project.project_id),
        ).fetchone()
        if current is None or not int(current[0]) or current[1] != "clean" or current[2] != current_digest:
            self.repository.register_source(
                root_kind=root_kind, source_locator=locator, project_id=project.project_id,
                observed_at=observed_at,
            )
            self.repository.mark_repair_required(
                root_kind, locator, observed_at, discard_queue=True,
            )
            return False
        location_key = hasher.digest("source", str(path))
        row = self.store.connection.execute(
            """SELECT l.logical_source_key,l.session_key FROM rollout_source_locations AS x
                 JOIN rollout_logical_sources AS l ON l.logical_source_key=x.logical_source_key
                WHERE x.location_key=? AND l.project_id=?""",
            (location_key, project.project_id),
        ).fetchone()
        if row is None or row[1] is None:
            # Preserve the full ingest's unresolved/quarantine behavior, and
            # never checkpoint it as though attribution were complete.
            self.repository.register_source(
                root_kind=root_kind, source_locator=locator, project_id=project.project_id,
                observed_at=observed_at,
            )
            self.repository.mark_repair_required(
                root_kind, locator, observed_at, discard_queue=True,
            )
            return False
        checkpoint = _full_scan_checkpoint(self.roots, root_kind, locator, scan)
        with self.store.rollout_transaction() as connection:
            self.repository._register(
                connection, root_kind=root_kind, source_locator=locator, project_id=project.project_id,
                logical_source_key=str(row[0]), session_key=str(row[1]), observed_at=observed_at,
            )
            connection.execute(
                """UPDATE sync_source_registry SET source_state='ready' WHERE root_kind=? AND source_locator=?""",
                (root_kind, locator),
            )
            connection.execute(
                """INSERT INTO sync_source_checkpoints(
                       root_kind,source_locator,device_id,inode,file_size,byte_offset,line_number,
                       prefix_anchor,revision_anchor,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(root_kind,source_locator) DO UPDATE SET
                     device_id=excluded.device_id,inode=excluded.inode,file_size=excluded.file_size,
                     byte_offset=excluded.byte_offset,line_number=excluded.line_number,
                     prefix_anchor=excluded.prefix_anchor,revision_anchor=excluded.revision_anchor,
                     updated_at=excluded.updated_at""",
                (root_kind, locator, checkpoint.device_id, checkpoint.inode, checkpoint.file_size,
                 checkpoint.byte_offset, checkpoint.line_number, checkpoint.prefix_anchor,
                 checkpoint.revision_anchor, observed_at),
            )
            self.repository._bump_revision(connection, observed_at)
        self.repository.mark_dirty(project.project_id, project.project_id, "project", observed_at)
        return True

    def repair_source(self, root_kind: str, source_locator: str, observed_at: str) -> bool:
        """Repair exactly one known source; no root scan is performed."""
        return self._full_materialize(root_kind, source_locator, observed_at)

    @contextmanager
    def _directory(self, root_kind: str, locator: str) -> Iterator[int]:
        """Yield a held directory descriptor rooted in one trusted source tree."""
        with self.roots.open_directory(root_kind, locator) as directory:
            yield directory

    def _lease_window(self) -> tuple[str, str]:
        """Return a fresh wall-clock lease window for one repair operation."""
        observed = self.clock()
        return (
            _precise_utc(observed),
            _precise_utc(
                observed + timedelta(seconds=self.lease_ttl_seconds),
            ),
        )

    def _renew_lease(self, owner_key: str) -> tuple[str, str] | None:
        observed_at, expires_at = self._lease_window()
        if not self.repository.acquire_lease(
            owner_key, observed_at, expires_at,
        ):
            return None
        return observed_at, expires_at

    def run_batch(self, job_id: str, observed_at: str, *, directory_limit: int = 100) -> RepairRun:
        if not 1 <= directory_limit <= 1000:
            raise ValueError("directory_limit must be between 1 and 1000")
        job = self.repository.get_job(job_id)
        if job is None:
            raise KeyError("repair job is unknown")
        if job.job_kind not in {"repair", "backfill"}:
            if job.state in {"queued", "running"}:
                return RepairRun(0, 0, False, lease_acquired=False)
            raise KeyError("repair job is unknown")
        # A job identifies durable work, not a process.  An invocation needs a
        # fresh lease identity so a second dashboard/MCP call cannot renew the
        # first call's singleton lease merely by knowing the job id.
        owner = f"repair-{uuid.uuid4().hex}"
        lease_observed_at, expiry = self._lease_window()
        # Directory and file frontiers share the singleton ingest lease: a
        # second dashboard/MCP worker observes progress but does no I/O.
        if not self.repository.acquire_lease(
            owner, lease_observed_at, expiry,
        ):
            return RepairRun(0, 0, False, lease_acquired=False)
        job = self.repository.get_job(job_id)
        if job is None:
            self.repository.release_lease(owner, lease_observed_at)
            raise KeyError("repair job is unknown")
        if job.state in {"succeeded", "partial", "failed"}:
            self.repository.release_lease(owner, lease_observed_at)
            return RepairRun(0, 0, True)
        discovered = directories = 0
        lease_lost = False
        try:
            for root_kind in ("sessions", "archived_sessions"):
                try:
                    self.roots.root_for(root_kind)
                except RepairRequired:
                    continue
                if not self.repository.ensure_root_frontier_if_owned(
                    job_id=job_id,
                    root_kind=root_kind,
                    updated_at=observed_at,
                    owner_key=owner,
                    lease_observed_at=lease_observed_at,
                ):
                    lease_lost = True
                    break
            if lease_lost:
                return RepairRun(discovered, directories, False)
            pending = self.repository.resume_frontier(job_id)[:directory_limit]
            started = self.repository.refresh_job_from_frontier_if_owned(
                job_id, owner_key=owner,
                lease_observed_at=lease_observed_at, state="running",
                updated_at=observed_at,
            )
            if started is None:
                return RepairRun(0, 0, False)
            for frontier in pending:
                lease_window = self._renew_lease(owner)
                if lease_window is None:
                    lease_lost = True
                    break
                lease_observed_at, expiry = lease_window
                if frontier.directory_locator.startswith(self._FILE_PREFIX):
                    locator = frontier.directory_locator.removeprefix(self._FILE_PREFIX)
                    try:
                        with self._lease_heartbeat(owner, expiry) as lost:
                            materialized = self._full_materialize(frontier.root_kind, locator, observed_at)
                        if lost.is_set():
                            # The durable frontier remains pending; a later
                            # owner may safely retry the idempotent full scan.
                            lease_lost = True
                            break
                    except (OSError, RepairRequired, SourceChanged):
                        lease_window = self._renew_lease(owner)
                        if lease_window is None:
                            lease_lost = True
                            break
                        lease_observed_at, expiry = lease_window
                        if not self.repository.save_frontier_if_owned(
                            job_id=job_id, root_kind=frontier.root_kind,
                            directory_locator=frontier.directory_locator, state="repair_required",
                            discovered_count=1, updated_at=observed_at,
                            owner_key=owner,
                            lease_observed_at=lease_observed_at,
                        ):
                            lease_lost = True
                            break
                        continue
                    if materialized:
                        details = self.repository.checkpoint_for(frontier.root_kind, locator)
                        lease_window = self._renew_lease(owner)
                        if lease_window is None:
                            lease_lost = True
                            break
                        lease_observed_at, expiry = lease_window
                        if not self.repository.save_frontier_if_owned(
                            job_id=job_id, root_kind=frontier.root_kind,
                            directory_locator=frontier.directory_locator, state="scanned",
                            discovered_count=1, updated_at=observed_at,
                            owner_key=owner,
                            lease_observed_at=lease_observed_at,
                        ):
                            lease_lost = True
                            break
                    else:
                        lease_window = self._renew_lease(owner)
                        if lease_window is None:
                            lease_lost = True
                            break
                        lease_observed_at, expiry = lease_window
                        if not self.repository.save_frontier_if_owned(
                            job_id=job_id, root_kind=frontier.root_kind,
                            directory_locator=frontier.directory_locator, state="repair_required",
                            discovered_count=1, updated_at=observed_at,
                            owner_key=owner,
                            lease_observed_at=lease_observed_at,
                        ):
                            lease_lost = True
                            break
                    continue
                try:
                    children: list[tuple[str, int]] = []
                    directory_discovered = 0
                    with self._lease_heartbeat(owner, expiry) as lost:
                        with self._directory(
                            frontier.root_kind,
                            frontier.directory_locator,
                        ) as directory:
                            with os.scandir(directory) as entries:
                                for entry in entries:
                                    try:
                                        details = entry.stat(
                                            follow_symlinks=False,
                                        )
                                    except OSError:
                                        continue
                                    if stat.S_ISLNK(details.st_mode):
                                        continue
                                    relative = (
                                        entry.name
                                        if frontier.directory_locator
                                        == self._ROOT_MARKER
                                        else
                                        f"{frontier.directory_locator}/{entry.name}"
                                    )
                                    if stat.S_ISDIR(details.st_mode):
                                        children.append((relative, 0))
                                    elif (
                                        stat.S_ISREG(details.st_mode)
                                        and entry.name.endswith(".jsonl")
                                    ):
                                        children.append(
                                            (self._FILE_PREFIX + relative, 1),
                                        )
                                        directory_discovered += 1
                    if lost.is_set():
                        lease_lost = True
                        break
                    for child_locator, child_discovered in children:
                        lease_window = self._renew_lease(owner)
                        if lease_window is None:
                            lease_lost = True
                            break
                        lease_observed_at, expiry = lease_window
                        if not self.repository.save_frontier_if_owned(
                            job_id=job_id,
                            root_kind=frontier.root_kind,
                            directory_locator=child_locator,
                            state="pending",
                            discovered_count=child_discovered,
                            updated_at=observed_at,
                            owner_key=owner,
                            lease_observed_at=lease_observed_at,
                        ):
                            lease_lost = True
                            break
                    if lease_lost:
                        break
                    lease_window = self._renew_lease(owner)
                    if lease_window is None:
                        lease_lost = True
                        break
                    lease_observed_at, expiry = lease_window
                    if not self.repository.save_frontier_if_owned(
                        job_id=job_id, root_kind=frontier.root_kind, directory_locator=frontier.directory_locator,
                        state="scanned", discovered_count=directory_discovered, updated_at=observed_at,
                        owner_key=owner,
                        lease_observed_at=lease_observed_at,
                    ):
                        lease_lost = True
                        break
                    discovered += directory_discovered
                    directories += 1
                except RepairRequired:
                    lease_window = self._renew_lease(owner)
                    if lease_window is None:
                        lease_lost = True
                        break
                    lease_observed_at, expiry = lease_window
                    if not self.repository.save_frontier_if_owned(
                        job_id=job_id, root_kind=frontier.root_kind, directory_locator=frontier.directory_locator,
                        state="repair_required", discovered_count=0, updated_at=observed_at,
                        owner_key=owner,
                        lease_observed_at=lease_observed_at,
                    ):
                        lease_lost = True
                        break
            if lease_lost:
                return RepairRun(discovered, directories, False)
            pending_after = self.repository.resume_frontier(job_id)
            complete = not pending_after
            partial = bool(self.repository.list_frontier(job_id, "repair_required"))
            reconciliation_settled = False
            if complete:
                lease_window = self._renew_lease(owner)
                if lease_window is None:
                    return RepairRun(discovered, directories, False)
                lease_observed_at, expiry = lease_window
                dirty = self.repository.claim_dirty_roots(
                    owner, lease_observed_at, expiry,
                )
                for project_id in sorted({root.project_id for root in dirty}):
                    group = tuple(root for root in dirty if root.project_id == project_id)
                    lease_window = self._renew_lease(owner)
                    if lease_window is None:
                        lease_lost = True
                        break
                    lease_observed_at, expiry = lease_window
                    with self._lease_heartbeat(owner, expiry) as _lost:
                        reconcile_project(
                            self.store,
                            project_id,
                            Pseudonymizer.installation(
                                self.store.database_path.parent,
                            ).key,
                            expected_dirty_roots=group,
                        )
                    acknowledged_at, _ = self._lease_window()
                    self.repository.acknowledge_dirty_roots(
                        owner, group, acknowledged_at,
                    )
                reconciliation_settled = not self.repository.list_dirty_roots()
            finished = complete and reconciliation_settled
            if lease_lost:
                return RepairRun(discovered, directories, False)
            lease_window = self._renew_lease(owner)
            if lease_window is None:
                return RepairRun(discovered, directories, False)
            lease_observed_at, expiry = lease_window
            persisted = self.repository.refresh_job_from_frontier_if_owned(
                job_id, owner_key=owner,
                lease_observed_at=lease_observed_at,
                state=(
                    "partial"
                    if finished and partial
                    else "succeeded"
                    if finished
                    else "running"
                ),
                updated_at=observed_at,
                completed_at=observed_at if finished else None,
            )
            if persisted is None:
                return RepairRun(discovered, directories, False)
            return RepairRun(
                discovered,
                directories,
                persisted.state in {"succeeded", "partial"},
            )
        finally:
            released_at, _ = self._lease_window()
            self.repository.release_lease(owner, released_at)
