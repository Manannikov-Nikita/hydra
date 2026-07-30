from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import hydra_codex.audit_service as audit_service_module
from hydra_codex.audit_service import build_pilot_audit, current_storage_health
from hydra_codex.pilot import close_pilot, pilot_status, start_pilot
from hydra_codex.reconcile_engine import ReconciliationStale, reconcile_project
from hydra_codex.report_renderers import render_report_collection
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.services import LocalCommandServices
from hydra_codex.storage import MIGRATIONS, HydraStore
from tests.test_audit_builder import public_report


class OneShotAuditServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_audit_service"\n', encoding="utf-8",
        )
        self.database = self.root / "private" / "hydra.sqlite3"
        self.database.parent.mkdir()
        self.key = self.root / "private" / "rollout-hmac.key"
        self.environ = {
            "HOME": str(self.home),
            "TMPDIR": str(self.root / "tmp"),
            "HYDRA_DATABASE_PATH": str(self.database),
            "HYDRA_INSTALLATION_KEY_PATH": str(self.key),
        }
        source = self.home / ".codex" / "sessions" / "private-session.jsonl"
        source.parent.mkdir(parents=True)
        records = (
            {
                "timestamp": "2026-07-21T00:00:10Z",
                "type": "session_meta",
                "payload": {"id": "private-session", "cwd": str(self.project)},
            },
            {
                "timestamp": "2026-07-21T00:00:11Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "private-turn"},
            },
            {
                "timestamp": "2026-07-21T00:00:12Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 10,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "cache_write_input_tokens": 0,
                        "total_tokens": 120,
                    }},
                },
            },
            {
                "timestamp": "2026-07-21T00:00:13Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "private-turn"},
            },
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )
        store = HydraStore(self.database)
        try:
            self.run = start_pilot(
                store,
                project_id="hprj_audit_service",
                target=5,
                task_family="telemetry-analysis",
                now=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
        finally:
            store.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _materialize_pilot(self) -> None:
        store = HydraStore(self.database)
        try:
            ingest_rollouts(
                store,
                audit_service_module._default_rollout_roots(self.environ),
                self.project,
                "hprj_audit_service",
                hash_key=b"h" * 32,
            )
            reconcile_project(
                store,
                "hprj_audit_service",
                b"h" * 32,
            )
            pilot_status(
                store,
                "hprj_audit_service",
                self.run.pilot_id,
            )
            # Enrollment is itself a trusted source fact. Publish a fence that
            # includes the newly materialized pilot task before testing reads.
            reconcile_project(
                store,
                "hprj_audit_service",
                b"h" * 32,
            )
        finally:
            store.close()

    def test_audit_service_reads_only_materialized_state_and_renders(self) -> None:
        spool = self.root / "tmp" / "Hydra" / "spool"
        spool.mkdir(parents=True)
        pending = spool / "pending.json"
        pending.write_text('{"unattested":"must remain"}', encoding="utf-8")
        self._materialize_pilot()
        store = HydraStore(self.database)
        try:
            before_runs = store.connection.execute(
                "SELECT COUNT(*) FROM reconciliation_runs"
            ).fetchone()[0]
            before_sources = store.connection.execute(
                "SELECT COUNT(*) FROM rollout_sources"
            ).fetchone()[0]
        finally:
            store.close()
        service = LocalCommandServices(environ=self.environ)

        with patch(
            "hydra_codex.reconcile_engine._assemble_project",
            side_effect=AssertionError("audit report must not reassemble source facts"),
        ), patch.object(
            HydraStore,
            "_validate_schema",
            side_effect=AssertionError(
                "materialized pilot read repeated the full database audit",
            ),
        ):
            rendered = service.audit(
                self.run.pilot_id,
                "json",
                self.database,
                self.project,
            )
        payload = json.loads(rendered)

        self.assertEqual(payload["schema_version"], "hydra.audit/v1")
        self.assertEqual(payload["pilot_snapshot"]["pilot"]["pilot_id"], self.run.pilot_id)
        self.assertEqual(payload["collection"]["count"], 1)
        self.assertEqual(
            payload["collection"]["tasks"][0]["semantic_markers"], [],
        )
        evidence = {item["fact"]: item for item in payload["evidence_appendix"]}
        self.assertEqual(evidence["pilot.facts.aggregate_coverage"]["value"], 0.0)
        self.assertEqual(evidence["storage.rollout_sources"]["value"], 1)
        self.assertEqual(evidence["storage.rollout_events"]["value"], 4)
        self.assertIsNone(evidence["transport.pending_annotation_drain"]["value"])
        self.assertTrue(pending.exists(), "bare audit service must not drain unattested spool data")
        store = HydraStore(self.database)
        try:
            after_runs = store.connection.execute(
                "SELECT COUNT(*) FROM reconciliation_runs"
            ).fetchone()[0]
            after_sources = store.connection.execute(
                "SELECT COUNT(*) FROM rollout_sources"
            ).fetchone()[0]
            audit_snapshots = store.connection.execute(
                "SELECT COUNT(*) FROM storage_audit_snapshots"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual((after_runs, after_sources), (before_runs, before_sources))
        self.assertEqual(audit_snapshots, 0)
        for private in (
            "private-session", "private-turn", str(self.project), str(self.database),
            "unattested", "capability", "project_id",
        ):
            self.assertNotIn(private, rendered)

    def test_materialized_audit_requires_current_project_source_fence(self) -> None:
        self._materialize_pilot()
        store = HydraStore(self.database)
        try:
            store.connection.execute(
                "UPDATE pilot_runs SET target=6 WHERE pilot_id=?",
                (self.run.pilot_id,),
            )
            store.connection.commit()
        finally:
            store.close()

        with self.assertRaises(ReconciliationStale) as raised:
            LocalCommandServices(environ=self.environ).audit(
                self.run.pilot_id,
                "json",
                self.database,
                self.project,
            )
        self.assertEqual(str(raised.exception), "reconcile_required")

    def test_failed_render_does_not_create_a_storage_baseline(self) -> None:
        self._materialize_pilot()
        with self.assertRaises(ValueError):
            LocalCommandServices(environ=self.environ).audit(
                self.run.pilot_id, "unsupported", self.database, self.project,
            )
        store = HydraStore(self.database)
        try:
            count = store.connection.execute(
                "SELECT COUNT(*) FROM storage_audit_snapshots"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(count, 0)

    def test_canonical_audit_json_is_accepted_directly_by_rejected_close(self) -> None:
        self._materialize_pilot()
        rendered = LocalCommandServices(environ=self.environ).audit(
            self.run.pilot_id, "json", self.database, self.project,
        )
        audit_path = self.root / "audit.json"
        audit_path.write_text(rendered, encoding="utf-8")
        store = HydraStore(self.database)
        try:
            receipt = close_pilot(
                store,
                project_id="hprj_audit_service",
                pilot_id=self.run.pilot_id,
                audit_json=audit_path,
                decision="rejected",
                now=datetime(2026, 7, 22, tzinfo=timezone.utc),
            )
        finally:
            store.close()

        self.assertEqual(
            receipt.audit_sha256,
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )

    def test_current_storage_health_is_read_only_and_project_scoped(self) -> None:
        self._materialize_pilot()
        store = HydraStore(self.database)
        try:
            before = store.connection.total_changes
            health = current_storage_health(store, "hprj_audit_service")
            after = store.connection.total_changes
        finally:
            store.close()

        self.assertEqual(after, before)
        self.assertGreater(health.database_bytes, 0)
        self.assertGreaterEqual(health.wal_bytes, 0)
        self.assertEqual((health.rollout_sources, health.rollout_events), (1, 4))
        self.assertEqual((health.codex_event_sources, health.codex_events), (0, 0))
        self.assertEqual(health.schema_version, MIGRATIONS[-1][0])

    def test_build_holds_one_nested_safe_transaction_across_status_and_reports(self) -> None:
        self._materialize_pilot()
        store = HydraStore(self.database)
        writer = sqlite3.connect(self.database, timeout=0)
        original = (
            audit_service_module._list_reconciled_reports_after_refresh
        )
        interleaving = {"blocked": False}

        def attempt_interleaving(active_store, project_id):
            try:
                writer.execute(
                    "UPDATE pilot_runs SET target=6 WHERE pilot_id=?",
                    (self.run.pilot_id,),
                )
                writer.commit()
            except sqlite3.OperationalError as error:
                writer.rollback()
                interleaving["blocked"] = "locked" in str(error).lower()
            return original(active_store, project_id)

        try:
            with patch.object(
                audit_service_module,
                "_list_reconciled_reports_after_refresh",
                side_effect=attempt_interleaving,
            ):
                build_pilot_audit(
                    store,
                    project_id="hprj_audit_service",
                    pilot_id=self.run.pilot_id,
                )
        finally:
            writer.close()
            store.close()

        self.assertTrue(interleaving["blocked"])

    def test_dashboard_audit_build_does_not_refresh_pilot_enrollment(self) -> None:
        self._materialize_pilot()
        store = HydraStore(self.database)
        try:
            before = store.connection.total_changes
            audit = build_pilot_audit(
                store,
                project_id="hprj_audit_service",
                pilot_id=self.run.pilot_id,
                refresh_enrollment=False,
            )
            after = store.connection.total_changes
        finally:
            store.close()

        self.assertEqual(after, before)
        self.assertEqual(audit.schema_version, "hydra.audit/v1")

    def test_materialized_audit_uses_query_only_read_transaction(self) -> None:
        self._materialize_pilot()
        store = HydraStore(self.database)
        statements: list[str] = []
        store.connection.set_trace_callback(statements.append)
        try:
            with patch.object(
                store,
                "rollout_transaction",
                side_effect=AssertionError(
                    "materialized audit must not acquire a writer transaction",
                ),
            ):
                audit_service_module._build_pilot_audit_with_health(
                    store,
                    project_id="hprj_audit_service",
                    pilot_id=self.run.pilot_id,
                    refresh_enrollment=False,
                    materialized_only=True,
                )
            query_only_after = int(
                store.connection.execute("PRAGMA query_only").fetchone()[0]
            )
        finally:
            store.connection.set_trace_callback(None)
            store.close()

        normalized = tuple(statement.strip().upper() for statement in statements)
        self.assertIn("BEGIN", normalized)
        self.assertIn("PRAGMA QUERY_ONLY=ON", normalized)
        self.assertNotIn("BEGIN IMMEDIATE", normalized)
        self.assertEqual(query_only_after, 0)

    def test_materialized_task_reader_distinguishes_missing_from_corrupt_state(self) -> None:
        self._materialize_pilot()
        store = HydraStore(self.database)
        try:
            task_ref = str(store.connection.execute(
                """SELECT task_ref FROM materialized_report_snapshots
                     WHERE project_id='hprj_audit_service'""",
            ).fetchone()[0])
            reports = audit_service_module.read_materialized_task_reports(
                store, "hprj_audit_service", (task_ref,),
            )
            with self.assertRaisesRegex(
                KeyError, "unknown materialized task reference",
            ):
                audit_service_module.read_materialized_task_reports(
                    store,
                    "hprj_audit_service",
                    ("task_000000000000",),
                )
            with self.assertRaisesRegex(ValueError, "must be unique"):
                audit_service_module.read_materialized_task_reports(
                    store, "hprj_audit_service", (task_ref, task_ref),
                )
        finally:
            store.close()

        self.assertEqual(tuple(report.task_ref for report in reports), (task_ref,))


class ReportV4ByteStabilityTests(unittest.TestCase):
    def test_report_v4_and_report_list_v2_rendered_bytes_are_stable(self) -> None:
        reports = (
            public_report("compat-a", input_tokens=100, second=10),
            public_report("compat-b", input_tokens=200, second=20),
        )
        expected = {
            "json": "b6b9f2b24d1b697b1d580871115f9c743ff04f136a1aec9f2426fc4a6aede14e",
            "markdown": "243795a4ca62a37efb46696dc116d2e275c26bb34b57ce95d4d37d07f2e1f172",
            "html": "c2f7b058be5f8f2830998ab295b722fa359739b9bfec024522880142b554c359",
        }

        for output_format, digest in expected.items():
            with self.subTest(output_format=output_format):
                artifact = render_report_collection(reports, output_format).encode("utf-8")
                self.assertEqual(hashlib.sha256(artifact).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
