from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from hydra_codex.annotation_core import issue_capability
from hydra_codex.annotation_types import TrustedTurnContext
from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.reconcile_engine import list_reconciled_tasks, reconcile_project
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import MIGRATIONS, HydraStore
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


KEY = b"f" * 32
PROJECT = "final-review-project"


def _write_jsonl(path: Path, rows: tuple[dict[str, object], ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _rollout(
    kind: str, payload: dict[str, object], timestamp: str,
) -> dict[str, object]:
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def _total(value: int) -> dict[str, int]:
    return {
        "inputTokens": value,
        "cachedInputTokens": value // 10,
        "outputTokens": value // 5,
        "reasoningOutputTokens": value // 20,
        "totalTokens": value + value // 5,
    }


def _received(at: str, message: dict[str, object]) -> dict[str, object]:
    return {"received_at": at, "message": message}


class FinalReconciliationFindingsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT}"\n', encoding="utf-8",
        )
        self.store = HydraStore(self.base / "hydra.sqlite3")
        self.addCleanup(self.store.close)

    def _app(self, name: str, *events: dict[str, object]) -> CodexEventSource:
        return CodexEventSource(
            _write_jsonl(self.base / f"{name}.app.jsonl", tuple(events)),
            APP_SERVER_V2,
        )

    def _ingest_abort_conflict(self, store: HydraStore, *, resume: bool) -> str:
        thread = "canonical-abort-thread" + ("-resume" if resume else "")
        first_turn = "canonical-abort-turn"
        app = self._app(
            "canonical-abort" + ("-resume" if resume else ""),
            {"method": "turn/started", "params": {
                "threadId": thread,
                "turn": {"id": first_turn, "startedAt": 1720000000,
                         "status": "inProgress"},
            }},
            _received("2024-07-03T09:46:41Z", {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": thread, "turnId": first_turn,
                           "tokenUsage": {"total": _total(100), "last": _total(100)}},
            }),
            _received("2024-07-03T09:46:44Z", {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": thread,
                           "turnId": "resumed-turn" if resume else first_turn,
                           "tokenUsage": {"total": _total(200), "last": _total(200)}},
            }),
            {"method": "turn/completed", "params": {
                "threadId": thread,
                "turn": {"id": first_turn, "completedAt": 1720000010,
                         "status": "completed"},
            }},
        )
        rows = [
            _rollout("session_meta", {"id": thread, "cwd": str(self.project)},
                     "2024-07-03T09:46:39Z"),
            _rollout("event_msg", {"type": "task_started", "turn_id": first_turn},
                     "2024-07-03T09:46:40Z"),
            _rollout("event_msg", {"type": "turn_aborted", "turn_id": first_turn},
                     "2024-07-03T09:46:42Z"),
        ]
        if resume:
            rows.append(_rollout(
                "event_msg", {"type": "task_started", "turn_id": "resumed-turn"},
                "2024-07-03T09:46:43Z",
            ))
        rollout = _write_jsonl(
            self.base / ("canonical-abort-resume.rollout.jsonl" if resume
                         else "canonical-abort.rollout.jsonl"),
            tuple(rows),
        )
        ingest_codex_events(store, (app,), self.project, PROJECT, hash_key=KEY)
        ingest_rollouts(store, (rollout,), self.project, PROJECT, hash_key=KEY)
        return Pseudonymizer(KEY).digest("identity", thread)

    def test_canonical_abort_caps_incomplete_task_before_lower_app_completion(self) -> None:
        root = self._ingest_abort_conflict(self.store, resume=False)

        reconcile_project(self.store, PROJECT, b"reconciliation-key")
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]

        self.assertEqual(task.status, "incomplete")
        self.assertEqual(
            task.last_activity_at,
            datetime(2024, 7, 3, 9, 46, 42, tzinfo=timezone.utc),
        )
        self.assertEqual(task.metrics.recorded.input.value, 100)
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT attempt_ordinal,state FROM turn_attempts ORDER BY attempt_ordinal"
            )],
            [(1, "aborted")],
        )
        direct = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
            cutoff_at=task.last_activity_at,
        )
        self.assertEqual(direct.recorded.input.value, 100)

    def test_execution_materialization_migration_rebuilds_legacy_intent_only_rows(self) -> None:
        database = self.base / "legacy-v26.sqlite3"
        connection = sqlite3.connect(database)
        try:
            for version, statements in MIGRATIONS:
                if version >= 27:
                    break
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version,applied_at) "
                    "VALUES (?,datetime('now'))", (version,),
                )
                connection.execute(f"PRAGMA user_version={version}")
            connection.execute(
                """INSERT INTO rollout_test_runs(
                       evidence_key,source_digest,line_number,session_key,
                       tool_call_key,command_hash,runner,scope,outcome,
                       failure_cause,provenance,completeness)
                   VALUES ('intent','source',1,'session','call','command',
                           'pytest','targeted','unknown','unknown','derived','intent_only')"""
            )
            connection.execute(
                """INSERT INTO test_evidence_candidates(
                       candidate_key,candidate_kind,evidence_key,source_digest,
                       line_number,session_key,tool_call_key,command_hash,runner,
                       scope,outcome,failure_cause,provenance,completeness)
                   VALUES ('candidate','evidence','intent','source',1,'session',
                           'call','command','pytest','targeted','unknown','unknown',
                           'derived','intent_only')"""
            )
            connection.execute(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,conversation_key)
                   VALUES ('legacy-file-session',?,'worktree',1,'conversation')""",
                (PROJECT,),
            )
            connection.executemany(
                """INSERT INTO file_observations(
                       source_digest,line_number,session_key,operation,
                       relative_path,path_hash,observed_at,turn_key)
                   VALUES (?,0,'legacy-file-session',?,?,?,?,NULL)""",
                (
                    (
                        "preserved-source", "read", "src/preserved.py",
                        "preserved-hash", "2026-07-21T00:00:01Z",
                    ),
                    (
                        "unproven-source", "read", "src/unproven.py",
                        "unproven-hash", "2026-07-21T00:00:02Z",
                    ),
                    (
                        "failed-write-source", "write", "src/failed-write.py",
                        "failed-write-hash", "2026-07-21T00:00:03Z",
                    ),
                ),
            )
            connection.executemany(
                """INSERT INTO file_observation_candidates(
                       session_key,call_key,candidate_key,source_digest,source_ordinal,
                       operation,relative_path,path_hash,observed_at,turn_key,
                       tool_name,requires_success,evidence_kind)
                   VALUES ('legacy-file-session',?,?,?,0,'read',?,?,?,NULL,
                           'file_read',0,'exact')""",
                (
                    (
                        "preserved-call", "preserved-candidate", "preserved-source",
                        "src/preserved.py", "preserved-hash",
                        "2026-07-21T00:00:01Z",
                    ),
                    (
                        "unproven-call", "unproven-candidate", "unproven-source",
                        "src/unproven.py", "unproven-hash",
                        "2026-07-21T00:00:02Z",
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO file_observation_candidates(
                       session_key,call_key,candidate_key,source_digest,source_ordinal,
                       operation,relative_path,path_hash,observed_at,turn_key,
                       tool_name,requires_success,evidence_kind)
                   VALUES ('legacy-file-session','failed-write-call',
                           'failed-write-candidate','failed-write-source',0,
                           'write','src/failed-write.py','failed-write-hash',
                           '2026-07-21T00:00:03Z',NULL,'file_write',1,'exact')"""
            )
            connection.execute(
                """INSERT INTO tool_span_candidates(
                       session_key,call_key,source_digest,source_ordinal,candidate_kind,
                       category,terminal_state,latency_ms,tool_name,started_at,
                       finished_at,turn_key,provenance)
                   VALUES ('legacy-file-session','preserved-call','terminal-source',1,
                           'end','file','unknown',1,'file_read',NULL,
                           '2026-07-21T00:00:01Z',NULL,'exact')"""
            )
            connection.execute(
                """INSERT INTO tool_span_candidates(
                       session_key,call_key,source_digest,source_ordinal,candidate_kind,
                       category,terminal_state,latency_ms,tool_name,started_at,
                       finished_at,turn_key,provenance)
                   VALUES ('legacy-file-session','failed-write-call',
                           'failed-terminal-source',1,'end','file','failed',1,
                           'file_write',NULL,'2026-07-21T00:00:03Z',NULL,'exact')"""
            )
            connection.commit()
        finally:
            connection.close()

        migrated = HydraStore(database)
        self.addCleanup(migrated.close)

        self.assertEqual(migrated.schema_version(), MIGRATIONS[-1][0])
        self.assertEqual(migrated.count("rollout_test_runs"), 0)
        self.assertEqual(migrated.connection.execute(
            "SELECT COUNT(*) FROM test_evidence_candidates"
        ).fetchone()[0], 1)
        self.assertEqual(
            [tuple(row) for row in migrated.connection.execute(
                "SELECT operation,relative_path FROM file_observations"
            )],
            [("read", "src/preserved.py")],
        )
        columns = {
            row[1] for row in migrated.connection.execute(
                "PRAGMA table_info(turn_attempts)"
            )
        }
        self.assertTrue({
            "started_event_key", "terminal_event_key",
            "started_logical_source_key", "terminal_logical_source_key",
            "started_source_ordinal", "terminal_source_ordinal",
        }.issubset(columns))

    def test_distinct_resume_after_canonical_abort_reopens_task_and_keeps_new_usage(self) -> None:
        self._ingest_abort_conflict(self.store, resume=True)

        reconcile_project(self.store, PROJECT, b"reconciliation-key")
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]

        self.assertEqual(task.status, "incomplete")
        self.assertEqual(
            task.last_activity_at,
            datetime(2024, 7, 3, 9, 46, 44, tzinfo=timezone.utc),
        )
        self.assertEqual(task.metrics.recorded.input.value, 200)

    def test_same_turn_rollout_append_preserves_retry_after_authoritative_abort(self) -> None:
        thread, turn = "append-retry-thread", "append-retry-turn"
        path = self.base / "append-retry.rollout.jsonl"
        initial = (
            _rollout("session_meta", {"id": thread, "cwd": str(self.project)},
                     "2026-07-21T00:00:00Z"),
            _rollout("event_msg", {"type": "task_started", "turn_id": turn},
                     "2026-07-21T00:00:01Z"),
            _rollout("event_msg", {"type": "turn_aborted", "turn_id": turn},
                     "2026-07-21T00:00:02Z"),
        )
        app = self._app(
            "append-retry",
            _received("2026-07-21T00:00:06Z", {
                "method": "turn/completed",
                "params": {"threadId": thread, "turn": {
                    "id": turn, "status": "completed",
                }},
            }),
        )
        _write_jsonl(path, initial)
        ingest_rollouts(self.store, (path,), self.project, PROJECT, hash_key=KEY)
        ingest_codex_events(self.store, (app,), self.project, PROJECT, hash_key=KEY)

        _write_jsonl(path, (*initial,
            _rollout("event_msg", {"type": "task_started", "turn_id": turn},
                     "2026-07-21T00:00:07Z"),
            _rollout("event_msg", {"type": "task_complete", "turn_id": turn},
                     "2026-07-21T00:00:08Z"),
        ))
        ingest_rollouts(self.store, (path,), self.project, PROJECT, hash_key=KEY)

        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT attempt_ordinal,state FROM turn_attempts ORDER BY attempt_ordinal"
            )],
            [(1, "aborted"), (2, "completed")],
        )
        reconcile_project(self.store, PROJECT, b"reconciliation-key")
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]
        self.assertEqual(task.status, "complete")
        self.assertEqual(
            task.last_activity_at,
            datetime(2026, 7, 21, 0, 0, 8, tzinfo=timezone.utc),
        )

    def test_identityless_app_turn_and_items_are_quarantined_without_losing_thread_usage(self) -> None:
        thread = "identityless-app-thread"
        source = self._app(
            "identityless-app",
            {"method": "thread/started", "params": {
                "thread": {"id": thread, "createdAt": 1720000000},
            }},
            {"method": "turn/completed", "params": {
                "threadId": thread,
                "turn": {"completedAt": 1720000002, "status": "completed"},
            }},
            {"method": "item/started", "params": {
                "threadId": thread, "turnId": "stable-turn",
                "item": {"type": "commandExecution", "command": "pytest",
                         "status": "inProgress"},
            }},
            {"method": "item/completed", "params": {
                "threadId": thread, "turnId": "stable-turn",
                "item": {"type": "commandExecution", "command": "pytest",
                         "status": "completed", "exitCode": 0},
            }},
            _received("2024-07-03T09:46:43Z", {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": thread,
                           "tokenUsage": {"total": _total(50), "last": _total(50)}},
            }),
        )

        report = ingest_codex_events(
            self.store, (source,), self.project, PROJECT, hash_key=KEY,
        )

        self.assertEqual(report.issues, 3)
        self.assertEqual(
            [row[0] for row in self.store.connection.execute(
                "SELECT issue_code FROM codex_event_issues ORDER BY source_ordinal"
            )],
            ["invalid_envelope", "invalid_envelope", "invalid_envelope"],
        )
        self.assertEqual(self.store.count("codex_events"), 2)
        self.assertEqual(self.store.count("turn_lifecycle_events"), 0)
        self.assertEqual(self.store.count("tool_spans"), 0)
        self.assertEqual(self.store.count("rollout_test_runs"), 0)
        self.assertEqual(self.store.count("token_snapshots"), 1)
        self.assertEqual(self.store.count("rollout_sessions"), 1)

    def test_start_only_test_and_file_candidates_are_not_executed_metrics(self) -> None:
        session, turn = "intent-session", "intent-turn"
        source = _write_jsonl(self.base / "intent-only.rollout.jsonl", (
            _rollout("session_meta", {"id": session, "cwd": str(self.project)},
                     "2026-07-21T00:00:00Z"),
            _rollout("event_msg", {"type": "task_started", "turn_id": turn},
                     "2026-07-21T00:00:01Z"),
            _rollout("response_item", {
                "type": "function_call", "call_id": "test-intent",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "pytest tests/test_intent.py"}),
            }, "2026-07-21T00:00:02Z"),
            _rollout("response_item", {
                "type": "function_call", "call_id": "file-intent",
                "name": "file_read",
                "arguments": json.dumps({"path": "src/intent.py"}),
            }, "2026-07-21T00:00:03Z"),
            _rollout("event_msg", {"type": "task_complete", "turn_id": turn},
                     "2026-07-21T00:00:04Z"),
        ))

        ingest_rollouts(
            self.store, (source,), self.project, PROJECT, hash_key=KEY,
        )
        root = Pseudonymizer(KEY).digest("identity", session)
        metrics = aggregate_stored_task_tree(
            self.store.connection, project_id=PROJECT, root_id=root,
        )

        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM test_evidence_candidates"
        ).fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM file_observation_candidates"
        ).fetchone()[0], 1)
        self.assertEqual(self.store.count("rollout_test_runs"), 0)
        self.assertEqual(self.store.count("file_observations"), 0)
        self.assertEqual(metrics.test_runs.value, 0)
        self.assertEqual(metrics.file_reads.known_lower_bound, 0)

    def test_terminal_test_and_file_evidence_still_materialize(self) -> None:
        session, turn = "terminal-session", "terminal-turn"
        source = _write_jsonl(self.base / "terminal-evidence.rollout.jsonl", (
            _rollout("session_meta", {"id": session, "cwd": str(self.project)},
                     "2026-07-21T00:00:00Z"),
            _rollout("event_msg", {"type": "task_started", "turn_id": turn},
                     "2026-07-21T00:00:01Z"),
            _rollout("response_item", {
                "type": "function_call", "call_id": "failed-test",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "pytest tests/test_failed.py"}),
            }, "2026-07-21T00:00:02Z"),
            _rollout("response_item", {
                "type": "function_call_output", "call_id": "failed-test",
                "output": json.dumps({"exit_code": 1, "stderr": "assertion failed"}),
            }, "2026-07-21T00:00:03Z"),
            _rollout("response_item", {
                "type": "function_call", "call_id": "read-file",
                "name": "file_read",
                "arguments": json.dumps({"path": "src/read.py"}),
            }, "2026-07-21T00:00:04Z"),
            _rollout("response_item", {
                "type": "function_call_output", "call_id": "read-file",
                "output": json.dumps({"exit_code": 0}),
            }, "2026-07-21T00:00:05Z"),
            _rollout("event_msg", {"type": "task_complete", "turn_id": turn},
                     "2026-07-21T00:00:06Z"),
        ))

        ingest_rollouts(
            self.store, (source,), self.project, PROJECT, hash_key=KEY,
        )

        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT outcome,exit_status,completeness FROM rollout_test_runs"
            )],
            [("failed", 1, "complete")],
        )
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT operation,relative_path FROM file_observations"
            )],
            [("read", "src/read.py")],
        )

    def test_structural_terminal_without_exit_counts_execution_but_not_unproven_write(self) -> None:
        session = "unknown-terminal-session"
        source = _write_jsonl(self.base / "unknown-terminal.rollout.jsonl", (
            _rollout("session_meta", {"id": session, "cwd": str(self.project)},
                     "2026-07-21T00:00:00Z"),
            _rollout("response_item", {
                "type": "function_call", "call_id": "unknown-test",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "pytest tests/test_unknown.py"}),
            }, "2026-07-21T00:00:01Z"),
            _rollout("response_item", {
                "type": "function_call_output", "call_id": "unknown-test",
                "output": "terminal without a structured exit",
            }, "2026-07-21T00:00:02Z"),
            _rollout("response_item", {
                "type": "function_call", "call_id": "unknown-read",
                "name": "file_read",
                "arguments": json.dumps({"path": "src/unknown-read.py"}),
            }, "2026-07-21T00:00:03Z"),
            _rollout("response_item", {
                "type": "function_call_output", "call_id": "unknown-read",
                "output": "terminal without a structured exit",
            }, "2026-07-21T00:00:04Z"),
            _rollout("response_item", {
                "type": "function_call", "call_id": "unknown-write",
                "name": "file_write",
                "arguments": json.dumps({"path": "src/unknown-write.py"}),
            }, "2026-07-21T00:00:05Z"),
            _rollout("response_item", {
                "type": "function_call_output", "call_id": "unknown-write",
                "output": "terminal without a structured exit",
            }, "2026-07-21T00:00:06Z"),
        ))

        ingest_rollouts(
            self.store, (source,), self.project, PROJECT, hash_key=KEY,
        )

        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT outcome,exit_status,completeness FROM rollout_test_runs"
            )],
            [("unknown", None, "result_without_exit")],
        )
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT operation,relative_path FROM file_observations"
            )],
            [("read", "src/unknown-read.py")],
        )

    def _bind_hook_session(self, raw_session: str, project_id: str) -> None:
        issue_capability(
            self.store, Pseudonymizer(KEY),
            TrustedTurnContext(
                project_id, raw_session, "hook-turn",
                "2026-07-21T00:00:00Z",
            ),
            expires_at="2026-07-22T00:00:00Z",
        )

    def test_hook_worktree_binding_accepts_only_safe_relative_paths(self) -> None:
        for invalid in (
            "/absolute", "C:/absolute", "../escape", "safe/../../escape", "..\\escape",
            "bad\npath", 7,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    TrustedTurnContext(
                        PROJECT, "session", "turn", "2026-07-21T00:00:00Z",
                        invalid,  # type: ignore[arg-type]
                    )
        context = TrustedTurnContext(
            PROJECT, "safe-session", "safe-turn", "2026-07-21T00:00:00Z",
            "feature/src",
        )
        issue_capability(
            self.store, Pseudonymizer(KEY), context,
            expires_at="2026-07-22T00:00:00Z",
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT worktree_path FROM sessions"
            ).fetchone()[0],
            "feature/src",
        )

    def test_deleted_same_project_worktree_uses_durable_hook_binding(self) -> None:
        raw_session = "deleted-worktree-session"
        deleted = self.base / "deleted-worktree"
        (deleted / ".hydra").mkdir(parents=True)
        (deleted / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT}"\n', encoding="utf-8",
        )
        self._bind_hook_session(raw_session, PROJECT)
        source = _write_jsonl(self.base / "deleted-worktree.rollout.jsonl", (
            _rollout("session_meta", {"id": raw_session, "cwd": str(deleted)},
                     "2026-07-21T00:00:00Z"),
            _rollout("event_msg", {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 10,
                    "output_tokens": 20, "reasoning_output_tokens": 5,
                }},
            }, "2026-07-21T00:00:01Z"),
            _rollout("event_msg", {"type": "task_complete", "turn_id": "hook-turn"},
                     "2026-07-21T00:00:02Z"),
        ))
        shutil.rmtree(deleted)

        ingest_rollouts(
            self.store, (source,), self.project, PROJECT, hash_key=KEY,
        )

        self.assertEqual(self.store.count("rollout_sessions"), 1)
        self.assertEqual(self.store.count("token_snapshots"), 1)
        self.assertNotIn(
            "unresolved_project",
            {row[0] for row in self.store.connection.execute(
                "SELECT envelope_kind FROM rollout_diagnostics"
            )},
        )

    def test_unbound_deleted_source_retries_after_hook_binding(self) -> None:
        raw_session = "retry-deleted-worktree-session"
        deleted = self.base / "retry-deleted-worktree"
        (deleted / ".hydra").mkdir(parents=True)
        (deleted / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT}"\n', encoding="utf-8",
        )
        source = _write_jsonl(self.base / "retry-deleted.rollout.jsonl", (
            _rollout("session_meta", {"id": raw_session, "cwd": str(deleted)},
                     "2026-07-21T00:00:00Z"),
            _rollout("event_msg", {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 10,
                    "output_tokens": 20, "reasoning_output_tokens": 5,
                }},
            }, "2026-07-21T00:00:01Z"),
        ))
        shutil.rmtree(deleted)

        ingest_rollouts(self.store, (source,), self.project, PROJECT, hash_key=KEY)
        self.assertEqual(self.store.count("token_snapshots"), 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT materialized FROM rollout_sources"
        ).fetchone()[0], 0)

        self._bind_hook_session(raw_session, PROJECT)
        ingest_rollouts(self.store, (source,), self.project, PROJECT, hash_key=KEY)

        self.assertEqual(self.store.count("token_snapshots"), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT materialized FROM rollout_sources"
        ).fetchone()[0], 1)
        database_dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn(str(deleted), database_dump)
        self.assertNotIn(raw_session, database_dump)

    def test_unbound_rewrite_and_truncate_revisions_remain_retryable(self) -> None:
        for relation in ("rewrite", "truncate"):
            with self.subTest(relation=relation):
                store = HydraStore(self.base / f"{relation}.sqlite3")
                self.addCleanup(store.close)
                raw_session = f"{relation}-deleted-session"
                deleted = self.base / f"{relation}-deleted-worktree"
                (deleted / ".hydra").mkdir(parents=True)
                (deleted / ".hydra" / "project.toml").write_text(
                    f'project_id = "{PROJECT}"\n', encoding="utf-8",
                )
                source = self.base / f"{relation}.rollout.jsonl"
                initial = (
                    _rollout("session_meta", {
                        "id": raw_session, "cwd": str(deleted),
                    }, "2026-07-21T00:00:00Z"),
                    _rollout("event_msg", {
                        "type": "token_count", "info": {"total_token_usage": {
                            "input_tokens": 100, "cached_input_tokens": 10,
                            "output_tokens": 20, "reasoning_output_tokens": 5,
                        }},
                    }, "2026-07-21T00:00:01Z"),
                    _rollout("event_msg", {
                        "type": "task_complete", "turn_id": "hook-turn",
                    }, "2026-07-21T00:00:02Z"),
                )
                _write_jsonl(source, initial)
                shutil.rmtree(deleted)
                ingest_rollouts(store, (source,), self.project, PROJECT, hash_key=KEY)
                first = store.connection.execute(
                    "SELECT source_digest,logical_source_key FROM rollout_sources"
                ).fetchone()
                store.connection.execute(
                    "UPDATE rollout_logical_sources SET canonical_revision_digest=? "
                    "WHERE logical_source_key=?",
                    (first[0], first[1]),
                )
                store.connection.commit()

                changed = initial[:2] if relation == "truncate" else (
                    initial[0],
                    _rollout("event_msg", {
                        "type": "token_count", "info": {"total_token_usage": {
                            "input_tokens": 120, "cached_input_tokens": 10,
                            "output_tokens": 20, "reasoning_output_tokens": 5,
                        }},
                    }, "2026-07-21T00:00:01Z"),
                    initial[2],
                )
                _write_jsonl(source, changed)
                ingest_rollouts(store, (source,), self.project, PROJECT, hash_key=KEY)
                self.assertEqual(store.count("token_snapshots"), 0)
                self.assertEqual(store.connection.execute(
                    "SELECT materialized FROM rollout_sources WHERE relation=?",
                    (relation,),
                ).fetchone()[0], 0)

                issue_capability(
                    store, Pseudonymizer(KEY),
                    TrustedTurnContext(
                        PROJECT, raw_session, "hook-turn",
                        "2026-07-21T00:00:00Z",
                    ),
                    expires_at="2026-07-22T00:00:00Z",
                )
                ingest_rollouts(store, (source,), self.project, PROJECT, hash_key=KEY)
                self.assertEqual(store.count("token_snapshots"), 1)
                self.assertEqual(store.connection.execute(
                    "SELECT materialized FROM rollout_sources WHERE relation=?",
                    (relation,),
                ).fetchone()[0], 1)

    def test_durable_hook_binding_never_rebinds_an_existing_foreign_project(self) -> None:
        raw_session = "foreign-worktree-session"
        self._bind_hook_session(raw_session, PROJECT)
        foreign = self.base / "foreign-project"
        (foreign / ".hydra").mkdir(parents=True)
        (foreign / ".hydra" / "project.toml").write_text(
            'project_id = "foreign-project"\n', encoding="utf-8",
        )
        source = _write_jsonl(self.base / "foreign-worktree.rollout.jsonl", (
            _rollout("session_meta", {"id": raw_session, "cwd": str(foreign)},
                     "2026-07-21T00:00:00Z"),
            _rollout("event_msg", {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 10,
                    "output_tokens": 20, "reasoning_output_tokens": 5,
                }},
            }, "2026-07-21T00:00:01Z"),
        ))

        ingest_rollouts(
            self.store, (source,), self.project, PROJECT, hash_key=KEY,
        )

        self.assertEqual(self.store.count("rollout_sessions"), 0)
        self.assertEqual(self.store.count("token_snapshots"), 0)
        self.assertIn(
            "unrelated_project",
            {row[0] for row in self.store.connection.execute(
                "SELECT envelope_kind FROM rollout_diagnostics"
            )},
        )


if __name__ == "__main__":
    unittest.main()
