"""Remove hidden receipt rowids and freeze receipted pilot runs."""

from __future__ import annotations


R18_PILOT_RECEIPTS_TABLE_SQL = """CREATE TABLE pilot_receipts (
       receipt_id TEXT PRIMARY KEY,
       pilot_id TEXT NOT NULL UNIQUE REFERENCES pilot_runs(pilot_id)
           ON DELETE RESTRICT,
       created_at TEXT NOT NULL,
       decision TEXT NOT NULL CHECK(decision IN ('verified','rejected')),
       task_refs_json TEXT NOT NULL,
       reconciliation_version INTEGER NOT NULL,
       schema_version INTEGER NOT NULL,
       thresholds_json TEXT NOT NULL,
       observed_facts_json TEXT NOT NULL,
       snapshot_digest TEXT NOT NULL,
       audit_sha256 TEXT NOT NULL
   ) WITHOUT ROWID"""


R18_TRIGGER_STATEMENTS: tuple[str, ...] = (
    """CREATE TRIGGER pilot_receipts_immutable_update
           BEFORE UPDATE ON pilot_receipts BEGIN
               SELECT RAISE(ABORT, 'pilot receipts are immutable');
           END""",
    """CREATE TRIGGER pilot_receipts_immutable_delete
           BEFORE DELETE ON pilot_receipts BEGIN
               SELECT RAISE(ABORT, 'pilot receipts are immutable');
           END""",
    """CREATE TRIGGER pilot_receipts_immutable_insert
           BEFORE INSERT ON pilot_receipts
           WHEN EXISTS (
               SELECT 1 FROM pilot_receipts
                WHERE receipt_id=NEW.receipt_id OR pilot_id=NEW.pilot_id
           )
           BEGIN
               SELECT RAISE(ABORT, 'pilot receipts are immutable');
           END""",
    """CREATE TRIGGER pilot_receipts_require_closed_run_insert
           BEFORE INSERT ON pilot_receipts
           WHEN NOT EXISTS (
               SELECT 1 FROM pilot_runs
                WHERE pilot_id=NEW.pilot_id
                  AND state='closed'
                  AND closed_at=NEW.created_at
           )
           BEGIN
               SELECT RAISE(
                   ABORT, 'pilot receipt must match its closed pilot run'
               );
           END""",
    """CREATE TRIGGER pilot_runs_immutable_after_receipt_insert
           BEFORE INSERT ON pilot_runs
           WHEN EXISTS (
               SELECT 1 FROM pilot_receipts WHERE pilot_id=NEW.pilot_id
           ) OR EXISTS (
               SELECT 1
                 FROM pilot_runs run
                 JOIN pilot_receipts receipt ON receipt.pilot_id=run.pilot_id
                WHERE run.rowid=NEW.rowid
           )
           BEGIN
               SELECT RAISE(ABORT, 'receipted pilot runs are immutable');
           END""",
    """CREATE TRIGGER pilot_runs_immutable_after_receipt_update
           BEFORE UPDATE ON pilot_runs
           WHEN EXISTS (
               SELECT 1 FROM pilot_receipts WHERE pilot_id=OLD.pilot_id
           )
           BEGIN
               SELECT RAISE(ABORT, 'receipted pilot runs are immutable');
           END""",
    """CREATE TRIGGER pilot_runs_immutable_after_receipt_delete
           BEFORE DELETE ON pilot_runs
           WHEN EXISTS (
               SELECT 1 FROM pilot_receipts WHERE pilot_id=OLD.pilot_id
           )
           BEGIN
               SELECT RAISE(ABORT, 'receipted pilot runs are immutable');
           END""",
)


R18_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (34, (
        "DROP TRIGGER pilot_receipts_immutable_insert",
        "DROP TRIGGER pilot_receipts_immutable_update",
        "DROP TRIGGER pilot_receipts_immutable_delete",
        "ALTER TABLE pilot_receipts RENAME TO pilot_receipts_v33",
        R18_PILOT_RECEIPTS_TABLE_SQL,
        """INSERT INTO pilot_receipts(
               receipt_id,pilot_id,created_at,decision,task_refs_json,
               reconciliation_version,schema_version,thresholds_json,
               observed_facts_json,snapshot_digest,audit_sha256)
           SELECT receipt_id,pilot_id,created_at,decision,task_refs_json,
                  reconciliation_version,schema_version,thresholds_json,
                  observed_facts_json,snapshot_digest,audit_sha256
             FROM pilot_receipts_v33""",
        "DROP TABLE pilot_receipts_v33",
        *R18_TRIGGER_STATEMENTS,
    )),
)


R18_REQUIRED_SCHEMA: dict[str, set[str]] = {}
