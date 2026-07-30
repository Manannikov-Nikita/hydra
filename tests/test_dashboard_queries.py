from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
import sqlite3
from collections.abc import Mapping

from hydra_codex.audit_model import AuditEvidence
from hydra_codex.dashboard_contract import validate_task_report
from hydra_codex.dashboard_model import DashboardRefreshView
from hydra_codex.dashboard_queries import (
    CatalogProject,
    DashboardQueryService,
    observe_resolved_project,
    sync_project_catalog,
)
from hydra_codex.diagnostics import DoctorCheck, DoctorReport
from hydra_codex.exact_time import require_exact_timestamp
from hydra_codex.project import ProjectResolution
from hydra_codex.public_refs import (
    project_catalog_references,
    project_public_references,
)
from hydra_codex.reconcile_engine import ReconciliationStale, reconcile_project
from hydra_codex.report_renderers import render_json
from hydra_codex.storage import HydraStore
from hydra_codex.sync_state import SyncStateRepository
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
            columns, {
                "project_id", "display_name", "first_seen_at", "last_seen_at",
                "display_name_provenance",
            },
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
                "project-a", Path("/private/project"), Path("nested"), "Hydra Core", "config",
            ),
            "2026-07-20T12:00:00Z",
        )

        row = self.store.connection.execute(
            "SELECT * FROM dashboard_projects",
        ).fetchone()
        self.assertEqual(
            tuple(row), (
                "project-a", "Hydra Core", "2026-07-20T12:00:00Z", "2026-07-20T12:00:00Z", "config",
            ),
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

    def materialize(
        self,
        project_id: str,
        reports: tuple[object, ...],
        *,
        revision: int = 7,
    ) -> None:
        store = HydraStore(self.database)
        try:
            rows = []
            for report in reports:
                epoch_ns = require_exact_timestamp(
                    report.last_activity_at, "test report activity",
                ).epoch_nanoseconds
                rows.append((
                    project_id, report.task_ref, render_json(report).rstrip("\n"),
                    "", "", report.last_activity_at, epoch_ns,
                    report.last_activity_at, revision,
                ))
            store.connection.executemany(
                """INSERT INTO materialized_report_snapshots(
                       project_id,task_ref,report_json,report_markdown,report_html,
                       last_activity_at,last_activity_epoch_ns,reconciled_at,
                       data_revision)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=? WHERE singleton=1",
                (revision,),
            )
            self._publish_test_fence(
                store, project_id, reports, revision=revision,
            )
            store.connection.commit()
        finally:
            store.close()

    def _publish_test_fence(
        self,
        store: HydraStore,
        project_id: str,
        reports: tuple[object, ...],
        *,
        revision: int,
    ) -> None:
        input_digest = f"{revision:064x}"
        reconciled_at = max(
            (str(report.last_activity_at) for report in reports),
            default="1970-01-01T00:00:00Z",
        )
        store.connection.execute(
            """INSERT INTO reconciliation_runs(
                   run_id,project_id,started_at,outcome,provenance,
                   reconciliation_version,input_digest,completed_at,task_count)
               VALUES (?, ?, ?, 'success', 'derived', 1, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                   outcome='success',completed_at=excluded.completed_at,
                   task_count=excluded.task_count""",
            (
                f"test-run-{project_id}-{revision}",
                project_id,
                reconciled_at,
                input_digest,
                reconciled_at,
                len(reports),
            ),
        )
        project_revision = store.connection.execute(
            """SELECT revision FROM sync_project_source_fact_revisions
                WHERE project_id=?""",
            (project_id,),
        ).fetchone()
        unattributed_revision = store.connection.execute(
            """SELECT revision
                 FROM sync_unattributed_source_fact_revision
                WHERE singleton=1"""
        ).fetchone()[0]
        store.connection.execute(
            """INSERT INTO sync_project_reconcile_fences(
                   project_id,project_revision,unattributed_revision,
                   storage_schema_version,storage_schema_cookie,
                   reconciliation_version,input_digest)
               VALUES (?,?,?,?,?,1,?)
               ON CONFLICT(project_id) DO UPDATE SET
                   project_revision=excluded.project_revision,
                   unattributed_revision=excluded.unattributed_revision,
                   storage_schema_version=excluded.storage_schema_version,
                   storage_schema_cookie=excluded.storage_schema_cookie,
                   reconciliation_version=excluded.reconciliation_version,
                   input_digest=excluded.input_digest""",
            (
                project_id,
                0 if project_revision is None else int(project_revision[0]),
                int(unattributed_revision),
                store.schema_version(),
                int(store.connection.execute(
                    "PRAGMA schema_version",
                ).fetchone()[0]),
                input_digest,
            ),
        )

    def test_snapshot_uses_latest_task_and_never_writes(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        latest, first = self.reports["project-a"]
        self.materialize("project-a", self.reports["project-a"])
        before = self.database_dump()

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=AssertionError("task pages must stay materialized"),
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

    def test_task_snapshot_uses_only_revision_pinned_materialized_rows(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        latest, selected = self.reports["project-a"]
        self.materialize("project-a", self.reports["project-a"], revision=7)
        self.materialize("project-b", self.reports["project-b"], revision=7)

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=AssertionError(
                "task snapshot must not reconstruct global report history",
            ),
        ):
            snapshot = self.service.snapshot(
                project_ref=project_ref,
                task_ref=selected.task_ref,
                refresh=self.refresh,
            )

        payload = snapshot.as_dict()
        self.assertEqual(payload["data_revision"], 7)
        self.assertEqual(payload["selected_task"]["task_ref"], selected.task_ref)
        self.assertEqual(
            payload["project"]["overview"]["basis"]["task_ref"],
            latest.task_ref,
        )

    def test_snapshot_data_and_revision_share_one_read_transaction(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=3 WHERE singleton=1"
            )
            store.connection.commit()
        finally:
            store.close()
        original_prepare = self.service._prepare_snapshot_state

        def prepare_then_publish(store):
            prepared = original_prepare(store)
            writer = HydraStore(self.database)
            try:
                writer.connection.execute(
                    """UPDATE dashboard_projects SET display_name='Published later'
                         WHERE project_id='project-a'"""
                )
                writer.connection.execute(
                    "UPDATE sync_data_revision SET revision=4 WHERE singleton=1"
                )
                writer.connection.commit()
            finally:
                writer.close()
            return prepared

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=lambda _store, project_id: self.reports[project_id],
        ), patch.object(
            self.service,
            "_prepare_snapshot_state",
            side_effect=prepare_then_publish,
        ):
            payload = self.service.snapshot(
                project_ref=project_ref,
                task_ref=None,
                refresh=self.refresh,
            ).as_dict()

        self.assertEqual(payload["data_revision"], 3)
        self.assertEqual(payload["project"]["display_name"], "A <script>")

    def test_bootstrap_snapshots_use_catalog_without_rebuilding_reports(self) -> None:
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO reconciled_tasks(
                       project_id,root_key,public_ref,status,cutoff_at,last_activity_at,
                       task_family,reconciliation_version,input_digest)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    "project-a", "root-a", "task_0123456789ab", "complete",
                    "2026-07-22T10:00:00Z", "2026-07-22T10:00:00Z",
                    "telemetry-analysis", 1, "digest-a",
                ),
            )
            store.connection.execute(
                """INSERT INTO pilot_runs(
                       pilot_id,project_id,started_at,closed_at,target,task_family,
                       thresholds_json,state)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    "pilot-a", "project-a", "2026-07-22T09:00:00Z", None,
                    5, "telemetry-analysis", "{}", "open",
                ),
            )
            store.connection.commit()
        finally:
            store.close()

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=AssertionError("bootstrap must not rebuild task reports"),
        ), patch(
            "hydra_codex.dashboard_queries.read_pilot_status",
            side_effect=AssertionError("bootstrap must not rebuild pilot status"),
        ), patch(
            "hydra_codex.dashboard_queries.storage_status",
            side_effect=AssertionError("bootstrap must not scan raw storage facts"),
        ), patch.object(
            self.service,
            "_bootstrap_pilot",
            side_effect=AssertionError("bootstrap must not scan pilot history"),
            create=True,
        ):
            snapshots, empty = self.service.bootstrap_snapshots(refresh=self.refresh)

        project_a = self.catalog_refs()["project-a"]
        project_b = self.catalog_refs()["project-b"]
        self.assertEqual(set(snapshots), {project_a, project_b})
        self.assertIsNone(empty)
        first = snapshots[project_a].as_dict()
        second = snapshots[project_b].as_dict()
        self.assertIsNone(first["projects"][0]["task_count"]["value"])
        self.assertEqual(
            first["projects"][0]["task_count"]["caveats"],
            ["dashboard_refresh_required"],
        )
        self.assertEqual(first["project"]["freshness_state"], "stale")
        self.assertIsNone(
            first["project"]["overview"]["headline"]["working_tokens"]["value"],
        )
        self.assertEqual(
            first["project"]["overview"]["headline"]["working_tokens"]["caveats"],
            ["dashboard_refresh_required"],
        )
        self.assertIsNone(first["project"]["pilot"])
        storage = first["project"]["storage"]
        self.assertEqual(storage["baseline_state"], "unavailable")
        self.assertTrue(all(
            fact["value"] is None
            and fact["provenance"] == "estimated"
            and fact["caveats"] == ["dashboard_refresh_required"]
            for fact in storage["current"].values()
        ))
        self.assertEqual(second["project"]["freshness_state"], "stale")

    def test_bootstrap_snapshots_share_one_validated_project_catalog(self) -> None:
        snapshots, empty = self.service.bootstrap_snapshots(refresh=self.refresh)

        self.assertIsNone(empty)
        self.assertEqual(len(snapshots), 2)
        shared_projects = next(iter(snapshots.values())).projects
        self.assertTrue(all(
            snapshot.projects is shared_projects
            for snapshot in snapshots.values()
        ))

    def test_bootstrap_bulk_assembly_does_not_repeat_linear_project_resolves(self) -> None:
        original_resolve = self.service._resolve_project

        with patch.object(
            self.service,
            "_resolve_project",
            wraps=original_resolve,
        ) as resolve_project:
            snapshots, empty = self.service.bootstrap_snapshots(refresh=self.refresh)

        self.assertIsNone(empty)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(resolve_project.call_count, 0)

    def test_connection_bootstrap_reads_materialized_reports_immediately(self) -> None:
        from hydra_codex.report_renderers import render_json

        report = self.reports["project-a"][0]
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO materialized_report_snapshots(
                       project_id,task_ref,report_json,report_markdown,report_html,
                       last_activity_at,last_activity_epoch_ns,reconciled_at,
                       data_revision)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("project-a", report.task_ref, render_json(report).rstrip("\n"), "", "",
                 report.last_activity_at, require_exact_timestamp(
                     report.last_activity_at, "test report activity",
                 ).epoch_nanoseconds, "2026-07-22T11:00:00Z", 7),
            )
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=7 WHERE singleton=1"
            )
            self._publish_test_fence(
                store, "project-a", (report,), revision=7,
            )
            store.connection.commit()
        finally:
            store.close()
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            snapshots, empty = self.service.bootstrap_snapshots_from_connection(
                connection, refresh=self.refresh,
            )
        finally:
            connection.close()
        payload = snapshots[self.catalog_refs()["project-a"]].as_dict()
        self.assertIsNone(empty)
        self.assertEqual(payload["freshness"]["state"], "current")
        self.assertEqual(payload["project"]["overview"]["headline"]["working_tokens"], report.deduplicated_tokens.working.as_dict())
        self.assertEqual(payload["data_revision"], 7)

    def test_warm_bootstrap_uses_project_stats_without_aggregating_report_history(
        self,
    ) -> None:
        self.materialize("project-a", self.reports["project-a"])
        unrelated = tuple(
            replace(
                public_report(
                    f"unrelated-history-{index}",
                    input_tokens=index + 1,
                    second=1,
                ),
                last_activity_at=(
                    f"2026-07-{1 + index // 24:02d}T{index % 24:02d}:00:00Z"
                ),
            )
            for index in range(250)
        )
        self.materialize("project-unrelated-history", unrelated)
        store = HydraStore(self.database)
        try:
            statements: list[str] = []
            store.connection.set_trace_callback(statements.append)
            self.service.bootstrap_snapshots_from_connection(
                store.connection, refresh=self.refresh,
            )
        finally:
            store.close()

        snapshot_statements = tuple(
            statement.upper()
            for statement in statements
            if "MATERIALIZED_REPORT_SNAPSHOTS" in statement.upper()
        )
        self.assertTrue(snapshot_statements)
        self.assertFalse(any(
            token in statement
            for statement in snapshot_statements
            for token in ("GROUP BY", "COUNT(", "MIN(", "MAX(")
        ), snapshot_statements)
        self.assertTrue(any(
            "MATERIALIZED_PROJECT_STATS" in statement.upper()
            for statement in statements
        ))

    def test_warm_bootstrap_fails_closed_on_incoherent_project_stats(self) -> None:
        self.materialize("project-a", self.reports["project-a"])
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """UPDATE materialized_project_stats
                      SET report_count=report_count+1
                    WHERE project_id='project-a'""",
            )
            store.connection.commit()
            with self.assertRaisesRegex(
                ValueError, "materialized project stats",
            ):
                self.service.bootstrap_snapshots_from_connection(
                    store.connection, refresh=self.refresh,
                )
        finally:
            store.close()

    def test_connection_bootstrap_includes_materialized_only_projects(self) -> None:
        from hydra_codex.report_renderers import render_json

        report = replace(
            public_report("materialized-only", input_tokens=17, second=8),
            last_activity_at="2026-07-23T08:00:00Z",
        )
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO materialized_report_snapshots(
                       project_id,task_ref,report_json,report_markdown,report_html,
                       last_activity_at,last_activity_epoch_ns,reconciled_at,
                       data_revision)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    "project-c", report.task_ref, render_json(report).rstrip("\n"),
                    "", "", report.last_activity_at, require_exact_timestamp(
                        report.last_activity_at, "test report activity",
                    ).epoch_nanoseconds, "2026-07-23T08:00:00Z", 8,
                ),
            )
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=8 WHERE singleton=1"
            )
            self._publish_test_fence(
                store, "project-c", (report,), revision=8,
            )
            store.connection.commit()
        finally:
            store.close()

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            snapshots, empty = self.service.bootstrap_snapshots_from_connection(
                connection, refresh=self.refresh,
            )
        finally:
            connection.close()

        refs = project_catalog_references(
            ("project-a", "project-b", "project-c"), b"k" * 32,
        )
        project_ref = refs["project-c"]
        self.assertIsNone(empty)
        self.assertEqual(len(snapshots), 1)
        payload = next(iter(snapshots.values())).as_dict()
        summary = next(
            project for project in payload["projects"]
            if project["project_ref"] == project_ref
        )
        self.assertEqual(summary["task_count"]["value"], 1)
        self.assertEqual(summary["freshness_state"], "current")

    def test_connection_bootstrap_bounds_reports_and_ignores_future_revisions(self) -> None:
        from hydra_codex.report_renderers import render_json

        reports = tuple(
            replace(
                public_report(f"bounded-{index}", input_tokens=10 + index, second=5),
                last_activity_at=f"2026-07-23T{index:02d}:00:00Z",
            )
            for index in range(12)
        )
        future_only = replace(
            public_report("future-only", input_tokens=101, second=5),
            last_activity_at="2026-07-25T00:00:00Z",
        )
        store = HydraStore(self.database)
        try:
            store.connection.executemany(
                """INSERT INTO materialized_report_snapshots(
                       project_id,task_ref,report_json,report_markdown,report_html,
                       last_activity_at,last_activity_epoch_ns,reconciled_at,
                       data_revision)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                tuple(
                    (
                        "project-a", report.task_ref,
                        render_json(report).rstrip("\n"), "", "",
                        report.last_activity_at, require_exact_timestamp(
                            report.last_activity_at, "test report activity",
                        ).epoch_nanoseconds, "2026-07-23T12:00:00Z", 10,
                    )
                    for report in reports
                ) + ((
                        "project-future", future_only.task_ref,
                        render_json(future_only).rstrip("\n"), "", "",
                        future_only.last_activity_at, require_exact_timestamp(
                            future_only.last_activity_at, "test report activity",
                        ).epoch_nanoseconds, "2026-07-25T00:00:00Z", 11,
                    ),),
            )
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=10 WHERE singleton=1"
            )
            store.connection.commit()
        finally:
            store.close()

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            with patch(
                "hydra_codex.dashboard_queries.validate_task_report",
                wraps=validate_task_report,
            ) as validate:
                snapshots, empty = self.service.bootstrap_snapshots_from_connection(
                    connection, refresh=self.refresh,
                )
        finally:
            connection.close()

        self.assertIsNone(empty)
        self.assertEqual(validate.call_count, 10)
        payload = snapshots[self.catalog_refs()["project-a"]].as_dict()
        project = payload["project"]
        self.assertEqual(len(project["recent_tasks"]), 10)
        self.assertEqual(
            project["overview"]["basis"]["task_ref"], reports[-1].task_ref,
        )
        future_ref = project_catalog_references(
            ("project-future",), b"k" * 32,
        )["project-future"]
        self.assertNotIn(future_ref, snapshots)
        summary = next(
            item for item in payload["projects"]
            if item["project_ref"] == self.catalog_refs()["project-a"]
        )
        self.assertEqual(summary["task_count"]["value"], 12)
        self.assertEqual(payload["data_revision"], 10)

    def test_connection_bootstrap_uses_constant_queries_and_global_report_budget(
        self,
    ) -> None:
        from hydra_codex.report_renderers import render_json

        project_ids = tuple(f"project-budget-{index:03d}" for index in range(40))
        store = HydraStore(self.database)
        try:
            store.connection.executemany(
                """INSERT INTO dashboard_projects(
                       project_id,display_name,first_seen_at,last_seen_at)
                   VALUES (?,?,?,?)""",
                (
                    (
                        project_id, None, "2026-07-23T00:00:00Z",
                        "2026-07-23T12:00:00Z",
                    )
                    for project_id in project_ids
                ),
            )
            snapshots = []
            for project_index, project_id in enumerate(project_ids):
                for task_index in range(12):
                    report = replace(
                        public_report(
                            f"budget-{project_index}-{task_index}",
                            input_tokens=task_index + 1,
                            second=5,
                        ),
                        last_activity_at=(
                            f"2026-07-23T{task_index:02d}:"
                            f"{project_index % 60:02d}:00Z"
                        ),
                    )
                    snapshots.append((
                        project_id, report.task_ref,
                        render_json(report).rstrip("\n"), "", "",
                        report.last_activity_at, require_exact_timestamp(
                            report.last_activity_at, "test report activity",
                        ).epoch_nanoseconds, report.last_activity_at, 9,
                    ))
            store.connection.executemany(
                """INSERT INTO materialized_report_snapshots(
                       project_id,task_ref,report_json,report_markdown,report_html,
                       last_activity_at,last_activity_epoch_ns,reconciled_at,
                       data_revision)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                snapshots,
            )
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=9 WHERE singleton=1"
            )
            store.connection.commit()
        finally:
            store.close()

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            with patch(
                "hydra_codex.dashboard_queries.validate_task_report",
                wraps=validate_task_report,
            ) as validate:
                cached, empty = self.service.bootstrap_snapshots_from_connection(
                    connection, refresh=self.refresh,
                )
        finally:
            connection.close()

        selects = [
            statement for statement in statements
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        self.assertIsNone(empty)
        self.assertEqual(len(cached), 1)
        self.assertLessEqual(len(selects), 3)
        self.assertLessEqual(validate.call_count, 10)
        self.assertEqual(len(next(iter(cached.values())).projects), 42)
        warm_reads = [
            statement for statement in selects
            if "report_json" in statement
            and "materialized_report_snapshots" in statement
        ]
        self.assertEqual(len(warm_reads), 1)
        self.assertIn(
            "ORDER BY last_activity_epoch_ns DESC,task_ref",
            warm_reads[0],
        )
        self.assertNotIn("json_extract", warm_reads[0])

    def test_connection_bootstrap_marks_materialized_projects_with_pending_work_stale(
        self,
    ) -> None:
        from hydra_codex.report_renderers import render_json
        from hydra_codex.sync_state import SyncStateRepository

        store = HydraStore(self.database)
        try:
            for project_id, report in self.reports.items():
                public = report[0]
                store.connection.execute(
                    """INSERT INTO materialized_report_snapshots(
                           project_id,task_ref,report_json,report_markdown,report_html,
                           last_activity_at,last_activity_epoch_ns,reconciled_at,
                           data_revision)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        project_id, public.task_ref,
                        render_json(public).rstrip("\n"), "", "",
                        public.last_activity_at, require_exact_timestamp(
                            public.last_activity_at, "test report activity",
                        ).epoch_nanoseconds, public.last_activity_at, 1,
                    ),
                )
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=1 WHERE singleton=1"
            )
            store.connection.commit()
            repository = SyncStateRepository(store)
            repository.record_hook_event_and_enqueue(
                event_key="pending-a", project_id="project-a",
                session_key="session-a", turn_key="turn-a",
                event_kind="prompt", observed_at="2026-07-23T00:00:00Z",
            )
            repository.mark_dirty(
                "project-b", "project-b", "project",
                "2026-07-23T00:00:01Z",
            )
        finally:
            store.close()

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            cached, empty = self.service.bootstrap_snapshots_from_connection(
                connection, refresh=self.refresh,
            )
        finally:
            connection.close()

        self.assertIsNone(empty)
        payload = next(iter(cached.values())).as_dict()
        states = {
            item["project_ref"]: item["freshness_state"]
            for item in payload["projects"]
        }
        refs = self.catalog_refs()
        self.assertEqual(states[refs["project-a"]], "stale")
        self.assertEqual(states[refs["project-b"]], "stale")
        self.assertEqual(payload["project"]["freshness_state"], "stale")

    def test_connection_bootstrap_scopes_source_revision_freshness_by_project(
        self,
    ) -> None:
        self.materialize("project-a", self.reports["project-a"])
        self.materialize("project-b", self.reports["project-b"])
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO hook_safe_facts(
                       event_key,project_id,session_key,turn_key,event_kind,
                       tool_category,tool_status,duration_ms,observed_at)
                   VALUES (
                       'project-b-after-publish','project-b','session-b',
                       'turn-b','prompt',NULL,NULL,NULL,
                       '2026-07-23T00:00:02Z'
                   )"""
            )
            store.connection.commit()
        finally:
            store.close()

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            snapshots, _empty = self.service.bootstrap_snapshots_from_connection(
                connection, refresh=self.refresh,
            )
        finally:
            connection.close()
        payload = next(iter(snapshots.values())).as_dict()
        refs = self.catalog_refs()
        states = {
            item["project_ref"]: item["freshness_state"]
            for item in payload["projects"]
        }
        self.assertEqual(states[refs["project-a"]], "current")
        self.assertEqual(states[refs["project-b"]], "stale")

        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO file_observations(
                       source_digest,line_number,session_key,operation,
                       relative_path,path_hash)
                   VALUES (
                       'unmapped-dashboard-source',1,'missing-session','read',
                       'safe.txt','safe-hash'
                   )"""
            )
            store.connection.commit()
        finally:
            store.close()

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            snapshots, _empty = self.service.bootstrap_snapshots_from_connection(
                connection, refresh=self.refresh,
            )
        finally:
            connection.close()
        payload = next(iter(snapshots.values())).as_dict()
        states = {
            item["project_ref"]: item["freshness_state"]
            for item in payload["projects"]
        }
        self.assertEqual(states[refs["project-a"]], "stale")
        self.assertEqual(states[refs["project-b"]], "stale")

    def test_source_fact_commit_wakes_polling_and_reload_marks_project_stale(
        self,
    ) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController

        self.materialize("project-a", self.reports["project-a"])
        self.materialize("project-b", self.reports["project-b"])
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=None,
            installation_key=b"k" * 32,
            clock=lambda: datetime(
                2026, 7, 23, 0, 0, 3, tzinfo=timezone.utc,
            ),
        )
        try:
            before = controller.changes(0)["data_revision"]
            store = HydraStore(self.database)
            try:
                store.connection.execute(
                    """INSERT INTO hook_safe_facts(
                           event_key,project_id,session_key,turn_key,event_kind,
                           tool_category,tool_status,duration_ms,observed_at)
                       VALUES (
                           'project-a-poll-wakeup','project-a','session-a',
                           'turn-a','prompt',NULL,NULL,NULL,
                           '2026-07-23T00:00:02Z'
                       )"""
                )
                store.connection.commit()
            finally:
                store.close()

            changes = controller.changes(before)
        finally:
            controller.close()

        self.assertTrue(changes["changed"])
        self.assertGreater(changes["data_revision"], before)

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            snapshots, _empty = self.service.bootstrap_snapshots_from_connection(
                connection, refresh=self.refresh,
            )
        finally:
            connection.close()
        payload = next(iter(snapshots.values())).as_dict()
        refs = self.catalog_refs()
        states = {
            item["project_ref"]: item["freshness_state"]
            for item in payload["projects"]
        }
        self.assertEqual(states[refs["project-a"]], "stale")
        self.assertEqual(states[refs["project-b"]], "current")

    def test_reconcile_fence_success_lookup_uses_exact_composite_index(
        self,
    ) -> None:
        store = HydraStore(self.database)
        try:
            plan = tuple(
                str(row[3])
                for row in store.connection.execute(
                    """EXPLAIN QUERY PLAN
                       SELECT 1 FROM reconciliation_runs
                        WHERE project_id=?
                          AND outcome='success'
                          AND reconciliation_version=?
                          AND input_digest=?
                        LIMIT 1""",
                    ("project-a", 1, "0" * 64),
                )
            )
        finally:
            store.close()

        self.assertTrue(
            any(
                "reconciliation_runs_source_fence_lookup" in step
                for step in plan
            ),
            plan,
        )
        self.assertFalse(
            any("SCAN reconciliation_runs" in step for step in plan),
            plan,
        )

    def test_connection_bootstrap_treats_empty_reconciled_project_as_current(
        self,
    ) -> None:
        store = HydraStore(self.database)
        try:
            reconcile_project(store, "project-empty", b"k" * 32)
        finally:
            store.close()

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            with patch(
                "hydra_codex.dashboard_queries.list_reconciled_reports",
                side_effect=AssertionError(
                    "empty materialized project must stay on the bounded path",
                ),
            ):
                snapshots, empty = (
                    self.service.bootstrap_snapshots_from_connection(
                        connection, refresh=self.refresh,
                    )
                )
        finally:
            connection.close()

        self.assertIsNone(empty)
        payload = next(iter(snapshots.values())).as_dict()
        refs = project_catalog_references(
            ("project-a", "project-b", "project-empty"), b"k" * 32,
        )
        summary = next(
            item for item in payload["projects"]
            if item["project_ref"] == refs["project-empty"]
        )
        self.assertEqual(summary["freshness_state"], "current")
        self.assertEqual(summary["task_count"]["value"], 0)

    def test_uncached_project_snapshot_loads_only_its_materialized_top_ten(
        self,
    ) -> None:
        from hydra_codex.report_renderers import render_json

        store = HydraStore(self.database)
        try:
            for project_id, reports in self.reports.items():
                store.connection.executemany(
                    """INSERT INTO materialized_report_snapshots(
                           project_id,task_ref,report_json,report_markdown,report_html,
                           last_activity_at,last_activity_epoch_ns,reconciled_at,
                           data_revision)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        (
                            project_id, report.task_ref,
                            render_json(report).rstrip("\n"), "", "",
                            report.last_activity_at, require_exact_timestamp(
                                report.last_activity_at, "test report activity",
                            ).epoch_nanoseconds, report.last_activity_at, 4,
                        )
                        for report in reports
                    ),
                )
            store.connection.execute(
                "UPDATE sync_data_revision SET revision=4 WHERE singleton=1"
            )
            for project_id, reports in self.reports.items():
                self._publish_test_fence(
                    store, project_id, reports, revision=4,
                )
            store.connection.commit()
        finally:
            store.close()

        refs = self.catalog_refs()
        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=AssertionError("project snapshot must stay materialized"),
        ), patch(
            "hydra_codex.dashboard_queries.validate_task_report",
            wraps=validate_task_report,
        ) as validate:
            payload = self.service.snapshot(
                project_ref=refs["project-b"],
                task_ref=None,
                refresh=self.refresh,
            ).as_dict()

        self.assertEqual(payload["selected_project_ref"], refs["project-b"])
        self.assertEqual(payload["project"]["freshness_state"], "current")
        self.assertEqual(
            payload["project"]["overview"]["basis"]["task_ref"],
            self.reports["project-b"][0].task_ref,
        )
        self.assertLessEqual(validate.call_count, 10)

    def test_bootstrap_snapshots_preserve_empty_onboarding_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "empty.sqlite3"
            HydraStore(database).close()
            service = DashboardQueryService(
                lambda: HydraStore(database), b"k" * 32,
                lambda: datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
                self.service._doctor_report,
            )

            snapshots, empty = service.bootstrap_snapshots(refresh=self.refresh)

        self.assertEqual(snapshots, {})
        self.assertIsNotNone(empty)
        self.assertEqual(empty.as_dict()["projects"], [])
        self.assertIsNone(empty.as_dict()["selected_project_ref"])
        self.assertEqual(empty.as_dict()["freshness"]["state"], "unavailable")

    def test_empty_materialized_snapshot_rejects_an_unknown_project_selector(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "empty.sqlite3"
            HydraStore(database).close()
            service = DashboardQueryService(
                lambda: HydraStore(database), b"k" * 32,
                lambda: datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
                self.service._doctor_report,
            )

            with self.assertRaisesRegex(
                KeyError, r"^'unknown public reference'$",
            ):
                service.snapshot(
                    project_ref="project_000000000000",
                    task_ref=None,
                    refresh=self.refresh,
                )

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
        self.materialize("project-a", self.reports["project-a"])
        statements: list[str] = []
        opened: list[HydraStore] = []

        def factory() -> HydraStore:
            store = HydraStore(self.database)
            store.connection.set_trace_callback(statements.append)
            opened.append(store)
            validated_reopener = store.validated_reopener

            def tracked_reopener():
                reopen = validated_reopener()

                def tracked_open() -> HydraStore:
                    reopened = reopen()
                    reopened.connection.set_trace_callback(statements.append)
                    opened.append(reopened)
                    return reopened

                return tracked_open

            store.validated_reopener = tracked_reopener  # type: ignore[method-assign]
            return store

        service = DashboardQueryService(
            factory,
            b"k" * 32,
            lambda: datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
            self.service._doctor_report,
        )
        validated: list[str] = []

        def validate(payload, **options):
            self.assertTrue(opened[-1].connection.in_transaction)
            validate_task_report(payload, **options)
            validated.append(str(payload["task_ref"]))

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=AssertionError("task pages must stay materialized"),
        ), patch(
            "hydra_codex.dashboard_queries.validate_task_report",
            side_effect=validate,
        ), patch(
            "hydra_codex.dashboard_model.validate_task_report",
            side_effect=validate,
        ):
            first_page = service.tasks(project_ref, cursor=None, limit=1).as_dict()
            first_validations = len(validated)
            second_page = service.tasks(
                project_ref, cursor=first_page["page"]["next_cursor"], limit=1,
            ).as_dict()
            with self.assertRaises(ValueError):
                service.tasks(project_ref, cursor=None, limit=101)

        self.assertEqual(
            [item["task_ref"] for item in first_page["items"]],
            [self.reports["project-a"][0].task_ref],
        )
        self.assertEqual(
            [item["task_ref"] for item in second_page["items"]],
            [self.reports["project-a"][1].task_ref],
        )
        self.assertFalse(second_page["page"]["has_more"])
        # Each bounded row gets one legacy-shape validation before the safe
        # freshness overlay, then the selected/sentinel DTO validates strictly.
        self.assertEqual(first_validations, 4)
        self.assertEqual(len(validated), 8)
        report_reads = [
            statement for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "report_json" in statement
            and "materialized_report_snapshots" in statement
        ]
        self.assertEqual(len(report_reads), 3)
        self.assertTrue(all("LIMIT 2" in statement for statement in report_reads[::2]))

    def test_task_page_rejects_stale_source_fence_before_reading_payloads(
        self,
    ) -> None:
        self.materialize("project-a", self.reports["project-a"])
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO hook_safe_facts(
                       event_key,project_id,session_key,turn_key,event_kind,
                       tool_category,tool_status,duration_ms,observed_at)
                   VALUES (
                       'task-page-after-publish','project-a','session-a',
                       'turn-a','prompt',NULL,NULL,NULL,
                       '2026-07-23T00:00:02Z'
                   )"""
            )
            store.connection.commit()
        finally:
            store.close()

        with patch.object(
            self.service,
            "_materialized_payload",
            side_effect=AssertionError("stale task payload must not be read"),
        ), self.assertRaisesRegex(ReconciliationStale, "reconcile_required"):
            self.service.tasks(
                self.catalog_refs()["project-a"], cursor=None, limit=10,
            )

    def test_task_page_orders_full_timestamp_precision_before_task_ref(self) -> None:
        older = replace(
            public_report("nanosecond-older", input_tokens=10, second=5),
            last_activity_at="2026-07-22T11:00:00.100000001Z",
        )
        newer = replace(
            public_report("nanosecond-newer", input_tokens=20, second=5),
            last_activity_at="2026-07-22T11:00:00.100000002Z",
        )
        self.materialize("project-a", (older, newer))

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=AssertionError("task ordering must stay materialized"),
        ):
            page = self.service.tasks(
                self.catalog_refs()["project-a"], cursor=None, limit=10,
            ).as_dict()

        self.assertEqual(
            [item["task_ref"] for item in page["items"]],
            [newer.task_ref, older.task_ref],
        )

    def test_repeated_hot_queries_share_one_full_store_validation(self) -> None:
        self.materialize("project-a", self.reports["project-a"])
        validation_calls = 0
        original = HydraStore._validate_schema

        def validate(store: HydraStore, latest: int) -> None:
            nonlocal validation_calls
            validation_calls += 1
            original(store, latest)

        with patch.object(HydraStore, "_validate_schema", new=validate):
            project_ref = self.catalog_refs()["project-a"]
            self.service.tasks(project_ref, cursor=None, limit=1)
            self.service.tasks(project_ref, cursor=None, limit=1)

        self.assertEqual(validation_calls, 1)

    def test_legacy_materialized_report_gets_current_safe_freshness_overlay(
        self,
    ) -> None:
        self.materialize("project-a", self.reports["project-a"])
        store = HydraStore(self.database)
        try:
            row = store.connection.execute(
                """SELECT task_ref,report_json FROM materialized_report_snapshots
                     WHERE project_id='project-a'
                     ORDER BY last_activity_epoch_ns DESC LIMIT 1""",
            ).fetchone()
            payload = json.loads(str(row["report_json"]))
            payload.pop("sync_freshness")
            store.connection.execute(
                """UPDATE materialized_report_snapshots SET report_json=?
                     WHERE project_id='project-a' AND task_ref=?""",
                (
                    json.dumps(
                        payload, sort_keys=True, separators=(",", ":"),
                    ),
                    str(row["task_ref"]),
                ),
            )
            store.connection.commit()
            SyncStateRepository(store).register_and_enqueue(
                root_kind="sessions",
                source_locator="pending.jsonl",
                project_id="project-a",
                observed_at="2026-07-22T11:30:00Z",
            )
            revision = SyncStateRepository(store).data_revision()
        finally:
            store.close()

        project_ref = self.catalog_refs()["project-a"]
        page = self.service.tasks(
            project_ref, cursor=None, limit=1,
        ).as_dict()
        self.assertEqual(page["items"][0]["sync_freshness"], {
            "schema_version": "hydra.sync-freshness/v1",
            "state": "queued",
            "data_revision": revision,
        })

    def test_materialized_reads_reject_swapped_index_epochs(self) -> None:
        latest, first = self.reports["project-a"]
        self.materialize("project-a", (latest, first))
        connection = sqlite3.connect(self.database)
        try:
            latest_epoch = connection.execute(
                """SELECT last_activity_epoch_ns
                     FROM materialized_report_snapshots
                    WHERE project_id='project-a' AND task_ref=?""",
                (latest.task_ref,),
            ).fetchone()[0]
            first_epoch = connection.execute(
                """SELECT last_activity_epoch_ns
                     FROM materialized_report_snapshots
                    WHERE project_id='project-a' AND task_ref=?""",
                (first.task_ref,),
            ).fetchone()[0]
            connection.execute(
                """UPDATE materialized_report_snapshots
                      SET last_activity_epoch_ns=CASE task_ref
                          WHEN ? THEN ? WHEN ? THEN ? END
                    WHERE project_id='project-a'""",
                (latest.task_ref, first_epoch, first.task_ref, latest_epoch),
            )
            connection.commit()
        finally:
            connection.close()

        project_ref = self.catalog_refs()["project-a"]
        bootstrap_connection = sqlite3.connect(self.database)
        bootstrap_connection.row_factory = sqlite3.Row
        try:
            with self.assertRaisesRegex(
                ValueError, "materialized task report activity is invalid",
            ):
                self.service.bootstrap_snapshots_from_connection(
                    bootstrap_connection, refresh=self.refresh,
                )
        finally:
            bootstrap_connection.close()
        with self.assertRaisesRegex(
            ValueError, "materialized task report activity is invalid",
        ):
            self.service.tasks(project_ref, cursor=None, limit=1)
        with self.assertRaisesRegex(
            ValueError, "materialized task report activity is invalid",
        ):
            self.service.tasks(project_ref, cursor=latest.task_ref, limit=1)

    def test_compare_requires_both_refs_in_selected_project(self) -> None:
        project_ref = self.catalog_refs()["project-a"]
        latest, first = self.reports["project-a"]
        unrelated = replace(
            public_report("unrelated", input_tokens=99, second=5),
            last_activity_at="2026-07-22T12:00:00Z",
        )
        self.materialize(
            "project-a", self.reports["project-a"] + (unrelated,),
        )
        from hydra_codex.audit_service import read_materialized_task_reports

        with patch(
            "hydra_codex.dashboard_queries.list_reconciled_reports",
            side_effect=AssertionError("compare must stay materialized"),
        ), patch(
            "hydra_codex.dashboard_queries.read_materialized_task_reports",
            wraps=read_materialized_task_reports,
        ) as read_reports, patch(
            "hydra_codex.audit_service.validate_task_report",
            wraps=validate_task_report,
        ) as validate:
            comparison = self.service.compare(project_ref, first.task_ref, latest.task_ref)
            valid_compare_reads = validate.call_count
            with self.assertRaisesRegex(KeyError, r"^'unknown public reference'$"):
                self.service.compare(project_ref, "task_000000000000", latest.task_ref)

        self.assertEqual(comparison.schema_version, "hydra.comparison/v2")
        self.assertEqual(valid_compare_reads, 2)
        self.assertEqual(read_reports.call_count, 2)
        self.assertEqual(
            read_reports.call_args_list[0].args[2],
            (first.task_ref, latest.task_ref),
        )

    def test_compare_rejects_stale_source_fence_before_reading_payloads(
        self,
    ) -> None:
        project_ref = self.catalog_refs()["project-a"]
        latest, first = self.reports["project-a"]
        self.materialize("project-a", self.reports["project-a"])
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                """INSERT INTO hook_safe_facts(
                       event_key,project_id,session_key,turn_key,event_kind,
                       tool_category,tool_status,duration_ms,observed_at)
                   VALUES (
                       'compare-after-publish','project-a','session-a',
                       'turn-a','prompt',NULL,NULL,NULL,
                       '2026-07-23T00:00:02Z'
                   )"""
            )
            store.connection.commit()
        finally:
            store.close()

        with patch(
            "hydra_codex.dashboard_queries.read_materialized_task_reports",
            side_effect=AssertionError("stale comparison payload must not be read"),
        ), self.assertRaisesRegex(ReconciliationStale, "reconcile_required"):
            self.service.compare(project_ref, first.task_ref, latest.task_ref)

    def test_evidence_reads_only_latest_selected_project_pilot(self) -> None:
        store = HydraStore(self.database)
        try:
            store.connection.executemany(
                """INSERT INTO pilot_runs(
                       pilot_id,project_id,started_at,closed_at,target,task_family,
                       thresholds_json,state)
                   VALUES (?,?,?,?,1,'all','{}',?)""",
                (
                    ("hpilot_v1_11111111111111111111111111111111", "project-a", "2026-07-22T00:00:00Z", "2026-07-22T01:00:00Z", "closed"),
                    ("hpilot_v1_22222222222222222222222222222222", "project-a", "2026-07-22T00:00:00.1Z", None, "open"),
                    ("hpilot_v1_33333333333333333333333333333333", "project-b", "2026-07-23T00:00:00Z", None, "open"),
                ),
            )
            self._publish_test_fence(
                store, "project-a", (), revision=0,
            )
            store.connection.commit()
        finally:
            store.close()
        evidence = AuditEvidence(
            "ev_0123456789abcdef", "tasks.safe.working", 30, "tokens", "derived",
        )
        called: list[tuple[str, str]] = []

        def build(_store, *, project_id, pilot_id):
            called.append((project_id, pilot_id))
            return SimpleNamespace(evidence_appendix=(evidence,))

        before = self.database_dump()
        with patch(
            "hydra_codex.audit_service.build_pilot_audit",
            side_effect=AssertionError("dashboard evidence must stay materialized"),
        ), patch(
            "hydra_codex.dashboard_queries.read_materialized_pilot_audit",
            side_effect=build,
        ):
            selected = self.service.evidence(
                self.catalog_refs()["project-a"], evidence.evidence_id,
            )

        self.assertIs(selected, evidence)
        self.assertEqual(called, [(
            "project-a", "hpilot_v1_22222222222222222222222222222222",
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
