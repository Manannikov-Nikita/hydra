from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydra_codex.classifier import classify_test_command, classify_test_outcome
from hydra_codex.metrics import SessionEdge, TokenSnapshot, TurnAttempt, aggregate_project, aggregate_tokens, aggregate_turns, tree_contribution
from hydra_codex.rollout import RolloutRoot, ingest_rollouts
from hydra_codex.storage import HydraStore
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


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

    def test_global_reconciliation_runs_once_per_ingest_batch(self) -> None:
        for index in range(2):
            write_jsonl(self.root / "rollouts" / f"tests-{index}.jsonl", [
                v1("session_meta", {
                    "id": f"anon-tests-{index}", "cwd": str(self.project),
                }),
                v1("response_item", {
                    "type": "function_call", "call_id": f"call-{index}",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": f"pytest tests/test_{index}.py"}),
                }, 1),
                v1("response_item", {
                    "type": "function_call_output", "call_id": f"call-{index}",
                    "output": json.dumps({"exit_code": 0}),
                }, 2),
            ])
        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)

        ingest_rollouts(
            self.store, (self.root / "rollouts",), self.project,
            "project-synthetic", hash_key=b"b" * 32,
        )

        materializations = [
            statement for statement in statements
            if "FROM test_evidence_candidates" in statement
            and "SELECT candidate_key,candidate_kind,evidence_key" in statement
        ]
        token_selections = [
            statement for statement in statements
            if "SELECT session_key,source_family,observed_at,event_key,input_tokens" in statement
        ]
        token_epochs = [
            statement for statement in statements
            if "envelope_kind='counter_reset'" in statement
        ]
        fork_baselines = [
            statement for statement in statements
            if "DELETE FROM fork_baselines" in statement
        ]
        turn_attempts = [
            statement for statement in statements
            if "FROM turn_lifecycle_events" in statement
            and "SELECT event_key,session_key,turn_key,event_kind" in statement
        ]
        self.assertEqual(len(materializations), 1)
        self.assertEqual(len(token_selections), 1)
        self.assertEqual(len(token_epochs), 1)
        self.assertEqual(len(fork_baselines), 1)
        self.assertEqual(len(turn_attempts), 1)
        self.assertEqual(self.store.count("rollout_test_runs"), 2)

    def test_ingest_progress_reports_safe_batch_stages_without_paths(self) -> None:
        source = self.root / "rollouts" / "private-name.jsonl"
        write_jsonl(source, [
            v1("session_meta", {"id": "anon-progress", "cwd": str(self.project)}),
        ])
        progress: list[tuple[str, int, int]] = []

        ingest_rollouts(
            self.store, (self.root / "rollouts",), self.project,
            "project-synthetic", hash_key=b"p" * 32,
            progress=lambda stage, current, total: progress.append(
                (stage, current, total),
            ),
        )

        self.assertEqual(progress[0], ("discover", 0, 1))
        self.assertIn(("scan", 1, 1), progress)
        self.assertEqual(progress[-1], ("complete", 1, 1))
        self.assertNotIn("private-name", repr(progress))

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

    def test_cached_tokens_above_input_are_diagnosed_without_breaking_reports(self) -> None:
        source = self.root / "rollouts" / "invalid-token-relation.jsonl"
        write_jsonl(source, [
            v1("session_meta", {"id": "invalid-token-session", "cwd": str(self.project)}),
            v1("event_msg", {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 20,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 11,
                }},
            }, 1),
            v1("event_msg", {
                "type": "task_complete", "turn_id": "invalid-token-turn",
            }, 2),
        ])

        ingest_rollouts(
            self.store, (source,), self.project, "project-synthetic",
            hash_key=b"t" * 32,
        )

        self.assertEqual(self.store.count("token_snapshots"), 0)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT envelope_kind FROM rollout_diagnostics"
            ).fetchone()[0],
            "token_count",
        )
        root = self.store.connection.execute(
            "SELECT session_key FROM rollout_sessions"
        ).fetchone()[0]
        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id="project-synthetic", root_id=root,
        )
        self.assertIsNone(metrics.recorded.working.value)
        self.assertEqual(metrics.recorded.provenance, "estimated")

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
            v1("response_item", {"type": "function_call_output", "call_id": "call-b", "output": json.dumps({"success": True})}, 5),
            v1("response_item", {"type": "function_call", "call_id": "call-read", "name": "file_read", "arguments": json.dumps({"path": "src/read.py"})}, 4),
            v1("response_item", {"type": "function_call", "call_id": "call-patch", "name": "apply_patch", "arguments": json.dumps({"patch": "safe"})}, 5),
            v1("event_msg", {"type": "patch_apply_end", "call_id": "call-patch", "success": True, "status": "ok", "stdout": private_text, "stderr": "", "changes": {str(self.project / "src" / "safe.py"): {"type": "modify", "move_path": None, "unified_diff": private_text}}}, 6),
        ])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic")

        spans = self.store.connection.execute("SELECT category, terminal_state FROM tool_spans ORDER BY call_key").fetchall()
        self.assertEqual(sorted(tuple(row) for row in spans), [
            ("instrumentation", "unknown"),
            ("tool", "success"),
            ("tool", "success"),
            ("tool", "unknown"),
        ])
        self.assertEqual(
            {tuple(row) for row in self.store.connection.execute(
                "SELECT operation, relative_path FROM file_observations"
            )},
            {("write", "src/safe.py")},
        )
        self.assertNotIn(private_text, "\n".join(self.store.connection.iterdump()))

    def test_direct_function_calls_persist_only_normalized_families_and_safe_accesses(self) -> None:
        write_jsonl(self.root / "rollouts" / "direct-functions.jsonl", [
            v1("session_meta", {"id": "anon-direct", "cwd": str(self.project)}),
            v1("response_item", {"type": "function_call_output", "call_id": "unknown", "output": "safe"}, 1),
            v1("response_item", {
                "type": "function_call", "call_id": "unknown", "name": "dehydrate_cache",
                "arguments": json.dumps({"path": "src/private-cache.py"}),
            }, 2),
            v1("response_item", {
                "type": "function_call", "call_id": "image", "name": "view_image",
                "arguments": json.dumps({"path": str(self.project / "assets" / "preview.png")}),
            }, 3),
            v1("response_item", {"type": "function_call_output", "call_id": "image", "output": "safe"}, 4),
            v1("response_item", {
                "type": "function_call", "call_id": "hydra", "name": "hydra_annotate",
                "arguments": json.dumps({"path": "src/not-a-file.py"}),
            }, 5),
            v1("response_item", {"type": "function_call_output", "call_id": "hydra", "output": "safe"}, 6),
            v1("response_item", {
                "type": "function_call", "call_id": "mcp", "name": "mcp__private_server__private_tool",
                "arguments": json.dumps({"path": "src/private-mcp.py"}),
            }, 7),
            v1("event_msg", {"type": "mcp_tool_call_end", "call_id": "mcp", "result": {"Ok": {}}}, 8),
            v1("response_item", {
                "type": "function_call", "call_id": "web", "name": "web__run",
                "arguments": json.dumps({"path": "src/private-web.py"}),
            }, 9),
            v1("event_msg", {"type": "web_search_end", "call_id": "web", "result": {"Ok": {}}}, 10),
            v1("response_item", {
                "type": "function_call", "call_id": "exec", "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": "cat direct.py", "workdir": str(self.project / "src"),
                }),
            }, 11),
            v1("response_item", {
                "type": "function_call_output", "call_id": "exec",
                "output": json.dumps({"exit_code": 0}),
            }, 12),
        ])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic")

        rows = self.store.connection.execute(
            "SELECT tool_name,category,provenance,terminal_state,completeness "
            "FROM tool_spans ORDER BY source_ordinal"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("unknown", "tool", "lower_bound", "unknown", "complete"),
            ("view_image", "tool", "exact", "unknown", "complete"),
            ("hydra", "instrumentation", "exact", "unknown", "complete"),
            ("mcp", "tool", "exact", "success", "complete"),
            ("web", "web", "exact", "success", "complete"),
            ("exec_command", "tool", "exact", "success", "complete"),
        ])
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT operation,relative_path FROM file_observations ORDER BY relative_path"
            )],
            [("read", "assets/preview.png"), ("read", "src/direct.py")],
        )
        self.assertEqual(self.store.count("rollout_test_runs"), 0)
        persisted = "\n".join(self.store.connection.iterdump())
        for private_value in (
            "dehydrate_cache", "private_server", "private-cache.py", "not-a-file.py",
            "private-mcp.py", "private-web.py", "cat direct.py",
        ):
            self.assertNotIn(private_value, persisted)

    def test_failed_shell_and_patch_results_do_not_emit_file_facts(self) -> None:
        write_jsonl(self.root / "rollouts" / "failed-files.jsonl", [
            v1("session_meta", {"id": "anon-failed-files", "cwd": str(self.project)}),
            v1("response_item", {
                "type": "function_call", "call_id": "failed-read", "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat src/does-not-exist.py"}),
            }, 1),
            v1("response_item", {
                "type": "function_call_output", "call_id": "failed-read",
                "output": json.dumps({"exit_code": 1, "stderr": "not found"}),
            }, 2),
            v1("response_item", {
                "type": "function_call", "call_id": "failed-patch", "name": "apply_patch",
                "arguments": json.dumps({
                    "patch": "*** Update File: src/not-written.py\n+not written",
                }),
            }, 3),
            v1("event_msg", {
                "type": "patch_apply_end", "call_id": "failed-patch", "success": False,
                "changes": {"src/not-written.py": {"type": "modify"}},
            }, 4),
        ])

        ingest_rollouts(
            self.store, (self.root / "rollouts",), self.project, "project-synthetic",
        )

        self.assertEqual(self.store.count("file_observations"), 0)

    def test_direct_file_facts_use_only_matching_success_terminal_timestamp(self) -> None:
        write_jsonl(self.root / "rollouts" / "successful-files.jsonl", [
            v1("session_meta", {"id": "anon-successful-files", "cwd": str(self.project)}),
            # Output-first records are allowed, but still require an exact exit code.
            v1("response_item", {
                "type": "function_call_output", "call_id": "late-read",
                "output": json.dumps({"exit_code": 0}),
            }, 3),
            v1("response_item", {
                "type": "function_call", "call_id": "late-read", "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat src/late.py"}),
            }, 2),
            v1("response_item", {
                "type": "function_call", "call_id": "unstructured", "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat src/unstructured.py"}),
            }, 4),
            v1("response_item", {
                "type": "function_call_output", "call_id": "unstructured",
                "output": "Script completed without a structured exit",
            }, 5),
            v1("response_item", {
                "type": "function_call", "call_id": "successful-patch", "name": "apply_patch",
                "arguments": json.dumps({
                    "patch": "*** Update File: src/patched.py\n+patched",
                }),
            }, 6),
            v1("event_msg", {
                "type": "patch_apply_end", "call_id": "successful-patch", "success": True,
                "changes": {"src/patched.py": {"type": "modify"}},
            }, 7),
        ])

        ingest_rollouts(
            self.store, (self.root / "rollouts",), self.project, "project-synthetic",
        )

        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT operation,relative_path,observed_at,line_number "
                "FROM file_observations ORDER BY relative_path"
            )],
            [
                ("read", "src/late.py", "2026-07-20T00:00:03Z", 0),
                ("write", "src/patched.py", "2026-07-20T00:00:07Z", 0),
            ],
        )

    def test_tool_span_persistence_joins_both_event_orders_and_creates_end_only_rows(self) -> None:
        write_jsonl(self.root / "rollouts" / "tool-orders.jsonl", [
            v1("session_meta", {"id": "anon-tool-orders", "cwd": str(self.project)}),
            v1("turn_context", {"turn_id": "turn-tools"}, 1),
            v1("response_item", {"type": "function_call_output", "call_id": "late-start", "output": "safe"}, 2),
            v1("response_item", {"type": "function_call", "call_id": "late-start", "name": "exec_command", "arguments": "{}"}, 3),
            v1("event_msg", {"type": "mcp_tool_call_end", "call_id": "mcp-only", "result": {"Ok": {}}}, 4),
            v1("event_msg", {"type": "patch_apply_end", "call_id": "patch-only", "success": False}, 5),
            v1("event_msg", {"type": "web_search_end", "call_id": "web-only", "result": {"Ok": {}}}, 6),
            v1("response_item", {"type": "function_call_output", "call_id": "patch-only", "output": "safe"}, 7),
        ])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic", hash_key=b"j" * 32)

        rows = self.store.connection.execute(
            "SELECT tool_name, started_at, finished_at, turn_key, category, terminal_state, completeness, provenance "
            "FROM tool_spans ORDER BY source_ordinal"
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual([(row[0], row[1], row[2], row[4], row[5], row[6], row[7]) for row in rows], [
            ("exec_command", "2026-07-20T00:00:03Z", "2026-07-20T00:00:02Z", "tool", "unknown", "complete", "exact"),
            ("mcp", None, "2026-07-20T00:00:04Z", "tool", "success", "incomplete", "exact"),
            ("patch", None, "2026-07-20T00:00:05Z", "tool", "failed", "incomplete", "exact"),
            ("web", None, "2026-07-20T00:00:06Z", "web", "success", "incomplete", "exact"),
        ])
        self.assertEqual(len({row[3] for row in rows}), 1)

    def test_custom_exec_wrapper_joins_in_both_orders_without_promoting_nested_spans(self) -> None:
        raw_program = ('tools.exec_command({"cmd": "pytest tests/safe.py"}); '
                       'tools.exec_command({"cmd": "pytest tests/also-safe.py"}); '
                       'tools.apply_patch("*** Update File: src/safe.py\\n+safe");')
        raw_output = "Script completed\nWall time: 0.025 seconds"
        write_jsonl(self.root / "rollouts" / "custom-exec.jsonl", [
            v1("session_meta", {"id": "anon-custom", "cwd": str(self.project)}),
            v1("turn_context", {"turn_id": "turn-custom"}, 1),
            v1("response_item", {"type": "custom_tool_call", "call_id": "exec-first", "name": "exec", "input": raw_program}, 2),
            v1("response_item", {"type": "custom_tool_call_output", "call_id": "exec-first", "output": raw_output}, 3),
            v1("response_item", {"type": "custom_tool_call_output", "call_id": "output-first", "output": [{"type": "input_text", "text": "Script completed\nWall time: 0.010 seconds"}]}, 4),
            v1("response_item", {"type": "custom_tool_call", "call_id": "output-first", "name": "exec", "input": 'tools.exec_command({"cmd": "pytest tests/other.py"});'}, 5),
            v1("response_item", {"type": "custom_tool_call", "call_id": "failed", "name": "exec", "input": 'tools.exec_command({"cmd": "pytest tests/fail.py"});'}, 6),
            v1("response_item", {"type": "custom_tool_call_output", "call_id": "failed", "output": [{"type": "input_text", "text": "Script failed with exit code 1"}]}, 7),
        ])

        ingest_rollouts(self.store, (self.root / "rollouts",), self.project, "project-synthetic", hash_key=b"n" * 32)

        rows = self.store.connection.execute(
            "SELECT tool_name, terminal_state, latency_ms, completeness, provenance FROM tool_spans ORDER BY source_ordinal, tool_name"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("apply_patch", "unknown", None, "incomplete", "lower_bound"),
            ("custom_exec", "unknown", None, "complete", "exact"),
            ("exec_command", "unknown", None, "incomplete", "lower_bound"),
            ("exec_command", "unknown", None, "incomplete", "lower_bound"),
            ("custom_exec", "success", 10, "complete", "exact"),
            ("exec_command", "unknown", None, "incomplete", "lower_bound"),
            ("custom_exec", "failed", None, "complete", "exact"),
            ("exec_command", "unknown", None, "incomplete", "lower_bound"),
        ])
        self.assertNotIn(raw_program, "\n".join(self.store.connection.iterdump()))
        self.assertNotIn(raw_output, "\n".join(self.store.connection.iterdump()))
        self.assertEqual(self.store.count("file_observations"), 0)

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

    @staticmethod
    def prepared_scan(source: Path, key: bytes):
        from hydra_codex.rollout_identity import Pseudonymizer
        from hydra_codex.rollout_sources import scan_source

        return scan_source(source, key, Pseudonymizer(key).digest)

    def test_prepared_ingest_uses_exact_scans_without_legacy_discovery_or_rescan(self) -> None:
        key = b"p" * 32
        source = self.root / "active" / "thread.jsonl"
        write_jsonl(source, [v1("session_meta", {"id": "prepared", "cwd": str(self.project)})])
        scan = self.prepared_scan(source, key)
        progress: list[tuple[str, int, int]] = []

        with (
            patch(
                "hydra_codex.rollout.discover_rollouts",
                side_effect=AssertionError("prepared ingest must not use legacy discovery"),
            ),
            patch(
                "hydra_codex.rollout.scan_source",
                side_effect=AssertionError("prepared ingest must not rescan"),
            ),
        ):
            report = ingest_rollouts(
                self.store,
                (RolloutRoot(source.resolve(), "active"),),
                self.project,
                "project-synthetic",
                hash_key=key,
                progress=lambda stage, current, total: progress.append(
                    (stage, current, total)
                ),
                prepared_scans={source.resolve(): scan},
            )

        self.assertEqual((report.files_seen, report.unique_sources), (1, 1))
        self.assertEqual(
            self.store.connection.execute(
                "SELECT location_type FROM rollout_source_locations"
            ).fetchone()[0],
            "active",
        )
        self.assertTrue(progress)
        self.assertTrue(all(
            stage in {"inspect", "reconcile"}
            and isinstance(current, int)
            and isinstance(total, int)
            and 0 <= current <= total
            for stage, current, total in progress
        ))
        self.assertNotIn(str(source), repr(progress))

    def test_prepared_ingest_rejects_missing_extra_noncanonical_and_explicit_sets(self) -> None:
        from hydra_codex.rollout_identity import ACTIVE_HASHER

        key = b"q" * 32
        first = self.root / "active" / "first.jsonl"
        second = self.root / "active" / "second.jsonl"
        write_jsonl(first, [v1("session_meta", {"id": "first", "cwd": str(self.project)})])
        write_jsonl(second, [v1("session_meta", {"id": "second", "cwd": str(self.project)})])
        first_scan = self.prepared_scan(first, key)
        second_scan = self.prepared_scan(second, key)
        cases = (
            (
                (
                    RolloutRoot(first.resolve(), "active"),
                    RolloutRoot(second.resolve(), "active"),
                ),
                {first.resolve(): first_scan},
            ),
            (
                (RolloutRoot(first.resolve(), "active"),),
                {first.resolve(): first_scan, second.resolve(): second_scan},
            ),
            (
                (RolloutRoot(first.resolve(), "active"),),
                {first.parent / "nested" / ".." / first.name: first_scan},
            ),
            (
                (RolloutRoot(first.resolve(), "explicit"),),
                {first.resolve(): first_scan},
            ),
        )

        for roots, prepared in cases:
            with self.subTest(roots=roots, keys=tuple(prepared)), self.assertRaises(ValueError):
                ingest_rollouts(
                    self.store,
                    roots,
                    self.project,
                    "project-synthetic",
                    hash_key=key,
                    prepared_scans=prepared,
                )
            self.assertIsNone(ACTIVE_HASHER.get())
        self.assertEqual(self.store.count("rollout_sources"), 0)

    def test_prepared_scan_is_bound_to_its_canonical_path_even_for_a_hardlink(self) -> None:
        key = b"u" * 32
        original = self.root / "active" / "original.jsonl"
        alias = self.root / "active" / "alias.jsonl"
        write_jsonl(original, [v1("session_meta", {"id": "bound", "cwd": str(self.project)})])
        os.link(original, alias)
        scan = self.prepared_scan(original, key)

        with self.assertRaises(ValueError):
            ingest_rollouts(
                self.store, (RolloutRoot(alias.resolve(), "active"),), self.project,
                "project-synthetic", hash_key=key,
                prepared_scans={alias.resolve(): scan},
            )
        self.assertEqual(self.store.count("rollout_sources"), 0)

    def test_prepared_unchanged_and_known_fast_paths_revalidate_source_stat(self) -> None:
        from hydra_codex.rollout_sources import SourceChanged

        key = b"r" * 32
        active = self.root / "active" / "thread.jsonl"
        archived = self.root / "archived" / "thread.jsonl"
        rows = [v1("session_meta", {"id": "stat-bound", "cwd": str(self.project)})]
        write_jsonl(active, rows)
        ingest_rollouts(
            self.store, (RolloutRoot(active, "active"),), self.project,
            "project-synthetic", hash_key=key,
        )

        unchanged_scan = self.prepared_scan(active, key)
        from hydra_codex import rollout as rollout_module

        original_unchanged = rollout_module._unchanged_location

        def touch_after_unchanged(*args, **kwargs):
            result = original_unchanged(*args, **kwargs)
            details = active.stat()
            os.utime(active, ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000))
            return result

        with (
            patch(
                "hydra_codex.rollout._unchanged_location",
                side_effect=touch_after_unchanged,
            ),
            self.assertRaises(SourceChanged),
        ):
            ingest_rollouts(
                self.store, (RolloutRoot(active.resolve(), "active"),), self.project,
                "project-synthetic", hash_key=key,
                prepared_scans={active.resolve(): unchanged_scan},
            )

        write_jsonl(active, rows)
        write_jsonl(archived, rows)
        known_scan = self.prepared_scan(archived, key)

        def touch_before_known(*args, **kwargs):
            result = original_unchanged(*args, **kwargs)
            details = archived.stat()
            os.utime(
                archived,
                ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000),
            )
            return result

        with (
            patch(
                "hydra_codex.rollout._unchanged_location",
                side_effect=touch_before_known,
            ),
            self.assertRaises(SourceChanged),
        ):
            ingest_rollouts(
                self.store, (RolloutRoot(archived.resolve(), "archived"),), self.project,
                "project-synthetic", hash_key=key,
                prepared_scans={archived.resolve(): known_scan},
            )

    def test_prepared_ingest_preserves_archive_label_on_unchanged_source(self) -> None:
        key = b"v" * 32
        source = self.root / "thread.jsonl"
        write_jsonl(source, [v1("session_meta", {"id": "label", "cwd": str(self.project)})])
        scan = self.prepared_scan(source, key)
        ingest_rollouts(
            self.store, (RolloutRoot(source.resolve(), "active"),), self.project,
            "project-synthetic", hash_key=key,
            prepared_scans={source.resolve(): scan},
        )
        ingest_rollouts(
            self.store, (RolloutRoot(source.resolve(), "archived"),), self.project,
            "project-synthetic", hash_key=key,
            prepared_scans={source.resolve(): scan},
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT location_type FROM rollout_source_locations"
            ).fetchone()[0],
            "archived",
        )

    def test_prepared_materialization_rejects_swap_at_descriptor_open(self) -> None:
        from hydra_codex.rollout_sources import SourceChanged

        key = b"x" * 32
        source = self.root / "active" / "materialize.jsonl"
        replacement = self.root / "external" / "materialize.jsonl"
        write_jsonl(source, [v1("session_meta", {"id": "expected", "cwd": str(self.project)})])
        write_jsonl(replacement, [v1("session_meta", {"id": "replacement", "cwd": str(self.project)})])
        scan = self.prepared_scan(source, key)
        canonical_source = source.resolve()
        original_open = os.open
        swapped = False

        def swap_then_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if Path(path) == canonical_source and not swapped:
                swapped = True
                source.unlink()
                source.symlink_to(replacement)
            return original_open(path, flags, *args, **kwargs)

        with (
            patch("os.open", side_effect=swap_then_open),
            self.assertRaises(SourceChanged),
        ):
            ingest_rollouts(
                self.store, (RolloutRoot(canonical_source, "active"),), self.project,
                "project-synthetic", hash_key=key,
                prepared_scans={canonical_source: scan},
            )
        self.assertEqual(self.store.count("rollout_sources"), 0)

    def test_global_unchanged_attribution_is_private_and_ambiguity_fails_closed(self) -> None:
        from hydra_codex.rollout import (
            SOURCE_SCANNER_VERSION,
            UnchangedLocationAttribution,
            unchanged_location_attribution,
        )
        from hydra_codex.rollout_identity import Pseudonymizer
        from hydra_codex.rollout_sources import source_stat

        key = b"w" * 32
        source = self.root / "active" / "attribution.jsonl"
        write_jsonl(source, [v1("session_meta", {"id": "attribution", "cwd": str(self.project)})])
        ingest_rollouts(
            self.store, (RolloutRoot(source, "active"),), self.project,
            "project-synthetic", hash_key=key,
        )
        location = Pseudonymizer(key).digest("source", str(source.resolve()))
        stat_value = source_stat(source)

        attribution = unchanged_location_attribution(
            self.store.connection, location, stat_value,
        )

        self.assertIsInstance(attribution, UnchangedLocationAttribution)
        assert attribution is not None
        self.assertEqual(attribution.project_id, "project-synthetic")
        self.assertEqual(len(tuple(attribution)), 3)
        rendered = repr(attribution)
        for private_value in (attribution.project_id, attribution.logical, attribution.revision):
            self.assertNotIn(private_value, rendered)

        other_logical = "private-other-logical"
        other_revision = "private-other-revision"
        self.store.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES (?, 'project-other', NULL, ?, 'clean')""",
            (other_logical, other_revision),
        )
        self.store.connection.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key,relation,
                   line_count,byte_count,chain_digest,materialized)
               VALUES (?,'jsonl',?,'initial',0,0,'safe',1)""",
            (other_revision, other_logical),
        )
        self.store.connection.execute(
            """INSERT INTO rollout_source_locations(
                   logical_source_key,location_key,location_type,revision_digest)
               VALUES (?,?,'active',?)""",
            (other_logical, location, other_revision),
        )
        self.store.connection.execute(
            """INSERT INTO rollout_source_location_states(
                   project_id,location_key,logical_source_key,revision_digest,
                   st_dev,st_ino,st_size,st_mtime_ns,st_ctime_ns,scanner_version)
               VALUES ('project-other',?,?,?,?,?,?,?,?,?)""",
            (
                location, other_logical, other_revision,
                stat_value.dev, stat_value.ino, stat_value.size,
                stat_value.mtime_ns, stat_value.ctime_ns,
                SOURCE_SCANNER_VERSION,
            ),
        )
        self.store.connection.commit()

        self.assertIsNone(unchanged_location_attribution(
            self.store.connection, location, stat_value,
        ))
