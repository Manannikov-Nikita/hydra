"""Add the privacy-safe dashboard project catalog."""

from __future__ import annotations


U21_DASHBOARD_PROJECTS_TABLE_SQL = """CREATE TABLE dashboard_projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CHECK(display_name IS NULL OR (length(display_name) BETWEEN 1 AND 80))
) WITHOUT ROWID"""


U21_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (37, (U21_DASHBOARD_PROJECTS_TABLE_SQL,)),
)


U21_REQUIRED_SCHEMA: dict[str, set[str]] = {
    "dashboard_projects": {
        "project_id", "display_name", "first_seen_at", "last_seen_at",
    },
}
