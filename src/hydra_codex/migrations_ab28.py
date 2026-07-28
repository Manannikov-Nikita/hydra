"""Fence dirty-root acknowledgements with an immutable per-claim token."""

from __future__ import annotations

from .migrations_aa27 import AA27_SYNC_DIRTY_ROOTS_TABLE_SQL


AB28_SYNC_DIRTY_ROOTS_TABLE_SQL = AA27_SYNC_DIRTY_ROOTS_TABLE_SQL.replace(
    "    eligible_epoch_ns INTEGER,\n    PRIMARY KEY",
    "    eligible_epoch_ns INTEGER, claim_token TEXT,\n    PRIMARY KEY",
)

AB28_DIRTY_CLAIM_TOKEN_INSERT_TRIGGER_SQL = """CREATE TRIGGER sync_dirty_claim_token_insert
BEFORE INSERT ON sync_dirty_roots
WHEN NOT (
    (
        NEW.claim_owner IS NULL
        AND NEW.claim_expires_at IS NULL
        AND NEW.claim_token IS NULL
    )
    OR (
        NEW.claim_owner IS NOT NULL
        AND NEW.claim_expires_at IS NOT NULL
        AND typeof(NEW.claim_token) = 'text'
        AND length(NEW.claim_token) = 32
        AND NEW.claim_token NOT GLOB '*[^0-9a-f]*'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'dirty root claim token is invalid');
END"""

AB28_DIRTY_CLAIM_TOKEN_UPDATE_TRIGGER_SQL = """CREATE TRIGGER sync_dirty_claim_token_update
BEFORE UPDATE ON sync_dirty_roots
WHEN NOT (
    (
        NEW.claim_owner IS NULL
        AND NEW.claim_expires_at IS NULL
        AND NEW.claim_token IS NULL
    )
    OR (
        NEW.claim_owner IS NOT NULL
        AND NEW.claim_expires_at IS NOT NULL
        AND typeof(NEW.claim_token) = 'text'
        AND length(NEW.claim_token) = 32
        AND NEW.claim_token NOT GLOB '*[^0-9a-f]*'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'dirty root claim token is invalid');
END"""

AB28_REQUIRED_TRIGGER_SQL = {
    "sync_dirty_claim_token_insert": AB28_DIRTY_CLAIM_TOKEN_INSERT_TRIGGER_SQL,
    "sync_dirty_claim_token_update": AB28_DIRTY_CLAIM_TOKEN_UPDATE_TRIGGER_SQL,
}

AB28_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (46, (
        "ALTER TABLE sync_dirty_roots ADD COLUMN claim_token TEXT",
        """UPDATE sync_dirty_roots
              SET claim_owner=NULL,
                  claim_expires_at=NULL,
                  claim_token=NULL,
                  eligible_epoch_ns=hydra_rfc3339_nanos(observed_at)""",
        *AB28_REQUIRED_TRIGGER_SQL.values(),
    )),
)

AB28_REQUIRED_SCHEMA = {
    "sync_dirty_roots": {"claim_token"},
}
