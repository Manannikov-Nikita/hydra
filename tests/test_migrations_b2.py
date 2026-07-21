from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hydra_codex.annotation_spool import _record_transport
from hydra_codex.metrics import aggregate_project, aggregate_project_facts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import MIGRATIONS, V2_TRIGGER_STATEMENTS, HydraStore, StorageUnavailable
from hydra_codex.task_tree_storage import aggregate_stored_task_tree
from hydra_codex.token_selection import refresh_token_source_selection


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
        if version >= 22:
            connection.executemany(
                """INSERT INTO tool_span_candidates(
                       session_key,call_key,source_digest,source_ordinal,
                       candidate_kind,category,terminal_state,latency_ms,
                       tool_name,started_at,finished_at,turn_key,provenance)
                   VALUES ('legacy-rollout-session','legacy-call','legacy-source',0,
                           ?,'tool',?,?, 'function',NULL,NULL,NULL,'exact')""",
                (
                    ("legacy_description", "unknown", None),
                    ("legacy_values", "success", 1),
                ),
            )
    if version >= 13:
        connection.execute(
            """INSERT INTO rollout_test_runs(
                   evidence_key,source_digest,line_number,session_key,tool_call_key,command_hash,runner,
                   scope,outcome,failure_cause,provenance,completeness)
               VALUES ('legacy-evidence','legacy-source',2,'legacy-rollout-session','legacy-call',
                       'command','pytest','targeted','success','none','exact','complete')"""
        )
        if version >= 24:
            connection.execute(
                """INSERT INTO test_evidence_candidates(
                       candidate_key,candidate_kind,evidence_key,source_digest,
                       line_number,session_key,tool_call_key,command_hash,runner,
                       scope,exit_status,outcome,failure_cause,provenance,completeness)
                   VALUES ('legacy:legacy-evidence','evidence','legacy-evidence',
                           'legacy-source',2,'legacy-rollout-session','legacy-call',
                           'command','pytest','targeted',0,'success','none','exact','complete')"""
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


def replace_empty_table(path: Path, table: str, create_statement: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"DROP TABLE {table}")
        connection.execute(create_statement)
        connection.commit()
    finally:
        connection.close()


class MigrationMatrixB2Tests(unittest.TestCase):
    def test_v30_accepted_unacknowledged_retry_uses_existing_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v30-accepted.sqlite3"
            build_schema(database, 30)
            keys = Pseudonymizer(b"v" * 32)
            turn_key = "turn-safe"
            request_digest = "hreq_v1_" + keys.digest("event", "accepted-request")
            transport_key = "htransport_v1_" + keys.digest(
                "event", f"{turn_key}/accepted/accepted/{request_digest}",
            )
            connection = sqlite3.connect(database)
            connection.execute(
                """INSERT INTO annotation_transport_events(
                       transport_key,project_id,session_key,turn_key,request_digest,
                       disposition,diagnostic_category,staged_at,staged_at_ns,
                       staged_order,received_at,latency_ms,provenance)
                   VALUES (?, 'hprj_safe', 'session-safe', ?, ?, 'accepted', NULL,
                           '2026-07-21T00:00:00Z', 1, '00000000000000000001:legacy.json',
                           '2026-07-21T00:00:01Z', 1000, 'derived')""",
                (transport_key, turn_key, request_digest),
            )
            connection.commit()
            connection.close()

            store = HydraStore(database)
            self.addCleanup(store.close)
            _record_transport(
                store,
                keys,
                project_id="hprj_safe",
                session_key="session-safe",
                turn_key=turn_key,
                request_digest=request_digest,
                disposition="accepted",
                category=None,
                staged_at_ns=2,
                staged_at="2026-07-21T00:00:02Z",
                staged_order="00000000000000000002:horder_v1_" + "a" * 64,
                received_at="2026-07-21T00:00:03Z",
                file_key="hspool_v1_" + "b" * 64,
            )

            self.assertEqual(store.schema_version(), 34)
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE disposition='accepted'"
            ).fetchone()[0], 1)

    def test_v30_transport_orders_are_sanitized_without_losing_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v30.sqlite3"
            build_schema(database, 30)
            sentinel = "PRIVATE-LEGACY-FILENAME-SENTINEL.json"
            connection = sqlite3.connect(database)
            connection.execute(
                """INSERT INTO annotation_transport_events(
                       transport_key,project_id,session_key,turn_key,request_digest,
                       disposition,diagnostic_category,staged_at,staged_at_ns,
                       staged_order,received_at,latency_ms,provenance)
                   VALUES ('htransport_v1_legacy','hprj_safe','session-safe','turn-safe',
                           NULL,'quarantined','malformed','2026-07-21T00:00:00Z',1,
                           ?,'2026-07-21T00:00:01Z',1000,'derived')""",
                (f"00000000000000000001:{sentinel}",),
            )
            connection.commit()
            connection.close()

            store = HydraStore(database)
            self.addCleanup(store.close)

            self.assertEqual(store.schema_version(), 34)
            row = store.connection.execute(
                "SELECT diagnostic_category,staged_order FROM annotation_transport_events"
            ).fetchone()
            self.assertEqual(row["diagnostic_category"], "malformed")
            self.assertNotIn(sentinel, row["staged_order"])
            self.assertNotIn(sentinel, "\n".join(store.connection.iterdump()))

    def test_v28_migration_adds_private_location_state_without_changing_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v28.sqlite3"
            build_schema(database, 28)

            store = HydraStore(database)
            self.addCleanup(store.close)

            self.assertEqual(store.schema_version(), 34)
            self.assertTrue({
                "transport_key", "disposition", "diagnostic_category",
                "staged_at_ns", "latency_ms",
            }.issubset({
                row[1] for row in store.connection.execute(
                    "PRAGMA table_info(annotation_transport_events)"
                )
            }))
            self.assertEqual(
                {
                    row[1] for row in store.connection.execute(
                        "PRAGMA table_info(rollout_source_location_states)"
                    )
                },
                {
                    "project_id", "location_key", "logical_source_key",
                    "revision_digest", "st_dev", "st_ino", "st_size",
                    "st_mtime_ns", "st_ctime_ns", "scanner_version",
                },
            )
            self.assertEqual(
                {
                    row[1]: row[5] for row in store.connection.execute(
                        "PRAGMA table_info(rollout_source_location_states)"
                    )
                    if row[5]
                },
                {"project_id": 1, "location_key": 2},
            )
            foreign_keys: dict[int, list[tuple[int, str, str, str]]] = {}
            for row in store.connection.execute(
                "PRAGMA foreign_key_list(rollout_source_location_states)"
            ):
                foreign_keys.setdefault(int(row[0]), []).append(
                    (int(row[1]), str(row[2]), str(row[3]), str(row[4]))
                )
            bindings = {
                tuple(
                    (table, source, target)
                    for _sequence, table, source, target in sorted(group)
                )
                for group in foreign_keys.values()
            }
            self.assertEqual(
                bindings,
                {
                    (("rollout_logical_sources", "logical_source_key", "logical_source_key"),),
                    (("rollout_sources", "revision_digest", "source_digest"),),
                    (
                        ("rollout_source_locations", "logical_source_key", "logical_source_key"),
                        ("rollout_source_locations", "location_key", "location_key"),
                    ),
                },
            )
            self.assertEqual(store.count("rollout_source_location_states"), 0)
            self.assertEqual(
                tuple(store.connection.execute(
                    "SELECT logical_source_key,location_key,revision_digest "
                    "FROM rollout_source_locations"
                ).fetchone()),
                ("legacy-logical", "legacy-location", "legacy-source"),
            )

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

    def test_immutable_candidate_trigger_drift_fails_closed(self) -> None:
        trigger_names = (
            "lineage_claim_candidates_no_update",
            "lineage_claim_candidates_no_delete",
            "test_evidence_candidates_no_update",
            "test_evidence_candidates_no_delete",
            "file_observation_candidates_no_update",
            "file_observation_candidates_no_delete",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for trigger_name in trigger_names:
                for drift_kind in ("missing", "tampered"):
                    with self.subTest(trigger=trigger_name, drift=drift_kind):
                        database = root / f"{trigger_name}-{drift_kind}.sqlite3"
                        store = HydraStore(database)
                        store.close()
                        connection = sqlite3.connect(database)
                        connection.execute(f"DROP TRIGGER {trigger_name}")
                        if drift_kind == "tampered":
                            table_name = trigger_name.removesuffix("_no_update").removesuffix("_no_delete")
                            action = "UPDATE" if trigger_name.endswith("_no_update") else "DELETE"
                            connection.execute(
                                f"""CREATE TRIGGER {trigger_name}
                                    BEFORE {action} ON {table_name}
                                    BEGIN SELECT 1; END"""
                            )
                        connection.commit()
                        connection.close()
                        with self.assertRaisesRegex(
                            StorageUnavailable, "immutable candidate trigger",
                        ):
                            HydraStore(database)

    def test_legacy_impossible_token_vectors_are_quarantined_before_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for carrier in ("token_snapshots", "fork_baselines"):
                with self.subTest(carrier=carrier):
                    database = root / f"invalid-{carrier}.sqlite3"
                    build_schema(database, 25)
                    connection = sqlite3.connect(database)
                    connection.execute(
                        f"UPDATE {carrier} SET input_tokens=10,cached_input_tokens=20"
                    )
                    connection.commit()
                    connection.close()

                    store = HydraStore(database)
                    try:
                        aggregate_stored_task_tree(
                            store.connection,
                            project_id="preserved-project",
                            root_id="legacy-rollout-session",
                            cutoff_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                        )
                        if carrier == "token_snapshots":
                            refresh_token_source_selection(
                                store.connection, "preserved-project",
                            )
                            row = store.connection.execute(
                                """SELECT vector_valid,contributes_total,selection_caveat
                                     FROM token_snapshots"""
                            ).fetchone()
                            self.assertEqual(tuple(row), (
                                0, 0, "invalid_legacy_token_vector",
                            ))
                            facts = aggregate_project_facts(
                                store.connection, "preserved-project",
                            )
                            metrics = aggregate_project(
                                store.connection, "preserved-project",
                            )
                            self.assertIsNone(facts["recorded_working"].value)
                            self.assertEqual(
                                facts["recorded_working"].known_lower_bound, 0,
                            )
                            self.assertEqual(
                                facts["recorded_working"].provenance, "estimated",
                            )
                            self.assertIn(
                                "invalid_legacy_token_vector",
                                facts["recorded_working"].caveats,
                            )
                            self.assertIsNone(metrics.recorded_working_tokens)
                            self.assertEqual(metrics.provenance, "estimated")
                        else:
                            row = store.connection.execute(
                                "SELECT vector_valid,validation_caveat FROM fork_baselines"
                            ).fetchone()
                            self.assertEqual(tuple(row), (
                                0, "invalid_legacy_token_vector",
                            ))
                    finally:
                        store.close()

    def test_tool_candidate_migration_normalizes_legacy_app_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "legacy-app-tool-status.sqlite3"
            build_schema(database, 21)
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
                self.assertEqual(
                    [tuple(row) for row in store.connection.execute(
                        """SELECT call_key,terminal_state,provenance
                             FROM tool_spans
                            WHERE call_key IN (
                                'legacy-completed','legacy-declined','legacy-estimated'
                            ) ORDER BY call_key"""
                    )],
                    [
                        ("legacy-completed", "success", "exact"),
                        ("legacy-declined", "failed", "exact"),
                        ("legacy-estimated", "success", "estimated"),
                    ],
                )
            finally:
                store.close()

    def test_v28_backfills_nested_custom_exec_roles_without_deleting_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "custom-exec-role-v27.sqlite3"
            build_schema(database, 27)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """INSERT INTO rollout_sessions(
                           session_key,project_id,path_key,resume_segments,
                           conversation_key,started_at,last_activity_at)
                       VALUES ('role-session','role-project','safe',1,'safe',
                               '2026-07-21T00:00:00Z','2026-07-21T00:00:10Z')"""
                )
                connection.executemany(
                    """INSERT INTO tool_spans(
                           session_key,call_key,category,terminal_state,latency_ms,
                           tool_name,started_at,finished_at,turn_key,source_digest,
                           source_ordinal,completeness,provenance)
                       VALUES ('role-session',?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        (
                            "outer", "opaque_exec", "success", 1, "custom_exec",
                            "2026-07-21T00:00:01Z", "2026-07-21T00:00:02Z", "turn",
                            "shared-safe-digest", 7, "complete", "exact",
                        ),
                        (
                            "nested", "tool", "unknown", None, "exec_command",
                            "2026-07-21T00:00:01Z", None, "turn",
                            "shared-safe-digest", 7, "incomplete", "lower_bound",
                        ),
                        (
                            "legacy", "tool", "success", 1, "unknown",
                            "2026-07-21T00:00:03Z", "2026-07-21T00:00:04Z", "turn",
                            "legacy-safe-digest", 8, "complete", "lower_bound",
                        ),
                    ),
                )
                connection.commit()
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM tool_spans WHERE session_key='role-session'"
                ).fetchone()[0], 3)
            finally:
                connection.close()

            store = HydraStore(database)
            try:
                after = aggregate_stored_task_tree(
                    store.connection,
                    project_id="role-project",
                    root_id="role-session",
                    cutoff_at=datetime(2026, 7, 21, 0, 0, 10, tzinfo=timezone.utc),
                )
                self.assertEqual(after.tool_calls.value, 2)
                self.assertEqual(store.connection.execute(
                    "SELECT COUNT(*) FROM tool_spans WHERE session_key='role-session'"
                ).fetchone()[0], 3)
                self.assertEqual(
                    [tuple(row) for row in store.connection.execute(
                        "SELECT call_key,role FROM tool_span_roles ORDER BY call_key"
                    )],
                    [("nested", "nested_inferred")],
                )
            finally:
                store.close()

    def test_missing_tool_span_roles_table_fails_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing-tool-span-roles.sqlite3"
            store = HydraStore(database)
            store.close()
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE tool_span_roles")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(
                StorageUnavailable, "missing required columns for tool_span_roles",
            ):
                HydraStore(database)

    def test_altered_tool_span_role_trust_constraints_fail_schema_validation(self) -> None:
        cases = {
            "primary-key-order": """CREATE TABLE tool_span_roles (
                session_key TEXT NOT NULL,
                call_key TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role='nested_inferred'),
                PRIMARY KEY(role,session_key,call_key),
                FOREIGN KEY(session_key,call_key)
                    REFERENCES tool_spans(session_key,call_key) ON DELETE CASCADE
            )""",
            "missing-composite-foreign-key": """CREATE TABLE tool_span_roles (
                session_key TEXT NOT NULL,
                call_key TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role='nested_inferred'),
                PRIMARY KEY(session_key,call_key,role)
            )""",
            "weakened-role-check": """CREATE TABLE tool_span_roles (
                session_key TEXT NOT NULL,
                call_key TEXT NOT NULL,
                role TEXT NOT NULL CHECK(
                    role IN ('nested_inferred', 'foreign_inferred')
                ),
                PRIMARY KEY(session_key,call_key,role),
                FOREIGN KEY(session_key,call_key)
                    REFERENCES tool_spans(session_key,call_key) ON DELETE CASCADE
            )""",
            "altered-role-literal": """CREATE TABLE tool_span_roles (
                session_key TEXT NOT NULL,
                call_key TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role='nested_ inferred'),
                PRIMARY KEY(session_key,call_key,role),
                FOREIGN KEY(session_key,call_key)
                    REFERENCES tool_spans(session_key,call_key) ON DELETE CASCADE
            )""",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, statement in cases.items():
                with self.subTest(case=name):
                    database = root / f"tool-span-roles-{name}.sqlite3"
                    store = HydraStore(database)
                    store.close()
                    replace_empty_table(database, "tool_span_roles", statement)

                    with self.assertRaisesRegex(
                        StorageUnavailable, "tool_span_roles trust constraints",
                    ):
                        HydraStore(database)

    def test_altered_location_state_trust_constraints_fail_schema_validation(self) -> None:
        columns = """
            project_id TEXT NOT NULL,
            location_key TEXT NOT NULL,
            logical_source_key TEXT NOT NULL,
            revision_digest TEXT NOT NULL,
            st_dev INTEGER NOT NULL,
            st_ino INTEGER NOT NULL,
            st_size INTEGER NOT NULL,
            st_mtime_ns INTEGER NOT NULL,
            st_ctime_ns INTEGER NOT NULL,
            scanner_version INTEGER NOT NULL
        """
        required_foreign_keys = {
            "logical": """FOREIGN KEY(logical_source_key)
                REFERENCES rollout_logical_sources(logical_source_key)""",
            "revision": """FOREIGN KEY(revision_digest)
                REFERENCES rollout_sources(source_digest)""",
            "location": """FOREIGN KEY(logical_source_key,location_key)
                REFERENCES rollout_source_locations(logical_source_key,location_key)""",
        }
        cases = {
            "primary-key-order": (
                "PRIMARY KEY(location_key,project_id)",
                tuple(required_foreign_keys.values()),
            ),
            **{
                f"missing-{missing}-foreign-key": (
                    "PRIMARY KEY(project_id,location_key)",
                    tuple(
                        statement for name, statement in required_foreign_keys.items()
                        if name != missing
                    ),
                )
                for missing in required_foreign_keys
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, (primary_key, foreign_keys) in cases.items():
                with self.subTest(case=name):
                    database = root / f"location-states-{name}.sqlite3"
                    store = HydraStore(database)
                    store.close()
                    constraints = ",\n".join((primary_key, *foreign_keys))
                    replace_empty_table(
                        database,
                        "rollout_source_location_states",
                        f"""CREATE TABLE rollout_source_location_states (
                            {columns},
                            {constraints}
                        )""",
                    )

                    with self.assertRaisesRegex(
                        StorageUnavailable,
                        "rollout_source_location_states trust constraints",
                    ):
                        HydraStore(database)

    def test_v24_migration_immediately_suppresses_declined_legacy_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "legacy-declined-intent.sqlite3"
            build_schema(database, 23)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """UPDATE rollout_test_runs
                          SET exit_status=NULL,outcome='unknown',failure_cause='unknown',
                              completeness='intent_only'
                        WHERE evidence_key='legacy-evidence'"""
                )
                connection.execute(
                    """INSERT INTO codex_event_sources(
                           source_digest,project_id,schema_version,source_format,
                           line_count,byte_count)
                       VALUES ('legacy-app-source','preserved-project',
                               'codex.app-server/v2','app_server',1,1)"""
                )
                connection.execute(
                    """INSERT INTO codex_events(
                           source_digest,source_ordinal,event_key,project_id,
                           source_format,schema_version,event_type,session_key,
                           status,provenance,tool_call_key,tool_name,tool_category,
                           tool_phase,tool_status)
                       VALUES ('legacy-app-source',1,'legacy-declined-event',
                               'preserved-project','app_server','codex.app-server/v2',
                               'item_completed','legacy-rollout-session','declined',
                               'exact','legacy-call','exec_command','tool',
                               'completed','declined')"""
                )
                connection.commit()
            finally:
                connection.close()

            store = HydraStore(database)
            try:
                self.assertEqual(store.count("rollout_test_runs"), 0)
                self.assertEqual(
                    [row[0] for row in store.connection.execute(
                        """SELECT candidate_kind FROM test_evidence_candidates
                             WHERE session_key='legacy-rollout-session'
                               AND tool_call_key='legacy-call'
                             ORDER BY candidate_kind"""
                    )],
                    ["evidence", "non_execution"],
                )
            finally:
                store.close()

    def test_v24_migration_does_not_call_failed_app_execution_a_non_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for exit_status in (1, None):
                with self.subTest(exit_status=exit_status):
                    database = root / f"legacy-failed-{exit_status}.sqlite3"
                    build_schema(database, 23)
                    connection = sqlite3.connect(database)
                    try:
                        if exit_status is None:
                            connection.execute(
                                """UPDATE rollout_test_runs
                                      SET exit_status=NULL,outcome='unknown',
                                          failure_cause='unknown',completeness='intent_only'
                                    WHERE evidence_key='legacy-evidence'"""
                            )
                        else:
                            connection.execute(
                                """UPDATE rollout_test_runs
                                      SET exit_status=1,outcome='failed',
                                          failure_cause='product_failure',completeness='complete'
                                    WHERE evidence_key='legacy-evidence'"""
                            )
                        connection.execute(
                            """INSERT INTO codex_event_sources(
                                   source_digest,project_id,schema_version,source_format,
                                   line_count,byte_count)
                               VALUES ('legacy-failed-source','preserved-project',
                                       'codex.app-server/v2','app_server',1,1)"""
                        )
                        connection.execute(
                            """INSERT INTO codex_events(
                                   source_digest,source_ordinal,event_key,project_id,
                                   source_format,schema_version,event_type,session_key,
                                   status,provenance,tool_call_key,tool_name,tool_category,
                                   tool_phase,tool_status,tool_exit_status)
                               VALUES ('legacy-failed-source',1,'legacy-failed-event',
                                       'preserved-project','app_server','codex.app-server/v2',
                                       'item_completed','legacy-rollout-session','failed',
                                       'exact','legacy-call','exec_command','tool',
                                       'completed','failed',?)""",
                            (exit_status,),
                        )
                        connection.commit()
                    finally:
                        connection.close()

                    store = HydraStore(database)
                    try:
                        candidates = [
                            row[0] for row in store.connection.execute(
                                """SELECT candidate_kind
                                     FROM test_evidence_candidates
                                    WHERE session_key='legacy-rollout-session'
                                      AND tool_call_key='legacy-call'"""
                            )
                        ]
                        self.assertIn("evidence", candidates)
                        self.assertNotIn("non_execution", candidates)
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
