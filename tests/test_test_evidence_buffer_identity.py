from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore


KEY = b"buffer-identity-test-key-0000000"
PROJECT_ID = "buffer-identity-project"
_ABSENT = object()


class TestEvidenceBufferIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def event(
        self, thread: str, *, status: str, command: object = _ABSENT,
        exit_code: object = _ABSENT, suffix: str = "call",
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "id": "shared-raw-item-id", "type": "commandExecution",
            "status": status,
        }
        if command is not _ABSENT:
            item["command"] = command
        if exit_code is not _ABSENT:
            item["exitCode"] = exit_code
        return {
            "received_at": f"2024-07-03T09:46:4{suffix}Z",
            "message": {
                "method": "item/started" if status == "inProgress" else "item/completed",
                "params": {
                    "threadId": thread, "turnId": f"{thread}-turn", "item": item,
                },
            },
        }

    def ingest(
        self, database_name: str, events: tuple[dict[str, object], ...],
    ) -> HydraStore:
        source_path = self.root / f"{database_name}.jsonl"
        source_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        store = HydraStore(self.root / f"{database_name}.sqlite3")
        self.addCleanup(store.close)
        ingest_codex_events(
            store, (CodexEventSource(source_path, APP_SERVER_V2),),
            self.project, PROJECT_ID, hash_key=KEY,
        )
        return store

    def session_key(self, raw: str) -> str:
        return Pseudonymizer(KEY).digest("identity", raw)

    def test_cross_thread_same_item_id_keeps_completion_and_started_intent_separate(self) -> None:
        completed = self.event(
            "completed-thread", status="completed",
            command="pytest tests/test_completed.py", exit_code=0, suffix="1",
        )
        started = self.event(
            "started-thread", status="inProgress",
            command="pytest tests/test_started.py", suffix="2",
        )

        for order, events in enumerate(((completed, started), (started, completed)), start=1):
            with self.subTest(order=order):
                store = self.ingest(f"cross-thread-{order}", events)
                candidates = [
                    tuple(row) for row in store.connection.execute(
                        """SELECT session_key,completeness
                             FROM test_evidence_candidates
                            WHERE candidate_kind='evidence'
                            ORDER BY session_key"""
                    )
                ]
                runs = [
                    tuple(row) for row in store.connection.execute(
                        """SELECT session_key,outcome,exit_status
                             FROM rollout_test_runs"""
                    )
                ]

                self.assertEqual(candidates, sorted([
                    (self.session_key("completed-thread"), "complete"),
                    (self.session_key("started-thread"), "intent_only"),
                ]))
                self.assertEqual(
                    runs,
                    [(self.session_key("completed-thread"), "success", 0)],
                )

    def test_same_session_reused_call_preserves_both_hashes_and_fails_closed(self) -> None:
        first = self.event(
            "reused-thread", status="completed",
            command="pytest tests/test_first.py", exit_code=0, suffix="1",
        )
        second = self.event(
            "reused-thread", status="completed",
            command="pytest tests/test_second.py", exit_code=0, suffix="2",
        )

        for order, events in enumerate(((first, second), (second, first)), start=1):
            with self.subTest(order=order):
                store = self.ingest(f"reused-command-{order}", events)
                self.assertEqual(store.count("rollout_test_runs"), 0)
                self.assertEqual(
                    tuple(store.connection.execute(
                        """SELECT COUNT(*),COUNT(DISTINCT command_hash)
                             FROM test_evidence_candidates
                            WHERE candidate_kind='evidence'"""
                    ).fetchone()),
                    (2, 2),
                )

    def test_same_session_completed_result_never_transfers_to_new_started_command(self) -> None:
        completed = self.event(
            "generation-thread", status="completed",
            command="pytest tests/test_generation_a.py", exit_code=0, suffix="1",
        )
        started = self.event(
            "generation-thread", status="inProgress",
            command="pytest tests/test_generation_b.py", suffix="2",
        )

        for order, events in enumerate(((completed, started), (started, completed)), start=1):
            with self.subTest(order=order):
                store = self.ingest(f"generation-boundary-{order}", events)
                self.assertEqual(store.count("rollout_test_runs"), 0)
                self.assertEqual(
                    {
                        tuple(row) for row in store.connection.execute(
                            """SELECT completeness,outcome,exit_status
                                 FROM test_evidence_candidates
                                WHERE candidate_kind='evidence'"""
                        )
                    },
                    {
                        ("complete", "success", 0),
                        ("intent_only", "unknown", None),
                    },
                )
                self.assertEqual(
                    store.connection.execute(
                        """SELECT COUNT(DISTINCT command_hash)
                             FROM test_evidence_candidates
                            WHERE candidate_kind='evidence'"""
                    ).fetchone()[0],
                    2,
                )
                dump = "\n".join(store.connection.iterdump())
                for raw in (
                    "shared-raw-item-id", "reused-thread",
                    "pytest tests/test_first.py", "pytest tests/test_second.py",
                ):
                    self.assertNotIn(raw, dump)

    def test_cross_thread_cancellation_never_pops_other_thread_completion(self) -> None:
        completed = self.event(
            "executed-thread", status="completed",
            command="pytest tests/test_executed.py", exit_code=0, suffix="1",
        )
        cancelled = self.event(
            "cancelled-thread", status="cancelled", suffix="2",
        )

        for order, events in enumerate(((completed, cancelled), (cancelled, completed)), start=1):
            with self.subTest(order=order):
                store = self.ingest(f"cross-thread-cancel-{order}", events)
                self.assertEqual(
                    [
                        tuple(row) for row in store.connection.execute(
                            """SELECT session_key,outcome,exit_status
                                 FROM rollout_test_runs"""
                        )
                    ],
                    [(self.session_key("executed-thread"), "success", 0)],
                )
                self.assertEqual(
                    {
                        tuple(row) for row in store.connection.execute(
                            """SELECT session_key,candidate_kind
                                 FROM test_evidence_candidates"""
                        )
                    },
                    {
                        (self.session_key("executed-thread"), "evidence"),
                        (self.session_key("cancelled-thread"), "non_execution"),
                    },
                )

    def test_unambiguous_commandless_terminal_in_same_source_remains_bound(self) -> None:
        started = self.event(
            "commandless-thread", status="inProgress",
            command="pytest tests/test_commandless.py", suffix="1",
        )
        completed = self.event(
            "commandless-thread", status="completed", suffix="2",
        )

        store = self.ingest("commandless-terminal", (started, completed))

        self.assertEqual(
            tuple(store.connection.execute(
                """SELECT outcome,exit_status,completeness
                     FROM rollout_test_runs"""
            ).fetchone()),
            ("unknown", None, "result_without_exit"),
        )


if __name__ == "__main__":
    unittest.main()
