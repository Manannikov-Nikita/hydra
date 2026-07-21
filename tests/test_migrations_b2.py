from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydra_codex.storage import MIGRATIONS, V2_TRIGGER_STATEMENTS, HydraStore, StorageUnavailable


def seed_rollout_rows(connection: sqlite3.Connection, version: int) -> None:
    if version < 3:
        return
    session_columns = {row[1] for row in connection.execute("PRAGMA table_info(rollout_sessions)")}
    session_values = {
        "session_key": "legacy-rollout-session", "project_id": "preserved-project",
        "path_key": "safe", "resume_segments": 1, "conversation_key": "legacy-conversation",
        "started_at": "2026-07-21T00:00:00Z", "last_activity_at": "2026-07-21T00:00:01Z",
    }
    chosen = [name for name in session_values if name in session_columns]
    connection.execute(
        f"INSERT INTO rollout_sessions({','.join(chosen)}) VALUES ({','.join('?' for _ in chosen)})",
        tuple(session_values[name] for name in chosen),
    )
    if version >= 14:
        connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
               VALUES ('legacy-logical','preserved-project','legacy-rollout-session',NULL,'clean')"""
        )
        connection.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key,relation,line_count,byte_count,chain_digest,materialized)
               VALUES ('legacy-source','jsonl','legacy-logical','initial',1,1,'chain',1)"""
        )
        connection.execute(
            """INSERT INTO rollout_source_locations(
                   logical_source_key,location_key,location_type,revision_digest)
               VALUES ('legacy-logical','legacy-location','explicit','legacy-source')"""
        )
    else:
        connection.execute("INSERT INTO rollout_sources VALUES ('legacy-source','jsonl')")
        connection.execute(
            "INSERT INTO rollout_source_locations VALUES ('legacy-source','legacy-location','active')"
        )
    token_columns = {row[1] for row in connection.execute("PRAGMA table_info(token_snapshots)")}
    token_values = {
        "source_digest": "legacy-source", "line_number": 1,
        "session_key": "legacy-rollout-session", "project_id": "preserved-project", "epoch": 0,
        "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3,
        "reasoning_tokens": 1, "cache_write_tokens": 0, "vendor_total": 13,
        "context_window": 100, "completeness": "complete", "turn_key": "legacy-turn",
        "observed_at": "2026-07-21T00:00:01Z",
    }
    chosen = [name for name in token_values if name in token_columns]
    connection.execute(
        f"INSERT INTO token_snapshots({','.join(chosen)}) VALUES ({','.join('?' for _ in chosen)})",
        tuple(token_values[name] for name in chosen),
    )
    attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(turn_attempts)")}
    attempt_values = {
        "session_key": "legacy-rollout-session", "turn_key": "legacy-turn", "attempt_ordinal": 1,
        "state": "completed", "emitted_duration_ms": 10, "wall_duration_ms": 10,
        "started_at": "2026-07-21T00:00:00Z", "finished_at": "2026-07-21T00:00:00.010000Z",
        "timing_provenance": "derived",
    }
    chosen = [name for name in attempt_values if name in attempt_columns]
    connection.execute(
        f"INSERT INTO turn_attempts({','.join(chosen)}) VALUES ({','.join('?' for _ in chosen)})",
        tuple(attempt_values[name] for name in chosen),
    )
    if version >= 4:
        baseline_columns = {row[1] for row in connection.execute("PRAGMA table_info(fork_baselines)")}
        baseline_values = {
            "child_key": "legacy-rollout-session", "source_digest": "legacy-source", "line_number": 1,
            "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3,
            "reasoning_tokens": 1, "cache_write_tokens": 0, "provenance": "exact",
            "observed_at": "2026-07-21T00:00:01Z",
        }
        chosen = [name for name in baseline_values if name in baseline_columns]
        connection.execute(
            f"INSERT INTO fork_baselines({','.join(chosen)}) VALUES ({','.join('?' for _ in chosen)})",
            tuple(baseline_values[name] for name in chosen),
        )
    if version >= 12:
        connection.execute(
            """INSERT INTO tool_spans(session_key,call_key,category,terminal_state,latency_ms,tool_name,
                   completeness,provenance) VALUES ('legacy-rollout-session','legacy-call','tool','success',1,
                   'function','complete','exact')"""
        )
    if version >= 13:
        connection.execute(
            """INSERT INTO rollout_test_runs(
                   evidence_key,source_digest,line_number,session_key,tool_call_key,command_hash,runner,
                   scope,outcome,failure_cause,provenance,completeness)
               VALUES ('legacy-evidence','legacy-source',2,'legacy-rollout-session','legacy-call',
                       'command','pytest','targeted','success','none','exact','complete')"""
        )


def build_schema(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    for migration, statements in MIGRATIONS:
        if migration > version:
            break
        for statement in statements:
            connection.execute(statement)
        if migration == 2:
            for statement in V2_TRIGGER_STATEMENTS:
                connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version,applied_at) VALUES (?,'2026-07-21T00:00:00Z')",
            (migration,),
        )
        connection.execute(f"PRAGMA user_version={migration}")
    if version >= 1:
        connection.execute(
            """INSERT INTO sessions(session_id,project_id,worktree_path,started_at,provenance)
               VALUES ('preserved-session','preserved-project','safe','2026-07-21T00:00:00Z','exact')"""
        )
    seed_rollout_rows(connection, version)
    connection.commit()
    connection.close()


class MigrationMatrixB2Tests(unittest.TestCase):
    def test_every_prior_schema_migrates_and_preserves_rows(self) -> None:
        latest = MIGRATIONS[-1][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in range(latest):
                with self.subTest(version=version):
                    database = root / f"v{version}.sqlite3"
                    build_schema(database, version)
                    store = HydraStore(database)
                    try:
                        self.assertEqual(store.schema_version(), latest)
                        self.assertEqual(store.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        self.assertIsNone(store.connection.execute("PRAGMA foreign_key_check").fetchone())
                        if version >= 1:
                            self.assertEqual(store.connection.execute(
                                "SELECT project_id FROM sessions WHERE session_id='preserved-session'"
                            ).fetchone()[0], "preserved-project")
                        if version >= 3:
                            self.assertEqual(store.connection.execute(
                                "SELECT input_tokens FROM token_snapshots WHERE source_digest='legacy-source'"
                            ).fetchone()[0], 10)
                            self.assertEqual(store.connection.execute(
                                "SELECT state FROM turn_attempts WHERE turn_key='legacy-turn'"
                            ).fetchone()[0], "completed")
                        if version >= 4:
                            self.assertEqual(store.connection.execute(
                                "SELECT provenance FROM fork_baselines WHERE child_key='legacy-rollout-session'"
                            ).fetchone()[0], "exact")
                        if version >= 12:
                            self.assertEqual(store.connection.execute(
                                "SELECT terminal_state FROM tool_spans WHERE call_key='legacy-call'"
                            ).fetchone()[0], "success")
                            self.assertEqual(store.connection.execute(
                                """SELECT COUNT(*) FROM tool_span_candidates
                                     WHERE call_key='legacy-call'"""
                            ).fetchone()[0], 2)
                            self.assertEqual(store.connection.execute(
                                """SELECT terminal_state FROM tool_span_candidates
                                     WHERE call_key='legacy-call'
                                       AND candidate_kind='legacy_values'"""
                            ).fetchone()[0], "success")
                        if version >= 13:
                            self.assertEqual(store.connection.execute(
                                "SELECT outcome FROM rollout_test_runs WHERE evidence_key='legacy-evidence'"
                            ).fetchone()[0], "success")
                    finally:
                        store.close()

    def test_future_or_structurally_drifted_schema_fails_closed(self) -> None:
        latest = MIGRATIONS[-1][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            future = root / "future.sqlite3"
            connection = sqlite3.connect(future)
            connection.execute(f"PRAGMA user_version={latest + 1}")
            connection.close()
            with self.assertRaises(StorageUnavailable):
                HydraStore(future)

            drifted = root / "drifted.sqlite3"
            store = HydraStore(drifted)
            store.close()
            connection = sqlite3.connect(drifted)
            connection.execute("DROP TABLE rollout_events")
            connection.commit()
            connection.close()
            with self.assertRaises(StorageUnavailable):
                HydraStore(drifted)

    def test_tool_candidate_migration_normalizes_legacy_app_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "legacy-app-tool-status.sqlite3"
            build_schema(database, MIGRATIONS[-2][0])
            connection = sqlite3.connect(database)
            try:
                connection.executemany(
                    """INSERT INTO tool_spans(
                           session_key,call_key,category,terminal_state,latency_ms,
                           tool_name,completeness,provenance)
                       VALUES ('legacy-rollout-session',?,'tool',?,1,
                               'exec_command','complete',?)""",
                    (
                        ("legacy-completed", "completed", "exact"),
                        ("legacy-declined", "declined", "exact"),
                        ("legacy-estimated", "completed", "estimated"),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            store = HydraStore(database)
            try:
                rows = [tuple(row) for row in store.connection.execute(
                    """SELECT call_key,terminal_state FROM tool_span_candidates
                         WHERE candidate_kind='legacy_values'
                           AND call_key LIKE 'legacy-%'
                         ORDER BY call_key"""
                )]
                self.assertEqual(rows, [
                    ("legacy-call", "success"),
                    ("legacy-completed", "success"),
                    ("legacy-declined", "failed"),
                    ("legacy-estimated", "success"),
                ])
                self.assertEqual(store.connection.execute(
                    """SELECT provenance FROM tool_span_candidates
                         WHERE call_key='legacy-estimated'
                           AND candidate_kind='legacy_values'"""
                ).fetchone()[0], "estimated")
            finally:
                store.close()

    def test_interrupted_migration_rolls_back_and_clean_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "interrupted.sqlite3"
            build_schema(database, MIGRATIONS[-2][0])
            broken = MIGRATIONS[:-1] + ((MIGRATIONS[-1][0], (
                "CREATE TABLE interrupted_marker(value TEXT)", "THIS IS NOT SQL",
            )),)
            with patch("hydra_codex.storage.MIGRATIONS", broken):
                with self.assertRaises(StorageUnavailable):
                    HydraStore(database)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], MIGRATIONS[-2][0])
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interrupted_marker'"
                ).fetchone())
            finally:
                connection.close()
            store = HydraStore(database)
            try:
                self.assertEqual(store.schema_version(), MIGRATIONS[-1][0])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
