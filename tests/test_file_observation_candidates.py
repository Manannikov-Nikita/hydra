from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from hydra_codex.rollout_identity import ACTIVE_HASHER, Pseudonymizer
from hydra_codex.rollout_persistence import persist_file
from hydra_codex.storage import MIGRATIONS, HydraStore
from hydra_codex.tool_spans import persist_tool_end, persist_tool_start


class FileObservationCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = HydraStore(self.root / "hydra.sqlite3")
        self.connection = self.store.connection
        self.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,conversation_key)
               VALUES ('session','project','path',1,'conversation')"""
        )
        for source, schema, family in (
            ("app-raw", "codex.app-server/v2", "app_server"),
        ):
            self.connection.execute(
                """INSERT INTO codex_event_sources(
                       source_digest,project_id,schema_version,source_format,
                       line_count,byte_count)
                   VALUES (?,?,?,?,1,1)""",
                (source, "project", schema, family),
            )
        for source, source_type, chain in (
            ("rollout", "jsonl", "rollout-chain"),
            ("app", "explicit", "app-raw"),
        ):
            logical = f"{source}-logical"
            self.connection.execute(
                """INSERT INTO rollout_logical_sources(
                       logical_source_key,project_id,lineage_state)
                   VALUES (?,?,'clean')""",
                (logical, "project"),
            )
            self.connection.execute(
                """INSERT INTO rollout_sources(
                       source_digest,source_type,logical_source_key,relation,
                       line_count,byte_count,chain_digest,materialized)
                   VALUES (?,?,?,'initial',1,1,?,1)""",
                (source, source_type, logical, chain),
            )
        self.hash_token = ACTIVE_HASHER.set(Pseudonymizer(b"file-candidates-key-000000000001"))

    def tearDown(self) -> None:
        ACTIVE_HASHER.reset(self.hash_token)
        self.store.close()
        self.temporary.cleanup()

    def _tool(
        self, source: str, *, tool_name: str, terminal: str,
        call: str = "call", ordinal: int = 1,
    ) -> None:
        persist_tool_start(
            self.connection,
            session_key="session",
            call_key=call,
            category="tool",
            tool_name=tool_name,
            started_at="2026-07-21T00:00:00Z",
            turn_key=f"{source}-turn",
            source_digest=source,
            source_ordinal=ordinal,
        )
        persist_tool_end(
            self.connection,
            session_key="session",
            call_key=call,
            category="tool",
            tool_name=tool_name,
            finished_at="2026-07-21T00:00:01Z",
            terminal_state=terminal,
            latency_ms=1000,
            turn_key=f"{source}-turn",
            source_digest=source,
            source_ordinal=ordinal + 1,
        )

    def _file(
        self,
        source: str,
        *,
        operation: str,
        path: str,
        observed_at: str,
        tool_name: str,
        call: str = "call",
        line: int = 3,
        turn: str | None = None,
    ) -> None:
        persist_file(
            self.connection,
            source,
            line,
            "session",
            operation,
            path,
            self.root,
            observed_at,
            turn or f"{source}-turn",
            observation_call_key=call,
            observation_tool_name=tool_name,
            requires_success=True,
        )

    def test_higher_authority_failed_rollout_suppresses_conflicting_app_write_in_any_order(self) -> None:
        outcomes: list[list[tuple[str, str]]] = []
        for index, order in enumerate(("app-first", "rollout-first")):
            call = f"conflict-{index}"

            def app() -> None:
                self._tool("app", tool_name="apply_patch", terminal="success", call=call)
                self._file(
                    "app", operation="write", path=f"src/conflict-{index}.py",
                    observed_at="2026-07-21T00:00:01.200000Z",
                    tool_name="apply_patch", call=call,
                )

            def rollout() -> None:
                self._tool(
                    "rollout", tool_name="exec_command", terminal="failed",
                    call=call,
                )

            if order == "app-first":
                app()
                rollout()
            else:
                rollout()
                app()
            outcomes.append([
                tuple(row) for row in self.connection.execute(
                    "SELECT operation,relative_path FROM file_observations "
                    "WHERE relative_path=?",
                    (f"src/conflict-{index}.py",),
                )
            ])

        self.assertEqual(outcomes, [[], []])

    def test_valid_cross_adapter_fact_dedupes_and_keeps_earliest_precise_time(self) -> None:
        self._tool("app", tool_name="exec_command", terminal="success")
        self._file(
            "app", operation="read", path="src/shared.py",
            observed_at="2026-07-21T00:00:01.100100Z",
            tool_name="exec_command", line=4, turn="turn-b",
        )
        self._tool("rollout", tool_name="exec_command", terminal="success")
        self._file(
            "rollout", operation="read", path="src/shared.py",
            observed_at="2026-07-21T00:00:01.100900Z",
            tool_name="exec_command", line=5, turn="turn-a",
        )

        rows = self.connection.execute(
            "SELECT operation,relative_path,observed_at,turn_key "
            "FROM file_observations"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("read", "src/shared.py", "2026-07-21T00:00:01.100100Z", "turn-b")],
        )

    def test_lower_source_cannot_add_a_path_when_canonical_source_has_no_file_fact(self) -> None:
        self._tool("rollout", tool_name="exec_command", terminal="success")
        self._tool("app", tool_name="exec_command", terminal="success")
        self._file(
            "app", operation="read", path="src/lower-only.py",
            observed_at="2026-07-21T00:00:01.100100Z",
            tool_name="exec_command",
        )

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM file_observations"
            ).fetchone()[0],
            0,
        )

    def test_same_call_preserves_legitimate_multiple_paths_and_operations(self) -> None:
        self._tool("rollout", tool_name="exec_command", terminal="success")
        for operation, path in (
            ("read", "src/input.py"),
            ("write", "build/output.py"),
            ("write", "build/second.py"),
        ):
            self._file(
                "rollout", operation=operation, path=path,
                observed_at="2026-07-21T00:00:01.500000Z",
                tool_name="exec_command",
            )

        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                "SELECT operation,relative_path FROM file_observations "
                "ORDER BY operation,relative_path"
            )],
            [
                ("read", "src/input.py"),
                ("write", "build/output.py"),
                ("write", "build/second.py"),
            ],
        )

    def test_append_only_same_family_revision_does_not_lose_terminal_file_fact(self) -> None:
        for source in ("zz-rollout-old", "aa-rollout-new"):
            logical = f"{source}-logical"
            self.connection.execute(
                """INSERT INTO rollout_logical_sources(
                       logical_source_key,project_id,lineage_state)
                   VALUES (?,?,'clean')""",
                (logical, "project"),
            )
            self.connection.execute(
                """INSERT INTO rollout_sources(
                       source_digest,source_type,logical_source_key,relation,
                       line_count,byte_count,chain_digest,materialized)
                   VALUES (?,'jsonl',?,'initial',1,1,?,1)""",
                (source, logical, f"{source}-chain"),
            )

        persist_tool_start(
            self.connection, session_key="session", call_key="extension-call",
            category="tool", tool_name="exec_command",
            started_at="2026-07-21T00:00:00Z", turn_key="old-turn",
            source_digest="zz-rollout-old", source_ordinal=1,
        )
        persist_tool_start(
            self.connection, session_key="session", call_key="extension-call",
            category="tool", tool_name="exec_command",
            started_at="2026-07-21T00:00:00Z", turn_key="new-turn",
            source_digest="aa-rollout-new", source_ordinal=1,
        )
        persist_tool_end(
            self.connection, session_key="session", call_key="extension-call",
            category="tool", tool_name="exec_command",
            finished_at="2026-07-21T00:00:01Z", terminal_state="success",
            latency_ms=1000, turn_key="new-turn",
            source_digest="aa-rollout-new", source_ordinal=2,
        )
        self._file(
            "aa-rollout-new", operation="read", path="src/extended.py",
            observed_at="2026-07-21T00:00:01Z", tool_name="exec_command",
            call="extension-call", line=3, turn="new-turn",
        )

        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                "SELECT operation,relative_path FROM file_observations"
            )],
            [("read", "src/extended.py")],
        )

    def test_candidate_rows_are_database_immutable(self) -> None:
        self._tool("rollout", tool_name="exec_command", terminal="success")
        self._file(
            "rollout", operation="read", path="src/immutable.py",
            observed_at="2026-07-21T00:00:01Z", tool_name="exec_command",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute(
                "UPDATE file_observation_candidates SET turn_key='changed'"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute("DELETE FROM file_observation_candidates")


class FileObservationMigrationTests(unittest.TestCase):
    def test_v25_reuses_unambiguous_legacy_apply_patch_call_across_append_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "legacy-apply-patch.sqlite3"
            connection = sqlite3.connect(database)
            try:
                # This is the normal pre-candidate shape: the canonical span
                # points at the function-call/start line while the direct file
                # fact was emitted at the successful terminal line.
                for version, statements in MIGRATIONS:
                    if version >= 22:
                        break
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) "
                        "VALUES (?,datetime('now'))",
                        (version,),
                    )
                    connection.execute(f"PRAGMA user_version={version}")
                connection.execute(
                    """INSERT INTO rollout_sessions(
                           session_key,project_id,path_key,resume_segments,conversation_key)
                       VALUES ('legacy-session','project','path',1,'conversation')"""
                )
                connection.execute(
                    """INSERT INTO tool_spans(
                           session_key,call_key,category,terminal_state,latency_ms,
                           tool_name,started_at,finished_at,turn_key,source_digest,
                           source_ordinal,completeness,provenance)
                       VALUES ('legacy-session','legacy-patch-call','tool','success',1,
                               'apply_patch','2026-07-21T00:00:00Z',
                               '2026-07-21T00:00:01Z','legacy-turn',
                               'legacy-rollout-source',4,'complete','exact')"""
                )
                connection.execute(
                    """INSERT INTO file_observations(
                           source_digest,line_number,session_key,operation,
                           relative_path,path_hash,observed_at,turn_key)
                       VALUES ('legacy-rollout-source',5,'legacy-session','write',
                               'src/changed.py','legacy-path-hash',
                               '2026-07-21T00:00:01Z','legacy-turn')"""
                )
                connection.commit()
            finally:
                connection.close()

            store = HydraStore(database)
            self.addCleanup(store.close)

            migrated = store.connection.execute(
                """SELECT call_key,source_ordinal,operation,relative_path
                     FROM file_observation_candidates"""
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in migrated],
                [("legacy-patch-call", 5, "write", "src/changed.py")],
            )

            token = ACTIVE_HASHER.set(
                Pseudonymizer(b"file-migration-patch-key-000000001")
            )
            try:
                persist_tool_end(
                    store.connection,
                    session_key="legacy-session",
                    call_key="legacy-patch-call",
                    category="tool",
                    tool_name="patch",
                    finished_at="2026-07-21T00:00:01Z",
                    terminal_state="success",
                    latency_ms=1,
                    turn_key="legacy-turn",
                    source_digest="legacy-rollout-source",
                    source_ordinal=5,
                )
                persist_file(
                    store.connection,
                    "legacy-rollout-source",
                    5,
                    "legacy-session",
                    "write",
                    "src/changed.py",
                    Path(temporary),
                    "2026-07-21T00:00:01Z",
                    "legacy-turn",
                    observation_call_key="legacy-patch-call",
                    observation_tool_name="apply_patch",
                    requires_success=True,
                )
            finally:
                ACTIVE_HASHER.reset(token)

            self.assertEqual(
                [tuple(row) for row in store.connection.execute(
                    """SELECT operation,relative_path
                         FROM file_observations"""
                )],
                [("write", "src/changed.py")],
            )

    def test_v25_does_not_guess_legacy_call_when_terminal_metadata_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "ambiguous-legacy-file.sqlite3"
            connection = sqlite3.connect(database)
            try:
                for version, statements in MIGRATIONS:
                    if version >= 22:
                        break
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) "
                        "VALUES (?,datetime('now'))",
                        (version,),
                    )
                    connection.execute(f"PRAGMA user_version={version}")
                connection.execute(
                    """INSERT INTO rollout_sessions(
                           session_key,project_id,path_key,resume_segments,conversation_key)
                       VALUES ('legacy-session','project','path',1,'conversation')"""
                )
                for call_key in ("call-a", "call-b"):
                    connection.execute(
                        """INSERT INTO tool_spans(
                               session_key,call_key,category,terminal_state,latency_ms,
                               tool_name,started_at,finished_at,turn_key,source_digest,
                               source_ordinal,completeness,provenance)
                           VALUES ('legacy-session',?,'tool','success',1,
                                   'apply_patch','2026-07-21T00:00:00Z',
                                   '2026-07-21T00:00:01Z','legacy-turn',
                                   'legacy-rollout-source',4,'complete','exact')""",
                        (call_key,),
                    )
                connection.execute(
                    """INSERT INTO file_observations(
                           source_digest,line_number,session_key,operation,
                           relative_path,path_hash,observed_at,turn_key)
                       VALUES ('legacy-rollout-source',5,'legacy-session','write',
                               'src/changed.py','legacy-path-hash',
                               '2026-07-21T00:00:01Z','legacy-turn')"""
                )
                connection.commit()
            finally:
                connection.close()

            store = HydraStore(database)
            self.addCleanup(store.close)
            call_key = store.connection.execute(
                "SELECT call_key FROM file_observation_candidates"
            ).fetchone()[0]
            self.assertTrue(call_key.startswith("legacy/"), call_key)

    def test_v25_backfills_legacy_observations_into_privacy_safe_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            try:
                for version, statements in MIGRATIONS:
                    if version >= 25:
                        break
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) "
                        "VALUES (?,datetime('now'))",
                        (version,),
                    )
                    connection.execute(f"PRAGMA user_version={version}")
                connection.execute(
                    """INSERT INTO rollout_sessions(
                           session_key,project_id,path_key,resume_segments,conversation_key)
                       VALUES ('legacy-session','project','path',1,'conversation')"""
                )
                connection.execute(
                    """INSERT INTO file_observations(
                           source_digest,line_number,session_key,operation,
                           relative_path,path_hash,observed_at,turn_key)
                       VALUES ('opaque-source',7,'legacy-session','read',
                               'src/safe.py','opaque-path',
                               '2026-07-21T00:00:01.000123Z','opaque-turn')"""
                )
                connection.execute(
                    """INSERT INTO tool_spans(
                           session_key,call_key,category,terminal_state,latency_ms,
                           tool_name,source_digest,source_ordinal,completeness,provenance)
                       VALUES ('legacy-session','legacy-call','tool','success',1,
                               'exec_command','opaque-source',7,'complete','exact')"""
                )
                connection.execute(
                    """INSERT INTO tool_span_candidates(
                           session_key,call_key,source_digest,source_ordinal,
                           candidate_kind,category,terminal_state,tool_name,provenance)
                       VALUES ('legacy-session','legacy-call','opaque-source',7,
                               'start','tool','unknown','exec_command','exact')"""
                )
                connection.commit()
            finally:
                connection.close()

            store = HydraStore(database)
            self.addCleanup(store.close)

            candidate = store.connection.execute(
                """SELECT session_key,call_key,source_digest,source_ordinal,operation,
                          relative_path,path_hash,observed_at,turn_key,
                          tool_name,requires_success,evidence_kind
                     FROM file_observation_candidates"""
            ).fetchone()
            self.assertEqual(tuple(candidate), (
                "legacy-session", "legacy-call", "opaque-source", 7, "read", "src/safe.py",
                "opaque-path", "2026-07-21T00:00:01.000123Z", "opaque-turn",
                "unknown", 0, "legacy",
            ))
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM file_observations"
                ).fetchone()[0],
                1,
            )
            token = ACTIVE_HASHER.set(
                Pseudonymizer(b"file-migration-key-0000000000001")
            )
            try:
                persist_file(
                    store.connection, "opaque-source", 8, "legacy-session",
                    "read", "src/safe.py", Path(temporary),
                    "2026-07-21T00:00:01.000124Z", "new-turn",
                    observation_call_key="legacy-call",
                    observation_tool_name="exec_command",
                    requires_success=True,
                )
            finally:
                ACTIVE_HASHER.reset(token)
            self.assertEqual(
                [tuple(row) for row in store.connection.execute(
                    """SELECT operation,relative_path,observed_at
                         FROM file_observations"""
                )],
                [("read", "src/safe.py", "2026-07-21T00:00:01.000123Z")],
            )


if __name__ == "__main__":
    unittest.main()
