"""Bounded durable summaries and exact eligibility ordering for sync work."""

from __future__ import annotations

import sqlite3

from .exact_time import require_exact_timestamp
from .migrations_w23 import (
    W23_SYNC_DIRTY_ROOTS_TABLE_SQL,
    W23_SYNC_INGEST_QUEUE_TABLE_SQL,
)


AA27_SYNC_INGEST_QUEUE_TABLE_SQL = W23_SYNC_INGEST_QUEUE_TABLE_SQL.replace(
    "    reason_code TEXT,\n    PRIMARY KEY",
    "    reason_code TEXT,\n    eligible_epoch_ns INTEGER,\n    PRIMARY KEY",
)
AA27_SYNC_DIRTY_ROOTS_TABLE_SQL = W23_SYNC_DIRTY_ROOTS_TABLE_SQL.replace(
    "    claim_expires_at TEXT,\n    PRIMARY KEY",
    "    claim_expires_at TEXT,\n    eligible_epoch_ns INTEGER,\n    PRIMARY KEY",
)

AA27_SYNC_WORK_SUMMARY_TABLE_SQL = """CREATE TABLE sync_work_summary (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    ingest_total INTEGER NOT NULL CHECK(ingest_total >= 0),
    outbox_total INTEGER NOT NULL CHECK(outbox_total >= 0),
    dirty_total INTEGER NOT NULL CHECK(dirty_total >= 0)
) WITHOUT ROWID"""

AA27_QUEUE_ELIGIBILITY_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_queue_eligibility_insert
BEFORE INSERT ON sync_ingest_queue
WHEN typeof(NEW.eligible_epoch_ns) != 'integer'
  OR NEW.eligible_epoch_ns != CASE NEW.queue_state
      WHEN 'queued' THEN hydra_rfc3339_nanos(NEW.available_at)
      ELSE hydra_rfc3339_nanos(NEW.claim_expires_at)
  END
BEGIN
    SELECT RAISE(ABORT, 'sync queue exact eligibility is required');
END"""

AA27_QUEUE_ELIGIBILITY_UPDATE_TRIGGER_SQL = """CREATE TRIGGER sync_queue_eligibility_update
BEFORE UPDATE ON sync_ingest_queue
WHEN typeof(NEW.eligible_epoch_ns) != 'integer'
  OR NEW.eligible_epoch_ns != CASE NEW.queue_state
      WHEN 'queued' THEN hydra_rfc3339_nanos(NEW.available_at)
      ELSE hydra_rfc3339_nanos(NEW.claim_expires_at)
  END
BEGIN
    SELECT RAISE(ABORT, 'sync queue exact eligibility is required');
END"""

AA27_OUTBOX_ELIGIBILITY_INSERT_TRIGGER_SQL = """CREATE TRIGGER hook_outbox_eligibility_insert
BEFORE INSERT ON hook_event_outbox
WHEN NOT (
    (
        NEW.acknowledged_at IS NULL
        AND typeof(NEW.eligible_epoch_ns) = 'integer'
        AND NEW.eligible_epoch_ns = CASE
            WHEN NEW.claimed_by IS NULL
            THEN hydra_rfc3339_nanos(NEW.observed_at)
            ELSE hydra_rfc3339_nanos(NEW.claim_expires_at)
        END
    )
    OR (NEW.acknowledged_at IS NOT NULL AND NEW.eligible_epoch_ns IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'hook outbox exact eligibility is required');
END"""

AA27_OUTBOX_ELIGIBILITY_UPDATE_TRIGGER_SQL = """CREATE TRIGGER hook_outbox_eligibility_update
BEFORE UPDATE ON hook_event_outbox
WHEN NOT (
    (
        NEW.acknowledged_at IS NULL
        AND typeof(NEW.eligible_epoch_ns) = 'integer'
        AND NEW.eligible_epoch_ns = CASE
            WHEN NEW.claimed_by IS NULL
            THEN hydra_rfc3339_nanos(NEW.observed_at)
            ELSE hydra_rfc3339_nanos(NEW.claim_expires_at)
        END
    )
    OR (NEW.acknowledged_at IS NOT NULL AND NEW.eligible_epoch_ns IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'hook outbox exact eligibility is required');
END"""

AA27_DIRTY_ELIGIBILITY_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_dirty_eligibility_insert
BEFORE INSERT ON sync_dirty_roots
WHEN typeof(NEW.eligible_epoch_ns) != 'integer'
  OR NEW.eligible_epoch_ns != CASE
      WHEN NEW.claim_owner IS NULL
      THEN hydra_rfc3339_nanos(NEW.observed_at)
      ELSE hydra_rfc3339_nanos(NEW.claim_expires_at)
  END
BEGIN
    SELECT RAISE(ABORT, 'dirty root exact eligibility is required');
END"""

AA27_DIRTY_ELIGIBILITY_UPDATE_TRIGGER_SQL = """CREATE TRIGGER sync_dirty_eligibility_update
BEFORE UPDATE ON sync_dirty_roots
WHEN typeof(NEW.eligible_epoch_ns) != 'integer'
  OR NEW.eligible_epoch_ns != CASE
      WHEN NEW.claim_owner IS NULL
      THEN hydra_rfc3339_nanos(NEW.observed_at)
      ELSE hydra_rfc3339_nanos(NEW.claim_expires_at)
  END
BEGIN
    SELECT RAISE(ABORT, 'dirty root exact eligibility is required');
END"""

AA27_QUEUE_COUNT_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_queue_summary_insert
AFTER INSERT ON sync_ingest_queue
BEGIN
    UPDATE sync_work_summary
       SET ingest_total=ingest_total+1
     WHERE singleton=1;
END"""

AA27_QUEUE_COUNT_DELETE_TRIGGER_SQL = """CREATE TRIGGER sync_queue_summary_delete
AFTER DELETE ON sync_ingest_queue
BEGIN
    UPDATE sync_work_summary
       SET ingest_total=ingest_total-1
     WHERE singleton=1;
END"""

AA27_OUTBOX_COUNT_INSERT_TRIGGER_SQL = """CREATE TRIGGER hook_outbox_summary_insert
AFTER INSERT ON hook_event_outbox
WHEN NEW.acknowledged_at IS NULL
BEGIN
    UPDATE sync_work_summary
       SET outbox_total=outbox_total+1
     WHERE singleton=1;
END"""

AA27_OUTBOX_COUNT_DELETE_TRIGGER_SQL = """CREATE TRIGGER hook_outbox_summary_delete
AFTER DELETE ON hook_event_outbox
WHEN OLD.acknowledged_at IS NULL
BEGIN
    UPDATE sync_work_summary
       SET outbox_total=outbox_total-1
     WHERE singleton=1;
END"""

AA27_OUTBOX_COUNT_UPDATE_TRIGGER_SQL = """CREATE TRIGGER hook_outbox_summary_update
AFTER UPDATE OF acknowledged_at ON hook_event_outbox
WHEN (OLD.acknowledged_at IS NULL) != (NEW.acknowledged_at IS NULL)
BEGIN
    UPDATE sync_work_summary
       SET outbox_total=outbox_total
           + CASE WHEN NEW.acknowledged_at IS NULL THEN 1 ELSE -1 END
     WHERE singleton=1;
END"""

AA27_DIRTY_COUNT_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_dirty_summary_insert
AFTER INSERT ON sync_dirty_roots
BEGIN
    UPDATE sync_work_summary
       SET dirty_total=dirty_total+1
     WHERE singleton=1;
END"""

AA27_DIRTY_COUNT_DELETE_TRIGGER_SQL = """CREATE TRIGGER sync_dirty_summary_delete
AFTER DELETE ON sync_dirty_roots
BEGIN
    UPDATE sync_work_summary
       SET dirty_total=dirty_total-1
     WHERE singleton=1;
END"""


AA27_REQUIRED_TRIGGER_SQL = {
    "sync_queue_eligibility_insert": AA27_QUEUE_ELIGIBILITY_INSERT_TRIGGER_SQL,
    "sync_queue_eligibility_update": AA27_QUEUE_ELIGIBILITY_UPDATE_TRIGGER_SQL,
    "hook_outbox_eligibility_insert": AA27_OUTBOX_ELIGIBILITY_INSERT_TRIGGER_SQL,
    "hook_outbox_eligibility_update": AA27_OUTBOX_ELIGIBILITY_UPDATE_TRIGGER_SQL,
    "sync_dirty_eligibility_insert": AA27_DIRTY_ELIGIBILITY_INSERT_TRIGGER_SQL,
    "sync_dirty_eligibility_update": AA27_DIRTY_ELIGIBILITY_UPDATE_TRIGGER_SQL,
    "sync_queue_summary_insert": AA27_QUEUE_COUNT_INSERT_TRIGGER_SQL,
    "sync_queue_summary_delete": AA27_QUEUE_COUNT_DELETE_TRIGGER_SQL,
    "hook_outbox_summary_insert": AA27_OUTBOX_COUNT_INSERT_TRIGGER_SQL,
    "hook_outbox_summary_delete": AA27_OUTBOX_COUNT_DELETE_TRIGGER_SQL,
    "hook_outbox_summary_update": AA27_OUTBOX_COUNT_UPDATE_TRIGGER_SQL,
    "sync_dirty_summary_insert": AA27_DIRTY_COUNT_INSERT_TRIGGER_SQL,
    "sync_dirty_summary_delete": AA27_DIRTY_COUNT_DELETE_TRIGGER_SQL,
}

AA27_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (45, (
        "ALTER TABLE sync_ingest_queue ADD COLUMN eligible_epoch_ns INTEGER",
        "ALTER TABLE hook_event_outbox ADD COLUMN eligible_epoch_ns INTEGER",
        "ALTER TABLE sync_dirty_roots ADD COLUMN eligible_epoch_ns INTEGER",
        AA27_SYNC_WORK_SUMMARY_TABLE_SQL,
        """INSERT INTO sync_work_summary(
               singleton,ingest_total,outbox_total,dirty_total
           ) VALUES (1,0,0,0)""",
        """CREATE INDEX sync_ingest_queue_eligibility
               ON sync_ingest_queue(
                   eligible_epoch_ns,root_kind,source_locator
               )""",
        """CREATE INDEX hook_event_outbox_eligibility
               ON hook_event_outbox(eligible_epoch_ns,event_key)
               WHERE acknowledged_at IS NULL""",
        """CREATE INDEX sync_dirty_roots_eligibility
               ON sync_dirty_roots(
                   eligible_epoch_ns,project_id,root_kind,root_key
               )""",
        *AA27_REQUIRED_TRIGGER_SQL.values(),
    )),
)

AA27_REQUIRED_SCHEMA = {
    "sync_ingest_queue": {"eligible_epoch_ns"},
    "hook_event_outbox": {"eligible_epoch_ns"},
    "sync_dirty_roots": {"eligible_epoch_ns"},
    "sync_work_summary": {
        "singleton", "ingest_total", "outbox_total", "dirty_total",
    },
}


def _epoch(value: object, label: str) -> int:
    try:
        instant = require_exact_timestamp(value, label)
        epoch = instant.epoch_nanoseconds
        if not -(1 << 63) <= epoch < (1 << 63):
            raise ValueError("timestamp exceeds SQLite integer range")
        return epoch
    except (TypeError, ValueError) as error:
        raise sqlite3.IntegrityError(
            f"{label} cannot be migrated",
        ) from error


def backfill_sync_work_eligibility(connection: sqlite3.Connection) -> None:
    """Validate legacy work rows and seed the exact, trigger-maintained summary."""
    queue_updates: list[tuple[int, str, str]] = []
    for root_kind, source_locator, state, available_at, claim_expires_at in (
        connection.execute(
            """SELECT root_kind,source_locator,queue_state,available_at,
                      claim_expires_at
                 FROM sync_ingest_queue
                ORDER BY root_kind,source_locator""",
        )
    ):
        eligible_at = (
            available_at if state == "queued" else claim_expires_at
        )
        queue_updates.append((
            _epoch(eligible_at, "sync queue eligibility"),
            str(root_kind),
            str(source_locator),
        ))
    connection.executemany(
        """UPDATE sync_ingest_queue SET eligible_epoch_ns=?
             WHERE root_kind=? AND source_locator=?""",
        queue_updates,
    )

    outbox_updates: list[tuple[int | None, str]] = []
    for event_key, observed_at, claimed_by, claim_expires_at, acknowledged_at in (
        connection.execute(
            """SELECT event_key,observed_at,claimed_by,claim_expires_at,
                      acknowledged_at
                 FROM hook_event_outbox
                ORDER BY event_key""",
        )
    ):
        if acknowledged_at is not None:
            epoch = None
        else:
            eligible_at = (
                observed_at if claimed_by is None else claim_expires_at
            )
            epoch = _epoch(eligible_at, "hook outbox eligibility")
        outbox_updates.append((epoch, str(event_key)))
    connection.executemany(
        """UPDATE hook_event_outbox SET eligible_epoch_ns=?
             WHERE event_key=?""",
        outbox_updates,
    )

    dirty_updates: list[tuple[int, str, str, str]] = []
    for project_id, root_key, root_kind, observed_at, owner, expiry in (
        connection.execute(
            """SELECT project_id,root_key,root_kind,observed_at,
                      claim_owner,claim_expires_at
                 FROM sync_dirty_roots
                ORDER BY project_id,root_kind,root_key""",
        )
    ):
        eligible_at = observed_at if owner is None else expiry
        dirty_updates.append((
            _epoch(eligible_at, "dirty root eligibility"),
            str(project_id),
            str(root_key),
            str(root_kind),
        ))
    connection.executemany(
        """UPDATE sync_dirty_roots SET eligible_epoch_ns=?
             WHERE project_id=? AND root_key=? AND root_kind=?""",
        dirty_updates,
    )

    connection.execute(
        """UPDATE sync_work_summary
              SET ingest_total=(SELECT COUNT(*) FROM sync_ingest_queue),
                  outbox_total=(
                      SELECT COUNT(*) FROM hook_event_outbox
                       WHERE acknowledged_at IS NULL
                  ),
                  dirty_total=(SELECT COUNT(*) FROM sync_dirty_roots)
            WHERE singleton=1""",
    )
