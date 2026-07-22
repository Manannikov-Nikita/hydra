from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.audit_renderers import render_audit_json
from hydra_codex.audit_service import build_pilot_audit
from hydra_codex.pilot import start_pilot
from hydra_codex.reconcile_engine import reconcile_project
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.storage import HydraStore
from tests.test_reporting import FIXTURES, materialize_historical


class HistoricalBattleAuditTests(unittest.TestCase):
    def test_historical_battle_fixture_has_zero_marker_coverage_without_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / ".hydra").mkdir(parents=True)
            project_id = "hprj_historical_audit"
            (project / ".hydra" / "project.toml").write_text(
                f'project_id = "{project_id}"\n', encoding="utf-8",
            )
            rollouts = root / "rollouts"
            materialize_historical(FIXTURES / "newer", rollouts, project)
            manifest = json.loads(
                (FIXTURES / "newer-manifest.json").read_text(encoding="utf-8"),
            )
            completed_at = datetime.fromisoformat(
                manifest["cutoff"].replace("Z", "+00:00"),
            )
            store = HydraStore(root / "hydra.sqlite3")
            self.addCleanup(store.close)
            ingest_rollouts(
                store,
                (rollouts,),
                project,
                project_id,
                hash_key=b"h" * 32,
            )
            reconcile_project(store, project_id, b"h" * 32)
            run = start_pilot(
                store,
                project_id=project_id,
                target=5,
                task_family="telemetry-analysis",
                now=completed_at - timedelta(days=1),
            )

            audit = build_pilot_audit(
                store,
                project_id=project_id,
                pilot_id=run.pilot_id,
            )
            payload = audit.as_dict()
            evidence = {item["fact"]: item for item in payload["evidence_appendix"]}
            task = payload["collection"]["tasks"][0]
            task_ref = task["task_ref"]

            self.assertEqual(payload["collection"]["count"], 1)
            self.assertEqual(
                evidence[f"tasks.{task_ref}.deduplicated_working_tokens"]["value"],
                manifest["expected"]["working_tokens"],
            )
            self.assertEqual(
                evidence[f"tasks.{task_ref}.semantic_coverage"]["value"], 0.0,
            )
            self.assertEqual(
                evidence[f"tasks.{task_ref}.semantic.marker_count"]["value"], 0,
            )
            self.assertEqual(task["semantic_markers"], [])
            rendered = render_audit_json(audit)
            self.assertNotIn(manifest["root"], rendered)
            self.assertNotIn(str(project), rendered)
            self.assertNotIn("session_id", rendered)


if __name__ == "__main__":
    unittest.main()
