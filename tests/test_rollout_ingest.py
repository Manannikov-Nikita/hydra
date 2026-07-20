from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hydra_codex.classifier import classify_test_command, classify_test_outcome
from hydra_codex.metrics import SessionEdge, TokenSnapshot, TurnAttempt, aggregate_project, aggregate_tokens, aggregate_turns, tree_contribution
from hydra_codex.rollout import RolloutRoot, ingest_rollouts
from hydra_codex.storage import HydraStore


def v1(kind: str, payload: dict, second: int = 0) -> dict:
    """A safe, faithful Codex rollout v1 envelope without transcript content."""
    return {"timestamp": f"2026-07-20T00:00:{second:02d}Z", "type": kind, "payload": payload}


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")


class TokenAggregationTests(unittest.TestCase):
    def test_cumulative_duplicates_and_counter_reset_create_distinct_epochs(self) -> None:
        snapshots = (
            TokenSnapshot("alpha", 1, 0, 100, 20, 10, 3, 0),
            TokenSnapshot("alpha", 2, 0, 100, 20, 10, 3, 0),
            TokenSnapshot("alpha", 3, 0, 150, 30, 20, 5, 0),
            TokenSnapshot("alpha", 4, 1, 25, 5, 4, 1, 0),
            TokenSnapshot("alpha", 5, 1, 40, 10, 8, 2, 0),
        )

        totals = aggregate_tokens(snapshots)

        self.assertEqual((totals.working_tokens, totals.full_context, totals.reasoning_tokens), (178, 218, 7))
        self.assertEqual((totals.epochs, totals.provenance), (2, "exact"))

    def test_confirmed_and_ambiguous_fork_contributions_are_explicit(self) -> None:
        child = aggregate_tokens((TokenSnapshot("child", 1, 0, 300, 100, 50, 0, 0),))

        confirmed = tree_contribution(child, SessionEdge("child", "parent", 100, "confirmed", 1.0))
        inferred = tree_contribution(child, SessionEdge("child", None, None, "inferred", 0.3))

        self.assertEqual((confirmed.working_tokens, confirmed.provenance), (150, "derived"))
        self.assertEqual((inferred.working_tokens, inferred.provenance), (None, "estimated"))

    def test_turn_aggregation_keeps_wall_clock_and_agent_time_distinct(self) -> None:
        totals = aggregate_turns((
            TurnAttempt("a", "2026-07-20T00:00:00Z", "2026-07-20T00:00:03Z", 1200),
            TurnAttempt("b", "2026-07-20T00:00:04Z", None, 800),
        ))
        self.assertEqual((totals.wall_clock_ms, totals.agent_time_ms, totals.provenance), (3000, 2000, "derived"))

    def test_overlapping_turns_use_root_wall_span_and_summed_agent_time(self) -> None:
        totals = aggregate_turns((
            TurnAttempt("a", "2026-07-20T00:00:00Z", "2026-07-20T00:00:04Z", 4000),
            TurnAttempt("b", "2026-07-20T00:00:02Z", "2026-07-20T00:00:05Z", 3000),
        ))
        self.assertEqual((totals.wall_clock_ms, totals.agent_time_ms), (5000, 7000))


class RolloutIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".hydra").mkdir()
        (self.project / ".hydra" / "project.toml").write_text('project_id = "project-synthetic"\n', encoding="utf-8")
        self.store = HydraStore(self.root / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_active_archive_duplicate_resume_abort_open_turn_and_unknown_schema(self) -> None:
        records = [
            v1("session_meta", {"id": "anon-session-a", "cwd": str(self.project / "one")}),
            v1("event_msg", {"type": "task_started", "turn_id": "turn-a", "duration_ms": 9}, 1),
            v1("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 3, "cache_write_input_tokens": 2, "total_tokens": 110}, "model_context_window": 1000}}, 2),
            v1("session_meta", {"id": "anon-session-a", "cwd": str(self.project / "one")}, 3),
            v1("event_msg", {"type": "turn_aborted", "turn_id": "turn-a"}, 4),
            v1("event_msg", {"type": "task_started", "turn_id": "turn-open"}, 5),
            v1("future_envelope", {"safe_category": "future"}, 6),
        ]
        active = self.root / "active" / "rollout.jsonl"
        archived = self.root / "archive" / "copy.jsonl"
        write_jsonl(active, records)
        write_jsonl(archived, records)

        report = ingest_rollouts(self.store, (self.root / "active", self.root / "archive"), self.project, "project-synthetic")

        self.assertEqual((report.files_seen, report.unique_sources), (2, 1))
        self.assertEqual(self.store.count("rollout_source_locations"), 2)
        self.assertEqual(self.store.count("rollout_sessions"), 1)
        self.assertEqual(self.store.count("turn_attempts"), 2)
        self.assertEqual({row[0] for row in self.store.connection.execute("SELECT state FROM turn_attempts")}, {"aborted", "open"})
        self.assertEqual(self.store.count("rollout_diagnostics"), 1)
        self.assertEqual(tuple(self.store.connection.execute("SELECT reasoning_tokens, cache_write_tokens, vendor_total, context_window FROM token_snapshots").fetchone()), (3, 2, 110, 1000))

    def test_one_session_per_file_worktrees_and_parallel_sessions(self) -> None:
        write_jsonl(self.root / "rollouts" / "a.jsonl", [v1("session_meta", {"id": "anon-a", "cwd": str(self.project / "feature-a")})])
        write_jsonl(self.root / "rollouts" / "b.jsonl", [v1("session_meta", {"id": "anon-b", "cwd": str(self.project / "feature-b")})])
        write_jsonl(self.root / "rollouts" / "c.jsonl", [v1("session_meta", {"id": "anon-c", "cwd": str(self.project / "shared")})])
        write_jsonl(self.root / "rollouts" / "d.jsonl", [v1("session_meta", {"id": "anon-d", "cwd": str(self.project / "shared")})])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic")

        rows = self.store.connection.execute("SELECT project_id, path_key FROM rollout_sessions ORDER BY session_key").fetchall()
        self.assertEqual([row[0] for row in rows], ["project-synthetic"] * 4)
        self.assertEqual(len({row[1] for row in rows}), 3)

    def test_subagent_activity_fallback_preserves_an_unresolved_child_edge(self) -> None:
        write_jsonl(self.root / "rollouts" / "parent.jsonl", [
            v1("session_meta", {"id": "anon-parent", "cwd": str(self.project)}),
            v1("event_msg", {"type": "sub_agent_activity", "agent_thread_id": "anon-child"}, 1),
        ])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic")

        self.assertEqual((self.store.count("rollout_sessions"), self.store.count("session_edges")), (2, 1))

    def test_partial_vectors_turn_timing_custom_tools_and_location_labels_are_safe(self) -> None:
        source = self.root / "archive" / "safe.jsonl"
        write_jsonl(source, [
            v1("session_meta", {"id": "anon-partial", "cwd": str(self.project)}),
            v1("event_msg", {"type": "task_started", "turn_id": "timed"}, 0),
            v1("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 20}, "model_context_window": 4096}}, 1),
            v1("response_item", {"type": "custom_tool_call", "call_id": "opaque-a", "name": "opaque_exec", "input": "never store this"}, 2),
            v1("response_item", {"type": "custom_tool_call_output", "call_id": "opaque-a", "output": "never store this"}, 3),
            v1("event_msg", {"type": "task_complete", "turn_id": "timed", "duration_ms": 50}, 4),
        ])

        ingest_rollouts(self.store, (RolloutRoot(source, "archived"),), self.project, "project-synthetic", hash_key=b"t" * 32)

        self.assertEqual(tuple(self.store.connection.execute("SELECT completeness, context_window FROM token_snapshots").fetchone()), ("partial", 4096))
        self.assertEqual(tuple(self.store.connection.execute("SELECT category, terminal_state FROM tool_spans").fetchone()), ("opaque_exec", "unknown"))
        self.assertEqual(self.store.connection.execute("SELECT location_type FROM rollout_source_locations").fetchone()[0], "archived")
        turn = self.store.connection.execute("SELECT started_at, finished_at, emitted_duration_ms FROM turn_attempts").fetchone()
        self.assertEqual(tuple(turn), ("2026-07-20T00:00:00Z", "2026-07-20T00:00:04Z", 50))
        metrics = aggregate_project(self.store.connection, "project-synthetic")
        self.assertEqual((metrics.working_tokens, metrics.full_context, metrics.provenance), (None, None, "estimated"))

    def test_injected_keys_are_stable_per_key_and_distinct_across_keys(self) -> None:
        source = self.root / "rollouts" / "keyed.jsonl"
        write_jsonl(source, [v1("session_meta", {"id": "anon-keyed", "cwd": str(self.project)})])
        stores = [HydraStore(self.root / f"key-{index}.sqlite3") for index in range(3)]
        self.addCleanup(lambda: [store.close() for store in stores])
        for store, key in zip(stores, (b"s" * 32, b"s" * 32, b"o" * 32)):
            ingest_rollouts(store, (source,), self.project, "project-synthetic", hash_key=key)
        keys = [store.connection.execute("SELECT session_key FROM rollout_sessions").fetchone()[0] for store in stores]
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])

    def test_malformed_out_of_order_events_are_diagnostic_and_reingest_is_idempotent(self) -> None:
        source = self.root / "rollouts" / "broken.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(
            json.dumps(v1("event_msg", {"type": "task_complete", "turn_id": "late"})) + "\nnot-json\n" +
            json.dumps(v1("session_meta", {"id": "anon-late", "cwd": str(self.project)})) + "\n",
            encoding="utf-8",
        )

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic")
        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic")

        self.assertEqual((self.store.count("rollout_sources"), self.store.count("rollout_diagnostics"), self.store.count("turn_attempts")), (1, 2, 1))

    def test_tool_join_instrumentation_file_allowlist_and_no_raw_transcript_persistence(self) -> None:
        private_text = "private prompt tool output patch transcript"
        write_jsonl(self.root / "rollouts" / "tools.jsonl", [
            v1("session_meta", {"id": "anon-tool", "cwd": str(self.project)}),
            v1("response_item", {"type": "function_call", "call_id": "call-a", "name": "exec_command", "arguments": json.dumps({"cmd": "pytest tests/test_safe.py"})}, 1),
            v1("response_item", {"type": "function_call_output", "call_id": "call-a", "output": json.dumps({"exit_code": 0, "stdout": private_text})}, 2),
            v1("event_msg", {"type": "mcp_tool_call_end", "call_id": "call-a", "duration": {"secs": 0, "nanos": 9000000}, "result": {"Ok": {}}, "invocation": {"server": "safe", "tool": "safe", "arguments": {}}}, 3),
            v1("response_item", {"type": "function_call", "call_id": "call-b", "name": "hydra_annotate", "arguments": json.dumps({"path": "src/safe.py"})}, 4),
            v1("response_item", {"type": "function_call", "call_id": "call-read", "name": "file_read", "arguments": json.dumps({"path": "src/read.py"})}, 4),
            v1("event_msg", {"type": "patch_apply_end", "call_id": "call-b", "success": True, "status": "ok", "stdout": private_text, "stderr": "", "changes": {str(self.project / "src" / "safe.py"): {"type": "modify", "move_path": None, "unified_diff": private_text}}}, 5),
        ])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic")

        spans = self.store.connection.execute("SELECT category, terminal_state FROM tool_spans ORDER BY call_key").fetchall()
        self.assertEqual(sorted(tuple(row) for row in spans), [("instrumentation", "success"), ("tool", "success"), ("tool", "unknown")])
        self.assertEqual(tuple(self.store.connection.execute("SELECT operation, relative_path FROM file_observations").fetchone()), ("write", "src/safe.py"))
        self.assertIn(("read", "src/read.py"), [tuple(row) for row in self.store.connection.execute("SELECT operation, relative_path FROM file_observations")])
        self.assertNotIn(private_text, "\n".join(self.store.connection.iterdump()))

    def test_test_detection_and_model_cause_conflict_use_deterministic_evidence(self) -> None:
        self.assertEqual(classify_test_command("python -m pytest"), ("pytest", "full"))
        self.assertEqual(classify_test_command("npm test -- ui.spec.ts"), ("npm", "targeted"))
        for command, runner in (("vitest run", "vitest"), ("jest", "jest"), ("playwright test", "playwright"), ("pnpm test", "pnpm"), ("yarn test", "yarn"), ("bun test", "bun"), ("go test ./...", "go"), ("cargo test", "cargo"), ("mvn test", "maven"), ("gradle test", "gradle"), ("xcodebuild test", "xcode"), ("swift test", "swift"), ("dotnet test", "dotnet")):
            with self.subTest(command=command):
                self.assertEqual(classify_test_command(command)[0], runner)
        self.assertEqual(classify_test_outcome(1, "assertion failed", ()), ("product_failure", "failed"))
        self.assertEqual(classify_test_outcome(1, "sandbox unavailable", ()), ("infra_retry", "blocked"))
        self.assertEqual(classify_test_outcome(0, "", ("same-hash",)), ("flaky_retry", "success"))
        write_jsonl(self.root / "rollouts" / "tests.jsonl", [
            v1("session_meta", {"id": "anon-test", "cwd": str(self.project)}),
            v1("response_item", {"type": "function_call", "call_id": "call-test", "name": "exec_command", "arguments": json.dumps({"cmd": "pytest tests/test_safe.py"})}, 1),
            v1("response_item", {"type": "function_call_output", "call_id": "call-test", "output": json.dumps({"exit_code": 1, "stderr": "assertion failed"})}, 2),
        ])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic", model_causes={"call-test": "infra_failure"})

        self.assertEqual((self.store.count("rollout_test_runs"), self.store.count("semantic_conflicts")), (1, 1))


class HistoricalFixtureAcceptanceTests(unittest.TestCase):
    def test_anonymized_legacy_manifests_materialize_v1_files_and_reconstruct_totals(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "rollouts"
        expected = {
            "019f64b9-quiz-newer.json": (5_018_356, 171_325_172, 25),
            "019f64b9-quiz-older.json": (6_587_842, 213_288_642, 28),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            project = base / "project"
            project.mkdir()
            store = HydraStore(base / "hydra.sqlite3")
            self.addCleanup(store.close)
            for filename, totals in expected.items():
                (project / ".hydra").mkdir(exist_ok=True)
                (project / ".hydra" / "project.toml").write_text(f'project_id = "project-{filename}"\n', encoding="utf-8")
                manifest = json.loads((fixtures / filename).read_text(encoding="utf-8"))
                root = base / manifest["root"]
                for index, vector in enumerate(manifest["vectors"], start=1):
                    payload = {"id": f"{manifest['session_prefix']}-{index:02d}", "cwd": str(project)}
                    if index == 2:
                        payload["parent_thread_id"] = f"{manifest['session_prefix']}-01"
                    records = [v1("session_meta", payload)]
                    if index == 2:
                        records.append(v1("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}}}, 1))
                        vector = {**vector, "input_tokens": vector["input_tokens"] + 100}
                    records.append(v1("event_msg", {"type": "token_count", "info": {"total_token_usage": vector}}, 2))
                    write_jsonl(root / f"session-{index:02d}.jsonl", [
                        *records,
                    ])
                ingest_rollouts(store, (root,), project, f"project-{filename}")
                metrics = aggregate_project(store.connection, f"project-{filename}")
                self.assertEqual((metrics.recorded_working_tokens, metrics.recorded_full_context), (totals[0] + 100, totals[1] + 100))
                self.assertEqual((metrics.working_tokens, metrics.full_context, metrics.sessions), totals)
                self.assertEqual(metrics.semantic_coverage, 0)
                self.assertGreaterEqual(store.count("session_edges"), 1)
                child_key = store.connection.execute("SELECT child_key FROM session_edges LIMIT 1").fetchone()[0]
                child_rows = store.connection.execute(
                    """SELECT session_key, line_number, epoch, input_tokens, cached_input_tokens, output_tokens,
                              reasoning_tokens, cache_write_tokens FROM token_snapshots WHERE session_key = ?""", (child_key,)
                ).fetchall()
                child = aggregate_tokens(TokenSnapshot(*tuple(row)) for row in child_rows)
                unique_child = tree_contribution(child, SessionEdge(child_key, "parent", 100, "confirmed", 1.0))
                self.assertEqual(unique_child.working_tokens, child.working_tokens - 100)
