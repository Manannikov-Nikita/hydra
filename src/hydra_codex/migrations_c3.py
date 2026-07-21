"""Capability-backed semantic annotation schema."""

from __future__ import annotations


C3_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (18, (
        """CREATE TABLE trusted_turn_bindings (
            turn_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','finished')),
            last_sequence INTEGER NOT NULL DEFAULT -1 CHECK(last_sequence >= -1),
            UNIQUE(project_id,session_key,turn_key)
        )""",
        """CREATE TABLE turn_capabilities (
            capability_digest TEXT PRIMARY KEY,
            turn_key TEXT NOT NULL REFERENCES trusted_turn_bindings(turn_key),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            revoked_at TEXT,
            stop_retry INTEGER NOT NULL DEFAULT 0 CHECK(stop_retry >= 0)
        )""",
        "CREATE INDEX turn_capabilities_turn ON turn_capabilities(turn_key,created_at)",
        """CREATE TABLE annotation_receipts (
            annotation_id TEXT PRIMARY KEY REFERENCES annotations(annotation_id),
            turn_key TEXT NOT NULL REFERENCES trusted_turn_bindings(turn_key),
            capability_digest TEXT NOT NULL REFERENCES turn_capabilities(capability_digest),
            sequence INTEGER NOT NULL CHECK(sequence >= 0),
            request_digest TEXT NOT NULL UNIQUE,
            payload_digest TEXT NOT NULL,
            first_received_at TEXT NOT NULL,
            last_received_at TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
            UNIQUE(turn_key,sequence)
        )""",
        """CREATE TABLE semantic_intervals (
            interval_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            turn_key TEXT NOT NULL REFERENCES trusted_turn_bindings(turn_key),
            start_annotation_id TEXT NOT NULL REFERENCES annotations(annotation_id),
            end_annotation_id TEXT REFERENCES annotations(annotation_id),
            start_sequence INTEGER NOT NULL CHECK(start_sequence >= 0),
            end_sequence INTEGER CHECK(end_sequence IS NULL OR end_sequence > start_sequence),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            phase TEXT NOT NULL,
            cause TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK(provenance IN ('derived','model_reported')),
            UNIQUE(turn_key,start_sequence)
        )""",
        "CREATE UNIQUE INDEX semantic_intervals_one_open ON semantic_intervals(turn_key) WHERE ended_at IS NULL",
        """CREATE TABLE semantic_fact_staging (
            fact_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            turn_key TEXT NOT NULL REFERENCES trusted_turn_bindings(turn_key),
            sequence INTEGER,
            fact_kind TEXT NOT NULL CHECK(fact_kind IN (
                'annotation_sequence_conflict','annotation_request_conflict',
                'annotation_out_of_order','self_report_missing','semantic_conflict'
            )),
            observed_at TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK(provenance IN ('exact','derived'))
        )""",
    )),
)


C3_REQUIRED_SCHEMA = {
    "trusted_turn_bindings": {"project_id", "session_key", "turn_key", "last_sequence"},
    "turn_capabilities": {"capability_digest", "expires_at", "used_at", "revoked_at", "stop_retry"},
    "annotation_receipts": {"request_digest", "payload_digest", "retry_count"},
    "semantic_intervals": {"start_annotation_id", "end_annotation_id", "phase", "provenance"},
    "semantic_fact_staging": {"fact_kind", "observed_at", "provenance"},
}
