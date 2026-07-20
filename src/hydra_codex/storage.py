"""SQLite storage with private, idempotent telemetry persistence."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from .contracts import AnnotationRecord, ConflictRecord, ThreadSessionRecord, TurnRecord


class StorageUnavailable(RuntimeError):
    """Raised when the configured database cannot safely be opened."""


@dataclass(frozen=True)
class WriteResult:
    inserted: bool
    conflicted: bool


def default_database_path(home: Path | None = None) -> Path:
    base = Path.home() if home is None else Path(home)
    return base / "Library" / "Application Support" / "Hydra" / "hydra.sqlite3"


def redact_note(note: str) -> str:
    """Keep a short operational note while removing common direct identifiers."""
    redacted = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", note)
    return re.sub(r"\b(?:sk|api|token)[_-][A-Za-z0-9_-]{8,}\b", "[secret]", redacted, flags=re.I)


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                provenance TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                ordinal INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                provenance TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS annotations (
                annotation_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                turn_id TEXT NOT NULL REFERENCES turns(turn_id),
                sequence INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                phase TEXT NOT NULL,
                cause TEXT NOT NULL,
                scope_change TEXT NOT NULL,
                confidence REAL NOT NULL,
                outcome TEXT,
                provenance TEXT NOT NULL,
                note_redacted TEXT NOT NULL,
                note_hash TEXT NOT NULL,
                note_length INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS annotations_session_sequence
                ON annotations(session_id, sequence, annotation_id)""",
            """CREATE TABLE IF NOT EXISTS conflicts (
                conflict_id TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                existing_hash TEXT NOT NULL,
                incoming_hash TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS token_samples (
                sample_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                turn_id TEXT NOT NULL REFERENCES turns(turn_id),
                observed_at TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                provenance TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS tool_calls (
                call_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                turn_id TEXT NOT NULL REFERENCES turns(turn_id),
                tool_name TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                provenance TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS test_runs (
                test_run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                turn_id TEXT NOT NULL REFERENCES turns(turn_id),
                observed_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                provenance TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ingest_sources (
                source_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                digest TEXT NOT NULL,
                provenance TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS reconciliation_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                provenance TEXT NOT NULL
            )""",
        ),
    ),
)


class HydraStore:
    """Single-process SQLite store; callers provide an explicit path for tests."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.database_path = default_database_path() if path is None else Path(path)
        if path is None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.database_path.parent.is_dir():
            raise StorageUnavailable(f"database parent does not exist: {self.database_path.parent}")
        try:
            self.connection = sqlite3.connect(self.database_path)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._migrate()
        except sqlite3.Error as error:
            try:
                self.connection.close()
            except AttributeError:
                pass
            raise StorageUnavailable(f"cannot open Hydra database: {self.database_path}") from error

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
            self.connection = None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def _migrate(self) -> None:
        try:
            current_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            for version, statements in MIGRATIONS:
                if version <= current_version:
                    continue
                with self._transaction() as connection:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                        (version,),
                    )
                    connection.execute(f"PRAGMA user_version = {version}")
        except sqlite3.Error as error:
            raise StorageUnavailable(f"cannot migrate Hydra database: {self.database_path}") from error

    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def upsert_session(self, record: ThreadSessionRecord) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO sessions(session_id, project_id, worktree_path, started_at, provenance)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO NOTHING""",
                (record.session_id, record.project_id, record.worktree_path, record.started_at, record.provenance.value),
            )

    def upsert_turn(self, record: TurnRecord) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO turns(turn_id, session_id, ordinal, observed_at, provenance)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(turn_id) DO NOTHING""",
                (record.turn_id, record.session_id, record.ordinal, record.observed_at, record.provenance.value),
            )

    @staticmethod
    def _annotation_values(record: AnnotationRecord) -> tuple[object, ...]:
        return (
            record.annotation_id, record.project_id, record.session_id, record.turn_id,
            record.sequence, record.observed_at, record.kind.value, record.phase.value,
            record.cause.value, record.scope_change.value, record.confidence,
            None if record.outcome is None else record.outcome.value, record.provenance.value,
            redact_note(record.note), hashlib.sha256(record.note.encode("utf-8")).hexdigest(), len(record.note),
        )

    def write_annotation(self, record: AnnotationRecord) -> WriteResult:
        values = self._annotation_values(record)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM annotations WHERE annotation_id = ?", (record.annotation_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO annotations(
                        annotation_id, project_id, session_id, turn_id, sequence, observed_at, kind,
                        phase, cause, scope_change, confidence, outcome, provenance, note_redacted,
                        note_hash, note_length
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                return WriteResult(inserted=True, conflicted=False)
            persisted = tuple(existing[column] for column in (
                "annotation_id", "project_id", "session_id", "turn_id", "sequence", "observed_at", "kind",
                "phase", "cause", "scope_change", "confidence", "outcome", "provenance", "note_redacted",
                "note_hash", "note_length",
            ))
            if persisted == values:
                return WriteResult(inserted=False, conflicted=False)
            existing_hash = hashlib.sha256(repr(persisted).encode("utf-8")).hexdigest()
            incoming_hash = hashlib.sha256(repr(values).encode("utf-8")).hexdigest()
            conflict_id = hashlib.sha256(
                f"{record.annotation_id}:{existing_hash}:{incoming_hash}".encode("utf-8")
            ).hexdigest()
            connection.execute(
                """INSERT INTO conflicts(conflict_id, record_id, existing_hash, incoming_hash, observed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(conflict_id) DO NOTHING""",
                (conflict_id, record.annotation_id, existing_hash, incoming_hash, record.observed_at),
            )
            return WriteResult(inserted=False, conflicted=True)

    def list_annotations(self, session_id: str) -> list[AnnotationRecord]:
        rows = self.connection.execute(
            "SELECT * FROM annotations WHERE session_id = ? ORDER BY sequence, annotation_id", (session_id,)
        ).fetchall()
        return [
            AnnotationRecord(
                annotation_id=row["annotation_id"], project_id=row["project_id"], session_id=row["session_id"],
                turn_id=row["turn_id"], sequence=row["sequence"], observed_at=row["observed_at"],
                kind=row["kind"], phase=row["phase"], cause=row["cause"], scope_change=row["scope_change"],
                confidence=row["confidence"], outcome=row["outcome"], note=row["note_redacted"],
                provenance=row["provenance"],
            )
            for row in rows
        ]

    def count(self, table: str) -> int:
        allowed = {"sessions", "turns", "annotations", "conflicts"}
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
