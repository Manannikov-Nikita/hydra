"""B2 rollout lineage and reconciliation schema additions."""

from __future__ import annotations


B2_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (14, (
        "ALTER TABLE rollout_sources ADD COLUMN logical_source_key TEXT",
        "ALTER TABLE rollout_sources ADD COLUMN relation TEXT NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE rollout_sources ADD COLUMN line_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE rollout_sources ADD COLUMN byte_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE rollout_sources ADD COLUMN chain_digest TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE rollout_sources ADD COLUMN materialized INTEGER NOT NULL DEFAULT 1",
        "UPDATE rollout_sources SET logical_source_key = source_digest WHERE logical_source_key IS NULL",
        """CREATE TABLE rollout_logical_sources (
            logical_source_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_key TEXT,
            canonical_revision_digest TEXT,
            lineage_state TEXT NOT NULL CHECK(lineage_state IN ('clean','conflicted'))
        )""",
        """INSERT INTO rollout_logical_sources(
               logical_source_key, project_id, session_key, canonical_revision_digest, lineage_state)
           SELECT source_digest, 'legacy-unresolved', NULL, source_digest, 'conflicted' FROM rollout_sources""",
        "ALTER TABLE rollout_source_locations RENAME TO rollout_source_locations_v13",
        """CREATE TABLE rollout_source_locations (
            logical_source_key TEXT NOT NULL REFERENCES rollout_logical_sources(logical_source_key),
            location_key TEXT NOT NULL,
            location_type TEXT NOT NULL CHECK(location_type IN ('active','archived','explicit')),
            revision_digest TEXT NOT NULL REFERENCES rollout_sources(source_digest),
            PRIMARY KEY(logical_source_key, location_key)
        )""",
        """INSERT INTO rollout_source_locations(logical_source_key,location_key,location_type,revision_digest)
           SELECT source_digest,location_key,
                  CASE location_type WHEN 'active' THEN 'active' WHEN 'archived' THEN 'archived' ELSE 'explicit' END,
                  source_digest FROM rollout_source_locations_v13""",
        "DROP TABLE rollout_source_locations_v13",
        """CREATE TABLE rollout_revision_lines (
            revision_digest TEXT NOT NULL REFERENCES rollout_sources(source_digest),
            line_number INTEGER NOT NULL,
            line_fingerprint TEXT NOT NULL,
            PRIMARY KEY(revision_digest,line_number)
        )""",
        """CREATE TABLE rollout_events (
            event_key TEXT PRIMARY KEY,
            logical_source_key TEXT NOT NULL REFERENCES rollout_logical_sources(logical_source_key),
            source_ordinal INTEGER NOT NULL,
            envelope_kind TEXT NOT NULL,
            observed_at TEXT,
            timestamp_quality TEXT NOT NULL CHECK(timestamp_quality IN ('valid','missing','invalid')),
            fingerprint TEXT NOT NULL
        )""",
        """CREATE TABLE rollout_revision_events (
            revision_digest TEXT NOT NULL REFERENCES rollout_sources(source_digest),
            event_key TEXT NOT NULL REFERENCES rollout_events(event_key),
            source_ordinal INTEGER NOT NULL,
            PRIMARY KEY(revision_digest,source_ordinal)
        )""",
        """CREATE TABLE rollout_session_segments (
            session_key TEXT NOT NULL REFERENCES rollout_sessions(session_key),
            logical_source_key TEXT NOT NULL REFERENCES rollout_logical_sources(logical_source_key),
            PRIMARY KEY(session_key,logical_source_key)
        )""",
        "ALTER TABLE rollout_sessions ADD COLUMN started_at TEXT",
        "ALTER TABLE rollout_sessions ADD COLUMN last_activity_at TEXT",
        """CREATE TABLE turn_lifecycle_events (
            event_key TEXT PRIMARY KEY REFERENCES rollout_events(event_key),
            session_key TEXT NOT NULL REFERENCES rollout_sessions(session_key),
            turn_key TEXT NOT NULL,
            event_kind TEXT NOT NULL CHECK(event_kind IN ('started','completed','aborted')),
            observed_at TEXT,
            timestamp_epoch REAL,
            emitted_duration_ms INTEGER,
            source_digest TEXT NOT NULL REFERENCES rollout_sources(source_digest),
            logical_source_key TEXT NOT NULL REFERENCES rollout_logical_sources(logical_source_key),
            source_ordinal INTEGER NOT NULL
        )""",
    )),
    (15, (
        "ALTER TABLE turn_attempts ADD COLUMN timing_provenance TEXT NOT NULL DEFAULT 'derived'",
    )),
    (16, (
        "ALTER TABLE turn_attempts RENAME TO turn_attempts_v15",
        """CREATE TABLE turn_attempts (
            session_key TEXT NOT NULL REFERENCES rollout_sessions(session_key),
            turn_key TEXT NOT NULL,
            attempt_ordinal INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('open','completed','aborted')),
            emitted_duration_ms INTEGER,
            wall_duration_ms INTEGER CHECK(wall_duration_ms IS NULL OR wall_duration_ms >= 0),
            started_at TEXT,
            finished_at TEXT,
            timing_provenance TEXT NOT NULL CHECK(timing_provenance IN ('exact','derived','estimated','model_reported')),
            PRIMARY KEY(session_key,turn_key,attempt_ordinal)
        )""",
        """INSERT INTO turn_attempts(
               session_key,turn_key,attempt_ordinal,state,emitted_duration_ms,wall_duration_ms,
               started_at,finished_at,timing_provenance)
           SELECT session_key,turn_key,attempt_ordinal,state,emitted_duration_ms,wall_duration_ms,
                  started_at,finished_at,timing_provenance FROM turn_attempts_v15""",
        "DROP TABLE turn_attempts_v15",
    )),
)
