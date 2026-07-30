from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hydra_codex.exact_time import require_exact_timestamp
from hydra_codex.migrations_aa27 import (
    AA27_DIRTY_ELIGIBILITY_INSERT_TRIGGER_SQL,
    AA27_DIRTY_ELIGIBILITY_UPDATE_TRIGGER_SQL,
    AA27_SYNC_DIRTY_ROOTS_TABLE_SQL,
)
from hydra_codex.migrations_ab28 import AB28_MIGRATIONS
from hydra_codex.storage import MIGRATIONS, HydraStore


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

        self.assertEqual(self.store.schema_version(), MIGRATIONS[-1][0])
        self.assertTrue({
            "sync_source_registry", "sync_source_checkpoints", "sync_ingest_queue",
            "sync_worker_leases", "sync_dirty_roots", "sync_jobs",
            "sync_backfill_frontier", "sync_data_revision", "hook_event_outbox",
            "materialized_report_snapshots",
        }.issubset(tables))
        self.assertEqual(
            self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000,
        )

    def test_project_observation_bumps_revision_only_for_material_catalog_changes(self) -> None:
        repository = self._repository()

        repository.observe_project(
            project_id="hprj_current",
            display_name="hydra",
            display_name_provenance="repo_basename",
            observed_at="2026-07-30T10:00:00Z",
        )
        inserted_revision = repository.data_revision()
        self.assertEqual(inserted_revision, 1)

        for observed_at in (
            "2026-07-30T10:00:00Z",
            "2026-07-30T09:59:59Z",
        ):
            repository.observe_project(
                project_id="hprj_current",
                display_name="hydra",
                display_name_provenance="repo_basename",
                observed_at=observed_at,
            )
        self.assertEqual(repository.data_revision(), inserted_revision)

        repository.observe_project(
            project_id="hprj_current",
            display_name="hydra",
            display_name_provenance="repo_basename",
            observed_at="2026-07-30T10:00:01Z",
        )
        self.assertEqual(repository.data_revision(), inserted_revision + 1)

        repository.observe_project(
            project_id="hprj_current",
            display_name="Hydra Core",
            display_name_provenance="config",
            observed_at="2026-07-30T10:00:01Z",
        )
        self.assertEqual(repository.data_revision(), inserted_revision + 2)
        row = self.store.connection.execute(
            """SELECT display_name,display_name_provenance,first_seen_at,last_seen_at
                 FROM dashboard_projects WHERE project_id='hprj_current'""",
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                "Hydra Core",
                "config",
                "2026-07-30T10:00:00Z",
                "2026-07-30T10:00:01Z",
            ),
        )

    def test_schema_46_releases_legacy_dirty_claims_and_requires_a_generation_token(self) -> None:
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        legacy.create_function(
            "hydra_rfc3339_nanos",
            1,
            lambda value: require_exact_timestamp(
                value, "test dirty eligibility",
            ).epoch_nanoseconds,
            deterministic=True,
        )
        legacy.execute(AA27_SYNC_DIRTY_ROOTS_TABLE_SQL)
        legacy.execute(AA27_DIRTY_ELIGIBILITY_INSERT_TRIGGER_SQL)
        legacy.execute(AA27_DIRTY_ELIGIBILITY_UPDATE_TRIGGER_SQL)
        observed_at = "2026-07-26T00:00:00Z"
        expires_at = "2026-07-26T00:00:10Z"
        legacy.execute(
            """INSERT INTO sync_dirty_roots(
                   project_id,root_key,root_kind,observed_at,claim_owner,
                   claim_expires_at,eligible_epoch_ns
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                "hprj_safe", "task-safe", "task", observed_at,
                "legacy-worker", expires_at,
                require_exact_timestamp(
                    expires_at, "test dirty expiry",
                ).epoch_nanoseconds,
            ),
        )

        for statement in AB28_MIGRATIONS[0][1]:
            legacy.execute(statement)

        row = legacy.execute(
            """SELECT claim_owner,claim_expires_at,claim_token,eligible_epoch_ns
                 FROM sync_dirty_roots""",
        ).fetchone()
        self.assertEqual(
            row,
            (
                None, None, None,
                require_exact_timestamp(
                    observed_at, "test dirty observation",
                ).epoch_nanoseconds,
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            legacy.execute(
                """UPDATE sync_dirty_roots
                      SET claim_owner='worker',
                          claim_expires_at=?,
                          eligible_epoch_ns=?""",
                (
                    expires_at,
                    require_exact_timestamp(
                        expires_at, "test dirty expiry",
                    ).epoch_nanoseconds,
                ),
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

    def test_deferred_work_poll_uses_bounded_summary_and_eligibility_indexes(
        self,
    ) -> None:
        repository = self._repository()
        future = "2026-07-27T00:00:00Z"
        for index in range(256):
            repository.register_and_enqueue(
                root_kind="sessions",
                source_locator=f"deferred/{index:04d}.jsonl",
                observed_at=future,
            )
        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            state = repository.pending_work("2026-07-26T00:00:00Z")
        finally:
            self.store.connection.set_trace_callback(None)

        self.assertEqual(
            (state.total, state.eligible, state.next_eligible_at),
            (256, 0, future),
        )
        poll = next(
            statement for statement in statements
            if "sync_ingest_queue" in statement
            and statement.lstrip().upper().startswith(("SELECT", "WITH"))
        )
        plan = tuple(
            str(row[3]).upper()
            for row in self.store.connection.execute(
                "EXPLAIN QUERY PLAN " + poll,
            )
        )
        for table in (
            "SYNC_INGEST_QUEUE", "HOOK_EVENT_OUTBOX", "SYNC_DIRTY_ROOTS",
        ):
            self.assertFalse(
                any(detail == f"SCAN {table}" for detail in plan),
                plan,
            )

    def test_queue_rejects_a_valid_epoch_borrowed_from_another_source(
        self,
    ) -> None:
        repository = self._repository()
        repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="deferred/first.jsonl",
            observed_at="2026-07-27T00:00:00Z",
        )
        repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="deferred/second.jsonl",
            observed_at="2026-07-28T00:00:00Z",
        )
        borrowed = self.store.connection.execute(
            """SELECT eligible_epoch_ns FROM sync_ingest_queue
                WHERE source_locator='deferred/second.jsonl'""",
        ).fetchone()[0]

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                """UPDATE sync_ingest_queue SET eligible_epoch_ns=?
                    WHERE source_locator='deferred/first.jsonl'""",
                (borrowed,),
            )

        state = repository.pending_work("2026-07-26T00:00:00Z")
        self.assertEqual(
            (state.total, state.eligible, state.next_eligible_at),
            (2, 0, "2026-07-27T00:00:00Z"),
        )

    def test_pending_outbox_poll_uses_eligibility_index_without_sorting(
        self,
    ) -> None:
        repository = self._repository()
        future = "2026-07-27T00:00:00Z"
        with self.store.rollout_transaction() as connection:
            connection.executemany(
                """INSERT INTO hook_event_outbox(
                       event_key,project_id,session_key,turn_key,event_kind,
                       observed_at,acknowledged_at,eligible_epoch_ns)
                   VALUES (?,?,?,?,?,?,?,NULL)""",
                (
                    (
                        f"acknowledged-{index:04d}", f"old-project-{index:04d}",
                        f"old-session-{index:04d}", f"old-turn-{index:04d}",
                        "prompt", future, future,
                    )
                    for index in range(1_500)
                ),
            )
        for index in range(1_500):
            repository.record_hook_event_and_enqueue(
                event_key=f"event-{index:04d}",
                project_id=f"project-{index:04d}",
                session_key=f"session-{index:04d}",
                turn_key=f"turn-{index:04d}",
                event_kind="prompt",
                observed_at=future,
            )
        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            state = repository.pending_work("2026-07-26T00:00:00Z")
        finally:
            self.store.connection.set_trace_callback(None)

        self.assertEqual(
            (state.total, state.eligible, state.next_eligible_at),
            (1_500, 0, future),
        )
        poll = next(
            statement for statement in statements
            if "sync_work_summary AS summary" in statement
        )
        plan = tuple(
            str(row[3]).upper()
            for row in self.store.connection.execute(
                "EXPLAIN QUERY PLAN " + poll,
            )
        )
        index_sql = str(self.store.connection.execute(
            """SELECT sql FROM sqlite_master
                WHERE type='index'
                  AND name='hook_event_outbox_eligibility'""",
        ).fetchone()[0]).upper()
        self.assertIn("WHERE ACKNOWLEDGED_AT IS NULL", index_sql)
        self.assertTrue(
            any("HOOK_EVENT_OUTBOX_ELIGIBILITY" in detail for detail in plan),
            plan,
        )
        self.assertFalse(
            any("TEMP B-TREE" in detail for detail in plan),
            plan,
        )

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

    def test_hook_fact_replay_does_not_advance_an_unchanged_dirty_generation(
        self,
    ) -> None:
        repository = self._repository()
        repository.record_hook_event_and_enqueue(
            event_key="replayed-event",
            project_id="hprj_safe",
            session_key="session-safe",
            turn_key="turn-safe",
            event_kind="post_tool",
            tool_category="shell",
            tool_status="success",
            duration_ms=1,
            observed_at="2026-07-26T00:00:00.0000001Z",
        )
        self.assertTrue(repository.acquire_lease(
            "worker-a",
            "2026-07-26T00:00:00.0000001Z",
            "2026-07-26T00:00:10Z",
        ))
        first = repository.claim_hook_events(
            "worker-a",
            "2026-07-26T00:00:00.0000001Z",
            "2026-07-26T00:00:10Z",
        )
        self.assertEqual(len(first), 1)
        original = repository.list_dirty_roots()
        self.assertEqual(original[0].observed_at, "2026-07-26T00:00:00.0000001Z")

        self.assertTrue(repository.acquire_lease(
            "worker-b",
            "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z",
        ))
        replayed = repository.claim_hook_events(
            "worker-b",
            "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z",
        )

        self.assertEqual(len(replayed), 1)
        self.assertEqual(repository.list_dirty_roots(), original)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM hook_safe_facts"
            ).fetchone()[0],
            1,
        )

    def test_hook_identity_is_not_written_as_a_new_source_session_binding(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/hook-session.jsonl"

        repository.record_hook_event_and_enqueue(
            event_key="hook-event",
            project_id="hprj_safe",
            session_key="hook-session",
            turn_key="hook-turn",
            event_kind="prompt",
            observed_at="2026-07-26T00:00:00Z",
            source=("sessions", locator),
        )

        self.assertEqual(
            self.store.connection.execute(
                "SELECT session_key FROM hook_event_outbox",
            ).fetchone()[0],
            "hook-session",
        )
        self.assertIsNone(
            repository.source_for("sessions", locator).session_key,
        )

    def test_hook_wakeup_preserves_an_existing_source_session_binding(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/existing-session.jsonl"
        repository.register_source(
            root_kind="sessions",
            source_locator=locator,
            project_id="hprj_safe",
            session_key="transcript-session",
            observed_at="2026-07-26T00:00:00Z",
        )

        repository.record_hook_event_and_enqueue(
            event_key="hook-event",
            project_id="hprj_safe",
            session_key="different-hook-session",
            turn_key="hook-turn",
            event_kind="prompt",
            observed_at="2026-07-26T00:00:01Z",
            source=("sessions", locator),
        )

        self.assertEqual(
            repository.source_for("sessions", locator).session_key,
            "transcript-session",
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

    def test_claim_renewal_never_moves_live_ownership_backwards(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/monotonic.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator,
            project_id="hprj_safe",
            observed_at="2026-07-26T00:00:00Z",
        )
        self.assertTrue(repository.acquire_lease(
            "worker", "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z",
        ))
        self.assertIsNotNone(repository.claim_next(
            "worker", "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z",
        ))

        self.assertTrue(repository.renew_claim(
            "worker", "sessions", locator,
            "2026-07-26T00:00:11Z",
            "2026-07-26T00:00:15Z",
        ))

        lease = self.store.connection.execute(
            """SELECT acquired_at,expires_at FROM sync_worker_leases
                 WHERE lease_name='ingest'""",
        ).fetchone()
        queue = self.store.connection.execute(
            """SELECT claim_expires_at FROM sync_ingest_queue
                 WHERE root_kind='sessions' AND source_locator=?""",
            (locator,),
        ).fetchone()
        self.assertEqual(tuple(lease), (
            "2026-07-26T00:00:11Z", "2026-07-26T00:00:20Z",
        ))
        self.assertEqual(queue[0], "2026-07-26T00:00:20Z")
        revision = repository.data_revision()
        self.assertFalse(repository.acquire_lease(
            "worker", "2026-07-26T00:00:10.5Z",
            "2026-07-26T00:00:16Z",
        ))
        self.assertEqual(repository.data_revision(), revision)

    def test_lease_and_queue_eligibility_compare_fractional_timestamps_chronologically(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/fractional.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions", source_locator=locator,
            observed_at="2026-07-26T00:00:00.01Z",
        )
        self.store.connection.execute(
            """UPDATE sync_ingest_queue
                  SET available_at='2026-07-26T00:00:00.100002Z',
                      eligible_epoch_ns=?""",
            (
                require_exact_timestamp(
                    "2026-07-26T00:00:00.100002Z",
                    "test queue eligibility",
                ).epoch_nanoseconds,
            ),
        )
        self.assertTrue(repository.acquire_lease(
            "worker-a", "2026-07-26T00:00:00.01Z",
            "2026-07-26T00:00:00.100002Z",
        ))

        self.assertFalse(repository.acquire_lease(
            "worker-b", "2026-07-26T00:00:00.1Z",
            "2026-07-26T00:00:01Z",
        ))
        self.assertIsNone(repository.claim_next(
            "worker-a", "2026-07-26T00:00:00.1Z",
            "2026-07-26T00:00:00.100002Z",
        ))
        self.assertTrue(repository.acquire_lease(
            "worker-b", "2026-07-26T00:00:00.100002Z",
            "2026-07-26T00:00:01Z",
        ))
        self.assertIsNotNone(repository.claim_next(
            "worker-b", "2026-07-26T00:00:00.100002Z",
            "2026-07-26T00:00:01Z",
        ))

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

    def test_quarantine_claim_discards_a_concurrent_requeue(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/rejected.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions",
            source_locator=locator,
            observed_at="2026-07-26T00:00:00Z",
        )
        self.assertTrue(repository.acquire_lease(
            "worker", "2026-07-26T00:00:00Z", "2026-07-26T00:10:00Z",
        ))
        claimed = repository.claim_next(
            "worker", "2026-07-26T00:00:01Z", "2026-07-26T00:01:00Z",
        )
        self.assertEqual(claimed.source_locator, locator)
        self.assertFalse(repository.register_and_enqueue(
            root_kind="sessions",
            source_locator=locator,
            observed_at="2026-07-26T00:00:02Z",
        ))

        self.assertTrue(repository.quarantine_claim(
            "worker", "sessions", locator, "2026-07-26T00:00:03Z",
        ))

        self.assertEqual(repository.list_queue(), ())
        self.assertEqual(
            repository.source_for("sessions", locator).source_state,
            "repair_required",
        )

    def test_stale_owner_cannot_quarantine_a_successor_claim(self) -> None:
        repository = self._repository()
        locator = "2026/07/26/successor.jsonl"
        repository.register_and_enqueue(
            root_kind="sessions",
            source_locator=locator,
            observed_at="2026-07-26T00:00:00Z",
        )
        self.assertTrue(repository.acquire_lease(
            "worker-a", "2026-07-26T00:00:00Z", "2026-07-26T00:00:02Z",
        ))
        first = repository.claim_next(
            "worker-a", "2026-07-26T00:00:01Z", "2026-07-26T00:00:02Z",
        )
        self.assertEqual(first.source_locator, locator)
        self.assertTrue(repository.acquire_lease(
            "worker-b", "2026-07-26T00:00:03Z", "2026-07-26T00:10:00Z",
        ))
        successor = repository.claim_next(
            "worker-b", "2026-07-26T00:00:03Z", "2026-07-26T00:01:00Z",
        )
        self.assertEqual(successor.source_locator, locator)

        self.assertFalse(repository.quarantine_claim(
            "worker-a", "sessions", locator, "2026-07-26T00:00:04Z",
        ))

        self.assertEqual(
            repository.source_for("sessions", locator).source_state,
            "ready",
        )
        queue = repository.list_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].queue_state, "claimed")
        self.assertEqual(
            self.store.connection.execute(
                """SELECT claimed_by FROM sync_ingest_queue
                    WHERE root_kind='sessions' AND source_locator=?""",
                (locator,),
            ).fetchone()[0],
            "worker-b",
        )

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

        job_id = repository.create_job(
            "sync", "2026-07-26T00:00:00.123450Z",
        )
        self.assertEqual(
            repository.get_job(job_id).created_at,
            "2026-07-26T00:00:00.12345Z",
        )

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
        self.assertEqual(
            recovered.acknowledge_dirty_roots(
                "crashed", claimed_dirty, "2026-07-26T00:00:11Z",
            ),
            0,
        )
        self.assertEqual(recovered.acknowledge_dirty_roots(
            "recovered", recovered_dirty, "2026-07-26T00:00:11Z",
        ), 1)
        self.assertEqual(recovered.list_dirty_roots(), ())

    def test_stale_same_owner_dirty_ack_cannot_delete_successor_claim(self) -> None:
        repository = self._repository()
        repository.mark_dirty(
            "hprj_safe", "task-safe", "task", "2026-07-26T00:00:00Z",
        )
        self.assertTrue(repository.acquire_lease(
            "reused-owner", "2026-07-26T00:00:00Z",
            "2026-07-26T00:00:10Z",
        ))
        stale = repository.claim_dirty_roots(
            "reused-owner", "2026-07-26T00:00:00Z",
            "2026-07-26T00:00:10Z", 10,
        )

        self.assertTrue(repository.acquire_lease(
            "reused-owner", "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z",
        ))
        successor = repository.claim_dirty_roots(
            "reused-owner", "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z", 10,
        )

        self.assertEqual(
            repository.acknowledge_dirty_roots(
                "reused-owner", stale, "2026-07-26T00:00:11Z",
            ),
            0,
        )
        self.assertEqual(
            [root.root_key for root in repository.list_dirty_roots()],
            ["task-safe"],
        )
        self.assertEqual(
            repository.acknowledge_dirty_roots(
                "reused-owner", successor, "2026-07-26T00:00:11Z",
            ),
            1,
        )

    def test_dirty_mutation_preserves_live_claim_and_cannot_be_acknowledged_away(
        self,
    ) -> None:
        repository = self._repository()
        repository.mark_dirty(
            "hprj_safe", "hprj_safe", "project",
            "2026-07-26T00:00:00Z",
        )
        self.assertTrue(repository.acquire_lease(
            "worker", "2026-07-26T00:00:00Z",
            "2026-07-26T00:00:10Z",
        ))
        claimed = repository.claim_dirty_roots(
            "worker", "2026-07-26T00:00:01Z",
            "2026-07-26T00:00:10Z", 10,
        )
        self.assertEqual(len(claimed), 1)

        with self.store.rollout_transaction() as connection:
            repository.mark_dirty_in_transaction(
                connection,
                "hprj_safe",
                "hprj_safe",
                "project",
                "2026-07-26T00:00:02Z",
            )

        row = self.store.connection.execute(
            """SELECT observed_at,claim_owner,claim_expires_at,
                      claim_token,eligible_epoch_ns
                 FROM sync_dirty_roots
                WHERE project_id='hprj_safe'
                  AND root_key='hprj_safe'
                  AND root_kind='project'"""
        ).fetchone()
        self.assertEqual(
            tuple(row[:4]),
            (
                "2026-07-26T00:00:02Z",
                "worker",
                "2026-07-26T00:00:10Z",
                claimed[0].claim_token,
            ),
        )
        self.assertEqual(
            row[4],
            require_exact_timestamp(
                "2026-07-26T00:00:10Z",
                "test dirty claim expiry",
            ).epoch_nanoseconds,
        )
        self.assertFalse(repository.renew_dirty_claims(
            "worker",
            claimed,
            "2026-07-26T00:00:03Z",
            "2026-07-26T00:00:10Z",
        ))
        self.assertEqual(
            repository.acknowledge_dirty_roots(
                "worker", claimed, "2026-07-26T00:00:03Z",
            ),
            0,
        )

        self.assertTrue(repository.acquire_lease(
            "successor", "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z",
        ))
        successor = repository.claim_dirty_roots(
            "successor", "2026-07-26T00:00:10Z",
            "2026-07-26T00:00:20Z", 10,
        )
        self.assertEqual(
            [(root.project_id, root.observed_at) for root in successor],
            [("hprj_safe", "2026-07-26T00:00:02Z")],
        )
        self.assertEqual(
            repository.acknowledge_dirty_roots(
                "successor", successor, "2026-07-26T00:00:11Z",
            ),
            1,
        )

    def test_same_instant_dirty_generation_is_immediately_eligible(
        self,
    ) -> None:
        repository = self._repository()
        observed_at = "2026-07-26T00:00:00.0000001Z"
        repository.mark_dirty(
            "hprj_safe",
            "hprj_safe",
            "project",
            observed_at,
        )

        with self.store.rollout_transaction() as connection:
            repository.mark_dirty_in_transaction(
                connection,
                "hprj_safe",
                "hprj_safe",
                "project",
                observed_at,
            )

        row = self.store.connection.execute(
            """SELECT observed_at,eligible_epoch_ns
                 FROM sync_dirty_roots
                WHERE project_id='hprj_safe'
                  AND root_key='hprj_safe'
                  AND root_kind='project'"""
        ).fetchone()
        self.assertEqual(row[0], observed_at)
        self.assertEqual(
            row[1],
            require_exact_timestamp(
                observed_at,
                "test dirty eligibility",
            ).epoch_nanoseconds,
        )
        self.assertTrue(repository.acquire_lease(
            "worker",
            observed_at,
            "2026-07-26T00:00:10Z",
        ))
        claimed = repository.claim_dirty_roots(
            "worker",
            observed_at,
            "2026-07-26T00:00:10Z",
            10,
        )
        self.assertEqual(len(claimed), 1)

    def test_same_instant_dirty_mutation_advances_a_live_claim_generation(
        self,
    ) -> None:
        repository = self._repository()
        observed_at = "2026-07-26T00:00:00.0000001Z"
        repository.mark_dirty(
            "hprj_safe",
            "hprj_safe",
            "project",
            observed_at,
        )
        self.assertTrue(repository.acquire_lease(
            "worker",
            observed_at,
            "2026-07-26T00:00:10Z",
        ))
        claimed = repository.claim_dirty_roots(
            "worker",
            observed_at,
            "2026-07-26T00:00:10Z",
            10,
        )
        self.assertEqual(len(claimed), 1)

        with self.store.rollout_transaction() as connection:
            repository.mark_dirty_in_transaction(
                connection,
                "hprj_safe",
                "hprj_safe",
                "project",
                observed_at,
            )

        row = self.store.connection.execute(
            """SELECT observed_at,claim_owner,claim_expires_at,claim_token,
                      eligible_epoch_ns
                 FROM sync_dirty_roots
                WHERE project_id='hprj_safe'
                  AND root_key='hprj_safe'
                  AND root_kind='project'"""
        ).fetchone()
        self.assertEqual(row[0], "2026-07-26T00:00:00.000001Z")
        self.assertEqual(
            tuple(row[1:4]),
            ("worker", "2026-07-26T00:00:10Z", claimed[0].claim_token),
        )
        self.assertEqual(
            row[4],
            require_exact_timestamp(
                "2026-07-26T00:00:10Z",
                "test dirty claim expiry",
            ).epoch_nanoseconds,
        )
        self.assertEqual(
            repository.acknowledge_dirty_roots(
                "worker",
                claimed,
                "2026-07-26T00:00:01Z",
            ),
            0,
        )

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
            reopened.connection.execute(
                "SELECT revision FROM sync_data_revision WHERE singleton=1"
            ).fetchone()[0],
            persisted_revision,
        )

    def test_job_progress_and_terminal_state_cannot_regress(self) -> None:
        repository = self._repository()
        job_id = repository.create_job("sync", "2026-07-26T00:00:00Z")
        repository.update_job(
            job_id, state="running", sources_discovered=4, sources_completed=2,
            bytes_processed=128, updated_at="2026-07-26T00:00:01Z",
        )

        for counters in ((3, 2, 128), (4, 1, 128), (4, 2, 127)):
            with self.subTest(counters=counters):
                with self.assertRaisesRegex(ValueError, "regress"):
                    repository.update_job(
                        job_id, state="running",
                        sources_discovered=counters[0],
                        sources_completed=counters[1],
                        bytes_processed=counters[2],
                        updated_at="2026-07-26T00:00:02Z",
                    )

        with self.assertRaisesRegex(ValueError, "transition"):
            repository.update_job(
                job_id, state="queued", sources_discovered=4, sources_completed=2,
                bytes_processed=128, updated_at="2026-07-26T00:00:02Z",
            )

        repository.update_job(
            job_id, state="succeeded", sources_discovered=4, sources_completed=4,
            bytes_processed=256, updated_at="2026-07-26T00:00:03Z",
        )
        with self.assertRaisesRegex(ValueError, "terminal"):
            repository.update_job(
                job_id, state="running", sources_discovered=4, sources_completed=4,
                bytes_processed=256, updated_at="2026-07-26T00:00:04Z",
            )
        with self.assertRaisesRegex(ValueError, "terminal"):
            repository.update_job(
                job_id, state="succeeded", sources_discovered=5, sources_completed=5,
                bytes_processed=512, updated_at="2026-07-26T00:00:04Z",
            )

    def test_lease_owned_job_floor_and_completion_keep_nanosecond_precision(
        self,
    ) -> None:
        repository = self._repository()
        job_id = repository.create_job(
            "backfill",
            "2026-07-26T00:00:00Z",
        )
        owner = "nanosecond-owner"
        self.assertTrue(repository.acquire_lease(
            owner,
            "2026-07-26T00:00:00Z",
            "2026-07-26T00:00:02Z",
        ))
        newer = "2026-07-26T00:00:01.000000002Z"
        older = "2026-07-26T00:00:01.000000001Z"
        repository.update_job(
            job_id,
            state="running",
            sources_discovered=0,
            sources_completed=0,
            bytes_processed=0,
            updated_at=newer,
        )

        terminal = repository.refresh_job_from_frontier_if_owned(
            job_id,
            owner_key=owner,
            lease_observed_at=older,
            state="succeeded",
            updated_at=older,
            completed_at=older,
        )

        self.assertIsNotNone(terminal)
        self.assertEqual(terminal.state, "succeeded")
        self.assertEqual(terminal.updated_at, newer)
        self.assertEqual(terminal.completed_at, newer)
        row = self.store.connection.execute(
            """SELECT updated_at,completed_at,updated_epoch_ns
                 FROM sync_jobs WHERE job_id=?""",
            (job_id,),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                newer,
                newer,
                require_exact_timestamp(newer).epoch_nanoseconds,
            ),
        )

    def test_job_batch_progress_is_additive_across_worker_lease_handoff(self) -> None:
        repository = self._repository()
        second_store = HydraStore(self.database)
        self.addCleanup(second_store.close)
        from hydra_codex.sync_state import SyncStateRepository
        second = SyncStateRepository(second_store)
        job_id = repository.create_job("sync", "2026-07-26T00:00:00Z")
        repository.update_job(
            job_id, state="running", sources_discovered=2, sources_completed=0,
            bytes_processed=0, updated_at="2026-07-26T00:00:00.01Z",
        )

        first = repository.advance_job(
            job_id, sources_completed_delta=1, bytes_processed_delta=100,
            repair_required_delta=0, remaining_sources=1,
            updated_at="2026-07-26T00:00:00.1Z",
        )
        final = second.advance_job(
            job_id, sources_completed_delta=1, bytes_processed_delta=250,
            repair_required_delta=0, remaining_sources=0,
            updated_at="2026-07-26T00:00:00.11Z",
        )

        self.assertEqual(
            (first.sources_completed, first.bytes_processed), (1, 100),
        )
        self.assertEqual(
            (final.sources_discovered, final.sources_completed, final.bytes_processed),
            (2, 2, 350),
        )

    def test_active_job_reuse_is_scoped_to_the_requested_kind(self) -> None:
        repository = self._repository()

        sync_id, sync_reused = repository.get_or_create_active_job(
            "sync", "2026-07-26T00:00:00Z",
        )
        repair_id, repair_reused = repository.get_or_create_active_job(
            "repair", "2026-07-26T00:00:01Z",
        )
        second_sync, second_sync_reused = repository.get_or_create_active_job(
            "sync", "2026-07-26T00:00:02Z",
        )
        second_repair, second_repair_reused = repository.get_or_create_active_job(
            "repair", "2026-07-26T00:00:03Z",
        )

        self.assertFalse(sync_reused)
        self.assertFalse(repair_reused)
        self.assertNotEqual(sync_id, repair_id)
        self.assertEqual((second_sync, second_sync_reused), (sync_id, True))
        self.assertEqual((second_repair, second_repair_reused), (repair_id, True))

    def test_terminal_job_transition_waits_for_and_observes_concurrent_enqueue(self) -> None:
        repository = self._repository()
        repository.register_source(
            root_kind="sessions", source_locator="concurrent.jsonl",
            observed_at="2026-07-26T00:00:00Z",
        )
        job_id = repository.create_job("sync", "2026-07-26T00:00:00Z")
        repository.update_job(
            job_id, state="running", sources_discovered=0,
            sources_completed=0, bytes_processed=0,
            updated_at="2026-07-26T00:00:00Z",
        )
        self.assertTrue(
            hasattr(repository, "finish_job_if_idle"),
            "terminal transition must share one transaction with the work check",
        )

        queued_but_uncommitted = threading.Event()
        allow_enqueue_commit = threading.Event()
        finish_started = threading.Event()
        finish_result: list[object] = []

        def enqueue_while_finisher_waits() -> None:
            writer = HydraStore(self.database)
            try:
                writer_repository = type(repository)(writer)
                with writer.rollout_transaction() as connection:
                    connection.execute(
                        """INSERT INTO sync_ingest_queue(
                               root_kind,source_locator,queue_state,enqueued_at,
                               available_at,claimed_by,claimed_at,claim_expires_at,
                               requeue_pending,attempts,reason_code,
                               eligible_epoch_ns)
                           VALUES ('sessions','concurrent.jsonl','queued',
                                   '2026-07-26T00:00:01Z','2026-07-26T00:00:01Z',
                                   NULL,NULL,NULL,0,0,NULL,?)""",
                        (
                            require_exact_timestamp(
                                "2026-07-26T00:00:01Z",
                                "test queue eligibility",
                            ).epoch_nanoseconds,
                        ),
                    )
                    writer_repository._bump_revision(
                        connection, "2026-07-26T00:00:01Z",
                    )
                    queued_but_uncommitted.set()
                    allow_enqueue_commit.wait(2)
            finally:
                writer.close()

        def finish_after_enqueue_started() -> None:
            finisher = HydraStore(self.database)
            try:
                finish_started.set()
                finish_result.append(type(repository)(finisher).finish_job_if_idle(
                    job_id, updated_at="2026-07-26T00:00:02Z",
                ))
            except BaseException as error:
                finish_result.append(error)
            finally:
                finisher.close()

        writer_thread = threading.Thread(target=enqueue_while_finisher_waits)
        finish_thread = threading.Thread(target=finish_after_enqueue_started)
        writer_thread.start()
        self.assertTrue(queued_but_uncommitted.wait(1))
        finish_thread.start()
        self.assertTrue(finish_started.wait(1))
        time.sleep(0.05)
        self.assertTrue(finish_thread.is_alive())
        allow_enqueue_commit.set()
        writer_thread.join(2)
        finish_thread.join(2)

        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(finish_thread.is_alive())
        self.assertEqual(finish_result, [None])
        self.assertEqual(repository.get_job(job_id).state, "running")
        self.assertEqual(repository.queue_count(), 1)

    def test_active_job_ordering_uses_chronological_rfc3339_instants(self) -> None:
        repository = self._repository()
        older = repository.create_job(
            "sync", "2026-07-26T10:00:00Z",
            job_id="sync_00000000000000000000000000000001",
        )
        newer = repository.create_job(
            "sync", "2026-07-26T10:00:00.1Z",
            job_id="sync_00000000000000000000000000000002",
        )

        self.assertEqual(repository.current_job("sync").job_id, newer)
        self.assertEqual(repository.list_jobs()[0].job_id, newer)
        repair, reused = repository.get_or_create_active_job(
            "repair", "2026-07-26T10:00:01Z",
        )
        self.assertFalse(reused)
        self.assertNotEqual(repair, newer)
        self.assertEqual(repository.get_job(repair).job_kind, "repair")
        self.assertNotEqual(older, newer)

    def test_latest_job_queries_use_exact_epoch_indexes_and_constant_limits(self) -> None:
        repository = self._repository()
        with self.store.rollout_transaction() as connection:
            connection.executemany(
                """INSERT INTO sync_jobs(
                       job_id,job_kind,state,sources_discovered,
                       sources_completed,bytes_processed,created_at,
                       updated_at,completed_at,updated_epoch_ns)
                   VALUES (?,'sync','succeeded',0,0,0,?,?,?,?)""",
                (
                    (
                        f"sync_{index + 1_000:032x}",
                        "2026-07-25T10:00:00Z",
                        "2026-07-25T10:00:00Z",
                        "2026-07-25T10:00:00Z",
                        require_exact_timestamp(
                            "2026-07-25T10:00:00Z",
                        ).epoch_nanoseconds,
                    )
                    for index in range(1, 501)
                ),
            )
        older = repository.create_job(
            "sync", "2026-07-26T10:00:00Z",
            job_id="sync_00000000000000000000000000000011",
        )
        newest_terminal = repository.create_job(
            "repair", "2026-07-26T10:00:00.100000Z",
            job_id="sync_00000000000000000000000000000012",
        )
        repository.update_job(
            newest_terminal, state="succeeded",
            sources_discovered=0, sources_completed=0, bytes_processed=0,
            updated_at="2026-07-26T10:00:00.100000Z",
        )
        active = repository.create_job(
            "backfill", "2026-07-26T10:00:00.000001Z",
            job_id="sync_00000000000000000000000000000013",
        )

        self.assertEqual(repository.latest_job().job_id, newest_terminal)
        self.assertEqual(repository.latest_active_job().job_id, active)
        self.assertEqual(repository.current_job("sync").job_id, older)
        epochs = tuple(self.store.connection.execute(
            "SELECT updated_epoch_ns FROM sync_jobs ORDER BY job_id",
        ))
        self.assertTrue(all(
            isinstance(row[0], int) for row in epochs
        ))
        plans = {
            "latest": "\n".join(
                str(row[3]) for row in self.store.connection.execute(
                    """EXPLAIN QUERY PLAN
                       SELECT job_id FROM sync_jobs
                        ORDER BY updated_epoch_ns DESC,job_id DESC LIMIT 1""",
                )
            ),
            "active": "\n".join(
                str(row[3]) for row in self.store.connection.execute(
                    """EXPLAIN QUERY PLAN
                       SELECT job_id FROM sync_jobs
                        WHERE state IN ('queued','running')
                        ORDER BY updated_epoch_ns DESC,job_id DESC LIMIT 1""",
                )
            ),
        }
        self.assertIn("sync_jobs_updated_epoch", plans["latest"])
        self.assertIn("sync_jobs_active_updated_epoch", plans["active"])
        self.assertNotIn("TEMP B-TREE", repr(plans))

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
        self.assertEqual(upgraded.schema_version(), MIGRATIONS[-1][0])
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
