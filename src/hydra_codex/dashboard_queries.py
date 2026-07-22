"""Privacy-safe storage queries shared by the dashboard refresh flow."""

from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3

from .project import ProjectResolution
from .storage import HydraStore


@dataclass(frozen=True)
class CatalogProject:
    """A private catalog row containing no filesystem location fields."""

    project_id: str = field(repr=False)
    display_name: str | None
    first_seen_at: str
    last_seen_at: str


def _catalog_rows(connection: sqlite3.Connection) -> tuple[CatalogProject, ...]:
    return tuple(
        CatalogProject(
            str(row["project_id"]),
            None if row["display_name"] is None else str(row["display_name"]),
            str(row["first_seen_at"]), str(row["last_seen_at"]),
        )
        for row in connection.execute(
            """SELECT project_id,display_name,first_seen_at,last_seen_at
                 FROM dashboard_projects ORDER BY project_id""",
        )
    )


def sync_project_catalog(
    store: HydraStore, observed_at: str,
) -> tuple[CatalogProject, ...]:
    """Derive catalog identities/timestamps without copying stored path fields."""
    rows = store.connection.execute(
        """WITH observations(project_id, seen_at) AS (
            SELECT project_id, started_at FROM sessions
            UNION ALL SELECT project_id, COALESCE(last_activity_at, started_at)
              FROM rollout_sessions
            UNION ALL SELECT project_id, started_at FROM reconciliation_runs
            UNION ALL SELECT project_id, started_at FROM pilot_runs
            UNION ALL SELECT project_id, observed_at FROM storage_audit_snapshots
        )
        SELECT project_id, MIN(COALESCE(seen_at, ?)), MAX(COALESCE(seen_at, ?))
          FROM observations WHERE project_id <> '' GROUP BY project_id
          ORDER BY project_id""",
        (observed_at, observed_at),
    ).fetchall()
    with store.rollout_transaction() as connection:
        for project_id, first_seen, last_seen in rows:
            connection.execute(
                """INSERT INTO dashboard_projects(
                       project_id,display_name,first_seen_at,last_seen_at)
                   VALUES (?,NULL,?,?)
                   ON CONFLICT(project_id) DO UPDATE SET
                     first_seen_at=MIN(first_seen_at,excluded.first_seen_at),
                     last_seen_at=MAX(last_seen_at,excluded.last_seen_at)""",
                (project_id, first_seen, last_seen),
            )
    return _catalog_rows(store.connection)


def observe_resolved_project(
    store: HydraStore, resolution: ProjectResolution, observed_at: str,
) -> None:
    """Remember a local project's optional trusted display name and recency."""
    with store.rollout_transaction() as connection:
        connection.execute(
            """INSERT INTO dashboard_projects(
                   project_id,display_name,first_seen_at,last_seen_at)
               VALUES (?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET
                 display_name=COALESCE(excluded.display_name,display_name),
                 last_seen_at=MAX(last_seen_at,excluded.last_seen_at)""",
            (
                resolution.project_id, resolution.display_name, observed_at, observed_at,
            ),
        )
