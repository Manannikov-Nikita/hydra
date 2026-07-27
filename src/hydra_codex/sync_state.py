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
_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CANONICAL_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")


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
class SourceRecord:
    root_kind: str
    source_locator: str
    source_state: str
    project_id: str | None
    logical_source_key: str | None
    session_key: str | None


@dataclass(frozen=True)
class QueueItem(SourceRecord):
    queue_state: str
    attempts: int
    available_at: str
    claim_expires_at: str | None
    reason_code: str | None


@dataclass(frozen=True)
class DirtyRoot:
    project_id: str
    root_key: str
    root_kind: str
    observed_at: str
    claim_owner: str | None
    claim_expires_at: str | None


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


@dataclass(frozen=True)
class BackfillFrontier:
    job_id: str
    root_kind: str
    directory_locator: str
    state: str
    discovered_count: int
    updated_at: str


@dataclass(frozen=True)
class HookEvent:
    """One private, safe hook fact leased for durable projection."""

    event_key: str
    project_id: str
    session_key: str
    turn_key: str
    event_kind: str
    tool_category: str | None
    tool_status: str | None
    duration_ms: int | None
    observed_at: str


def validate_root_relative_locator(locator: str) -> str:
    """Return an ASCII canonical root-relative locator suitable for SQLite storage."""
    if not isinstance(locator, str) or not 1 <= len(locator) <= 512:
        raise ValueError("source locator must be a canonical root-relative path")
    if any(ord(character) < 32 or ord(character) > 126 for character in locator):
        raise ValueError("source locator must be a canonical root-relative path")
    if locator.startswith("/") or "\\" in locator or "//" in locator:
        raise ValueError("source locator must be a canonical root-relative path")
    parts = locator.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("source locator must be a canonical root-relative path")
    return locator


def _timestamp(value: str | None) -> str:
    candidate = value or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not isinstance(candidate, str) or not _CANONICAL_UTC.fullmatch(candidate):
        raise ValueError("timestamp must be canonical UTC RFC3339")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp must be canonical UTC RFC3339") from error
    if parsed.tzinfo != timezone.utc or ("." in candidate and candidate[:-1].endswith("0")):
        raise ValueError("timestamp must be canonical UTC RFC3339")
    return candidate


def _anchor(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not _HEX_ANCHOR.fullmatch(value):
        raise ValueError(f"{field} must be a lower-case SHA-256 hex digest")
    return value


def _identity(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} is invalid")
    return value


class SyncStateRepository:
    """Atomic private persistence APIs for the MCP/dashboard worker boundary."""

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

    @staticmethod
    def _validate_root(root_kind: str) -> str:
        if root_kind not in _ROOT_KINDS:
            raise ValueError("untrusted source root")
        return root_kind

    @staticmethod
    def _source_from_row(row) -> SourceRecord:
        return SourceRecord(*(
            None if value is None else str(value) for value in row
        ))

    @staticmethod
    def _queue_from_row(row) -> QueueItem:
        values = tuple(None if value is None else str(value) for value in row)
        return QueueItem(
            root_kind=values[0], source_locator=values[1], source_state=values[2],
            project_id=values[3], logical_source_key=values[4], session_key=values[5],
            queue_state=values[6], attempts=int(values[7]), available_at=values[8],
            claim_expires_at=values[9], reason_code=values[10],
        )

    def data_revision(self) -> int:
        return int(self._store.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1"
        ).fetchone()[0])

    def _register(
        self, connection, *, root_kind: str, source_locator: str, project_id: str | None,
        logical_source_key: str | None, session_key: str | None, observed_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO sync_source_registry(
                   root_kind,source_locator,source_state,project_id,logical_source_key,session_key,
                   first_seen_at,last_seen_at)
               VALUES (?,?,'ready',?,?,?,?,?)
               ON CONFLICT(root_kind,source_locator) DO UPDATE SET
                   project_id=COALESCE(excluded.project_id,sync_source_registry.project_id),
                   logical_source_key=COALESCE(excluded.logical_source_key,sync_source_registry.logical_source_key),
                   session_key=COALESCE(excluded.session_key,sync_source_registry.session_key),
                   last_seen_at=excluded.last_seen_at""",
            (root_kind, source_locator, project_id, logical_source_key, session_key, observed_at, observed_at),
        )

    def register_source(
        self, *, root_kind: str, source_locator: str, project_id: str | None = None,
        logical_source_key: str | None = None, session_key: str | None = None,
        observed_at: str | None = None,
    ) -> int:
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            self._register(
                connection, root_kind=root, source_locator=locator,
                project_id=_identity(project_id, "project identity"),
                logical_source_key=_identity(logical_source_key, "logical source identity"),
                session_key=_identity(session_key, "session identity"), observed_at=now,
            )
            return self._bump_revision(connection, now)

    def register_and_enqueue(
        self, *, root_kind: str, source_locator: str, project_id: str | None = None,
        logical_source_key: str | None = None, session_key: str | None = None,
        observed_at: str | None = None,
    ) -> bool:
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            self._register(
                connection, root_kind=root, source_locator=locator,
                project_id=_identity(project_id, "project identity"),
                logical_source_key=_identity(logical_source_key, "logical source identity"),
                session_key=_identity(session_key, "session identity"), observed_at=now,
            )
            result = connection.execute(
                """INSERT INTO sync_ingest_queue(
                       root_kind,source_locator,queue_state,enqueued_at,available_at,claimed_by,claimed_at,claim_expires_at,requeue_pending,attempts,reason_code)
                   VALUES (?,?,'queued',?,?,NULL,NULL,NULL,0,0,NULL)
                   ON CONFLICT(root_kind,source_locator) DO NOTHING""",
                (root, locator, now, now),
            )
            inserted = result.rowcount == 1
            if not inserted:
                connection.execute(
                    """UPDATE sync_ingest_queue SET requeue_pending=1
                         WHERE root_kind=? AND source_locator=? AND queue_state='claimed'""",
                    (root, locator),
                )
            self._bump_revision(connection, now)
            return inserted

    def record_hook_event_and_enqueue(
        self, *, event_key: str, project_id: str, session_key: str, turn_key: str,
        event_kind: str, observed_at: str, tool_category: str | None = None,
        tool_status: str | None = None, duration_ms: int | None = None,
        source: tuple[str, str] | None = None,
    ) -> bool:
        """Atomically save one safe hook fact and its optional source wakeup."""
        now = _timestamp(observed_at)
        if event_kind not in {"prompt", "post_tool", "stop"}:
            raise ValueError("hook event kind is invalid")
        if tool_category not in {None, "shell", "read", "write", "search", "browser", "other"}:
            raise ValueError("hook tool category is invalid")
        if tool_status not in {None, "success", "failure", "unknown"}:
            raise ValueError("hook tool status is invalid")
        if duration_ms is not None and (
            isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 0 <= duration_ms <= 86_400_000
        ):
            raise ValueError("hook duration is invalid")
        event = _identity(event_key, "hook event identity")
        project = _identity(project_id, "project identity")
        session = _identity(session_key, "session identity")
        turn = _identity(turn_key, "turn identity")
        prepared: tuple[str, str] | None = None
        if source is not None:
            prepared = (self._validate_root(source[0]), validate_root_relative_locator(source[1]))
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO hook_event_outbox(
                       event_key,project_id,session_key,turn_key,event_kind,tool_category,tool_status,duration_ms,observed_at)
                   VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                (event, project, session, turn, event_kind, tool_category, tool_status, duration_ms, now),
            )
            inserted = False
            if prepared is not None:
                root, locator = prepared
                self._register(
                    connection, root_kind=root, source_locator=locator, project_id=project,
                    logical_source_key=None, session_key=session, observed_at=now,
                )
                result = connection.execute(
                    """INSERT INTO sync_ingest_queue(
                           root_kind,source_locator,queue_state,enqueued_at,available_at,claimed_by,claimed_at,claim_expires_at,requeue_pending,attempts,reason_code)
                       VALUES (?,?,'queued',?,?,NULL,NULL,NULL,0,0,NULL)
                       ON CONFLICT(root_kind,source_locator) DO NOTHING""",
                    (root, locator, now, now),
                )
                inserted = result.rowcount == 1
                if not inserted:
                    connection.execute(
                        """UPDATE sync_ingest_queue SET requeue_pending=1 WHERE root_kind=? AND source_locator=?
                             AND queue_state='claimed'""", (root, locator),
                    )
            self._bump_revision(connection, now)
            return inserted

    @staticmethod
    def _hook_event_from_row(row) -> HookEvent:
        return HookEvent(
            event_key=str(row[0]), project_id=str(row[1]), session_key=str(row[2]),
            turn_key=str(row[3]), event_kind=str(row[4]),
            tool_category=None if row[5] is None else str(row[5]),
            tool_status=None if row[6] is None else str(row[6]),
            duration_ms=None if row[7] is None else int(row[7]), observed_at=str(row[8]),
        )

    def claim_hook_events(
        self, owner_key: str, observed_at: str, claim_expires_at: str, *, limit: int = 1000,
    ) -> tuple[HookEvent, ...]:
        """Lease and project unacknowledged safe hook facts exactly once per claim.

        Projection is deliberately tiny and privacy preserving: a hook event
        only makes its already-opaque project root dirty.  Reconciliation can
        fail or restart independently because this durable marker survives the
        outbox acknowledgement.
        """
        owner = _identity(owner_key, "worker owner key")
        now = _timestamp(observed_at)
        expiry = _timestamp(claim_expires_at)
        if expiry <= now:
            raise ValueError("hook event claim expiry must be in the future")
        if not 1 <= limit <= 1000:
            raise ValueError("hook event claim limit must be between 1 and 1000")
        with self._store.rollout_transaction() as connection:
            lease = connection.execute(
                "SELECT expires_at FROM sync_worker_leases WHERE lease_name='ingest' AND owner_key=? AND expires_at>?",
                (owner, now),
            ).fetchone()
            if lease is None:
                return ()
            if expiry > str(lease[0]):
                raise ValueError("hook event claim cannot outlive worker lease")
            rows = connection.execute(
                """SELECT event_key,project_id,session_key,turn_key,event_kind,tool_category,tool_status,duration_ms,observed_at
                     FROM hook_event_outbox
                    WHERE acknowledged_at IS NULL AND (claimed_by IS NULL OR claim_expires_at<=?)
                    ORDER BY observed_at,event_key LIMIT ?""",
                (now, limit),
            ).fetchall()
            if not rows:
                return ()
            events = tuple(self._hook_event_from_row(row) for row in rows)
            for event in events:
                changed = connection.execute(
                    """UPDATE hook_event_outbox SET claimed_by=?,claimed_at=?,claim_expires_at=?
                         WHERE event_key=? AND acknowledged_at IS NULL
                           AND (claimed_by IS NULL OR claim_expires_at<=?)""",
                    (owner, now, expiry, event.event_key, now),
                ).rowcount == 1
                if not changed:
                    # A defensive retry guard for a future non-serialized DB
                    # backend: only project facts our lease actually won.
                    continue
                connection.execute(
                    """INSERT INTO hook_safe_facts(
                           event_key,project_id,session_key,turn_key,event_kind,tool_category,tool_status,duration_ms,observed_at)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                    (
                        event.event_key, event.project_id, event.session_key, event.turn_key,
                        event.event_kind, event.tool_category, event.tool_status,
                        event.duration_ms, event.observed_at,
                    ),
                )
                connection.execute(
                    """INSERT INTO sync_dirty_roots(project_id,root_key,root_kind,observed_at,claim_owner,claim_expires_at)
                       VALUES (?,?, 'project', ?,NULL,NULL)
                       ON CONFLICT(project_id,root_key,root_kind) DO UPDATE SET
                         observed_at=excluded.observed_at,claim_owner=NULL,claim_expires_at=NULL""",
                    (event.project_id, event.project_id, now),
                )
            claimed = tuple(
                event for event in events if connection.execute(
                    "SELECT claimed_by FROM hook_event_outbox WHERE event_key=?", (event.event_key,),
                ).fetchone()[0] == owner
            )
            if claimed:
                self._bump_revision(connection, now)
            return claimed

    def acknowledge_hook_events(
        self, owner_key: str, event_keys: tuple[str, ...], observed_at: str,
    ) -> int:
        """Acknowledge only facts still held by this live worker lease."""
        owner = _identity(owner_key, "worker owner key")
        now = _timestamp(observed_at)
        if not event_keys:
            return 0
        if len(event_keys) > 1000:
            raise ValueError("too many hook events to acknowledge")
        keys = tuple(_identity(key, "hook event identity") for key in event_keys)
        if len(set(keys)) != len(keys):
            raise ValueError("hook event identities must be unique")
        placeholders = ",".join("?" for _ in keys)
        with self._store.rollout_transaction() as connection:
            changed = connection.execute(
                f"""UPDATE hook_event_outbox
                       SET acknowledged_at=?,claimed_by=NULL,claimed_at=NULL,claim_expires_at=NULL
                     WHERE event_key IN ({placeholders}) AND acknowledged_at IS NULL
                       AND claimed_by=? AND claim_expires_at>?""",
                (now, *keys, owner, now),
            ).rowcount
            if changed:
                self._bump_revision(connection, now)
            return int(changed)

    def enqueue(self, root_kind: str, source_locator: str, observed_at: str | None = None) -> bool:
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            result = connection.execute(
                """INSERT INTO sync_ingest_queue(
                       root_kind,source_locator,queue_state,enqueued_at,available_at,claimed_by,claimed_at,claim_expires_at,requeue_pending,attempts,reason_code)
                   VALUES (?,?,'queued',?,?,NULL,NULL,NULL,0,0,NULL)
                   ON CONFLICT(root_kind,source_locator) DO NOTHING""",
                (root, locator, now, now),
            )
            inserted = result.rowcount == 1
            if not inserted:
                connection.execute(
                    """UPDATE sync_ingest_queue SET requeue_pending=1
                         WHERE root_kind=? AND source_locator=? AND queue_state='claimed'""",
                    (root, locator),
                )
            self._bump_revision(connection, now)
            return inserted

    def source_for(self, root_kind: str, source_locator: str) -> SourceRecord | None:
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        row = self._store.connection.execute(
            """SELECT root_kind,source_locator,source_state,project_id,logical_source_key,session_key
                 FROM sync_source_registry WHERE root_kind=? AND source_locator=?""", (root, locator),
        ).fetchone()
        return None if row is None else self._source_from_row(row)

    def list_sources(self) -> tuple[SourceRecord, ...]:
        return tuple(self._source_from_row(row) for row in self._store.connection.execute(
            """SELECT root_kind,source_locator,source_state,project_id,logical_source_key,session_key
                 FROM sync_source_registry ORDER BY root_kind,source_locator"""
        ))

    def list_queue(self, limit: int = 100) -> tuple[QueueItem, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("queue limit must be between 1 and 1000")
        return tuple(self._queue_from_row(row) for row in self._store.connection.execute(
            """SELECT q.root_kind,q.source_locator,s.source_state,s.project_id,s.logical_source_key,s.session_key,
                      q.queue_state,q.attempts,q.available_at,q.claim_expires_at,q.reason_code
                 FROM sync_ingest_queue q JOIN sync_source_registry s
                   ON s.root_kind=q.root_kind AND s.source_locator=q.source_locator
                ORDER BY q.available_at,q.enqueued_at,q.root_kind,q.source_locator LIMIT ?""", (limit,),
        ))

    def claim_next(self, owner_key: str, observed_at: str, claim_expires_at: str) -> QueueItem | None:
        observed_at = _timestamp(observed_at)
        claim_expires_at = _timestamp(claim_expires_at)
        if not isinstance(owner_key, str) or not 1 <= len(owner_key) <= 128:
            raise ValueError("worker owner key is invalid")
        if claim_expires_at <= observed_at:
            raise ValueError("queue claim expiry must follow claim time")
        with self._store.rollout_transaction() as connection:
            lease = connection.execute(
                "SELECT expires_at FROM sync_worker_leases WHERE lease_name='ingest' AND owner_key=? AND expires_at>?",
                (owner_key, observed_at),
            ).fetchone()
            if lease is None:
                return None
            if claim_expires_at > str(lease[0]):
                raise ValueError("queue claim cannot outlive worker lease")
            selected = connection.execute(
                """SELECT root_kind,source_locator FROM sync_ingest_queue
                     WHERE (queue_state='queued' AND available_at<=?)
                        OR (queue_state='claimed' AND claim_expires_at<=?)
                     ORDER BY available_at,enqueued_at,root_kind,source_locator LIMIT 1""", (observed_at, observed_at),
            ).fetchone()
            if selected is None:
                return None
            root, locator = str(selected[0]), str(selected[1])
            changed = connection.execute(
                """UPDATE sync_ingest_queue SET queue_state='claimed',claimed_by=?,claimed_at=?,claim_expires_at=?,
                       requeue_pending=0
                     WHERE root_kind=? AND source_locator=? AND (
                         (queue_state='queued' AND available_at<=?)
                         OR (queue_state='claimed' AND claim_expires_at<=?)
                     )""",
                (owner_key, observed_at, claim_expires_at, root, locator, observed_at, observed_at),
            ).rowcount
            if changed != 1:
                return None
            self._bump_revision(connection, observed_at)
            row = connection.execute(
                """SELECT q.root_kind,q.source_locator,s.source_state,s.project_id,s.logical_source_key,s.session_key,
                          q.queue_state,q.attempts,q.available_at,q.claim_expires_at,q.reason_code
                     FROM sync_ingest_queue q JOIN sync_source_registry s
                       ON s.root_kind=q.root_kind AND s.source_locator=q.source_locator
                    WHERE q.root_kind=? AND q.source_locator=?""", (root, locator),
            ).fetchone()
            return self._queue_from_row(row)

    def retry_claim(
        self, owner_key: str, root_kind: str, source_locator: str, *, reason_code: str,
        available_at: str, observed_at: str,
    ) -> bool:
        available_at = _timestamp(available_at)
        observed_at = _timestamp(observed_at)
        if not _SAFE_REASON.fullmatch(reason_code):
            raise ValueError("retry reason must be a safe code")
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        with self._store.rollout_transaction() as connection:
            changed = connection.execute(
                """UPDATE sync_ingest_queue SET queue_state='queued',available_at=?,claimed_by=NULL,
                       claimed_at=NULL,claim_expires_at=NULL,requeue_pending=0,attempts=attempts+1,reason_code=?
                     WHERE root_kind=? AND source_locator=? AND queue_state='claimed' AND claimed_by=?""",
                (available_at, reason_code, root, locator, owner_key),
            ).rowcount == 1
            if changed:
                self._bump_revision(connection, observed_at)
            return changed

    def renew_claim(
        self, owner_key: str, root_kind: str, source_locator: str,
        observed_at: str, lease_expires_at: str,
    ) -> bool:
        """Atomically heartbeat the singleton lease and one owned queue claim."""
        observed_at = _timestamp(observed_at)
        lease_expires_at = _timestamp(lease_expires_at)
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        if lease_expires_at <= observed_at:
            raise ValueError("renewal expiry must follow observation")
        with self._store.rollout_transaction() as connection:
            held = connection.execute(
                """UPDATE sync_worker_leases SET acquired_at=?,expires_at=?
                     WHERE lease_name='ingest' AND owner_key=?""",
                (observed_at, lease_expires_at, owner_key),
            ).rowcount == 1
            if not held:
                return False
            renewed = connection.execute(
                """UPDATE sync_ingest_queue SET claim_expires_at=? WHERE root_kind=? AND source_locator=?
                     AND queue_state='claimed' AND claimed_by=?""",
                (lease_expires_at, root, locator, owner_key),
            ).rowcount == 1
            if renewed:
                self._bump_revision(connection, observed_at)
            return renewed

    def acknowledge_claim(self, owner_key: str, root_kind: str, source_locator: str, observed_at: str) -> bool:
        observed_at = _timestamp(observed_at)
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        with self._store.rollout_transaction() as connection:
            changed = connection.execute(
                """DELETE FROM sync_ingest_queue WHERE root_kind=? AND source_locator=?
                     AND queue_state='claimed' AND claimed_by=? AND claim_expires_at>?
                     AND requeue_pending=0""",
                (root, locator, owner_key, observed_at),
            ).rowcount == 1
            requeued = False
            if not changed:
                requeued = connection.execute(
                    """UPDATE sync_ingest_queue SET queue_state='queued',available_at=?,claimed_by=NULL,
                           claimed_at=NULL,claim_expires_at=NULL,requeue_pending=0
                         WHERE root_kind=? AND source_locator=? AND queue_state='claimed'
                           AND claimed_by=? AND claim_expires_at>? AND requeue_pending=1""",
                    (observed_at, root, locator, owner_key, observed_at),
                ).rowcount == 1
            if changed or requeued:
                self._bump_revision(connection, observed_at)
            return changed or requeued

    def save_checkpoint(
        self, root_kind: str, source_locator: str, *, byte_offset: int, line_number: int,
        prefix_anchor: str | None, revision_anchor: str | None, file_size: int | None = None,
        device_id: int | None = None, inode: int | None = None, observed_at: str | None = None,
    ) -> int:
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        effective_size = byte_offset if file_size is None else file_size
        if min(byte_offset, line_number, effective_size) < 0 or byte_offset > effective_size:
            raise ValueError("source checkpoint byte offset must fit within file size")
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
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
                (root, locator, device_id, inode, effective_size, byte_offset, line_number,
                 _anchor(prefix_anchor, "prefix anchor"), _anchor(revision_anchor, "revision anchor"), now),
            )
            return self._bump_revision(connection, now)

    def checkpoint_for(self, root_kind: str, source_locator: str) -> SourceCheckpoint:
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        row = self._store.connection.execute(
            """SELECT byte_offset,line_number,prefix_anchor,revision_anchor,file_size,device_id,inode
                 FROM sync_source_checkpoints WHERE root_kind=? AND source_locator=?""", (root, locator),
        ).fetchone()
        if row is None:
            return SourceCheckpoint(0, 0, None, None, 0, None, None)
        return SourceCheckpoint(
            int(row[0]), int(row[1]), None if row[2] is None else str(row[2]),
            None if row[3] is None else str(row[3]), int(row[4]),
            None if row[5] is None else int(row[5]), None if row[6] is None else int(row[6]),
        )

    def mark_repair_required(self, root_kind: str, source_locator: str, observed_at: str | None = None) -> int:
        root = self._validate_root(root_kind)
        locator = validate_root_relative_locator(source_locator)
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            if connection.execute(
                "UPDATE sync_source_registry SET source_state='repair_required',last_seen_at=? WHERE root_kind=? AND source_locator=?",
                (now, root, locator),
            ).rowcount != 1:
                raise KeyError("source is unknown")
            return self._bump_revision(connection, now)

    def acquire_lease(self, owner_key: str, acquired_at: str, expires_at: str) -> bool:
        acquired_at = _timestamp(acquired_at)
        expires_at = _timestamp(expires_at)
        if not isinstance(owner_key, str) or not 1 <= len(owner_key) <= 128:
            raise ValueError("worker owner key is invalid")
        if expires_at <= acquired_at:
            raise ValueError("worker lease expiry must follow acquisition")
        with self._store.rollout_transaction() as connection:
            result = connection.execute(
                """INSERT INTO sync_worker_leases(lease_name,owner_key,acquired_at,expires_at)
                   VALUES ('ingest',?,?,?) ON CONFLICT(lease_name) DO UPDATE SET
                       owner_key=excluded.owner_key,acquired_at=excluded.acquired_at,expires_at=excluded.expires_at
                   WHERE sync_worker_leases.owner_key=excluded.owner_key
                      OR sync_worker_leases.expires_at<=excluded.acquired_at""", (owner_key, acquired_at, expires_at),
            )
            acquired = result.rowcount == 1
            if acquired:
                self._bump_revision(connection, acquired_at)
            return acquired

    def lease_owned(self, owner_key: str, observed_at: str) -> bool:
        """Read the singleton lease without taking a writer transaction."""
        observed_at = _timestamp(observed_at)
        row = self._store.connection.execute(
            "SELECT 1 FROM sync_worker_leases WHERE lease_name='ingest' AND owner_key=? AND expires_at>?",
            (owner_key, observed_at),
        ).fetchone()
        return row is not None

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
        if root_kind not in {"project", "task"} or not project_id or not root_key:
            raise ValueError("dirty root identity is invalid")
        now = _timestamp(observed_at)
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO sync_dirty_roots(
                       project_id,root_key,root_kind,observed_at,claim_owner,claim_expires_at)
                   VALUES (?,?,?,?,NULL,NULL)
                   ON CONFLICT(project_id,root_key,root_kind) DO UPDATE SET
                       observed_at=excluded.observed_at,claim_owner=NULL,claim_expires_at=NULL""",
                (project_id, root_key, root_kind, now),
            )
            return self._bump_revision(connection, now)

    def list_dirty_roots(self, limit: int = 100) -> tuple[DirtyRoot, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("dirty root limit must be between 1 and 1000")
        return tuple(DirtyRoot(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]),
            None if row[4] is None else str(row[4]), None if row[5] is None else str(row[5]),
        ) for row in self._store.connection.execute(
            """SELECT project_id,root_key,root_kind,observed_at,claim_owner,claim_expires_at
                 FROM sync_dirty_roots ORDER BY observed_at,project_id,root_kind,root_key LIMIT ?""", (limit,),
        ))

    def claim_dirty_roots(
        self, owner_key: str, observed_at: str, claim_expires_at: str, limit: int = 100,
    ) -> tuple[DirtyRoot, ...]:
        observed_at = _timestamp(observed_at)
        claim_expires_at = _timestamp(claim_expires_at)
        if not isinstance(owner_key, str) or not 1 <= len(owner_key) <= 128:
            raise ValueError("worker owner key is invalid")
        if claim_expires_at <= observed_at:
            raise ValueError("dirty claim expiry must follow claim time")
        if not 1 <= limit <= 1000:
            raise ValueError("dirty root limit must be between 1 and 1000")
        with self._store.rollout_transaction() as connection:
            lease = connection.execute(
                "SELECT expires_at FROM sync_worker_leases WHERE lease_name='ingest' AND owner_key=? AND expires_at>?",
                (owner_key, observed_at),
            ).fetchone()
            if lease is None:
                return ()
            if claim_expires_at > str(lease[0]):
                raise ValueError("dirty claim cannot outlive worker lease")
            rows = tuple(connection.execute(
                """SELECT project_id,root_key,root_kind,observed_at FROM sync_dirty_roots
                     WHERE claim_owner IS NULL OR claim_expires_at<=?
                     ORDER BY observed_at,project_id,root_kind,root_key LIMIT ?""", (observed_at, limit),
            ))
            if not rows:
                return ()
            connection.executemany(
                """UPDATE sync_dirty_roots SET claim_owner=?,claim_expires_at=?
                     WHERE project_id=? AND root_key=? AND root_kind=?
                       AND (claim_owner IS NULL OR claim_expires_at<=?)""",
                ((owner_key, claim_expires_at, row[0], row[1], row[2], observed_at) for row in rows),
            )
            self._bump_revision(connection, observed_at)
            return tuple(
                DirtyRoot(str(row[0]), str(row[1]), str(row[2]), str(row[3]), owner_key, claim_expires_at)
                for row in rows
            )

    def acknowledge_dirty_roots(
        self, owner_key: str, roots: tuple[DirtyRoot, ...] | list[DirtyRoot], observed_at: str,
    ) -> int:
        observed_at = _timestamp(observed_at)
        if not roots:
            return 0
        with self._store.rollout_transaction() as connection:
            deleted = 0
            for root in roots:
                deleted += connection.execute(
                    """DELETE FROM sync_dirty_roots WHERE project_id=? AND root_key=? AND root_kind=?
                         AND claim_owner=? AND claim_expires_at>?""",
                    (root.project_id, root.root_key, root.root_kind, owner_key, observed_at),
                ).rowcount
            if deleted:
                self._bump_revision(connection, observed_at)
            return deleted

    def create_job(self, job_kind: str, created_at: str, job_id: str | None = None) -> str:
        created_at = _timestamp(created_at)
        if job_kind not in _JOB_KINDS:
            raise ValueError("sync job kind is invalid")
        identifier = job_id or f"sync_{uuid.uuid4().hex}"
        if not 1 <= len(identifier) <= 128:
            raise ValueError("sync job id is invalid")
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO sync_jobs(job_id,job_kind,state,sources_discovered,sources_completed,bytes_processed,
                       created_at,updated_at,completed_at) VALUES (?,?,'queued',0,0,0,?,?,NULL)""",
                (identifier, job_kind, created_at, created_at),
            )
            self._bump_revision(connection, created_at)
        return identifier

    @staticmethod
    def _job_from_row(row) -> SyncJob:
        return SyncJob(str(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[4]), int(row[5]),
                       str(row[6]), str(row[7]), None if row[8] is None else str(row[8]))

    def get_job(self, job_id: str) -> SyncJob | None:
        row = self._store.connection.execute(
            """SELECT job_id,job_kind,state,sources_discovered,sources_completed,bytes_processed,
                      created_at,updated_at,completed_at FROM sync_jobs WHERE job_id=?""", (job_id,),
        ).fetchone()
        return None if row is None else self._job_from_row(row)

    def current_job(self, job_kind: str) -> SyncJob | None:
        if job_kind not in _JOB_KINDS:
            raise ValueError("sync job kind is invalid")
        row = self._store.connection.execute(
            """SELECT job_id,job_kind,state,sources_discovered,sources_completed,bytes_processed,
                      created_at,updated_at,completed_at FROM sync_jobs
                 WHERE job_kind=? AND state IN ('queued','running') ORDER BY updated_at DESC,job_id DESC LIMIT 1""",
            (job_kind,),
        ).fetchone()
        return None if row is None else self._job_from_row(row)

    def list_jobs(self, limit: int = 100) -> tuple[SyncJob, ...]:
        return tuple(self._job_from_row(row) for row in self._store.connection.execute(
            """SELECT job_id,job_kind,state,sources_discovered,sources_completed,bytes_processed,
                      created_at,updated_at,completed_at FROM sync_jobs ORDER BY updated_at DESC,job_id DESC LIMIT ?""",
            (limit,),
        ))

    def update_job(self, job_id: str, *, state: str, sources_discovered: int, sources_completed: int,
                   bytes_processed: int, updated_at: str, completed_at: str | None = None) -> int:
        updated_at = _timestamp(updated_at)
        completed_at = None if completed_at is None else _timestamp(completed_at)
        if state not in _JOB_STATES or min(sources_discovered, sources_completed, bytes_processed) < 0:
            raise ValueError("sync job progress is invalid")
        if sources_completed > sources_discovered:
            raise ValueError("completed sources cannot exceed discovered sources")
        if state in {"succeeded", "partial", "failed"} and completed_at is None:
            completed_at = updated_at
        with self._store.rollout_transaction() as connection:
            if connection.execute(
                """UPDATE sync_jobs SET state=?,sources_discovered=?,sources_completed=?,bytes_processed=?,
                       updated_at=?,completed_at=? WHERE job_id=?""",
                (state, sources_discovered, sources_completed, bytes_processed, updated_at, completed_at, job_id),
            ).rowcount != 1:
                raise KeyError("sync job is unknown")
            return self._bump_revision(connection, updated_at)

    @staticmethod
    def _frontier_from_row(row) -> BackfillFrontier:
        return BackfillFrontier(*(str(value) if value is not None else None for value in row))  # type: ignore[arg-type]

    def save_frontier(self, *, job_id: str, root_kind: str, directory_locator: str, state: str,
                      discovered_count: int, updated_at: str) -> int:
        updated_at = _timestamp(updated_at)
        root = self._validate_root(root_kind)
        # ``@root`` is a private frontier sentinel, never a source locator or
        # public payload.  It lets a repair job persist that it has scanned the
        # root directory even when it contained no descendants.
        locator = directory_locator if directory_locator == "@root" else validate_root_relative_locator(directory_locator)
        if state not in _FRONTIER_STATES or discovered_count < 0:
            raise ValueError("backfill frontier is invalid")
        with self._store.rollout_transaction() as connection:
            connection.execute(
                """INSERT INTO sync_backfill_frontier(job_id,root_kind,directory_locator,state,discovered_count,updated_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(job_id,root_kind,directory_locator) DO UPDATE SET
                   state=excluded.state,discovered_count=excluded.discovered_count,updated_at=excluded.updated_at""",
                (job_id, root, locator, state, discovered_count, updated_at),
            )
            return self._bump_revision(connection, updated_at)

    def list_frontier(self, job_id: str, state: str | None = None) -> tuple[BackfillFrontier, ...]:
        if state is not None and state not in _FRONTIER_STATES:
            raise ValueError("backfill frontier state is invalid")
        query = """SELECT job_id,root_kind,directory_locator,state,discovered_count,updated_at
                     FROM sync_backfill_frontier WHERE job_id=?"""
        parameters: tuple[object, ...] = (job_id,)
        if state is not None:
            query += " AND state=?"
            parameters += (state,)
        query += " ORDER BY updated_at,root_kind,directory_locator"
        return tuple(self._frontier_from_row(row) for row in self._store.connection.execute(query, parameters))

    def resume_frontier(self, job_id: str) -> tuple[BackfillFrontier, ...]:
        return self.list_frontier(job_id, "pending")
