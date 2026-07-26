from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hydra_codex.storage import HydraStore


class DurableSyncStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _repository(self):
        try:
            from hydra_codex.sync_state import SyncStateRepository
        except ImportError as error:  # TDD: the durable state boundary is required.
            self.fail(f"durable sync state module is missing: {error}")
        return SyncStateRepository(self.store)

    def test_schema_39_creates_private_durable_sync_tables(self) -> None:
        tables = {
            str(row[0])
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        self.assertEqual(self.store.schema_version(), 39)
        self.assertTrue({
            "sync_source_registry", "sync_source_checkpoints", "sync_ingest_queue",
            "sync_worker_leases", "sync_dirty_roots", "sync_jobs",
            "sync_backfill_frontier", "sync_data_revision",
        }.issubset(tables))
        self.assertEqual(
            self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000,
        )

    def test_source_locator_is_validated_and_never_accepts_an_absolute_or_escaping_path(self) -> None:
        repository = self._repository()
        repository.register_source(
            root_kind="sessions", source_locator="2026/07/26/rollout.jsonl",
        )

        for invalid in ("/Users/alice/private.jsonl", "../private.jsonl", "a/../../b", "a\\b"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    repository.register_source(root_kind="sessions", source_locator=invalid)
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT source_locator FROM sync_source_registry"
            ).fetchall()],
            [("2026/07/26/rollout.jsonl",)],
        )

    def test_queue_and_checkpoint_are_idempotent_and_keep_only_append_state(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/rollout.jsonl"
        repository.register_source(root_kind="sessions", source_locator=locator)
        self.assertTrue(repository.enqueue(locator, "2026-07-26T00:00:00Z"))
        self.assertFalse(repository.enqueue(locator, "2026-07-26T00:00:01Z"))
        repository.save_checkpoint(
            locator, byte_offset=128, line_number=4,
            prefix_anchor="a" * 64, revision_anchor="b" * 64,
        )
        checkpoint = repository.checkpoint_for(locator)

        self.assertEqual(checkpoint.byte_offset, 128)
        self.assertEqual(checkpoint.line_number, 4)
        self.assertEqual(checkpoint.prefix_anchor, "a" * 64)
        self.assertEqual(checkpoint.revision_anchor, "b" * 64)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM sync_ingest_queue").fetchone()[0], 1,
        )

    def test_only_one_worker_holds_the_expiring_lease(self) -> None:
        repository = self._repository()
        self.assertTrue(repository.acquire_lease("worker-a", "2026-07-26T00:00:00Z", "2026-07-26T00:00:10Z"))
        self.assertFalse(repository.acquire_lease("worker-b", "2026-07-26T00:00:01Z", "2026-07-26T00:00:11Z"))
        self.assertTrue(repository.acquire_lease("worker-b", "2026-07-26T00:00:10Z", "2026-07-26T00:00:20Z"))
        lease = self.store.connection.execute(
            "SELECT owner_key,expires_at FROM sync_worker_leases WHERE lease_name='ingest'"
        ).fetchone()
        self.assertEqual(tuple(lease), ("worker-b", "2026-07-26T00:00:20Z"))

    def test_dirty_roots_jobs_frontier_and_revision_survive_store_reopen(self) -> None:
        repository = self._repository()
        revision = repository.mark_dirty("hprj_safe", "task-safe", "task", "2026-07-26T00:00:00Z")
        job_id = repository.create_job("backfill", "2026-07-26T00:00:00Z")
        repository.update_job(
            job_id, state="running", sources_discovered=1509, sources_completed=10,
            bytes_processed=1024, updated_at="2026-07-26T00:00:01Z",
        )
        repository.save_frontier(
            root_kind="sessions", directory_locator="2026/07/26", state="pending",
            discovered_count=10, updated_at="2026-07-26T00:00:01Z",
        )
        self.assertGreaterEqual(revision, 1)
        persisted_revision = repository.data_revision()
        self.store.close()
        reopened = HydraStore(self.database)
        self.addCleanup(reopened.close)

        self.assertEqual(
            reopened.connection.execute(
                "SELECT root_key FROM sync_dirty_roots WHERE project_id='hprj_safe'"
            ).fetchone()[0],
            "task-safe",
        )
        self.assertEqual(
            tuple(reopened.connection.execute(
                "SELECT sources_completed,bytes_processed FROM sync_jobs WHERE job_id=?", (job_id,)
            ).fetchone()),
            (10, 1024),
        )
        self.assertEqual(
            reopened.connection.execute(
                "SELECT directory_locator FROM sync_backfill_frontier WHERE root_kind='sessions'"
            ).fetchone()[0],
            "2026/07/26",
        )
        self.assertEqual(
            reopened.connection.execute("SELECT revision FROM sync_data_revision WHERE singleton=1").fetchone()[0],
            persisted_revision,
        )

    def test_v38_database_upgrades_without_losing_existing_catalog_rows(self) -> None:
        legacy_path = Path(self.temporary.name) / "v38.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        from hydra_codex.storage import MIGRATIONS
        for version, statements in MIGRATIONS:
            if version > 38:
                break
            for statement in statements:
                legacy.execute(statement)
            legacy.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?, '2026-07-26T00:00:00Z')",
                (version,),
            )
            legacy.execute(f"PRAGMA user_version={version}")
        legacy.execute(
            """INSERT INTO dashboard_projects(project_id,display_name,first_seen_at,last_seen_at)
               VALUES ('hprj_preserved','Preserved','2026-07-26T00:00:00Z','2026-07-26T00:00:00Z')"""
        )
        legacy.commit()
        legacy.close()

        upgraded = HydraStore(legacy_path)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.schema_version(), 39)
        self.assertEqual(
            upgraded.connection.execute(
                "SELECT display_name FROM dashboard_projects WHERE project_id='hprj_preserved'"
            ).fetchone()[0],
            "Preserved",
        )


if __name__ == "__main__":
    unittest.main()
