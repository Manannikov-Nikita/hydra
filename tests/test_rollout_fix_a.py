from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from hydra_codex.rollout import Pseudonymizer, ingest_rollouts, opaque
from hydra_codex.metrics import aggregate_project, aggregate_project_facts
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
        source = self.base / "rollouts"
        write(source / "root.jsonl", [record("session_meta", {"id": "root-thread", "session_id": "conversation", "cwd": str(self.project)}, 0)])
        write(source / "child.jsonl", [record("session_meta", {"id": "child-thread", "session_id": "conversation", "cwd": str(self.project), "parent_thread_id": "root-thread"}, 1)])
        write(source / "other.jsonl", [record("session_meta", {"id": "other-thread", "session_id": "conversation", "cwd": str(self.other)}, 2)])

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

    def test_archive_copy_and_appended_prefix_do_not_duplicate_token_events(self) -> None:
        active = self.base / "active" / "thread-uuid.jsonl"
        archive = self.base / "archive" / "thread-uuid.jsonl"
        rows = [
            record("session_meta", {"id": "thread", "cwd": str(self.project)}, 0),
            record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}}}, 1),
        ]
        write(active, rows)
        ingest_rollouts(self.store, (active,), self.project, "project-a", hash_key=b"c" * 32)
        write(active, rows + [record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 20, "cached_input_tokens": 4, "output_tokens": 5}}}, 2)])
        write(archive, rows)
        ingest_rollouts(self.store, (active, archive), self.project, "project-a", hash_key=b"c" * 32)

        self.assertEqual(self.store.count("token_snapshots"), 2)

    def test_replayed_parent_meta_does_not_switch_child_thread_and_completed_turn_stays_completed(self) -> None:
        source = self.base / "rollouts" / "child.jsonl"
        write(source, [
            record("session_meta", {"id": "child", "session_id": "conversation", "cwd": str(self.project), "parent_thread_id": "parent"}, 0),
            record("turn_context", {"turn_id": "turn-child"}, 1),
            record("event_msg", {"type": "task_complete", "turn_id": "turn-child", "duration_ms": 9}, 2),
            record("session_meta", {"id": "parent", "session_id": "conversation", "cwd": str(self.project)}, 3),
            record("event_msg", {"type": "task_started", "turn_id": "turn-child"}, 4),
            record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 8, "cached_input_tokens": 1, "output_tokens": 2}}}, 5),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"d" * 32)

        self.assertEqual(self.store.count("rollout_sessions"), 1)
        self.assertEqual(self.store.connection.execute("SELECT state FROM turn_attempts").fetchone()[0], "completed")
        self.assertEqual(self.store.connection.execute("SELECT turn_key FROM token_snapshots").fetchone()[0], self.store.connection.execute("SELECT turn_key FROM turn_attempts").fetchone()[0])

    def test_confirmed_parent_metadata_upgrades_inferred_edge_without_resume_inflation(self) -> None:
        source = self.base / "rollouts" / "child.jsonl"
        write(source, [
            record("session_meta", {"id": "child", "cwd": str(self.project)}, 0),
            record("event_msg", {"type": "sub_agent_activity", "agent_thread_id": "child"}, 1),
            record("session_meta", {"id": "child", "cwd": str(self.project), "parent_thread_id": "parent"}, 2),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"e" * 32)
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"e" * 32)

        edge = self.store.connection.execute("SELECT confidence_kind, confidence FROM session_edges").fetchone()
        self.assertEqual(tuple(edge), ("confirmed", 1.0))
        self.assertEqual(self.store.connection.execute("SELECT resume_segments FROM rollout_sessions WHERE path_key != 'unresolved'").fetchone()[0], 1)

    def test_cross_source_timestamp_order_wins_and_counter_reset_creates_epoch(self) -> None:
        old = self.base / "z-old.jsonl"
        new = self.base / "a-new.jsonl"
        write(old, [record("session_meta", {"id": "thread", "cwd": str(self.project)}, 0), record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 0}}}, 10)])
        write(new, [record("session_meta", {"id": "thread", "cwd": str(self.project)}, 0), record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 200, "cached_input_tokens": 40, "output_tokens": 20, "reasoning_output_tokens": 0}}}, 20)])
        ingest_rollouts(self.store, (new, old), self.project, "project-a", hash_key=b"f" * 32)
        first = aggregate_project(self.store.connection, "project-a")
        self.assertEqual(first.working_tokens, 180)
        reset = self.base / "m-reset.jsonl"
        write(reset, [record("session_meta", {"id": "thread", "cwd": str(self.project)}, 0), record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 30, "cached_input_tokens": 5, "output_tokens": 4, "reasoning_output_tokens": 0}}}, 30)])
        ingest_rollouts(self.store, (reset,), self.project, "project-a", hash_key=b"f" * 32)
        self.assertEqual(aggregate_project(self.store.connection, "project-a").working_tokens, 209)

    def test_invalid_components_are_null_and_valid_zero_is_present(self) -> None:
        source = self.base / "partial.jsonl"
        write(source, [
            record("session_meta", {"id": "thread", "cwd": str(self.project)}, 0),
            record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 0, "cached_input_tokens": -1, "output_tokens": True, "reasoning_output_tokens": "bad"}}}, 1),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"g" * 32)
        self.assertEqual(tuple(self.store.connection.execute("SELECT input_tokens, cached_input_tokens, output_tokens, reasoning_tokens FROM token_snapshots").fetchone()), (0, None, None, None))

    def test_late_child_token_is_not_a_replay_baseline(self) -> None:
        source = self.base / "child-late.jsonl"
        write(source, [
            record("session_meta", {"id": "child", "cwd": str(self.project), "parent_thread_id": "parent"}, 0),
            record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 1}}}, 5),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"h" * 32)
        self.assertEqual(self.store.count("fork_baselines"), 0)

    def test_last_eligible_baseline_candidate_wins_independent_of_root_order(self) -> None:
        early = self.base / "early.jsonl"
        late = self.base / "late.jsonl"
        meta = record("session_meta", {"id": "child", "cwd": str(self.project), "parent_thread_id": "parent"}, 0)
        vector = lambda value: record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": value, "cached_input_tokens": 1, "output_tokens": 2, "reasoning_output_tokens": 3}}}, value)
        write(early, [meta, vector(0)])
        write(late, [meta, vector(1)])
        ingest_rollouts(self.store, (late, early), self.project, "project-a", hash_key=b"i" * 32)
        self.assertEqual(self.store.connection.execute("SELECT input_tokens FROM fork_baselines").fetchone()[0], 1)

    def test_metric_facts_keep_working_full_exact_when_reasoning_missing(self) -> None:
        source = self.base / "facts.jsonl"
        write(source, [record("session_meta", {"id": "facts", "cwd": str(self.project)}, 0), record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}}}, 1)])
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"j" * 32)
        facts = aggregate_project_facts(self.store.connection, "project-a")
        self.assertEqual((facts["working"].value, facts["full"].value, facts["reasoning"].value), (11, 13, None))
        self.assertEqual(facts["reasoning"].provenance, "estimated")

    def test_metric_facts_sum_epochs_and_report_missing_confirmed_baseline(self) -> None:
        source = self.base / "epochs.jsonl"
        write(source, [
            record("session_meta", {"id": "child", "cwd": str(self.project), "parent_thread_id": "parent"}, 0),
            record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 1}}}, 2),
            record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 4, "cached_input_tokens": 1, "output_tokens": 1, "reasoning_output_tokens": 2}}}, 3),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-a", hash_key=b"k" * 32)
        facts = aggregate_project_facts(self.store.connection, "project-a")
        self.assertEqual(facts["recorded_working"].value, 15)
        self.assertEqual(facts["deduplicated_working"].caveats, ("zero_no_observation",))

    def test_fact_epochs_are_global_and_inferred_edge_is_not_exact_dedup(self) -> None:
        first = self.base / "first.jsonl"
        second = self.base / "second.jsonl"
        meta = record("session_meta", {"id": "child", "cwd": str(self.project)}, 0)
        write(first, [meta, record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 0}}}, 2)])
        write(second, [meta, record("event_msg", {"type": "sub_agent_activity", "agent_thread_id": "child"}, 1), record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 4, "cached_input_tokens": 1, "output_tokens": 1, "reasoning_output_tokens": 0}}}, 3)])
        ingest_rollouts(self.store, (first, second), self.project, "project-a", hash_key=b"l" * 32)
        facts = aggregate_project_facts(self.store.connection, "project-a")
        self.assertEqual(facts["recorded_working"].value, 15)
        self.assertEqual(facts["deduplicated_working"].caveats, ("inferred_parent_no_dedup",))

    def test_installation_key_is_atomic_domain_separated_and_invalid_fails_closed(self) -> None:
        key_dir = self.base / "keys"
        key_dir.mkdir()
        results: list[bytes] = []
        errors: list[Exception] = []
        def create() -> None:
            try:
                results.append(Pseudonymizer.installation(key_dir).key)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=create) for _ in range(16)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual({len(key) for key in results}, {32})
        self.assertEqual(len(set(results)), 1)
        hasher = Pseudonymizer(results[0])
        self.assertEqual(len({hasher.digest(domain, "same") for domain in ("identity", "path", "command", "source", "event", "diagnostic", "capability")}), 7)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((key_dir / "rollout-hmac.key").stat().st_mode), 0o600)
        (key_dir / "rollout-hmac.key").write_bytes(b"bad")
        with self.assertRaises(ValueError):
            Pseudonymizer.installation(key_dir)
