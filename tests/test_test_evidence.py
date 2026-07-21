from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.migrations_h8 import H8_MIGRATIONS
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.storage import HydraStore
from hydra_codex.test_evidence import parse_structured_result


def event(kind: str, payload: dict[str, object], second: int) -> dict[str, object]:
    return {
        "timestamp": f"2026-07-21T00:00:{second:02d}Z",
        "type": kind,
        "payload": payload,
    }


def write_rollout(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def start(session: str, project: Path, second: int = 0) -> dict[str, object]:
    return event("session_meta", {"id": session, "cwd": str(project)}, second)


def call(call_id: str, command: str, second: int) -> dict[str, object]:
    return event("response_item", {
        "type": "function_call", "call_id": call_id, "name": "exec_command",
        "arguments": json.dumps({"cmd": command}),
    }, second)


def result(call_id: str, output: object, second: int) -> dict[str, object]:
    return event("response_item", {
        "type": "function_call_output", "call_id": call_id,
        "output": json.dumps(output) if not isinstance(output, str) else output,
    }, second)


class StructuredResultTests(unittest.TestCase):
    def test_only_an_exact_non_boolean_integer_exit_is_authoritative(self) -> None:
        success = parse_structured_result(json.dumps({"exit_code": 0, "stdout": "private"}))
        product = parse_structured_result(json.dumps({"exit_code": 2, "stderr": "assertion failed"}))
        infra = parse_structured_result(json.dumps({
            "exit_code": 1, "output": "sandbox unavailable",
        }))

        self.assertEqual((success.exit_status, success.outcome, success.failure_cause, success.completeness),
                         (0, "success", "none", "complete"))
        self.assertEqual((product.exit_status, product.outcome, product.failure_cause),
                         (2, "failed", "product_failure"))
        self.assertEqual((infra.exit_status, infra.outcome, infra.failure_cause),
                         (1, "failed", "infra_failure"))
        for invalid in ({"exit_code": True}, {"exit_code": "0"}, {}, [], "Script completed"):
            with self.subTest(invalid=invalid):
                parsed = parse_structured_result(json.dumps(invalid) if not isinstance(invalid, str) else invalid)
                self.assertEqual((parsed.exit_status, parsed.outcome, parsed.failure_cause, parsed.completeness),
                                 (None, "unknown", "unknown", "result_without_exit"))
        unstructured_infra = parse_structured_result(json.dumps({
            "exit_code": True, "stderr": "sandbox unavailable",
        }))
        self.assertEqual((unstructured_infra.outcome, unstructured_infra.failure_cause),
                         ("unknown", "unknown"))

    def test_array_results_require_one_unambiguous_structured_exit(self) -> None:
        unique = parse_structured_result([
            {"type": "input_text", "text": "Script completed"},
            {"type": "input_text", "text": json.dumps({"exit_code": 7, "stderr": "assertion failed"})},
        ])
        ambiguous = parse_structured_result([
            {"exit_code": 0},
            {"type": "input_text", "text": json.dumps({"exit_code": 1})},
        ])
        header_only = parse_structured_result([
            {"type": "input_text", "text": "Script completed\nWall time: 0.1 seconds"},
        ])

        self.assertEqual((unique.exit_status, unique.outcome, unique.failure_cause),
                         (7, "failed", "product_failure"))
        self.assertEqual((ambiguous.exit_status, ambiguous.outcome, ambiguous.completeness),
                         (None, "unknown", "conflicted"))
        self.assertEqual((header_only.exit_status, header_only.outcome, header_only.completeness),
                         (None, "unknown", "result_without_exit"))


class TestEvidenceCandidateMigrationTests(unittest.TestCase):
    def test_v24_backfills_existing_materialized_test_evidence(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            """CREATE TABLE rollout_test_runs (
                evidence_key TEXT PRIMARY KEY, source_digest TEXT NOT NULL,
                line_number INTEGER NOT NULL, session_key TEXT NOT NULL,
                observed_at TEXT, turn_key TEXT, tool_call_key TEXT NOT NULL,
                command_hash TEXT NOT NULL, runner TEXT NOT NULL, scope TEXT NOT NULL,
                exit_status INTEGER, outcome TEXT NOT NULL, failure_cause TEXT NOT NULL,
                retry_kind TEXT NOT NULL DEFAULT 'none',
                attempt_ordinal INTEGER NOT NULL DEFAULT 1,
                provenance TEXT NOT NULL, completeness TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE codex_events (
                source_digest TEXT NOT NULL, source_ordinal INTEGER NOT NULL,
                event_key TEXT NOT NULL, session_key TEXT, observed_at TEXT,
                turn_key TEXT, tool_call_key TEXT, tool_name TEXT,
                tool_phase TEXT, tool_status TEXT
            )"""
        )
        connection.execute(
            """INSERT INTO rollout_test_runs(
                   evidence_key,source_digest,line_number,session_key,observed_at,turn_key,
                   tool_call_key,command_hash,runner,scope,exit_status,outcome,failure_cause,
                   provenance,completeness)
               VALUES ('legacy-evidence','legacy-source',7,'legacy-session',
                       '2026-07-21T00:00:07Z','legacy-turn','legacy-call','legacy-command',
                       'pytest','targeted',0,'success','none','derived','complete')"""
        )
        connection.execute(
            """INSERT INTO codex_events(
                   source_digest,source_ordinal,event_key,session_key,observed_at,
                   turn_key,tool_call_key,tool_name,tool_phase,tool_status)
               VALUES ('legacy-app-source',8,'legacy-decline','legacy-session',
                       '2026-07-21T00:00:08Z','legacy-turn','legacy-call',
                       'exec_command','completed','declined')"""
        )
        connection.execute(
            """INSERT INTO codex_events(
                   source_digest,source_ordinal,event_key,session_key,observed_at,
                   turn_key,tool_call_key,tool_name,tool_phase,tool_status)
               VALUES ('legacy-app-revision',9,'legacy-decline','legacy-session',
                       '2026-07-21T00:00:08Z','legacy-turn','legacy-call',
                       'exec_command','completed','declined')"""
        )

        self.assertTrue(H8_MIGRATIONS, "schema v24 candidate migration is missing")
        self.assertEqual(H8_MIGRATIONS[0][0], 24)
        for statement in H8_MIGRATIONS[0][1]:
            connection.execute(statement)

        row = connection.execute(
            """SELECT candidate_kind,evidence_key,source_digest,line_number,session_key,
                      tool_call_key,command_hash,runner,scope,exit_status,outcome,
                      failure_cause,provenance,completeness
                 FROM test_evidence_candidates
                WHERE candidate_kind='evidence'"""
        ).fetchone()
        self.assertEqual(row, (
            "evidence", "legacy-evidence", "legacy-source", 7, "legacy-session",
            "legacy-call", "legacy-command", "pytest", "targeted", 0,
            "success", "none", "derived", "complete",
        ))
        rejection = connection.execute(
            """SELECT candidate_kind,source_digest,line_number,session_key,
                      tool_call_key,completeness
                 FROM test_evidence_candidates
                WHERE candidate_kind='non_execution'"""
        ).fetchone()
        self.assertEqual(rejection, (
            "non_execution", "legacy-app-source", 8, "legacy-session",
            "legacy-call", "non_execution",
        ))
        self.assertEqual(connection.execute(
            """SELECT COUNT(*) FROM test_evidence_candidates
                 WHERE candidate_kind='non_execution'"""
        ).fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE test_evidence_candidates SET outcome='failed'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM test_evidence_candidates")


class PersistedTestEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text('project_id = "project-tests"\n', encoding="utf-8")
        self.store = HydraStore(self.root / "hydra.sqlite3")
        self.addCleanup(self.store.close)

    def ingest(self, *paths: Path, model_causes: dict[str, str] | None = None) -> None:
        ingest_rollouts(
            self.store, paths, self.project, "project-tests",
            model_causes=model_causes, hash_key=b"t" * 32,
        )

    def app_source(
        self, name: str, *, session: str, call_id: str, command: str,
        status: str, exit_code: int | None = None,
    ) -> CodexEventSource:
        path = self.root / f"{name}.app.jsonl"
        item: dict[str, object] = {
            "id": call_id,
            "type": "commandExecution",
            "command": command,
            "cwd": str(self.project),
            "status": status,
        }
        if exit_code is not None:
            item["exitCode"] = exit_code
        path.write_text(json.dumps({
            "received_at": "2026-07-21T00:00:03Z",
            "message": {"method": "item/completed", "params": {
                "threadId": session, "turnId": "app-turn", "item": item,
            }},
        }) + "\n", encoding="utf-8")
        return CodexEventSource(path, APP_SERVER_V2)

    def ingest_pair(
        self, store: HydraStore, rollout: Path, app: CodexEventSource,
        *, app_first: bool,
    ) -> None:
        if app_first:
            ingest_codex_events(
                store, (app,), self.project, "project-tests", hash_key=b"t" * 32,
            )
            ingest_rollouts(
                store, (rollout,), self.project, "project-tests", hash_key=b"t" * 32,
            )
        else:
            ingest_rollouts(
                store, (rollout,), self.project, "project-tests", hash_key=b"t" * 32,
            )
            ingest_codex_events(
                store, (app,), self.project, "project-tests", hash_key=b"t" * 32,
            )

    def test_same_call_uses_canonical_rollout_command_and_result_not_command_hash_identity(self) -> None:
        session, call_id = "same-call-session", "same-call"
        rollout = self.root / "same-call.rollout.jsonl"
        write_rollout(rollout, [
            start(session, self.project),
            call(call_id, "pytest", 1),
            result(call_id, {"exit_code": 1, "stderr": "assertion failed"}, 2),
        ])
        app = self.app_source(
            "same-call", session=session, call_id=call_id,
            command="pytest tests/test_other.py", status="completed", exit_code=0,
        )

        observed: list[list[tuple[object, ...]]] = []
        for index, app_first in enumerate((False, True), start=1):
            store = HydraStore(self.root / f"same-call-{index}.sqlite3")
            self.addCleanup(store.close)
            self.ingest_pair(store, rollout, app, app_first=app_first)
            observed.append([
                tuple(row) for row in store.connection.execute(
                    """SELECT runner,scope,outcome,exit_status,failure_cause
                         FROM rollout_test_runs"""
                )
            ])

        self.assertEqual(observed, [
            [("pytest", "full", "failed", 1, "product_failure")],
            [("pytest", "full", "failed", 1, "product_failure")],
        ])

    def test_complete_result_from_a_different_command_never_completes_authoritative_intent(self) -> None:
        session, call_id = "mismatched-result-session", "mismatched-result"
        rollout = self.root / "mismatched-result.rollout.jsonl"
        write_rollout(rollout, [
            start(session, self.project),
            call(call_id, "pytest", 1),
        ])
        app = self.app_source(
            "mismatched-result", session=session, call_id=call_id,
            command="pytest tests/test_other.py", status="completed", exit_code=0,
        )

        observed: list[list[tuple[object, ...]]] = []
        for index, app_first in enumerate((False, True), start=1):
            store = HydraStore(self.root / f"mismatched-result-{index}.sqlite3")
            self.addCleanup(store.close)
            self.ingest_pair(store, rollout, app, app_first=app_first)
            observed.append([
                tuple(row) for row in store.connection.execute(
                    """SELECT runner,scope,outcome,exit_status,failure_cause,completeness
                         FROM rollout_test_runs"""
                )
            ])

        self.assertEqual(observed, [
            [("pytest", "full", "unknown", None, "unknown", "conflicted")],
            [("pytest", "full", "unknown", None, "unknown", "conflicted")],
        ])

    def test_append_terminal_restores_persisted_intent_without_trusting_lower_command(self) -> None:
        session, call_id = "append-result-session", "append-result"
        rollout = self.root / "append-result.rollout.jsonl"
        prefix = [
            start(session, self.project),
            call(call_id, "pytest tests/test_live_append.py", 1),
        ]
        write_rollout(rollout, prefix)
        self.ingest(rollout)
        self.assertEqual(tuple(self.store.connection.execute(
            """SELECT runner,scope,outcome,exit_status,completeness
                 FROM rollout_test_runs"""
        ).fetchone()), ("pytest", "targeted", "unknown", None, "intent_only"))

        lower = self.app_source(
            "append-result", session=session, call_id=call_id,
            command="pytest tests/test_other.py", status="completed", exit_code=0,
        )
        ingest_codex_events(
            self.store, (lower,), self.project, "project-tests", hash_key=b"t" * 32,
        )
        write_rollout(rollout, prefix + [
            result(call_id, {"exit_code": 1, "stderr": "assertion failed"}, 2),
        ])
        self.ingest(rollout)

        rows = [tuple(row) for row in self.store.connection.execute(
            """SELECT runner,scope,outcome,exit_status,failure_cause,completeness
                 FROM rollout_test_runs"""
        )]
        self.assertEqual(rows, [
            ("pytest", "targeted", "failed", 1, "product_failure", "complete"),
        ])
        dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn("pytest tests/test_live_append.py", dump)
        self.assertNotIn("pytest tests/test_other.py", dump)
        self.assertNotIn("assertion failed", dump)

    def test_authoritative_non_test_description_suppresses_lower_app_test_claim(self) -> None:
        session, call_id = "non-test-description-session", "non-test-description"
        rollout = self.root / "non-test-description.rollout.jsonl"
        write_rollout(rollout, [
            start(session, self.project),
            call(call_id, "false", 1),
            result(call_id, {"exit_code": 1}, 2),
        ])
        app = self.app_source(
            "non-test-description", session=session, call_id=call_id,
            command="pytest tests/test_false_positive.py", status="completed",
            exit_code=0,
        )

        for index, app_first in enumerate((False, True), start=1):
            store = HydraStore(self.root / f"non-test-description-{index}.sqlite3")
            self.addCleanup(store.close)
            self.ingest_pair(store, rollout, app, app_first=app_first)
            self.assertEqual(store.count("rollout_test_runs"), 0)
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM test_evidence_candidates"
            ).fetchone()[0], 2)

    def test_canonical_non_exec_tool_suppresses_lower_app_test_claim(self) -> None:
        session, call_id = "non-exec-tool-session", "non-exec-tool"
        rollout = self.root / "non-exec-tool.rollout.jsonl"
        write_rollout(rollout, [
            start(session, self.project),
            event("response_item", {
                "type": "function_call", "call_id": call_id,
                "name": "apply_patch",
                "arguments": json.dumps({"patch": "safe"}),
            }, 1),
            result(call_id, {"exit_code": 0}, 2),
        ])
        app = self.app_source(
            "non-exec-tool", session=session, call_id=call_id,
            command="pytest tests/test_false_positive.py", status="completed",
            exit_code=0,
        )

        for index, app_first in enumerate((False, True), start=1):
            store = HydraStore(self.root / f"non-exec-tool-{index}.sqlite3")
            self.addCleanup(store.close)
            self.ingest_pair(store, rollout, app, app_first=app_first)
            self.assertEqual(tuple(store.connection.execute(
                "SELECT tool_name,terminal_state FROM tool_spans"
            ).fetchone()), ("apply_patch", "success"))
            self.assertEqual(store.count("rollout_test_runs"), 0)

    def test_terminal_app_non_execution_suppresses_rollout_intent_in_both_orders(self) -> None:
        session, call_id = "declined-intent-session", "declined-intent"
        rollout = self.root / "declined-intent.rollout.jsonl"
        write_rollout(rollout, [
            start(session, self.project),
            call(call_id, "pytest tests/test_never_ran.py", 1),
        ])
        app = self.app_source(
            "declined-intent", session=session, call_id=call_id,
            command="pytest tests/test_never_ran.py", status="declined",
        )

        for index, app_first in enumerate((False, True), start=1):
            store = HydraStore(self.root / f"declined-intent-{index}.sqlite3")
            self.addCleanup(store.close)
            self.ingest_pair(store, rollout, app, app_first=app_first)
            self.assertEqual(store.count("rollout_test_runs"), 0)

    def test_terminal_app_non_execution_does_not_erase_complete_rollout_result(self) -> None:
        session, call_id = "declined-complete-session", "declined-complete"
        rollout = self.root / "declined-complete.rollout.jsonl"
        write_rollout(rollout, [
            start(session, self.project),
            call(call_id, "pytest tests/test_did_run.py", 1),
            result(call_id, {"exit_code": 1, "stderr": "assertion failed"}, 2),
        ])
        app = self.app_source(
            "declined-complete", session=session, call_id=call_id,
            command="pytest tests/test_did_run.py", status="declined",
        )

        observed: list[list[tuple[object, ...]]] = []
        for index, app_first in enumerate((False, True), start=1):
            store = HydraStore(self.root / f"declined-complete-{index}.sqlite3")
            self.addCleanup(store.close)
            self.ingest_pair(store, rollout, app, app_first=app_first)
            observed.append([
                tuple(row) for row in store.connection.execute(
                    "SELECT outcome,exit_status,failure_cause FROM rollout_test_runs"
                )
            ])
        self.assertEqual(observed, [
            [("failed", 1, "product_failure")],
            [("failed", 1, "product_failure")],
        ])

    def test_custom_exec_test_intents_link_to_nested_spans_but_never_inherit_broker_success(self) -> None:
        path = self.root / "modern.jsonl"
        program = (
            'tools.exec_command({"cmd":"pytest tests/a.py"});'
            'tools.exec_command({"cmd":"vitest run tests/b.test.ts"});'
        )
        write_rollout(path, [
            start("modern", self.project),
            event("turn_context", {"turn_id": "turn-modern"}, 1),
            event("response_item", {"type": "custom_tool_call", "call_id": "outer", "name": "exec", "input": program}, 2),
            event("response_item", {"type": "custom_tool_call_output", "call_id": "outer", "output": [
                {"type": "input_text", "text": "Script completed\nWall time: 0.1 seconds"},
            ]}, 3),
        ])

        self.ingest(path)

        outer = self.store.connection.execute(
            "SELECT call_key FROM tool_spans WHERE tool_name = 'custom_exec'"
        ).fetchone()[0]
        nested = {
            row[0] for row in self.store.connection.execute(
                "SELECT call_key FROM tool_spans WHERE tool_name = 'exec_command'"
            )
        }
        rows = self.store.connection.execute(
            """SELECT tool_call_key, exit_status, outcome, failure_cause, retry_kind,
                      attempt_ordinal, provenance, completeness, observed_at, turn_key
                 FROM rollout_test_runs ORDER BY runner"""
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row[0] for row in rows}, nested)
        self.assertNotIn(outer, nested)
        for row in rows:
            self.assertEqual(tuple(row[:8]),
                             (row[0], None, "unknown", "unknown", "none", 1, "derived", "intent_only"))
            self.assertEqual(row[8], "2026-07-21T00:00:02Z")
            self.assertIsNotNone(row[9])
        dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn("pytest tests/a.py", dump)
        self.assertNotIn("Script completed", dump)

    def test_reconcile_retries_globally_across_resume_sources_and_isolates_siblings(self) -> None:
        first = self.root / "first.jsonl"
        resumed = self.root / "resumed.jsonl"
        sibling = self.root / "sibling.jsonl"
        write_rollout(first, [
            start("root", self.project),
            call("flake-1", "pytest tests/flake.py", 1),
            result("flake-1", {"exit_code": 1, "stderr": "assertion failed"}, 2),
            call("fixed-1", "pytest tests/fixed.py", 3),
            result("fixed-1", {"exit_code": 1, "stderr": "assertion failed"}, 4),
            call("infra-1", "pytest tests/infra.py", 5),
            result("infra-1", {"exit_code": 1, "output": "sandbox unavailable"}, 6),
            event("response_item", {"type": "custom_tool_call", "call_id": "unknown-1", "name": "exec",
                                    "input": 'tools.exec_command({"cmd":"pytest tests/unknown.py"});'}, 7),
        ])
        write_rollout(resumed, [
            start("root", self.project, 8),
            event("turn_context", {"turn_id": "resumed-turn"}, 8),
            call("flake-2", "pytest tests/flake.py", 9),
            result("flake-2", {"exit_code": 0}, 10),
            event("event_msg", {"type": "patch_apply_end", "call_id": "patch", "success": True,
                                "changes": {"src/fixed.py": {"type": "modify"}}}, 11),
            call("fixed-2", "pytest tests/fixed.py", 12),
            result("fixed-2", {"exit_code": 0}, 13),
            call("infra-2", "pytest tests/infra.py", 14),
            result("infra-2", {"exit_code": 0}, 15),
            call("unknown-2", "pytest tests/unknown.py", 16),
            result("unknown-2", {"exit_code": 0}, 17),
        ])
        write_rollout(sibling, [
            start("child", self.project),
            call("flake-child", "pytest tests/flake.py", 9),
            result("flake-child", {"exit_code": 0}, 10),
        ])

        self.ingest(resumed, sibling, first)
        rows = self.store.connection.execute(
            """SELECT session_key, runner, scope, outcome, failure_cause, retry_kind, attempt_ordinal,
                      exit_status, provenance, completeness
                 FROM rollout_test_runs ORDER BY session_key, command_hash, attempt_ordinal"""
        ).fetchall()
        root_key = self.store.connection.execute(
            """SELECT session_key FROM rollout_test_runs
                 GROUP BY session_key HAVING COUNT(*) > 1"""
        ).fetchone()[0]
        # Select by retry kind rather than opaque command hashes.
        retries = {row[5]: row for row in rows if row[0] == root_key and row[5] != "none"}
        self.assertEqual(set(retries), {"flaky_retry", "product_fix_verification", "infra_recovery", "unknown_recovery"})
        for retry, row in retries.items():
            self.assertEqual((row[3], row[6], row[7], row[8], row[9]),
                             ("success", 2, 0, "derived", "complete"), retry)
        write = self.store.connection.execute(
            """SELECT source_digest, line_number, observed_at, turn_key
                 FROM file_observations WHERE operation = 'write'"""
        ).fetchone()
        self.assertTrue(write[0])
        self.assertEqual(write[1], 0)
        self.assertEqual(write[2], "2026-07-21T00:00:11Z")
        self.assertIsNotNone(write[3])
        child_rows = [row for row in rows if row[0] != root_key]
        self.assertEqual(len(child_rows), 1)
        self.assertEqual((child_rows[0][5], child_rows[0][6]), ("none", 1))

        before = [tuple(row) for row in self.store.connection.execute(
            """SELECT evidence_key, retry_kind, attempt_ordinal, outcome, failure_cause
                 FROM rollout_test_runs ORDER BY evidence_key"""
        )]
        self.ingest(first, resumed, sibling)
        after = [tuple(row) for row in self.store.connection.execute(
            """SELECT evidence_key, retry_kind, attempt_ordinal, outcome, failure_cause
                 FROM rollout_test_runs ORDER BY evidence_key"""
        )]
        self.assertEqual(after, before)

    def test_invalid_or_conflicting_model_causes_are_quarantined_and_never_override_evidence(self) -> None:
        path = self.root / "conflicts.jsonl"
        secret = "not-an-allowed-model-cause-private"
        write_rollout(path, [
            start("conflicts", self.project),
            call("product", "pytest tests/product.py", 1),
            result("product", {"exit_code": 1, "stderr": "assertion failed"}, 2),
            call("infra", "pytest tests/infra.py", 3),
            result("infra", {"exit_code": 1, "stderr": "network unavailable"}, 4),
            call("matching", "pytest tests/matching.py", 5),
            result("matching", {"exit_code": 1, "stderr": "assertion failed"}, 6),
        ])

        self.ingest(path, model_causes={
            "product": secret,
            "infra": "test_failure",
            "matching": "test_failure",
        })

        causes = [row[0] for row in self.store.connection.execute(
            "SELECT failure_cause FROM rollout_test_runs ORDER BY observed_at"
        )]
        self.assertEqual(causes, ["product_failure", "infra_failure", "product_failure"])
        conflicts = [tuple(row) for row in self.store.connection.execute(
            "SELECT deterministic_cause, model_cause FROM semantic_conflicts ORDER BY line_number"
        )]
        self.assertEqual(conflicts, [("product_failure", "invalid"), ("infra_failure", "test_failure")])
        self.assertNotIn(secret, "\n".join(self.store.connection.iterdump()))


if __name__ == "__main__":
    unittest.main()
