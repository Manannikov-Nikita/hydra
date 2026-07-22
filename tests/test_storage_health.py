from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hydra_codex.storage import HydraStore
from hydra_codex.storage import StorageUnavailable
from hydra_codex.migrations_t20 import T20_TRIGGER_STATEMENTS
from hydra_codex.storage_health import (
    compact_storage,
    current_storage_health,
    record_audit_snapshot,
    render_storage_compaction,
    render_storage_status,
    storage_status,
)


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


class StorageSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)
        self.project_id = "hprj_storage_tests"

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_snapshots_are_append_only_and_identical_audit_is_deduplicated(self) -> None:
        health = current_storage_health(self.store, self.project_id)

        inserted = record_audit_snapshot(
            self.store,
            project_id=self.project_id,
            audit_sha256="a" * 64,
            observed_at=NOW,
            health=health,
        )
        duplicate = record_audit_snapshot(
            self.store,
            project_id=self.project_id,
            audit_sha256="a" * 64,
            observed_at=NOW,
            health=health,
        )

        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM storage_audit_snapshots"
        ).fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE storage_audit_snapshots SET database_bytes=0"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM storage_audit_snapshots")

    def test_status_is_public_and_marks_missing_growth_baseline(self) -> None:
        status = storage_status(self.store, self.project_id)
        payload = status.as_dict()

        self.assertEqual(payload["schema_version"], "hydra.storage-status/v1")
        self.assertIsNone(payload["baseline"])
        self.assertIsNone(payload["growth"])
        self.assertEqual(payload["diagnostics"], [
            {"code": "growth_baseline_unavailable", "severity": "info"},
        ])
        rendered = render_storage_status(status, "json")
        self.assertEqual(json.loads(rendered), payload)
        self.assertIn("Growth baseline unavailable", render_storage_status(status, "markdown"))
        for private in (self.project_id, str(self.database), "audit_sha256", "snapshot_id"):
            self.assertNotIn(private, rendered)

    def test_status_reports_signed_growth_from_latest_snapshot(self) -> None:
        baseline = current_storage_health(self.store, self.project_id)
        record_audit_snapshot(
            self.store,
            project_id=self.project_id,
            audit_sha256="b" * 64,
            observed_at=NOW,
            health=baseline,
        )
        self.store.connection.execute(
            """INSERT INTO sessions(
                   session_id,project_id,worktree_path,started_at,provenance)
               VALUES ('session-storage',?,'relative','2026-07-22T12:00:01Z','exact')""",
            (self.project_id,),
        )
        self.store.connection.commit()

        payload = storage_status(self.store, self.project_id).as_dict()

        self.assertEqual(payload["baseline_state"], "available")
        self.assertIsNotNone(payload["baseline"])
        self.assertEqual(payload["diagnostics"], [])
        for fact, value in payload["growth"].items():
            self.assertEqual(
                value,
                payload["current"][fact] - payload["baseline"][fact],
            )

    def test_altered_snapshot_uniqueness_constraints_fail_startup(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.database)
        for trigger in (
            "storage_audit_snapshots_immutable_update",
            "storage_audit_snapshots_immutable_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE storage_audit_snapshots")
        connection.execute(
            """CREATE TABLE storage_audit_snapshots (
                   snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                   observed_at TEXT NOT NULL, audit_sha256 TEXT NOT NULL,
                   database_bytes INTEGER NOT NULL, wal_bytes INTEGER NOT NULL,
                   rollout_sources INTEGER NOT NULL, rollout_events INTEGER NOT NULL,
                   codex_event_sources INTEGER NOT NULL, codex_events INTEGER NOT NULL,
                   schema_version INTEGER NOT NULL
               ) WITHOUT ROWID"""
        )
        for statement in T20_TRIGGER_STATEMENTS:
            connection.execute(statement)
        connection.commit()
        connection.close()

        with self.assertRaises(StorageUnavailable):
            HydraStore(self.database)
        self.store = HydraStore.__new__(HydraStore)

    def test_latest_snapshot_order_is_chronological_within_one_second(self) -> None:
        health = current_storage_health(self.store, self.project_id)
        record_audit_snapshot(
            self.store,
            project_id=self.project_id,
            audit_sha256="d" * 64,
            observed_at=NOW,
            health=replace(health, database_bytes=111),
        )
        record_audit_snapshot(
            self.store,
            project_id=self.project_id,
            audit_sha256="e" * 64,
            observed_at=NOW + timedelta(microseconds=500_000),
            health=replace(health, database_bytes=222),
        )

        payload = storage_status(self.store, self.project_id).as_dict()

        self.assertEqual(payload["baseline"]["database_bytes"], 222)


class StorageCompactionTests(unittest.TestCase):
    def test_compaction_checkpoints_and_vacuums_without_deleting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "hydra.sqlite3"
            store = HydraStore(database)
            try:
                project_id = "hprj_compaction"
                store.connection.execute(
                    """INSERT INTO sessions(
                           session_id,project_id,worktree_path,started_at,provenance)
                       VALUES ('session-compact',?,'relative',
                               '2026-07-22T12:00:00Z','exact')""",
                    (project_id,),
                )
                store.connection.commit()
                record_audit_snapshot(
                    store,
                    project_id=project_id,
                    audit_sha256="c" * 64,
                    observed_at=NOW,
                    health=current_storage_health(store, project_id),
                )
                before = {
                    str(row[0]): int(store.connection.execute(
                        f'SELECT COUNT(*) FROM "{str(row[0])}"'
                    ).fetchone()[0])
                    for row in store.connection.execute(
                        """SELECT name FROM sqlite_master
                            WHERE type='table' AND name NOT LIKE 'sqlite_%'
                            ORDER BY name"""
                    ).fetchall()
                }

                result = compact_storage(store)
                rendered = render_storage_compaction(result, "json")
                after = {
                    str(row[0]): int(store.connection.execute(
                        f'SELECT COUNT(*) FROM "{str(row[0])}"'
                    ).fetchone()[0])
                    for row in store.connection.execute(
                        """SELECT name FROM sqlite_master
                            WHERE type='table' AND name NOT LIKE 'sqlite_%'
                            ORDER BY name"""
                    ).fetchall()
                }
            finally:
                store.close()

        self.assertEqual(before, after)
        self.assertTrue(result.rows_preserved)
        self.assertEqual(result.audit_snapshots, 1)
        self.assertEqual(json.loads(rendered)["schema_version"], "hydra.storage-compact/v1")
        self.assertNotIn(str(database), rendered)


if __name__ == "__main__":
    unittest.main()
