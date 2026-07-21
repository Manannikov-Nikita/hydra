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
                self.assertEqual(store.schema_version(), 34)
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
                       VALUES ('pilot_test','project','2026-07-22T00:00:00Z',
                               '2026-07-22T01:00:00Z',5,
                               'telemetry-analysis','{}','closed')"""
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

    def test_insert_or_replace_cannot_rewrite_an_existing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            try:
                store.connection.execute(
                    """INSERT INTO pilot_runs(
                           pilot_id,project_id,started_at,closed_at,target,
                           task_family,thresholds_json,state)
                       VALUES ('pilot_replace','project','2026-07-22T00:00:00Z',
                               '2026-07-22T01:00:00Z',5,
                               'telemetry-analysis','{}','closed')"""
                )
                values = (
                    "receipt_replace", "pilot_replace", "2026-07-22T01:00:00Z",
                    "rejected", "[]", 1, store.schema_version(), "{}", "{}",
                    "digest", "audit",
                )
                store.connection.execute(
                    """INSERT INTO pilot_receipts(
                           receipt_id,pilot_id,created_at,decision,task_refs_json,
                           reconciliation_version,schema_version,thresholds_json,
                           observed_facts_json,snapshot_digest,audit_sha256)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(
                        """INSERT OR REPLACE INTO pilot_receipts(
                               receipt_id,pilot_id,created_at,decision,task_refs_json,
                               reconciliation_version,schema_version,thresholds_json,
                               observed_facts_json,snapshot_digest,audit_sha256)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (*values[:3], "verified", *values[4:]),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(
                        """INSERT OR REPLACE INTO pilot_receipts(
                               receipt_id,pilot_id,created_at,decision,task_refs_json,
                               reconciliation_version,schema_version,thresholds_json,
                               observed_facts_json,snapshot_digest,audit_sha256)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "receipt_rekeyed", values[1], values[2], "verified",
                            *values[4:],
                        ),
                    )

                self.assertEqual(store.connection.execute(
                    "SELECT decision FROM pilot_receipts WHERE receipt_id='receipt_replace'"
                ).fetchone()[0], "rejected")
            finally:
                store.close()

    def test_insert_immutability_trigger_is_required_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "hydra.sqlite3"
            store = HydraStore(database)
            trigger = store.connection.execute(
                """SELECT sql FROM sqlite_master
                     WHERE type='trigger'
                       AND name='pilot_receipts_immutable_insert'"""
            ).fetchone()
            store.close()
            if trigger is None:
                self.fail("pilot receipt insert immutability trigger is missing")
            connection = sqlite3.connect(database)
            connection.execute("DROP TRIGGER pilot_receipts_immutable_insert")
            connection.execute(
                """CREATE TRIGGER pilot_receipts_immutable_insert
                       BEFORE INSERT ON pilot_receipts BEGIN SELECT 1; END"""
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

    def _verified_close_snapshot(self, prefix: str):
        from hydra_codex.pilot import pilot_status, start_pilot

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE - timedelta(seconds=1),
        )
        for index, offset in enumerate((0, 20, 40, 60, 80), start=1):
            name = f"{prefix}-{index}"
            self.add_task(name, offset, 100, family="telemetry-analysis")
            self.db.execute(
                "UPDATE annotations SET scope_change='none' WHERE session_id=?",
                (name,),
            )
            self._accepted_transport(name, offset + 2)
        self.reconcile()
        snapshot = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()
        self.assertTrue(snapshot["transport_verified"])
        return run, snapshot

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

    def test_fractional_and_offset_events_after_completion_are_excluded(self) -> None:
        from hydra_codex.pilot import pilot_status, start_pilot

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE - timedelta(seconds=1),
        )
        self.add_task("temporal-boundary", 0, 100, instrumented=False)
        self.db.execute(
            """INSERT INTO sessions(
                   session_id,project_id,worktree_path,started_at,provenance)
               VALUES ('temporal-boundary',?,'.',?,'exact')""",
            (PROJECT, stamp(0)),
        )
        self.db.execute(
            """INSERT INTO turns(turn_id,session_id,ordinal,observed_at,provenance)
               VALUES ('turn-temporal-boundary','temporal-boundary',0,?,'exact')""",
            (stamp(0),),
        )
        self.db.execute(
            """INSERT INTO trusted_turn_bindings(
                   turn_key,project_id,session_key,created_at,state,last_sequence)
               VALUES ('turn-temporal-boundary',?,'temporal-boundary',?,'open',0)""",
            (PROJECT, stamp(1)),
        )
        self._annotation(
            "temporal-boundary", "turn-temporal-boundary", 0, 8,
            "phase", "understand", "prompt", "none", "telemetry-analysis",
            1.0, None, "", "derived",
        )
        self.db.execute(
            """UPDATE annotations
                  SET observed_at='2026-07-21T00:00:09.500+00:00'
                WHERE annotation_id='annotation-temporal-boundary-0'"""
        )
        self.db.execute(
            """INSERT INTO annotation_transport_events(
                   transport_key,project_id,session_key,turn_key,request_digest,
                   disposition,diagnostic_category,staged_at,staged_at_ns,
                   staged_order,received_at,latency_ms,provenance)
               VALUES ('transport-after-cutoff',?,'temporal-boundary',
                       'turn-temporal-boundary','request-after-cutoff','accepted',
                       NULL,'2026-07-20T20:00:09.250-04:00',9250000000,
                       '00000000009250000000:horder_v1_after',
                       '2026-07-21T00:00:10Z',750,'derived')""",
            (PROJECT,),
        )
        self.db.execute(
            """INSERT INTO annotation_transport_events(
                   transport_key,project_id,session_key,turn_key,request_digest,
                   disposition,diagnostic_category,staged_at,staged_at_ns,
                   staged_order,received_at,latency_ms,provenance)
               VALUES ('transport-before-cutoff',?,'temporal-boundary',
                       'turn-temporal-boundary','request-before-cutoff','accepted',
                       NULL,'2026-07-21T04:00:08.500+04:00',8500000000,
                       '00000000008500000000:horder_v1_before',
                       '2026-07-21T00:00:08.750Z',250,'derived')""",
            (PROJECT,),
        )
        self.reconcile()

        task = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()["tasks"][0]

        self.assertTrue(task["instrumented"])
        self.assertTrue(task["initial_missing"])
        self.assertEqual(task["accepted_transport_events"], 1)
        self.assertEqual(task["staging_latency_p95_ms"], 250)

    def test_submicrosecond_events_after_completion_are_excluded(self) -> None:
        from hydra_codex.pilot import pilot_status, start_pilot

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE - timedelta(seconds=1),
        )
        self.add_task("submicrosecond-boundary", 0, 100, instrumented=False)
        self.db.execute(
            """INSERT INTO sessions(
                   session_id,project_id,worktree_path,started_at,provenance)
               VALUES ('submicrosecond-boundary',?,'.',?,'exact')""",
            (PROJECT, stamp(0)),
        )
        self.db.execute(
            """INSERT INTO turns(turn_id,session_id,ordinal,observed_at,provenance)
               VALUES ('turn-submicrosecond-boundary',
                       'submicrosecond-boundary',0,?,'exact')""",
            (stamp(0),),
        )
        self.db.execute(
            """INSERT INTO trusted_turn_bindings(
                   turn_key,project_id,session_key,created_at,state,last_sequence)
               VALUES ('turn-submicrosecond-boundary',?,
                       'submicrosecond-boundary',?,'open',0)""",
            (PROJECT, stamp(1)),
        )
        self._annotation(
            "submicrosecond-boundary", "turn-submicrosecond-boundary", 0, 8,
            "phase", "understand", "prompt", "none", "telemetry-analysis",
            1.0, None, "", "derived",
        )
        self.db.execute(
            """UPDATE annotations
                  SET observed_at='2026-07-21T00:00:09.0000001Z'
                WHERE annotation_id='annotation-submicrosecond-boundary-0'"""
        )
        for suffix, staged_at, staged_ns, latency in (
            ("after", "2026-07-21T00:00:09.0000001Z", 9_000_000_100, 700),
            ("before", "2026-07-21T00:00:08.9999999Z", 8_999_999_900, 300),
        ):
            self.db.execute(
                """INSERT INTO annotation_transport_events(
                       transport_key,project_id,session_key,turn_key,
                       request_digest,disposition,diagnostic_category,staged_at,
                       staged_at_ns,staged_order,received_at,latency_ms,provenance)
                   VALUES (?,?,?,'turn-submicrosecond-boundary',?,'accepted',
                           NULL,?,?,?,?,?,'derived')""",
                (
                    f"transport-submicrosecond-{suffix}", PROJECT,
                    "submicrosecond-boundary",
                    f"request-submicrosecond-{suffix}", staged_at, staged_ns,
                    f"{staged_ns:020d}:horder_v1_submicrosecond-{suffix}",
                    stamp(10), latency,
                ),
            )
        self.reconcile()

        task = pilot_status(
            self.store, PROJECT, run.pilot_id,
        ).as_dict()["tasks"][0]

        self.assertTrue(task["instrumented"])
        self.assertTrue(task["initial_missing"])
        self.assertEqual(task["accepted_transport_events"], 1)
        self.assertEqual(task["staging_latency_p95_ms"], 300)

    def test_malformed_transport_timestamp_fails_closed(self) -> None:
        from hydra_codex.pilot import pilot_status

        run, _snapshot = self._verified_close_snapshot("malformed-transport")
        self.db.execute(
            """INSERT INTO annotation_transport_events(
                   transport_key,project_id,session_key,turn_key,request_digest,
                   disposition,diagnostic_category,staged_at,staged_at_ns,
                   staged_order,received_at,latency_ms,provenance)
               VALUES ('transport-malformed-time',?,'malformed-transport-1',
                       'turn-malformed-transport-1',NULL,'quarantined','malformed',
                       'not-rfc3339',1,'00000000000000000001:horder_v1_bad',
                       ?,100,'derived')""",
            (PROJECT, stamp(3)),
        )
        self.db.execute(
            """INSERT INTO annotation_transport_events(
                   transport_key,project_id,session_key,turn_key,request_digest,
                   disposition,diagnostic_category,staged_at,staged_at_ns,
                   staged_order,received_at,latency_ms,provenance)
               VALUES ('transport-unsupported-precision',?,
                       'malformed-transport-1','turn-malformed-transport-1',
                       'request-unsupported-precision','accepted',NULL,
                       '2026-07-21T00:00:09.0000000001Z',9000000001,
                       '00000000009000000001:horder_v1_too-precise',
                       ?,100,'derived')""",
            (PROJECT, stamp(10)),
        )
        for suffix, staged_at in (
            ("invalid-offset-minute", "2026-07-21T00:00:09-00:99"),
            ("unknown-zero-offset", "2026-07-21T00:00:09-00:00"),
        ):
            self.db.execute(
                """INSERT INTO annotation_transport_events(
                       transport_key,project_id,session_key,turn_key,
                       request_digest,disposition,diagnostic_category,staged_at,
                       staged_at_ns,staged_order,received_at,latency_ms,provenance)
                   VALUES (?,?,'malformed-transport-1',
                           'turn-malformed-transport-1',?,'accepted',NULL,?,2,
                           ?,?,100,'derived')""",
                (
                    f"transport-{suffix}", PROJECT, f"request-{suffix}",
                    staged_at, f"00000000000000000002:horder_v1_{suffix}",
                    stamp(10),
                ),
            )

        status = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertEqual(status["facts"]["delivery_failures"], 4)
        self.assertFalse(status["transport_verified"])

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

    def test_family_conflict_fails_pilot_semantic_conflict_threshold_once(self) -> None:
        from hydra_codex.pilot import pilot_status, start_pilot

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE - timedelta(seconds=1),
        )
        for index, offset in enumerate((0, 20, 40, 60, 80), start=1):
            name = f"family-gate-{index}"
            self.add_task(name, offset, 100, family="telemetry-analysis")
            self.db.execute(
                "UPDATE annotations SET scope_change='none' WHERE session_id=?",
                (name,),
            )
            self._accepted_transport(name, offset + 2)
        self.db.execute(
            """UPDATE annotations SET task_family='audit'
                 WHERE session_id='family-gate-1' AND kind='finish'"""
        )
        self.reconcile()

        status = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertEqual(status["facts"]["semantic_conflicts"], 1)
        self.assertFalse(status["threshold_results"]["maximum_semantic_conflicts"])
        self.assertFalse(status["transport_verified"])

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

    def test_close_rejects_task_completed_after_authoritative_close_time(self) -> None:
        from hydra_codex.pilot import close_pilot, pilot_status, start_pilot

        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE - timedelta(seconds=1),
        )
        for index, offset in enumerate((0, 15, 30, 45, 75), start=1):
            name = f"close-window-{index}"
            self.add_task(name, offset, 100, family="telemetry-analysis")
            self.db.execute(
                "UPDATE annotations SET scope_change='none' WHERE session_id=?",
                (name,),
            )
            self._accepted_transport(name, offset + 2)
        self.reconcile()
        open_snapshot = pilot_status(
            self.store, PROJECT, run.pilot_id,
        ).as_dict()
        self.assertEqual(open_snapshot["facts"]["eligible_tasks"], 5)
        self.assertTrue(open_snapshot["transport_verified"])
        audit_path = self.root / "future-completion-audit.json"
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": "hydra.audit/v1",
                    "pilot_snapshot": open_snapshot,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError, "audit pilot snapshot is stale or inconsistent",
        ):
            close_pilot(
                self.store,
                project_id=PROJECT,
                pilot_id=run.pilot_id,
                audit_json=audit_path,
                decision="verified",
                now=BASE + timedelta(seconds=75),
            )

        stored = self.db.execute(
            "SELECT state,closed_at FROM pilot_runs WHERE pilot_id=?",
            (run.pilot_id,),
        ).fetchone()
        self.assertEqual(tuple(stored), ("open", None))
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM pilot_receipts WHERE pilot_id=?", (run.pilot_id,),
        ).fetchone())

    def test_close_rejects_boolean_integer_snapshot_substitution(self) -> None:
        from hydra_codex.pilot import close_pilot

        run, snapshot = self._verified_close_snapshot("typed-audit")
        snapshot["tasks"][0]["instrumented"] = 1
        audit_path = self.root / "typed-audit.json"
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": "hydra.audit/v1",
                    "pilot_snapshot": snapshot,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError, "audit pilot snapshot is stale or inconsistent",
        ):
            close_pilot(
                self.store,
                project_id=PROJECT,
                pilot_id=run.pilot_id,
                audit_json=audit_path,
                decision="verified",
                now=BASE + timedelta(seconds=100),
            )

    def test_close_rejects_duplicate_audit_object_keys(self) -> None:
        from hydra_codex.pilot import close_pilot

        run, snapshot = self._verified_close_snapshot("duplicate-audit")
        audit_path = self.root / "duplicate-audit.json"
        audit_path.write_text(
            (
                '{"schema_version":"hydra.audit/v0",'
                '"schema_version":"hydra.audit/v1","pilot_snapshot":'
                + json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
                + "}"
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "audit JSON is invalid"):
            close_pilot(
                self.store,
                project_id=PROJECT,
                pilot_id=run.pilot_id,
                audit_json=audit_path,
                decision="verified",
                now=BASE + timedelta(seconds=100),
            )

    def test_close_rejects_non_finite_audit_values(self) -> None:
        from hydra_codex.pilot import close_pilot

        run, snapshot = self._verified_close_snapshot("non-finite-audit")
        snapshot["facts"]["enrollment"] = float("nan")
        audit_path = self.root / "non-finite-audit.json"
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": "hydra.audit/v1",
                    "pilot_snapshot": snapshot,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "non-finite"):
            close_pilot(
                self.store,
                project_id=PROJECT,
                pilot_id=run.pilot_id,
                audit_json=audit_path,
                decision="verified",
                now=BASE + timedelta(seconds=100),
            )

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
