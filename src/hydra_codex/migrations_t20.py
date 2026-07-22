"""Persist privacy-safe, append-only canonical audit storage snapshots."""

from __future__ import annotations


T20_STORAGE_AUDIT_SNAPSHOTS_TABLE_SQL = """CREATE TABLE storage_audit_snapshots (
       snapshot_id TEXT PRIMARY KEY,
       project_id TEXT NOT NULL,
       observed_at TEXT NOT NULL,
       audit_sha256 TEXT NOT NULL,
       database_bytes INTEGER NOT NULL CHECK(database_bytes >= 0),
       wal_bytes INTEGER NOT NULL CHECK(wal_bytes >= 0),
       rollout_sources INTEGER NOT NULL CHECK(rollout_sources >= 0),
       rollout_events INTEGER NOT NULL CHECK(rollout_events >= 0),
       codex_event_sources INTEGER NOT NULL CHECK(codex_event_sources >= 0),
       codex_events INTEGER NOT NULL CHECK(codex_events >= 0),
       schema_version INTEGER NOT NULL CHECK(schema_version > 0),
       UNIQUE(project_id,audit_sha256)
   ) WITHOUT ROWID"""


T20_TRIGGER_STATEMENTS: tuple[str, ...] = (
    """CREATE TRIGGER storage_audit_snapshots_immutable_update
           BEFORE UPDATE ON storage_audit_snapshots BEGIN
               SELECT RAISE(ABORT, 'storage audit snapshots are immutable');
           END""",
    """CREATE TRIGGER storage_audit_snapshots_immutable_delete
           BEFORE DELETE ON storage_audit_snapshots BEGIN
               SELECT RAISE(ABORT, 'storage audit snapshots are immutable');
           END""",
)


T20_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (36, (
        T20_STORAGE_AUDIT_SNAPSHOTS_TABLE_SQL,
        """CREATE INDEX storage_audit_snapshots_project_time
               ON storage_audit_snapshots(project_id,observed_at,snapshot_id)""",
        *T20_TRIGGER_STATEMENTS,
    )),
)


T20_REQUIRED_SCHEMA: dict[str, set[str]] = {
    "storage_audit_snapshots": {
        "snapshot_id", "project_id", "observed_at", "audit_sha256",
        "database_bytes", "wal_bytes", "rollout_sources", "rollout_events",
        "codex_event_sources", "codex_events", "schema_version",
    },
}
