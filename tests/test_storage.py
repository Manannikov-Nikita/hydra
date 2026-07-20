from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

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
from hydra_codex.storage import HydraStore, StorageUnavailable, default_database_path


def annotation(annotation_id: str, sequence: int, note: str = "completed"):
    return materialize_annotation(
        ModelAnnotationInput(
            kind=AnnotationKind.FINISH,
            phase=AnnotationPhase.IMPLEMENT,
            cause=AnnotationCause.PROMPT,
            scope_change=ScopeChange.NONE,
            confidence=0.9,
            outcome=Outcome.SUCCESS,
            note=note,
        ),
        AnnotationContext(
            annotation_id=annotation_id,
            project_id="hprj_4db8fca38ef042f3",
            session_id="session-1",
            turn_id="turn-1",
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
        self.assertEqual(self.store.schema_version(), 1)
        self.store.close()
        reopened = HydraStore(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version(), 1)
        self.assertEqual(reopened.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

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
        self.store.write_annotation(annotation("ann-private", 1, "email alice@example.com"))

        row = self.store.connection.execute(
            "SELECT note_redacted, note_hash, note_length FROM annotations WHERE annotation_id = ?",
            ("ann-private",),
        ).fetchone()
        columns = {item[1] for item in self.store.connection.execute("PRAGMA table_info(annotations)")}

        self.assertNotIn("alice@example.com", row[0])
        self.assertEqual(len(row[1]), 64)
        self.assertEqual(row[2], len("email alice@example.com"))
        self.assertFalse({"raw_note", "prompt", "message", "tool_output"} & columns)

    def test_unavailable_database_raises_domain_error(self) -> None:
        missing_parent = Path(self.temporary_directory.name) / "missing" / "hydra.sqlite3"
        with self.assertRaises(StorageUnavailable):
            HydraStore(missing_parent)

    def test_default_database_path_uses_macos_application_support(self) -> None:
        self.assertEqual(
            default_database_path(Path("/tmp/hydra-home")),
            Path("/tmp/hydra-home/Library/Application Support/Hydra/hydra.sqlite3"),
        )
