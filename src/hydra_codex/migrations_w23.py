"""Persist private, resumable state for incremental telemetry synchronization."""

from __future__ import annotations


W23_SYNC_SOURCE_REGISTRY_TABLE_SQL = """CREATE TABLE sync_source_registry (
    source_locator TEXT PRIMARY KEY,
    root_kind TEXT NOT NULL CHECK(root_kind IN ('sessions','archived_sessions')),
    source_state TEXT NOT NULL DEFAULT 'ready'
        CHECK(source_state IN ('ready','repair_required','missing')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CHECK(length(source_locator) BETWEEN 1 AND 512),
    CHECK(source_locator NOT LIKE '/%'),
    CHECK(instr(source_locator, char(92)) = 0),
    CHECK(instr(source_locator, '..') = 0)
) WITHOUT ROWID"""

W23_SYNC_SOURCE_CHECKPOINTS_TABLE_SQL = """CREATE TABLE sync_source_checkpoints (
    source_locator TEXT PRIMARY KEY REFERENCES sync_source_registry(source_locator)
        ON DELETE CASCADE,
    device_id INTEGER,
    inode INTEGER,
    file_size INTEGER NOT NULL DEFAULT 0 CHECK(file_size >= 0),
    byte_offset INTEGER NOT NULL DEFAULT 0 CHECK(byte_offset >= 0),
    line_number INTEGER NOT NULL DEFAULT 0 CHECK(line_number >= 0),
    prefix_anchor TEXT,
    revision_anchor TEXT,
    updated_at TEXT NOT NULL,
    CHECK(prefix_anchor IS NULL OR length(prefix_anchor) = 64),
    CHECK(revision_anchor IS NULL OR length(revision_anchor) = 64)
) WITHOUT ROWID"""

W23_SYNC_INGEST_QUEUE_TABLE_SQL = """CREATE TABLE sync_ingest_queue (
    source_locator TEXT PRIMARY KEY REFERENCES sync_source_registry(source_locator)
        ON DELETE CASCADE,
    enqueued_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0)
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
    PRIMARY KEY(project_id,root_key,root_kind),
    CHECK(length(project_id) BETWEEN 1 AND 160),
    CHECK(length(root_key) BETWEEN 1 AND 160)
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
    root_kind TEXT NOT NULL CHECK(root_kind IN ('sessions','archived_sessions')),
    directory_locator TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','scanned','repair_required')),
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK(discovered_count >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(root_kind,directory_locator),
    CHECK(length(directory_locator) BETWEEN 1 AND 512),
    CHECK(directory_locator NOT LIKE '/%'),
    CHECK(instr(directory_locator, char(92)) = 0),
    CHECK(instr(directory_locator, '..') = 0)
) WITHOUT ROWID"""

W23_SYNC_DATA_REVISION_TABLE_SQL = """CREATE TABLE sync_data_revision (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    updated_at TEXT NOT NULL
) WITHOUT ROWID"""


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
        "INSERT INTO sync_data_revision(singleton,revision,updated_at) VALUES (1,0,datetime('now'))",
        "CREATE INDEX sync_ingest_queue_enqueued ON sync_ingest_queue(enqueued_at,source_locator)",
        "CREATE INDEX sync_dirty_roots_project ON sync_dirty_roots(project_id,root_kind,observed_at)",
        "CREATE INDEX sync_jobs_kind_updated ON sync_jobs(job_kind,updated_at DESC)",
    )),
)


W23_REQUIRED_SCHEMA: dict[str, set[str]] = {
    "sync_source_registry": {
        "source_locator", "root_kind", "source_state", "first_seen_at", "last_seen_at",
    },
    "sync_source_checkpoints": {
        "source_locator", "device_id", "inode", "file_size", "byte_offset", "line_number",
        "prefix_anchor", "revision_anchor", "updated_at",
    },
    "sync_ingest_queue": {"source_locator", "enqueued_at", "attempts"},
    "sync_worker_leases": {"lease_name", "owner_key", "acquired_at", "expires_at"},
    "sync_dirty_roots": {"project_id", "root_key", "root_kind", "observed_at"},
    "sync_jobs": {
        "job_id", "job_kind", "state", "sources_discovered", "sources_completed",
        "bytes_processed", "created_at", "updated_at", "completed_at",
    },
    "sync_backfill_frontier": {
        "root_kind", "directory_locator", "state", "discovered_count", "updated_at",
    },
    "sync_data_revision": {"singleton", "revision", "updated_at"},
}
