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
from hydra_codex.storage import MIGRATIONS, HydraStore, StorageUnavailable, default_database_path


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
        self.assertEqual(self.store.schema_version(), 2)
        self.store.close()
        reopened = HydraStore(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version(), 2)
        self.assertEqual(reopened.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

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
        legacy.executemany(
            """INSERT INTO annotations(
                annotation_id, project_id, session_id, turn_id, sequence, observed_at, kind,
                phase, cause, scope_change, confidence, outcome, provenance, note_redacted,
                note_hash, note_length
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    "legacy-safe", "project-one", "legacy-session-1", "legacy-turn-1", 1,
                    "2026-07-20T09:02:00Z", "finish", "implement", "prompt", "none", 0.9,
                    "success", "model_reported", "token VALUE", "legacy-safe-hash", 11,
                ),
                (
                    "legacy-invalid", "project-one", "legacy-session-1", "legacy-turn-2", 2,
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
        safe_row = migrated.connection.execute(
            "SELECT note_redacted FROM annotations WHERE annotation_id = 'legacy-safe'"
        ).fetchone()
        invalid_row = migrated.connection.execute(
            "SELECT annotation_id FROM annotations WHERE annotation_id = 'legacy-invalid'"
        ).fetchone()
        conflict = migrated.connection.execute(
            "SELECT * FROM conflicts WHERE record_id = 'legacy-invalid'"
        ).fetchone()

        self.assertEqual(migrated.schema_version(), 2)
        self.assertIn("task_family", columns)
        self.assertEqual(safe_row[0], "[redacted]")
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

    def test_keyword_and_header_secret_forms_are_fail_closed(self) -> None:
        risky_notes = (
            "token VALUE",
            "api key VALUE",
            "X-Auth-Token VALUE",
            "Authorization VALUE",
            "Cookie VALUE",
            "credential VALUE",
            "passwd VALUE",
            "access-key VALUE",
        )
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
        self.store.write_annotation(annotation("ann-family", 1, task_family="privacy-hardening"))

        row = self.store.connection.execute(
            "SELECT task_family FROM annotations WHERE annotation_id = ?", ("ann-family",)
        ).fetchone()
        self.assertEqual(row[0], "privacy-hardening")
        self.assertEqual(self.store.list_annotations("session-1")[0].task_family, "privacy-hardening")

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
            default_database_path(Path("/tmp/hydra-home")),
            Path("/tmp/hydra-home/Library/Application Support/Hydra/hydra.sqlite3"),
        )
