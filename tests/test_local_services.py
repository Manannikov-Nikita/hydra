from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import tempfile
from threading import Barrier
import unittest
from unittest import mock

from hydra_codex.cli import main
from hydra_codex.contracts import ModelAnnotationInput
from hydra_codex.exact_time import require_exact_timestamp
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.services import LocalCommandServices
from hydra_codex.storage import HydraStore
from hydra_codex.sync_state import SyncStateRepository
from integrations.codex.hook import handle_event


NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)


def invoke(argv: list[str], *, environ: dict[str, str], stdin: str = ""):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        argv,
        stdin=io.StringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        environ=environ,
    )
    return code, stdout.getvalue(), stderr.getvalue()


class LocalCommandServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_local_services"\n', encoding="utf-8",
        )
        self.database = self.root / "private" / "hydra.sqlite3"
        self.database.parent.mkdir()
        self.key = self.root / "private" / "rollout-hmac.key"
        self.environ = {
            "HOME": str(self.root),
            "TMPDIR": str(self.root / "tmp"),
            "HYDRA_DATABASE_PATH": str(self.database),
            "HYDRA_INSTALLATION_KEY_PATH": str(self.key),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _open_turn(self) -> str:
        response = handle_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "private-session",
                "turn_id": "private-turn",
                "cwd": str(self.project),
                "prompt": "never persist this prompt",
            },
            environ=self.environ,
            clock=lambda: NOW,
        )
        instruction = str(response["hookSpecificOutput"]["additionalContext"])
        match = re.search(r"hcap_v1_[A-Za-z0-9_-]{43}", instruction)
        self.assertIsNotNone(match)
        return match.group(0)

    def test_hook_enqueues_only_a_trusted_root_relative_transcript_once(self) -> None:
        source = self.root / ".codex" / "sessions" / "2026" / "rollout.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text("{}\n", encoding="utf-8")
        payload = {
            "hook_event_name": "UserPromptSubmit", "session_id": "private-session",
            "turn_id": "private-turn", "cwd": str(self.project),
            "transcript_path": str(source), "prompt": "must not be stored",
        }
        handle_event(payload, environ=self.environ, clock=lambda: NOW)
        handle_event(payload, environ=self.environ, clock=lambda: NOW)
        store = HydraStore(self.database)
        try:
            queue = SyncStateRepository(store).list_queue()
            self.assertEqual([(item.root_kind, item.source_locator) for item in queue], [
                ("sessions", "2026/rollout.jsonl"),
            ])
            self.assertNotIn("must not be stored", repr(queue))
        finally:
            store.close()

    def test_hook_does_not_enqueue_an_untrusted_transcript_path(self) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit", "session_id": "private-session",
            "turn_id": "private-turn", "cwd": str(self.project),
            "transcript_path": "/private/untrusted.jsonl",
        }
        handle_event(payload, environ=self.environ, clock=lambda: NOW)
        store = HydraStore(self.database)
        try:
            self.assertEqual(SyncStateRepository(store).list_queue(), ())
        finally:
            store.close()

    def test_post_tool_hook_durably_records_only_safe_fact_and_is_idempotent(self) -> None:
        payload = {
            "hook_event_name": "PostToolUse", "session_id": "private-session",
            "turn_id": "private-turn", "cwd": str(self.project),
            "tool_name": "Bash", "duration_ms": 12, "tool_input": "secret=input",
            "tool_response": "secret=output",
        }
        handle_event(payload, environ=self.environ, clock=lambda: NOW)
        handle_event(payload, environ=self.environ, clock=lambda: NOW)
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT event_kind,tool_category,duration_ms FROM hook_event_outbox"
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("post_tool", "other", 12)])
            self.assertNotIn("secret", repr(rows))
        finally:
            store.close()

    def test_distinct_post_tool_call_ids_have_distinct_private_outbox_events(self) -> None:
        first = {
            "hook_event_name": "PostToolUse", "session_id": "private-session",
            "turn_id": "private-turn", "cwd": str(self.project),
            "tool_use_id": "private-tool-call-a", "tool_category": "shell",
            "tool_status": "success", "duration_ms": 12,
        }
        second = {**first, "tool_use_id": "private-tool-call-b", "duration_ms": 20}
        handle_event(first, environ=self.environ, clock=lambda: NOW)
        handle_event(second, environ=self.environ, clock=lambda: NOW)
        handle_event(first, environ=self.environ, clock=lambda: NOW)
        handle_event(second, environ=self.environ, clock=lambda: NOW)
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT event_key,event_kind,tool_category,tool_status,duration_ms FROM hook_event_outbox "
                "ORDER BY duration_ms"
            ).fetchall()
            self.assertEqual(
                [tuple(row[1:]) for row in rows],
                [("post_tool", "shell", "success", 12), ("post_tool", "shell", "success", 20)],
            )
            self.assertNotIn("private-tool-call", repr(rows))
        finally:
            store.close()

    def test_cli_sync_consumes_post_tool_and_lifecycle_outbox_facts(self) -> None:
        common = {
            "session_id": "private-session", "turn_id": "private-turn",
            "cwd": str(self.project),
        }
        handle_event(
            {"hook_event_name": "UserPromptSubmit", **common, "prompt": "not persisted"},
            environ=self.environ, clock=lambda: NOW,
        )
        handle_event(
            {"hook_event_name": "PostToolUse", **common, "tool_use_id": "private-call",
             "tool_category": "shell", "tool_status": "success", "duration_ms": 5},
            environ=self.environ, clock=lambda: NOW,
        )
        handle_event(
            {"hook_event_name": "Stop", "stop_hook_active": True, **common},
            environ=self.environ, clock=lambda: NOW,
        )
        sync = invoke(
            ["sync", "--db", str(self.database), "--cwd", str(self.project)],
            environ=self.environ,
        )
        self.assertEqual(sync[0], 0, sync[2])
        store = HydraStore(self.database)
        try:
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM hook_event_outbox WHERE acknowledged_at IS NOT NULL"
            ).fetchone()[0], 3)
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM hook_safe_facts"
            ).fetchone()[0], 3)
            self.assertNotIn("private-call", repr(store.connection.execute(
                "SELECT * FROM hook_event_outbox"
            ).fetchall()))
        finally:
            store.close()

    def test_sync_and_explicit_repair_are_private_bounded_commands(self) -> None:
        sync = invoke(
            ["sync", "--db", str(self.database), "--cwd", str(self.project)],
            environ=self.environ,
        )
        repair = invoke(
            ["repair", "--all", "--db", str(self.database), "--cwd", str(self.project)],
            environ=self.environ,
        )
        self.assertEqual(sync[0], 0, sync[2])
        self.assertEqual(repair[0], 0, repair[2])
        sync_payload = json.loads(sync[1])
        repair_payload = json.loads(repair[1])
        self.assertEqual(sync_payload["command"], "sync")
        self.assertEqual(repair_payload["command"], "repair")
        self.assertNotIn(str(self.root), sync[1] + repair[1])

    def test_cli_repair_all_runs_every_bounded_batch_to_completion(self) -> None:
        sessions = self.root / ".codex" / "sessions"
        for index in range(101):
            (sessions / f"batch-{index:03d}").mkdir(parents=True)

        code, stdout, stderr = invoke(
            ["repair", "--all", "--db", str(self.database), "--cwd", str(self.project)],
            environ=self.environ,
        )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["directories_scanned"], 102)
        self.assertEqual(payload["sources_discovered"], 0)
        self.assertEqual(payload["batches"], 3)
        store = HydraStore(self.database)
        try:
            jobs = SyncStateRepository(store).list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].state, "succeeded")
            self.assertEqual(
                SyncStateRepository(store).resume_frontier(jobs[0].job_id), (),
            )
        finally:
            store.close()

    def test_cli_repair_all_reuses_existing_frontier_without_recounting_prior_batch(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        sessions = self.root / ".codex" / "sessions"
        for index in range(3):
            (sessions / f"resume-{index}").mkdir(parents=True)
        (sessions / "discovered-before-resume.jsonl").write_text(
            "", encoding="utf-8",
        )
        store = HydraStore(self.database)
        try:
            repair = ResumableRepair(
                store,
                TrustedSourceRoots(
                    sessions=sessions,
                    archived_sessions=self.root / ".codex" / "archived_sessions",
                ),
            )
            job_id = repair.start_backfill(
                "2026-07-21T00:00:00Z", job_kind="repair",
            )
            first = repair.run_batch(
                job_id, "2026-07-21T00:00:00Z", directory_limit=1,
            )
            self.assertEqual(first.directories_scanned, 1)
            self.assertEqual(first.discovered, 1)
            self.assertFalse(first.completed)
        finally:
            store.close()

        code, stdout, stderr = invoke(
            ["repair", "--all", "--db", str(self.database), "--cwd", str(self.project)],
            environ=self.environ,
        )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["diagnostic"], "repair_required")
        self.assertEqual(payload["directories_scanned"], 3)
        # The first invocation already persisted this source in the job total;
        # this invocation reports only newly discovered sources.
        self.assertEqual(payload["sources_discovered"], 0)
        store = HydraStore(self.database)
        try:
            jobs = SyncStateRepository(store).list_jobs()
            self.assertEqual([job.job_id for job in jobs], [job_id])
            self.assertEqual(jobs[0].state, "partial")
            self.assertEqual(jobs[0].sources_discovered, 1)
        finally:
            store.close()

    def test_cli_repair_all_stops_with_partial_diagnostic_when_batch_cannot_progress(self) -> None:
        from hydra_codex.incremental_sync import RepairRun

        (self.root / ".codex" / "sessions").mkdir(parents=True)
        with mock.patch(
            "hydra_codex.incremental_sync.ResumableRepair.run_batch",
            return_value=RepairRun(0, 0, False),
        ) as run_batch:
            code, stdout, stderr = invoke(
                ["repair", "--all", "--db", str(self.database), "--cwd", str(self.project)],
                environ=self.environ,
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(run_batch.call_count, 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["diagnostic"], "no_progress")
        self.assertEqual(payload["batches"], 1)

    def test_sync_freshness_prioritizes_any_repair_required_source_over_queue_order(self) -> None:
        store = HydraStore(self.database)
        try:
            repository = SyncStateRepository(store)
            repository.register_and_enqueue(
                root_kind="sessions", source_locator="a/queued.jsonl",
                observed_at="2026-07-21T00:00:00Z",
            )
            repository.register_source(
                root_kind="sessions", source_locator="z/repair.jsonl",
            )
            repository.mark_repair_required(
                "sessions", "z/repair.jsonl", "2026-07-21T00:00:01Z",
            )
            freshness = LocalCommandServices(environ=self.environ)._sync_freshness(store)
        finally:
            store.close()
        self.assertEqual(freshness["state"], "repair_required")

    def test_sync_freshness_treats_expired_ingest_claim_as_queued_retry(self) -> None:
        store = HydraStore(self.database)
        try:
            repository = SyncStateRepository(store)
            repository.register_and_enqueue(
                root_kind="sessions", source_locator="a/expired.jsonl",
                observed_at="2026-07-21T00:00:00Z",
            )
            self.assertTrue(repository.acquire_lease(
                "worker", "2026-07-21T00:00:00Z", "2026-07-21T00:10:00Z",
            ))
            repository.claim_next("worker", "2026-07-21T00:00:00Z", "2026-07-21T00:01:00Z")
            freshness = LocalCommandServices(
                environ=self.environ,
                clock=lambda: datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc),
            )._sync_freshness(store)
        finally:
            store.close()
        self.assertEqual(freshness["state"], "queued")

    def test_sync_freshness_treats_expired_outbox_claim_as_queued_retry(self) -> None:
        store = HydraStore(self.database)
        try:
            repository = SyncStateRepository(store)
            repository.record_hook_event_and_enqueue(
                event_key="expired-event", project_id="hprj_local_services",
                session_key="session-safe", turn_key="turn-safe", event_kind="post_tool",
                observed_at="2026-07-21T00:00:00Z",
            )
            self.assertTrue(repository.acquire_lease(
                "worker", "2026-07-21T00:00:00Z", "2026-07-21T00:10:00Z",
            ))
            repository.claim_hook_events("worker", "2026-07-21T00:00:00Z", "2026-07-21T00:01:00Z")
            freshness = LocalCommandServices(
                environ=self.environ,
                clock=lambda: datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc),
            )._sync_freshness(store)
        finally:
            store.close()
        self.assertEqual(freshness["state"], "queued")

    def test_sync_freshness_keeps_unexpired_claims_running(self) -> None:
        store = HydraStore(self.database)
        try:
            repository = SyncStateRepository(store)
            repository.register_and_enqueue(
                root_kind="sessions", source_locator="a/running.jsonl",
                observed_at="2026-07-21T00:00:00Z",
            )
            self.assertTrue(repository.acquire_lease(
                "worker", "2026-07-21T00:00:00Z", "2026-07-21T00:10:00Z",
            ))
            repository.claim_next("worker", "2026-07-21T00:00:00Z", "2026-07-21T00:03:00Z")
            freshness = LocalCommandServices(
                environ=self.environ,
                clock=lambda: datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc),
            )._sync_freshness(store)
        finally:
            store.close()
        self.assertEqual(freshness["state"], "running")

    def test_sync_freshness_expires_whole_second_claim_before_fractional_now(self) -> None:
        store = HydraStore(self.database)
        try:
            repository = SyncStateRepository(store)
            repository.register_and_enqueue(
                root_kind="sessions", source_locator="a/fractional-expiry.jsonl",
                observed_at="2026-07-21T09:59:58Z",
            )
            self.assertTrue(repository.acquire_lease(
                "worker", "2026-07-21T09:59:58Z", "2026-07-21T10:01:00Z",
            ))
            repository.claim_next(
                "worker", "2026-07-21T09:59:59Z", "2026-07-21T10:00:00Z",
            )
            freshness = LocalCommandServices(
                environ=self.environ,
                clock=lambda: datetime(
                    2026, 7, 21, 10, 0, 0, 100_000,
                    tzinfo=timezone.utc,
                ),
            )._sync_freshness(store)
        finally:
            store.close()

        self.assertEqual(freshness["state"], "queued")

    def _annotate(self, capability: str, *, finish: bool = False):
        payload = {
            "kind": "finish" if finish else "phase",
            "phase": "test_full" if finish else "implement",
            "cause": "final_verification" if finish else "plan",
            "scope_change": "none",
            "task_family": "local-services",
            "confidence": 1.0 if finish else 0.9,
            "note": "done" if finish else "implementation",
        }
        if finish:
            payload["outcome"] = "success"
        environment = {**self.environ, "HYDRA_TURN_CAPABILITY": capability}
        return invoke(
            [
                "annotate", "--db", str(self.database),
                "--cwd", str(self.project),
            ],
            environ=environment,
            stdin=json.dumps(payload),
        )

    def _rollout(self, name: str, *, second: int, input_tokens: int) -> Path:
        source = self.root / "rollouts" / f"{name}.jsonl"
        source.parent.mkdir(exist_ok=True)
        records = (
            {
                "timestamp": f"2026-07-21T00:00:{second:02d}Z",
                "type": "session_meta",
                "payload": {"id": name, "cwd": str(self.project)},
            },
            {
                "timestamp": f"2026-07-21T00:00:{second + 1:02d}Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": f"turn-{name}"},
            },
            {
                "timestamp": f"2026-07-21T00:00:{second + 2:02d}Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": 10,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "cache_write_input_tokens": 0,
                        "total_tokens": input_tokens + 20,
                    }},
                },
            },
            {
                "timestamp": f"2026-07-21T00:00:{second + 3:02d}Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": f"turn-{name}"},
            },
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )
        return source

    def test_annotation_cli_stages_only_private_semantics_without_writing_sqlite(self) -> None:
        capability = self._open_turn()
        before = self.database.read_bytes()

        result = self._annotate(capability)

        self.assertEqual(result, (0, '{"command":"annotate","status":"ok"}\n', ""))
        self.assertEqual(self.database.read_bytes(), before)
        spool = self.root / "tmp" / "Hydra" / "spool"
        self.assertEqual(spool.stat().st_mode & 0o777, 0o700)
        envelopes = tuple(spool.glob("*.json"))
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].stat().st_mode & 0o777, 0o600)
        envelope = json.loads(envelopes[0].read_text(encoding="utf-8"))
        self.assertEqual(set(envelope), {"capability", "payload", "request_nonce"})
        self.assertEqual(envelope["capability"], capability)
        self.assertRegex(envelope["request_nonce"], r"^hreq_v1_[A-Za-z0-9_-]{32}$")
        self.assertEqual(set(envelope["payload"]), {
            "kind", "phase", "cause", "scope_change", "task_family",
            "confidence", "note",
        })
        serialized = json.dumps(envelope, sort_keys=True)
        for forbidden in (
            "private-session", "private-turn", "observed_at", "sequence",
            "project_id", "session_id", "turn_id",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_default_cli_accepts_hook_capability_for_phase_and_finish(self) -> None:
        capability = self._open_turn()

        phase = self._annotate(capability)
        phase_drain = handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "private-session",
                "turn_id": "private-turn",
                "cwd": str(self.project),
                "tool_name": "exec_command",
                "tool_input": "private command",
                "tool_response": "private output",
            },
            environ=self.environ,
            clock=lambda: datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc),
        )
        finish = self._annotate(capability, finish=True)
        finish_drain = handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "private-session",
                "turn_id": "private-turn",
                "cwd": str(self.project),
                "tool_name": "exec_command",
                "tool_input": "private command",
                "tool_response": "private output",
            },
            environ=self.environ,
            clock=lambda: datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(phase, (0, '{"command":"annotate","status":"ok"}\n', ""))
        self.assertEqual(finish, (0, '{"command":"annotate","status":"ok"}\n', ""))
        self.assertEqual((phase_drain, finish_drain), ({}, {}))
        self.assertEqual(tuple((self.root / "tmp" / "Hydra" / "spool").glob("*.json")), ())
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT sequence,kind,phase,cause,task_family,provenance "
                "FROM annotations ORDER BY sequence"
            ).fetchall()
            binding = store.connection.execute(
                "SELECT state,last_sequence FROM trusted_turn_bindings"
            ).fetchone()
        finally:
            store.close()
        self.assertEqual([tuple(row) for row in rows], [
            (0, "phase", "understand", "prompt", "unclassified", "derived"),
            (1, "phase", "implement", "plan", "local-services", "model_reported"),
            (2, "finish", "test_full", "final_verification", "local-services", "model_reported"),
        ])
        self.assertEqual(tuple(binding), ("finished", 2))
        database = self.database.read_bytes()
        for secret in (
            capability, "private-session", "private-turn", "never persist this prompt",
            "private command", "private output",
        ):
            self.assertNotIn(secret.encode(), database)

    def test_host_hook_not_cli_enforces_capability_project_binding(self) -> None:
        capability = self._open_turn()
        foreign = self.root / "foreign"
        (foreign / ".hydra").mkdir(parents=True)
        (foreign / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_foreign"\n', encoding="utf-8",
        )
        environment = {**self.environ, "HYDRA_TURN_CAPABILITY": capability}
        code, stdout, stderr = invoke(
            [
                "annotate", "--db", str(self.database),
                "--cwd", str(foreign),
                "--kind", "phase", "--phase", "implement", "--cause", "plan",
                "--scope-change", "none", "--task-family", "integration-test",
                "--confidence", "0.8", "--note", "private foreign note",
            ],
            environ=environment,
        )

        self.assertEqual((code, stdout, stderr), (
            0, '{"command":"annotate","status":"ok"}\n', "",
        ))
        staged = tuple((self.root / "tmp" / "Hydra" / "spool").glob("*.json"))
        self.assertEqual(len(staged), 1)
        self.assertEqual(handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "private-session", "turn_id": "private-turn",
                "cwd": str(foreign),
            },
            environ=self.environ,
            clock=lambda: datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc),
        ), {})
        self.assertTrue(staged[0].exists())
        store = HydraStore(self.database)
        try:
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM annotations"
            ).fetchone()[0], 1)
        finally:
            store.close()
        self.assertEqual(handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "private-session", "turn_id": "private-turn",
                "cwd": str(self.project),
            },
            environ=self.environ,
            clock=lambda: datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc),
        ), {})
        self.assertFalse(staged[0].exists())

    def test_concurrent_phase_writes_allocate_distinct_sequences(self) -> None:
        capability = self._open_turn()
        ready = Barrier(2)

        def write_phase(index: int):
            service = LocalCommandServices(
                environ=self.environ,
                clock=lambda: datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc),
            )
            ready.wait(timeout=2)
            return service.annotate(
                ModelAnnotationInput.from_mapping({
                    "kind": "phase",
                    "phase": "implement" if index == 0 else "test_targeted",
                    "cause": "plan",
                    "scope_change": "none",
                    "task_family": "local-services",
                    "confidence": 0.9,
                    "note": f"phase-{index}",
                }),
                capability,
                self.database,
                self.project,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            writes = tuple(pool.map(write_phase, range(2)))

        self.assertEqual(len({item.name for item in writes}), 2)
        self.assertTrue(all(item.is_file() for item in writes))
        self.assertEqual(handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "private-session", "turn_id": "private-turn",
                "cwd": str(self.project),
            },
            environ=self.environ,
            clock=lambda: datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc),
        ), {})
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT sequence,phase FROM annotations ORDER BY sequence"
            ).fetchall()
        finally:
            store.close()
        self.assertEqual(rows[0]["phase"], "understand")
        self.assertEqual({row["sequence"] for row in rows[1:]}, {1, 2})
        self.assertEqual(
            {row["phase"] for row in rows[1:]}, {"implement", "test_targeted"},
        )

    def test_ingest_uses_same_environment_database_and_key_as_hooks(self) -> None:
        source = self.root / "rollout.jsonl"
        source.write_text(json.dumps({
            "timestamp": "2026-07-21T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "safe-session", "cwd": str(self.project)},
        }) + "\n", encoding="utf-8")
        seen_databases: list[Path | None] = []
        seen_keys: list[Path] = []
        real_store = HydraStore

        def store_factory(path):
            seen_databases.append(path)
            return real_store(self.database)

        def key_loader(path):
            seen_keys.append(path)
            return Pseudonymizer(b"i" * 32)

        with (
            mock.patch("hydra_codex.cli.HydraStore", side_effect=store_factory),
            mock.patch(
                "hydra_codex.cli.Pseudonymizer.installation_key",
                side_effect=key_loader,
            ),
        ):
            result = invoke(
                [
                    "ingest", "--cwd", str(self.project),
                    "--source", f"explicit={source}",
                ],
                environ=self.environ,
            )

        self.assertEqual(result[0], 0)
        self.assertEqual(seen_databases, [self.database])
        self.assertEqual(seen_keys, [self.key])

    def test_annotation_cli_does_not_assign_time_or_open_sqlite(self) -> None:
        capability = self._open_turn()
        before = self.database.read_bytes()

        def clock() -> datetime:
            raise AssertionError("model-side annotation must not assign trusted time")

        service = LocalCommandServices(environ=self.environ, clock=clock)
        result = service.annotate(
            ModelAnnotationInput.from_mapping({
                "kind": "phase", "phase": "review", "cause": "plan",
                "scope_change": "none", "task_family": "local-services",
                "confidence": 1.0, "note": "transaction ordering",
            }),
            capability,
            self.database,
            self.project,
        )

        self.assertTrue(result.is_file())
        self.assertEqual(self.database.read_bytes(), before)

    def test_ingest_reconcile_report_and_compare_work_without_injected_services(self) -> None:
        self._rollout("task-a", second=10, input_tokens=100)
        self._rollout("task-b", second=20, input_tokens=180)
        common = ["--db", str(self.database), "--cwd", str(self.project)]

        ingested = invoke(
            [
                "ingest", *common,
                "--source", f"explicit={self.root / 'rollouts'}",
            ],
            environ=self.environ,
        )
        reconciled = invoke(["reconcile", *common], environ=self.environ)
        rendered = invoke(
            ["report", *common, "--last", "2", "--format", "json"],
            environ=self.environ,
        )

        self.assertEqual(ingested[0], 0, ingested[2])
        self.assertEqual(reconciled, (
            0, '{"command":"reconcile","status":"ok"}\n', "",
        ))
        self.assertEqual(rendered[0], 0, rendered[2])
        payload = json.loads(rendered[1])
        self.assertEqual(payload["schema_version"], "hydra.report-list/v2")
        self.assertEqual(payload["sync_freshness"]["schema_version"], "hydra.sync-freshness/v1")
        self.assertEqual(len(payload["reports"]), 2)
        self.assertEqual(
            {item["schema_version"] for item in payload["reports"]},
            {"hydra.report/v4"},
        )
        self.assertTrue(all(
            item["sync_freshness"] == payload["sync_freshness"]
            for item in payload["reports"]
        ))
        refs = [item["task_ref"] for item in payload["reports"]]
        self.assertTrue(all(re.fullmatch(r"task_[0-9a-f]+", item) for item in refs))
        self.assertEqual(
            {
                item["semantic"]["breakdown"]["marker_count"]["value"]
                for item in payload["reports"]
            },
            {0},
        )

        compared = invoke(
            ["compare", refs[1], refs[0], *common, "--format", "markdown"],
            environ=self.environ,
        )
        self.assertEqual(compared[0], 0, compared[2])
        self.assertIn("# Hydra task comparison", compared[1])
        self.assertIn(refs[0].replace("_", r"\_"), compared[1])
        self.assertNotIn("task-a", compared[1])
        self.assertNotIn("task-b", compared[1])

    def test_report_uses_materialized_snapshot_without_source_assembly(self) -> None:
        self._rollout("snapshot-task", second=10, input_tokens=100)
        common = ["--db", str(self.database), "--cwd", str(self.project)]
        self.assertEqual(invoke(
            ["ingest", *common, "--source", f"explicit={self.root / 'rollouts'}"],
            environ=self.environ,
        )[0], 0)
        self.assertEqual(invoke(["reconcile", *common], environ=self.environ)[0], 0)
        with mock.patch(
            "hydra_codex.reconcile_engine._assemble_project",
            side_effect=AssertionError("report must not reassemble"),
        ), mock.patch.object(
            HydraStore,
            "_validate_schema",
            side_effect=AssertionError("report repeated the full database audit"),
        ):
            service = LocalCommandServices(environ=self.environ)
            rendered = service.report(
                1, "json", self.database, self.project,
            )
            service.report(1, "json", self.database, self.project)
        self.assertEqual(json.loads(rendered)["reports"][0]["schema_version"], "hydra.report/v4")

    def test_report_pins_materialized_rows_freshness_and_revision_before_concurrent_enqueue(
        self,
    ) -> None:
        from hydra_codex.reconcile_engine import render_materialized_report_collection
        from hydra_codex.report_renderers import render_json
        from tests.test_audit_builder import public_report

        self._rollout("snapshot-before-enqueue", second=10, input_tokens=100)
        common = ["--db", str(self.database), "--cwd", str(self.project)]
        self.assertEqual(invoke(
            ["ingest", *common, "--source", f"explicit={self.root / 'rollouts'}"],
            environ=self.environ,
        )[0], 0)
        self.assertEqual(invoke(["reconcile", *common], environ=self.environ)[0], 0)
        store = HydraStore(self.database)
        try:
            before_revision = SyncStateRepository(store).data_revision()
            before_ref = str(store.connection.execute(
                """SELECT task_ref FROM materialized_report_snapshots
                     WHERE project_id='hprj_local_services'"""
            ).fetchone()[0])
        finally:
            store.close()
        published = public_report("published-after-enqueue", input_tokens=777, second=20)

        def enqueue_and_publish(store, project_id, limit, output_format, freshness):
            writer = HydraStore(self.database)
            try:
                repository = SyncStateRepository(writer)
                repository.register_and_enqueue(
                    root_kind="sessions",
                    source_locator="concurrent/new.jsonl",
                    project_id=project_id,
                    observed_at="2026-07-21T00:01:00Z",
                )
                revision = repository.data_revision()
                with writer.rollout_transaction() as connection:
                    connection.execute(
                        "DELETE FROM materialized_report_snapshots WHERE project_id=?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM materialized_project_stats WHERE project_id=?",
                        (project_id,),
                    )
                    connection.execute(
                        """INSERT INTO materialized_report_snapshots(
                               project_id,task_ref,report_json,report_markdown,report_html,
                               reconciled_at,data_revision,last_activity_at,
                               last_activity_epoch_ns)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            project_id, published.task_ref,
                            render_json(published).rstrip("\n"), "", "",
                            "2026-07-21T00:01:00Z", revision,
                            published.last_activity_at,
                            require_exact_timestamp(
                                published.last_activity_at,
                                "test report activity",
                            ).epoch_nanoseconds,
                        ),
                    )
            finally:
                writer.close()
            return render_materialized_report_collection(
                store, project_id, limit, output_format, freshness,
            )

        with mock.patch(
            "hydra_codex.reconcile_engine.render_materialized_report_collection",
            side_effect=enqueue_and_publish,
        ):
            first = json.loads(LocalCommandServices(environ=self.environ).report(
                1, "json", self.database, self.project,
            ))

        self.assertEqual(first["reports"][0]["task_ref"], before_ref)
        self.assertEqual(first["sync_freshness"], {
            "schema_version": "hydra.sync-freshness/v1",
            "state": "current",
            "data_revision": before_revision,
        })
        self.assertEqual(
            first["reports"][0]["sync_freshness"],
            first["sync_freshness"],
        )
        second = json.loads(LocalCommandServices(environ=self.environ).report(
            1, "json", self.database, self.project,
        ))
        self.assertEqual(second["reports"][0]["task_ref"], published.task_ref)
        self.assertEqual(second["sync_freshness"]["state"], "queued")
        self.assertEqual(
            second["reports"][0]["sync_freshness"],
            second["sync_freshness"],
        )
        self.assertGreater(
            second["sync_freshness"]["data_revision"], before_revision,
        )

    def test_snapshot_formats_show_current_queued_and_repair_freshness(self) -> None:
        self._rollout("freshness-task", second=10, input_tokens=100)
        common = ["--db", str(self.database), "--cwd", str(self.project)]
        self.assertEqual(invoke(
            ["ingest", *common, "--source", f"explicit={self.root / 'rollouts'}"],
            environ=self.environ,
        )[0], 0)
        self.assertEqual(invoke(["reconcile", *common], environ=self.environ)[0], 0)
        store = HydraStore(self.database)
        try:
            repository = SyncStateRepository(store)
            repository.register_and_enqueue(
                root_kind="sessions", source_locator="a/queued.jsonl",
                observed_at="2026-07-21T00:00:01Z",
            )
        finally:
            store.close()
        service = LocalCommandServices(environ=self.environ)
        queued = {fmt: service.report(1, fmt, self.database, self.project) for fmt in ("json", "markdown", "html")}
        self.assertEqual(json.loads(queued["json"])["sync_freshness"]["state"], "queued")
        self.assertIn("Sync freshness: queued.", queued["markdown"])
        self.assertIn("Sync freshness: queued.", queued["html"])
        store = HydraStore(self.database)
        try:
            repository = SyncStateRepository(store)
            repository.register_source(root_kind="sessions", source_locator="z/repair.jsonl")
            repository.mark_repair_required("sessions", "z/repair.jsonl", "2026-07-21T00:00:02Z")
        finally:
            store.close()
        repair = service.report(1, "json", self.database, self.project)
        self.assertEqual(json.loads(repair)["sync_freshness"]["state"], "repair_required")


if __name__ == "__main__":
    unittest.main()
