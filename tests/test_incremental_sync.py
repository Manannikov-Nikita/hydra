from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from hydra_codex.storage import HydraStore
from hydra_codex.sync_state import SyncStateRepository


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

    def test_normal_sync_never_discovers_or_reads_unqueued_registered_sources(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        for index in range(1500):
            path = self.root / "known" / f"{index}.jsonl"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'{"unread":true}\n')
            self.repository.register_source(root_kind="sessions", source_locator=f"known/{index}.jsonl", observed_at="2026-07-26T00:00:00Z")
        worker = IncrementalSyncWorker(self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"))
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
        self.assertEqual(
            [source.source_locator for source in self.repository.list_sources()],
            ["nested/one.jsonl", "rollout.jsonl"],
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
        checkpoint = self.repository.checkpoint_for("sessions", "rollout.jsonl")
        self.assertGreater(checkpoint.byte_offset, 0)
        self.assertEqual(self.repository.source_for("sessions", "rollout.jsonl").project_id, "hprj_safe")
        self.path.write_bytes(self.path.read_bytes() + b'{"type":"event_msg","payload":{"type":"task_complete","turn_id":"t"}}\n')
        self.repository.enqueue("sessions", "rollout.jsonl", "2026-07-26T00:01:00Z")
        worker = IncrementalSyncWorker(self.store, roots)
        self.assertEqual(worker.sync_once("worker", "2026-07-26T00:01:00Z", "2026-07-26T00:02:00Z").completed, 1)
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").line_number, 2)
