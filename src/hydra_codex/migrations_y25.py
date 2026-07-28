"""Index exact materialized report activity for bounded recent-report reads."""

from __future__ import annotations

import json
import sqlite3

from .exact_time import require_exact_timestamp


Y25_MATERIALIZED_REPORT_SNAPSHOTS_TABLE_SQL = """CREATE TABLE materialized_report_snapshots (
    project_id TEXT NOT NULL,
    task_ref TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_markdown TEXT NOT NULL,
    report_html TEXT NOT NULL,
    reconciled_at TEXT NOT NULL,
    data_revision INTEGER NOT NULL,
    last_activity_at TEXT,
    last_activity_epoch_ns INTEGER,
    PRIMARY KEY(project_id,task_ref)
) WITHOUT ROWID"""

Y25_MATERIALIZED_REPORT_ACTIVITY_INSERT_TRIGGER_SQL = """CREATE TRIGGER materialized_report_activity_required_insert
BEFORE INSERT ON materialized_report_snapshots
WHEN NEW.last_activity_at IS NULL
  OR NEW.last_activity_epoch_ns IS NULL
  OR typeof(NEW.last_activity_epoch_ns) != 'integer'
BEGIN
    SELECT RAISE(ABORT, 'materialized report activity is required');
END"""

Y25_MATERIALIZED_REPORT_ACTIVITY_UPDATE_TRIGGER_SQL = """CREATE TRIGGER materialized_report_activity_required_update
BEFORE UPDATE ON materialized_report_snapshots
WHEN NEW.last_activity_at IS NULL
  OR NEW.last_activity_epoch_ns IS NULL
  OR typeof(NEW.last_activity_epoch_ns) != 'integer'
BEGIN
    SELECT RAISE(ABORT, 'materialized report activity is required');
END"""


Y25_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (43, (
        "ALTER TABLE materialized_report_snapshots ADD COLUMN last_activity_at TEXT",
        "ALTER TABLE materialized_report_snapshots ADD COLUMN last_activity_epoch_ns INTEGER",
        "DROP INDEX materialized_report_snapshots_project",
        """CREATE INDEX materialized_report_snapshots_recent
               ON materialized_report_snapshots(
                   project_id,last_activity_epoch_ns DESC,task_ref
               )""",
        Y25_MATERIALIZED_REPORT_ACTIVITY_INSERT_TRIGGER_SQL,
        Y25_MATERIALIZED_REPORT_ACTIVITY_UPDATE_TRIGGER_SQL,
    )),
)

Y25_REQUIRED_SCHEMA = {
    "materialized_report_snapshots": {
        "project_id",
        "task_ref",
        "report_json",
        "report_markdown",
        "report_html",
        "reconciled_at",
        "data_revision",
        "last_activity_at",
        "last_activity_epoch_ns",
    },
}

Y25_REQUIRED_TRIGGER_SQL = {
    "materialized_report_activity_required_insert":
        Y25_MATERIALIZED_REPORT_ACTIVITY_INSERT_TRIGGER_SQL,
    "materialized_report_activity_required_update":
        Y25_MATERIALIZED_REPORT_ACTIVITY_UPDATE_TRIGGER_SQL,
}


def backfill_materialized_report_activity(
    connection: sqlite3.Connection,
) -> None:
    """Validate v42 public snapshots and persist an exact indexed sort instant."""
    from .dashboard_contract import validate_task_report
    from .public_payload import reject_private_fields

    rows = tuple(connection.execute(
        """SELECT project_id,task_ref,report_json
             FROM materialized_report_snapshots
            ORDER BY project_id,task_ref""",
    ))
    updates: list[tuple[str, int, str, str]] = []
    for project_id, task_ref, serialized in rows:
        try:
            payload = json.loads(str(serialized))
            validate_task_report(
                payload, allow_legacy_without_sync_freshness=True,
            )
            reject_private_fields(payload)
            if payload.get("task_ref") != task_ref:
                raise ValueError("task reference mismatch")
            activity = str(payload["last_activity_at"])
            instant = require_exact_timestamp(
                activity, "materialized report activity timestamp",
            )
            if not -(1 << 63) <= instant.epoch_nanoseconds < (1 << 63):
                raise ValueError("activity instant exceeds SQLite integer range")
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise sqlite3.IntegrityError(
                "materialized report activity cannot be migrated",
            ) from error
        updates.append((
            activity,
            instant.epoch_nanoseconds,
            str(project_id),
            str(task_ref),
        ))
    connection.executemany(
        """UPDATE materialized_report_snapshots
              SET last_activity_at=?,last_activity_epoch_ns=?
            WHERE project_id=? AND task_ref=?""",
        updates,
    )
