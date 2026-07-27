from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
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
