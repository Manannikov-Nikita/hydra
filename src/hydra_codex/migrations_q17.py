"""Close SQLite replacement semantics around immutable pilot receipts."""

from __future__ import annotations


Q17_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (33, (
        """CREATE TRIGGER pilot_receipts_immutable_insert
               BEFORE INSERT ON pilot_receipts
               WHEN EXISTS (
                   SELECT 1 FROM pilot_receipts
                    WHERE receipt_id=NEW.receipt_id OR pilot_id=NEW.pilot_id
               )
               BEGIN
                   SELECT RAISE(ABORT, 'pilot receipts are immutable');
               END""",
    )),
)


Q17_REQUIRED_SCHEMA: dict[str, set[str]] = {}
