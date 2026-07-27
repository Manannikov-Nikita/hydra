from __future__ import annotations

import sqlite3
import tempfile
import time
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

    def test_schema_42_creates_private_durable_sync_tables(self) -> None:
        tables = {
            str(row[0])
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        self.assertEqual(self.store.schema_version(), 42)
        self.assertTrue({
            "sync_source_registry", "sync_source_checkpoints", "sync_ingest_queue",
            "sync_worker_leases", "sync_dirty_roots", "sync_jobs",
            "sync_backfill_frontier", "sync_data_revision", "hook_event_outbox",
            "materialized_report_snapshots",
        }.issubset(tables))
        self.assertEqual(
            self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000,
        )

    def test_source_locator_is_validated_and_never_accepts_an_absolute_or_escaping_path(self) -> None:
        repository = self._repository()
        repository.register_source(
            root_kind="sessions", source_locator="2026/07/26/rollout.jsonl",
        )

        for invalid in (
            "/Users/alice/private.jsonl", "../private.jsonl", "a/../../b", "a\\b",
            "./rollout.jsonl", "a//b", "a/./b", "a/../b", "a/\nrollout.jsonl",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    repository.register_source(root_kind="sessions", source_locator=invalid)
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                "SELECT source_locator FROM sync_source_registry"
            ).fetchall()],
            [("2026/07/26/rollout.jsonl",)],
        )

    def test_database_rejects_noncanonical_locators_even_when_python_validation_is_bypassed(self) -> None:
        now = "2026-07-26T00:00:00Z"
        for invalid in ("/absolute", "a\\b", "../escape", "a//b", "a/./b", "a/../b", "a/\x01b"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store.connection.execute(
                        """INSERT INTO sync_source_registry(
                               root_kind,source_locator,source_state,first_seen_at,last_seen_at)
                           VALUES ('sessions',?,'ready',?,?)""",
                        (invalid, now, now),
                    )
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM sync_source_registry"
        ).fetchone()[0], 0)

    def test_same_relative_source_can_exist_in_active_and_archived_roots(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/rollout.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator, project_id="hprj_active",
            logical_source_key="source-active", session_key="session-active",
            observed_at="2026-07-26T00:00:00Z",
        )
        repository.register_and_enqueue(
            root_kind="archived_sessions", source_locator=locator, project_id="hprj_archive",
            logical_source_key="source-archive", session_key="session-archive",
            observed_at="2026-07-26T00:00:00Z",
        )

        self.assertEqual(
            [(item.root_kind, item.source_locator, item.project_id, item.session_key)
             for item in repository.list_sources()],
            [
                ("archived_sessions", locator, "hprj_archive", "session-archive"),
                ("sessions", locator, "hprj_active", "session-active"),
            ],
        )
        self.assertEqual(len(repository.list_queue()), 2)

    def test_queue_and_checkpoint_are_idempotent_and_keep_only_append_state(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/rollout.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator, observed_at="2026-07-26T00:00:00Z",
        )
        self.assertFalse(repository.enqueue("sessions", locator, "2026-07-26T00:00:01Z"))
        repository.save_checkpoint(
            "sessions", locator, byte_offset=128, file_size=128, line_number=4,
            prefix_anchor="a" * 64, revision_anchor="b" * 64,
        )
        checkpoint = repository.checkpoint_for("sessions", locator)

        self.assertEqual(checkpoint.byte_offset, 128)
        self.assertEqual(checkpoint.line_number, 4)
        self.assertEqual(checkpoint.prefix_anchor, "a" * 64)
        self.assertEqual(checkpoint.revision_anchor, "b" * 64)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM sync_ingest_queue").fetchone()[0], 1,
        )

    def test_checkpoint_cannot_exceed_file_size_and_failed_write_is_atomic(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/rollout.jsonl"
        repository.register_source(root_kind="sessions", source_locator=locator)
        before = repository.data_revision()
        with self.assertRaises(ValueError):
            repository.save_checkpoint(
                "sessions", locator, byte_offset=9, file_size=8, line_number=1,
                prefix_anchor=None, revision_anchor=None,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                """INSERT INTO sync_source_checkpoints(
                       root_kind,source_locator,file_size,byte_offset,line_number,updated_at)
                   VALUES ('sessions',?,8,9,1,'2026-07-26T00:00:00Z')""",
                (locator,),
            )
        self.assertEqual(repository.data_revision(), before)
        self.assertEqual(repository.checkpoint_for("sessions", locator).byte_offset, 0)

    def test_hook_fact_and_source_wakeup_share_one_transaction(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/atomic.jsonl"
        self.store.connection.execute(
            """CREATE TRIGGER reject_hook_queue BEFORE INSERT ON sync_ingest_queue
                 BEGIN SELECT RAISE(ABORT, 'forced queue failure'); END""",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced queue failure"):
            repository.record_hook_event_and_enqueue(
                event_key="event-safe", project_id="hprj_safe", session_key="session-safe",
                turn_key="turn-safe", event_kind="post_tool", tool_category="shell",
                tool_status="success", duration_ms=1, observed_at="2026-07-26T00:00:00Z",
                source=("sessions", locator),
            )
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM hook_event_outbox",
        ).fetchone()[0], 0)
        self.assertIsNone(repository.source_for("sessions", locator))

    def test_only_one_worker_holds_the_expiring_lease(self) -> None:
        repository = self._repository()
        self.assertTrue(repository.acquire_lease("worker-a", "2026-07-26T00:00:00Z", "2026-07-26T00:00:10Z"))
        self.assertFalse(repository.acquire_lease("worker-b", "2026-07-26T00:00:01Z", "2026-07-26T00:00:11Z"))
        self.assertTrue(repository.acquire_lease("worker-b", "2026-07-26T00:00:10Z", "2026-07-26T00:00:20Z"))
        lease = self.store.connection.execute(
            "SELECT owner_key,expires_at FROM sync_worker_leases WHERE lease_name='ingest'"
        ).fetchone()
        self.assertEqual(tuple(lease), ("worker-b", "2026-07-26T00:00:20Z"))

    def test_two_connections_claim_once_and_busy_failure_leaves_no_partial_state(self) -> None:
        second = HydraStore(self.database)
        self.addCleanup(second.close)
        first_repository = self._repository()
        from hydra_codex.sync_state import SyncStateRepository
        second_repository = SyncStateRepository(second)
        locator = "2026/07/26/rollout.jsonl"
        first_repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator, observed_at="2026-07-26T00:00:00Z",
        )
        self.assertTrue(first_repository.acquire_lease(
            "worker-a", "2026-07-26T00:00:00Z", "2026-07-26T00:10:00Z",
        ))
        claimed = first_repository.claim_next(
            "worker-a", "2026-07-26T00:00:01Z", "2026-07-26T00:01:00Z",
        )
        self.assertEqual((claimed.root_kind, claimed.source_locator), ("sessions", locator))
        self.assertIsNone(second_repository.claim_next(
            "worker-b", "2026-07-26T00:00:01Z", "2026-07-26T00:01:00Z",
        ))

        self.store.connection.execute("BEGIN IMMEDIATE")
        second.connection.execute("PRAGMA busy_timeout=25")
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            second_repository.register_and_enqueue(
                root_kind="sessions", source_locator="2026/07/26/blocked.jsonl",
                observed_at="2026-07-26T00:00:02Z",
            )
        self.assertLess(time.monotonic() - started, 1.0)
        self.store.connection.rollback()
        self.assertIsNone(second_repository.source_for("sessions", "2026/07/26/blocked.jsonl"))

    def test_worker_ready_queue_dirty_job_and_frontier_operations(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/rollout.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator, project_id="hprj_safe",
            logical_source_key="logical-safe", session_key="session-safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        self.assertTrue(repository.acquire_lease(
            "worker", "2026-07-26T00:00:00Z", "2026-07-26T00:10:00Z",
        ))
        item = repository.claim_next("worker", "2026-07-26T00:00:01Z", "2026-07-26T00:01:00Z")
        self.assertEqual((item.root_kind, item.project_id, item.logical_source_key),
                         ("sessions", "hprj_safe", "logical-safe"))
        self.assertTrue(repository.retry_claim(
            "worker", item.root_kind, item.source_locator, reason_code="partial_line",
            available_at="2026-07-26T00:00:02Z", observed_at="2026-07-26T00:00:01Z",
        ))
        self.assertEqual(repository.list_queue()[0].attempts, 1)
        item = repository.claim_next("worker", "2026-07-26T00:00:02Z", "2026-07-26T00:01:00Z")
        self.assertTrue(repository.acknowledge_claim("worker", item.root_kind, item.source_locator,
                                                      "2026-07-26T00:00:03Z"))

        repository.mark_dirty("hprj_safe", "task-safe", "task", "2026-07-26T00:00:00Z")
        dirty = repository.claim_dirty_roots(
            "worker", "2026-07-26T00:00:03Z", "2026-07-26T00:01:00Z", 10,
        )
        self.assertEqual([root.root_key for root in dirty], ["task-safe"])
        self.assertEqual(repository.acknowledge_dirty_roots("worker", dirty, "2026-07-26T00:00:04Z"), 1)
        self.assertEqual(repository.list_dirty_roots(), ())
        job_id = repository.create_job("backfill", "2026-07-26T00:00:00Z")
        repository.save_frontier(
            job_id=job_id, root_kind="sessions", directory_locator="2026/07/26", state="pending",
            discovered_count=1, updated_at="2026-07-26T00:00:01Z",
        )
        other_job = repository.create_job("backfill", "2026-07-26T00:00:02Z")
        repository.save_frontier(
            job_id=other_job, root_kind="sessions", directory_locator="2026/07/26", state="scanned",
            discovered_count=2, updated_at="2026-07-26T00:00:03Z",
        )
        self.assertEqual(repository.current_job("backfill").job_id, other_job)
        self.assertEqual(repository.resume_frontier(job_id)[0].directory_locator, "2026/07/26")
        self.assertEqual(repository.get_job(other_job).job_id, other_job)
        self.assertEqual(repository.list_frontier(other_job)[0].state, "scanned")
        self.assertEqual({job.job_id for job in repository.list_jobs()}, {job_id, other_job})

    def test_enqueue_during_claim_is_requeued_after_acknowledgement(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/race.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator, observed_at="2026-07-26T00:00:00Z",
        )
        self.assertTrue(repository.acquire_lease(
            "worker", "2026-07-26T00:00:00Z", "2026-07-26T00:10:00Z",
        ))
        first = repository.claim_next("worker", "2026-07-26T00:00:01Z", "2026-07-26T00:01:00Z")
        self.assertEqual(first.source_locator, locator)
        self.assertFalse(repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator, observed_at="2026-07-26T00:00:02Z",
        ))
        self.assertTrue(repository.acknowledge_claim(
            "worker", "sessions", locator, "2026-07-26T00:00:03Z",
        ))
        second = repository.claim_next("worker", "2026-07-26T00:00:04Z", "2026-07-26T00:01:00Z")
        self.assertEqual(second.source_locator, locator)

    def test_sync_timestamps_are_canonical_utc_rfc3339(self) -> None:
        repository = self._repository()
        invalid = ("2026-07-26T00:00:00z", "2026-07-26T00:00:00+00:00", "zz")
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    repository.register_source(
                        root_kind="sessions", source_locator="2026/07/26/time.jsonl", observed_at=value,
                    )
                with self.assertRaises(ValueError):
                    repository.acquire_lease("worker", value, "2026-07-26T00:01:00Z")
                with self.assertRaises(ValueError):
                    repository.create_job("sync", value)

    def test_tampered_w23_locator_trigger_fails_closed_on_reopen(self) -> None:
        self.store.close()
        altered = sqlite3.connect(self.database)
        altered.execute("DROP TRIGGER sync_source_registry_canonical_locator_insert")
        altered.commit()
        altered.close()
        from hydra_codex.storage import StorageUnavailable
        with self.assertRaisesRegex(StorageUnavailable, "incremental sync trust constraints"):
            HydraStore(self.database)

    def test_crashed_queue_and_dirty_claims_are_reclaimed_after_restart(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/crash.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator, project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        repository.mark_dirty("hprj_safe", "task-safe", "task", "2026-07-26T00:00:00Z")
        self.assertTrue(repository.acquire_lease(
            "crashed", "2026-07-26T00:00:00Z", "2026-07-26T00:00:10Z",
        ))
        self.assertEqual(
            repository.claim_next("crashed", "2026-07-26T00:00:01Z", "2026-07-26T00:00:10Z").source_locator,
            locator,
        )
        claimed_dirty = repository.claim_dirty_roots(
            "crashed", "2026-07-26T00:00:01Z", "2026-07-26T00:00:10Z", 10,
        )
        self.assertEqual([root.root_key for root in claimed_dirty], ["task-safe"])
        self.store.close()  # Simulate the owning MCP/dashboard process crashing before acknowledgements.

        restarted = HydraStore(self.database)
        self.addCleanup(restarted.close)
        from hydra_codex.sync_state import SyncStateRepository
        recovered = SyncStateRepository(restarted)
        self.assertTrue(recovered.acquire_lease(
            "recovered", "2026-07-26T00:00:10Z", "2026-07-26T00:00:20Z",
        ))
        queue_item = recovered.claim_next(
            "recovered", "2026-07-26T00:00:10Z", "2026-07-26T00:00:20Z",
        )
        self.assertEqual(queue_item.source_locator, locator)
        self.assertTrue(recovered.acknowledge_claim(
            "recovered", "sessions", locator, "2026-07-26T00:00:11Z",
        ))
        self.assertEqual([root.root_key for root in recovered.list_dirty_roots()], ["task-safe"])
        recovered_dirty = recovered.claim_dirty_roots(
            "recovered", "2026-07-26T00:00:10Z", "2026-07-26T00:00:20Z", 10,
        )
        self.assertEqual([root.root_key for root in recovered_dirty], ["task-safe"])
        self.assertEqual(recovered.acknowledge_dirty_roots(
            "recovered", recovered_dirty, "2026-07-26T00:00:11Z",
        ), 1)
        self.assertEqual(recovered.list_dirty_roots(), ())

    def test_dirty_roots_jobs_frontier_and_revision_survive_store_reopen(self) -> None:
        repository = self._repository()
        revision = repository.mark_dirty("hprj_safe", "task-safe", "task", "2026-07-26T00:00:00Z")
        job_id = repository.create_job("backfill", "2026-07-26T00:00:00Z")
        repository.update_job(
            job_id, state="running", sources_discovered=1509, sources_completed=10,
            bytes_processed=1024, updated_at="2026-07-26T00:00:01Z",
        )
        repository.save_frontier(
            job_id=job_id, root_kind="sessions", directory_locator="2026/07/26", state="pending",
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
        self.assertEqual(upgraded.schema_version(), 42)
        self.assertEqual(
            upgraded.connection.execute(
                "SELECT display_name FROM dashboard_projects WHERE project_id='hprj_preserved'"
            ).fetchone()[0],
            "Preserved",
        )

    def test_tampered_v42_materialized_snapshot_schema_fails_closed_on_reopen(self) -> None:
        self.store.connection.execute("DROP TABLE materialized_report_snapshots")
        self.store.connection.execute(
            """CREATE TABLE materialized_report_snapshots (
                   project_id TEXT NOT NULL,task_ref TEXT NOT NULL,report_json TEXT NOT NULL,
                   report_markdown TEXT NOT NULL,report_html TEXT,
                   reconciled_at TEXT NOT NULL,data_revision INTEGER NOT NULL,
                   PRIMARY KEY(project_id,task_ref)) WITHOUT ROWID""",
        )
        self.store.connection.commit()
        self.store.close()
        with self.assertRaisesRegex(Exception, "schema"):
            HydraStore(self.database)


if __name__ == "__main__":
    unittest.main()
