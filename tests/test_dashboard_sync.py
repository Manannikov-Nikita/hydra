from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from hydra_codex.exact_time import require_exact_timestamp
from hydra_codex.storage import HydraStore
from hydra_codex.sync_state import SyncStateRepository


class DashboardSyncControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)
        self.repository = SyncStateRepository(self.store)
        self.now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        self.root = Path(self.temporary.name) / "sessions"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_persisted_summary_is_safe_and_recovered_after_controller_restart(self) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController

        job_id = self.repository.create_job("sync", "2026-07-27T10:00:00Z")
        self.repository.update_job(
            job_id, state="running", sources_discovered=4, sources_completed=2,
            bytes_processed=128, updated_at="2026-07-27T10:00:01Z",
        )
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=None, installation_key=b"k" * 32, clock=lambda: self.now,
        )
        try:
            summary = controller.current()
        finally:
            controller.close()

        self.assertEqual(summary["sync_ref"], job_id)
        self.assertEqual(summary["state"], "running")
        self.assertEqual(summary["progress"], {
            "sources_queued": 4, "sources_processed": 2, "new_bytes": 128,
        })
        self.assertNotIn("source_locator", repr(summary))

    def test_current_prefers_explicit_repair_over_newer_normal_sync(self) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController

        repair_id = self.repository.create_job(
            "repair", "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            repair_id, state="running", sources_discovered=0,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:00Z",
        )
        sync_id = self.repository.create_job(
            "sync", "2026-07-27T10:00:01Z",
        )
        self.repository.update_job(
            sync_id, state="running", sources_discovered=1,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:01Z",
        )
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=None, installation_key=b"k" * 32, clock=lambda: self.now,
        )
        try:
            summary = controller.current()
        finally:
            controller.close()

        self.assertEqual(summary["sync_ref"], repair_id)
        self.assertEqual(summary["kind"], "repair")

    def test_changes_is_monotonic_and_has_no_private_database_fields(self) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController

        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=None, installation_key=b"k" * 32, clock=lambda: self.now,
        )
        try:
            before = controller.changes(0)
            self.repository.create_job("sync", "2026-07-27T10:00:00Z")
            after = controller.changes(before["data_revision"])
        finally:
            controller.close()

        self.assertFalse(before["changed"])
        self.assertTrue(after["changed"])
        self.assertGreater(after["data_revision"], before["data_revision"])
        self.assertNotIn("path", repr(after).lower())

    def test_changes_reads_only_indexed_latest_jobs_not_the_history_list(self) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController

        active = self.repository.create_job(
            "sync", "2026-07-27T10:00:00Z",
        )
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=None, installation_key=b"k" * 32, clock=lambda: self.now,
        )
        try:
            with mock.patch.object(
                SyncStateRepository, "list_jobs",
                side_effect=AssertionError("changes must not sort job history"),
            ):
                payload = controller.changes(0)
        finally:
            controller.close()

        self.assertEqual(payload["sync"]["sync_ref"], active)

    def test_idle_polling_reuses_one_full_database_validation(self) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController
        from hydra_codex.incremental_sync import TrustedSourceRoots

        factory_calls = 0
        validation_calls = 0
        original_validate = HydraStore._validate_schema

        def factory() -> HydraStore:
            nonlocal factory_calls
            factory_calls += 1
            return HydraStore(self.database)

        def validate(store: HydraStore, latest: int) -> None:
            nonlocal validation_calls
            validation_calls += 1
            original_validate(store, latest)

        with mock.patch.object(HydraStore, "_validate_schema", new=validate):
            controller = DashboardSyncController(
                store_factory=factory,
                roots=TrustedSourceRoots(
                    sessions=self.root,
                    archived_sessions=Path(self.temporary.name) / "archived",
                ),
                installation_key=b"k" * 32,
                clock=lambda: self.now,
                auto_activate=False,
            )
            try:
                self.assertEqual(factory_calls, 0)
                self.assertEqual(validation_calls, 0)
                for _ in range(5):
                    controller.current()
                    controller.changes(0)
                time.sleep(1.2)
            finally:
                controller.close()

        self.assertEqual(factory_calls, 1)
        self.assertEqual(validation_calls, 1)

    def test_summary_rejects_invalid_persisted_job_identifier(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.create_job(
                "sync", "2026-07-27T10:00:00Z", job_id="private/path",
            )

    def controller(self):
        from hydra_codex.dashboard_sync import DashboardSyncController
        from hydra_codex.incremental_sync import TrustedSourceRoots

        return DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=Path(self.temporary.name) / "archived",
            ),
            installation_key=b"k" * 32,
            clock=lambda: self.now,
        )

    def wait_for(self, controller, job_id: str, states: set[str]) -> object:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = controller.get(job_id)
            if job["state"] in states:
                return job
            time.sleep(0.01)
        self.fail(f"sync job {job_id} did not reach {states}")

    def enqueue_source(self, name: str) -> None:
        (self.root / name).write_text('{"type":"session_meta"}\n', encoding="utf-8")
        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator=name, project_id="project-a",
            observed_at="2026-07-27T10:00:00Z",
        )

    def test_start_sync_reuses_active_job_without_a_writer_transaction(self) -> None:
        job_id = self.repository.create_job(
            "sync", "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            job_id, state="running", sources_discovered=1,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:00Z",
        )
        self.assertTrue(self.repository.acquire_lease(
            "existing-worker", "2026-07-27T10:00:00Z",
            "2026-07-27T10:05:00Z",
        ))
        controller = self.controller()
        try:
            with mock.patch.object(
                SyncStateRepository, "get_or_create_active_job",
                side_effect=AssertionError(
                    "active job reuse must remain read-only",
                ),
            ):
                started, reused = controller.start_sync()
        finally:
            controller.close()
            self.repository.release_lease(
                "existing-worker", "2026-07-27T10:00:01Z",
            )

        self.assertTrue(reused)
        self.assertEqual(started["sync_ref"], job_id)
        self.assertEqual(started["state"], "running")

    def test_start_sync_reuses_active_job_while_database_writer_is_busy(self) -> None:
        job_id = self.repository.create_job(
            "sync", "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            job_id, state="running", sources_discovered=1,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:00Z",
        )
        self.assertTrue(self.repository.acquire_lease(
            "existing-worker", "2026-07-27T10:00:00Z",
            "2026-07-27T10:05:00Z",
        ))
        writer = sqlite3.connect(self.database)
        writer.execute("BEGIN IMMEDIATE")
        controller = self.controller()
        try:
            started, reused = controller.start_sync()
        finally:
            writer.rollback()
            writer.close()
            controller.close()
            self.repository.release_lease(
                "existing-worker", "2026-07-27T10:00:01Z",
            )

        self.assertTrue(reused)
        self.assertEqual(started["sync_ref"], job_id)
        self.assertEqual(started["state"], "running")

    def test_controller_drains_more_than_one_thousand_real_queued_sources(self) -> None:
        for number in range(1_001):
            self.enqueue_source(f"source-{number:04d}.jsonl")
        controller = self.controller()
        try:
            started, reused = controller.start_sync()
            terminal = self.wait_for(controller, started["sync_ref"], {"succeeded", "partial", "failed"})
        finally:
            controller.close()

        self.assertFalse(reused)
        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(terminal["progress"]["sources_processed"], 1_001)
        self.assertEqual(self.repository.list_queue(), ())

    def test_chunked_source_counts_once_while_all_bytes_accumulate(self) -> None:
        line = '{"type":"event_msg","payload":{}}\n'
        (self.root / "chunked.jsonl").write_text(
            line * 35_000, encoding="utf-8",
        )
        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="chunked.jsonl",
            project_id="project-a",
            observed_at="2026-07-27T10:00:00Z",
        )
        controller = self.controller()
        try:
            started, reused = controller.start_sync()
            terminal = self.wait_for(
                controller, started["sync_ref"],
                {"succeeded", "partial", "failed"},
            )
        finally:
            controller.close()

        self.assertFalse(reused)
        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(terminal["progress"]["sources_queued"], 1)
        self.assertEqual(terminal["progress"]["sources_processed"], 1)
        self.assertEqual(
            terminal["progress"]["new_bytes"],
            len(line.encode("utf-8")) * 35_000,
        )

    def test_committed_source_progress_survives_crash_before_worker_returns(
        self,
    ) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            MaterializedSource,
            TrustedSourceRoots,
        )

        payload = b'{"safe":true}\n'
        (self.root / "crash-progress.jsonl").write_bytes(payload)
        self.repository.register_and_enqueue(
            root_kind="sessions", source_locator="crash-progress.jsonl",
            project_id="project-a", observed_at="2026-07-27T10:00:00Z",
        )
        job_id = self.repository.create_job(
            "sync", "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            job_id, state="running", sources_discovered=1,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:00Z",
        )
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=Path(self.temporary.name) / "archived",
            ),
            materialize=lambda *_args: MaterializedSource(),
            clock=lambda: self.now,
        )
        commit = worker._commit

        def commit_then_crash(*args, **kwargs):
            commit(*args, **kwargs)
            raise SystemExit("injected process death after durable commit")

        with (
            mock.patch.object(worker, "_commit", side_effect=commit_then_crash),
            self.assertRaisesRegex(SystemExit, "process death"),
        ):
            try:
                worker.sync_once(
                    "dashboard-worker", "2026-07-27T10:00:00Z",
                    "2026-07-27T10:05:00Z", job_id=job_id,
                )
            except TypeError as error:
                self.fail(f"sync worker cannot account a durable job: {error}")

        recovered = self.repository.get_job(job_id)
        assert recovered is not None
        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual(
            (
                recovered.sources_discovered,
                recovered.sources_completed,
                recovered.bytes_processed,
            ),
            (1, 1, len(payload)),
        )
        terminal = self.repository.finish_job_if_idle(
            job_id, updated_at="2026-07-27T10:06:00Z",
        )
        assert terminal is not None
        self.assertEqual(terminal.state, "succeeded")

    def test_explicit_repair_gets_own_job_and_runs_after_sync_lease_handoff(
        self,
    ) -> None:
        self.enqueue_source("handoff-repair.jsonl")
        self.assertTrue(self.repository.acquire_lease(
            "held-before-both", "2026-07-27T10:00:00Z",
            "2026-07-27T10:05:00Z",
        ))
        controller = self.controller()
        try:
            sync, sync_reused = controller.start_sync()
            repair, repair_reused = controller.start_repair()
            self.assertFalse(sync_reused)
            self.assertFalse(repair_reused)
            self.assertNotEqual(sync["sync_ref"], repair["sync_ref"])
            self.assertEqual((sync["kind"], repair["kind"]), ("sync", "repair"))
            self.assertEqual(
                self.repository.list_frontier(repair["sync_ref"]),
                (),
            )
            self.assertTrue(self.repository.release_lease(
                "held-before-both", "2026-07-27T10:00:01Z",
            ))
            sync_terminal = self.wait_for(
                controller, sync["sync_ref"],
                {"succeeded", "partial", "failed"},
            )
            repair_terminal = self.wait_for(
                controller, repair["sync_ref"],
                {"succeeded", "partial", "failed"},
            )
        finally:
            controller.close()

        self.assertIn(sync_terminal["state"], {"succeeded", "partial"})
        self.assertIn(repair_terminal["state"], {"succeeded", "partial"})
        self.assertTrue(self.repository.list_frontier(
            repair["sync_ref"],
        ))

    def test_explicit_repair_blocks_sync_before_its_next_lease_attempt(
        self,
    ) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController
        from hydra_codex.incremental_sync import TrustedSourceRoots

        self.enqueue_source("repair-priority.jsonl")
        repair_id = self.repository.create_job(
            "repair", "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            repair_id, state="running", sources_discovered=0,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:00Z",
        )
        sync_id = self.repository.create_job(
            "sync", "2026-07-27T10:00:00Z",
        )
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=Path(self.temporary.name) / "archived",
            ),
            installation_key=b"k" * 32,
            clock=lambda: self.now,
            auto_activate=False,
        )

        def stop_after_priority_yield(_timeout=None):
            controller._closed.set()
            return True

        try:
            with mock.patch.object(
                controller._closed, "wait",
                side_effect=stop_after_priority_yield,
            ):
                controller._run_sync(sync_id)
        finally:
            controller.close()

        self.assertEqual(len(self.repository.list_queue()), 1)
        self.assertEqual(self.repository.get_job(sync_id).state, "running")
        self.assertEqual(self.repository.get_job(repair_id).state, "running")

        self.repository.update_job(
            repair_id, state="succeeded", sources_discovered=0,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:01Z",
        )
        resumed = self.controller()
        try:
            terminal = self.wait_for(
                resumed, sync_id, {"succeeded", "partial", "failed"},
            )
        finally:
            resumed.close()

        self.assertEqual(self.repository.list_queue(), ())
        self.assertEqual(terminal["state"], "succeeded")

    def test_restart_resumes_explicit_repair_before_newer_normal_sync(
        self,
    ) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController
        from hydra_codex.incremental_sync import TrustedSourceRoots

        repair_id = self.repository.create_job(
            "repair", "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            repair_id, state="running", sources_discovered=0,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:00Z",
        )
        sync_id = self.repository.create_job(
            "sync", "2026-07-27T10:00:01Z",
        )
        self.repository.update_job(
            sync_id, state="running", sources_discovered=0,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:01Z",
        )
        self.assertTrue(self.repository.acquire_lease(
            "restart-blocker", "2026-07-27T10:00:02Z",
            "2026-07-27T10:05:00Z",
        ))
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=Path(self.temporary.name) / "archived",
            ),
            installation_key=b"k" * 32,
            clock=lambda: datetime(
                2026, 7, 27, 10, 0, 2, tzinfo=timezone.utc,
            ),
            auto_activate=False,
        )
        try:
            controller.activate()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with controller._lock:
                    active_threads = tuple(controller._threads)
                if active_threads:
                    break
                time.sleep(0.01)

            self.assertIn(repair_id, active_threads)
            self.assertNotIn(sync_id, active_threads)
        finally:
            controller.close()
            self.repository.release_lease(
                "restart-blocker", "2026-07-27T10:00:03Z",
            )

    def test_close_stops_after_the_current_batch_without_false_terminal_completion(self) -> None:
        for number in range(1_001):
            self.enqueue_source(f"close-{number:04d}.jsonl")
        controller = self.controller()
        try:
            started, _reused = controller.start_sync()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                remaining = self.store.connection.execute(
                    "SELECT COUNT(*) FROM sync_ingest_queue",
                ).fetchone()[0]
                if remaining < 1_001:
                    break
                time.sleep(0.01)
            self.assertLess(remaining, 1_001)
            controller.close(timeout=0.01)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                remaining = self.store.connection.execute(
                    "SELECT COUNT(*) FROM sync_ingest_queue",
                ).fetchone()[0]
                if remaining <= 1:
                    break
                time.sleep(0.05)
            paused = controller.get(started["sync_ref"])

            self.assertEqual(paused["state"], "running")
            self.assertEqual(remaining, 1)
        finally:
            controller.close(timeout=30)

    def test_new_controller_resumes_persisted_running_job_and_finishes(self) -> None:
        self.enqueue_source("resume.jsonl")
        job_id = self.repository.create_job("sync", "2026-07-27T10:00:00Z")
        self.repository.update_job(
            job_id, state="running", sources_discovered=1, sources_completed=0,
            bytes_processed=0, updated_at="2026-07-27T10:00:00Z",
        )
        controller = self.controller()
        try:
            terminal = self.wait_for(
                controller, job_id, {"succeeded", "partial", "failed"},
            )
        finally:
            controller.close()

        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(self.repository.list_queue(), ())

    def test_running_dashboard_starts_fresh_hook_work_without_manual_post(self) -> None:
        with mock.patch("hydra_codex.dashboard_sync.reconcile_project"):
            controller = self.controller()
            started_at = time.monotonic()
            try:
                self.repository.record_hook_event_and_enqueue(
                    event_key="fresh-event", project_id="fresh-project",
                    session_key="fresh-session", turn_key="fresh-turn",
                    event_kind="prompt", observed_at="2026-07-27T10:00:00Z",
                )
                terminal = None
                deadline = started_at + 2
                while time.monotonic() < deadline:
                    jobs = self.repository.list_jobs()
                    if jobs and jobs[0].state in {"succeeded", "partial", "failed"}:
                        terminal = jobs[0]
                        break
                    time.sleep(0.01)
            finally:
                controller.close()

        self.assertIsNotNone(terminal)
        self.assertLess(time.monotonic() - started_at, 2)
        self.assertEqual(terminal.state, "succeeded")
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM hook_event_outbox WHERE acknowledged_at IS NULL",
        ).fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM sync_dirty_roots",
        ).fetchone()[0], 0)

    def test_sync_exhausts_more_than_hook_and_dirty_batch_limits_before_success(self) -> None:
        now = "2026-07-27T10:00:00Z"
        with self.store.rollout_transaction() as connection:
            connection.executemany(
                """INSERT INTO hook_event_outbox(
                       event_key,project_id,session_key,turn_key,event_kind,
                       tool_category,tool_status,duration_ms,observed_at,
                       eligible_epoch_ns)
                   VALUES (?,?,?,?,?,NULL,NULL,NULL,?,?)""",
                (
                    (
                        f"event-{number:04d}", f"project-{number:04d}",
                        f"session-{number:04d}", f"turn-{number:04d}",
                        "prompt", now,
                        require_exact_timestamp(
                            now, "test hook eligibility",
                        ).epoch_nanoseconds,
                    )
                    for number in range(1_001)
                ),
            )
            self.repository._bump_revision(connection, now)
        with mock.patch("hydra_codex.dashboard_sync.reconcile_project"):
            controller = self.controller()
            try:
                started, _reused = controller.start_sync()
                terminal = self.wait_for(
                    controller, started["sync_ref"],
                    {"succeeded", "partial", "failed"},
                )
            finally:
                controller.close()

        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM hook_event_outbox WHERE acknowledged_at IS NULL",
        ).fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM sync_dirty_roots",
        ).fetchone()[0], 0)

    def test_two_controllers_preserve_batch_counters_across_worker_lease_handoff(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker

        self.enqueue_source("handoff-a.jsonl")
        self.enqueue_source("handoff-b.jsonl")
        job_id = self.repository.create_job("sync", "2026-07-27T10:00:00Z")
        self.repository.update_job(
            job_id, state="running", sources_discovered=2,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-27T10:00:00Z",
        )
        original_sync_once = IncrementalSyncWorker.sync_once
        original_release = SyncStateRepository.release_lease
        first_attempts = threading.Barrier(2)
        state_lock = threading.Lock()
        attempt_count = 0
        delayed_first_release = False
        acquired_owners: list[str] = []

        def one_source_per_lease(worker, owner, observed, expires, **options):
            nonlocal attempt_count
            with state_lock:
                attempt_count += 1
                synchronize = attempt_count <= 2
            if synchronize:
                first_attempts.wait(2)
            options["maximum_sources"] = 1
            result = original_sync_once(
                worker, owner, observed, expires, **options,
            )
            if result.lease_acquired:
                with state_lock:
                    acquired_owners.append(owner)
            return result

        def release_with_one_handoff_window(repository, owner, observed_at=None):
            nonlocal delayed_first_release
            released = original_release(repository, owner, observed_at)
            with state_lock:
                delay = released and not delayed_first_release
                if delay:
                    delayed_first_release = True
            if delay:
                time.sleep(0.7)
            return released

        with (
            mock.patch.object(
                IncrementalSyncWorker, "sync_once",
                autospec=True, side_effect=one_source_per_lease,
            ),
            mock.patch.object(
                SyncStateRepository, "release_lease",
                autospec=True, side_effect=release_with_one_handoff_window,
            ),
        ):
            first = self.controller()
            second = self.controller()
            try:
                terminal = self.wait_for(
                    first, job_id, {"succeeded", "partial", "failed"},
                )
            finally:
                first.close()
                second.close()

        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(terminal["progress"]["sources_processed"], 2)
        self.assertGreater(terminal["progress"]["new_bytes"], 0)
        self.assertEqual(len(set(acquired_owners)), 2)

    def test_controller_releases_worker_lease_when_a_retained_batch_raises(self) -> None:
        from hydra_codex.incremental_sync import IncrementalSyncWorker

        self.enqueue_source("raising.jsonl")

        def acquire_then_raise(worker, owner, observed, expires, **_options):
            self.assertTrue(worker.repository.acquire_lease(
                owner, observed, expires,
            ))
            raise RuntimeError("injected worker failure")

        with mock.patch.object(
            IncrementalSyncWorker, "sync_once", autospec=True,
            side_effect=acquire_then_raise,
        ):
            controller = self.controller()
            try:
                started, _reused = controller.start_sync()
                terminal = self.wait_for(
                    controller, started["sync_ref"], {"failed"},
                )
            finally:
                controller.close()

        self.assertEqual(terminal["state"], "failed")
        self.assertIsNone(self.store.connection.execute(
            "SELECT owner_key FROM sync_worker_leases WHERE lease_name='ingest'",
        ).fetchone())

    def test_contender_failure_cannot_fail_a_job_with_a_live_worker_lease(self) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController

        job_id = self.repository.create_job(
            "repair",
            "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            job_id,
            state="running",
            sources_discovered=4,
            sources_completed=2,
            bytes_processed=128,
            updated_at="2026-07-27T10:00:00Z",
        )
        self.assertTrue(self.repository.acquire_lease(
            "repair-owner",
            "2026-07-27T10:00:00Z",
            "2026-07-27T10:05:00Z",
        ))
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=None,
            installation_key=b"k" * 32,
            clock=lambda: self.now,
            auto_activate=False,
        )
        try:
            controller._fail(job_id)
            protected = self.repository.get_job(job_id)
            self.assertEqual(protected.state, "running")

            self.assertTrue(self.repository.release_lease(
                "repair-owner",
                "2026-07-27T10:00:01Z",
            ))
            controller._fail(job_id)
            terminal = self.repository.get_job(job_id)
        finally:
            controller.close()

        self.assertEqual(terminal.state, "failed")

    def test_worker_acquiring_at_failure_boundary_keeps_job_running(self) -> None:
        from hydra_codex.dashboard_sync import DashboardSyncController

        job_id = self.repository.create_job(
            "repair",
            "2026-07-27T10:00:00Z",
        )
        self.repository.update_job(
            job_id,
            state="running",
            sources_discovered=4,
            sources_completed=2,
            bytes_processed=128,
            updated_at="2026-07-27T10:00:00Z",
        )
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=None,
            installation_key=b"k" * 32,
            clock=lambda: self.now,
            auto_activate=False,
        )
        original = SyncStateRepository.fail_job_if_unleased

        def acquire_then_fail(
            repository: SyncStateRepository,
            selected_job: str,
            observed_at: str,
        ) -> bool:
            self.assertTrue(repository.acquire_lease(
                "new-worker",
                observed_at,
                "2026-07-27T10:05:00Z",
            ))
            return original(repository, selected_job, observed_at)

        try:
            with mock.patch.object(
                SyncStateRepository,
                "fail_job_if_unleased",
                autospec=True,
                side_effect=acquire_then_fail,
            ):
                controller._fail(job_id)
        finally:
            controller.close()

        self.assertEqual(self.repository.get_job(job_id).state, "running")
        self.assertEqual(
            self.store.connection.execute(
                """SELECT owner_key FROM sync_worker_leases
                    WHERE lease_name='ingest'""",
            ).fetchone()[0],
            "new-worker",
        )

    def test_held_lease_is_retried_by_the_same_controller_until_it_can_drain(self) -> None:
        self.enqueue_source("leased.jsonl")
        self.assertTrue(self.repository.acquire_lease(
            "other-worker", "2026-07-27T10:00:00Z", "2026-07-27T10:05:00Z",
        ))
        controller = self.controller()
        try:
            started, _reused = controller.start_sync()
            still_running = self.wait_for(
                controller, started["sync_ref"], {"running"},
            )
            self.assertEqual(still_running["state"], "running")
            self.assertEqual(len(self.repository.list_queue()), 1)
            self.assertTrue(self.repository.release_lease(
                "other-worker", "2026-07-27T10:00:01Z",
            ))
            terminal = self.wait_for(
                controller, started["sync_ref"], {"succeeded", "partial", "failed"},
            )
        finally:
            controller.close()

        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(self.repository.list_queue(), ())

    def test_deferred_queue_item_is_retried_when_the_controller_clock_advances(self) -> None:
        self.enqueue_source("deferred.jsonl")
        self.assertTrue(self.repository.acquire_lease(
            "setup", "2026-07-27T10:00:00Z", "2026-07-27T10:00:30Z",
        ))
        item = self.repository.claim_next(
            "setup", "2026-07-27T10:00:00Z", "2026-07-27T10:00:30Z",
        )
        self.assertIsNotNone(item)
        self.assertTrue(self.repository.retry_claim(
            "setup", "sessions", "deferred.jsonl",
            reason_code="transient_failure",
            available_at="2026-07-27T10:00:01Z",
            observed_at="2026-07-27T10:00:00Z",
        ))
        self.assertTrue(self.repository.release_lease(
            "setup", "2026-07-27T10:00:00Z",
        ))
        now = [self.now]
        from hydra_codex.dashboard_sync import DashboardSyncController
        from hydra_codex.incremental_sync import TrustedSourceRoots
        controller = DashboardSyncController(
            store_factory=lambda: HydraStore(self.database),
            roots=TrustedSourceRoots(
                sessions=self.root,
                archived_sessions=Path(self.temporary.name) / "archived",
            ),
            installation_key=b"k" * 32,
            clock=lambda: now[0],
        )
        try:
            started, _reused = controller.start_sync()
            self.wait_for(controller, started["sync_ref"], {"running"})
            self.assertEqual(len(self.repository.list_queue()), 1)
            now[0] = datetime(2026, 7, 27, 10, 0, 2, tzinfo=timezone.utc)
            terminal = self.wait_for(
                controller, started["sync_ref"], {"succeeded", "partial", "failed"},
            )
        finally:
            controller.close()

        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(self.repository.list_queue(), ())

    def test_future_queue_wait_does_not_churn_revision_every_hundred_milliseconds(self) -> None:
        self.enqueue_source("future.jsonl")
        self.assertTrue(self.repository.acquire_lease(
            "setup", "2026-07-27T10:00:00Z", "2026-07-27T10:00:30Z",
        ))
        item = self.repository.claim_next(
            "setup", "2026-07-27T10:00:00Z", "2026-07-27T10:00:30Z",
        )
        self.assertIsNotNone(item)
        self.assertTrue(self.repository.retry_claim(
            "setup", "sessions", "future.jsonl",
            reason_code="transient_failure",
            available_at="2026-07-27T10:00:10Z",
            observed_at="2026-07-27T10:00:00Z",
        ))
        self.assertTrue(self.repository.release_lease(
            "setup", "2026-07-27T10:00:00Z",
        ))
        controller = self.controller()
        try:
            started, _reused = controller.start_sync()
            self.wait_for(controller, started["sync_ref"], {"running"})
            time.sleep(0.2)
            stable_revision = self.repository.data_revision()
            time.sleep(0.35)
            self.assertEqual(self.repository.data_revision(), stable_revision)
        finally:
            controller.close()

    def test_repair_controller_persists_and_exhausts_a_multi_batch_directory_frontier(self) -> None:
        for number in range(101):
            (self.root / f"directory-{number:03d}").mkdir()
        controller = self.controller()
        try:
            started, reused = controller.start_repair()
            terminal = self.wait_for(controller, started["sync_ref"], {"succeeded", "partial", "failed"})
        finally:
            controller.close()

        self.assertFalse(reused)
        self.assertEqual(terminal["state"], "succeeded")
        job = self.repository.get_job(started["sync_ref"])
        assert job is not None
        self.assertGreaterEqual(len(self.repository.list_frontier(job.job_id, "scanned")), 102)
