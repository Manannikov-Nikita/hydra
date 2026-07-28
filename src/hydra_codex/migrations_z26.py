"""Bounded materialized-project catalog stats and exact sync-job ordering."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3

from .exact_time import ExactInstant, require_exact_timestamp
from .migrations_w23 import W23_SYNC_JOBS_TABLE_SQL


Z26_MATERIALIZED_PROJECT_STATS_TABLE_SQL = """CREATE TABLE materialized_project_stats (
    project_id TEXT PRIMARY KEY,
    report_count INTEGER NOT NULL CHECK(report_count >= 0),
    first_reconciled_at TEXT NOT NULL,
    last_reconciled_at TEXT NOT NULL,
    first_activity_at TEXT,
    first_activity_epoch_ns INTEGER,
    last_activity_at TEXT,
    last_activity_epoch_ns INTEGER,
    data_revision INTEGER NOT NULL CHECK(data_revision >= 0),
    CHECK(first_reconciled_at <= last_reconciled_at),
    CHECK(
        (report_count = 0
         AND first_activity_at IS NULL AND first_activity_epoch_ns IS NULL
         AND last_activity_at IS NULL AND last_activity_epoch_ns IS NULL)
        OR
        (report_count > 0
         AND first_activity_at IS NOT NULL AND first_activity_epoch_ns IS NOT NULL
         AND last_activity_at IS NOT NULL AND last_activity_epoch_ns IS NOT NULL
         AND first_activity_epoch_ns <= last_activity_epoch_ns)
    )
) WITHOUT ROWID"""

Z26_SYNC_JOBS_TABLE_SQL = W23_SYNC_JOBS_TABLE_SQL.replace(
    "completed_at TEXT,",
    "completed_at TEXT, updated_epoch_ns INTEGER,",
)

Z26_SYNC_JOB_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_job_exact_updated_epoch_insert
BEFORE INSERT ON sync_jobs
WHEN NEW.updated_epoch_ns IS NULL
  OR typeof(NEW.updated_epoch_ns) != 'integer'
BEGIN
    SELECT RAISE(ABORT, 'sync job exact updated epoch is required');
END"""

Z26_SYNC_JOB_UPDATE_TRIGGER_SQL = """CREATE TRIGGER sync_job_exact_updated_epoch_update
BEFORE UPDATE ON sync_jobs
WHEN NEW.updated_epoch_ns IS NULL
  OR typeof(NEW.updated_epoch_ns) != 'integer'
  OR (
      NEW.updated_at != OLD.updated_at
      AND NEW.updated_epoch_ns = OLD.updated_epoch_ns
  )
BEGIN
    SELECT RAISE(ABORT, 'sync job exact updated epoch is required');
END"""

Z26_SNAPSHOT_REVISION_TRIGGER_SQL = """CREATE TRIGGER materialized_snapshot_stats_revision_insert
BEFORE INSERT ON materialized_report_snapshots
WHEN EXISTS (
    SELECT 1 FROM materialized_project_stats
     WHERE project_id=NEW.project_id AND data_revision!=NEW.data_revision
)
BEGIN
    SELECT RAISE(ABORT, 'materialized project stats revision mismatch');
END"""

Z26_SNAPSHOT_STATS_TRIGGER_SQL = """CREATE TRIGGER materialized_snapshot_stats_insert
AFTER INSERT ON materialized_report_snapshots
BEGIN
    INSERT INTO materialized_project_stats(
        project_id,report_count,first_reconciled_at,last_reconciled_at,
        first_activity_at,first_activity_epoch_ns,
        last_activity_at,last_activity_epoch_ns,data_revision
    ) VALUES (
        NEW.project_id,1,NEW.reconciled_at,NEW.reconciled_at,
        NEW.last_activity_at,NEW.last_activity_epoch_ns,
        NEW.last_activity_at,NEW.last_activity_epoch_ns,NEW.data_revision
    )
    ON CONFLICT(project_id) DO UPDATE SET
        report_count=materialized_project_stats.report_count+1,
        first_reconciled_at=MIN(
            materialized_project_stats.first_reconciled_at,
            excluded.first_reconciled_at
        ),
        last_reconciled_at=MAX(
            materialized_project_stats.last_reconciled_at,
            excluded.last_reconciled_at
        ),
        first_activity_at=CASE
            WHEN excluded.first_activity_epoch_ns
                 <materialized_project_stats.first_activity_epoch_ns
            THEN excluded.first_activity_at
            ELSE materialized_project_stats.first_activity_at
        END,
        first_activity_epoch_ns=MIN(
            materialized_project_stats.first_activity_epoch_ns,
            excluded.first_activity_epoch_ns
        ),
        last_activity_at=CASE
            WHEN excluded.last_activity_epoch_ns
                 >materialized_project_stats.last_activity_epoch_ns
            THEN excluded.last_activity_at
            ELSE materialized_project_stats.last_activity_at
        END,
        last_activity_epoch_ns=MAX(
            materialized_project_stats.last_activity_epoch_ns,
            excluded.last_activity_epoch_ns
        );
END"""

Z26_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (44, (
        Z26_MATERIALIZED_PROJECT_STATS_TABLE_SQL,
        "ALTER TABLE sync_jobs ADD COLUMN updated_epoch_ns INTEGER",
        "DROP INDEX sync_jobs_kind_updated",
        """CREATE INDEX sync_jobs_updated_epoch
               ON sync_jobs(updated_epoch_ns DESC,job_id DESC)""",
        """CREATE INDEX sync_jobs_active_updated_epoch
               ON sync_jobs(updated_epoch_ns DESC,job_id DESC)
               WHERE state IN ('queued','running')""",
        """CREATE INDEX sync_jobs_kind_active_updated_epoch
               ON sync_jobs(job_kind,updated_epoch_ns DESC,job_id DESC)
               WHERE state IN ('queued','running')""",
        """CREATE INDEX materialized_project_stats_revision
               ON materialized_project_stats(data_revision,project_id)""",
        Z26_SYNC_JOB_INSERT_TRIGGER_SQL,
        Z26_SYNC_JOB_UPDATE_TRIGGER_SQL,
        Z26_SNAPSHOT_REVISION_TRIGGER_SQL,
        Z26_SNAPSHOT_STATS_TRIGGER_SQL,
    )),
)

Z26_REQUIRED_SCHEMA = {
    "materialized_project_stats": {
        "project_id", "report_count", "first_reconciled_at",
        "last_reconciled_at", "first_activity_at",
        "first_activity_epoch_ns", "last_activity_at",
        "last_activity_epoch_ns", "data_revision",
    },
    "sync_jobs": {"updated_epoch_ns"},
}

Z26_REQUIRED_TRIGGER_SQL = {
    "sync_job_exact_updated_epoch_insert": Z26_SYNC_JOB_INSERT_TRIGGER_SQL,
    "sync_job_exact_updated_epoch_update": Z26_SYNC_JOB_UPDATE_TRIGGER_SQL,
    "materialized_snapshot_stats_revision_insert":
        Z26_SNAPSHOT_REVISION_TRIGGER_SQL,
    "materialized_snapshot_stats_insert": Z26_SNAPSHOT_STATS_TRIGGER_SQL,
}


@dataclass
class _ProjectStats:
    count: int
    first_reconciled: ExactInstant
    last_reconciled: ExactInstant
    first_activity: ExactInstant
    last_activity: ExactInstant
    revision: int


def _snapshot_stats(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    from .dashboard_contract import validate_task_report

    projects: dict[str, _ProjectStats] = {}
    for row in connection.execute(
        """SELECT project_id,task_ref,report_json,reconciled_at,data_revision,
                  last_activity_at,last_activity_epoch_ns
             FROM materialized_report_snapshots
            ORDER BY project_id,task_ref""",
    ):
        try:
            project_id = str(row[0])
            payload = json.loads(str(row[2]))
            validate_task_report(
                payload, allow_legacy_without_sync_freshness=True,
            )
            if payload.get("task_ref") != row[1]:
                raise ValueError("task reference mismatch")
            reconciled = require_exact_timestamp(
                row[3], "materialized reconciliation timestamp",
            )
            revision = row[4]
            activity = require_exact_timestamp(
                row[5], "materialized activity timestamp",
            )
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
                or isinstance(row[6], bool)
                or not isinstance(row[6], int)
                or activity.epoch_nanoseconds != row[6]
                or payload.get("last_activity_at") != row[5]
            ):
                raise ValueError("materialized snapshot metadata mismatch")
            current = projects.get(project_id)
            if current is None:
                projects[project_id] = _ProjectStats(
                    1, reconciled, reconciled, activity, activity, revision,
                )
                continue
            if current.revision != revision:
                raise ValueError("materialized project revision mismatch")
            current.count += 1
            current.first_reconciled = min(
                current.first_reconciled, reconciled,
                key=lambda value: value.epoch_nanoseconds,
            )
            current.last_reconciled = max(
                current.last_reconciled, reconciled,
                key=lambda value: value.epoch_nanoseconds,
            )
            current.first_activity = min(
                current.first_activity, activity,
                key=lambda value: value.epoch_nanoseconds,
            )
            current.last_activity = max(
                current.last_activity, activity,
                key=lambda value: value.epoch_nanoseconds,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise sqlite3.IntegrityError(
                "materialized project stats cannot be migrated",
            ) from error
    return tuple(
        (
            project_id, stats.count,
            stats.first_reconciled.canonical,
            stats.last_reconciled.canonical,
            stats.first_activity.canonical,
            stats.first_activity.epoch_nanoseconds,
            stats.last_activity.canonical,
            stats.last_activity.epoch_nanoseconds,
            stats.revision,
        )
        for project_id, stats in sorted(projects.items())
    )


def _queue_legacy_materialization(
    connection: sqlite3.Connection,
) -> None:
    """Resume pre-v42 reconciliations through the normal bounded dirty-root path."""
    project_times: dict[str, ExactInstant] = {}
    for project_id, started_at, completed_at in connection.execute(
        """SELECT project_id,started_at,completed_at
             FROM reconciliation_runs
            WHERE outcome='success'
            ORDER BY project_id,run_id""",
    ):
        try:
            project = str(project_id)
            if not 1 <= len(project) <= 160:
                raise ValueError("legacy project identity is invalid")
            observed = require_exact_timestamp(
                completed_at if completed_at is not None else started_at,
                "legacy reconciliation timestamp",
            )
        except (TypeError, ValueError) as error:
            raise sqlite3.IntegrityError(
                "legacy materialization cannot be resumed",
            ) from error
        current = project_times.get(project)
        if (
            current is None
            or observed.epoch_nanoseconds > current.epoch_nanoseconds
        ):
            project_times[project] = observed

    reconciled: dict[str, set[str]] = {}
    for project_id, public_ref in connection.execute(
        """SELECT project_id,public_ref FROM reconciled_tasks
            ORDER BY project_id,public_ref""",
    ):
        reconciled.setdefault(str(project_id), set()).add(str(public_ref))
    materialized: dict[str, set[str]] = {}
    for project_id, task_ref in connection.execute(
        """SELECT project_id,task_ref FROM materialized_report_snapshots
            ORDER BY project_id,task_ref""",
    ):
        materialized.setdefault(str(project_id), set()).add(str(task_ref))
    counts = {
        str(project_id): int(report_count)
        for project_id, report_count in connection.execute(
            """SELECT project_id,report_count FROM materialized_project_stats
                ORDER BY project_id""",
        )
    }
    pending = tuple(
        (
            project,
            project,
            "project",
            observed.canonical,
        )
        for project, observed in sorted(project_times.items())
        if (
            project not in counts
            or counts[project] != len(reconciled.get(project, ()))
            or materialized.get(project, set()) != reconciled.get(project, set())
        )
    )
    connection.executemany(
        """INSERT INTO sync_dirty_roots(
               project_id,root_key,root_kind,observed_at,
               claim_owner,claim_expires_at)
           VALUES (?,?,?,?,NULL,NULL)
           ON CONFLICT(project_id,root_key,root_kind) DO UPDATE SET
             observed_at=MAX(observed_at,excluded.observed_at)""",
        pending,
    )
    if pending:
        updated_at = max(
            (require_exact_timestamp(row[3]) for row in pending),
            key=lambda value: value.epoch_nanoseconds,
        ).canonical
        connection.execute(
            """UPDATE sync_data_revision
                  SET revision=revision+1,updated_at=?
                WHERE singleton=1""",
            (updated_at,),
        )


def backfill_bounded_materialized_indexes(
    connection: sqlite3.Connection,
) -> None:
    """Backfill v44 summaries once; normal hot reads remain bounded thereafter."""
    stats = _snapshot_stats(connection)
    connection.executemany(
        """INSERT INTO materialized_project_stats(
               project_id,report_count,first_reconciled_at,last_reconciled_at,
               first_activity_at,first_activity_epoch_ns,
               last_activity_at,last_activity_epoch_ns,data_revision)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        stats,
    )
    updates: list[tuple[int, str]] = []
    for job_id, updated_at in connection.execute(
        "SELECT job_id,updated_at FROM sync_jobs ORDER BY job_id",
    ):
        try:
            epoch = require_exact_timestamp(
                updated_at, "sync job updated timestamp",
            ).epoch_nanoseconds
        except (TypeError, ValueError) as error:
            raise sqlite3.IntegrityError(
                "sync job exact updated epoch cannot be migrated",
            ) from error
        updates.append((epoch, str(job_id)))
    connection.executemany(
        "UPDATE sync_jobs SET updated_epoch_ns=? WHERE job_id=?",
        updates,
    )
    _queue_legacy_materialization(connection)
