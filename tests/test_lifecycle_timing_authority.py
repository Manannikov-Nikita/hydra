from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.reconcile_engine import list_reconciled_tasks, reconcile_project
from hydra_codex.reconcile_reports import list_reconciled_reports
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


PROJECT_ID = "lifecycle-timing-authority"
KEY = b"l" * 32


def _write(path: Path, rows: tuple[dict[str, object], ...]) -> Path:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


class LifecycleTimingAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT_ID}"\n', encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_rollout_completion_beats_later_estimated_app_receipt_in_both_orders(self) -> None:
        thread = "exact-boundary-thread"
        turn = "exact-boundary-turn"
        rollout = _write(self.base / "exact-boundary.rollout.jsonl", (
            {
                "timestamp": "2024-07-03T09:46:30Z",
                "type": "session_meta",
                "payload": {"id": thread, "cwd": str(self.project)},
            },
            {
                "timestamp": "2024-07-03T09:46:40Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn},
            },
            {
                "timestamp": "2024-07-03T09:46:45Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 0, "reasoning_output_tokens": 0,
                    },
                }},
            },
            {
                "timestamp": "2024-07-03T09:46:50Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn},
            },
            {
                "timestamp": "2024-07-03T09:46:55Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {
                        "input_tokens": 200, "cached_input_tokens": 0,
                        "output_tokens": 0, "reasoning_output_tokens": 0,
                    },
                }},
            },
        ))
        app = _write(self.base / "late-receipt.app.jsonl", ({
            "received_at": "2024-07-03T09:47:00Z",
            "message": {"method": "turn/completed", "params": {
                "threadId": thread,
                "turn": {"id": turn, "status": "completed"},
            }},
        },))
        expected_cutoff = datetime(
            2024, 7, 3, 9, 46, 50, tzinfo=timezone.utc,
        )
        root = Pseudonymizer(KEY).digest("identity", thread)

        observed: list[tuple[object, ...]] = []
        for index, app_first in enumerate((True, False), start=1):
            store = HydraStore(self.base / f"boundary-{index}.sqlite3")
            self.addCleanup(store.close)
            if app_first:
                ingest_codex_events(
                    store, (CodexEventSource(app, APP_SERVER_V2),),
                    self.project, PROJECT_ID, hash_key=KEY,
                )
                ingest_rollouts(
                    store, (rollout,), self.project, PROJECT_ID, hash_key=KEY,
                )
            else:
                ingest_rollouts(
                    store, (rollout,), self.project, PROJECT_ID, hash_key=KEY,
                )
                ingest_codex_events(
                    store, (CodexEventSource(app, APP_SERVER_V2),),
                    self.project, PROJECT_ID, hash_key=KEY,
                )

            direct = aggregate_stored_task_tree(
                store.connection, project_id=PROJECT_ID, root_id=root,
            )
            reconcile_project(store, PROJECT_ID, installation_key=b"r" * 32)
            task = list_reconciled_tasks(store, PROJECT_ID)[0]
            report = list_reconciled_reports(store, PROJECT_ID)[0]
            observed.append((
                direct.cutoff_at, direct.recorded.working.value,
                direct.recorded.provenance, direct.recorded.caveats,
                task.metrics.cutoff_at, task.metrics.recorded.working.value,
                task.metrics.recorded.provenance, task.metrics.recorded.caveats,
                report.deduplicated_tokens.working.value,
                report.deduplicated_tokens.working.provenance,
                report.deduplicated_tokens.working.caveats,
            ))

        for result in observed:
            self.assertEqual(result[0], expected_cutoff)
            self.assertEqual(result[1], 100)
            self.assertEqual(result[2], "exact")
            self.assertIn("lifecycle_timing_conflict:1", result[3])
            self.assertEqual(result[4], expected_cutoff)
            self.assertEqual(result[5], 100)
            self.assertEqual(result[6], "exact")
            self.assertIn("lifecycle_timing_conflict:1", result[7])
            self.assertEqual(result[8], 100)
            self.assertEqual(result[9], "derived")
            self.assertIn("lifecycle_timing_conflict:1", result[10])

    def test_receipt_only_app_start_does_not_become_exact_session_start(self) -> None:
        thread = "receipt-only-start-thread"
        turn = "receipt-only-start-turn"
        source = _write(self.base / "receipt-only-start.app.jsonl", (
            {
                "received_at": "2024-07-03T09:46:40.100000Z",
                "message": {"method": "turn/started", "params": {
                    "threadId": thread,
                    "turn": {"id": turn, "status": "inProgress"},
                }},
            },
            {
                "received_at": "2024-07-03T09:46:41Z",
                "message": {"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread, "turnId": turn,
                    "tokenUsage": {
                        "total": {
                            "inputTokens": 100, "cachedInputTokens": 40,
                            "outputTokens": 30, "reasoningOutputTokens": 5,
                            "totalTokens": 130,
                        },
                        "last": {
                            "inputTokens": 100, "cachedInputTokens": 40,
                            "outputTokens": 30, "reasoningOutputTokens": 5,
                            "totalTokens": 130,
                        },
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:42.100000Z",
                "message": {"method": "turn/completed", "params": {
                    "threadId": thread,
                    "turn": {
                        "id": turn, "completedAt": 1720000002,
                        "status": "completed",
                    },
                }},
            },
        ))
        store = HydraStore(self.base / "receipt-only-start.sqlite3")
        self.addCleanup(store.close)
        ingest_codex_events(
            store, (CodexEventSource(source, APP_SERVER_V2),),
            self.project, PROJECT_ID, hash_key=KEY,
        )
        root = Pseudonymizer(KEY).digest("identity", thread)

        self.assertIsNone(store.connection.execute(
            "SELECT started_at FROM rollout_sessions WHERE session_key=?", (root,),
        ).fetchone()[0])
        direct = aggregate_stored_task_tree(
            store.connection, project_id=PROJECT_ID, root_id=root,
        )
        self.assertIsNone(direct.root_wall_clock_ms.value)
        self.assertEqual(direct.root_wall_clock_ms.provenance, "estimated")
        self.assertIn("missing_root_session_start", direct.root_wall_clock_ms.caveats)
        self.assertIsNone(direct.agent_time_ms.value)
        self.assertEqual(direct.agent_time_ms.provenance, "estimated")
        self.assertIn("missing_session_start:1", direct.agent_time_ms.caveats)

        reconcile_project(store, PROJECT_ID, installation_key=b"r" * 32)
        task = list_reconciled_tasks(store, PROJECT_ID)[0]
        self.assertIsNone(task.metrics.root_wall_clock_ms.value)
        self.assertEqual(task.metrics.root_wall_clock_ms.provenance, "estimated")
        self.assertIsNone(task.metrics.agent_time_ms.value)
        self.assertEqual(task.metrics.agent_time_ms.provenance, "estimated")

    def test_later_turn_can_be_closed_by_its_own_estimated_app_completion(self) -> None:
        thread = "resumed-turn-thread"
        first_turn = "resumed-turn-one"
        second_turn = "resumed-turn-two"
        rollout = _write(self.base / "resumed-turn.rollout.jsonl", (
            {
                "timestamp": "2024-07-03T09:46:00Z",
                "type": "session_meta",
                "payload": {"id": thread, "cwd": str(self.project)},
            },
            {
                "timestamp": "2024-07-03T09:46:10Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": first_turn},
            },
            {
                "timestamp": "2024-07-03T09:46:20Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 0, "reasoning_output_tokens": 0,
                    },
                }},
            },
            {
                "timestamp": "2024-07-03T09:46:50Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": first_turn},
            },
            {
                "timestamp": "2024-07-03T09:46:55Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": second_turn},
            },
            {
                "timestamp": "2024-07-03T09:46:58Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {
                        "input_tokens": 150, "cached_input_tokens": 0,
                        "output_tokens": 0, "reasoning_output_tokens": 0,
                    },
                }},
            },
        ))
        app = _write(self.base / "resumed-turn.app.jsonl", ({
            "received_at": "2024-07-03T09:47:00Z",
            "message": {"method": "turn/completed", "params": {
                "threadId": thread,
                "turn": {"id": second_turn, "status": "completed"},
            }},
        },))
        expected_cutoff = datetime(
            2024, 7, 3, 9, 47, 0, tzinfo=timezone.utc,
        )
        root = Pseudonymizer(KEY).digest("identity", thread)

        observed: list[tuple[object, ...]] = []
        for index, app_first in enumerate((True, False), start=1):
            store = HydraStore(self.base / f"resumed-turn-{index}.sqlite3")
            self.addCleanup(store.close)
            if app_first:
                ingest_codex_events(
                    store, (CodexEventSource(app, APP_SERVER_V2),),
                    self.project, PROJECT_ID, hash_key=KEY,
                )
                ingest_rollouts(
                    store, (rollout,), self.project, PROJECT_ID, hash_key=KEY,
                )
            else:
                ingest_rollouts(
                    store, (rollout,), self.project, PROJECT_ID, hash_key=KEY,
                )
                ingest_codex_events(
                    store, (CodexEventSource(app, APP_SERVER_V2),),
                    self.project, PROJECT_ID, hash_key=KEY,
                )

            direct = aggregate_stored_task_tree(
                store.connection, project_id=PROJECT_ID, root_id=root,
            )
            summary = reconcile_project(
                store, PROJECT_ID, installation_key=b"r" * 32,
            )
            task = list_reconciled_tasks(store, PROJECT_ID)[0]
            observed.append((
                direct.cutoff_at, direct.recorded.working.value,
                direct.recorded.provenance, direct.recorded.caveats,
                summary.complete_count, task.status, task.metrics.cutoff_at,
                task.metrics.recorded.working.value,
                task.metrics.recorded.caveats,
            ))

        for result in observed:
            self.assertEqual(result[0], expected_cutoff)
            self.assertEqual(result[1], 150)
            self.assertEqual(result[2], "estimated")
            self.assertIn("estimated_lifecycle_cutoff_from_receipt", result[3])
            self.assertNotIn("lifecycle_timing_conflict:1", result[3])
            self.assertEqual((result[4], result[5]), (1, "complete"))
            self.assertEqual(result[6], expected_cutoff)
            self.assertEqual(result[7], 150)
            self.assertNotIn("lifecycle_timing_conflict:1", result[8])

    def test_distinct_receipt_only_start_after_exact_completion_stays_incomplete(self) -> None:
        thread = "open-resumed-turn-thread"
        first_turn = "open-resumed-turn-one"
        second_turn = "open-resumed-turn-two"
        rollout = _write(self.base / "open-resumed-turn.rollout.jsonl", (
            {
                "timestamp": "2024-07-03T09:46:00Z",
                "type": "session_meta",
                "payload": {"id": thread, "cwd": str(self.project)},
            },
            {
                "timestamp": "2024-07-03T09:46:10Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": first_turn},
            },
            {
                "timestamp": "2024-07-03T09:46:40Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 0, "reasoning_output_tokens": 0,
                    },
                }},
            },
            {
                "timestamp": "2024-07-03T09:46:50Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": first_turn},
            },
        ))
        app = _write(self.base / "open-resumed-turn.app.jsonl", ({
            "received_at": "2024-07-03T09:47:00Z",
            "message": {"method": "turn/started", "params": {
                "threadId": thread,
                "turn": {"id": second_turn, "status": "inProgress"},
            }},
        },))
        store = HydraStore(self.base / "open-resumed-turn.sqlite3")
        self.addCleanup(store.close)
        ingest_rollouts(
            store, (rollout,), self.project, PROJECT_ID, hash_key=KEY,
        )
        ingest_codex_events(
            store, (CodexEventSource(app, APP_SERVER_V2),),
            self.project, PROJECT_ID, hash_key=KEY,
        )
        root = Pseudonymizer(KEY).digest("identity", thread)

        with self.assertRaisesRegex(
            ValueError, "root task_complete observation is required",
        ):
            aggregate_stored_task_tree(
                store.connection, project_id=PROJECT_ID, root_id=root,
            )
        summary = reconcile_project(
            store, PROJECT_ID, installation_key=b"r" * 32,
        )
        task = list_reconciled_tasks(store, PROJECT_ID)[0]
        self.assertEqual((summary.complete_count, summary.incomplete_count), (0, 1))
        self.assertEqual(task.status, "incomplete")
        self.assertEqual(
            task.metrics.cutoff_at,
            datetime(2024, 7, 3, 9, 47, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
