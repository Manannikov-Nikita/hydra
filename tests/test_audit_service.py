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
from hydra_codex.pilot import close_pilot, start_pilot
from hydra_codex.report_renderers import render_report_collection
from hydra_codex.services import LocalCommandServices
from hydra_codex.storage import HydraStore
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

    def test_one_shot_service_ingests_reconciles_validates_builds_and_renders(self) -> None:
        spool = self.root / "tmp" / "Hydra" / "spool"
        spool.mkdir(parents=True)
        pending = spool / "pending.json"
        pending.write_text('{"unattested":"must remain"}', encoding="utf-8")
        service = LocalCommandServices(environ=self.environ)

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
            snapshot = store.connection.execute(
                """SELECT audit_sha256,database_bytes,wal_bytes,rollout_sources,
                          rollout_events,codex_event_sources,codex_events,schema_version
                     FROM storage_audit_snapshots
                    WHERE project_id='hprj_audit_service'"""
            ).fetchone()
        finally:
            store.close()
        self.assertEqual(
            str(snapshot[0]), hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(tuple(snapshot[1:]), tuple(
            evidence[f"storage.{name}"]["value"] for name in (
                "database_bytes", "wal_bytes", "rollout_sources",
                "rollout_events", "codex_event_sources", "codex_events",
                "schema_version",
            )
        ))
        for private in (
            "private-session", "private-turn", str(self.project), str(self.database),
            "unattested", "capability", "project_id",
        ):
            self.assertNotIn(private, rendered)

    def test_failed_render_does_not_create_a_storage_baseline(self) -> None:
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
        LocalCommandServices(environ=self.environ).audit(
            self.run.pilot_id, "json", self.database, self.project,
        )
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
        self.assertEqual(health.schema_version, 37)

    def test_build_holds_one_nested_safe_transaction_across_status_and_reports(self) -> None:
        LocalCommandServices(environ=self.environ).audit(
            self.run.pilot_id, "json", self.database, self.project,
        )
        store = HydraStore(self.database)
        writer = sqlite3.connect(self.database, timeout=0)
        original = audit_service_module.list_reconciled_reports
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
                "list_reconciled_reports",
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


class LegacyReportByteCompatibilityTests(unittest.TestCase):
    def test_report_v3_and_report_list_v1_rendered_bytes_are_unchanged(self) -> None:
        reports = (
            public_report("compat-a", input_tokens=100, second=10),
            public_report("compat-b", input_tokens=200, second=20),
        )
        expected = {
            "json": "7f22fafd301b63f7f954f4cd6ef73cb053230d1f135d20fc19e9cb35dc205917",
            "markdown": "501fd8bdf5c563cd999732fcf04fb1d27cf5ac8f72968cba13737736c05d5c12",
            "html": "f82ba9a9348ad1351d7e35e457fd61c75581f8782f5761f88d0403e695ac33c0",
        }

        for output_format, digest in expected.items():
            with self.subTest(output_format=output_format):
                artifact = render_report_collection(reports, output_format).encode("utf-8")
                self.assertEqual(hashlib.sha256(artifact).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
