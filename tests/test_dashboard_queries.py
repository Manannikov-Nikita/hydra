from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hydra_codex.dashboard_queries import (
    CatalogProject,
    observe_resolved_project,
    sync_project_catalog,
)
from hydra_codex.project import ProjectResolution
from hydra_codex.public_refs import (
    project_catalog_references,
    project_public_references,
)
from hydra_codex.storage import HydraStore


class DashboardQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_task_reference_bytes_remain_stable(self) -> None:
        projection = project_public_references(("alpha", "beta"), b"k" * 32)

        self.assertEqual(
            projection.public_references,
            ("task_69ceb162b693", "task_5a60334a2739"),
        )

    def test_task_reference_collisions_expand_without_changing_prefix(self) -> None:
        projection = project_public_references(("id-0", "id-11"), b"k" * 32, minimum_length=1)

        self.assertEqual(projection["id-0"], "task_6a")
        self.assertEqual(projection["id-11"], "task_66")

    def test_project_catalog_references_use_an_independent_domain(self) -> None:
        projects = project_catalog_references(("alpha", "beta"), b"k" * 32)
        tasks = project_public_references(("alpha", "beta"), b"k" * 32)

        self.assertEqual(len(projects), 2)
        self.assertTrue(all(value.startswith("project_") for value in projects.public_references))
        self.assertNotEqual(projects.public_references, tasks.public_references)

    def test_sync_catalogs_project_observations_without_persisting_paths(self) -> None:
        connection = self.store.connection
        connection.execute(
            """INSERT INTO sessions(session_id,project_id,worktree_path,started_at,provenance)
               VALUES ('session-a','project-a','private/worktree','2026-07-20T09:00:00Z','exact')""",
        )
        connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,conversation_key,
                   started_at,last_activity_at)
               VALUES ('rollout-b','project-b','private/path',1,'safe',
                       '2026-07-20T10:00:00Z','2026-07-20T12:00:00Z')""",
        )
        connection.execute(
            """INSERT INTO reconciliation_runs(
                   run_id,project_id,started_at,outcome,provenance)
               VALUES ('reconcile-c','project-c','2026-07-20T11:00:00Z','success','exact')""",
        )
        connection.execute(
            """INSERT INTO pilot_runs(
                   pilot_id,project_id,started_at,closed_at,target,task_family,
                   thresholds_json,state)
               VALUES ('pilot-d','project-d','2026-07-20T13:00:00Z',NULL,1,
                       'all','{}','open')""",
        )
        connection.execute(
            """INSERT INTO storage_audit_snapshots(
                   snapshot_id,project_id,observed_at,audit_sha256,database_bytes,
                   wal_bytes,rollout_sources,rollout_events,codex_event_sources,
                   codex_events,schema_version)
               VALUES ('audit-e','project-e','2026-07-20T14:00:00Z','digest',0,0,0,0,0,0,37)""",
        )
        connection.commit()

        catalog = sync_project_catalog(self.store, "2026-07-20T15:00:00Z")

        self.assertEqual(catalog, (
            CatalogProject("project-a", None, "2026-07-20T09:00:00Z", "2026-07-20T09:00:00Z"),
            CatalogProject("project-b", None, "2026-07-20T12:00:00Z", "2026-07-20T12:00:00Z"),
            CatalogProject("project-c", None, "2026-07-20T11:00:00Z", "2026-07-20T11:00:00Z"),
            CatalogProject("project-d", None, "2026-07-20T13:00:00Z", "2026-07-20T13:00:00Z"),
            CatalogProject("project-e", None, "2026-07-20T14:00:00Z", "2026-07-20T14:00:00Z"),
        ))
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(dashboard_projects)")
        }
        self.assertEqual(
            columns, {"project_id", "display_name", "first_seen_at", "last_seen_at"},
        )
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT project_id,display_name,first_seen_at,last_seen_at "
                "FROM dashboard_projects ORDER BY project_id"
            )],
            [
                ("project-a", None, "2026-07-20T09:00:00Z", "2026-07-20T09:00:00Z"),
                ("project-b", None, "2026-07-20T12:00:00Z", "2026-07-20T12:00:00Z"),
                ("project-c", None, "2026-07-20T11:00:00Z", "2026-07-20T11:00:00Z"),
                ("project-d", None, "2026-07-20T13:00:00Z", "2026-07-20T13:00:00Z"),
                ("project-e", None, "2026-07-20T14:00:00Z", "2026-07-20T14:00:00Z"),
            ],
        )

    def test_observe_resolved_project_adds_only_trusted_display_name(self) -> None:
        observe_resolved_project(
            self.store,
            ProjectResolution(
                "project-a", Path("/private/project"), Path("nested"), "Hydra Core",
            ),
            "2026-07-20T12:00:00Z",
        )

        row = self.store.connection.execute(
            "SELECT * FROM dashboard_projects",
        ).fetchone()
        self.assertEqual(
            tuple(row), ("project-a", "Hydra Core", "2026-07-20T12:00:00Z", "2026-07-20T12:00:00Z"),
        )
        self.assertNotIn("/private/project", "\n".join(self.store.connection.iterdump()))


if __name__ == "__main__":
    unittest.main()
