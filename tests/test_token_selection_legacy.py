from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


KEY = b"legacy-app-cutoff-fixture-key-01"
PROJECT = "legacy-app-cutoff"


class LegacyAppCutoffTests(unittest.TestCase):
    def test_same_source_ordinal_preserves_pre_completion_token_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                f'project_id = "{PROJECT}"\n', encoding="utf-8",
            )
            source = root / "legacy-app.jsonl"

            def usage(value: int) -> dict[str, object]:
                total = {
                    "inputTokens": value,
                    "cachedInputTokens": value // 10,
                    "outputTokens": value // 5,
                    "reasoningOutputTokens": value // 20,
                    "totalTokens": value + value // 5,
                }
                return {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "legacy-thread", "turnId": "legacy-turn",
                        "tokenUsage": {"total": total, "last": total},
                    },
                }

            rows = (
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "legacy-thread",
                        "turn": {
                            "id": "legacy-turn", "startedAt": 1720000000,
                            "status": "inProgress",
                        },
                    },
                },
                usage(100),
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "legacy-thread",
                        "turn": {
                            "id": "legacy-turn", "completedAt": 1720000002,
                            "status": "completed",
                        },
                    },
                },
                usage(200),
            )
            source.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            store = HydraStore(root / "hydra.sqlite3")
            self.addCleanup(store.close)

            ingest_codex_events(
                store, (CodexEventSource(source, APP_SERVER_V2),),
                project, PROJECT, hash_key=KEY,
            )

            session = Pseudonymizer(KEY).digest("identity", "legacy-thread")
            metrics = aggregate_stored_task_tree(
                store.connection, project_id=PROJECT, root_id=session,
            )
            self.assertEqual(metrics.recorded.working.value, 110)
            self.assertEqual(metrics.recorded.working.known_lower_bound, 110)
            self.assertIn(
                "post_cutoff_timestamp_missing_token:1",
                metrics.recorded.working.caveats,
            )


if __name__ == "__main__":
    unittest.main()
