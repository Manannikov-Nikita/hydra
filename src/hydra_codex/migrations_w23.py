"""Persist private, resumable state for incremental telemetry synchronization."""

from __future__ import annotations


W23_SYNC_SOURCE_REGISTRY_TABLE_SQL = """CREATE TABLE sync_source_registry (
    root_kind TEXT NOT NULL CHECK(root_kind IN ('sessions','archived_sessions')),
    source_locator TEXT NOT NULL,
    source_state TEXT NOT NULL DEFAULT 'ready'
        CHECK(source_state IN ('ready','repair_required','missing')),
    project_id TEXT,
    logical_source_key TEXT,
    session_key TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(root_kind,source_locator),
    CHECK(length(source_locator) BETWEEN 1 AND 512),
    CHECK(length(project_id) <= 160),
    CHECK(length(logical_source_key) <= 160),
    CHECK(length(session_key) <= 160)
) WITHOUT ROWID"""

W23_SYNC_SOURCE_CHECKPOINTS_TABLE_SQL = """CREATE TABLE sync_source_checkpoints (
    root_kind TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    device_id INTEGER,
    inode INTEGER,
    file_size INTEGER NOT NULL DEFAULT 0 CHECK(file_size >= 0),
    byte_offset INTEGER NOT NULL DEFAULT 0 CHECK(byte_offset >= 0 AND byte_offset <= file_size),
    line_number INTEGER NOT NULL DEFAULT 0 CHECK(line_number >= 0),
    prefix_anchor TEXT,
    revision_anchor TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(root_kind,source_locator),
    FOREIGN KEY(root_kind,source_locator)
        REFERENCES sync_source_registry(root_kind,source_locator) ON DELETE CASCADE,
    CHECK(prefix_anchor IS NULL OR length(prefix_anchor) = 64),
    CHECK(revision_anchor IS NULL OR length(revision_anchor) = 64)
) WITHOUT ROWID"""

W23_SYNC_INGEST_QUEUE_TABLE_SQL = """CREATE TABLE sync_ingest_queue (
    root_kind TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    queue_state TEXT NOT NULL DEFAULT 'queued' CHECK(queue_state IN ('queued','claimed')),
    enqueued_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    claim_expires_at TEXT,
    requeue_pending INTEGER NOT NULL DEFAULT 0 CHECK(requeue_pending IN (0,1)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    reason_code TEXT,
    PRIMARY KEY(root_kind,source_locator),
    FOREIGN KEY(root_kind,source_locator)
        REFERENCES sync_source_registry(root_kind,source_locator) ON DELETE CASCADE,
    CHECK((queue_state='queued' AND claimed_by IS NULL AND claimed_at IS NULL AND claim_expires_at IS NULL)
          OR (queue_state='claimed' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL
              AND claim_expires_at IS NOT NULL AND claim_expires_at > claimed_at)),
    CHECK(claimed_by IS NULL OR length(claimed_by) BETWEEN 1 AND 128),
    CHECK(reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64)
) WITHOUT ROWID"""

W23_SYNC_WORKER_LEASES_TABLE_SQL = """CREATE TABLE sync_worker_leases (
    lease_name TEXT PRIMARY KEY CHECK(lease_name = 'ingest'),
    owner_key TEXT NOT NULL CHECK(length(owner_key) BETWEEN 1 AND 128),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK(expires_at > acquired_at)
) WITHOUT ROWID"""

W23_SYNC_DIRTY_ROOTS_TABLE_SQL = """CREATE TABLE sync_dirty_roots (
    project_id TEXT NOT NULL,
    root_key TEXT NOT NULL,
    root_kind TEXT NOT NULL CHECK(root_kind IN ('project','task')),
    observed_at TEXT NOT NULL,
    claim_owner TEXT,
    claim_expires_at TEXT,
    PRIMARY KEY(project_id,root_key,root_kind),
    CHECK(length(project_id) BETWEEN 1 AND 160),
    CHECK(length(root_key) BETWEEN 1 AND 160),
    CHECK((claim_owner IS NULL AND claim_expires_at IS NULL)
          OR (claim_owner IS NOT NULL AND claim_expires_at IS NOT NULL)),
    CHECK(claim_owner IS NULL OR length(claim_owner) BETWEEN 1 AND 128)
) WITHOUT ROWID"""

W23_SYNC_JOBS_TABLE_SQL = """CREATE TABLE sync_jobs (
    job_id TEXT PRIMARY KEY,
    job_kind TEXT NOT NULL CHECK(job_kind IN ('sync','backfill','repair')),
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','partial','failed')),
    sources_discovered INTEGER NOT NULL DEFAULT 0 CHECK(sources_discovered >= 0),
    sources_completed INTEGER NOT NULL DEFAULT 0 CHECK(sources_completed >= 0),
    bytes_processed INTEGER NOT NULL DEFAULT 0 CHECK(bytes_processed >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK(length(job_id) BETWEEN 1 AND 128),
    CHECK(sources_completed <= sources_discovered)
) WITHOUT ROWID"""

W23_SYNC_BACKFILL_FRONTIER_TABLE_SQL = """CREATE TABLE sync_backfill_frontier (
    job_id TEXT NOT NULL REFERENCES sync_jobs(job_id) ON DELETE CASCADE,
    root_kind TEXT NOT NULL CHECK(root_kind IN ('sessions','archived_sessions')),
    directory_locator TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','scanned','repair_required')),
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK(discovered_count >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id,root_kind,directory_locator),
    CHECK(length(directory_locator) BETWEEN 1 AND 512)
) WITHOUT ROWID"""

W23_SYNC_DATA_REVISION_TABLE_SQL = """CREATE TABLE sync_data_revision (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    updated_at TEXT NOT NULL
) WITHOUT ROWID"""


def _locator_trigger(table: str, column: str, action: str) -> str:
    return f"""CREATE TRIGGER {table}_canonical_locator_{action.lower()}
        BEFORE {action} ON {table}
        WHEN NOT (
            typeof(NEW.{column})='text'
            AND length(NEW.{column}) BETWEEN 1 AND 512
            AND length(CAST(NEW.{column} AS BLOB))=length(NEW.{column})
            AND NEW.{column} NOT GLOB '*[^ -~]*'
            AND NEW.{column} NOT LIKE '/%'
            AND instr(NEW.{column},char(92))=0
            AND instr(NEW.{column},'//')=0
            AND NEW.{column} NOT IN ('.','..')
            AND NEW.{column} NOT LIKE './%'
            AND NEW.{column} NOT LIKE '../%'
            AND NEW.{column} NOT LIKE '%/./%'
            AND NEW.{column} NOT LIKE '%/../%'
            AND NEW.{column} NOT LIKE '%/.'
            AND NEW.{column} NOT LIKE '%/..'
        ) BEGIN SELECT RAISE(ABORT,'noncanonical private source locator'); END"""


W23_LOCATOR_TRIGGER_STATEMENTS: tuple[str, ...] = tuple(
    _locator_trigger(table, column, action)
    for table, column in (
        ("sync_source_registry", "source_locator"),
        ("sync_backfill_frontier", "directory_locator"),
    )
    for action in ("INSERT", "UPDATE")
)


W23_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (39, (
        W23_SYNC_SOURCE_REGISTRY_TABLE_SQL,
        W23_SYNC_SOURCE_CHECKPOINTS_TABLE_SQL,
        W23_SYNC_INGEST_QUEUE_TABLE_SQL,
        W23_SYNC_WORKER_LEASES_TABLE_SQL,
        W23_SYNC_DIRTY_ROOTS_TABLE_SQL,
        W23_SYNC_JOBS_TABLE_SQL,
        W23_SYNC_BACKFILL_FRONTIER_TABLE_SQL,
        W23_SYNC_DATA_REVISION_TABLE_SQL,
        *W23_LOCATOR_TRIGGER_STATEMENTS,
        "INSERT INTO sync_data_revision(singleton,revision,updated_at) VALUES (1,0,datetime('now'))",
        "CREATE INDEX sync_ingest_queue_available ON sync_ingest_queue(queue_state,available_at,enqueued_at)",
        "CREATE INDEX sync_dirty_roots_project ON sync_dirty_roots(project_id,root_kind,observed_at)",
        "CREATE INDEX sync_dirty_roots_claim ON sync_dirty_roots(claim_expires_at,observed_at)",
        "CREATE INDEX sync_jobs_kind_updated ON sync_jobs(job_kind,updated_at DESC)",
        "CREATE INDEX sync_frontier_resume ON sync_backfill_frontier(job_id,state,updated_at)",
    )),
)


W23_REQUIRED_SCHEMA: dict[str, set[str]] = {
    "sync_source_registry": {
        "root_kind", "source_locator", "source_state", "project_id", "logical_source_key",
        "session_key", "first_seen_at", "last_seen_at",
    },
    "sync_source_checkpoints": {
        "root_kind", "source_locator", "device_id", "inode", "file_size", "byte_offset",
        "line_number", "prefix_anchor", "revision_anchor", "updated_at",
    },
    "sync_ingest_queue": {
        "root_kind", "source_locator", "queue_state", "enqueued_at", "available_at",
        "claimed_by", "claimed_at", "claim_expires_at", "requeue_pending", "attempts", "reason_code",
    },
    "sync_worker_leases": {"lease_name", "owner_key", "acquired_at", "expires_at"},
    "sync_dirty_roots": {
        "project_id", "root_key", "root_kind", "observed_at", "claim_owner", "claim_expires_at",
    },
    "sync_jobs": {
        "job_id", "job_kind", "state", "sources_discovered", "sources_completed",
        "bytes_processed", "created_at", "updated_at", "completed_at",
    },
    "sync_backfill_frontier": {
        "job_id", "root_kind", "directory_locator", "state", "discovered_count", "updated_at",
    },
    "sync_data_revision": {"singleton", "revision", "updated_at"},
}


W23_REQUIRED_TABLE_SQL: dict[str, str] = {
    "sync_source_registry": W23_SYNC_SOURCE_REGISTRY_TABLE_SQL,
    "sync_source_checkpoints": W23_SYNC_SOURCE_CHECKPOINTS_TABLE_SQL,
    "sync_ingest_queue": W23_SYNC_INGEST_QUEUE_TABLE_SQL,
    "sync_worker_leases": W23_SYNC_WORKER_LEASES_TABLE_SQL,
    "sync_dirty_roots": W23_SYNC_DIRTY_ROOTS_TABLE_SQL,
    "sync_jobs": W23_SYNC_JOBS_TABLE_SQL,
    "sync_backfill_frontier": W23_SYNC_BACKFILL_FRONTIER_TABLE_SQL,
    "sync_data_revision": W23_SYNC_DATA_REVISION_TABLE_SQL,
}


W23_REQUIRED_TRIGGER_SQL: dict[str, str] = {
    statement.split()[2]: statement for statement in W23_LOCATOR_TRIGGER_STATEMENTS
}
