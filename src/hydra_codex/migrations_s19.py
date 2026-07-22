"""Protect receipted pilot runs from UPDATE OR REPLACE target collisions."""

from __future__ import annotations


S19_PILOT_RUN_UPDATE_TRIGGER_SQL = """CREATE TRIGGER pilot_runs_immutable_after_receipt_update
       BEFORE UPDATE ON pilot_runs
       WHEN EXISTS (
           SELECT 1 FROM pilot_receipts WHERE pilot_id=OLD.pilot_id
       ) OR EXISTS (
           SELECT 1 FROM pilot_receipts WHERE pilot_id=NEW.pilot_id
       ) OR EXISTS (
           SELECT 1
             FROM pilot_runs run
             JOIN pilot_receipts receipt ON receipt.pilot_id=run.pilot_id
            WHERE run.rowid=NEW.rowid
       )
       BEGIN
           SELECT RAISE(ABORT, 'receipted pilot runs are immutable');
       END"""


S19_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (35, (
        "DROP TRIGGER pilot_runs_immutable_after_receipt_update",
        S19_PILOT_RUN_UPDATE_TRIGGER_SQL,
    )),
)


S19_REQUIRED_SCHEMA: dict[str, set[str]] = {}
