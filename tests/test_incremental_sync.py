from __future__ import annotations

import hashlib
import json
import os
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
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

    def test_source_open_rejects_non_symlink_root_replacement(self) -> None:
        from hydra_codex.incremental_sync import RepairRequired, read_incremental_source

        replacement = Path(self.temporary.name) / "replacement"
        redirected = replacement / "2026" / "07" / "rollout.jsonl"
        redirected.parent.mkdir(parents=True)
        redirected.write_bytes(b'{"redirected":true}\n')
        parked = Path(self.temporary.name) / "parked"
        original_open = os.open
        swapped = False

        def swap_before_root_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path) == self.root and kwargs.get("dir_fd") is None:
                self.root.rename(parked)
                replacement.rename(self.root)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch("hydra_codex.incremental_sync.os.open", side_effect=swap_before_root_open),
            self.assertRaisesRegex(RepairRequired, "trusted source root"),
        ):
            read_incremental_source(
                self._reader(), "sessions", "2026/07/rollout.jsonl",
            )

    def test_directory_open_rejects_non_symlink_root_replacement(self) -> None:
        from hydra_codex.incremental_sync import RepairRequired

        replacement = Path(self.temporary.name) / "replacement"
        replacement.mkdir()
        parked = Path(self.temporary.name) / "parked"
        original_open = os.open
        swapped = False

        def swap_before_root_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path) == self.root and kwargs.get("dir_fd") is None:
                self.root.rename(parked)
                replacement.rename(self.root)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch("hydra_codex.incremental_sync.os.open", side_effect=swap_before_root_open),
            self.assertRaisesRegex(RepairRequired, "trusted source root"),
        ):
            with self._reader().open_directory("sessions", "@root"):
                self.fail("replaced root must not be yielded")


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

    def test_controller_can_retain_worker_lease_until_job_progress_is_durable(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl",
            project_id="hprj_safe", observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root, archived_sessions=self.root / "archive",
            ),
        )

        result = worker.sync_once(
            "dashboard-worker", "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z", release_lease=False,
        )

        self.assertTrue(result.lease_acquired)
        self.assertTrue(self.repository.lease_owned(
            "dashboard-worker", "2026-07-26T00:00:01Z",
        ))
        self.assertTrue(self.repository.release_lease(
            "dashboard-worker", "2026-07-26T00:00:01Z",
        ))

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
            materialize=materialize,
            reconcile=lambda project_id, _roots: reconciled.append(project_id),
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

    def test_unattributed_explicit_repair_is_partial_without_normal_queue_loop(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        self.assertFalse(repair.repair_source(
            "sessions", "rollout.jsonl", "2026-07-26T00:00:00Z",
        ))
        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual(
            self.repository.source_for(
                "sessions", "rollout.jsonl",
            ).source_state,
            "repair_required",
        )

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
        self.assertEqual(report.completed, 1)
        self.assertEqual(report.bytes_processed, len(line) * 35_000)
        self.assertEqual(seen, list(range(1, 35_001)))
        self.assertEqual(self.repository.checkpoint_for("sessions", "rollout.jsonl").byte_offset, len(line) * 35_000)
        self.assertEqual(self.repository.list_queue(), ())

    def test_concurrent_append_during_bounded_read_retries_without_permanent_repair(self) -> None:
        from hydra_codex import incremental_sync
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            MaterializedSource,
            TrustedSourceRoots,
        )

        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="rollout.jsonl",
            project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        original_checkpoint = incremental_sync._checkpoint_for
        appended = False

        def checkpoint_then_append(handle, details, offset, line_number):
            nonlocal appended
            checkpoint = original_checkpoint(
                handle, details, offset, line_number,
            )
            if not appended:
                appended = True
                with self.path.open("ab") as writer:
                    writer.write(b'{"type":"event_msg"}\n')
            return checkpoint

        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            materialize=lambda *_args: MaterializedSource(
                project_id="hprj_safe",
            ),
        )
        with mock.patch.object(
            incremental_sync, "_checkpoint_for",
            side_effect=checkpoint_then_append,
        ):
            report = worker.sync_once(
                "worker", "2026-07-26T00:00:00Z",
                "2026-07-26T00:01:00Z",
            )

        self.assertEqual(report.repair_required, 0)
        self.assertEqual(report.completed, 1)
        self.assertEqual(
            self.repository.source_for(
                "sessions", "rollout.jsonl",
            ).source_state,
            "ready",
        )
        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual(
            self.repository.checkpoint_for(
                "sessions", "rollout.jsonl",
            ).line_number,
            2,
        )

    def test_normal_sync_uses_fresh_clock_for_commit_outbox_reconcile_and_release(
        self,
    ) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            MaterializedSource,
            TrustedSourceRoots,
        )

        started = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        current = [started]
        self.repository.record_hook_event_and_enqueue(
            event_key="fresh-clock-event", project_id="hprj_safe",
            session_key="session-safe", turn_key="turn-safe",
            event_kind="prompt", observed_at="2026-07-27T10:00:00Z",
            source=("sessions", "rollout.jsonl"),
        )

        def materialize(*_args):
            current[0] = started + timedelta(seconds=30)
            return MaterializedSource(project_id="hprj_safe")

        def reconcile(_project_id, _roots):
            current[0] = started + timedelta(seconds=45)

        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            materialize=materialize,
            reconcile=reconcile,
            clock=lambda: current[0],
        )

        report = worker.sync_once(
            "worker", "2026-07-27T10:00:00Z",
            "2026-07-27T10:01:00Z",
        )

        self.assertEqual(report.completed, 1)
        self.assertEqual(
            self.store.connection.execute(
                """SELECT updated_at FROM sync_source_checkpoints
                     WHERE root_kind='sessions'
                       AND source_locator='rollout.jsonl'""",
            ).fetchone()[0],
            "2026-07-27T10:00:30Z",
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT acknowledged_at FROM hook_event_outbox
                     WHERE event_key='fresh-clock-event'""",
            ).fetchone()[0],
            "2026-07-27T10:00:30Z",
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT updated_at FROM sync_data_revision WHERE singleton=1",
            ).fetchone()[0],
            "2026-07-27T10:00:45Z",
        )

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

    def test_materializer_validation_quarantines_source_and_releases_claim_and_lease(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )

        def reject(*_args):
            raise ValueError("trusted source binding is inconsistent")

        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            materialize=reject,
        )

        report = worker.sync_once(
            "worker",
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z",
        )

        self.assertEqual(
            (report.claimed, report.completed, report.repair_required),
            (1, 0, 1),
        )
        self.assertEqual(
            self.repository.checkpoint_for(
                "sessions",
                "rollout.jsonl",
            ).byte_offset,
            0,
        )
        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).source_state,
            "repair_required",
        )
        self.assertFalse(
            self.repository.lease_owned(
                "worker",
                "2026-07-26T00:00:01Z",
            ),
        )

    def test_materializer_rejection_discards_an_enqueue_racing_the_claim(self) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            TrustedSourceRoots,
        )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        observed = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(minutes=1)).isoformat().replace(
            "+00:00", "Z",
        )
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            observed_at=observed,
        )

        def reject(*_args):
            raise ValueError("trusted source binding is inconsistent")

        class EnqueueAfterRollback:
            def __enter__(inner_self):
                return threading.Event()

            def __exit__(inner_self, *_error):
                self.repository.register_and_enqueue(
                    root_kind="sessions",
                    source_locator="rollout.jsonl",
                    project_id="hprj_safe",
                    observed_at=observed,
                )
                return False

        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            materialize=reject,
            clock=lambda: now,
        )
        worker._lease_heartbeat = lambda *_args, **_kwargs: EnqueueAfterRollback()

        report = worker.sync_once(
            "worker",
            observed,
            expires,
            maximum_sources=2,
        )

        self.assertEqual(
            (report.claimed, report.completed, report.repair_required),
            (1, 0, 1),
        )
        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).source_state,
            "repair_required",
        )

    def test_reader_repair_discards_an_enqueue_racing_the_claim(self) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            RepairRequired,
            TrustedSourceRoots,
        )

        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )

        def reject(*_args, **_kwargs):
            self.repository.register_and_enqueue(
                root_kind="sessions",
                source_locator="rollout.jsonl",
                project_id="hprj_safe",
                observed_at="2026-07-26T00:00:01Z",
            )
            raise RepairRequired("source identity changed")

        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )
        with mock.patch(
            "hydra_codex.incremental_sync.read_incremental_source",
            side_effect=reject,
        ):
            report = worker.sync_once(
                "worker",
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:01:00Z",
                maximum_sources=2,
            )

        self.assertEqual(
            (report.claimed, report.completed, report.repair_required),
            (1, 0, 1),
        )
        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).source_state,
            "repair_required",
        )

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

    def test_heartbeat_keeps_dirty_claim_live_during_slow_reconcile(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        now = datetime.now(timezone.utc)
        observed = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        self.repository.mark_dirty(
            "hprj_safe", "hprj_safe", "project", observed,
        )
        reconciled: list[str] = []

        def slow_reconcile(project_id: str, _roots) -> None:
            time.sleep(1.4)
            reconciled.append(project_id)

        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            reconcile=slow_reconcile,
        )

        report = worker.sync_once("worker", observed, expires)

        self.assertTrue(report.lease_acquired)
        self.assertEqual(reconciled, ["hprj_safe"])
        self.assertEqual(self.repository.list_dirty_roots(), ())

    def test_successful_reconcile_acknowledges_its_expired_dirty_claim(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        started = datetime.now(timezone.utc)
        observed = started.isoformat().replace("+00:00", "Z")
        expires = (started + timedelta(seconds=0.2)).isoformat().replace(
            "+00:00", "Z",
        )
        self.repository.mark_dirty(
            "hprj_safe", "hprj_safe", "project", observed,
        )
        self.assertTrue(
            self.repository.acquire_lease("worker", observed, expires),
        )

        def reconcile(_project_id: str, _roots) -> None:
            with self.store.rollout_transaction() as connection:
                connection.execute(
                    "CREATE TABLE expired_dirty_claim_regression(value INTEGER)",
                )
                connection.execute(
                    "INSERT INTO expired_dirty_claim_regression(value) VALUES (1)",
                )
                time.sleep(0.45)

        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            reconcile=reconcile,
        )

        completed = worker.reconcile_dirty(
            "worker",
            observed,
            expires,
            current_time=lambda: datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z",
            ),
        )

        self.assertEqual(completed, 1)
        self.assertEqual(self.repository.list_dirty_roots(), ())

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

    def test_heartbeat_reopen_failure_is_fail_closed(self) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            TrustedSourceRoots,
        )
        from hydra_codex.storage import StorageUnavailable

        now = datetime.now(timezone.utc)
        observed = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        with mock.patch.object(
            self.store,
            "validated_reopener",
            return_value=lambda: (_ for _ in ()).throw(
                StorageUnavailable("heartbeat reopen failed"),
            ),
        ):
            with worker._lease_heartbeat(
                "worker", expires, interval_seconds=0.01,
            ) as lost:
                self.assertTrue(lost.wait(0.5))

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

    def test_default_materializer_bootstraps_a_trusted_hook_session_before_an_appended_token(self) -> None:
        """A hook-bound append must not require session_meta inside the new byte range."""
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            session_key="session-safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        report = worker.sync_once(
            "worker",
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z",
        )

        self.assertEqual(
            (report.claimed, report.completed, report.repair_required),
            (1, 1, 0),
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT session_key,project_id,path_key,resume_segments,
                          conversation_key
                     FROM rollout_sessions"""
            ).fetchone()),
            ("session-safe", "hprj_safe", "incremental", 1, "session-safe"),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM rollout_session_segments"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM incremental_session_placeholders",
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT session_key,project_id,input_tokens,output_tokens
                     FROM token_snapshots"""
            ).fetchone()),
            ("session-safe", "hprj_safe", 10, 3),
        )

    def test_canonical_lineage_repairs_a_stale_hook_session_binding(self) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            TrustedSourceRoots,
        )

        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.store.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES ('canonical-session','hprj_safe','safe',1,
                       'canonical-conversation')"""
        )
        self.store.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES ('canonical-logical','hprj_safe','canonical-session',
                       ?,'clean')""",
            ("a" * 64,),
        )
        self.store.connection.commit()
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            logical_source_key="canonical-logical",
            session_key="stale-hook-session",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        report = worker.sync_once(
            "worker",
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z",
        )

        self.assertEqual(
            (report.completed, report.repair_required),
            (1, 0),
        )
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).session_key,
            "canonical-session",
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT session_key FROM token_snapshots",
            ).fetchone()[0],
            "canonical-session",
        )

    def test_provisional_lineage_cannot_override_a_conflicting_registry_session(self) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            TrustedSourceRoots,
        )

        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.store.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES ('provisional-session','hprj_safe','safe',1,
                       'provisional-conversation')"""
        )
        self.store.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES ('provisional-logical','hprj_safe',
                       'provisional-session',NULL,'clean')"""
        )
        self.store.connection.commit()
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            logical_source_key="provisional-logical",
            session_key="registry-session",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        report = worker.sync_once(
            "worker",
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z",
        )

        self.assertEqual(
            (report.completed, report.repair_required),
            (0, 1),
        )
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).source_state,
            "repair_required",
        )
        self.assertEqual(self.store.count("token_snapshots"), 0)

    def test_incremental_token_selection_is_scoped_to_the_materialized_session(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots
        from hydra_codex.token_selection import (
            refresh_token_session_selection as real_refresh,
        )

        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            session_key="session-safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        with mock.patch(
            "hydra_codex.incremental_sync.refresh_token_session_selection",
            wraps=real_refresh,
        ) as refresh:
            report = worker.sync_once(
                "worker",
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:01:00Z",
            )

        self.assertEqual(report.completed, 1)
        refresh.assert_called_once_with(
            self.store.connection,
            "hprj_safe",
            "session-safe",
        )

    def test_incremental_preprocessing_does_not_read_or_mutate_another_project(self) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            MaterializedSource,
            TrustedSourceRoots,
        )
        from hydra_codex.rollout_reconcile import (
            reconcile_turn_attempts as real_reconcile_turn_attempts,
        )
        from hydra_codex.test_evidence import (
            materialize_test_evidence as real_materialize_test_evidence,
            reconcile_test_retries as real_reconcile_test_retries,
        )

        connection = self.store.connection
        connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES ('session-foreign','hprj_foreign','safe',1,
                       'conversation-foreign')"""
        )
        connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES ('logical-foreign','hprj_foreign','session-foreign',
                       NULL,'clean')"""
        )
        connection.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key,relation,
                   line_count,byte_count,chain_digest,materialized)
               VALUES ('source-foreign','jsonl','logical-foreign','canonical',
                       1,1,'chain-foreign',1)"""
        )
        connection.execute(
            """UPDATE rollout_logical_sources
                  SET canonical_revision_digest='source-foreign'
                WHERE logical_source_key='logical-foreign'"""
        )
        connection.execute(
            """INSERT INTO rollout_events(
                   event_key,logical_source_key,source_ordinal,envelope_kind,
                   observed_at,timestamp_quality,fingerprint)
               VALUES ('event-foreign','logical-foreign',1,'event_msg',
                       '2026-07-26T00:00:00Z','valid','fingerprint-foreign')"""
        )
        connection.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,
                   timestamp_epoch,emitted_duration_ms,source_digest,
                   logical_source_key,source_ordinal)
               VALUES ('event-foreign','session-foreign','turn-foreign',
                       'completed','2026-07-26T00:00:00Z',0,NULL,
                       'source-foreign','logical-foreign',1)"""
        )
        connection.execute(
            """INSERT INTO turn_attempts(
                   session_key,turn_key,attempt_ordinal,state,
                   emitted_duration_ms,wall_duration_ms,started_at,finished_at,
                   timing_provenance)
               VALUES ('session-foreign','turn-foreign',1,'open',
                       NULL,NULL,NULL,NULL,'estimated')"""
        )
        connection.execute(
            """INSERT INTO rollout_test_runs(
                   evidence_key,source_digest,line_number,session_key,
                   observed_at,turn_key,tool_call_key,command_hash,runner,scope,
                   exit_status,outcome,failure_cause,retry_kind,
                   attempt_ordinal,provenance,completeness)
               VALUES ('evidence-foreign','source-foreign',1,'session-foreign',
                       NULL,'turn-foreign','call-foreign','command-foreign',
                       'pytest','targeted',0,'success','none','none',9,
                       'derived','complete')"""
        )
        connection.commit()
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            materialize=lambda _item, _tail, _connection: MaterializedSource(
                project_id="hprj_safe",
            ),
        )

        with (
            mock.patch(
                "hydra_codex.incremental_sync.materialize_test_evidence",
                wraps=real_materialize_test_evidence,
            ) as materialize,
            mock.patch(
                "hydra_codex.incremental_sync.reconcile_test_retries",
                wraps=real_reconcile_test_retries,
            ) as retries,
            mock.patch(
                "hydra_codex.incremental_sync.reconcile_turn_attempts",
                wraps=real_reconcile_turn_attempts,
            ) as attempts,
        ):
            report = worker.sync_once(
                "worker",
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:01:00Z",
            )

        self.assertEqual(report.completed, 1)
        materialize.assert_called_once_with(connection, "hprj_safe")
        retries.assert_called_once_with(connection, "hprj_safe")
        attempts.assert_called_once_with(
            connection,
            mock.ANY,
            project_id="hprj_safe",
        )
        self.assertEqual(
            tuple(connection.execute(
                """SELECT attempt_ordinal,state
                     FROM turn_attempts
                    WHERE session_key='session-foreign'
                      AND turn_key='turn-foreign'"""
            ).fetchone()),
            (1, "open"),
        )
        self.assertEqual(
            tuple(connection.execute(
                """SELECT attempt_ordinal,retry_kind
                     FROM rollout_test_runs
                    WHERE evidence_key='evidence-foreign'"""
            ).fetchone()),
            (9, "none"),
        )

    def test_default_materializer_rejects_a_trusted_session_owned_by_another_project(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.store.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES ('session-shared','hprj_foreign','safe',1,
                       'conversation-foreign')"""
        )
        self.store.connection.commit()
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            session_key="session-shared",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        report = worker.sync_once(
            "worker",
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z",
        )

        self.assertEqual(
            (report.completed, report.repair_required),
            (0, 1),
        )
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).source_state,
            "repair_required",
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT project_id FROM rollout_sessions
                     WHERE session_key='session-shared'"""
            ).fetchone()[0],
            "hprj_foreign",
        )
        self.assertEqual(self.store.count("token_snapshots"), 0)
        self.assertEqual(self.store.count("rollout_session_segments"), 0)

    def test_default_materializer_rejects_a_logical_source_owned_by_another_project(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.store.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES ('logical-shared','hprj_foreign',NULL,
                       NULL,'clean')"""
        )
        self.store.connection.commit()
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            logical_source_key="logical-shared",
            session_key="local-session",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        report = worker.sync_once(
            "worker",
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z",
        )

        self.assertEqual(
            (report.completed, report.repair_required),
            (0, 1),
        )
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).source_state,
            "repair_required",
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT project_id,session_key FROM rollout_logical_sources
                     WHERE logical_source_key='logical-shared'"""
            ).fetchone()),
            ("hprj_foreign", None),
        )
        self.assertEqual(self.store.count("token_snapshots"), 0)
        self.assertEqual(self.store.count("rollout_session_segments"), 0)

    def test_default_materializer_rejects_session_metadata_that_disagrees_with_the_hook(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        hasher = Pseudonymizer.installation(self.database.parent)
        trusted_session = hasher.digest("identity", "trusted-thread")
        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"session_meta",'
            b'"payload":{"id":"different-thread","cwd":"safe"}}\n'
        )
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            session_key=trusted_session,
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        report = worker.sync_once(
            "worker",
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:01:00Z",
        )

        self.assertEqual(
            (report.completed, report.repair_required),
            (0, 1),
        )
        self.assertEqual(
            self.repository.source_for(
                "sessions",
                "rollout.jsonl",
            ).source_state,
            "repair_required",
        )
        self.assertEqual(self.store.count("rollout_sessions"), 0)
        self.assertEqual(self.store.count("rollout_logical_sources"), 0)
        self.assertEqual(self.store.count("rollout_session_segments"), 0)

    def test_matching_session_metadata_completes_incremental_placeholder_timing(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        hasher = Pseudonymizer.installation(self.database.parent)
        session_key = hasher.digest("identity", "trusted-thread")
        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"session_meta",'
            b'"payload":{"id":"trusted-thread",'
            b'"session_id":"trusted-conversation","cwd":"safe"}}\n'
        )
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            session_key=session_key,
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        self.assertEqual(
            worker.sync_once(
                "worker",
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:01:00Z",
            ).completed,
            1,
        )

        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT started_at,last_activity_at,path_key,
                          conversation_key,resume_segments
                     FROM rollout_sessions WHERE session_key=?""",
                (session_key,),
            ).fetchone()),
            (
                "2026-07-26T00:00:01Z",
                "2026-07-26T00:00:01Z",
                "incremental",
                hasher.digest("conversation", "trusted-conversation"),
                1,
            ),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM incremental_session_placeholders",
            ).fetchone()[0],
            1,
        )

    def test_default_materializer_binds_an_unbound_same_project_logical_source(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.store.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES ('logical-unbound','hprj_safe',NULL,NULL,'clean')"""
        )
        self.store.connection.commit()
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            logical_source_key="logical-unbound",
            session_key="local-session",
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        self.assertEqual(
            worker.sync_once(
                "worker",
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:01:00Z",
            ).completed,
            1,
        )

        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT project_id,session_key
                     FROM rollout_logical_sources
                    WHERE logical_source_key='logical-unbound'"""
            ).fetchone()),
            ("hprj_safe", "local-session"),
        )
        self.assertEqual(self.store.count("token_snapshots"), 1)
        self.assertEqual(self.store.count("rollout_session_segments"), 1)

    def test_full_ingest_canonicalizes_incremental_session_metadata_and_segment_count(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots
        from hydra_codex.rollout import ingest_rollouts

        hasher = Pseudonymizer.installation(self.database.parent)
        session_key = hasher.digest("identity", "thread-real")
        self.path.write_bytes(
            b'{"timestamp":"2026-07-26T00:00:01Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":10,"cached_input_tokens":1,"output_tokens":3,'
            b'"reasoning_output_tokens":2},"model_context_window":100}}}\n'
        )
        self.repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="rollout.jsonl",
            project_id="hprj_safe",
            session_key=session_key,
            observed_at="2026-07-26T00:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )
        self.assertEqual(
            worker.sync_once(
                "worker",
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:01:00Z",
            ).completed,
            1,
        )
        with self.path.open("ab") as source:
            source.write(
                (
                    '{"timestamp":"2026-07-26T00:00:02Z",'
                    '"type":"session_meta","payload":{'
                    '"id":"thread-real","session_id":"conversation-real",'
                    f'"cwd":{json.dumps(str(self.root / "worktree"))}'
                    "}}\n"
                ).encode("utf-8")
            )
        (self.root / ".hydra").mkdir()
        (self.root / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_safe"\n',
            encoding="utf-8",
        )

        ingest_rollouts(
            self.store,
            (self.path,),
            self.root,
            "hprj_safe",
            hash_key=hasher.key,
        )

        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT project_id,path_key,resume_segments,conversation_key
                     FROM rollout_sessions WHERE session_key=?""",
                (session_key,),
            ).fetchone()),
            (
                "hprj_safe",
                "worktree",
                2,
                hasher.digest("conversation", "conversation-real"),
            ),
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM rollout_session_segments
                     WHERE session_key=?""",
                (session_key,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM incremental_session_placeholders",
            ).fetchone()[0],
            0,
        )

    def test_full_ingest_never_treats_a_real_incremental_path_as_placeholder(self) -> None:
        from hydra_codex.rollout import ingest_rollouts

        hasher = Pseudonymizer.installation(self.database.parent)
        (self.root / ".hydra").mkdir()
        (self.root / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_safe"\n',
            encoding="utf-8",
        )
        first_cwd = self.root / "incremental"
        second_cwd = self.root / "other"
        first_cwd.mkdir()
        second_cwd.mkdir()
        self.path.write_text(
            json.dumps({
                "timestamp": "2026-07-26T00:00:01Z",
                "type": "session_meta",
                "payload": {
                    "id": "real-thread",
                    "session_id": "first-conversation",
                    "cwd": str(first_cwd),
                },
            }) + "\n",
            encoding="utf-8",
        )
        second = self.root / "second.jsonl"
        second.write_text(
            json.dumps({
                "timestamp": "2026-07-26T00:00:02Z",
                "type": "session_meta",
                "payload": {
                    "id": "real-thread",
                    "session_id": "second-conversation",
                    "cwd": str(second_cwd),
                },
            }) + "\n",
            encoding="utf-8",
        )

        ingest_rollouts(
            self.store,
            (self.path, second),
            self.root,
            "hprj_safe",
            hash_key=hasher.key,
        )

        session_key = hasher.digest("identity", "real-thread")
        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT path_key,conversation_key,resume_segments
                     FROM rollout_sessions WHERE session_key=?""",
                (session_key,),
            ).fetchone()),
            (
                "incremental",
                hasher.digest("conversation", "first-conversation"),
                2,
            ),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM incremental_session_placeholders",
            ).fetchone()[0],
            0,
        )

    def test_reconcile_claims_only_dirty_projects_and_acks_after_success(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker, TrustedSourceRoots

        self.repository.mark_dirty("hprj_one", "task-1", "task", "2026-07-26T00:00:00Z")
        self.repository.mark_dirty("hprj_one", "task-2", "task", "2026-07-26T00:00:00Z")
        self.repository.mark_dirty("hprj_two", "hprj_two", "project", "2026-07-26T00:00:00Z")
        claimed: list[tuple[object, ...]] = []
        reconciled: list[tuple[str, tuple[object, ...]]] = []
        worker = IncrementalSyncWorker(
            self.store, TrustedSourceRoots(sessions=self.root, archived_sessions=self.root / "archive"),
            reconcile=lambda project_id, roots: reconciled.append((project_id, roots)),
        )
        claim_dirty_roots = worker.repository.claim_dirty_roots

        def capture_claim(*args, **kwargs):
            roots = claim_dirty_roots(*args, **kwargs)
            claimed.append(roots)
            return roots

        worker.repository.claim_dirty_roots = capture_claim
        worker.sync_once("worker", "2026-07-26T00:00:00Z", "2026-07-26T00:01:00Z")
        self.assertEqual(len(claimed), 1)
        self.assertEqual(
            reconciled,
            [
                (
                    project_id,
                    tuple(root for root in claimed[0] if root.project_id == project_id),
                )
                for project_id in ("hprj_one", "hprj_two")
            ],
        )
        self.assertEqual(
            [
                (project_id, tuple(root.root_key for root in roots))
                for project_id, roots in reconciled
            ],
            [
                ("hprj_one", ("task-1", "task-2")),
                ("hprj_two", ("hprj_two",)),
            ],
        )
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

    def test_resumable_repair_does_not_report_a_lease_for_an_unrelated_job(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        job_id = self.repository.create_job(
            "sync",
            "2026-07-26T00:00:00Z",
        )
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        result = repair.run_batch(
            job_id,
            "2026-07-26T00:00:01Z",
            directory_limit=1,
        )

        self.assertFalse(result.completed)
        self.assertFalse(result.lease_acquired)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM sync_worker_leases"
            ).fetchone()[0],
            0,
        )

    def test_backfill_recovers_job_progress_from_a_durable_file_frontier(self) -> None:
        """A crash after saving @file must not leave the resumed job at 0/0."""
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        project = Path(self.temporary.name) / "progress-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_progress"\ntelemetry = "hybrid"\n',
        )
        self.path.write_text(
            '{"type":"session_meta","payload":{"id":"progress","session_id":"progress",'
            f'"cwd":"{project}"}}}}\n',
        )
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root, archived_sessions=self.root / "archive",
            ),
        )
        job_id = self.repository.create_job(
            "backfill", "2026-07-26T00:00:00Z",
        )
        self.repository.save_frontier(
            job_id=job_id, root_kind="sessions",
            directory_locator="@file/rollout.jsonl", state="pending",
            discovered_count=1, updated_at="2026-07-26T00:00:00Z",
        )
        for root_kind in ("sessions", "archived_sessions"):
            self.repository.save_frontier(
                job_id=job_id,
                root_kind=root_kind,
                directory_locator="@root",
                state="scanned",
                discovered_count=0,
                updated_at="2026-07-26T00:00:00Z",
            )
        self.repository.update_job(
            job_id, state="running", sources_discovered=1,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-26T00:00:00Z",
        )
        # Reproduce the exact pre-fix crash image: the frontier is durable while
        # the separately maintained job counter still says no source exists.
        self.store.connection.execute(
            """UPDATE sync_jobs SET sources_discovered=0,sources_completed=0,
                   bytes_processed=0 WHERE job_id=?""",
            (job_id,),
        )

        result = repair.run_batch(
            job_id, "2026-07-26T00:00:01Z", directory_limit=1,
        )

        job = self.repository.get_job(job_id)
        self.assertTrue(result.completed)
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(
            (job.sources_discovered, job.sources_completed, job.bytes_processed),
            (1, 1, self.path.stat().st_size),
        )

    def test_backfill_stops_before_frontier_commit_after_ttl_handoff(self) -> None:
        """A previous lease owner cannot mutate this or later frontiers."""
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        roots = TrustedSourceRoots(
            sessions=self.root, archived_sessions=self.root / "archive",
        )
        repair = ResumableRepair(self.store, roots, lease_ttl_seconds=1)
        job_id = self.repository.create_job(
            "backfill", "2026-07-26T00:00:00Z",
        )
        for locator in ("@file/one.jsonl", "@file/two.jsonl"):
            self.repository.save_frontier(
                job_id=job_id, root_kind="sessions",
                directory_locator=locator, state="pending",
                discovered_count=1, updated_at="2026-07-26T00:00:00Z",
            )
        for root_kind in ("sessions", "archived_sessions"):
            self.repository.save_frontier(
                job_id=job_id,
                root_kind=root_kind,
                directory_locator="@root",
                state="scanned",
                discovered_count=0,
                updated_at="2026-07-26T00:00:00Z",
            )
        self.repository.update_job(
            job_id, state="running", sources_discovered=2,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-26T00:00:00Z",
        )
        contender_store = HydraStore(self.database)
        self.addCleanup(contender_store.close)
        contender = SyncStateRepository(contender_store)
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        lease_clock = {"now": started}
        observed = started.isoformat().replace("+00:00", "Z")
        handoff = (started + timedelta(seconds=1)).isoformat().replace(
            "+00:00", "Z",
        )
        calls: list[str] = []

        def lease_window():
            current = lease_clock["now"]
            return (
                current.isoformat().replace("+00:00", "Z"),
                (current + timedelta(seconds=1)).isoformat().replace(
                    "+00:00", "Z",
                ),
            )

        def hand_off_after_materialize(_root_kind, locator, _observed_at):
            calls.append(locator)
            lease_clock["now"] = started + timedelta(seconds=1)
            self.assertTrue(
                contender.acquire_lease(
                    "contender", handoff,
                    (started + timedelta(minutes=1)).isoformat().replace(
                        "+00:00", "Z",
                    ),
                ),
            )
            return False

        with (
            mock.patch.object(
                repair, "_lease_window", side_effect=lease_window,
            ),
            mock.patch.object(
                repair, "_lease_heartbeat",
                side_effect=lambda *_args: nullcontext(threading.Event()),
            ),
            mock.patch.object(
                repair, "_full_materialize",
                side_effect=hand_off_after_materialize,
            ),
        ):
            result = repair.run_batch(
                job_id, observed, directory_limit=2,
            )

        self.assertFalse(result.completed)
        self.assertEqual(calls, ["one.jsonl"])
        self.assertEqual(
            [frontier.state for frontier in self.repository.resume_frontier(job_id)],
            ["pending", "pending"],
        )
        self.assertEqual(self.repository.get_job(job_id).state, "running")

    def test_backfill_scandir_uses_held_directory_after_parent_symlink_swap(self) -> None:
        """The scan must enumerate the validated directory, never its new path."""
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        self.path.unlink()
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "safe.jsonl").write_text("{}\n")
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "escape.jsonl").write_text("{}\n")
        roots = TrustedSourceRoots(
            sessions=self.root, archived_sessions=self.root / "archive",
        )
        repair = ResumableRepair(self.store, roots)
        job_id = repair.start_backfill("2026-07-26T00:00:00Z")
        repair.run_batch(
            job_id, "2026-07-26T00:00:01Z", directory_limit=1,
        )
        original_directory = self.root / "nested-original"
        real_scandir = os.scandir

        def swap_then_scan(directory):
            nested.rename(original_directory)
            nested.symlink_to(outside, target_is_directory=True)
            return real_scandir(directory)

        with mock.patch(
            "hydra_codex.incremental_sync.os.scandir",
            side_effect=swap_then_scan,
        ):
            repair.run_batch(
                job_id, "2026-07-26T00:00:02Z", directory_limit=1,
            )

        locators = {
            frontier.directory_locator
            for frontier in self.repository.list_frontier(job_id)
        }
        self.assertIn("@file/nested/safe.jsonl", locators)
        self.assertNotIn("@file/nested/escape.jsonl", locators)

    def test_repair_creates_its_own_job_while_sync_is_active(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        sync_job = self.repository.create_job("sync", "2026-07-26T00:00:00Z")
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )

        repair_job = repair.start_backfill(
            "2026-07-26T00:00:01Z", job_kind="repair",
        )
        result = repair.run_batch(
            repair_job, "2026-07-26T00:00:01Z",
        )

        self.assertNotEqual(repair_job, sync_job)
        self.assertFalse(result.completed)
        jobs = {
            job.job_id: (job.job_kind, job.state)
            for job in self.repository.list_jobs()
        }
        self.assertEqual(jobs[sync_job], ("sync", "queued"))
        self.assertEqual(jobs[repair_job][0], "repair")

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

    def test_backfill_acknowledges_dirty_claim_after_reconcile_outlives_lease(self) -> None:
        from hydra_codex.incremental_sync import ResumableRepair, TrustedSourceRoots

        now = datetime.now(timezone.utc)
        observed_at = now.isoformat().replace("+00:00", "Z")
        self.repository.mark_dirty(
            "hprj_slow", "hprj_slow", "project", observed_at,
        )
        job_id = self.repository.create_job("backfill", observed_at)
        self.repository.save_frontier(
            job_id=job_id,
            root_kind="sessions",
            directory_locator="@root",
            state="scanned",
            discovered_count=0,
            updated_at=observed_at,
        )
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
            lease_ttl_seconds=1,
        )
        reconciled: list[str] = []

        def slow_reconcile(
            store, project_id, _key, *, expected_dirty_roots,
        ) -> None:
            reconciled.append(project_id)
            self.assertEqual(
                tuple(root.project_id for root in expected_dirty_roots),
                ("hprj_slow",),
            )
            with store.rollout_transaction() as connection:
                connection.execute(
                    "CREATE TABLE slow_backfill_reconcile(value INTEGER)",
                )
                connection.execute(
                    "INSERT INTO slow_backfill_reconcile(value) VALUES (1)",
                )
                time.sleep(1.25)

        with mock.patch(
            "hydra_codex.incremental_sync.reconcile_project",
            side_effect=slow_reconcile,
        ):
            result = repair.run_batch(
                job_id, observed_at, directory_limit=1,
            )

        self.assertTrue(result.completed)
        self.assertEqual(reconciled, ["hprj_slow"])
        self.assertEqual(self.repository.list_dirty_roots(), ())
        self.assertEqual(self.repository.get_job(job_id).state, "succeeded")

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
        lease_now = datetime.now(timezone.utc)
        lease_observed = lease_now.isoformat().replace("+00:00", "Z")
        lease_expiry = (lease_now + timedelta(minutes=1)).isoformat().replace(
            "+00:00", "Z",
        )
        self.assertTrue(self.repository.acquire_lease(
            "other", lease_observed, lease_expiry,
        ))
        blocked = repair.run_batch(job_id, "2026-07-26T00:00:02Z", directory_limit=1)
        self.assertFalse(blocked.completed)
        self.assertEqual(self.repository.get_job(job_id).state, "running")
        self.assertEqual(self.repository.get_job(job_id).sources_completed, 0)
        self.assertTrue(self.repository.release_lease(
            "other", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        ))
        resumed = repair.run_batch(job_id, "2026-07-26T00:00:03Z", directory_limit=1)
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

    def test_two_resumers_initialize_an_empty_frontier_once_under_the_lease(
        self,
    ) -> None:
        from hydra_codex.incremental_sync import (
            ResumableRepair,
            TrustedSourceRoots,
        )

        archive = Path(self.temporary.name) / "resumer-archive"
        archive.mkdir()
        roots = TrustedSourceRoots(
            sessions=self.root,
            archived_sessions=archive,
        )
        first = ResumableRepair(self.store, roots)
        second_store = HydraStore(self.database)
        self.addCleanup(second_store.close)
        second = ResumableRepair(second_store, roots)
        expansions: list[tuple[str, str, str]] = []
        contender_results: list[object] = []
        first_directory = first._directory
        second_directory = second._directory

        def first_with_contender(root_kind: str, locator: str):
            expansions.append(("first", root_kind, locator))
            contender_results.append(second.run_batch(
                job_id,
                "2026-07-26T00:00:01Z",
                directory_limit=1,
            ))
            return first_directory(root_kind, locator)

        def second_after_handoff(root_kind: str, locator: str):
            expansions.append(("second", root_kind, locator))
            return second_directory(root_kind, locator)

        with mock.patch.object(
            SyncStateRepository,
            "save_frontier",
            side_effect=AssertionError(
                "frontier initialization must require the worker lease",
            ),
        ):
            job_id = first.start_backfill("2026-07-26T00:00:00Z")
            self.assertEqual(self.repository.list_frontier(job_id), ())
            with mock.patch.object(
                first,
                "_directory",
                side_effect=first_with_contender,
            ):
                first_result = first.run_batch(
                    job_id,
                    "2026-07-26T00:00:01Z",
                    directory_limit=1,
                )
            with mock.patch.object(
                second,
                "_directory",
                side_effect=second_after_handoff,
            ):
                second_result = second.run_batch(
                    job_id,
                    "2026-07-26T00:00:02Z",
                    directory_limit=1,
                )

        self.assertFalse(first_result.completed)
        self.assertFalse(second_result.completed)
        self.assertEqual(len(contender_results), 1)
        self.assertFalse(contender_results[0].lease_acquired)
        self.assertEqual(
            expansions,
            [
                ("first", "archived_sessions", "@root"),
                ("second", "sessions", "@root"),
            ],
        )
        root_states = {
            (frontier.root_kind, frontier.directory_locator): frontier.state
            for frontier in self.repository.list_frontier(job_id)
            if frontier.directory_locator == "@root"
        }
        self.assertEqual(root_states, {
            ("archived_sessions", "@root"): "scanned",
            ("sessions", "@root"): "scanned",
        })

    def test_partial_root_seed_resume_discovers_archive_before_success(
        self,
    ) -> None:
        from hydra_codex.incremental_sync import (
            ResumableRepair,
            TrustedSourceRoots,
        )

        self.path.unlink()
        archive = Path(self.temporary.name) / "archived-only-root"
        archive.mkdir()
        project = Path(self.temporary.name) / "archive-project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_archive_resume"\ntelemetry = "hybrid"\n',
            encoding="utf-8",
        )
        archived_source = archive / "archive-only.jsonl"
        archived_source.write_text(
            '{"type":"session_meta","payload":{"id":"archive-only",'
            '"session_id":"archive-only",'
            f'"cwd":"{project}"}}}}\n',
            encoding="utf-8",
        )
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=archive,
            ),
        )
        job_id = repair.start_backfill("2026-07-26T00:00:00Z")
        # Exact crash image: the first owner persisted only the sessions
        # sentinel and died before creating the archive sentinel.
        self.repository.save_frontier(
            job_id=job_id,
            root_kind="sessions",
            directory_locator="@root",
            state="pending",
            discovered_count=0,
            updated_at="2026-07-26T00:00:00Z",
        )

        first = repair.run_batch(
            job_id,
            "2026-07-26T00:00:01Z",
            directory_limit=1,
        )

        self.assertFalse(first.completed)
        self.assertEqual(self.repository.get_job(job_id).state, "running")
        archive_root = self.store.connection.execute(
            """SELECT state FROM sync_backfill_frontier
                WHERE job_id=? AND root_kind='archived_sessions'
                  AND directory_locator='@root'""",
            (job_id,),
        ).fetchone()
        self.assertIsNotNone(archive_root)
        self.assertEqual(archive_root[0], "pending")

        second = repair.run_batch(
            job_id,
            "2026-07-26T00:00:02Z",
            directory_limit=1,
        )
        self.assertFalse(second.completed)
        self.assertEqual(second.discovered, 1)
        self.assertEqual(
            self.repository.get_job(job_id).sources_discovered,
            1,
        )

        third = repair.run_batch(
            job_id,
            "2026-07-26T00:00:03Z",
            directory_limit=1,
        )
        self.assertTrue(third.completed)
        self.assertEqual(self.repository.get_job(job_id).state, "succeeded")
        source = self.repository.source_for(
            "archived_sessions",
            "archive-only.jsonl",
        )
        self.assertIsNotNone(source)
        self.assertEqual(source.source_state, "ready")

    def test_terminal_handoff_after_initial_read_is_observed_under_the_lease(
        self,
    ) -> None:
        from hydra_codex.incremental_sync import (
            ResumableRepair,
            TrustedSourceRoots,
        )

        job_id = self.repository.create_job(
            "backfill",
            "2026-07-26T00:00:00Z",
        )
        self.repository.save_frontier(
            job_id=job_id,
            root_kind="sessions",
            directory_locator="@root",
            state="scanned",
            discovered_count=0,
            updated_at="2026-07-26T00:00:00Z",
        )
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=self.root / "archive",
            ),
        )
        external = HydraStore(self.database)
        self.addCleanup(external.close)
        external_repository = SyncStateRepository(external)
        acquire = repair.repository.acquire_lease
        handed_off = False

        def finish_then_handoff(
            owner_key: str,
            observed_at: str,
            expires_at: str,
        ) -> bool:
            nonlocal handed_off
            if not handed_off:
                handed_off = True
                self.assertTrue(external_repository.acquire_lease(
                    "terminal-owner",
                    observed_at,
                    expires_at,
                ))
                terminal = external_repository.refresh_job_from_frontier_if_owned(
                    job_id,
                    owner_key="terminal-owner",
                    lease_observed_at=observed_at,
                    state="succeeded",
                    updated_at=observed_at,
                    completed_at=observed_at,
                )
                self.assertIsNotNone(terminal)
                self.assertTrue(external_repository.release_lease(
                    "terminal-owner",
                    observed_at,
                ))
            return acquire(owner_key, observed_at, expires_at)

        with mock.patch.object(
            repair.repository,
            "acquire_lease",
            side_effect=finish_then_handoff,
        ):
            result = repair.run_batch(
                job_id,
                "2026-07-26T00:00:01Z",
                directory_limit=1,
            )

        self.assertTrue(result.completed)
        self.assertTrue(handed_off)
        self.assertEqual(self.repository.get_job(job_id).state, "succeeded")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM sync_worker_leases",
            ).fetchone()[0],
            0,
        )

    def test_pending_insert_before_terminal_refresh_keeps_job_running(
        self,
    ) -> None:
        from hydra_codex.incremental_sync import (
            ResumableRepair,
            TrustedSourceRoots,
        )

        archive = Path(self.temporary.name) / "finalize-race-archive"
        archive.mkdir()
        job_id = self.repository.create_job(
            "backfill",
            "2026-07-26T00:00:00Z",
        )
        for root_kind in ("sessions", "archived_sessions"):
            self.repository.save_frontier(
                job_id=job_id,
                root_kind=root_kind,
                directory_locator="@root",
                state="scanned",
                discovered_count=0,
                updated_at="2026-07-26T00:00:00Z",
            )
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=archive,
            ),
        )
        legacy_store = HydraStore(self.database)
        self.addCleanup(legacy_store.close)
        legacy = SyncStateRepository(legacy_store)
        refresh = repair.repository.refresh_job_from_frontier_if_owned
        inserted = False

        def insert_pending_before_terminal(*args, **kwargs):
            nonlocal inserted
            if (
                not inserted
                and kwargs.get("state") in {"succeeded", "partial"}
            ):
                inserted = True
                legacy.save_frontier(
                    job_id=job_id,
                    root_kind="sessions",
                    directory_locator="@file/late.jsonl",
                    state="pending",
                    discovered_count=1,
                    updated_at="2026-07-26T00:00:01Z",
                )
            return refresh(*args, **kwargs)

        with mock.patch.object(
            repair.repository,
            "refresh_job_from_frontier_if_owned",
            side_effect=insert_pending_before_terminal,
        ):
            result = repair.run_batch(
                job_id,
                "2026-07-26T00:00:01Z",
                directory_limit=1,
            )

        self.assertTrue(inserted)
        self.assertFalse(result.completed)
        self.assertEqual(self.repository.get_job(job_id).state, "running")
        self.assertEqual(
            [
                frontier.directory_locator
                for frontier in self.repository.resume_frontier(job_id)
            ],
            ["@file/late.jsonl"],
        )

    def test_lease_owner_tolerates_newer_unowned_observer_timestamp(
        self,
    ) -> None:
        from hydra_codex.incremental_sync import (
            ResumableRepair,
            TrustedSourceRoots,
        )

        self.path.unlink()
        archive = Path(self.temporary.name) / "observer-archive"
        archive.mkdir()
        job_id = self.repository.create_job(
            "backfill",
            "2026-07-26T00:00:00Z",
        )
        for root_kind, state in (
            ("sessions", "pending"),
            ("archived_sessions", "scanned"),
        ):
            self.repository.save_frontier(
                job_id=job_id,
                root_kind=root_kind,
                directory_locator="@root",
                state=state,
                discovered_count=0,
                updated_at="2026-07-26T00:00:00Z",
            )
        repair = ResumableRepair(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=archive,
            ),
        )
        external = HydraStore(self.database)
        self.addCleanup(external.close)
        external_repository = SyncStateRepository(external)
        directory = repair._directory
        observed = False

        def observe_with_newer_timestamp(root_kind: str, locator: str):
            nonlocal observed
            if not observed:
                observed = True
                current = external_repository.get_job(job_id)
                self.assertIsNotNone(current)
                external_repository.update_job(
                    job_id,
                    state="running",
                    sources_discovered=current.sources_discovered,
                    sources_completed=current.sources_completed,
                    bytes_processed=current.bytes_processed,
                    updated_at="2026-07-26T00:00:02Z",
                )
            return directory(root_kind, locator)

        with mock.patch.object(
            repair,
            "_directory",
            side_effect=observe_with_newer_timestamp,
        ):
            result = repair.run_batch(
                job_id,
                "2026-07-26T00:00:01Z",
                directory_limit=1,
            )

        self.assertTrue(observed)
        self.assertTrue(result.completed)
        job = self.repository.get_job(job_id)
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(job.updated_at, "2026-07-26T00:00:02Z")
        self.assertEqual(job.completed_at, "2026-07-26T00:00:02Z")

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
                reconcile=lambda project_id, dirty_roots: reconcile_project(
                    incremental, project_id, self.key,
                    expected_dirty_roots=dirty_roots,
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
                reconcile=lambda project_id, dirty_roots: reconcile_project(
                    incremental, project_id, self.key,
                    expected_dirty_roots=dirty_roots,
                ),
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
