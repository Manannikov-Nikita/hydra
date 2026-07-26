"""Private durable state primitives for the incremental telemetry worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import uuid

from .storage import HydraStore


_ROOT_KINDS = frozenset({"sessions", "archived_sessions"})
_JOB_KINDS = frozenset({"sync", "backfill", "repair"})
_JOB_STATES = frozenset({"queued", "running", "succeeded", "partial", "failed"})
_FRONTIER_STATES = frozenset({"pending", "scanned", "repair_required"})
_HEX_ANCHOR = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SourceCheckpoint:
    byte_offset: int
    line_number: int
    prefix_anchor: str | None
    revision_anchor: str | None
    file_size: int
    device_id: int | None
    inode: int | None


@dataclass(frozen=True)
class SyncJob:
    job_id: str
    job_kind: str
    state: str
    sources_discovered: int
    sources_completed: int
    bytes_processed: int
    created_at: str
    updated_at: str
    completed_at: str | None


def validate_root_relative_locator(locator: str) -> str:
    """Return a canonical storage locator, rejecting paths outside known roots."""
    if not isinstance(locator, str) or not locator or len(locator) > 512:
        raise ValueError("source locator must be a non-empty root-relative path")
    if "\x00" in locator or "\\" in locator or locator.startswith("/"):
        raise ValueError("source locator must be a non-empty root-relative path")
    parts = locator.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("source locator must be a non-empty root-relative path")
    return locator


def _timestamp(value: str | None) -> str:
    return value or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _anchor(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not _HEX_ANCHOR.fullmatch(value):
        raise ValueError(f"{field} must be a lower-case SHA-256 hex digest")
    return value


class SyncStateRepository:
    """Transactional private storage for workers; never returns filesystem paths."""

    def __init__(self, store: HydraStore) -> None:
        self._store = store

    def _bump_revision(self, connection, observed_at: str) -> int:
        connection.execute(
            "UPDATE sync_data_revision SET revision=revision+1,updated_at=? WHERE singleton=1",
            (observed_at,),
        )
        return int(connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1"
        ).fetchone()[0])

    def data_revision(self) -> int:
        return int(self._store.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1"
        ).fetchone()[0])

    def register_source(self, *, root_kind: str, source_locator: str, observed_at: str | None = None) -> None:
        if root_kind not in _ROOT_KINDS:
            raise ValueError("untrusted source root")
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            existing = connection.execute(
                "SELECT root_kind FROM sync_source_registry WHERE source_locator=?", (locator,)
            ).fetchone()
            if existing is not None and str(existing[0]) != root_kind:
                raise ValueError("source locator cannot move between trusted roots")
            connection.execute(
                """INSERT INTO sync_source_registry(
                       source_locator,root_kind,source_state,first_seen_at,last_seen_at)
                   VALUES (?,?,'ready',?,?)
                   ON CONFLICT(source_locator) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (locator, root_kind, now, now),
            )
            self._bump_revision(connection, now)

    def enqueue(self, source_locator: str, observed_at: str | None = None) -> bool:
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            result = connection.execute(
                "INSERT INTO sync_ingest_queue(source_locator,enqueued_at) VALUES (?,?) ON CONFLICT(source_locator) DO NOTHING",
                (locator, now),
            )
            inserted = result.rowcount == 1
            if inserted:
                self._bump_revision(connection, now)
            return inserted

    def queued_sources(self, limit: int = 100) -> tuple[str, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("queue limit must be between 1 and 1000")
        return tuple(str(row[0]) for row in self._store.connection.execute(
            "SELECT source_locator FROM sync_ingest_queue ORDER BY enqueued_at,source_locator LIMIT ?",
            (limit,),
        ))

    def acknowledge_queue(self, source_locator: str, observed_at: str | None = None) -> bool:
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM sync_ingest_queue WHERE source_locator=?", (locator,)
            ).rowcount == 1
            if deleted:
                self._bump_revision(connection, now)
            return deleted

    def save_checkpoint(
        self, source_locator: str, *, byte_offset: int, line_number: int,
        prefix_anchor: str | None, revision_anchor: str | None,
        file_size: int | None = None, device_id: int | None = None,
        inode: int | None = None, observed_at: str | None = None,
    ) -> int:
        locator = validate_root_relative_locator(source_locator)
        if byte_offset < 0 or line_number < 0 or (file_size is not None and file_size < 0):
            raise ValueError("source checkpoint offsets must be non-negative")
        prefix = _anchor(prefix_anchor, "prefix anchor")
        revision = _anchor(revision_anchor, "revision anchor")
        now = _timestamp(observed_at)
        effective_size = byte_offset if file_size is None else file_size
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO sync_source_checkpoints(
                       source_locator,device_id,inode,file_size,byte_offset,line_number,
                       prefix_anchor,revision_anchor,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_locator) DO UPDATE SET
                       device_id=excluded.device_id,inode=excluded.inode,file_size=excluded.file_size,
                       byte_offset=excluded.byte_offset,line_number=excluded.line_number,
                       prefix_anchor=excluded.prefix_anchor,revision_anchor=excluded.revision_anchor,
                       updated_at=excluded.updated_at""",
                (locator, device_id, inode, effective_size, byte_offset, line_number, prefix, revision, now),
            )
            return self._bump_revision(connection, now)

    def checkpoint_for(self, source_locator: str) -> SourceCheckpoint:
        locator = validate_root_relative_locator(source_locator)
        row = self._store.connection.execute(
            """SELECT byte_offset,line_number,prefix_anchor,revision_anchor,file_size,device_id,inode
                 FROM sync_source_checkpoints WHERE source_locator=?""",
            (locator,),
        ).fetchone()
        if row is None:
            return SourceCheckpoint(0, 0, None, None, 0, None, None)
        return SourceCheckpoint(
            int(row[0]), int(row[1]), None if row[2] is None else str(row[2]),
            None if row[3] is None else str(row[3]), int(row[4]),
            None if row[5] is None else int(row[5]), None if row[6] is None else int(row[6]),
        )

    def mark_repair_required(self, source_locator: str, observed_at: str | None = None) -> int:
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            connection.execute(
                "UPDATE sync_source_registry SET source_state='repair_required',last_seen_at=? WHERE source_locator=?",
                (now, locator),
            )
            return self._bump_revision(connection, now)

    def acquire_lease(self, owner_key: str, acquired_at: str, expires_at: str) -> bool:
        if not isinstance(owner_key, str) or not 1 <= len(owner_key) <= 128:
            raise ValueError("worker owner key is invalid")
        if expires_at <= acquired_at:
            raise ValueError("worker lease expiry must follow acquisition")
        with self._store.rollout_transaction() as connection:
            result = connection.execute(
                """INSERT INTO sync_worker_leases(lease_name,owner_key,acquired_at,expires_at)
                   VALUES ('ingest',?,?,?)
                   ON CONFLICT(lease_name) DO UPDATE SET
                       owner_key=excluded.owner_key,acquired_at=excluded.acquired_at,
                       expires_at=excluded.expires_at
                   WHERE sync_worker_leases.owner_key=excluded.owner_key
                      OR sync_worker_leases.expires_at<=excluded.acquired_at""",
                (owner_key, acquired_at, expires_at),
            )
            acquired = result.rowcount == 1
            if acquired:
                self._bump_revision(connection, acquired_at)
            return acquired

    def release_lease(self, owner_key: str, observed_at: str | None = None) -> bool:
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            released = connection.execute(
                "DELETE FROM sync_worker_leases WHERE lease_name='ingest' AND owner_key=?", (owner_key,)
            ).rowcount == 1
            if released:
                self._bump_revision(connection, now)
            return released

    def mark_dirty(self, project_id: str, root_key: str, root_kind: str, observed_at: str | None = None) -> int:
        if root_kind not in {"project", "task"}:
            raise ValueError("dirty root kind is invalid")
        if not project_id or not root_key:
            raise ValueError("dirty root identity is invalid")
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO sync_dirty_roots(project_id,root_key,root_kind,observed_at)
                   VALUES (?,?,?,?) ON CONFLICT(project_id,root_key,root_kind)
                   DO UPDATE SET observed_at=excluded.observed_at""",
                (project_id, root_key, root_kind, now),
            )
            return self._bump_revision(connection, now)

    def create_job(self, job_kind: str, created_at: str, job_id: str | None = None) -> str:
        if job_kind not in _JOB_KINDS:
            raise ValueError("sync job kind is invalid")
        identifier = job_id or f"sync_{uuid.uuid4().hex}"
        if not 1 <= len(identifier) <= 128:
            raise ValueError("sync job id is invalid")
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO sync_jobs(
                       job_id,job_kind,state,sources_discovered,sources_completed,bytes_processed,
                       created_at,updated_at,completed_at)
                   VALUES (?,?,'queued',0,0,0,?,?,NULL)""",
                (identifier, job_kind, created_at, created_at),
            )
            self._bump_revision(connection, created_at)
        return identifier

    def update_job(
        self, job_id: str, *, state: str, sources_discovered: int,
        sources_completed: int, bytes_processed: int, updated_at: str,
        completed_at: str | None = None,
    ) -> int:
        if state not in _JOB_STATES:
            raise ValueError("sync job state is invalid")
        if min(sources_discovered, sources_completed, bytes_processed) < 0:
            raise ValueError("sync job progress is invalid")
        if sources_completed > sources_discovered:
            raise ValueError("completed sources cannot exceed discovered sources")
        if state in {"succeeded", "partial", "failed"} and completed_at is None:
            completed_at = updated_at
        with self._store.rollout_transaction() as connection:
            changed = connection.execute(
                """UPDATE sync_jobs SET state=?,sources_discovered=?,sources_completed=?,
                       bytes_processed=?,updated_at=?,completed_at=? WHERE job_id=?""",
                (state, sources_discovered, sources_completed, bytes_processed, updated_at, completed_at, job_id),
            ).rowcount
            if changed != 1:
                raise KeyError("sync job is unknown")
            return self._bump_revision(connection, updated_at)

    def save_frontier(
        self, *, root_kind: str, directory_locator: str, state: str,
        discovered_count: int, updated_at: str,
    ) -> int:
        if root_kind not in _ROOT_KINDS or state not in _FRONTIER_STATES or discovered_count < 0:
            raise ValueError("backfill frontier is invalid")
        locator = validate_root_relative_locator(directory_locator)
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO sync_backfill_frontier(
                       root_kind,directory_locator,state,discovered_count,updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(root_kind,directory_locator) DO UPDATE SET
                       state=excluded.state,discovered_count=excluded.discovered_count,
                       updated_at=excluded.updated_at""",
                (root_kind, locator, state, discovered_count, updated_at),
            )
            return self._bump_revision(connection, updated_at)
