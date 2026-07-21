from __future__ import annotations

from pathlib import Path
import hashlib
import json
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest

from hydra_codex.storage import HydraStore, StorageUnavailable
from hydra_codex.reconcile_engine import list_reconciled_reports
from hydra_codex.services import LocalCommandServices
from tests.test_report_semantic_trends import BASE, PROJECT, StoredReportScenario, stamp


class PilotMigrationTests(unittest.TestCase):
    def test_pilot_state_is_persisted_and_receipts_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            try:
                self.assertEqual(store.schema_version(), 32)
                tables = {
                    str(row[0])
                    for row in store.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {"pilot_runs", "pilot_tasks", "pilot_receipts"}.issubset(tables)
                )
                store.connection.execute(
                    """INSERT INTO pilot_runs(
                           pilot_id,project_id,started_at,closed_at,target,
                           task_family,thresholds_json,state)
                       VALUES ('pilot_test','project','2026-07-22T00:00:00Z',NULL,5,
                               'telemetry-analysis','{}','open')"""
                )
                store.connection.execute(
                    """INSERT INTO pilot_receipts(
                           receipt_id,pilot_id,created_at,decision,task_refs_json,
                           reconciliation_version,schema_version,thresholds_json,
                           observed_facts_json,snapshot_digest,audit_sha256)
                       VALUES ('receipt_test','pilot_test','2026-07-22T01:00:00Z',
                               'rejected','[]',1,32,'{}','{}','digest','audit')"""
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(
                        "UPDATE pilot_receipts SET decision='verified'"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute("DELETE FROM pilot_receipts")
            finally:
                store.close()

    def test_altered_receipt_immutability_trigger_fails_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "hydra.sqlite3"
            store = HydraStore(database)
            store.close()
            connection = sqlite3.connect(database)
            connection.execute("DROP TRIGGER pilot_receipts_immutable_update")
            connection.execute(
                """CREATE TRIGGER pilot_receipts_immutable_update
                       BEFORE UPDATE ON pilot_receipts BEGIN SELECT 1; END"""
            )
            connection.commit()
            connection.close()

            with self.assertRaises(StorageUnavailable):
                HydraStore(database)


class PilotLifecycleTests(unittest.TestCase):
    def test_start_persists_one_project_cohort_and_resumes_it_after_restart(self) -> None:
        try:
            from hydra_codex.pilot import start_pilot
        except ModuleNotFoundError:
            self.fail("pilot start API is missing")

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "hydra.sqlite3"
            started_at = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
            first_store = HydraStore(database)
            try:
                first = start_pilot(
                    first_store,
                    project_id="project",
                    target=5,
                    task_family="telemetry-analysis",
                    now=started_at,
                )
            finally:
                first_store.close()

            resumed_store = HydraStore(database)
            try:
                resumed = start_pilot(
                    resumed_store,
                    project_id="project",
                    target=5,
                    task_family="telemetry-analysis",
                    now=datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc),
                )
                row = resumed_store.connection.execute(
                    "SELECT * FROM pilot_runs"
                ).fetchone()
            finally:
                resumed_store.close()

            self.assertEqual(resumed.pilot_id, first.pilot_id)
            self.assertEqual(resumed.started_at, started_at)
            self.assertEqual(resumed.target, 5)
            self.assertEqual(row["state"], "open")
            self.assertEqual(row["started_at"], "2026-07-22T10:00:00Z")


class PilotCohortTests(StoredReportScenario):
    def _accepted_transport(self, session: str, second: int, latency_ms: int = 1000) -> None:
        for index in range(2):
            staged_second = second + index
            self.db.execute(
                """INSERT INTO annotation_transport_events(
                       transport_key,project_id,session_key,turn_key,request_digest,
                       disposition,diagnostic_category,staged_at,staged_at_ns,
                       staged_order,received_at,latency_ms,provenance)
                   VALUES (?,?,?,?,?,'accepted',NULL,?,?,?,?,?,'derived')""",
                (
                    f"transport-{session}-{index}", PROJECT, session,
                    f"turn-{session}", f"request-{session}-{index}",
                    stamp(staged_second), staged_second * 1_000_000_000,
                    f"{staged_second:020d}:horder_v1_{session}-{index}",
                    (BASE + timedelta(
                        seconds=staged_second, milliseconds=latency_ms,
                    )).isoformat(),
                    latency_ms,
                ),
            )

    def test_status_enrolls_every_completed_root_after_start_and_fifth_task_changes_gate(self) -> None:
        try:
            from hydra_codex.pilot import pilot_status, start_pilot
        except ImportError:
            self.fail("pilot status API is missing")

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE + timedelta(seconds=10),
        )
        self.add_task("old-root", 0, 100, instrumented=False)
        for index, offset in enumerate((20, 40, 60, 80), start=1):
            name = f"root-{index}"
            self.add_task(
                name, offset, 100,
                family="audit" if index == 4 else "telemetry-analysis",
            )
            self.db.execute(
                "UPDATE annotations SET scope_change='none' WHERE session_id=?",
                (name,),
            )
            self._accepted_transport(name, offset + 2)
        self.reconcile()

        before_fifth = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertEqual(before_fifth["facts"]["eligible_tasks"], 4)
        self.assertEqual(before_fifth["facts"]["instrumented_tasks"], 4)
        self.assertEqual(before_fifth["facts"]["enrollment"], 1.0)
        self.assertFalse(before_fifth["threshold_results"]["minimum_completed_tasks"])
        self.assertFalse(before_fifth["transport_verified"])

        self.add_task("root-5", 100, 100, instrumented=False)
        self.reconcile()
        after_fifth = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertEqual(after_fifth["schema_version"], "hydra.pilot/v1")
        self.assertEqual(after_fifth["facts"]["eligible_tasks"], 5)
        self.assertEqual(after_fifth["facts"]["instrumented_tasks"], 4)
        self.assertEqual(after_fifth["facts"]["enrollment"], 0.8)
        self.assertEqual(after_fifth["facts"]["initial_missing"], 1)
        self.assertEqual(after_fifth["facts"]["finish_missing"], 1)
        self.assertEqual(after_fifth["facts"]["delivery_failures"], 0)
        self.assertEqual(after_fifth["facts"]["semantic_conflicts"], 0)
        self.assertEqual(after_fifth["facts"]["schema_diagnostics"], 0)
        self.assertEqual(after_fifth["facts"]["aggregate_coverage"], 0.8)
        self.assertEqual(after_fifth["facts"]["minimum_task_coverage"], 0.0)
        self.assertEqual(after_fifth["facts"]["staging_latency_p95_ms"], 1000)
        self.assertIsNone(after_fifth["facts"]["token_overhead"])
        self.assertTrue(after_fifth["threshold_results"]["minimum_completed_tasks"])
        self.assertFalse(after_fifth["threshold_results"]["minimum_enrollment"])
        self.assertFalse(after_fifth["transport_verified"])
        self.assertFalse(after_fifth["trend_ready"])
        self.assertEqual(len(after_fifth["tasks"]), 5)
        self.assertEqual(
            {item["task_family"] for item in after_fifth["tasks"]},
            {None, "audit", "telemetry-analysis"},
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM pilot_tasks WHERE pilot_id=?", (run.pilot_id,)
            ).fetchone()[0],
            5,
        )

    def test_conflicting_task_families_reconcile_to_null(self) -> None:
        self.add_task(
            "mixed-family", 20, 100, finish=False,
            family="telemetry-analysis",
        )
        self.db.execute(
            "UPDATE annotations SET scope_change='none' WHERE session_id='mixed-family'"
        )
        self._annotation(
            "mixed-family", "turn-mixed-family", 2, 22, "phase", "review",
            "review_finding", "none", "audit", 0.9, None, "safe", "model_reported",
        )
        self.db.execute(
            "UPDATE trusted_turn_bindings SET last_sequence=2 WHERE session_key='mixed-family'"
        )

        report = self.reconcile()[0]

        self.assertIsNone(report.task_family)
        self.assertIsNone(report.trend_input.task_family)

    def test_expanded_and_redefined_scope_are_excluded_from_automatic_trends(self) -> None:
        self.add_task(
            "expanded-scope", 20, 100, family="telemetry-analysis",
        )
        self.add_task(
            "redefined-scope", 40, 100, family="telemetry-analysis",
        )
        self.db.execute(
            """UPDATE annotations SET scope_change='redefined'
                 WHERE session_id='redefined-scope' AND provenance='model_reported'"""
        )

        reports = self.reconcile()

        self.assertEqual(
            {report.task_family for report in reports}, {"telemetry-analysis"},
        )
        self.assertTrue(all(
            report.trend_input.task_family is None for report in reports
        ))

    def test_verified_receipt_binds_exact_audit_snapshot_and_becomes_stale(self) -> None:
        try:
            from hydra_codex.pilot import close_pilot, pilot_status, start_pilot
        except ImportError:
            self.fail("pilot close API is missing")

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE + timedelta(seconds=10),
        )
        for index, offset in enumerate((20, 40, 60, 80, 100), start=1):
            name = f"verified-{index}"
            self.add_task(name, offset, 100, family="telemetry-analysis")
            self.db.execute(
                "UPDATE annotations SET scope_change='none' WHERE session_id=?",
                (name,),
            )
            self._accepted_transport(name, offset + 2, latency_ms=1500)
        self.reconcile()
        before = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()
        self.assertTrue(before["transport_verified"])
        self.assertFalse(before["trend_ready"])
        audit_bytes = json.dumps(
            {"schema_version": "hydra.audit/v1", "pilot_snapshot": before},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        audit_path = self.root / "pilot-audit.json"
        audit_path.write_bytes(audit_bytes)

        receipt = close_pilot(
            self.store,
            project_id=PROJECT,
            pilot_id=run.pilot_id,
            audit_json=audit_path,
            decision="verified",
            now=BASE + timedelta(seconds=120),
        ).as_dict()
        current = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertEqual(receipt["audit_sha256"], hashlib.sha256(audit_bytes).hexdigest())
        self.assertEqual(receipt["snapshot_digest"], before["snapshot_digest"])
        self.assertEqual(receipt["task_refs"], [item["task_ref"] for item in before["tasks"]])
        self.assertTrue(current["receipt"]["current"])
        self.assertTrue(current["trend_ready"])
        self.assertEqual(current["pilot"]["state"], "closed")

        self.db.execute(
            """INSERT INTO annotation_transport_events(
                   transport_key,project_id,session_key,turn_key,request_digest,
                   disposition,diagnostic_category,staged_at,staged_at_ns,
                   staged_order,received_at,latency_ms,provenance)
               VALUES ('transport-late-failure',?,?,?,NULL,'quarantined','malformed',
                       ?,?,?,?,500,'derived')""",
            (
                PROJECT, "verified-1", "turn-verified-1", stamp(24),
                24_000_000_000, "00000000000000000024:horder_v1_failure",
                (BASE + timedelta(seconds=24, milliseconds=500)).isoformat(),
            ),
        )
        self.db.commit()
        stale = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertFalse(stale["receipt"]["current"])
        self.assertFalse(stale["trend_ready"])
        self.assertFalse(stale["transport_verified"])

    def test_pilot_trend_warning_stays_candidate_until_verified_receipt(self) -> None:
        from hydra_codex.pilot import close_pilot, pilot_status, start_pilot

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="quiz",
            now=BASE - timedelta(seconds=1),
        )
        for index in range(5):
            name = f"trend-{index}"
            offset = index * 20
            self.add_task(
                name, offset, 200 if index == 4 else 100,
                retries=3 if index == 4 else 1,
                semantic_cause="test_failure",
            )
            self.db.execute(
                "UPDATE annotations SET scope_change='none' WHERE session_id=?",
                (name,),
            )
            self._accepted_transport(name, offset + 2)
        self.reconcile()
        status = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()
        self.assertTrue(status["transport_verified"])

        candidate = list_reconciled_reports(self.store, PROJECT)[0]

        self.assertFalse(candidate.trend_result.warning)
        self.assertIn("pilot_receipt_required", candidate.trend_result.caveats)

        audit_path = self.root / "trend-audit.json"
        audit_path.write_text(
            json.dumps(
                {"schema_version": "hydra.audit/v1", "pilot_snapshot": status},
                sort_keys=True, separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        close_pilot(
            self.store,
            project_id=PROJECT,
            pilot_id=run.pilot_id,
            audit_json=audit_path,
            decision="verified",
            now=BASE + timedelta(seconds=100),
        )

        verified = list_reconciled_reports(self.store, PROJECT)[0]
        self.assertTrue(verified.trend_result.warning)


class PilotServiceTests(StoredReportScenario):
    def test_local_service_renders_public_pilot_status_in_all_formats(self) -> None:
        service = LocalCommandServices(
            environ={}, clock=lambda: BASE + timedelta(seconds=10),
        )
        if not hasattr(service, "pilot_start"):
            self.fail("local pilot services are missing")

        started = json.loads(service.pilot_start(
            5, "telemetry-analysis", self.database, self.project,
        ))
        self.reconcile()
        rendered_json = service.pilot_status("json", self.database, self.project)
        rendered_markdown = service.pilot_status(
            "markdown", self.database, self.project,
        )
        rendered_html = service.pilot_status("html", self.database, self.project)
        payload = json.loads(rendered_json)

        self.assertEqual(started["command"], "pilot start")
        self.assertRegex(started["pilot_id"], r"^hpilot_v1_[0-9a-f]{32}$")
        self.assertEqual(payload["schema_version"], "hydra.pilot/v1")
        self.assertIn("Hydra pilot status", rendered_markdown)
        self.assertIn("<html", rendered_html)
        for private in (PROJECT, str(self.project), "root_key", "session_key"):
            self.assertNotIn(private, rendered_json + rendered_markdown + rendered_html)


if __name__ == "__main__":
    unittest.main()
