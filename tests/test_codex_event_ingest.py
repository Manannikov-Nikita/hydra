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
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.rollout_reconcile import reconcile_token_epochs
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
        self.assertEqual(metrics.test_runs.value, 1)
        self.assertEqual(metrics.targeted_test_runs.value, 1)
        self.assertNotIn("app_total_timestamp_missing", metrics.recorded.caveats)
        self.assertEqual(metrics.recorded.provenance, "exact")

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

    def test_timestamped_app_totals_remain_available_at_the_task_cutoff(self) -> None:
        thread, turn = "temporal-thread", "temporal-turn"

        def usage(value: int) -> dict[str, object]:
            totals = {
                "inputTokens": value, "cachedInputTokens": 0,
                "outputTokens": 0, "reasoningOutputTokens": 0,
                "totalTokens": value,
            }
            return {
                "method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread, "turnId": turn,
                    "tokenUsage": {"total": totals, "last": totals},
                },
            }

        source = self.base / "temporal-app-totals.jsonl"
        rows = (
            {"received_at": "2024-07-03T09:46:40Z", "message": {
                "method": "turn/started", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "status": "inProgress",
                    },
                },
            }},
            {"received_at": "2024-07-03T09:46:41Z", "message": usage(100)},
            {"received_at": "2024-07-03T09:46:42Z", "message": {
                "method": "turn/completed", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "status": "completed",
                    },
                },
            }},
            {"received_at": "2024-07-03T09:46:43Z", "message": usage(200)},
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )
        event_source = CodexEventSource(source, APP_SERVER_V2)
        self.ingest(event_source)
        self.ingest(event_source)
        root = Pseudonymizer(KEY).digest("identity", thread)

        completed = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
        )
        after_resume = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
            cutoff_at=datetime(2024, 7, 3, 9, 46, 44, tzinfo=timezone.utc),
        )
        selected = list(self.store.connection.execute(
            """SELECT input_tokens,contributes_total FROM token_snapshots
                 WHERE session_key=? ORDER BY input_tokens""",
            (root,),
        ))
        reconcile_project(
            self.store, project_id=PROJECT, installation_key=b"r" * 32,
        )
        reconciled = list_reconciled_tasks(self.store, project_id=PROJECT)[0]

        self.assertEqual(completed.recorded.input.value, 100)
        self.assertEqual(after_resume.recorded.input.value, 200)
        self.assertEqual(reconciled.metrics.recorded.input.value, 100)
        self.assertEqual(
            [(row[0], row[1]) for row in selected], [(100, 1), (200, 1)],
        )

    def test_timestamped_app_counter_reset_creates_a_new_contributing_epoch(self) -> None:
        thread, turn = "reset-thread", "reset-turn"

        def event(timestamp: str, value: int) -> dict[str, object]:
            totals = {
                "inputTokens": value, "cachedInputTokens": 0,
                "outputTokens": 0, "reasoningOutputTokens": 0,
                "totalTokens": value,
            }
            return {
                "received_at": timestamp,
                "message": {
                    "method": "thread/tokenUsage/updated", "params": {
                        "threadId": thread, "turnId": turn,
                        "tokenUsage": {"total": totals, "last": totals},
                    },
                },
            }

        source = self.base / "app-counter-reset.jsonl"
        rows = (
            {"received_at": "2024-07-03T09:46:40Z", "message": {
                "method": "turn/started", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "status": "inProgress",
                    },
                },
            }},
            event("2024-07-03T09:46:41Z", 100),
            event("2024-07-03T09:46:42Z", 150),
            event("2024-07-03T09:46:43Z", 20),
            {"received_at": "2024-07-03T09:46:44Z", "message": {
                "method": "turn/completed", "params": {
                    "threadId": thread, "turn": {
                        "id": turn, "status": "completed",
                    },
                },
            }},
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )
        self.ingest(CodexEventSource(source, APP_SERVER_V2))
        root = Pseudonymizer(KEY).digest("identity", thread)

        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
        )
        epochs = [tuple(row) for row in self.store.connection.execute(
            """SELECT input_tokens,epoch,contributes_total FROM token_snapshots
                 WHERE session_key=? ORDER BY observed_at""",
            (root,),
        )]
        diagnostics = {row[0] for row in self.store.connection.execute(
            "SELECT envelope_kind FROM rollout_diagnostics"
        )}

        self.assertEqual(metrics.recorded.input.value, 170)
        self.assertEqual(epochs, [(100, 0, 1), (150, 0, 1), (20, 1, 1)])
        self.assertIn("counter_reset", diagnostics)

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

    def test_fork_baseline_uses_selected_rollout_instead_of_later_otel_fallback(self) -> None:
        self.ingest(CodexEventSource(_otel_for_app_thread(self.base), OTEL_LOG_V1))
        rollout = self.base / "child-rollout.jsonl"
        records = (
            {
                "timestamp": "2024-07-03T09:46:40Z",
                "type": "session_meta",
                "payload": {
                    "id": "fixture-thread-a", "parent_thread_id": "fixture-parent",
                    "cwd": str(self.project),
                },
            },
            {
                "timestamp": "2024-07-03T09:46:40.500000Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 50,
                    "output_tokens": 20, "reasoning_output_tokens": 10,
                }}},
            },
            {
                "timestamp": "2024-07-03T09:46:41.200000Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 150, "cached_input_tokens": 60,
                    "output_tokens": 30, "reasoning_output_tokens": 15,
                }}},
            },
        )
        rollout.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )

        ingest_rollouts(
            self.store, (rollout,), self.project, PROJECT, hash_key=KEY,
        )

        selected = self.store.connection.execute(
            """SELECT snapshots.source_family,baselines.input_tokens,
                      baselines.cached_input_tokens,baselines.output_tokens,
                      baselines.reasoning_tokens
                 FROM fork_baselines AS baselines
                 JOIN token_snapshots AS snapshots
                   ON snapshots.source_digest=baselines.source_digest
                  AND snapshots.line_number=baselines.line_number"""
        ).fetchone()
        self.assertIsNotNone(selected, [tuple(row) for row in self.store.connection.execute(
            """SELECT sessions.session_key,sessions.started_at,edges.confidence_kind,
                      snapshots.source_family,snapshots.contributes_total,
                      snapshots.observed_at,snapshots.input_tokens
                 FROM rollout_sessions AS sessions
                 LEFT JOIN session_edges AS edges ON edges.child_key=sessions.session_key
                 LEFT JOIN token_snapshots AS snapshots
                   ON snapshots.session_key=sessions.session_key
                ORDER BY snapshots.source_family"""
        )])
        self.assertEqual(tuple(selected), ("rollout", 100, 50, 20, 10))
        session = Pseudonymizer(KEY).digest("identity", "fixture-thread-a")
        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=session,
            cutoff_at=datetime(2024, 7, 3, 9, 46, 41, 200000, tzinfo=timezone.utc),
        )
        self.assertEqual(metrics.recorded.input.value, 150)
        self.assertEqual(
            [row[0] for row in self.store.connection.execute(
                """SELECT epoch FROM token_snapshots
                     WHERE session_key=? AND source_family='rollout'
                     ORDER BY observed_at""",
                (session,),
            )],
            [0, 0],
        )

    def test_otel_model_calls_keep_distinct_epochs_after_cumulative_reconciliation(self) -> None:
        template = json.loads(
            (FIXTURES / "otel_log_v1.jsonl").read_text(encoding="utf-8").splitlines()[2]
        )
        rows: list[dict[str, object]] = []
        for index, input_tokens in enumerate((50, 100), start=1):
            item = json.loads(json.dumps(template))
            item["timeUnixNano"] = str(1720000000000000000 + index * 100000000)
            for attribute in item["attributes"]:
                if attribute["key"] == "input_tokens":
                    attribute["value"]["intValue"] = str(input_tokens)
                elif attribute["key"] == "cached_input_tokens":
                    attribute["value"]["intValue"] = "0"
                elif attribute["key"] == "output_tokens":
                    attribute["value"]["intValue"] = "0"
                elif attribute["key"] == "reasoning_output_tokens":
                    attribute["value"]["intValue"] = "0"
            rows.append(item)
        source = self.base / "two-otel-calls.jsonl"
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )
        self.ingest(CodexEventSource(source, OTEL_LOG_V1))
        reconcile_token_epochs(self.store.connection, PROJECT)

        root = Pseudonymizer(KEY).digest("identity", "fixture-thread-b")
        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
            cutoff_at=datetime(2024, 7, 3, 9, 46, 41, tzinfo=timezone.utc),
        )
        self.assertEqual(metrics.recorded.input.value, 150)
        epochs = [row[0] for row in self.store.connection.execute(
            "SELECT epoch FROM token_snapshots ORDER BY line_number"
        )]
        self.assertEqual(len(set(epochs)), 2)

    def test_timestamp_missing_app_total_after_same_source_completion_is_excluded(self) -> None:
        totals = lambda value: {
            "inputTokens": value, "cachedInputTokens": 0,
            "outputTokens": 0, "reasoningOutputTokens": 0,
            "totalTokens": value,
        }
        source = self.base / "after-completion-total.jsonl"
        rows = (
            {"method": "turn/started", "params": {
                "threadId": "cutoff-thread", "turn": {
                    "id": "cutoff-turn", "startedAt": 1720000000,
                    "status": "inProgress",
                },
            }},
            {"method": "thread/tokenUsage/updated", "params": {
                "threadId": "cutoff-thread", "turnId": "cutoff-turn",
                "tokenUsage": {"total": totals(100), "last": totals(100)},
            }},
            {"method": "turn/completed", "params": {
                "threadId": "cutoff-thread", "turn": {
                    "id": "cutoff-turn", "completedAt": 1720000002,
                    "status": "completed",
                },
            }},
            {"method": "thread/tokenUsage/updated", "params": {
                "threadId": "cutoff-thread", "turnId": "cutoff-turn",
                "tokenUsage": {"total": totals(200), "last": totals(200)},
            }},
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )
        self.ingest(CodexEventSource(source, APP_SERVER_V2))
        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)

        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.status, "complete")
        self.assertIsNone(task.metrics.recorded.input.value)
        self.assertIn(
            "post_cutoff_timestamp_missing_token:1", task.metrics.recorded.caveats,
        )

    def test_app_schema_issues_reach_task_and_project_pilot_diagnostics(self) -> None:
        malformed = self.base / "malformed-app.jsonl"
        malformed.write_text("not-json-private\n", encoding="utf-8")
        invalid_duration = self.app_source("invalid-duration", {
            "method": "item/completed",
            "params": {
                "threadId": "diagnostic-thread", "turnId": "diagnostic-turn",
                "completedAtMs": 1720000001500,
                "item": {
                    "id": "diagnostic-call", "type": "commandExecution",
                    "command": "echo safe", "cwd": str(self.project),
                    "status": "completed", "durationMs": "bad", "exitCode": 0,
                },
            },
        })
        self.ingest(
            CodexEventSource(malformed, APP_SERVER_V2), invalid_duration,
        )

        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        from hydra_codex.reconcile_reports import list_reconciled_reports

        report = list_reconciled_reports(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.semantic.schema_diagnostics, 1)
        self.assertIn("schema:event:invalid_duration", task.semantic.diagnostics)
        self.assertEqual(report.schema_diagnostics.value, 1)
        self.assertEqual(report.pilot_health.schema_diagnostics.value, 2)
        self.assertIn(
            "project_event_schema_diagnostics",
            report.pilot_health.schema_diagnostics.caveats,
        )

    def test_completion_only_app_stream_stays_incomplete_without_synthetic_start(self) -> None:
        self.ingest(self.app_source("completion-only", {
            "method": "turn/completed",
            "params": {
                "threadId": "completion-only-thread",
                "turn": {
                    "id": "completion-only-turn", "completedAt": 1720000002,
                    "durationMs": 2000, "status": "completed",
                },
            },
        }))

        reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.status, "incomplete")
        self.assertIsNone(task.metrics.root_wall_clock_ms.value)

    def test_receiver_only_collaboration_items_never_create_lineage_edges(self) -> None:
        source = self.base / "official-receiver-only-collaboration.jsonl"
        rows = [
            {
                "received_at": "2024-07-03T09:46:40.000000Z", "message": {
                    "method": "turn/started", "params": {
                        "threadId": thread, "turn": {
                            "id": f"{thread}-turn", "status": "inProgress",
                        },
                    },
                },
            }
            for thread in ("official-parent", "official-child")
        ]
        rows.extend(
            {
                "received_at": f"2024-07-03T09:46:40.{index}00000Z", "message": {
                    "method": "item/completed", "params": {
                        "threadId": sender, "turnId": f"{sender}-turn",
                        "item": {
                            "id": f"receiver-only-{operation}",
                            "type": "collabToolCall", "tool": operation,
                            "senderThreadId": sender,
                            "receiverThreadId": receiver,
                            "status": "completed",
                        },
                    },
                },
            }
            for index, (operation, sender, receiver) in enumerate((
                ("followup_task", "official-parent", "official-child"),
                ("send_message", "official-child", "official-parent"),
                ("wait_agent", "official-parent", "official-child"),
                ("close_agent", "official-child", "official-parent"),
            ), start=1)
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )

        self.ingest(CodexEventSource(source, APP_SERVER_V2))

        self.assertEqual(self.store.count("session_edges"), 0)
        self.assertEqual(self.store.count("rollout_sessions"), 2)

    def test_app_spawn_and_child_snapshot_rebuild_fork_baseline_during_ingest(self) -> None:
        source = self.base / "official-spawn-baseline.jsonl"
        total = {
            "inputTokens": 100, "cachedInputTokens": 50,
            "outputTokens": 20, "reasoningOutputTokens": 10,
            "totalTokens": 120,
        }
        zero = {
            "inputTokens": 0, "cachedInputTokens": 0,
            "outputTokens": 0, "reasoningOutputTokens": 0,
            "totalTokens": 0,
        }
        rows = (
            {
                "received_at": "2024-07-03T09:46:40.000000Z", "message": {
                    "method": "turn/started", "params": {
                        "threadId": "official-parent", "turn": {
                            "id": "official-parent-turn", "status": "inProgress",
                        },
                    },
                },
            },
            {
                "received_at": "2024-07-03T09:46:40.100000Z", "message": {
                    "method": "item/completed", "params": {
                        "threadId": "official-parent", "turnId": "official-parent-turn",
                        "item": {
                            "id": "spawn-call", "type": "collabToolCall",
                            "tool": "spawn_agent", "senderThreadId": "official-parent",
                            "newThreadId": "official-child", "status": "completed",
                        },
                    },
                },
            },
            {
                "received_at": "2024-07-03T09:46:40.200000Z", "message": {
                    "method": "thread/tokenUsage/updated", "params": {
                        "threadId": "official-parent", "turnId": "official-parent-turn",
                        "tokenUsage": {"total": zero, "last": zero},
                    },
                },
            },
            {
                "received_at": "2024-07-03T09:46:40.500000Z", "message": {
                    "method": "thread/tokenUsage/updated", "params": {
                        "threadId": "official-child", "turnId": "official-child-turn",
                        "tokenUsage": {"total": total, "last": total},
                    },
                },
            },
            {
                "received_at": "2024-07-03T09:46:41.500000Z", "message": {
                    "method": "turn/completed", "params": {
                        "threadId": "official-parent", "turn": {
                            "id": "official-parent-turn", "status": "completed",
                        },
                    },
                },
            },
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )

        self.ingest(CodexEventSource(source, APP_SERVER_V2))

        baseline = self.store.connection.execute(
            """SELECT input_tokens,cached_input_tokens,output_tokens,reasoning_tokens
                 FROM fork_baselines"""
        ).fetchone()
        self.assertIsNotNone(baseline)
        self.assertEqual(tuple(baseline), (100, 50, 20, 10))
        root = Pseudonymizer(KEY).digest("identity", "official-parent")
        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
        )
        self.assertEqual(metrics.recorded.working.value, 70)
        self.assertEqual(metrics.replay_baseline.working.value, 70)
        self.assertEqual(metrics.unique.working.value, 0)

    def test_official_app_items_emit_safe_files_and_confirmed_subagent_edge(self) -> None:
        source = self.base / "official-items.jsonl"
        rows = (
            {
                "received_at": "2024-07-03T09:46:40.000000Z", "message": {
                "method": "turn/started", "params": {
                    "threadId": "official-parent", "turn": {
                        "id": "official-turn",
                        "status": "inProgress",
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:40.100000Z", "message": {
                "method": "item/started", "params": {
                    "threadId": "official-parent", "turnId": "official-turn",
                    "item": {
                        "id": "collab-call", "type": "collabToolCall",
                        "senderThreadId": "official-parent",
                        "newThreadId": "official-child", "status": "inProgress",
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:40.150000Z", "message": {
                "method": "item/started", "params": {
                    "threadId": "official-parent", "turnId": "official-turn",
                    "item": {
                        "id": "cat-call", "type": "commandExecution",
                        "command": "cat src/a.py", "cwd": str(self.project),
                        "status": "inProgress",
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:40.200000Z", "message": {
                "method": "item/completed", "params": {
                    "threadId": "official-parent", "turnId": "official-turn",
                    "item": {
                        "id": "cat-call", "type": "commandExecution",
                        "command": "cat src/a.py", "cwd": str(self.project),
                        "status": "completed", "durationMs": 2, "exitCode": 0,
                        "aggregatedOutput": "private tool output",
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:40.250000Z", "message": {
                "method": "item/started", "params": {
                    "threadId": "official-parent", "turnId": "official-turn",
                    "item": {
                        "id": "patch-call", "type": "fileChange",
                        "changes": [{
                            "path": "src/b.py", "kind": "update", "diff": "private diff",
                        }],
                        "status": "inProgress",
                    },
                }},
            },
            {
                "received_at": "2024-07-03T09:46:40.300000Z", "message": {
                "method": "item/completed", "params": {
                    "threadId": "official-parent", "turnId": "official-turn",
                    "item": {
                        "id": "patch-call", "type": "fileChange",
                        "changes": [{
                            "path": "src/b.py", "kind": "update", "diff": "private diff",
                        }],
                        "status": "completed",
                    },
                }},
            },
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )
        self.ingest(CodexEventSource(source, APP_SERVER_V2))

        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT operation,relative_path FROM file_observations ORDER BY relative_path"
            )],
            [("read", "src/a.py"), ("write", "src/b.py")],
        )
        edge = self.store.connection.execute(
            "SELECT confidence_kind,confidence FROM session_edges"
        ).fetchone()
        self.assertEqual(tuple(edge), ("confirmed", 1.0))
        self.assertEqual(
            {row[0] for row in self.store.connection.execute("SELECT tool_name FROM tool_spans")},
            {"apply_patch", "collaboration", "exec_command"},
        )
        persisted = "\n".join(self.store.connection.iterdump())
        self.assertNotIn("cat src/a.py", persisted)
        self.assertNotIn("private tool output", persisted)
        self.assertNotIn("private diff", persisted)

    def test_hybrid_rollout_and_app_tool_call_emit_one_test_evidence_row(self) -> None:
        session = "hybrid-test-thread"
        call_id = "shared-test-call"
        command = "python3.12 -m unittest tests.test_safe"
        rollout = self.base / "hybrid-rollout.jsonl"
        rollout_rows = (
            {
                "timestamp": "2024-07-03T09:46:40Z",
                "type": "session_meta",
                "payload": {"id": session, "cwd": str(self.project)},
            },
            {
                "timestamp": "2024-07-03T09:46:40.100000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call", "call_id": call_id,
                    "name": "exec_command", "arguments": json.dumps({"cmd": command}),
                },
            },
            {
                "timestamp": "2024-07-03T09:46:40.200000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output", "call_id": call_id,
                    "output": json.dumps({"exit_code": 0, "output": "rollout output"}),
                },
            },
        )
        rollout.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rollout_rows),
            encoding="utf-8",
        )
        ingest_rollouts(
            self.store, (rollout,), self.project, PROJECT, hash_key=KEY,
        )

        app = self.base / "hybrid-app.jsonl"
        app.write_text(json.dumps({
            "received_at": "2024-07-03T09:46:40.300000Z",
            "message": {
                "method": "item/completed",
                "params": {
                    "threadId": session, "turnId": "hybrid-turn",
                    "item": {
                        "id": call_id, "type": "commandExecution",
                        "command": command, "cwd": str(self.project),
                        "status": "completed", "exitCode": 0,
                        "aggregatedOutput": "app output",
                    },
                },
            },
        }, sort_keys=True) + "\n", encoding="utf-8")
        self.ingest(CodexEventSource(app, APP_SERVER_V2))

        self.assertEqual(self.store.count("tool_spans"), 1)
        self.assertEqual(self.store.count("rollout_test_runs"), 1)

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
