from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from hydra_codex.cli import main
from hydra_codex.codex_event_ingest import (
    CodexEventSource,
    ingest_codex_events,
)
from hydra_codex.codex_events import APP_SERVER_V2, OTEL_LOG_V1
from hydra_codex.reconcile_engine import list_reconciled_tasks, reconcile_project
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import MIGRATIONS, HydraStore, StorageUnavailable
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


FIXTURES = Path(__file__).parent / "fixtures" / "codex_events"
KEY = b"event-adapter-fixture-key-000001"
PROJECT = "event-ingest-project"


def _project(base: Path) -> Path:
    project = base / "project"
    (project / ".hydra").mkdir(parents=True)
    (project / ".hydra" / "project.toml").write_text(
        f'project_id = "{PROJECT}"\n', encoding="utf-8",
    )
    return project


def _otel_for_app_thread(base: Path) -> Path:
    value = (FIXTURES / "otel_log_v1.jsonl").read_text(encoding="utf-8")
    value = value.replace("fixture-thread-b", "fixture-thread-a")
    value = value.replace("fixture-turn-b", "fixture-turn-a")
    value = value.replace("fixture-call-b", "fixture-call-a")
    target = base / "matching-otel.jsonl"
    target.write_text(value, encoding="utf-8")
    return target


class CodexEventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = _project(self.base)
        self.store = HydraStore(self.base / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def ingest(self, *sources: CodexEventSource):
        return ingest_codex_events(
            self.store, sources, self.project, PROJECT, hash_key=KEY,
        )

    def app_source(self, name: str, event: dict[str, object]) -> CodexEventSource:
        path = self.base / f"{name}.jsonl"
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return CodexEventSource(path, APP_SERVER_V2)

    def test_v21_schema_persists_content_identity_locations_and_safe_facts_idempotently(self) -> None:
        original = FIXTURES / "app_server_v2.jsonl"
        first = self.base / "first.jsonl"
        copied = self.base / "copied.jsonl"
        shutil.copyfile(original, first)
        shutil.copyfile(original, copied)

        initial = self.ingest(CodexEventSource(first, APP_SERVER_V2))
        repeated = self.ingest(
            CodexEventSource(copied, APP_SERVER_V2),
            CodexEventSource(first, APP_SERVER_V2),
        )

        self.assertGreaterEqual(MIGRATIONS[-1][0], 21)
        self.assertEqual(
            (initial.files_seen, initial.unique_sources, initial.events, initial.issues),
            (1, 1, 6, 0),
        )
        self.assertEqual(
            (repeated.files_seen, repeated.unique_sources, repeated.events, repeated.issues),
            (2, 1, 6, 0),
        )
        counts = {
            table: self.store.count(table)
            for table in (
                "codex_event_sources", "codex_event_source_locations", "codex_events",
                "codex_event_contents", "codex_event_tokens", "codex_event_issues",
            )
        }
        self.assertEqual(counts["codex_event_sources"], 1)
        self.assertEqual(counts["codex_event_source_locations"], 2)
        self.assertEqual(counts["codex_events"], 6)
        self.assertGreater(counts["codex_event_contents"], 0)
        self.assertEqual(counts["codex_event_tokens"], 2)
        self.assertEqual(counts["codex_event_issues"], 0)
        ordinals = [row[0] for row in self.store.connection.execute(
            "SELECT source_ordinal FROM codex_events ORDER BY source_ordinal"
        )]
        self.assertEqual(ordinals, [1, 2, 3, 4, 5, 6])

        dump = "\n".join(self.store.connection.iterdump())
        for private in (
            str(first), str(copied), "fixture-thread-a", "fixture-turn-a",
            "python -m unittest", "fixture test output", "anonymized assistant fixture",
        ):
            self.assertNotIn(private, dump)

    def test_source_fingerprinting_streams_without_path_read_bytes(self) -> None:
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            report = self.ingest(
                CodexEventSource(FIXTURES / "app_server_v2.jsonl", APP_SERVER_V2),
            )
        self.assertEqual((report.files_seen, report.events), (1, 6))

    def test_app_lifecycle_total_and_tool_are_visible_to_reconcile_without_semantic_guessing(self) -> None:
        self.ingest(CodexEventSource(FIXTURES / "app_server_v2.jsonl", APP_SERVER_V2))
        root = Pseudonymizer(KEY).digest("identity", "fixture-thread-a")

        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
        )
        self.assertEqual(metrics.recorded.working.value, 90)
        self.assertEqual(metrics.recorded.full.value, 130)
        self.assertEqual(metrics.recorded.reasoning.value, 5)
        self.assertEqual(metrics.tool_calls.value, 1)
        self.assertIn("app_total_timestamp_missing", metrics.recorded.caveats)

        summary = reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual((summary.task_count, summary.complete_count), (1, 1))
        self.assertEqual(task.metrics.recorded.working.value, 90)
        self.assertEqual(task.semantic.coverage.value, 0.0)
        self.assertEqual(task.semantic.unclassified_working.value, 90)

    def test_split_app_sources_reverse_order_keep_only_latest_cumulative_total(self) -> None:
        thread, turn = "split-thread", "split-turn"

        def usage(value: int) -> dict[str, object]:
            totals = {
                "inputTokens": value, "cachedInputTokens": value // 10,
                "outputTokens": value // 5, "reasoningOutputTokens": value // 20,
                "totalTokens": value + value // 5,
            }
            return {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread, "turnId": turn,
                    "tokenUsage": {"total": totals, "last": totals},
                },
            }

        sources = (
            self.app_source("complete", {
                "method": "turn/completed", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "completedAt": 1720000002, "status": "completed",
                    },
                },
            }),
            self.app_source("total-200", usage(200)),
            self.app_source("total-100", usage(100)),
            self.app_source("start", {
                "method": "turn/started", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "startedAt": 1720000000, "status": "inProgress",
                    },
                },
            }),
        )
        self.ingest(*sources)
        root = Pseudonymizer(KEY).digest("identity", thread)

        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
        )
        self.assertEqual(metrics.recorded.input.value, 200)
        self.assertEqual(metrics.recorded.working.value, 220)
        self.assertEqual(metrics.recorded.full.value, 240)
        selected = list(self.store.connection.execute(
            """SELECT input_tokens,contributes_total FROM token_snapshots
                 WHERE session_key=? ORDER BY input_tokens""",
            (root,),
        ))
        self.assertEqual([(row[0], row[1]) for row in selected], [(100, 0), (200, 1)])

    def test_incomplete_app_stream_retains_timestamp_missing_total_as_unclassified(self) -> None:
        thread, turn = "incomplete-app-thread", "incomplete-app-turn"
        totals = {
            "inputTokens": 80, "cachedInputTokens": 20, "outputTokens": 15,
            "reasoningOutputTokens": 3, "totalTokens": 95,
        }
        self.ingest(
            self.app_source("incomplete-total", {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread, "turnId": turn,
                    "tokenUsage": {"total": totals, "last": totals},
                },
            }),
            self.app_source("incomplete-start", {
                "method": "turn/started", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "startedAt": 1720000000, "status": "inProgress",
                    },
                },
            }),
        )
        root = Pseudonymizer(KEY).digest("identity", thread)
        cutoff = datetime(2024, 7, 3, 9, 46, 41, tzinfo=timezone.utc)
        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root, cutoff_at=cutoff,
        )
        self.assertEqual(metrics.recorded.working.value, 75)
        self.assertIn("app_total_timestamp_missing", metrics.recorded.caveats)

        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.status, "incomplete")
        self.assertEqual(task.semantic.unclassified_working.value, 75)

    def test_app_turn_started_after_completion_makes_current_cumulative_task_incomplete(self) -> None:
        thread = "resumed-app-thread"

        def turn(method: str, turn_id: str, timestamp: int) -> dict[str, object]:
            field = "startedAt" if method == "turn/started" else "completedAt"
            return {
                "method": method, "params": {
                    "threadId": thread, "turn": {
                        "id": turn_id, field: timestamp,
                        "status": "inProgress" if method == "turn/started" else "completed",
                    },
                },
            }

        def usage(turn_id: str, value: int) -> dict[str, object]:
            totals = {
                "inputTokens": value, "cachedInputTokens": value // 10,
                "outputTokens": value // 5, "reasoningOutputTokens": value // 20,
                "totalTokens": value + value // 5,
            }
            return {
                "method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread, "turnId": turn_id,
                    "tokenUsage": {"total": totals, "last": totals},
                },
            }

        source = self.base / "resumed.jsonl"
        source.write_text("".join(
            json.dumps(event) + "\n" for event in (
                turn("turn/started", "first", 1720000000), usage("first", 100),
                turn("turn/completed", "first", 1720000002),
                turn("turn/started", "second", 1720000003), usage("second", 200),
            )
        ), encoding="utf-8")
        self.ingest(CodexEventSource(source, APP_SERVER_V2))

        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.status, "incomplete")
        self.assertEqual(task.metrics.recorded.input.value, 200)
        self.assertEqual(task.semantic.unclassified_working.value, 220)

    def test_same_second_split_source_later_turn_uses_turn_identity_to_remain_incomplete(self) -> None:
        thread = "same-second-resume"

        def totals(value: int) -> dict[str, int]:
            return {
                "inputTokens": value, "cachedInputTokens": value // 10,
                "outputTokens": value // 5, "reasoningOutputTokens": value // 20,
                "totalTokens": value + value // 5,
            }

        events = (
            {"method": "turn/started", "params": {"threadId": thread, "turn": {
                "id": "first", "startedAt": 1720000000, "status": "inProgress"}}},
            {"method": "thread/tokenUsage/updated", "params": {
                "threadId": thread, "turnId": "first",
                "tokenUsage": {"total": totals(100), "last": totals(100)}}},
            {"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": "first", "completedAt": 1720000002, "status": "completed"}}},
            {"method": "turn/started", "params": {"threadId": thread, "turn": {
                "id": "second", "startedAt": 1720000002, "status": "inProgress"}}},
            {"method": "thread/tokenUsage/updated", "params": {
                "threadId": thread, "turnId": "second",
                "tokenUsage": {"total": totals(200), "last": totals(200)}}},
        )
        self.ingest(*(
            self.app_source(f"same-second-{index}", event)
            for index, event in enumerate(events)
        ))

        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.status, "incomplete")
        self.assertEqual(task.metrics.recorded.input.value, 200)

    def test_same_turn_equal_start_and_completion_timestamp_stays_complete(self) -> None:
        thread, turn = "same-turn-thread", "same-turn"
        totals = {
            "inputTokens": 50, "cachedInputTokens": 10, "outputTokens": 10,
            "reasoningOutputTokens": 2, "totalTokens": 60,
        }
        self.ingest(*(
            self.app_source(name, event) for name, event in (
                ("same-turn-start", {"method": "turn/started", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "startedAt": 1720000002, "status": "inProgress"}}}),
                ("same-turn-total", {"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread, "turnId": turn,
                    "tokenUsage": {"total": totals, "last": totals}}}),
                ("same-turn-complete", {"method": "turn/completed", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "completedAt": 1720000002, "status": "completed"}}}),
            )
        ))
        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        self.assertEqual(list_reconciled_tasks(self.store, project_id=PROJECT)[0].status, "complete")

    def test_mixed_app_and_otel_selects_app_total_but_retains_otel_as_allocation_hint(self) -> None:
        otel = _otel_for_app_thread(self.base)
        self.ingest(
            CodexEventSource(otel, OTEL_LOG_V1),
            CodexEventSource(FIXTURES / "app_server_v2.jsonl", APP_SERVER_V2),
        )
        root = Pseudonymizer(KEY).digest("identity", "fixture-thread-a")
        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
        )

        self.assertEqual(metrics.recorded.working.value, 90)
        self.assertNotEqual(metrics.recorded.working.value, 170)
        self.assertIn("app_cumulative_preferred_over_otel", metrics.recorded.caveats)
        selections = list(self.store.connection.execute(
            """SELECT source_family,contributes_total,selection_caveat
                 FROM token_snapshots WHERE session_key=? ORDER BY source_family""",
            (root,),
        ))
        self.assertEqual(
            [(row[0], row[1]) for row in selections],
            [("app_server", 1), ("otel", 0)],
        )
        self.assertEqual(selections[0][2], "app_cumulative_preferred_over_otel")
        hint = self.store.connection.execute(
            """SELECT observed_at,input_tokens,cached_input_tokens,output_tokens
                 FROM token_snapshots WHERE session_key=? AND source_family='otel'""",
            (root,),
        ).fetchone()
        self.assertIsNotNone(hint[0])
        self.assertEqual(tuple(hint[1:]), (90, 30, 20))

    def test_timestamped_otel_call_allocates_app_total_without_adding_to_it(self) -> None:
        self.ingest(
            CodexEventSource(_otel_for_app_thread(self.base), OTEL_LOG_V1),
            CodexEventSource(FIXTURES / "app_server_v2.jsonl", APP_SERVER_V2),
        )
        root = Pseudonymizer(KEY).digest("identity", "fixture-thread-a")
        turn = "semantic-turn"
        self.store.connection.execute(
            """INSERT INTO sessions(session_id,project_id,worktree_path,started_at,provenance)
               VALUES (?,?,?,'2024-07-03T09:46:40Z','exact')""",
            (root, PROJECT, "safe"),
        )
        self.store.connection.execute(
            """INSERT INTO turns(turn_id,session_id,ordinal,observed_at,provenance)
               VALUES (?,?,0,'2024-07-03T09:46:40Z','exact')""",
            (turn, root),
        )
        self.store.connection.execute(
            """INSERT INTO trusted_turn_bindings(
                   turn_key,project_id,session_key,created_at,state,last_sequence)
               VALUES ('binding',?,?,'2024-07-03T09:46:40Z','open',0)""",
            (PROJECT, root),
        )
        self.store.connection.execute(
            """INSERT INTO annotations(
                   annotation_id,project_id,session_id,turn_id,sequence,observed_at,kind,
                   phase,cause,scope_change,task_family,confidence,outcome,provenance,
                   note_redacted,note_hash,note_length)
               VALUES ('annotation',?,?,?,0,'2024-07-03T09:46:40.500000Z','phase',
                       'implement','plan','none','event-ingest',1.0,NULL,
                       'model_reported','safe','hash',4)""",
            (PROJECT, root, turn),
        )
        self.store.connection.execute(
            """INSERT INTO semantic_intervals(
                   interval_key,project_id,session_key,turn_key,start_annotation_id,
                   start_sequence,started_at,ended_at,phase,cause,provenance)
               VALUES ('interval',?,?,'binding','annotation',0,
                       '2024-07-03T09:46:40.500000Z','2024-07-03T09:46:41Z',
                       'implement','plan','model_reported')""",
            (PROJECT, root),
        )

        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.metrics.recorded.working.value, 90)
        self.assertEqual(task.semantic.phase_working["implement"].value, 80)
        self.assertEqual(task.semantic.unclassified_working.value, 10)
        self.assertEqual(
            sum(row[0] for row in self.store.connection.execute(
                "SELECT working_tokens FROM reconciled_token_deltas WHERE working_tokens IS NOT NULL"
            )),
            90,
        )

    def test_otel_only_is_an_estimated_timestamped_fallback_and_incomplete_task(self) -> None:
        self.ingest(CodexEventSource(FIXTURES / "otel_log_v1.jsonl", OTEL_LOG_V1))
        root = Pseudonymizer(KEY).digest("identity", "fixture-thread-b")
        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]

        self.assertEqual(task.status, "incomplete")
        self.assertEqual(task.metrics.recorded.working.value, 80)
        self.assertEqual(task.metrics.recorded.full.value, 110)
        self.assertEqual(task.metrics.recorded.reasoning.value, 4)
        self.assertEqual(task.metrics.recorded.provenance, "estimated")
        self.assertIn("otel_per_call_fallback", task.metrics.recorded.caveats)
        self.assertEqual(task.metrics.tool_calls.value, 1)
        self.assertEqual(
            [row[0] for row in self.store.connection.execute(
                "SELECT observed_at FROM codex_events ORDER BY source_ordinal"
            )],
            [
                "2024-07-03T09:46:40Z", "2024-07-03T09:46:41Z",
                "2024-07-03T09:46:40.900000Z", "2024-07-03T09:46:42Z",
            ],
        )

    def test_rollout_family_remains_authoritative_when_app_and_otel_exist(self) -> None:
        session = Pseudonymizer(KEY).digest("identity", "fixture-thread-a")
        self.store.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,conversation_key,started_at)
               VALUES (?,?,?,1,'','2024-07-03T09:46:40Z')""",
            (session, PROJECT, "worktree"),
        )
        self.store.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,lineage_state)
               VALUES ('rollout-logical',?,?,'clean')""",
            (PROJECT, session),
        )
        self.store.connection.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key,relation,line_count,
                   byte_count,chain_digest,materialized)
               VALUES ('rollout-source','explicit','rollout-logical','canonical',1,1,'chain',1)"""
        )
        self.store.connection.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                   completeness,observed_at)
               VALUES ('rollout-source',1,?,?,0,50,10,10,2,0,'complete','2024-07-03T09:46:41Z')""",
            (session, PROJECT),
        )
        self.ingest(
            CodexEventSource(_otel_for_app_thread(self.base), OTEL_LOG_V1),
            CodexEventSource(FIXTURES / "app_server_v2.jsonl", APP_SERVER_V2),
        )
        selected = list(self.store.connection.execute(
            """SELECT source_family,contributes_total FROM token_snapshots
                 WHERE session_key=? ORDER BY source_family""",
            (session,),
        ))
        self.assertEqual(
            [(row[0], row[1]) for row in selected],
            [("app_server", 0), ("otel", 0), ("rollout", 1)],
        )

    def test_malformed_and_schema_drift_persist_only_safe_issues(self) -> None:
        malformed = self.base / "malformed.jsonl"
        malformed.write_text(
            "not-json-private\n" + json.dumps({
                "method": "new/private/method", "params": {"private": "secret"},
            }) + "\n",
            encoding="utf-8",
        )
        report = self.ingest(CodexEventSource(malformed, APP_SERVER_V2))

        self.assertEqual((report.events, report.issues), (0, 2))
        self.assertEqual(
            [row[0] for row in self.store.connection.execute(
                "SELECT issue_code FROM codex_event_issues ORDER BY source_ordinal"
            )],
            ["malformed_json", "unsupported_envelope"],
        )
        dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn("not-json-private", dump)
        self.assertNotIn("secret", dump)

    def test_schema_validation_rejects_missing_event_fact_column(self) -> None:
        database = self.store.database_path
        self.store.close()
        connection = sqlite3.connect(database)
        try:
            connection.execute("ALTER TABLE codex_event_tokens DROP COLUMN output_tokens")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(StorageUnavailable, "missing required columns"):
            HydraStore(database)


class CodexEventCliTests(unittest.TestCase):
    def test_cli_accepts_named_event_sources_and_reports_separate_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = _project(base)
            stdout, stderr = io.StringIO(), io.StringIO()
            key_path = base / "keys" / "privacy.key"
            code = main(
                [
                    "ingest", "--cwd", str(project), "--db", str(base / "hydra.sqlite3"),
                    "--event-source", f"app-server-v2={FIXTURES / 'app_server_v2.jsonl'}",
                    "--event-source", f"otel-v1={FIXTURES / 'otel_log_v1.jsonl'}",
                ],
                stdin=io.StringIO(), stdout=stdout, stderr=stderr,
                environ={"HOME": str(base / "home")}, installation_key_path=key_path,
            )

            self.assertEqual((code, stderr.getvalue()), (0, ""))
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["event_files_seen"], 2)
            self.assertEqual(payload["event_unique_sources"], 2)
            self.assertEqual(payload["event_count"], 10)
            self.assertEqual(payload["event_issues"], 0)


if __name__ == "__main__":
    unittest.main()
