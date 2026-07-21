"""Codex App Server and OTLP event persistence schema."""

from __future__ import annotations


E5_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (21, (
        "ALTER TABLE token_snapshots ADD COLUMN source_family TEXT NOT NULL DEFAULT 'rollout' CHECK(source_family IN ('rollout','app_server','otel'))",
        "ALTER TABLE token_snapshots ADD COLUMN counter_scope TEXT NOT NULL DEFAULT 'thread_total'",
        "ALTER TABLE token_snapshots ADD COLUMN event_key TEXT",
        "ALTER TABLE token_snapshots ADD COLUMN contributes_total INTEGER NOT NULL DEFAULT 1 CHECK(contributes_total IN (0,1))",
        "ALTER TABLE token_snapshots ADD COLUMN selection_provenance TEXT NOT NULL DEFAULT 'exact' CHECK(selection_provenance IN ('exact','derived','estimated'))",
        "ALTER TABLE token_snapshots ADD COLUMN selection_caveat TEXT",
        "CREATE UNIQUE INDEX token_snapshots_event_key ON token_snapshots(event_key) WHERE event_key IS NOT NULL",
        """CREATE TABLE codex_event_sources (
            source_digest TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            source_format TEXT NOT NULL CHECK(source_format IN ('app_server','otel')),
            line_count INTEGER NOT NULL CHECK(line_count >= 0),
            byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
            provenance TEXT NOT NULL DEFAULT 'exact' CHECK(provenance='exact')
        )""",
        """CREATE TABLE codex_event_source_locations (
            source_digest TEXT NOT NULL REFERENCES codex_event_sources(source_digest),
            location_key TEXT NOT NULL,
            PRIMARY KEY(source_digest,location_key)
        )""",
        """CREATE TABLE codex_events (
            source_digest TEXT NOT NULL REFERENCES codex_event_sources(source_digest),
            source_ordinal INTEGER NOT NULL CHECK(source_ordinal > 0),
            event_key TEXT NOT NULL,
            project_id TEXT NOT NULL,
            source_format TEXT NOT NULL CHECK(source_format IN ('app_server','otel')),
            schema_version TEXT NOT NULL,
            event_type TEXT NOT NULL,
            observed_at TEXT,
            observed_at_ns INTEGER,
            session_key TEXT,
            turn_key TEXT,
            duration_ms INTEGER,
            status TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK(provenance IN ('exact','derived','estimated')),
            tool_call_key TEXT,
            tool_name TEXT,
            tool_category TEXT,
            tool_phase TEXT,
            tool_status TEXT,
            tool_duration_ms INTEGER,
            tool_exit_status INTEGER,
            PRIMARY KEY(source_digest,source_ordinal)
        )""",
        "CREATE INDEX codex_events_session_time ON codex_events(project_id,session_key,observed_at,source_ordinal)",
        """CREATE TABLE codex_event_contents (
            source_digest TEXT NOT NULL,
            source_ordinal INTEGER NOT NULL,
            field TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            characters INTEGER NOT NULL CHECK(characters >= 0),
            utf8_bytes INTEGER NOT NULL CHECK(utf8_bytes >= 0),
            PRIMARY KEY(source_digest,source_ordinal,field,content_digest),
            FOREIGN KEY(source_digest,source_ordinal)
                REFERENCES codex_events(source_digest,source_ordinal) ON DELETE CASCADE
        )""",
        """CREATE TABLE codex_event_tokens (
            token_key TEXT PRIMARY KEY,
            source_digest TEXT NOT NULL,
            source_ordinal INTEGER NOT NULL,
            session_key TEXT,
            turn_key TEXT,
            observed_at TEXT,
            counter_scope TEXT NOT NULL,
            cumulative INTEGER NOT NULL CHECK(cumulative IN (0,1)),
            input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
            cached_input_tokens INTEGER NOT NULL CHECK(cached_input_tokens >= 0),
            output_tokens INTEGER NOT NULL CHECK(output_tokens >= 0),
            reasoning_tokens INTEGER NOT NULL CHECK(reasoning_tokens >= 0),
            reported_total_tokens INTEGER,
            source_family TEXT NOT NULL CHECK(source_family IN ('app_server','otel')),
            provenance TEXT NOT NULL CHECK(provenance IN ('exact','derived','estimated')),
            FOREIGN KEY(source_digest,source_ordinal)
                REFERENCES codex_events(source_digest,source_ordinal) ON DELETE CASCADE
        )""",
        """CREATE TABLE codex_event_issues (
            source_digest TEXT NOT NULL REFERENCES codex_event_sources(source_digest),
            source_ordinal INTEGER NOT NULL CHECK(source_ordinal > 0),
            event_key TEXT NOT NULL,
            issue_code TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'exact' CHECK(provenance='exact'),
            PRIMARY KEY(source_digest,source_ordinal,event_key,issue_code)
        )""",
    )),
)


E5_REQUIRED_SCHEMA = {
    "token_snapshots": {
        "source_family", "counter_scope", "event_key", "contributes_total",
        "selection_provenance", "selection_caveat",
    },
    "codex_event_sources": {
        "source_digest", "project_id", "schema_version", "source_format",
        "line_count", "byte_count", "provenance",
    },
    "codex_event_source_locations": {"source_digest", "location_key"},
    "codex_events": {
        "source_digest", "source_ordinal", "event_key", "project_id",
        "source_format", "schema_version", "event_type", "observed_at",
        "observed_at_ns", "session_key", "turn_key", "duration_ms", "status",
        "provenance", "tool_call_key", "tool_name", "tool_category", "tool_phase",
        "tool_status", "tool_duration_ms", "tool_exit_status",
    },
    "codex_event_contents": {
        "source_digest", "source_ordinal", "field", "content_digest",
        "characters", "utf8_bytes",
    },
    "codex_event_tokens": {
        "token_key", "source_digest", "source_ordinal", "session_key", "turn_key",
        "observed_at", "counter_scope", "cumulative", "input_tokens",
        "cached_input_tokens", "output_tokens", "reasoning_tokens",
        "reported_total_tokens", "source_family", "provenance",
    },
    "codex_event_issues": {
        "source_digest", "source_ordinal", "event_key", "issue_code", "provenance",
    },
}
