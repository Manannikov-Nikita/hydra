from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydra_codex.contracts import (
    AnnotationCause,
    AnnotationContext,
    AnnotationKind,
    AnnotationPhase,
    ModelAnnotationInput,
    Outcome,
    ScopeChange,
    ThreadSessionRecord,
    TurnRecord,
    materialize_annotation,
)
from hydra_codex.storage import (
    MIGRATIONS,
    HydraStore,
    StorageUnavailable,
    ValidatedStoreProvider,
    default_database_path,
)


SECRET_FORM_MATRIX = (
    "token VALUE", "token:VALUE", "token=VALUE",
    "api key VALUE", "api_key VALUE", "api_key:VALUE", "api_key=VALUE",
    "Authorization VALUE", "Authorization:VALUE", "Authorization=VALUE",
    "Cookie VALUE", "Cookie:VALUE", "Cookie=VALUE",
    "X-Auth-Token VALUE", "X-Auth-Token:VALUE", "X-Auth-Token=VALUE",
    "password VALUE", "password:VALUE", "password=VALUE",
)


def annotation(
    annotation_id: str,
    sequence: int,
    note: str = "completed",
    *,
    task_family: str = "foundation",
    project_id: str = "hprj_4db8fca38ef042f3",
    session_id: str = "session-1",
    turn_id: str = "turn-1",
):
    return materialize_annotation(
        ModelAnnotationInput(
            kind=AnnotationKind.FINISH,
            phase=AnnotationPhase.IMPLEMENT,
            cause=AnnotationCause.PROMPT,
            scope_change=ScopeChange.NONE,
            task_family=task_family,
            confidence=0.9,
            outcome=Outcome.SUCCESS,
            note=note,
        ),
        AnnotationContext(
            annotation_id=annotation_id,
            project_id=project_id,
            session_id=session_id,
            turn_id=turn_id,
            sequence=sequence,
            observed_at="2026-07-20T10:00:00Z",
        ),
    )


class SQLiteStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)
        self.store.upsert_session(
            ThreadSessionRecord(
                session_id="session-1",
                project_id="hprj_4db8fca38ef042f3",
                worktree_path="feature/alpha",
                started_at="2026-07-20T09:00:00Z",
            )
        )
        self.store.upsert_turn(
            TurnRecord(
                turn_id="turn-1",
                session_id="session-1",
                ordinal=1,
                observed_at="2026-07-20T09:01:00Z",
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_migrates_a_fresh_database_and_reopens_at_the_same_version(self) -> None:
        self.assertEqual(self.store.schema_version(), MIGRATIONS[-1][0])
        self.store.close()
        reopened = HydraStore(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version(), MIGRATIONS[-1][0])
        self.assertEqual(reopened.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_current_schema_open_skips_repeat_whole_database_validation(self) -> None:
        with patch.object(
            HydraStore,
            "_validate_schema",
            side_effect=AssertionError("hot open repeated the full database audit"),
        ):
            first = HydraStore.open_current(self.database)
            second = HydraStore.open_current(self.database)
        first.close()
        second.close()

    def test_bounded_writer_open_checks_schema_without_scanning_database(self) -> None:
        with patch.object(
            HydraStore,
            "_validate_database_integrity",
            side_effect=AssertionError(
                "bounded writer open scanned the whole database",
            ),
        ):
            store = HydraStore.open_bounded_writer(self.database)
        store.close()

        self.store.connection.execute(
            "DROP TRIGGER sync_queue_eligibility_insert",
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(
            StorageUnavailable, "bounded sync-work trigger",
        ):
            HydraStore.open_bounded_writer(self.database)

    def test_bounded_writer_rejects_schema_change_before_first_write(self) -> None:
        bounded = HydraStore.open_bounded_writer(self.database)
        self.addCleanup(bounded.close)
        other = sqlite3.connect(self.database)
        try:
            other.execute(
                "DROP TRIGGER sync_queue_eligibility_insert",
            )
            other.commit()
        finally:
            other.close()

        with self.assertRaisesRegex(
            StorageUnavailable, "after bounded writer validation",
        ):
            with bounded.rollout_transaction() as connection:
                connection.execute(
                    """INSERT INTO sync_worker_leases(
                           lease_name,owner_key,acquired_at,expires_at
                       ) VALUES (
                           'ingest','unsafe',
                           '2026-07-27T00:00:00Z',
                           '2026-07-27T00:01:00Z'
                       )""",
                )
        self.assertIsNone(
            bounded.connection.execute(
                """SELECT owner_key FROM sync_worker_leases
                    WHERE lease_name='ingest'""",
            ).fetchone(),
        )

    def test_bounded_writer_rejects_schema_change_during_validation(self) -> None:
        validate = HydraStore._validate_schema

        def tamper_after_validation(store, latest, *, full_validation=True):
            validate(store, latest, full_validation=full_validation)
            other = sqlite3.connect(self.database)
            try:
                other.execute(
                    "DROP TRIGGER sync_queue_eligibility_insert",
                )
                other.commit()
            finally:
                other.close()

        with (
            patch.object(
                HydraStore, "_validate_schema",
                new=tamper_after_validation,
            ),
            self.assertRaisesRegex(
                StorageUnavailable,
                "changed during bounded writer validation",
            ),
        ):
            HydraStore.open_bounded_writer(self.database)

    def test_constructor_closes_connection_when_storage_validation_fails(self) -> None:
        database = Path(self.temporary_directory.name) / "failed-open.sqlite3"
        failed = HydraStore.__new__(HydraStore)

        with patch.object(
            HydraStore,
            "_migrate",
            side_effect=StorageUnavailable("forced validation failure"),
        ), self.assertRaisesRegex(StorageUnavailable, "forced validation failure"):
            failed.__init__(database)

        self.assertIsNone(failed.connection)

    def test_validated_provider_bootstraps_once_and_reopens_per_caller(self) -> None:
        bootstrap_calls = 0

        def bootstrap() -> HydraStore:
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            return HydraStore.open_current(self.database)

        provider = ValidatedStoreProvider(bootstrap)
        first = provider.open()
        second = provider.open()
        try:
            self.assertIsNot(first.connection, second.connection)
            self.assertEqual(bootstrap_calls, 1)
        finally:
            first.close()
            second.close()

    def test_validated_reopener_rejects_in_place_schema_change(self) -> None:
        reopen = self.store.validated_reopener()
        other = sqlite3.connect(self.database)
        try:
            other.execute("CREATE TABLE unexpected_schema_drift(value TEXT)")
            other.commit()
        finally:
            other.close()

        with self.assertRaisesRegex(
            StorageUnavailable,
            "schema changed after startup validation",
        ):
            reopen()

    def test_migrates_version_eleven_tool_spans_without_losing_existing_rows(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "version-eleven.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        for version, statements in MIGRATIONS[:11]:
            for statement in statements:
                legacy.execute(statement)
            legacy.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-07-20T00:00:00Z')", (version,))
            legacy.execute(f"PRAGMA user_version = {version}")
        legacy.execute(
            "INSERT INTO rollout_sessions(session_key, project_id, path_key, resume_segments, conversation_key) VALUES ('s', 'p', 'safe', 1, 'c')"
        )
        legacy.execute("INSERT INTO tool_spans(session_key, call_key, category, terminal_state) VALUES ('s', 'c', 'tool', 'success')")
        legacy.commit()
        legacy.close()

        migrated = HydraStore(legacy_path)
        self.addCleanup(migrated.close)

        row = migrated.connection.execute(
            "SELECT terminal_state, completeness, provenance, tool_name, started_at, finished_at FROM tool_spans"
        ).fetchone()
        self.assertEqual(migrated.schema_version(), MIGRATIONS[-1][0])
        self.assertEqual(tuple(row), ("success", "incomplete", "exact", None, None, None))

    def test_second_migration_sanitizes_and_quarantines_actual_version_one_rows(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        for statement in MIGRATIONS[0][1]:
            legacy.execute(statement)
        legacy.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-07-20T00:00:00Z')")
        legacy.executemany(
            "INSERT INTO sessions(session_id, project_id, worktree_path, started_at, provenance) VALUES (?, ?, ?, ?, ?)",
            (
                ("legacy-session-1", "project-one", "first", "2026-07-20T09:00:00Z", "exact"),
                ("legacy-session-2", "project-two", "second", "2026-07-20T09:00:00Z", "exact"),
            ),
        )
        legacy.executemany(
            "INSERT INTO turns(turn_id, session_id, ordinal, observed_at, provenance) VALUES (?, ?, ?, ?, ?)",
            (
                ("legacy-turn-1", "legacy-session-1", 1, "2026-07-20T09:01:00Z", "exact"),
                ("legacy-turn-2", "legacy-session-2", 1, "2026-07-20T09:01:00Z", "exact"),
            ),
        )
        safe_legacy_rows = tuple(
            (
                f"legacy-safe-{sequence}", "project-one", "legacy-session-1", "legacy-turn-1", sequence,
                "2026-07-20T09:02:00Z", "finish", "implement", "prompt", "none", 0.9,
                "success", "model_reported", note, f"legacy-safe-hash-{sequence}", len(note),
            )
            for sequence, note in enumerate(SECRET_FORM_MATRIX, start=1)
        )
        legacy.executemany(
            """INSERT INTO annotations(
                annotation_id, project_id, session_id, turn_id, sequence, observed_at, kind,
                phase, cause, scope_change, confidence, outcome, provenance, note_redacted,
                note_hash, note_length
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            safe_legacy_rows + (
                (
                    "legacy-invalid", "project-one", "legacy-session-1", "legacy-turn-2", 99,
                    "2026-07-20T09:03:00Z", "finish", "implement", "prompt", "none", 0.9,
                    "success", "model_reported", "Cookie legacy-secret", "legacy-invalid-hash", 20,
                ),
            ),
        )
        legacy.execute("PRAGMA user_version = 1")
        legacy.commit()
        legacy.close()

        migrated = HydraStore(legacy_path)
        self.addCleanup(migrated.close)
        columns = {row[1] for row in migrated.connection.execute("PRAGMA table_info(annotations)")}
        safe_rows = migrated.connection.execute(
            "SELECT note_redacted FROM annotations WHERE annotation_id LIKE 'legacy-safe-%' ORDER BY annotation_id"
        ).fetchall()
        invalid_row = migrated.connection.execute(
            "SELECT annotation_id FROM annotations WHERE annotation_id = 'legacy-invalid'"
        ).fetchone()
        conflict = migrated.connection.execute(
            "SELECT * FROM conflicts WHERE record_id = 'legacy-invalid'"
        ).fetchone()

        self.assertEqual(migrated.schema_version(), MIGRATIONS[-1][0])
        self.assertIn("task_family", columns)
        self.assertEqual(len(safe_rows), len(SECRET_FORM_MATRIX))
        self.assertTrue(all(row[0] == "[redacted]" for row in safe_rows))
        self.assertIsNone(invalid_row)
        self.assertIsNotNone(conflict)
        self.assertNotIn("legacy-secret", repr(tuple(conflict)))

    def test_idempotent_upserts_do_not_duplicate_records(self) -> None:
        self.store.upsert_session(
            ThreadSessionRecord(
                session_id="session-1",
                project_id="hprj_4db8fca38ef042f3",
                worktree_path="feature/alpha",
                started_at="2026-07-20T09:00:00Z",
            )
        )
        self.store.upsert_turn(
            TurnRecord(
                turn_id="turn-1",
                session_id="session-1",
                ordinal=1,
                observed_at="2026-07-20T09:01:00Z",
            )
        )

        self.assertEqual(self.store.count("sessions"), 1)
        self.assertEqual(self.store.count("turns"), 1)

    def test_annotations_are_ordered_by_sequence_even_when_written_out_of_order(self) -> None:
        self.store.write_annotation(annotation("ann-later", 20))
        self.store.write_annotation(annotation("ann-earlier", 10))

        self.assertEqual(
            [item.annotation_id for item in self.store.list_annotations("session-1")],
            ["ann-earlier", "ann-later"],
        )

    def test_duplicate_annotation_is_idempotent(self) -> None:
        record = annotation("ann-duplicate", 1)
        self.assertFalse(self.store.write_annotation(record).conflicted)
        self.assertFalse(self.store.write_annotation(record).conflicted)

        self.assertEqual(self.store.count("annotations"), 1)
        self.assertEqual(self.store.count("conflicts"), 0)

    def test_conflicting_annotation_is_recorded_without_overwriting_original(self) -> None:
        self.store.write_annotation(annotation("ann-conflict", 1, "first note"))
        result = self.store.write_annotation(annotation("ann-conflict", 1, "second note"))

        self.assertTrue(result.conflicted)
        self.assertEqual(self.store.count("annotations"), 1)
        self.assertEqual(self.store.count("conflicts"), 1)

    def test_annotations_store_redacted_note_and_metadata_not_raw_content_columns(self) -> None:
        private_note = (
            "Authorization: Bearer bearer-secret-1234567890\n"
            "password=correct-horse-battery-staple api_key=ABCD1234EFGH5678\n"
            "https://alice:password@example.com/path /Users/alice/private.txt "
            "alice@example.com QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
        )
        self.store.write_annotation(annotation("ann-private", 1, private_note))

        row = self.store.connection.execute(
            "SELECT note_redacted, note_hash, note_length FROM annotations WHERE annotation_id = ?",
            ("ann-private",),
        ).fetchone()
        columns = {item[1] for item in self.store.connection.execute("PRAGMA table_info(annotations)")}

        for raw_fragment in (
            "bearer-secret-1234567890", "correct-horse-battery-staple", "ABCD1234EFGH5678",
            "alice:password@example.com", "/Users/alice/private.txt", "alice@example.com",
            "QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        ):
            self.assertNotIn(raw_fragment, row[0])
        self.assertNotIn("\n", row[0])
        self.assertEqual(len(row[1]), 64)
        self.assertEqual(row[2], len(private_note))
        self.assertFalse({"raw_note", "prompt", "message", "tool_output"} & columns)

    def test_common_phone_numbers_are_redacted_before_storage(self) -> None:
        for sequence, note in enumerate((
            "Call +7 (999) 123-45-67 after the run",
            "Reach me at 8 999 123 45 67",
            "US contact +1 415-555-2671",
        ), start=1):
            with self.subTest(note=note):
                annotation_id = f"ann-phone-{sequence}"
                self.store.write_annotation(annotation(annotation_id, sequence, note))
                stored = self.store.connection.execute(
                    "SELECT note_redacted FROM annotations WHERE annotation_id=?",
                    (annotation_id,),
                ).fetchone()[0]
                self.assertEqual(stored, "[redacted]")

    def test_keyword_and_header_secret_forms_are_fail_closed(self) -> None:
        risky_notes = SECRET_FORM_MATRIX + ("credential VALUE", "passwd VALUE", "access-key VALUE")
        for sequence, note in enumerate(risky_notes, start=1):
            with self.subTest(note=note):
                annotation_id = f"ann-header-{sequence}"
                self.store.write_annotation(annotation(annotation_id, sequence, note))
                stored = self.store.connection.execute(
                    "SELECT note_redacted FROM annotations WHERE annotation_id = ?", (annotation_id,)
                ).fetchone()[0]
                self.assertEqual(stored, "[redacted]")
                self.assertNotIn("VALUE", stored)

    def test_task_family_persists_and_round_trips(self) -> None:
        self.store.write_annotation(annotation(
            "ann-family", 1, task_family="release-workflow-hardening",
        ))

        row = self.store.connection.execute(
            "SELECT task_family FROM annotations WHERE annotation_id = ?", ("ann-family",)
        ).fetchone()
        self.assertEqual(row[0], "release-workflow-hardening")
        self.assertEqual(
            self.store.list_annotations("session-1")[0].task_family,
            "release-workflow-hardening",
        )

    def test_unsafe_task_family_is_rejected_before_annotation_storage(self) -> None:
        for sequence, family in enumerate((
            "/Users/alice/private", "alice@example.com", "raw family with spaces",
            "019f75d4-5125-7343-8537-49b80f27f286", "token=private",
            "customer-123456", "alice", "secret-token", "private-looking",
            "alice-review", "acme-workflow", "alice-termination-review",
        ), start=1):
            with self.subTest(family=family):
                with self.assertRaisesRegex(ValueError, "privacy-safe category"):
                    self.store.write_annotation(annotation(
                        f"unsafe-family-{sequence}", sequence, task_family=family,
                    ))
        self.assertEqual(self.store.count("annotations"), 0)

    def test_conflicts_do_not_store_the_raw_note(self) -> None:
        raw_note = "password=unpersistable-secret-123456"
        self.store.write_annotation(annotation("ann-conflict-private", 1, raw_note))
        self.store.write_annotation(annotation("ann-conflict-private", 1, raw_note + "-changed"))

        conflict = self.store.connection.execute("SELECT * FROM conflicts").fetchone()
        self.assertNotIn(raw_note, repr(tuple(conflict)))

    def test_project_mismatch_is_rejected_without_writing_annotation(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.write_annotation(annotation("ann-wrong-project", 1, project_id="other-project"))
        self.assertEqual(self.store.count("annotations"), 0)

    def test_turn_from_another_session_is_rejected_without_writing_annotation(self) -> None:
        self.store.upsert_session(
            ThreadSessionRecord(
                session_id="session-2", project_id="hprj_4db8fca38ef042f3",
                worktree_path="feature/beta", started_at="2026-07-20T09:00:00Z",
            )
        )
        self.store.upsert_turn(
            TurnRecord(
                turn_id="turn-2", session_id="session-2", ordinal=1,
                observed_at="2026-07-20T09:01:00Z",
            )
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.write_annotation(annotation("ann-wrong-turn", 1, turn_id="turn-2"))
        self.assertEqual(self.store.count("annotations"), 0)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions are unavailable")
    def test_default_created_storage_is_user_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            with patch("hydra_codex.storage.Path.home", return_value=home):
                store = HydraStore()
            self.addCleanup(store.close)

            self.assertEqual(stat.S_IMODE(store.database_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.database_path.stat().st_mode), 0o600)

    def test_unavailable_database_raises_domain_error(self) -> None:
        missing_parent = Path(self.temporary_directory.name) / "missing" / "hydra.sqlite3"
        with self.assertRaises(StorageUnavailable):
            HydraStore(missing_parent)

    def test_default_database_path_uses_macos_application_support(self) -> None:
        self.assertEqual(
            default_database_path(Path("/tmp/hydra-home"), platform="darwin"),
            Path("/tmp/hydra-home/Library/Application Support/Hydra/hydra.sqlite3"),
        )
