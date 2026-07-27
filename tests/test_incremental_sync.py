from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hydra_codex.storage import HydraStore
from hydra_codex.sync_state import SyncStateRepository
from hydra_codex.rollout_identity import Pseudonymizer


def _anchor(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class IncrementalSourceReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "sessions"
        self.root.mkdir()
        self.path = self.root / "2026" / "07" / "rollout.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"type":"session_meta"}\n{"type":"event_msg"}\n')

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _reader(self):
        from hydra_codex.incremental_sync import TrustedSourceRoots
        return TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archived")

    def test_tail_reads_only_complete_appended_lines_with_absolute_ordinals(self) -> None:
        from hydra_codex.incremental_sync import read_incremental_source

        roots = self._reader()
        initial = read_incremental_source(roots, "sessions", "2026/07/rollout.jsonl")
        self.assertEqual([line.ordinal for line in initial.lines], [1, 2])
        self.path.write_bytes(self.path.read_bytes() + b'{"type":"event_msg","n":3}\n{"partial"')
        appended = read_incremental_source(
            roots, "sessions", "2026/07/rollout.jsonl", initial.checkpoint,
        )
        self.assertEqual([line.ordinal for line in appended.lines], [3])
        self.assertEqual(appended.checkpoint.line_number, 3)
        self.assertEqual(appended.checkpoint.byte_offset, len(self.path.read_bytes()) - len(b'{"partial"'))
        self.assertNotIn(b'{"type":"session_meta"}', b''.join(line.value for line in appended.lines))

    def test_tail_claim_is_bounded_by_complete_line_count(self) -> None:
        from hydra_codex.incremental_sync import read_incremental_source

        roots = self._reader()
        first = read_incremental_source(roots, "sessions", "2026/07/rollout.jsonl", max_lines=1)
        self.assertEqual([line.ordinal for line in first.lines], [1])
        self.assertLess(first.checkpoint.byte_offset, self.path.stat().st_size)
        second = read_incremental_source(roots, "sessions", "2026/07/rollout.jsonl", first.checkpoint, max_lines=1)
        self.assertEqual([line.ordinal for line in second.lines], [2])
        self.assertEqual(second.checkpoint.byte_offset, self.path.stat().st_size)

    def test_tail_reports_complete_work_separately_from_eof_partial_line(self) -> None:
        from hydra_codex.incremental_sync import read_incremental_source

        self.path.write_bytes(b'{"n":1}\n{"n":2}\n{"partial"')
        roots = self._reader()
        bounded = read_incremental_source(
            roots, "sessions", "2026/07/rollout.jsonl", max_lines=1,
        )
        self.assertTrue(bounded.has_complete_work)
        self.assertFalse(bounded.partial_line)
        partial = read_incremental_source(
            roots, "sessions", "2026/07/rollout.jsonl", bounded.checkpoint,
        )
        self.assertFalse(partial.has_complete_work)
        self.assertTrue(partial.partial_line)

    def test_oversized_unterminated_line_requires_repair_instead_of_requeue_loop(self) -> None:
        from hydra_codex.incremental_sync import RepairRequired, read_incremental_source

        self.path.write_bytes(b"x" * 33)
        with self.assertRaisesRegex(RepairRequired, "line exceeds bounded tail limit"):
            read_incremental_source(
                self._reader(), "sessions", "2026/07/rollout.jsonl", max_bytes=32,
            )

    def test_rewrite_truncate_inode_and_symlink_are_source_local_repair(self) -> None:
        from hydra_codex.incremental_sync import RepairRequired, read_incremental_source

        roots = self._reader()
        initial = read_incremental_source(roots, "sessions", "2026/07/rollout.jsonl")
        self.path.write_bytes(b'{"rewritten":true}\n')
        with self.assertRaises(RepairRequired):
            read_incremental_source(roots, "sessions", "2026/07/rollout.jsonl", initial.checkpoint)
        replacement = self.path.with_name("replacement.jsonl")
        replacement.write_bytes(b'{"type":"session_meta"}\n{"type":"event_msg"}\n')
        replacement.replace(self.path)
        with self.assertRaises(RepairRequired):
            read_incremental_source(roots, "sessions", "2026/07/rollout.jsonl", initial.checkpoint)
        self.path.unlink()
        self.path.symlink_to(Path(self.temporary.name) / "outside.jsonl")
        with self.assertRaises(RepairRequired):
            read_incremental_source(roots, "sessions", "2026/07/rollout.jsonl")


class IncrementalWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)
        self.repository = SyncStateRepository(self.store)
        self.root = Path(self.temporary.name) / "sessions"
        self.root.mkdir()
        self.path = self.root / "rollout.jsonl"
        self.path.write_bytes(b'{"type":"session_meta"}\n')

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_crash_after_materialization_replays_idempotently_then_acks_and_marks_only_dirty_project(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, MaterializedSource, TrustedSourceRoots

        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl", project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        seen: set[int] = set()

        def materialize(item, tail, connection):
            for line in tail.lines:
                connection.execute("CREATE TABLE IF NOT EXISTS tail_test_events (ordinal INTEGER PRIMARY KEY)")
                connection.execute("INSERT INTO tail_test_events(ordinal) VALUES (?) ON CONFLICT DO NOTHING", (line.ordinal,))
                seen.add(line.ordinal)
            return MaterializedSource(project_id="hprj_safe")

        worker = IncrementalSyncWorker(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"), materialize=materialize)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            worker.sync_once("worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z", crash_after_materialize=True)
        self.assertEqual(self.repository.list_queue()[0].queue_state, "claimed")
        report = worker.sync_once("worker", "2026-07-26T00:02:00Z", "2026-07-26T00:03:00Z")
        self.assertEqual(report.completed, 1)
        self.assertEqual(seen, {1})
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tail_test_events").fetchone()[0], 1)
        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual([(root.project_id, root.root_kind) for root in self.repository.list_dirty_roots()], [("hprj_safe", "project")])

    def test_hook_outbox_replays_post_tool_and_lifecycle_events_after_crash_before_ack(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, MaterializedSource, TrustedSourceRoots

        self.repository.record_hook_event_and_enqueue(
            event_key="prompt-event", project_id="hprj_safe", session_key="session-safe",
            turn_key="turn-safe", event_kind="prompt", observed_at="2026-07-26T00:00:00Z",
        )
        self.repository.record_hook_event_and_enqueue(
            event_key="tool-event", project_id="hprj_safe", session_key="session-safe",
            turn_key="turn-safe", event_kind="post_tool", tool_category="shell",
            tool_status="success", duration_ms=8, observed_at="2026-07-26T00:00:00Z",
            source=("sessions", "rollout.jsonl"),
        )
        self.repository.record_hook_event_and_enqueue(
            event_key="stop-event", project_id="hprj_safe", session_key="session-safe",
            turn_key="turn-safe", event_kind="stop", observed_at="2026-07-26T00:00:00Z",
        )
        materialized: list[int] = []
        reconciled: list[str] = []

        def materialize(_item, tail, _connection):
            materialized.extend(line.ordinal for line in tail.lines)
            return MaterializedSource(project_id="hprj_safe")

        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
            materialize=materialize, reconcile=reconciled.append,
        )
        with self.assertRaisesRegex(RuntimeError, "outbox consume"):
            worker.sync_once(
                "worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z",
                crash_after_outbox_consume=True,
            )
        self.assertEqual(materialized, [1])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM hook_event_outbox WHERE acknowledged_at IS NULL"
        ).fetchone()[0], 3)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM sync_dirty_roots"
        ).fetchone()[0], 1)

        report = worker.sync_once("worker", "2026-07-26T00:02:00Z", "2026-07-26T00:03:00Z")
        self.assertEqual((report.claimed, report.completed), (0, 0))
        self.assertEqual(materialized, [1])
        self.assertEqual(reconciled, ["hprj_safe"])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM hook_event_outbox WHERE acknowledged_at IS NOT NULL"
        ).fetchone()[0], 3)
        self.assertEqual(self.repository.list_dirty_roots(), ())

    def test_unattributed_source_is_requeued_without_a_checkpoint(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl", observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
        )
        worker.sync_once("worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z")
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").byte_offset, 0)
        self.assertEqual(self.repository.list_queue()[0].queue_state, "queued")
        self.assertEqual(self.repository.list_queue()[0].reason_code, "unattributed")

    def test_bounded_tail_requeues_until_large_complete_source_is_drained(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, MaterializedSource, TrustedSourceRoots

        line = b'{"type":"event_msg","payload":{}}\n'
        self.path.write_bytes(line * 35_000)
        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl", project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        seen: list[int] = []

        def materialize(_item, tail, _connection):
            seen.extend(item.ordinal for item in tail.lines)
            return MaterializedSource(project_id="hprj_safe")

        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
            materialize=materialize,
        )
        report = worker.sync_once(
            "worker", "2026-07-26T00:00:00Z", "2026-07-26T00:10:00Z",
        )
        self.assertGreater(report.claimed, 1)
        self.assertEqual(seen, list(range(1, 35_001)))
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").byte_offset, len(line) * 35_000)
        self.assertEqual(self.repository.list_queue(), ())

    def test_materializer_failure_requeues_without_checkpoint_loss(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl", project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )

        def fail(*_args):
            raise RuntimeError("temporary parser failure")

        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
            materialize=fail,
        )
        report = worker.sync_once("worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z")
        self.assertEqual(report.completed, 0)
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").byte_offset, 0)
        self.assertEqual(self.repository.list_queue()[0].reason_code, "transient_failure")

    def test_heartbeat_keeps_short_lease_during_slow_materializer(self) -> None:
        from datetime import datetime, timedelta, timezone
        from hydra_codex.incremental_sync import IncrementalSyncWorker, MaterializedSource, TrustedSourceRoots

        now = datetime.now(timezone.utc).replace(microsecond=0)
        observed = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl", project_id="hprj_safe", observed_at=observed,
        )
        contender: list[bool] = []

        def materialize(*_args):
            time.sleep(0.45)
            other = HydraStore(self.database)
            try:
                other.connection.execute("PRAGMA busy_timeout = 0")
                contender_now = datetime.now(timezone.utc).replace(microsecond=0)
                try:
                    contender.append(SyncStateRepository(other).acquire_lease(
                        "other", contender_now.isoformat().replace("+00:00", "Z"),
                        (contender_now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                    ))
                except Exception:  # SQLite's held writer lock is also a failed acquisition.
                    contender.append(False)
            finally:
                other.close()
            time.sleep(0.45)
            return MaterializedSource(project_id="hprj_safe")

        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
            materialize=materialize,
        )
        self.assertEqual(worker.sync_once("worker", observed, expires).completed, 1)
        self.assertEqual(contender, [False])

    def test_heartbeat_loss_rolls_back_slow_materializer_and_requeues(self) -> None:
        from datetime import datetime, timedelta, timezone
        from hydra_codex.incremental_sync import IncrementalSyncWorker, MaterializedSource, TrustedSourceRoots

        now = datetime.now(timezone.utc).replace(microsecond=0)
        observed = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl", project_id="hprj_safe", observed_at=observed,
        )
        calls = 0
        original = SyncStateRepository.renew_claim

        def lose_on_heartbeat(repository, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                return False
            return original(repository, *args, **kwargs)

        def materialize(_item, _tail, connection):
            connection.execute("CREATE TABLE IF NOT EXISTS heartbeat_rollback (value INTEGER)")
            connection.execute("INSERT INTO heartbeat_rollback(value) VALUES (1)")
            time.sleep(0.7)
            return MaterializedSource(project_id="hprj_safe")

        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
            materialize=materialize,
        )
        with (
            mock.patch.object(SyncStateRepository, "renew_claim", new=lose_on_heartbeat),
            mock.patch.object(SyncStateRepository, "lease_owned", return_value=False),
        ):
            self.assertEqual(worker.sync_once("worker", observed, expires).completed, 0)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(self.repository.list_queue()[0].reason_code, "lease_lost")
        self.assertIsNone(self.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='heartbeat_rollback'"
        ).fetchone())

    def test_normal_sync_never_discovers_or_reads_unqueued_registered_sources(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        for index in range(1500):
            path = self.root / "known" / f"{index}.jsonl"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'{"unread":true}\n')
            self.repository.register_source(root_kind="sessions", source_locator=f"known/{index}.jsonl", observed_at="2026-07-26T00:00:00Z")
        worker = IncrementalSyncWorker(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        with mock.patch("hydra_codex.incremental_sync.read_incremental_source", side_effect=AssertionError("must not read")):
            report = worker.sync_once("worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z")
        self.assertEqual((report.claimed, report.completed, report.repair_required), (0, 0, 0))

    def test_default_materializer_persists_appended_token_and_lifecycle_facts(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.path.write_bytes(
            b'{"type":"session_meta","payload":{"id":"session-1","session_id":"session-1"}}\n'
            b'{"type":"turn_context","payload":{"turn_id":"turn-1"}}\n'
        )
        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl", project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        worker.sync_once("worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z")
        before = self.repository.checkpoint_for("sessions", "rollout.jsonl").byte_offset
        self.path.write_bytes(self.path.read_bytes() + (
            b'{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,"reasoning_output_tokens":2},'
            b'"model_context_window":100}}}\n'
            b'{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1","duration_ms":4}}\n'
        ))
        self.repository.enqueue("sessions", "rollout.jsonl", "2026-07-26T00:02:00Z")
        report = worker.sync_once("worker", "2026-07-26T00:02:00Z", "2026-07-26T00:03:00Z")
        self.assertEqual(report.bytes_processed, len(self.path.read_bytes()) - before)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM token_snapshots").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT event_kind FROM turn_lifecycle_events").fetchone()[0], "completed")
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").line_number, 4)

    def test_reconcile_claims_only_dirty_projects_and_acks_after_success(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.repository.mark_dirty("hprj_one", "task-1", "task", "2026-07-26T00:00:00Z")
        self.repository.mark_dirty("hprj_two", "hprj_two", "project", "2026-07-26T00:00:00Z")
        reconciled: list[str] = []
        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
            reconcile=reconciled.append,
        )
        worker.sync_once("worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z")
        self.assertEqual(reconciled, ["hprj_one", "hprj_two"])
        self.assertEqual(self.repository.list_dirty_roots(), ())

    def test_resumable_repair_uses_scandir_and_registers_without_following_symlinks(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        nested = self.root / "nested"
        nested.mkdir()
        (nested / "one.jsonl").write_text("{}\n")
        (self.root / "escape").symlink_to(Path(self.temporary.name))
        repair = ResumableRepair(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        job = repair.start("2026-07-26T00:00:00Z")
        first = repair.run_batch(job, "2026-07-26T00:00:01Z", directory_limit=1)
        self.assertEqual(first.discovered, 1)
        second = repair.run_batch(job, "2026-07-26T00:00:02Z", directory_limit=2)
        self.assertEqual(second.discovered, 1)
        repair.run_batch(job, "2026-07-26T00:00:03Z", directory_limit=2)
        self.assertEqual(
            [source.source_locator for source in self.repository.list_sources()],
            ["nested/one.jsonl", "rollout.jsonl"],
        )

    def test_repair_reuses_an_active_sync_job_instead_of_creating_a_second_job(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        sync_job = self.repository.create_job("sync", "2026-07-26T00:00:00Z")
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        reused = repair.start_backfill(
            "2026-07-26T00:00:01Z", job_kind="repair",
        )
        result = repair.run_batch(reused, "2026-07-26T00:00:01Z")

        self.assertEqual(reused, sync_job)
        self.assertFalse(result.completed)
        self.assertEqual(
            [(job.job_id, job.job_kind, job.state) for job in self.repository.list_jobs()],
            [(sync_job, "sync", "queued")],
        )

    def test_repair_full_materializes_attributed_source_then_append_can_tail(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text('project_id = "hprj_safe"\ntelemetry = "hybrid"\n')
        self.path.write_text(
            '{"type":"session_meta","payload":{"id":"session-1","session_id":"session-1",'
            f'"cwd":"{project}"}}}}\n'
        )
        roots = TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive")
        repair = ResumableRepair(self.store, roots)
        repaired = repair.repair_source("sessions", "rollout.jsonl", "2026-07-26T00:00:00Z")
        self.assertTrue(repaired)
        project_row = self.store.connection.execute(
            """SELECT display_name,display_name_provenance
                 FROM dashboard_projects WHERE project_id='hprj_safe'""",
        ).fetchone()
        self.assertEqual(tuple(project_row), ("project", "repo_basename"))
        checkpoint = self.repository.checkpoint_for("sessions", "rollout.jsonl")
        self.assertGreater(checkpoint.byte_offset, 0)
        self.assertEqual(self.repository.source_for("sessions", "rollout.jsonl").project_id, "hprj_safe")
        self.path.write_bytes(self.path.read_bytes() + b'{"type":"event_msg","payload":{"type":"task_complete","turn_id":"t"}}\n')
        self.repository.enqueue("sessions", "rollout.jsonl", "2026-07-26T00:01:00Z")
        worker = IncrementalSyncWorker(self.store, roots)
        self.assertEqual(worker.sync_once("worker", "2026-07-26T00:01:00Z", "2026-07-26T00:02:00Z").completed, 1)
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").line_number, 2)

    def test_repair_leaves_unterminated_record_for_one_reconstructed_append(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, MaterializedSource, ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "partial-repair-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text('project_id = "hprj_partial"\ntelemetry = "hybrid"\n')
        prefix = (
            '{"type":"session_meta","payload":{"id":"partial-session","session_id":"partial-session",'
            f'"cwd":"{project}"}}}}\n'
        ).encode()
        unfinished = b'{"type":"event_msg","payload":{"type":"task_complete"'
        self.path.write_bytes(prefix + unfinished)
        roots = TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive")
        repair = ResumableRepair(self.store, roots)
        self.assertTrue(repair.repair_source("sessions", "rollout.jsonl", "2026-07-26T00:00:00Z"))
        checkpoint = self.repository.checkpoint_for("sessions", "rollout.jsonl")
        self.assertEqual((checkpoint.byte_offset, checkpoint.line_number), (len(prefix), 1))

        suffix = b'}}\n'
        self.path.write_bytes(prefix + unfinished + suffix)
        self.repository.enqueue("sessions", "rollout.jsonl", "2026-07-26T00:01:00Z")
        seen: list[bytes] = []

        def materialize(_item, tail, _connection):
            seen.extend(line.value for line in tail.lines)
            return MaterializedSource(project_id="hprj_partial")

        worker = IncrementalSyncWorker(self.store, roots, materialize=materialize)
        self.assertEqual(worker.sync_once("worker", "2026-07-26T00:01:00Z", "2026-07-26T00:02:00Z").completed, 1)
        self.assertEqual(seen, [unfinished + suffix])

    def test_large_repair_checkpoint_is_at_eof_then_append_reads_only_new_bytes(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, MaterializedSource, ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "large-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text('project_id = "hprj_large"\ntelemetry = "hybrid"\n')
        meta = (
            '{"type":"session_meta","payload":{"id":"large-session","session_id":"large-session",'
            f'"cwd":"{project}"}}}}\n'
        ).encode()
        filler = b'{"type":"event_msg","payload":{}}\n'
        self.path.write_bytes(meta + filler * 120_000)
        roots = TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive")
        repair = ResumableRepair(self.store, roots)
        self.assertTrue(repair.repair_source("sessions", "rollout.jsonl", "2026-07-26T00:00:00Z"))
        checkpoint = self.repository.checkpoint_for("sessions", "rollout.jsonl")
        self.assertGreater(checkpoint.byte_offset, 1024 * 1024)
        self.assertEqual(checkpoint.byte_offset, self.path.stat().st_size)
        self.assertEqual(checkpoint.line_number, 120_001)

        appended = b'{"type":"event_msg","payload":{"type":"task_complete"}}\n'
        self.path.write_bytes(self.path.read_bytes() + appended)
        self.repository.enqueue("sessions", "rollout.jsonl", "2026-07-26T00:01:00Z")
        reads: list[bytes] = []

        def materialize(_item, tail, _connection):
            reads.extend(line.value for line in tail.lines)
            return MaterializedSource(project_id="hprj_large")

        worker = IncrementalSyncWorker(self.store, roots, materialize=materialize)
        self.assertEqual(worker.sync_once("worker", "2026-07-26T00:01:00Z", "2026-07-26T00:02:00Z").completed, 1)
        self.assertEqual(reads, [appended])

    def test_repair_holds_descriptor_when_parent_is_swapped_before_parse(self) -> None:
        """A repair scan must not re-open through a newly symlinked parent."""
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "held-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_held"\ntelemetry = "hybrid"\n'
        )
        nested = self.root / "safe"
        nested.mkdir()
        source = nested / "rollout.jsonl"
        source.write_text(
            '{"type":"session_meta","payload":{"id":"held-session","session_id":"held-session",'
            f'"cwd":"{project}"}}}}\n'
        )
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "rollout.jsonl").write_text('{"type":"session_meta","payload":{"id":"escape"}}\n')
        roots = TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive")
        repair = ResumableRepair(self.store, roots)

        original_scan = __import__("hydra_codex.incremental_sync", fromlist=["scan_source"]).scan_source
        swapped = False

        def swap_after_scan(*args, **kwargs):
            nonlocal swapped
            result = original_scan(*args, **kwargs)
            if not swapped:
                nested.rename(self.root / "safe-original")
                nested.symlink_to(outside, target_is_directory=True)
                swapped = True
            return result

        with mock.patch("hydra_codex.incremental_sync.scan_source", side_effect=swap_after_scan):
            with self.assertRaisesRegex(Exception, "rollout source changed"):
                repair.repair_source("sessions", "safe/rollout.jsonl", "2026-07-26T00:00:00Z")
        self.assertIsNone(self.repository.source_for("sessions", "safe/rollout.jsonl"))

    def test_backfill_reconciles_dirty_project_before_job_succeeds(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "backfill-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text('project_id = "hprj_backfill"\ntelemetry = "hybrid"\n')
        self.path.write_text(
            '{"type":"session_meta","payload":{"id":"backfill-session","session_id":"backfill-session",'
            f'"cwd":"{project}"}}}}\n'
        )
        repair = ResumableRepair(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        job_id = repair.start_backfill("2026-07-26T00:00:00Z")
        repair.run_batch(job_id, "2026-07-26T00:00:01Z", directory_limit=1)
        repair.run_batch(job_id, "2026-07-26T00:00:02Z", directory_limit=1)
        job = self.repository.get_job(job_id)
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(job.sources_completed, 1)
        self.assertEqual(self.repository.list_dirty_roots(), ())
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM reconciliation_runs WHERE project_id='hprj_backfill'").fetchone()[0], 1)

    def test_backfill_waits_for_reconciliation_lease_before_succeeding(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "leased-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text('project_id = "hprj_leased"\ntelemetry = "hybrid"\n')
        self.path.write_text(
            '{"type":"session_meta","payload":{"id":"leased-session","session_id":"leased-session",'
            f'"cwd":"{project}"}}}}\n'
        )
        repair = ResumableRepair(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        job_id = repair.start_backfill("2026-07-26T00:00:00Z")
        repair.run_batch(job_id, "2026-07-26T00:00:01Z", directory_limit=1)
        self.assertTrue(self.repository.acquire_lease("other", "2026-07-26T00:00:01Z", "2026-07-26T00:10:00Z"))
        blocked = repair.run_batch(job_id, "2026-07-26T00:00:02Z", directory_limit=1)
        self.assertFalse(blocked.completed)
        self.assertEqual(self.repository.get_job(job_id).state, "running")
        self.assertEqual(self.repository.get_job(job_id).sources_completed, 0)
        resumed = repair.run_batch(job_id, "2026-07-26T00:10:00Z", directory_limit=1)
        self.assertTrue(resumed.completed)
        self.assertEqual(self.repository.get_job(job_id).state, "succeeded")
        self.assertEqual(self.repository.list_dirty_roots(), ())

    def test_concurrent_backfill_invocations_expand_one_frontier_once(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        repair = ResumableRepair(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        job_id = repair.start_backfill("2026-07-26T00:00:00Z")
        entered = threading.Event()
        release = threading.Event()
        expansions: list[str] = []
        first_result: list[object] = []

        def run_first() -> None:
            first_store = HydraStore(self.database)
            try:
                first_repair = ResumableRepair(
                    first_store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
                )
                original = first_repair._directory

                def slow_directory(root_kind, locator):
                    expansions.append(locator)
                    entered.set()
                    release.wait(2)
                    return original(root_kind, locator)

                with mock.patch.object(first_repair, "_directory", side_effect=slow_directory):
                    first_result.append(first_repair.run_batch(job_id, "2026-07-26T00:00:01Z", directory_limit=1))
            finally:
                first_store.close()

        first = threading.Thread(target=run_first)
        first.start()
        self.assertTrue(entered.wait(2))
        second_store = HydraStore(self.database)
        self.addCleanup(second_store.close)
        second = ResumableRepair(second_store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        contender = second.run_batch(job_id, "2026-07-26T00:00:01Z", directory_limit=1)
        release.set()
        first.join(3)
        self.assertFalse(contender.completed)
        self.assertEqual(expansions, ["@root"])
        self.assertEqual(len(first_result), 1)

    def test_backfill_heartbeat_keeps_slow_full_materialize_exclusive_past_ttl(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "heartbeat-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text('project_id = "hprj_heartbeat"\ntelemetry = "hybrid"\n')
        self.path.write_text(
            '{"type":"session_meta","payload":{"id":"heartbeat","session_id":"heartbeat",'
            f'"cwd":"{project}"}}}}\n'
        )
        roots = TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive")
        repair = ResumableRepair(self.store, roots, lease_ttl_seconds=1)
        job_id = self.repository.create_job("backfill", "2026-07-26T00:00:00Z")
        self.repository.save_frontier(
            job_id=job_id, root_kind="sessions", directory_locator="@file/rollout.jsonl",
            state="pending", discovered_count=1, updated_at="2026-07-26T00:00:00Z",
        )
        self.repository.update_job(
            job_id, state="running", sources_discovered=1, sources_completed=0,
            bytes_processed=0, updated_at="2026-07-26T00:00:00Z",
        )
        started = threading.Event()
        result: list[object] = []

        def run_slow_batch() -> None:
            runner_store = HydraStore(self.database)
            try:
                runner_repair = ResumableRepair(runner_store, roots, lease_ttl_seconds=1)
                original = runner_repair._full_materialize

                def slow_materialize(*args):
                    started.set()
                    time.sleep(3)
                    return original(*args)

                with mock.patch.object(runner_repair, "_full_materialize", side_effect=slow_materialize):
                    result.append(runner_repair.run_batch(job_id, observed, directory_limit=1))
            finally:
                runner_store.close()

        observed_now = datetime.now(timezone.utc)
        observed = observed_now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{observed_now.microsecond:06d}".rstrip("0") + "Z"
        runner = threading.Thread(target=run_slow_batch)
        runner.start()
        self.assertTrue(started.wait(2))
        time.sleep(1.5)
        contender_store = HydraStore(self.database)
        self.addCleanup(contender_store.close)
        contender = SyncStateRepository(contender_store)
        contender_now = datetime.now(timezone.utc)
        contender_observed = contender_now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{contender_now.microsecond:06d}".rstrip("0") + "Z"
        lease_before_contender = tuple(contender_store.connection.execute(
            "SELECT owner_key,expires_at FROM sync_worker_leases WHERE lease_name='ingest'"
        ).fetchone() or ())
        self.assertFalse(
            contender.acquire_lease("contender", contender_observed, "2030-01-01T00:00:00Z"),
            (lease_before_contender, contender_observed),
        )
        runner.join(5)
        self.assertEqual(len(result), 1)

    def test_rewrite_repair_remains_quarantined_without_advancing_checkpoint(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "rewrite-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text('project_id = "hprj_rewrite"\ntelemetry = "hybrid"\n')
        self.path.write_text('{"type":"session_meta","payload":{"id":"s","cwd":"' + str(project) + '"}}\n')
        repair = ResumableRepair(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
        self.assertTrue(repair.repair_source("sessions", "rollout.jsonl", "2026-07-26T00:00:00Z"))
        before = self.repository.checkpoint_for("sessions", "rollout.jsonl").byte_offset
        self.path.write_text('{"type":"session_meta","payload":{"id":"s","cwd":"' + str(project) + '","changed":true}}\n')
        self.assertFalse(repair.repair_source("sessions", "rollout.jsonl", "2026-07-26T00:01:00Z"))
        self.assertEqual(self.repository.source_for("sessions", "rollout.jsonl").source_state, "repair_required")
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").byte_offset, before)


class IncrementalParityTests(unittest.TestCase):
    """Production acceptance parity between full ingest and append tailing."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_id = "hprj_parity"
        self.project = self.root / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            f'project_id = "{self.project_id}"\ntelemetry = "hybrid"\n',
            encoding="utf-8",
        )
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.source = self.sessions / "rollout.jsonl"
        self.key = Pseudonymizer.installation(self.root).key

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record(self, kind: str, payload: dict[str, object], second: int) -> str:
        return json.dumps({
            "timestamp": f"2026-07-26T00:00:{second:02d}Z",
            "type": kind,
            "payload": payload,
        }, sort_keys=True) + "\n"

    def _write_prefix(self) -> None:
        self.source.write_text("".join((
            self._record("session_meta", {
                "id": "parity-session", "session_id": "parity-session",
                "cwd": str(self.project),
            }, 0),
            self._record("turn_context", {"turn_id": "parity-turn"}, 1),
            self._record("event_msg", {
                "type": "task_started", "turn_id": "parity-turn", "duration_ms": 1,
            }, 2),
            self._record("event_msg", {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 20,
                    "output_tokens": 10, "reasoning_output_tokens": 3,
                    "total_tokens": 110,
                }, "model_context_window": 1000},
            }, 3),
        )), encoding="utf-8")

    def _append_completion(self) -> None:
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(self._record("event_msg", {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 150, "cached_input_tokens": 30,
                    "output_tokens": 20, "reasoning_output_tokens": 5,
                    "total_tokens": 170,
                }, "model_context_window": 1000},
            }, 4))
            handle.write(self._record("event_msg", {
                "type": "task_complete", "turn_id": "parity-turn", "duration_ms": 5000,
            }, 5))

    def _snapshot(self, store: HydraStore) -> tuple[object, object, object]:
        from hydra_codex.reconcile_engine import list_reconciled_reports

        reports = tuple(report.as_dict() for report in list_reconciled_reports(
            store, project_id=self.project_id,
        ))
        tokens = tuple(store.connection.execute(
            """SELECT line_number,source_family,input_tokens,cached_input_tokens,
                      output_tokens,reasoning_tokens,contributes_total,
                      selection_provenance,selection_caveat
                 FROM token_snapshots WHERE project_id=? ORDER BY line_number""",
            (self.project_id,),
        ))
        selected = tuple(store.connection.execute(
            """SELECT line_number,source_family,contributes_total,
                      selection_provenance,selection_caveat
                 FROM token_snapshots WHERE project_id=? ORDER BY line_number""",
            (self.project_id,),
        ))
        return reports, tokens, selected

    def test_append_sync_and_source_repair_match_canonical_full_ingest_without_duplicate_tokens(self) -> None:
        """A tail must preserve legacy task totals; repair must not double them."""
        from hydra_codex.incremental_sync import IncrementalSyncWorker, ResumableRepair, TrustedSourceRoots
        from hydra_codex.reconcile_engine import reconcile_project
        from hydra_codex.rollout import RolloutRoot, ingest_rollouts
        from hydra_codex.sync_state import SyncStateRepository

        # This fails if the tail uses a different legacy identity/selection
        # contract, or if an explicit repair leaves two token contributions.
        self._write_prefix()
        incremental = HydraStore(self.root / "incremental.sqlite3")
        roots = TrustedSourceRoots(
            sessions=self.sessions, archived_sessions=self.root / "archived",
        )
        try:
            repair = ResumableRepair(incremental, roots)
            self.assertTrue(repair.repair_source(
                "sessions", "rollout.jsonl", "2026-07-26T00:00:00Z",
            ))
            self._append_completion()
            SyncStateRepository(incremental).enqueue(
                "sessions", "rollout.jsonl", "2026-07-26T00:00:10Z",
            )
            worker = IncrementalSyncWorker(
                incremental, roots,
                reconcile=lambda project_id: reconcile_project(
                    incremental, project_id, self.key,
                ),
            )
            self.assertEqual(worker.sync_once(
                "parity-worker", "2026-07-26T00:00:10Z", "2026-07-26T00:01:10Z",
            ).completed, 1)
            incremental_before_repair = self._snapshot(incremental)

            legacy = HydraStore(self.root / "legacy.sqlite3")
            try:
                ingest_rollouts(
                    legacy, (RolloutRoot(self.source, "active"),), self.project,
                    self.project_id, hash_key=self.key,
                )
                reconcile_project(legacy, self.project_id, self.key)
                canonical = self._snapshot(legacy)
            finally:
                legacy.close()

            self.assertEqual(incremental_before_repair, canonical)
            self.assertEqual(len(canonical[0]), 1)
            self.assertEqual(canonical[0][0]["status"], "complete")
            self.assertEqual(tuple(incremental.connection.execute(
                    "SELECT COUNT(*),COUNT(DISTINCT source_digest || ':' || line_number) "
                    "FROM token_snapshots WHERE project_id=?", (self.project_id,),
                ).fetchone()),
                (2, 2),
            )

            self.assertTrue(repair.repair_source(
                "sessions", "rollout.jsonl", "2026-07-26T00:02:00Z",
            ))
            reconcile_project(incremental, self.project_id, self.key)
            self.assertEqual(self._snapshot(incremental), canonical)
            self.assertEqual(tuple(incremental.connection.execute(
                    "SELECT COUNT(*),COUNT(DISTINCT source_digest || ':' || line_number) "
                    "FROM token_snapshots WHERE project_id=?", (self.project_id,),
                ).fetchone()),
                (2, 2),
            )
        finally:
            incremental.close()

    def test_repair_prefix_and_reconstructed_partial_match_canonical_report_without_diagnostics(self) -> None:
        """The legacy repair parser must never diagnose an in-progress JSONL line."""
        from hydra_codex.incremental_sync import IncrementalSyncWorker, ResumableRepair, TrustedSourceRoots
        from hydra_codex.reconcile_engine import reconcile_project
        from hydra_codex.rollout import RolloutRoot, ingest_rollouts
        from hydra_codex.sync_state import SyncStateRepository

        self._write_prefix()
        completion = self._record("event_msg", {
            "type": "task_complete", "turn_id": "parity-turn", "duration_ms": 5000,
        }, 5)
        unfinished = completion[:-2]
        self.source.write_text(self.source.read_text(encoding="utf-8") + unfinished, encoding="utf-8")
        roots = TrustedSourceRoots(sessions=self.sessions, archived_sessions=self.root / "archived")
        incremental = HydraStore(self.root / "partial-incremental.sqlite3")
        try:
            repair = ResumableRepair(incremental, roots)
            self.assertTrue(repair.repair_source("sessions", "rollout.jsonl", "2026-07-26T00:00:00Z"))
            self.source.write_text(self.source.read_text(encoding="utf-8") + "}\n", encoding="utf-8")
            SyncStateRepository(incremental).enqueue("sessions", "rollout.jsonl", "2026-07-26T00:00:10Z")
            worker = IncrementalSyncWorker(
                incremental, roots,
                reconcile=lambda project_id: reconcile_project(incremental, project_id, self.key),
            )
            self.assertEqual(worker.sync_once(
                "partial-worker", "2026-07-26T00:00:10Z", "2026-07-26T00:01:10Z",
            ).completed, 1)

            legacy = HydraStore(self.root / "partial-legacy.sqlite3")
            try:
                ingest_rollouts(legacy, (RolloutRoot(self.source, "active"),), self.project, self.project_id, hash_key=self.key)
                reconcile_project(legacy, self.project_id, self.key)
                self.assertEqual(self._snapshot(incremental), self._snapshot(legacy))
                self.assertEqual(
                    tuple(incremental.connection.execute(
                        "SELECT envelope_kind FROM rollout_diagnostics ORDER BY source_digest,line_number,envelope_kind"
                    )),
                    tuple(legacy.connection.execute(
                        "SELECT envelope_kind FROM rollout_diagnostics ORDER BY source_digest,line_number,envelope_kind"
                    )),
                )
            finally:
                legacy.close()
        finally:
            incremental.close()
