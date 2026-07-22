from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hydra_codex.migrations_p16 import P16_MIGRATIONS
from hydra_codex.migrations_q17 import Q17_MIGRATIONS
from hydra_codex.pilot import close_pilot, pilot_status, start_pilot
from hydra_codex.reconcile_engine import list_reconciled_tasks
from hydra_codex.storage import HydraStore, StorageUnavailable
from tests.test_migrations_b2 import build_schema
from tests.test_report_semantic_trends import (
    BASE,
    PROJECT,
    StoredReportScenario,
    stamp,
)


def _migration_statement(prefix: str) -> str:
    for _version, statements in (*P16_MIGRATIONS, *Q17_MIGRATIONS):
        for statement in statements:
            if statement.lstrip().startswith(prefix):
                return statement
    raise AssertionError(f"missing migration statement: {prefix}")


def _insert_closed_run(
    connection: sqlite3.Connection,
    pilot_id: str,
    project_id: str = "project",
) -> None:
    connection.execute(
        """INSERT INTO pilot_runs(
               pilot_id,project_id,started_at,closed_at,target,
               task_family,thresholds_json,state)
           VALUES (?,?, '2026-07-22T00:00:00Z','2026-07-22T01:00:00Z',5,
                   'telemetry-analysis','{}','closed')""",
        (pilot_id, project_id),
    )


def _insert_receipt(
    connection: sqlite3.Connection,
    receipt_id: str,
    pilot_id: str,
    schema_version: int,
    decision: str = "rejected",
) -> None:
    connection.execute(
        """INSERT INTO pilot_receipts(
               receipt_id,pilot_id,created_at,decision,task_refs_json,
               reconciliation_version,schema_version,thresholds_json,
               observed_facts_json,snapshot_digest,audit_sha256)
           VALUES (?,?, '2026-07-22T01:00:00Z',?,'[]',1,?,'{}','{}',
                   'digest','audit')""",
        (receipt_id, pilot_id, decision, schema_version),
    )


class PilotControllerMigrationTests(unittest.TestCase):
    def test_hidden_rowid_replace_cannot_delete_an_immutable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            try:
                _insert_closed_run(store.connection, "pilot_original")
                _insert_closed_run(store.connection, "pilot_replacement")
                _insert_receipt(
                    store.connection,
                    "receipt_original",
                    "pilot_original",
                    store.schema_version(),
                )

                with self.assertRaises(sqlite3.OperationalError):
                    rowid = store.connection.execute(
                        "SELECT rowid FROM pilot_receipts"
                    ).fetchone()[0]
                    store.connection.execute(
                        """INSERT OR REPLACE INTO pilot_receipts(
                               rowid,receipt_id,pilot_id,created_at,decision,
                               task_refs_json,reconciliation_version,schema_version,
                               thresholds_json,observed_facts_json,snapshot_digest,
                               audit_sha256)
                           VALUES (?, 'receipt_replacement','pilot_replacement',
                                   '2026-07-22T02:00:00Z','verified','[]',1,?,
                                   '{}','{}','replacement-digest','replacement-audit')""",
                        (rowid, store.schema_version()),
                    )

                row = store.connection.execute(
                    "SELECT receipt_id,pilot_id,decision FROM pilot_receipts"
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    ("receipt_original", "pilot_original", "rejected"),
                )
            finally:
                store.close()

    def test_v33_receipt_migrates_without_rowid_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v33-receipt.sqlite3"
            build_schema(database, 33)
            connection = sqlite3.connect(database)
            _insert_closed_run(connection, "pilot_preserved")
            _insert_receipt(connection, "receipt_preserved", "pilot_preserved", 33)
            connection.commit()
            connection.close()

            store = HydraStore(database)
            try:
                self.assertEqual(store.schema_version(), 35)
                self.assertEqual(
                    store.connection.execute("PRAGMA foreign_keys").fetchone()[0],
                    1,
                )
                table_sql = str(store.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='pilot_receipts'"
                ).fetchone()[0])
                self.assertIn("WITHOUT ROWID", table_sql.upper())
                row = store.connection.execute(
                    """SELECT receipt_id,pilot_id,decision,snapshot_digest,audit_sha256
                         FROM pilot_receipts"""
                ).fetchone()
                self.assertEqual(tuple(row), (
                    "receipt_preserved", "pilot_preserved", "rejected",
                    "digest", "audit",
                ))
                trigger_names = {
                    str(row[0]) for row in store.connection.execute(
                        """SELECT name FROM sqlite_master
                             WHERE type='trigger' AND tbl_name IN (
                                 'pilot_receipts','pilot_runs'
                             )"""
                    )
                }
                self.assertTrue({
                    "pilot_receipts_immutable_insert",
                    "pilot_receipts_immutable_update",
                    "pilot_receipts_immutable_delete",
                    "pilot_runs_immutable_after_receipt_insert",
                    "pilot_runs_immutable_after_receipt_update",
                    "pilot_runs_immutable_after_receipt_delete",
                }.issubset(trigger_names))
            finally:
                store.close()

    def test_rowid_receipt_table_fails_exact_startup_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "rowid-drift.sqlite3"
            store = HydraStore(database)
            latest = store.schema_version()
            store.close()
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TABLE pilot_receipts")
            connection.execute(_migration_statement("CREATE TABLE pilot_receipts"))
            for prefix in (
                "CREATE TRIGGER pilot_receipts_immutable_update",
                "CREATE TRIGGER pilot_receipts_immutable_delete",
                "CREATE TRIGGER pilot_receipts_immutable_insert",
            ):
                connection.execute(_migration_statement(prefix))
            connection.execute(f"PRAGMA user_version={latest}")
            connection.commit()
            connection.close()

            with self.assertRaises(StorageUnavailable):
                HydraStore(database)

    def test_receipt_requires_the_matching_closed_run_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "receipt-run-binding.sqlite3")
            try:
                store.connection.execute(
                    """INSERT INTO pilot_runs(
                           pilot_id,project_id,started_at,closed_at,target,
                           task_family,thresholds_json,state)
                       VALUES ('pilot_open','project','2026-07-22T00:00:00Z',
                               NULL,5,'telemetry-analysis','{}','open')"""
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    _insert_receipt(
                        store.connection,
                        "receipt_open",
                        "pilot_open",
                        store.schema_version(),
                    )

                _insert_closed_run(store.connection, "pilot_mismatch")
                store.connection.execute(
                    """UPDATE pilot_runs SET closed_at='2026-07-22T00:59:59Z'
                         WHERE pilot_id='pilot_mismatch'"""
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    _insert_receipt(
                        store.connection,
                        "receipt_mismatch",
                        "pilot_mismatch",
                        store.schema_version(),
                    )
            finally:
                store.close()

    def test_pilot_run_guard_trigger_drift_fails_startup_validation(self) -> None:
        for operation in ("insert", "update", "delete"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                database = Path(temporary) / "run-trigger-drift.sqlite3"
                store = HydraStore(database)
                store.close()
                trigger = f"pilot_runs_immutable_after_receipt_{operation}"
                connection = sqlite3.connect(database)
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                connection.execute(
                    f"""CREATE TRIGGER {trigger}
                            BEFORE {operation.upper()} ON pilot_runs
                            BEGIN SELECT 1; END"""
                )
                connection.commit()
                connection.close()

                with self.assertRaises(StorageUnavailable):
                    HydraStore(database)


class PilotControllerScenario(StoredReportScenario):
    def _accepted_transport(
        self,
        session: str,
        second: int,
        latency_ms: int = 1000,
    ) -> None:
        for index in range(2):
            staged_second = second + index
            self.db.execute(
                """INSERT INTO annotation_transport_events(
                       transport_key,project_id,session_key,turn_key,
                       request_digest,disposition,diagnostic_category,staged_at,
                       staged_at_ns,staged_order,received_at,latency_ms,provenance)
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

    def _verified_snapshot(self, prefix: str):
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

    def test_completion_one_hundred_nanoseconds_after_start_is_eligible(self) -> None:
        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE + timedelta(seconds=9),
        )
        self.add_task("exact-enrollment", 0, 100, family="telemetry-analysis")
        self.db.execute(
            """UPDATE rollout_events
                  SET observed_at='2026-07-21T00:00:09.0000001Z'
                WHERE event_key='complete-exact-enrollment'"""
        )
        self.reconcile()

        status = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertEqual(status["facts"]["eligible_tasks"], 1)
        self.assertEqual(
            status["tasks"][0]["completed_at"],
            "2026-07-21T00:00:09Z",
        )

    def test_annotations_one_hundred_nanoseconds_after_completion_are_excluded(self) -> None:
        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE - timedelta(seconds=1),
        )
        self.add_task("exact-annotations", 0, 100, family="telemetry-analysis")
        self.db.execute(
            """UPDATE annotations
                  SET observed_at='2026-07-21T00:00:09.0000001Z'
                WHERE session_id='exact-annotations' AND sequence IN (1,2)"""
        )
        self.reconcile()

        task = pilot_status(
            self.store, PROJECT, run.pilot_id,
        ).as_dict()["tasks"][0]

        self.assertTrue(task["finish_missing"])
        self.assertIsNone(task["task_family"])
        self.assertEqual(task["scope_change"], "none")

    def test_token_one_hundred_nanoseconds_after_completion_is_excluded(self) -> None:
        self.add_task(
            "exact-token-cutoff", 0, 100, family="telemetry-analysis",
        )
        self.db.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                   completeness,observed_at)
               VALUES ('source-exact-token-cutoff',2,'exact-token-cutoff',?,0,
                       1000,0,0,0,0,'complete',
                       '2026-07-21T00:00:09.0000001Z')""",
            (PROJECT,),
        )

        self.reconcile()
        task = list_reconciled_tasks(self.store, PROJECT)[0]

        self.assertEqual(task.metrics.unique.working_tokens, 100)
        self.assertEqual(task.semantic.classified_working, 100)
        self.assertEqual(task.semantic.coverage.value, 1.0)

    def test_stable_run_metadata_changes_snapshot_digest_before_receipt(self) -> None:
        run, before = self._verified_snapshot("metadata-binding")
        self.db.execute(
            """UPDATE pilot_runs
                  SET started_at=?,target=6,task_family='quiz'
                WHERE pilot_id=?""",
            (stamp(-2), run.pilot_id),
        )
        self.db.commit()

        after = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertNotEqual(after["snapshot_digest"], before["snapshot_digest"])
        self.assertEqual(after["pilot"]["target"], 6)
        self.assertEqual(after["pilot"]["task_family"], "quiz")

    def test_verified_run_metadata_and_lifecycle_are_immutable(self) -> None:
        run, snapshot = self._verified_snapshot("run-guard")
        audit_path = self.root / "run-guard-audit.json"
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
        close_pilot(
            self.store,
            project_id=PROJECT,
            pilot_id=run.pilot_id,
            audit_json=audit_path,
            decision="verified",
            now=BASE + timedelta(seconds=100),
        )

        mutations = (
            ("UPDATE pilot_runs SET project_id=? WHERE pilot_id=?", ("changed", run.pilot_id)),
            ("UPDATE pilot_runs SET started_at=? WHERE pilot_id=?", (stamp(-10), run.pilot_id)),
            ("UPDATE pilot_runs SET target=7 WHERE pilot_id=?", (run.pilot_id,)),
            ("UPDATE pilot_runs SET task_family='quiz' WHERE pilot_id=?", (run.pilot_id,)),
            ("UPDATE pilot_runs SET thresholds_json='{}' WHERE pilot_id=?", (run.pilot_id,)),
            (
                "UPDATE pilot_runs SET state='open',closed_at=NULL WHERE pilot_id=?",
                (run.pilot_id,),
            ),
            ("DELETE FROM pilot_runs WHERE pilot_id=?", (run.pilot_id,)),
        )
        for sql, parameters in mutations:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(sql, parameters)

        self.db.rollback()
        foreign_key_free = sqlite3.connect(self.database)
        try:
            foreign_key_free.execute("PRAGMA foreign_keys=OFF")
            rowid = foreign_key_free.execute(
                "SELECT rowid FROM pilot_runs WHERE pilot_id=?", (run.pilot_id,),
            ).fetchone()[0]
            replacements = (
                (run.pilot_id, None),
                ("replacement-pilot", rowid),
            )
            for replacement_id, replacement_rowid in replacements:
                with self.subTest(replacement_id=replacement_id):
                    columns = "" if replacement_rowid is None else "rowid,"
                    values = "" if replacement_rowid is None else "?,"
                    parameters = (
                        *((replacement_rowid,) if replacement_rowid is not None else ()),
                        replacement_id,
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        foreign_key_free.execute(
                            f"""INSERT OR REPLACE INTO pilot_runs(
                                   {columns}pilot_id,project_id,started_at,closed_at,
                                   target,task_family,thresholds_json,state)
                               VALUES ({values}?,'changed',?,NULL,7,'quiz','{{}}','open')""",
                            (*parameters, stamp(-10)),
                        )
            with self.assertRaises(sqlite3.IntegrityError):
                foreign_key_free.execute(
                    "DELETE FROM pilot_runs WHERE pilot_id=?", (run.pilot_id,),
                )
        finally:
            foreign_key_free.close()

    def test_close_before_exact_run_start_is_rejected(self) -> None:
        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=BASE,
        )
        self.db.execute(
            "UPDATE pilot_runs SET started_at=? WHERE pilot_id=?",
            ("2026-07-21T00:00:00.0000001Z", run.pilot_id),
        )
        self.db.commit()
        self.reconcile()
        snapshot = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()
        audit_path = self.root / "exact-start-audit.json"
        audit_path.write_text(
            json.dumps(
                {"schema_version": "hydra.audit/v1", "pilot_snapshot": snapshot},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "predates its start"):
            close_pilot(
                self.store,
                project_id=PROJECT,
                pilot_id=run.pilot_id,
                audit_json=audit_path,
                decision="rejected",
                now=BASE,
            )

        state, closed_at = self.db.execute(
            "SELECT state,closed_at FROM pilot_runs WHERE pilot_id=?",
            (run.pilot_id,),
        ).fetchone()
        self.assertEqual((state, closed_at), ("open", None))

    def test_update_or_replace_cannot_target_a_receipted_run(self) -> None:
        run, snapshot = self._verified_snapshot("run-update-replace")
        audit_path = self.root / "run-update-replace-audit.json"
        audit_path.write_text(
            json.dumps(
                {"schema_version": "hydra.audit/v1", "pilot_snapshot": snapshot},
                sort_keys=True,
                separators=(",", ":"),
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

        unguarded = sqlite3.connect(self.database)
        try:
            unguarded.execute("PRAGMA foreign_keys=OFF")
            unguarded.execute(
                """INSERT INTO pilot_runs(
                       pilot_id,project_id,started_at,closed_at,target,
                       task_family,thresholds_json,state)
                   VALUES ('pilot_unreceipted','project-unreceipted',?,?,5,
                           'telemetry-analysis','{}','closed')""",
                (stamp(0), stamp(50)),
            )
            unguarded.commit()
            protected_rowid = unguarded.execute(
                "SELECT rowid FROM pilot_runs WHERE pilot_id=?", (run.pilot_id,),
            ).fetchone()[0]
            probes = (
                (
                    """UPDATE OR REPLACE pilot_runs SET pilot_id=?
                         WHERE pilot_id='pilot_unreceipted'""",
                    (run.pilot_id,),
                ),
                (
                    """UPDATE OR REPLACE pilot_runs SET rowid=?
                         WHERE pilot_id='pilot_unreceipted'""",
                    (protected_rowid,),
                ),
            )
            for sql, parameters in probes:
                with self.subTest(sql=sql):
                    unguarded.execute("SAVEPOINT replacement_probe")
                    try:
                        with self.assertRaises(sqlite3.IntegrityError):
                            unguarded.execute(sql, parameters)
                    finally:
                        unguarded.execute("ROLLBACK TO replacement_probe")
                        unguarded.execute("RELEASE replacement_probe")

            self.assertEqual(
                unguarded.execute(
                    "SELECT project_id,target FROM pilot_runs WHERE pilot_id=?",
                    (run.pilot_id,),
                ).fetchone(),
                (PROJECT, 5),
            )
            self.assertEqual(
                unguarded.execute(
                    "SELECT COUNT(*) FROM pilot_runs WHERE pilot_id='pilot_unreceipted'"
                ).fetchone()[0],
                1,
            )
        finally:
            unguarded.close()

    def test_status_holds_one_immediate_transaction_across_all_reads(self) -> None:
        run, _snapshot = self._verified_snapshot("atomic-status")
        import hydra_codex.pilot as pilot_module

        original = pilot_module._task_sessions
        second = sqlite3.connect(self.database, timeout=0)
        interleaving = {"blocked": False}

        def attempt_interleaving(store, project_id):
            try:
                second.execute(
                    "UPDATE pilot_runs SET target=6 WHERE pilot_id=?",
                    (run.pilot_id,),
                )
                second.commit()
            except sqlite3.OperationalError as error:
                second.rollback()
                interleaving["blocked"] = "locked" in str(error).lower()
            return original(store, project_id)

        try:
            with patch.object(
                pilot_module,
                "_task_sessions",
                side_effect=attempt_interleaving,
            ):
                pilot_module.pilot_status(self.store, PROJECT, run.pilot_id)
        finally:
            second.close()

        self.assertTrue(interleaving["blocked"])
        self.assertEqual(self.db.execute(
            "SELECT target FROM pilot_runs WHERE pilot_id=?", (run.pilot_id,),
        ).fetchone()[0], 5)

    def test_project_event_issues_block_once_with_and_without_task_attribution(self) -> None:
        run, _snapshot = self._verified_snapshot("project-schema")
        self.db.execute(
            """INSERT INTO codex_event_sources(
                   source_digest,project_id,schema_version,source_format,
                   line_count,byte_count)
               VALUES ('pilot-project-issues',?,'app_server/v2','app_server',2,2)""",
            (PROJECT,),
        )
        self.db.execute(
            """INSERT INTO codex_events(
                   source_digest,source_ordinal,event_key,project_id,
                   source_format,schema_version,event_type,observed_at,
                   observed_at_ns,session_key,turn_key,duration_ms,status,provenance)
               VALUES ('pilot-project-issues',1,'attributed-issue',?,
                       'app_server','app_server/v2','turn/completed',?,1,
                       'project-schema-1','turn-project-schema-1',1,
                       'completed','exact')""",
            (PROJECT, stamp(3)),
        )
        self.db.executemany(
            """INSERT INTO codex_event_issues(
                   source_digest,source_ordinal,event_key,issue_code)
               VALUES ('pilot-project-issues',?,?, 'schema_drift')""",
            ((1, "attributed-issue"), (2, "unattributed-issue")),
        )
        self.reconcile()

        status = pilot_status(self.store, PROJECT, run.pilot_id).as_dict()

        self.assertEqual(status["tasks"][0]["schema_diagnostics"], 1)
        self.assertEqual(status["facts"]["schema_diagnostics"], 2)
        self.assertFalse(status["threshold_results"]["maximum_schema_diagnostics"])
        self.assertFalse(status["transport_verified"])

    def test_pilot_id_is_not_a_digest_of_private_project_metadata(self) -> None:
        now = BASE + timedelta(seconds=123)
        run = start_pilot(
            self.store,
            project_id=PROJECT,
            target=5,
            task_family="telemetry-analysis",
            now=now,
        )
        deterministic_preimage = json.dumps(
            {
                "project_id": PROJECT,
                "started_at": now.isoformat().replace("+00:00", "Z"),
                "target": 5,
                "task_family": "telemetry-analysis",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertNotEqual(
            run.pilot_id,
            "hpilot_v1_" + hashlib.sha256(deterministic_preimage).hexdigest()[:32],
        )


if __name__ == "__main__":
    unittest.main()
