from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore


KEY = b"tool-lineage-arbitration-key-001"
PROJECT = "tool-lineage-arbitration"


def write_jsonl(path: Path, rows: tuple[dict[str, object], ...]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class ToolAndLineageArbitrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT}"\n', encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _sources(self, suffix: str) -> tuple[Path, Path]:
        rollout = self.base / f"tool-{suffix}-rollout.jsonl"
        write_jsonl(rollout, (
            {
                "timestamp": "2024-07-03T09:46:40Z", "type": "session_meta",
                "payload": {"id": "tool-session", "cwd": str(self.project)},
            },
            {
                "timestamp": "2024-07-03T09:46:40.100000Z", "type": "response_item",
                "payload": {
                    "type": "function_call", "call_id": "conflicting-call",
                    "name": "exec_command", "arguments": json.dumps({"cmd": "false"}),
                },
            },
            {
                "timestamp": "2024-07-03T09:46:40.200000Z", "type": "response_item",
                "payload": {
                    "type": "function_call_output", "call_id": "conflicting-call",
                    "output": json.dumps({"exit_code": 2}),
                },
            },
            {
                "timestamp": "2024-07-03T09:46:40.500000Z", "type": "response_item",
                "payload": {
                    "type": "function_call", "call_id": "gap-fill-call",
                    "name": "exec_command", "arguments": json.dumps({"cmd": "true"}),
                },
            },
        ))
        app = self.base / f"tool-{suffix}-app.jsonl"
        write_jsonl(app, (
            {
                "received_at": "2024-07-03T09:46:40.300000Z",
                "message": {"method": "item/started", "params": {
                    "threadId": "tool-session", "turnId": "app-turn",
                    "item": {
                        "id": "conflicting-call", "type": "fileChange",
                        "status": "inProgress", "changes": [{"path": "src/app.py"}],
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:40.400000Z",
                "message": {"method": "item/completed", "params": {
                    "threadId": "tool-session", "turnId": "app-turn",
                    "item": {
                        "id": "conflicting-call", "type": "fileChange",
                        "status": "completed", "changes": [{"path": "src/app.py"}],
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:40.600000Z",
                "message": {"method": "item/completed", "params": {
                    "threadId": "tool-session", "turnId": "app-turn",
                    "item": {
                        "id": "gap-fill-call", "type": "commandExecution",
                        "command": "true", "cwd": str(self.project),
                        "status": "completed", "exitCode": 0,
                    },
                }},
            },
        ))
        return rollout, app

    def _ingest_order(self, order: str) -> list[tuple[object, ...]]:
        rollout, app = self._sources(order)
        store = HydraStore(self.base / f"tool-{order}.sqlite3")
        self.addCleanup(store.close)
        if order == "rollout-first":
            ingest_rollouts(store, (rollout,), self.project, PROJECT, hash_key=KEY)
            ingest_codex_events(
                store, (CodexEventSource(app, APP_SERVER_V2),),
                self.project, PROJECT, hash_key=KEY,
            )
        else:
            ingest_codex_events(
                store, (CodexEventSource(app, APP_SERVER_V2),),
                self.project, PROJECT, hash_key=KEY,
            )
            ingest_rollouts(store, (rollout,), self.project, PROJECT, hash_key=KEY)
        return [tuple(row) for row in store.connection.execute(
            "SELECT tool_name,category,terminal_state,started_at,finished_at,"
            "completeness,provenance,source_digest FROM tool_spans ORDER BY started_at"
        )]

    def test_tool_claims_use_source_precedence_in_both_ingest_orders(self) -> None:
        observed = [self._ingest_order(order) for order in ("rollout-first", "app-first")]
        self.assertEqual(observed[0], observed[1])
        rows = [row[:7] for row in observed[0]]
        self.assertEqual(rows, [
            (
                "exec_command", "tool", "failed",
                "2024-07-03T09:46:40.100000Z", "2024-07-03T09:46:40.200000Z",
                "complete", "exact",
            ),
            (
                "exec_command", "tool", "success",
                "2024-07-03T09:46:40.500000Z", "2024-07-03T09:46:40.600000Z",
                "complete", "exact",
            ),
        ])
        # Both canonical rows retain the higher-authority rollout source.
        self.assertTrue(all(row[7] == observed[0][0][7] for row in observed[0]))

    def _lineage_order(self, order: str, app_parent: str) -> tuple[object, ...]:
        child = "shared-child"
        rollout = self.base / f"lineage-{order}-{app_parent}-rollout.jsonl"
        write_jsonl(rollout, ({
            "timestamp": "2024-07-03T09:46:40Z", "type": "session_meta",
            "payload": {
                "id": child, "cwd": str(self.project), "parent_thread_id": "parent-a",
            },
        },))
        app = self.base / f"lineage-{order}-{app_parent}-app.jsonl"
        write_jsonl(app, ({
            "received_at": "2024-07-03T09:46:40.100000Z",
            "message": {"method": "item/completed", "params": {
                "threadId": app_parent, "turnId": "spawn-turn",
                "item": {
                    "id": "spawn-call", "type": "collabToolCall",
                    "senderThreadId": app_parent, "newThreadId": child,
                    "status": "completed",
                },
            }},
        },))
        store = HydraStore(self.base / f"lineage-{order}-{app_parent}.sqlite3")
        self.addCleanup(store.close)
        if order == "rollout-first":
            ingest_rollouts(store, (rollout,), self.project, PROJECT, hash_key=KEY)
            ingest_codex_events(
                store, (CodexEventSource(app, APP_SERVER_V2),),
                self.project, PROJECT, hash_key=KEY,
            )
        else:
            ingest_codex_events(
                store, (CodexEventSource(app, APP_SERVER_V2),),
                self.project, PROJECT, hash_key=KEY,
            )
            ingest_rollouts(store, (rollout,), self.project, PROJECT, hash_key=KEY)
        return tuple(store.connection.execute(
            "SELECT parent_key,confidence_kind,confidence FROM session_edges"
        ).fetchone())

    def test_conflicting_confirmed_parents_are_quarantined_in_both_orders(self) -> None:
        rows = [self._lineage_order(order, "parent-b") for order in ("rollout-first", "app-first")]
        self.assertEqual(rows, [(None, "ambiguous", 0.0), (None, "ambiguous", 0.0)])

    def test_identical_confirmed_parent_claims_remain_confirmed(self) -> None:
        expected_parent = Pseudonymizer(KEY).digest("identity", "parent-a")
        rows = [self._lineage_order(order, "parent-a") for order in ("rollout-first", "app-first")]
        self.assertEqual(rows, [
            (expected_parent, "confirmed", 1.0),
            (expected_parent, "confirmed", 1.0),
        ])


if __name__ == "__main__":
    unittest.main()
