"""Track incremental session placeholders without overloading a valid path."""

from __future__ import annotations

import sqlite3

from .migrations_v22 import V22_TOKEN_SNAPSHOT_QUERY_INDEX_SQL


AC29_INCREMENTAL_SESSION_PLACEHOLDERS_TABLE_SQL = (
    """CREATE TABLE incremental_session_placeholders (
        session_key TEXT PRIMARY KEY
            REFERENCES rollout_sessions(session_key) ON DELETE CASCADE
    ) WITHOUT ROWID"""
)

AC29_BACKFILL_INCREMENTAL_SESSION_PLACEHOLDERS_SQL = (
    """INSERT INTO incremental_session_placeholders(session_key)
       SELECT DISTINCT session.session_key
         FROM rollout_sessions session
         JOIN rollout_logical_sources logical
           ON logical.session_key=session.session_key
         JOIN rollout_sources source
           ON source.logical_source_key=logical.logical_source_key
        WHERE session.path_key='incremental'
          AND logical.canonical_revision_digest IS NULL
          AND source.relation='append'
          AND source.line_count=0
          AND source.byte_count=0
          AND source.chain_digest=''
          AND NOT EXISTS (
            SELECT 1
              FROM rollout_logical_sources canonical
             WHERE canonical.session_key=session.session_key
               AND canonical.canonical_revision_digest IS NOT NULL
          )
       ON CONFLICT DO NOTHING"""
)


AC29_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        47,
        (
            AC29_INCREMENTAL_SESSION_PLACEHOLDERS_TABLE_SQL,
            AC29_BACKFILL_INCREMENTAL_SESSION_PLACEHOLDERS_SQL,
            "DROP INDEX token_snapshots_project_session_valid",
            V22_TOKEN_SNAPSHOT_QUERY_INDEX_SQL,
        ),
    ),
)


AC29_REQUIRED_SCHEMA = {
    "incremental_session_placeholders": {"session_key"},
}


def recover_missing_incremental_session_placeholders(
    connection: sqlite3.Connection,
) -> None:
    """Rebuild the one derived AC29 table when a recorded v47 lacks it."""
    existing = connection.execute(
        """SELECT type FROM sqlite_master
            WHERE name='incremental_session_placeholders'"""
    ).fetchone()
    if existing is not None:
        raise sqlite3.IntegrityError(
            "incremental session placeholder recovery requires a missing object"
        )
    connection.execute(AC29_INCREMENTAL_SESSION_PLACEHOLDERS_TABLE_SQL)
    connection.execute(AC29_BACKFILL_INCREMENTAL_SESSION_PLACEHOLDERS_SQL)
