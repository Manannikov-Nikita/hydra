from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
from collections.abc import Mapping

from hydra_codex.audit_model import AuditEvidence
from hydra_codex.dashboard_model import DashboardRefreshView
from hydra_codex.dashboard_queries import (
    CatalogProject,
    DashboardQueryService,
    observe_resolved_project,
    sync_project_catalog,
)
from hydra_codex.diagnostics import DoctorCheck, DoctorReport
from hydra_codex.project import ProjectResolution
from hydra_codex.public_refs import (
    project_catalog_references,
    project_public_references,
)
from hydra_codex.storage import HydraStore
from tests.test_audit_builder import public_report


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


class DashboardPublicQueryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        store = HydraStore(self.database)
        store.connection.executemany(
            """INSERT INTO dashboard_projects(
                   project_id,display_name,first_seen_at,last_seen_at)
               VALUES (?,?,?,?)""",
            (
                ("project-a", "A <script>", "2026-07-20T00:00:00Z", "2026-07-22T10:00:00Z"),
                ("project-b", None, "2026-07-20T00:00:00Z", "2026-07-22T11:00:00Z"),
            ),
        )
        store.connection.commit()
        store.close()
        first = replace(public_report("first", input_tokens=10, second=10),
                        last_activity_at="2026-07-22T10:00:00Z")
        latest = replace(public_report("latest", input_tokens=30, second=20),
                         last_activity_at="2026-07-22T11:00:00Z")
        other = replace(public_report("other", input_tokens=50, second=30),
                        task_ref=latest.task_ref,
                        trend_input=replace(public_report("other", input_tokens=50, second=30).trend_input,
                                            task_ref=latest.task_ref),
                        last_activity_at="2026-07-22T12:00:00Z")
        self.reports = {"project-a": (latest, first), "project-b": (other,)}
        checks = tuple(DoctorCheck(code, "ok") for code in (
            "project_resolution", "storage_available", "schema_current",
            "foreign_keys_ok", "integrity_ok", "storage_permissions_restricted",
        ))
        self.refresh = DashboardRefreshView(None, "idle", None, None, None, {}, ())
        self.service = DashboardQueryService(
            lambda: HydraStore(self.database),
            b"k" * 32,
            lambda: datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
            DoctorReport(checks),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def catalog_refs(self):
        return project_catalog_references(("project-a", "project-b"), b"k" * 32)

    def database_dump(self) -> str:
        store = HydraStore(self.database)
        try:
            return "\n".join(store.connection.iterdump())
        finally:
            store.close()

    def test_snapshot_uses_latest_task_and_never_writes(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        latest, first = self.reports["project-a"]
        before = self.database_dump()

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=lambda _store, project_id: self.reports[project_id],
        ):
            snapshot = self.service.snapshot(
                project_ref=project_ref,
                task_ref=first.task_ref,
                refresh=self.refresh,
            )

        payload = snapshot.as_dict()
        self.assertEqual(self.database_dump(), before)
        self.assertEqual(payload["project"]["overview"]["basis"], {
            "kind": "latest_task", "task_ref": latest.task_ref,
        })
        self.assertEqual(
            payload["project"]["overview"]["headline"]["working_tokens"],
            latest.deduplicated_tokens.working.as_dict(),
        )
        self.assertNotEqual(
            payload["project"]["overview"]["headline"]["working_tokens"]["value"],
            latest.deduplicated_tokens.working.value
            + first.deduplicated_tokens.working.value,
        )
        self.assertEqual(payload["selected_task"]["task_ref"], first.task_ref)
        self.assertEqual(payload["project"]["display_name"], "A <script>")

    def test_private_refresh_seam_reuses_caller_owned_store(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        store = HydraStore(self.database)
        try:
            with patch(
                "hydra_codex.dashboard_queries.list_reconciled_reports",
                side_effect=lambda _store, project_id: self.reports[project_id],
            ):
                snapshots = self.service._refresh_snapshots_from_store(
                    store,
                    refresh=self.refresh,
                    project_ids={"project-a"},
                )

            self.assertEqual(tuple(snapshots), (project_ref,))
            self.assertIsNotNone(store.connection)
            self.assertEqual(snapshots[project_ref].selected_project_ref, project_ref)
        finally:
            store.close()

    def test_private_refresh_seam_reads_each_project_once(self) -> None:
        store = HydraStore(self.database)
        try:
            with patch(
                "hydra_codex.dashboard_queries.list_reconciled_reports",
                side_effect=lambda _store, project_id: self.reports[project_id],
            ) as reports:
                snapshots = self.service._refresh_snapshots_from_store(
                    store,
                    refresh=self.refresh,
                    project_ids=None,
                )

            self.assertEqual(len(snapshots), 2)
            self.assertEqual(reports.call_count, 2)
        finally:
            store.close()

    def test_private_display_names_fall_back_to_opaque_project_label(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        unsafe_names = (
            "Hydra /Users/alice/private-project",
            "Hydra /workspace/private-project",
            "Hydra /srv/private-project",
            r"Hydra C:\Users\alice\private-project",
            r"Hydra \\server\private-project",
            "Hydra //server/private-project",
            "Hydra file:///Users/alice/private-project",
            "Hydra ~/private-project",
            "Hydra project-a",
            "Hydra 123e4567-e89b-12d3-a456-426614174000",
            "Hydra 0123456789abcdef0123456789abcdef",
            "Hydra session_0123456789abcdef",
            "ValueError: private project failed",
            "Hydra git status && env",
            "Hydra docker compose up",
            "Hydra report; env",
            "Hydra | status",
            "Hydra > output",
            "Hydra < input",
            "Hydra `status`",
            "Hydra $(status)",
            "Hydra abcdefghijklmnop1234",
            "Hydra owner@example.com",
            "Hydra\u200bCore",
            "Hydra\u202eCore",
            "Hydra Cafe\u0301",
            r"path=\\server\share",
            "OSError [Errno 2] failed",
            "ssh host",
            "ls -la",
            "Clone project-b",
            r"Hydra >output",
            r"Hydra ~alice/private",
            "FOO=bar ls",
            "Echo hello",
            "KeyboardInterrupt happened",
            "Clone hprj_ab12",
            "Clone root_abcdef",
            "Hydra & status",
        )
        for display_name in unsafe_names:
            store = HydraStore(self.database)
            try:
                store.connection.execute(
                    "UPDATE dashboard_projects SET display_name=? WHERE project_id='project-a'",
                    (display_name,),
                )
                store.connection.commit()
            finally:
                store.close()
            with patch(
                "hydra_codex.dashboard_queries.list_reconciled_reports",
                side_effect=lambda _store, project_id: self.reports[project_id],
            ):
                payload = self.service.snapshot(
                    project_ref=project_ref,
                    task_ref=None,
                    refresh=self.refresh,
                ).as_dict()
            with self.subTest(display_name=display_name):
                self.assertEqual(
                    payload["project"]["display_name"],
                    f"Project {project_ref.removeprefix('project_')[:8]}",
                )

        for display_name in ("ObservabilityDashboard", "Hydra Core", "A <script>"):
            store = HydraStore(self.database)
            try:
                store.connection.execute(
                    "UPDATE dashboard_projects SET display_name=? WHERE project_id='project-a'",
                    (display_name,),
                )
                store.connection.commit()
            finally:
                store.close()
            with patch(
                "hydra_codex.dashboard_queries.list_reconciled_reports",
                side_effect=lambda _store, project_id: self.reports[project_id],
            ):
                payload = self.service.snapshot(
                    project_ref=project_ref, task_ref=None, refresh=self.refresh,
                ).as_dict()
            with self.subTest(safe_display_name=display_name):
                self.assertEqual(payload["project"]["display_name"], display_name)

    def test_pilot_and_storage_numbers_are_dashboard_numeric_facts(self) -> None:
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO pilot_runs(
                       pilot_id,project_id,started_at,closed_at,target,task_family,
                       thresholds_json,state)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    "hpilot_v1_11111111111111111111111111111111",
                    "project-a", "2026-07-22T00:00:00Z", None, 5,
                    "telemetry-analysis", "{}", "open",
                ),
            )
            store.connection.commit()
        finally:
            store.close()
        project_ref = self.catalog_refs()["project-a"]

        pilot_status = SimpleNamespace(as_dict=lambda: {
            "schema_version": "hydra.pilot/v1",
            "pilot": {
                "pilot_id": "hpilot_v1_11111111111111111111111111111111",
                "started_at": "2026-07-22T00:00:00Z",
                "closed_at": None,
                "target": 5,
                "task_family": "telemetry-analysis",
                "state": "open",
            },
            "facts": {
                "eligible_tasks": 3,
                "instrumented_tasks": 2,
                "enrollment": 2 / 3,
                "aggregate_coverage": 0.75,
            },
            "threshold_results": {"missing_marker_rate": True},
            "transport_verified": True,
            "trend_ready": False,
        })
        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=lambda _store, project_id: self.reports[project_id],
        ), patch(
            "hydra_codex.dashboard_queries.read_pilot_status",
            return_value=pilot_status,
        ):
            project = self.service.snapshot(
                project_ref=project_ref,
                task_ref=None,
                refresh=self.refresh,
            ).as_dict()["project"]

        def assert_numbers_wrapped(value: object, parent: str | None = None) -> None:
            if isinstance(value, bool) or value is None or isinstance(value, str):
                return
            if isinstance(value, (int, float)):
                self.assertIn(parent, {"value", "lower_bound"})
                return
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    assert_numbers_wrapped(nested, str(key))
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    assert_numbers_wrapped(nested, parent)

        assert_numbers_wrapped(project["pilot"])
        assert_numbers_wrapped(project["storage"])
        self.assertEqual(project["pilot"]["target"]["unit"], "count")
        self.assertEqual(project["storage"]["current"]["database_bytes"]["unit"], "bytes")

    def test_task_pages_are_project_scoped_ordered_and_bounded(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=lambda _store, project_id: self.reports[project_id],
        ):
            first_page = self.service.tasks(project_ref, cursor=None, limit=1).as_dict()
            second_page = self.service.tasks(
                project_ref, cursor=first_page["page"]["next_cursor"], limit=1,
            ).as_dict()
            with self.assertRaises(ValueError):
                self.service.tasks(project_ref, cursor=None, limit=101)

        self.assertEqual(
            [item["task_ref"] for item in first_page["items"]],
            [self.reports["project-a"][0].task_ref],
        )
        self.assertEqual(
            [item["task_ref"] for item in second_page["items"]],
            [self.reports["project-a"][1].task_ref],
        )
        self.assertFalse(second_page["page"]["has_more"])

    def test_compare_requires_both_refs_in_selected_project(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        latest, first = self.reports["project-a"]
        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=lambda _store, project_id: self.reports[project_id],
        ):
            comparison = self.service.compare(project_ref, first.task_ref, latest.task_ref)
            with self.assertRaisesRegex(KeyError, r"^'unknown public reference'$"):
                self.service.compare(project_ref, "task_000000000000", latest.task_ref)

        self.assertEqual(comparison.schema_version, "hydra.comparison/v2")

    def test_evidence_reads_only_latest_selected_project_pilot(self) -> None:
        store = HydraStore(self.database)
        try:
            store.connection.executemany(
                """INSERT INTO pilot_runs(
                       pilot_id,project_id,started_at,closed_at,target,task_family,
                       thresholds_json,state)
                   VALUES (?,?,?,?,1,'all','{}',?)""",
                (
                    ("hpilot_v1_11111111111111111111111111111111", "project-a", "2026-07-21T00:00:00Z", "2026-07-21T01:00:00Z", "closed"),
                    ("hpilot_v1_22222222222222222222222222222222", "project-a", "2026-07-22T00:00:00Z", None, "open"),
                    ("hpilot_v1_33333333333333333333333333333333", "project-b", "2026-07-23T00:00:00Z", None, "open"),
                ),
            )
            store.connection.commit()
        finally:
            store.close()
        evidence = AuditEvidence(
            "ev_0123456789abcdef", "tasks.safe.working", 30, "tokens", "derived",
        )
        called: list[tuple[str, str, bool]] = []

        def build(_store, *, project_id, pilot_id, refresh_enrollment=True):
            called.append((project_id, pilot_id, refresh_enrollment))
            return SimpleNamespace(evidence_appendix=(evidence,))

        before = self.database_dump()
        with patch("hydra_codex.dashboard_queries.build_pilot_audit", side_effect=build):
            selected = self.service.evidence(
                self.catalog_refs()["project-a"], evidence.evidence_id,
            )

        self.assertIs(selected, evidence)
        self.assertEqual(called, [(
            "project-a", "hpilot_v1_22222222222222222222222222222222", False,
        )])
        self.assertEqual(self.database_dump(), before)

    def test_unknown_selectors_use_one_categorical_error(self) -> None:
        for call in (
            lambda: self.service.snapshot(
                project_ref="project_000000000000", task_ref=None, refresh=self.refresh,
            ),
            lambda: self.service.tasks("project_000000000000", cursor=None),
        ):
            with self.subTest(call=call), self.assertRaisesRegex(
                KeyError, r"^'unknown public reference'$",
            ):
                call()


if __name__ == "__main__":
    unittest.main()
