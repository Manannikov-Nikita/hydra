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

from .project import ProjectNotFound, resolve_project
from .reconcile_engine import reconcile_project
from .rollout import ingest_rollouts
from .rollout_identity import Pseudonymizer, RolloutRoot
from .rollout_observations import safe_int, usage as parse_usage
from .rollout_privacy import canonical_timestamp, nonempty_string
from .rollout_reconcile import reconcile_fork_baselines, reconcile_token_epochs, reconcile_turn_attempts
from .rollout_sources import SOURCE_CHANGED_MESSAGE, SourceChanged, SourceStat, line_fingerprint, open_source, scan_source, source_stat
from .storage import HydraStore
from .sync_state import QueueItem, SourceCheckpoint, SyncStateRepository, validate_root_relative_locator
from .test_evidence import reconcile_test_evidence
from .token_selection import refresh_token_source_selection


_PREFIX_BYTES = 256
_BOUNDARY_BYTES = 256


class RepairRequired(RuntimeError):
    """One registered source no longer has an append-only relationship."""


@dataclass(frozen=True)
class TrustedSourceRoots:
    sessions: Path
    archived_sessions: Path

    def root_for(self, root_kind: str) -> Path:
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
        return value

    @contextmanager
    def open_held(self, root_kind: str, locator: str) -> Iterator[tuple[BinaryIO, SourceStat]]:
        """Open a regular source by descriptor-relative traversal, fail closed.

        Each component is opened from the directory descriptor that named it;
        a parent swap to a symlink after validation therefore cannot redirect
        the final open. The returned descriptor remains held while its caller
        validates anchors and reads the append.
        """
        relative = validate_root_relative_locator(locator)
        root = self.root_for(root_kind)
        directory: int | None = None
        source: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory = os.open(root, flags)
            if not stat.S_ISDIR(os.fstat(directory).st_mode):
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
    except RepairRequired:
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
Reconciler = Callable[[str], None]


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
            "SELECT canonical_revision_digest FROM rollout_logical_sources WHERE logical_source_key=?", (logical,),
        ).fetchone()
        source_digest = str(canonical[0]) if canonical is not None and canonical[0] is not None else hasher.digest(
            "source", f"incremental/{item.root_kind}/{item.source_locator}",
        )
        session_key = item.session_key
        row = connection.execute(
            "SELECT session_key FROM rollout_logical_sources WHERE logical_source_key=?", (logical,),
        ).fetchone()
        if session_key is None and row is not None and row[0] is not None:
            session_key = str(row[0])
        connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
               VALUES (?,?,?,NULL,'clean') ON CONFLICT(logical_source_key) DO UPDATE SET
                 project_id=excluded.project_id""",
            (logical, item.project_id, session_key),
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
                    session_key = hasher.digest("identity", identity)
                    conversation = nonempty_string(payload.get("session_id"), identity)
                    connection.execute(
                        """INSERT INTO rollout_sessions(
                               session_key,project_id,path_key,resume_segments,conversation_key,started_at,last_activity_at)
                           VALUES (?,?, 'incremental',1,?,?,?) ON CONFLICT(session_key) DO UPDATE SET
                             last_activity_at=COALESCE(excluded.last_activity_at,rollout_sessions.last_activity_at)""",
                        (session_key, item.project_id, hasher.digest("conversation", conversation) if conversation else session_key,
                         observed.text, observed.text),
                    )
                    connection.execute(
                        "UPDATE rollout_logical_sources SET session_key=? WHERE logical_source_key=?", (session_key, logical),
                    )
                    connection.execute(
                        """INSERT INTO rollout_session_segments(session_key,logical_source_key)
                           VALUES (?,?) ON CONFLICT DO NOTHING""", (session_key, logical),
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
    ) -> None:
        self.store = store
        self.roots = roots
        self.repository = SyncStateRepository(store)
        self.materialize = materialize or rollout_tail_materializer(store)
        self.reconcile = reconcile

    @contextmanager
    def _lease_heartbeat(
        self, owner_key: str, lease_expires_at: str, item: QueueItem | None = None,
        *, interval_seconds: float | None = None,
    ) -> Iterator[threading.Event]:
        """Renew a held lease while arbitrary parser/reconciler code runs.

        The heartbeat owns a separate SQLite connection so a slow materializer
        cannot starve the renewal behind its transaction.  A failed renewal is
        sticky: callers must not acknowledge/commit derived state afterwards.
        """
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
        interval = max(0.01, interval_seconds if interval_seconds is not None else remaining / 3)
        def beat() -> None:
            # SQLite connections are thread-affine.  Construct this auxiliary
            # store inside the heartbeat thread, rather than accidentally
            # catching ProgrammingError forever on a connection opened by the
            # invoking MCP/dashboard thread.
            auxiliary = HydraStore(self.store.database_path)
            try:
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
                        if item is None:
                            held = repository.acquire_lease(owner_key, observed, renewed_expiry)
                        else:
                            held = repository.renew_claim(
                                owner_key, item.root_kind, item.source_locator,
                                observed, renewed_expiry,
                            )
                    except (OSError, sqlite3.Error):
                        # Busy means our own in-flight atomic materialization
                        # owns the writer lock; retry on the next heartbeat.
                        continue
                    except ValueError:
                        held = False
                    if not held:
                        # A false claim renewal can race the successful queue
                        # ack. Only a read-confirmed owner change is terminal.
                        try:
                            still_owned = repository.lease_owned(owner_key, observed)
                        except sqlite3.Error:
                            continue
                        if not still_owned:
                            lost.set()
                            return
            finally:
                auxiliary.close()

        thread = threading.Thread(target=beat, name="hydra-sync-lease", daemon=True)
        thread.start()
        try:
            yield lost
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval * 2))

    def reconcile_dirty(self, owner_key: str, observed_at: str, lease_expires_at: str) -> int:
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
        for project_id in sorted({root.project_id for root in roots}):
            project_roots = tuple(root for root in roots if root.project_id == project_id)
            with self._lease_heartbeat(owner_key, lease_expires_at) as lost:
                self.reconcile(project_id)
            if lost.is_set():
                continue
            completed += self.repository.acknowledge_dirty_roots(owner_key, project_roots, observed_at)
        return completed

    def _commit(
        self, item: QueueItem, tail: TailRead, result: MaterializedSource,
        owner_key: str, observed_at: str, ownership_lost: threading.Event | None = None,
    ) -> None:
        """Commit parser facts, checkpoint, dirty marker and queue ack together."""
        with self.store.rollout_transaction() as connection:
            # A stale/dead worker cannot commit over the next lease holder.
            owned = connection.execute(
                """SELECT requeue_pending FROM sync_ingest_queue WHERE root_kind=? AND source_locator=?
                     AND queue_state='claimed' AND claimed_by=? AND claim_expires_at>?""",
                (item.root_kind, item.source_locator, owner_key, observed_at),
            ).fetchone()
            if owned is None:
                raise RepairRequired("queue claim expired")
            result = self.materialize(item, tail, connection)
            if ownership_lost is not None and ownership_lost.is_set():
                # The materializer's writes are still inside this transaction;
                # raising here rolls them back before a successor can observe
                # any facts or a durable checkpoint.
                raise RepairRequired("worker lease lost")
            if result.project_id is not None:
                # Keep incremental facts in the same derived-state shape as a
                # legacy ingest batch before exposing the project as dirty.
                reconcile_test_evidence(connection)
                refresh_token_source_selection(connection, result.project_id)
                diagnose = lambda _source, _ordinal, _kind: None
                reconcile_token_epochs(connection, result.project_id, diagnose)
                reconcile_fork_baselines(connection, result.project_id)
                reconcile_turn_attempts(connection, diagnose)
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
                    """INSERT INTO sync_dirty_roots(project_id,root_key,root_kind,observed_at,claim_owner,claim_expires_at)
                       VALUES (?,?,?,?,NULL,NULL)
                       ON CONFLICT(project_id,root_key,root_kind) DO UPDATE SET
                         observed_at=excluded.observed_at,claim_owner=NULL,claim_expires_at=NULL""",
                    (result.project_id, root_key, result.dirty_root_kind, observed_at),
                )
            if tail.has_complete_work or int(owned[0]):
                connection.execute(
                    """UPDATE sync_ingest_queue SET queue_state='queued',available_at=?,claimed_by=NULL,
                           claimed_at=NULL,claim_expires_at=NULL,requeue_pending=0,reason_code=NULL
                         WHERE root_kind=? AND source_locator=?""",
                    (observed_at, item.root_kind, item.source_locator),
                )
            else:
                connection.execute(
                    """DELETE FROM sync_ingest_queue WHERE root_kind=? AND source_locator=?
                         AND queue_state='claimed' AND claimed_by=?""",
                    (item.root_kind, item.source_locator, owner_key),
                )
            self.repository._bump_revision(connection, observed_at)

    def sync_once(
        self, owner_key: str, observed_at: str, lease_expires_at: str, *,
        maximum_sources: int = 100, crash_after_materialize: bool = False,
        crash_after_outbox_consume: bool = False,
    ) -> SyncRun:
        if not 1 <= maximum_sources <= 1000:
            raise ValueError("maximum_sources must be between 1 and 1000")
        now = _utc(observed_at)
        if not self.repository.acquire_lease(owner_key, now, lease_expires_at):
            return SyncRun(0, 0, 0, 0, False)
        claimed = completed = repairs = processed = 0
        for _ in range(maximum_sources):
            item = self.repository.claim_next(owner_key, now, lease_expires_at)
            if item is None:
                break
            claimed += 1
            try:
                tail = read_incremental_source(
                    self.roots, item.root_kind, item.source_locator,
                    self.repository.checkpoint_for(item.root_kind, item.source_locator),
                )
            except RepairRequired:
                self.repository.mark_repair_required(item.root_kind, item.source_locator, now)
                self.repository.acknowledge_claim(owner_key, item.root_kind, item.source_locator, now)
                repairs += 1
                continue
            if item.project_id is None:
                # Without a trusted project binding, advancing the durable
                # offset would irreversibly discard facts we cannot attribute.
                self.repository.retry_claim(
                    owner_key, item.root_kind, item.source_locator,
                    reason_code="unattributed", available_at=lease_expires_at, observed_at=now,
                )
                continue
            # SQLite permits only one writer.  Give a materialization
            # transaction a conservative initial lease window, then keep
            # heartbeating from a separate connection whenever the write lock
            # is free.  Without this extension an otherwise healthy, slow
            # parser could block the heartbeat behind its own transaction.
            requested_deadline = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00"))
            live_lease = requested_deadline > datetime.now(timezone.utc)
            protected_expiry = (
                max(
                    lease_expires_at,
                    (datetime.now(timezone.utc) + timedelta(seconds=60)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                )
                if live_lease else lease_expires_at
            )
            if not self.repository.renew_claim(
                owner_key, item.root_kind, item.source_locator, now, protected_expiry,
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
                requested_seconds = max(0.01, (requested_deadline - datetime.now(timezone.utc)).total_seconds())
                heartbeat = (
                    self._lease_heartbeat(
                        owner_key, protected_expiry, item,
                        interval_seconds=requested_seconds / 3,
                    ) if live_lease else nullcontext(threading.Event())
                )
                with heartbeat as lost:
                    if lost.is_set():
                        raise RepairRequired("worker lease lost")
                    self._commit(item, tail, MaterializedSource(), owner_key, now, lost)
            except RepairRequired:
                self.repository.retry_claim(
                    owner_key, item.root_kind, item.source_locator,
                    reason_code="lease_lost", available_at=lease_expires_at, observed_at=now,
                )
                continue
            except (OSError, sqlite3.OperationalError, SourceChanged, RuntimeError):
                # The materializer transaction rolled back; release this claim
                # for a bounded retry rather than relying on process death.
                self.repository.retry_claim(
                    owner_key, item.root_kind, item.source_locator,
                    reason_code="transient_failure", available_at=lease_expires_at, observed_at=now,
                )
                continue
            completed += 1
            processed += tail.bytes_read
        hook_events = self.repository.claim_hook_events(
            owner_key, now, lease_expires_at,
        )
        if crash_after_outbox_consume:
            # Test-only fault point.  The safe facts have been projected into
            # durable dirty roots, but remain unacknowledged for an idempotent
            # replay once this lease expires.
            raise RuntimeError("injected crash after outbox consume")
        if hook_events:
            self.repository.acknowledge_hook_events(
                owner_key, tuple(event.event_key for event in hook_events), now,
            )
        self.reconcile_dirty(owner_key, now, lease_expires_at)
        self.repository.release_lease(owner_key, now)
        return SyncRun(claimed, completed, repairs, processed, True)


@dataclass(frozen=True)
class RepairRun:
    discovered: int
    directories_scanned: int
    completed: bool


class ResumableRepair:
    """Explicit bounded repair/backfill; this is the only directory walker."""

    _ROOT_MARKER = "@root"
    _FILE_PREFIX = "@file/"

    def __init__(self, store: HydraStore, roots: TrustedSourceRoots, *, lease_ttl_seconds: int = 300) -> None:
        if not 1 <= lease_ttl_seconds <= 3600:
            raise ValueError("repair lease TTL must be between 1 and 3600 seconds")
        self.store = store
        self.roots = roots
        self.repository = SyncStateRepository(store)
        self.lease_ttl_seconds = lease_ttl_seconds

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
        job = self.repository.current_job(job_kind)
        if job is not None:
            return job.job_id
        job_id = self.repository.create_job(job_kind, observed_at)
        for root_kind in ("sessions", "archived_sessions"):
            try:
                self.roots.root_for(root_kind)
            except RepairRequired:
                continue
            self.repository.save_frontier(
                job_id=job_id, root_kind=root_kind, directory_locator=self._ROOT_MARKER,
                state="pending", discovered_count=0, updated_at=observed_at,
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
            self.repository.register_and_enqueue(
                root_kind=root_kind, source_locator=locator, observed_at=observed_at,
            )
            return False
        try:
            project = resolve_project(materialized_scan.cwd)
        except (ProjectNotFound, OSError, TypeError, ValueError):
            self.repository.register_and_enqueue(
                root_kind=root_kind, source_locator=locator, observed_at=observed_at,
            )
            return False
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
            self.repository.mark_repair_required(root_kind, locator, observed_at)
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
            self.repository.register_and_enqueue(
                root_kind=root_kind, source_locator=locator, project_id=project.project_id,
                observed_at=observed_at,
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

    def _directory(self, root_kind: str, locator: str) -> Path:
        root = self.roots.root_for(root_kind)
        if locator == self._ROOT_MARKER:
            return root
        # Directory frontier shares locator validation but deliberately permits
        # directories, unlike TrustedSourceRoots.resolve which requires a file.
        candidate = root
        try:
            for component in validate_root_relative_locator(locator).split("/"):
                candidate = candidate / component
                details = candidate.lstat()
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                    raise RepairRequired("repair frontier is no longer a directory")
        except RepairRequired:
            raise
        except OSError as error:
            raise RepairRequired("repair frontier is unavailable") from error
        return candidate

    def run_batch(self, job_id: str, observed_at: str, *, directory_limit: int = 100) -> RepairRun:
        if not 1 <= directory_limit <= 1000:
            raise ValueError("directory_limit must be between 1 and 1000")
        job = self.repository.get_job(job_id)
        if job is None or job.job_kind not in {"repair", "backfill"}:
            raise KeyError("repair job is unknown")
        # A job identifies durable work, not a process.  An invocation needs a
        # fresh lease identity so a second dashboard/MCP call cannot renew the
        # first call's singleton lease merely by knowing the job id.
        owner = f"repair-{uuid.uuid4().hex}"
        expiry = _precise_utc(
            datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            + timedelta(seconds=self.lease_ttl_seconds),
        )
        # Directory and file frontiers share the singleton ingest lease: a
        # second dashboard/MCP worker observes progress but does no I/O.
        if not self.repository.acquire_lease(owner, observed_at, expiry):
            return RepairRun(0, 0, False)
        pending = self.repository.resume_frontier(job_id)[:directory_limit]
        discovered = directories = completed_sources = processed_bytes = 0
        try:
            for frontier in pending:
                if frontier.directory_locator.startswith(self._FILE_PREFIX):
                    locator = frontier.directory_locator.removeprefix(self._FILE_PREFIX)
                    try:
                        with self._lease_heartbeat(owner, expiry) as lost:
                            materialized = self._full_materialize(frontier.root_kind, locator, observed_at)
                        if lost.is_set():
                            # The durable frontier remains pending; a later
                            # owner may safely retry the idempotent full scan.
                            continue
                    except (OSError, RepairRequired, SourceChanged):
                        self.repository.save_frontier(
                            job_id=job_id, root_kind=frontier.root_kind,
                            directory_locator=frontier.directory_locator, state="repair_required",
                            discovered_count=1, updated_at=observed_at,
                        )
                        continue
                    if materialized:
                        details = self.repository.checkpoint_for(frontier.root_kind, locator)
                        self.repository.save_frontier(
                            job_id=job_id, root_kind=frontier.root_kind,
                            directory_locator=frontier.directory_locator, state="scanned",
                            discovered_count=1, updated_at=observed_at,
                        )
                        completed_sources += 1
                        processed_bytes += details.file_size
                    else:
                        self.repository.save_frontier(
                            job_id=job_id, root_kind=frontier.root_kind,
                            directory_locator=frontier.directory_locator, state="repair_required",
                            discovered_count=1, updated_at=observed_at,
                        )
                    continue
                try:
                    with self._lease_heartbeat(owner, expiry) as lost:
                        directory = self._directory(frontier.root_kind, frontier.directory_locator)
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                try:
                                    details = entry.stat(follow_symlinks=False)
                                except OSError:
                                    continue
                                if stat.S_ISLNK(details.st_mode):
                                    continue
                                relative = entry.name if frontier.directory_locator == self._ROOT_MARKER else f"{frontier.directory_locator}/{entry.name}"
                                if stat.S_ISDIR(details.st_mode):
                                    self.repository.save_frontier(
                                        job_id=job_id, root_kind=frontier.root_kind, directory_locator=relative,
                                        state="pending", discovered_count=0, updated_at=observed_at,
                                    )
                                elif stat.S_ISREG(details.st_mode) and entry.name.endswith(".jsonl"):
                                    self.repository.save_frontier(
                                        job_id=job_id, root_kind=frontier.root_kind,
                                        directory_locator=self._FILE_PREFIX + relative, state="pending",
                                        discovered_count=1, updated_at=observed_at,
                                    )
                                    discovered += 1
                    if lost.is_set():
                        continue
                    self.repository.save_frontier(
                        job_id=job_id, root_kind=frontier.root_kind, directory_locator=frontier.directory_locator,
                        state="scanned", discovered_count=discovered, updated_at=observed_at,
                    )
                    directories += 1
                except RepairRequired:
                    self.repository.save_frontier(
                        job_id=job_id, root_kind=frontier.root_kind, directory_locator=frontier.directory_locator,
                        state="repair_required", discovered_count=0, updated_at=observed_at,
                    )
            pending_after = self.repository.resume_frontier(job_id)
            complete = not pending_after
            partial = bool(self.repository.list_frontier(job_id, "repair_required"))
            reconciliation_settled = False
            if complete:
                dirty = self.repository.claim_dirty_roots(owner, observed_at, expiry)
                for project_id in sorted({root.project_id for root in dirty}):
                    group = tuple(root for root in dirty if root.project_id == project_id)
                    with self._lease_heartbeat(owner, expiry) as lost:
                        reconcile_project(self.store, project_id, Pseudonymizer.installation(self.store.database_path.parent).key)
                    if lost.is_set():
                        break
                    self.repository.acknowledge_dirty_roots(owner, group, observed_at)
                reconciliation_settled = not self.repository.list_dirty_roots()
            finished = complete and reconciliation_settled
            latest = self.repository.get_job(job_id)
            assert latest is not None
            self.repository.update_job(
                job_id, state="partial" if finished and partial else "succeeded" if finished else "running",
                sources_discovered=latest.sources_discovered + discovered,
                sources_completed=latest.sources_completed + completed_sources,
                bytes_processed=latest.bytes_processed + processed_bytes,
                updated_at=observed_at,
                completed_at=observed_at if finished else None,
            )
            return RepairRun(discovered, directories, finished)
        finally:
            self.repository.release_lease(owner, observed_at)
