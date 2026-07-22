"""Add the bounded token lookup used by per-task reconciliation."""

from __future__ import annotations


V22_TOKEN_SNAPSHOT_QUERY_INDEX_SQL = """CREATE INDEX token_snapshots_project_session_valid
    ON token_snapshots(project_id,session_key)
    WHERE contributes_total=1 AND vector_valid=1"""


V22_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (38, (V22_TOKEN_SNAPSHOT_QUERY_INDEX_SQL,)),
)
