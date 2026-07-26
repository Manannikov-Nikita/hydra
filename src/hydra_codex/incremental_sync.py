"""Durable, queue-driven JSONL tailing without global rollout discovery.

This module intentionally has no dependency on :func:`discover_trusted_rollouts`.
Normal sync only resolves a source that a trusted hook or repair job previously
registered.  Materialising a line is injected because the legacy full-file
parser carries transient state; callers can migrate to this transaction seam
without reintroducing a full-file scan.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import BinaryIO

from .project import ProjectNotFound, resolve_project
from .rollout import ingest_rollouts
from .rollout_identity import Pseudonymizer, RolloutRoot
from .rollout_observations import safe_int, usage as parse_usage
from .rollout_privacy import canonical_timestamp, nonempty_string
from .rollout_sources import SourceChanged, SourceStat, line_fingerprint, open_source, scan_source, source_stat
from .storage import HydraStore
from .sync_state import QueueItem, SourceCheckpoint, SyncStateRepository, validate_root_relative_locator


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


@dataclass(frozen=True)
class TailRead:
    lines: tuple[TailLine, ...]
    checkpoint: SourceCheckpoint
    bytes_read: int


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


def read_incremental_source(
    roots: TrustedSourceRoots, root_kind: str, source_locator: str,
    checkpoint: SourceCheckpoint | None = None,
) -> TailRead:
    """Read only new, newline-complete bytes from one trusted source.

    A changed inode, a smaller size, a changed stable prefix, or a changed
    boundary before the durable offset is a repair condition for *this source*
    only.  A partial final line is deliberately not included in the checkpoint.
    """
    path = roots.resolve(root_kind, source_locator)
    prior = checkpoint or SourceCheckpoint(0, 0, None, None, 0, None, None)
    try:
        before = source_stat(path)
        if prior.byte_offset > before.size:
            raise RepairRequired("source was truncated")
        if prior.device_id is not None and (prior.device_id, prior.inode) != (before.dev, before.ino):
            raise RepairRequired("source inode changed")
        with open_source(path, before) as handle:
            if prior.prefix_anchor is not None and _prefix_anchor(handle, before.size) != prior.prefix_anchor:
                raise RepairRequired("source prefix changed")
            if prior.revision_anchor is not None and _boundary_anchor(handle, prior.byte_offset) != prior.revision_anchor:
                raise RepairRequired("source boundary changed")
            handle.seek(prior.byte_offset)
            appended = handle.read()
            last_newline = appended.rfind(b"\n")
            complete = b"" if last_newline < 0 else appended[:last_newline + 1]
            lines = tuple(
                TailLine(prior.line_number + index, raw)
                for index, raw in enumerate(complete.splitlines(keepends=True), start=1)
            )
            offset = prior.byte_offset + len(complete)
            next_checkpoint = _checkpoint_for(handle, before, offset, prior.line_number + len(lines))
        return TailRead(lines, next_checkpoint, len(complete))
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


Materializer = Callable[[QueueItem, TailRead, object], MaterializedSource]
Reconciler = Callable[[str], None]


def _utc(value: str) -> str:
    # Repository validation owns canonical timestamp verification.  This helper
    # creates a stable timestamp for callers which omit a clock in production.
    if value:
        return value
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
            self.reconcile(project_id)
            completed += self.repository.acknowledge_dirty_roots(owner_key, project_roots, observed_at)
        return completed

    def _commit(
        self, item: QueueItem, tail: TailRead, result: MaterializedSource,
        owner_key: str, observed_at: str,
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
            if int(owned[0]):
                connection.execute(
                    """UPDATE sync_ingest_queue SET queue_state='queued',available_at=?,claimed_by=NULL,
                           claimed_at=NULL,claim_expires_at=NULL,requeue_pending=0
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
    ) -> SyncRun:
        if not 1 <= maximum_sources <= 1000:
            raise ValueError("maximum_sources must be between 1 and 1000")
        now = _utc(observed_at)
        if not self.repository.acquire_lease(owner_key, now, lease_expires_at):
            return SyncRun(0, 0, 0, 0)
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
            if crash_after_materialize:
                # Test-only fault point: the queue claim survives and its
                # expiry causes an idempotent replay after a process restart.
                with self.store.rollout_transaction() as connection:
                    self.materialize(item, tail, connection)
                raise RuntimeError("injected crash after materialize")
            try:
                self._commit(item, tail, MaterializedSource(), owner_key, now)
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
        self.reconcile_dirty(owner_key, now, lease_expires_at)
        return SyncRun(claimed, completed, repairs, processed)


@dataclass(frozen=True)
class RepairRun:
    discovered: int
    directories_scanned: int
    completed: bool


class ResumableRepair:
    """Explicit bounded repair/backfill; this is the only directory walker."""

    _ROOT_MARKER = "@root"

    def __init__(self, store: HydraStore, roots: TrustedSourceRoots) -> None:
        self.store = store
        self.roots = roots
        self.repository = SyncStateRepository(store)

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
        path = self.roots.resolve(root_kind, locator)
        try:
            canonical_path = path.resolve(strict=True)
        except OSError as error:
            raise RepairRequired("source changed before full repair") from error
        hasher = Pseudonymizer.installation(self.store.database_path.parent)
        scan = scan_source(canonical_path, hasher.key, hasher.digest)
        if scan.identity is None or scan.cwd is None:
            self.repository.register_and_enqueue(
                root_kind=root_kind, source_locator=locator, observed_at=observed_at,
            )
            return False
        try:
            project = resolve_project(scan.cwd)
        except (ProjectNotFound, OSError, TypeError, ValueError):
            self.repository.register_and_enqueue(
                root_kind=root_kind, source_locator=locator, observed_at=observed_at,
            )
            return False
        label = "active" if root_kind == "sessions" else "archived"
        # ``prepared_scans`` makes legacy ingest use exactly this validated
        # source; no directory discovery or global walk is reachable here.
        ingest_rollouts(
            self.store, (RolloutRoot(canonical_path, label),), project.project_root, project.project_id,
            hash_key=hasher.key, prepared_scans={canonical_path: scan},
        )
        location_key = hasher.digest("source", str(canonical_path))
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
        checkpoint = read_incremental_source(self.roots, root_kind, locator).checkpoint
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
        pending = self.repository.resume_frontier(job_id)[:directory_limit]
        discovered = directories = completed_sources = processed_bytes = 0
        for frontier in pending:
            try:
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
                            if self._full_materialize(frontier.root_kind, relative, observed_at):
                                completed_sources += 1
                                processed_bytes += int(details.st_size)
                            discovered += 1
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
        complete = not self.repository.resume_frontier(job_id)
        latest = self.repository.get_job(job_id)
        assert latest is not None
        self.repository.update_job(
            job_id, state="succeeded" if complete else "running",
            sources_discovered=latest.sources_discovered + discovered,
            sources_completed=latest.sources_completed + completed_sources,
            bytes_processed=latest.bytes_processed + processed_bytes,
            updated_at=observed_at,
            completed_at=observed_at if complete else None,
        )
        return RepairRun(discovered, directories, complete)
