from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hydra_codex.rollout import ingest_rollouts
from hydra_codex.storage import HydraStore


def record(kind: str, payload: dict, second: int) -> dict:
    return {"timestamp": f"2026-07-21T00:00:{second:02d}Z", "type": kind, "payload": payload}


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class FixAIdentityAndProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text('project_id = "project-a"\n', encoding="utf-8")
        self.other = self.base / "other"
        (self.other / ".hydra").mkdir(parents=True)
        (self.other / ".hydra" / "project.toml").write_text('project_id = "project-b"\n', encoding="utf-8")
        self.store = HydraStore(self.base / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_thread_id_is_distinct_from_shared_conversation_and_unrelated_cwd_is_quarantined(self) -> None:
        source = self.base / "rollouts" / "threads.jsonl"
        write(source, [
            record("session_meta", {"id": "root-thread", "session_id": "conversation", "cwd": str(self.project)}, 0),
            record("session_meta", {"id": "child-thread", "session_id": "conversation", "cwd": str(self.project), "parent_thread_id": "root-thread"}, 1),
            record("session_meta", {"id": "other-thread", "session_id": "conversation", "cwd": str(self.other)}, 2),
        ])

        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"a" * 32)

        rows = self.store.connection.execute("SELECT session_key, conversation_key FROM rollout_sessions ORDER BY session_key").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row[1] for row in rows}), 1)
        self.assertEqual(self.store.count("session_edges"), 1)
        self.assertGreaterEqual(self.store.count("rollout_diagnostics"), 1)

    def test_append_reingest_keeps_prefix_and_new_event_once(self) -> None:
        source = self.base / "rollouts" / "append.jsonl"
        first = [record("session_meta", {"id": "thread", "cwd": str(self.project)}, 0)]
        write(source, first)
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"b" * 32)
        write(source, first + [record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}}}, 1)])
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"b" * 32)

        self.assertEqual((self.store.count("rollout_sessions"), self.store.count("token_snapshots")), (1, 1))
