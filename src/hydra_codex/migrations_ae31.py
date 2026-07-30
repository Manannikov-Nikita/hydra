"""Fence resumable repair frontiers against stale maintenance writers."""

from __future__ import annotations


_AE31_V51_FRONTIER_ACTIVE_PARENT_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_backfill_frontier_active_parent_insert
BEFORE INSERT ON sync_backfill_frontier
WHEN NOT EXISTS (
    SELECT 1
      FROM sync_jobs
     WHERE job_id=NEW.job_id
       AND job_kind IN ('backfill','repair')
       AND state IN ('queued','running')
)
BEGIN
    SELECT RAISE(IGNORE);
END"""


_AE31_V51_FRONTIER_MONOTONIC_UPDATE_TRIGGER_SQL = """CREATE TRIGGER sync_backfill_frontier_monotonic_update
BEFORE UPDATE ON sync_backfill_frontier
WHEN NOT EXISTS (
    SELECT 1
      FROM sync_jobs
     WHERE job_id=NEW.job_id
       AND job_kind IN ('backfill','repair')
       AND state IN ('queued','running')
)
OR NOT (
    (OLD.state='pending'
     AND NEW.state IN ('pending','scanned','repair_required'))
    OR (OLD.state='scanned' AND NEW.state='scanned')
    OR (OLD.state='repair_required' AND NEW.state='repair_required')
)
BEGIN
    SELECT RAISE(IGNORE);
END"""


AE31_FRONTIER_ACTIVE_PARENT_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_backfill_frontier_active_parent_insert
BEFORE INSERT ON sync_backfill_frontier
WHEN EXISTS (
    SELECT 1
      FROM sync_jobs
     WHERE job_id=NEW.job_id
       AND (
           job_kind NOT IN ('backfill','repair')
           OR state NOT IN ('queued','running')
       )
)
BEGIN
    SELECT RAISE(IGNORE);
END"""


AE31_FRONTIER_MONOTONIC_UPDATE_TRIGGER_SQL = """CREATE TRIGGER sync_backfill_frontier_monotonic_update
BEFORE UPDATE ON sync_backfill_frontier
WHEN EXISTS (
    SELECT 1 FROM sync_jobs WHERE job_id=NEW.job_id
)
AND (
    EXISTS (
        SELECT 1
          FROM sync_jobs
         WHERE job_id=NEW.job_id
           AND (
               job_kind NOT IN ('backfill','repair')
               OR state NOT IN ('queued','running')
           )
    )
    OR NOT (
        (OLD.state='pending'
         AND NEW.state IN ('pending','scanned','repair_required'))
        OR (OLD.state='scanned' AND NEW.state='scanned')
        OR (OLD.state='repair_required' AND NEW.state='repair_required')
    )
)
BEGIN
    SELECT RAISE(IGNORE);
END"""


AE31_REQUIRED_TRIGGER_SQL = {
    "sync_backfill_frontier_active_parent_insert":
        AE31_FRONTIER_ACTIVE_PARENT_INSERT_TRIGGER_SQL,
    "sync_backfill_frontier_monotonic_update":
        AE31_FRONTIER_MONOTONIC_UPDATE_TRIGGER_SQL,
}


AE31_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        51,
        (
            _AE31_V51_FRONTIER_ACTIVE_PARENT_INSERT_TRIGGER_SQL,
            _AE31_V51_FRONTIER_MONOTONIC_UPDATE_TRIGGER_SQL,
        ),
    ),
    (
        52,
        (
            "DROP TRIGGER IF EXISTS sync_backfill_frontier_active_parent_insert",
            "DROP TRIGGER IF EXISTS sync_backfill_frontier_monotonic_update",
            *AE31_REQUIRED_TRIGGER_SQL.values(),
        ),
    ),
)
