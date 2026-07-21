"""Shared project-level schema diagnostic accounting."""

from __future__ import annotations

import sqlite3


def project_event_schema_counts(
    connection: sqlite3.Connection, project_id: str,
) -> tuple[int, dict[str, int]]:
    """Return all project event issues and per-task attributed subsets."""
    total = int(connection.execute(
        """SELECT COUNT(*)
             FROM codex_event_issues i
             JOIN codex_event_sources s ON s.source_digest=i.source_digest
            WHERE s.project_id=?""",
        (project_id,),
    ).fetchone()[0])
    attributed = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            """SELECT t.public_ref,COALESCE(SUM(d.occurrence_count),0)
                 FROM reconciled_tasks t
                 JOIN reconciled_task_diagnostics d
                   ON d.project_id=t.project_id AND d.root_key=t.root_key
                WHERE t.project_id=? AND d.diagnostic_code LIKE 'schema:event:%'
                GROUP BY t.public_ref""",
            (project_id,),
        )
    }
    return total, attributed
