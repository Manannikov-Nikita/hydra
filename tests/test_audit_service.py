from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.audit_service import current_storage_health
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
        for private in (
            "private-session", "private-turn", str(self.project), str(self.database),
            "unattested", "capability", "project_id",
        ):
            self.assertNotIn(private, rendered)

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
        self.assertEqual(health.schema_version, 35)


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
