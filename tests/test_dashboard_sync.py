from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest

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
