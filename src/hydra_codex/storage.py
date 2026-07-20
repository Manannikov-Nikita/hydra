"""SQLite storage with private, idempotent telemetry persistence."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
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
    """Return a normalized safe note, failing closed when it contains secret-like data."""
    normalized = " ".join("".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in note
    ).split())
    sensitive_patterns = (
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
        r"\bauthorization\s*:\s*(?:bearer|basic)\s+\S+",
        r"\bbearer\s+[A-Za-z0-9._~+/=-]+",
        r"\b(?:authorization|cookie|credential|passwd|password|secret|token|api[-_ ]?key|x[-_ ]?auth[-_ ]?token|access[-_ ]?key)\b\s*(?::|=)?\s*\S+",
        r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@\S+",
        r"(?<!\w)/(?:Users|home|private|var|etc|tmp|opt|Volumes)(?:/\S*)?",
        r"(?<![\w-])[A-Za-z0-9+/=_-]{20,}(?![\w-])",
    )
    if not normalized or any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in sensitive_patterns):
        return "[redacted]"
    return normalized


V2_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER IF NOT EXISTS annotations_project_matches_session_insert
        BEFORE INSERT ON annotations
        FOR EACH ROW
        WHEN COALESCE((SELECT project_id FROM sessions WHERE session_id = NEW.session_id), '') != NEW.project_id
        BEGIN SELECT RAISE(ABORT, 'annotation project must match session project'); END""",
    """CREATE TRIGGER IF NOT EXISTS annotations_turn_matches_session_insert
        BEFORE INSERT ON annotations
        FOR EACH ROW
        WHEN COALESCE((SELECT session_id FROM turns WHERE turn_id = NEW.turn_id), '') != NEW.session_id
        BEGIN SELECT RAISE(ABORT, 'annotation turn must belong to session'); END""",
    """CREATE TRIGGER IF NOT EXISTS annotations_project_matches_session_update
        BEFORE UPDATE OF project_id, session_id ON annotations
        FOR EACH ROW
        WHEN COALESCE((SELECT project_id FROM sessions WHERE session_id = NEW.session_id), '') != NEW.project_id
        BEGIN SELECT RAISE(ABORT, 'annotation project must match session project'); END""",
    """CREATE TRIGGER IF NOT EXISTS annotations_turn_matches_session_update
        BEFORE UPDATE OF session_id, turn_id ON annotations
        FOR EACH ROW
        WHEN COALESCE((SELECT session_id FROM turns WHERE turn_id = NEW.turn_id), '') != NEW.session_id
        BEGIN SELECT RAISE(ABORT, 'annotation turn must belong to session'); END""",
)


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
    (
        2,
        (
            "ALTER TABLE annotations ADD COLUMN task_family TEXT NOT NULL DEFAULT 'legacy'",
        ),
    ),
    (
        3,
        (
            """CREATE TABLE IF NOT EXISTS rollout_sources (
                source_digest TEXT PRIMARY KEY, source_type TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS rollout_source_locations (
                source_digest TEXT NOT NULL REFERENCES rollout_sources(source_digest),
                location_key TEXT NOT NULL, location_type TEXT NOT NULL,
                PRIMARY KEY(source_digest, location_key)
            )""",
            """CREATE TABLE IF NOT EXISTS rollout_diagnostics (
                source_digest TEXT NOT NULL, line_number INTEGER NOT NULL,
                envelope_kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(source_digest, line_number, fingerprint)
            )""",
            """CREATE TABLE IF NOT EXISTS rollout_sessions (
                session_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, path_key TEXT NOT NULL,
                resume_segments INTEGER NOT NULL DEFAULT 1
            )""",
            """CREATE TABLE IF NOT EXISTS token_snapshots (
                source_digest TEXT NOT NULL, line_number INTEGER NOT NULL,
                session_key TEXT NOT NULL REFERENCES rollout_sessions(session_key), project_id TEXT NOT NULL,
                epoch INTEGER NOT NULL, input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
                vendor_total INTEGER, context_window INTEGER, completeness TEXT NOT NULL,
                PRIMARY KEY(source_digest, line_number)
            )""",
            """CREATE TABLE IF NOT EXISTS session_edges (
                child_key TEXT PRIMARY KEY REFERENCES rollout_sessions(session_key), parent_key TEXT,
                baseline_working_tokens INTEGER, confidence_kind TEXT NOT NULL, confidence REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS turn_attempts (
                session_key TEXT NOT NULL, turn_key TEXT NOT NULL, attempt_ordinal INTEGER NOT NULL,
                state TEXT NOT NULL, emitted_duration_ms INTEGER, wall_duration_ms INTEGER,
                PRIMARY KEY(session_key, turn_key, attempt_ordinal)
            )""",
            """CREATE TABLE IF NOT EXISTS tool_spans (
                session_key TEXT NOT NULL, call_key TEXT NOT NULL, category TEXT NOT NULL,
                terminal_state TEXT NOT NULL, latency_ms INTEGER,
                PRIMARY KEY(session_key, call_key)
            )""",
            """CREATE TABLE IF NOT EXISTS file_observations (
                source_digest TEXT NOT NULL, line_number INTEGER NOT NULL, session_key TEXT NOT NULL,
                operation TEXT NOT NULL, relative_path TEXT NOT NULL, path_hash TEXT NOT NULL,
                PRIMARY KEY(source_digest, line_number, operation, relative_path)
            )""",
            """CREATE TABLE IF NOT EXISTS rollout_test_runs (
                source_digest TEXT NOT NULL, line_number INTEGER NOT NULL, session_key TEXT NOT NULL,
                command_hash TEXT NOT NULL, runner TEXT NOT NULL, scope TEXT NOT NULL,
                classification TEXT NOT NULL, outcome TEXT NOT NULL,
                PRIMARY KEY(source_digest, line_number)
            )""",
            """CREATE TABLE IF NOT EXISTS metric_facts (
                fact_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, metric_name TEXT NOT NULL,
                metric_value INTEGER NOT NULL, provenance TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS semantic_conflicts (
                conflict_key TEXT PRIMARY KEY, source_digest TEXT NOT NULL, line_number INTEGER NOT NULL,
                deterministic_cause TEXT NOT NULL, model_cause TEXT NOT NULL
            )""",
        ),
    ),
    (
        4,
        (
            """CREATE TABLE IF NOT EXISTS fork_baselines (
                child_key TEXT PRIMARY KEY REFERENCES rollout_sessions(session_key), source_digest TEXT NOT NULL,
                line_number INTEGER NOT NULL, input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
                provenance TEXT NOT NULL
            )""",
        ),
    ),
    (5, ("ALTER TABLE turn_attempts ADD COLUMN started_at TEXT", "ALTER TABLE turn_attempts ADD COLUMN finished_at TEXT")),
    (6, ("ALTER TABLE rollout_sessions ADD COLUMN conversation_key TEXT NOT NULL DEFAULT ''",)),
    (7, ("CREATE TABLE IF NOT EXISTS rollout_event_keys (event_key TEXT PRIMARY KEY, source_digest TEXT NOT NULL, source_ordinal INTEGER NOT NULL)",)),
    (8, ("ALTER TABLE token_snapshots ADD COLUMN turn_key TEXT",)),
    (9, ("ALTER TABLE token_snapshots ADD COLUMN observed_at TEXT",)),
    (10, (
        "ALTER TABLE token_snapshots RENAME TO token_snapshots_v9",
        """CREATE TABLE token_snapshots (
            source_digest TEXT NOT NULL, line_number INTEGER NOT NULL, session_key TEXT NOT NULL REFERENCES rollout_sessions(session_key), project_id TEXT NOT NULL,
            epoch INTEGER NOT NULL, input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cache_write_tokens INTEGER,
            vendor_total INTEGER, context_window INTEGER, completeness TEXT NOT NULL, turn_key TEXT, observed_at TEXT,
            PRIMARY KEY(source_digest, line_number))""",
        """INSERT INTO token_snapshots SELECT source_digest,line_number,session_key,project_id,epoch,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,vendor_total,context_window,completeness,turn_key,observed_at FROM token_snapshots_v9""",
        "DROP TABLE token_snapshots_v9",
    )),
    (11, ("ALTER TABLE fork_baselines ADD COLUMN observed_at TEXT",)),
    (12, (
        "ALTER TABLE tool_spans ADD COLUMN tool_name TEXT",
        "ALTER TABLE tool_spans ADD COLUMN started_at TEXT",
        "ALTER TABLE tool_spans ADD COLUMN finished_at TEXT",
        "ALTER TABLE tool_spans ADD COLUMN turn_key TEXT",
        "ALTER TABLE tool_spans ADD COLUMN source_digest TEXT",
        "ALTER TABLE tool_spans ADD COLUMN source_ordinal INTEGER",
        "ALTER TABLE tool_spans ADD COLUMN completeness TEXT NOT NULL DEFAULT 'incomplete'",
        "ALTER TABLE tool_spans ADD COLUMN provenance TEXT NOT NULL DEFAULT 'exact'",
    )),
    (13, (
        "ALTER TABLE rollout_test_runs RENAME TO rollout_test_runs_v12",
        """CREATE TABLE rollout_test_runs (
            evidence_key TEXT PRIMARY KEY, source_digest TEXT NOT NULL,
            line_number INTEGER NOT NULL, session_key TEXT NOT NULL,
            observed_at TEXT, turn_key TEXT, tool_call_key TEXT NOT NULL,
            command_hash TEXT NOT NULL, runner TEXT NOT NULL, scope TEXT NOT NULL,
            exit_status INTEGER, outcome TEXT NOT NULL, failure_cause TEXT NOT NULL,
            retry_kind TEXT NOT NULL DEFAULT 'none', attempt_ordinal INTEGER NOT NULL DEFAULT 1,
            provenance TEXT NOT NULL, completeness TEXT NOT NULL
        )""",
        """INSERT INTO rollout_test_runs(
               evidence_key, source_digest, line_number, session_key, tool_call_key,
               command_hash, runner, scope, outcome, failure_cause, provenance, completeness)
           SELECT source_digest || ':' || line_number, source_digest, line_number, session_key,
                  source_digest || ':' || line_number, command_hash, runner, scope, outcome,
                  CASE classification
                    WHEN 'product_failure' THEN 'product_failure' WHEN 'infra_retry' THEN 'infra_failure'
                    ELSE 'unknown'
                  END,
                  'derived', 'legacy'
             FROM rollout_test_runs_v12""",
        "DROP TABLE rollout_test_runs_v12",
        "ALTER TABLE file_observations ADD COLUMN observed_at TEXT", "ALTER TABLE file_observations ADD COLUMN turn_key TEXT",
        "CREATE INDEX rollout_test_runs_session_command ON rollout_test_runs(session_key, command_hash)",
    )),
)


class HydraStore:
    """Single-process SQLite store; callers provide an explicit path for tests."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.database_path = default_database_path() if path is None else Path(path)
        try:
            if path is None:
                self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.name == "posix":
                    os.chmod(self.database_path.parent, 0o700)
            elif not self.database_path.parent.is_dir():
                raise StorageUnavailable(f"database parent does not exist: {self.database_path.parent}")
            self.connection = sqlite3.connect(self.database_path)
            self.connection.row_factory = sqlite3.Row
            if os.name == "posix":
                os.chmod(self.database_path, 0o600)
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._migrate()
        except (OSError, sqlite3.Error) as error:
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

    @contextmanager
    def rollout_transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic, idempotent persistence boundary for safe normalized rollout facts."""
        with self._transaction() as connection:
            yield connection

    def _migrate(self) -> None:
        try:
            current_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            for version, statements in MIGRATIONS:
                if version <= current_version:
                    continue
                with self._transaction() as connection:
                    for statement in statements:
                        connection.execute(statement)
                    if version == 2:
                        self._sanitize_and_quarantine_v1_annotations(connection)
                        for statement in V2_TRIGGER_STATEMENTS:
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                        (version,),
                    )
                    connection.execute(f"PRAGMA user_version = {version}")
        except sqlite3.Error as error:
            raise StorageUnavailable(f"cannot migrate Hydra database: {self.database_path}") from error

    @staticmethod
    def _sanitize_and_quarantine_v1_annotations(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT annotations.*, sessions.project_id AS session_project_id,
                      turns.session_id AS turn_session_id
               FROM annotations
               LEFT JOIN sessions ON sessions.session_id = annotations.session_id
               LEFT JOIN turns ON turns.turn_id = annotations.turn_id"""
        ).fetchall()
        for row in rows:
            is_valid = (
                row["session_project_id"] == row["project_id"]
                and row["turn_session_id"] == row["session_id"]
            )
            if is_valid:
                connection.execute(
                    "UPDATE annotations SET note_redacted = ? WHERE annotation_id = ?",
                    (redact_note(row["note_redacted"]), row["annotation_id"]),
                )
                continue
            existing_hash = hashlib.sha256(
                "|".join(str(row[field]) for field in (
                    "annotation_id", "project_id", "session_id", "turn_id", "sequence",
                    "observed_at", "note_hash", "note_length",
                )).encode("utf-8")
            ).hexdigest()
            incoming_hash = hashlib.sha256(b"quarantined during migration v2").hexdigest()
            conflict_id = hashlib.sha256(
                f"migration-v2:{row['annotation_id']}:{existing_hash}".encode("utf-8")
            ).hexdigest()
            connection.execute(
                """INSERT INTO conflicts(conflict_id, record_id, existing_hash, incoming_hash, observed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(conflict_id) DO NOTHING""",
                (conflict_id, row["annotation_id"], existing_hash, incoming_hash, row["observed_at"]),
            )
            connection.execute("DELETE FROM annotations WHERE annotation_id = ?", (row["annotation_id"],))

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
            record.cause.value, record.scope_change.value, record.task_family, record.confidence,
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
                        phase, cause, scope_change, task_family, confidence, outcome, provenance, note_redacted,
                        note_hash, note_length
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                return WriteResult(inserted=True, conflicted=False)
            persisted = tuple(existing[column] for column in (
                "annotation_id", "project_id", "session_id", "turn_id", "sequence", "observed_at", "kind",
                "phase", "cause", "scope_change", "task_family", "confidence", "outcome", "provenance", "note_redacted",
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
                task_family=row["task_family"], confidence=row["confidence"], outcome=row["outcome"], note=row["note_redacted"],
                provenance=row["provenance"],
            )
            for row in rows
        ]

    def count(self, table: str) -> int:
        allowed = {
            "sessions", "turns", "annotations", "conflicts", "rollout_sources",
            "rollout_source_locations", "rollout_diagnostics", "rollout_sessions",
            "token_snapshots", "session_edges", "turn_attempts", "tool_spans",
            "file_observations", "rollout_test_runs", "metric_facts", "semantic_conflicts",
            "fork_baselines",
            "rollout_event_keys",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
