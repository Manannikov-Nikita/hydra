"""Persisted deterministic task reconciliation schema."""

from __future__ import annotations


D4_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (20, (
        "ALTER TABLE reconciliation_runs ADD COLUMN reconciliation_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE reconciliation_runs ADD COLUMN input_digest TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE reconciliation_runs ADD COLUMN completed_at TEXT",
        "ALTER TABLE reconciliation_runs ADD COLUMN task_count INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE reconciled_tasks (
            project_id TEXT NOT NULL,
            root_key TEXT NOT NULL,
            public_ref TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('complete','incomplete')),
            cutoff_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            task_family TEXT,
            reconciliation_version INTEGER NOT NULL,
            input_digest TEXT NOT NULL,
            PRIMARY KEY(project_id,root_key),
            UNIQUE(project_id,public_ref)
        )""",
        "CREATE INDEX reconciled_tasks_recent ON reconciled_tasks(project_id,last_activity_at DESC,public_ref)",
        """CREATE TABLE reconciled_token_deltas (
            project_id TEXT NOT NULL,
            root_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            event_key TEXT NOT NULL,
            observed_at TEXT,
            ordinal INTEGER NOT NULL,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            reasoning_tokens INTEGER,
            working_tokens INTEGER,
            full_context INTEGER,
            provenance TEXT NOT NULL CHECK(provenance IN ('exact','derived','estimated')),
            phase TEXT,
            cause TEXT,
            PRIMARY KEY(project_id,root_key,session_key,event_key),
            FOREIGN KEY(project_id,root_key) REFERENCES reconciled_tasks(project_id,root_key)
                ON DELETE CASCADE
        )""",
        """CREATE TABLE reconciled_phase_metrics (
            project_id TEXT NOT NULL,
            root_key TEXT NOT NULL,
            phase TEXT NOT NULL,
            working_tokens INTEGER,
            working_lower_bound INTEGER NOT NULL,
            working_provenance TEXT NOT NULL CHECK(working_provenance IN ('derived','estimated')),
            full_context INTEGER,
            full_context_lower_bound INTEGER NOT NULL,
            full_context_provenance TEXT NOT NULL CHECK(full_context_provenance IN ('derived','estimated')),
            reasoning_tokens INTEGER,
            reasoning_lower_bound INTEGER NOT NULL,
            reasoning_provenance TEXT NOT NULL CHECK(reasoning_provenance IN ('derived','estimated')),
            PRIMARY KEY(project_id,root_key,phase),
            FOREIGN KEY(project_id,root_key) REFERENCES reconciled_tasks(project_id,root_key)
                ON DELETE CASCADE
        )""",
        """CREATE TABLE reconciled_semantic_summaries (
            project_id TEXT NOT NULL,
            root_key TEXT NOT NULL,
            classified_working INTEGER NOT NULL,
            unclassified_working INTEGER,
            unclassified_working_lower_bound INTEGER NOT NULL,
            unclassified_working_provenance TEXT NOT NULL
                CHECK(unclassified_working_provenance IN ('derived','estimated')),
            unclassified_full_context INTEGER,
            unclassified_full_context_lower_bound INTEGER NOT NULL,
            unclassified_full_context_provenance TEXT NOT NULL
                CHECK(unclassified_full_context_provenance IN ('derived','estimated')),
            unclassified_reasoning INTEGER,
            unclassified_reasoning_lower_bound INTEGER NOT NULL,
            unclassified_reasoning_provenance TEXT NOT NULL
                CHECK(unclassified_reasoning_provenance IN ('derived','estimated')),
            coverage_value REAL,
            coverage_provenance TEXT NOT NULL CHECK(coverage_provenance IN ('derived','estimated')),
            marker_count INTEGER NOT NULL,
            self_report_missing INTEGER NOT NULL,
            semantic_conflicts INTEGER NOT NULL,
            schema_diagnostics INTEGER NOT NULL,
            PRIMARY KEY(project_id,root_key),
            FOREIGN KEY(project_id,root_key) REFERENCES reconciled_tasks(project_id,root_key)
                ON DELETE CASCADE
        )""",
        """CREATE TABLE reconciled_task_diagnostics (
            project_id TEXT NOT NULL,
            root_key TEXT NOT NULL,
            diagnostic_code TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0),
            PRIMARY KEY(project_id,root_key,diagnostic_code),
            FOREIGN KEY(project_id,root_key) REFERENCES reconciled_tasks(project_id,root_key)
                ON DELETE CASCADE
        )""",
    )),
)


D4_REQUIRED_SCHEMA = {
    "reconciliation_runs": {
        "run_id", "project_id", "started_at", "outcome", "provenance",
        "reconciliation_version", "input_digest", "completed_at", "task_count",
    },
    "reconciled_tasks": {
        "project_id", "root_key", "public_ref", "status", "cutoff_at",
        "last_activity_at", "task_family", "reconciliation_version", "input_digest",
    },
    "reconciled_token_deltas": {
        "project_id", "root_key", "session_key", "event_key", "observed_at", "ordinal",
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
        "working_tokens", "full_context", "provenance", "phase", "cause",
    },
    "reconciled_phase_metrics": {
        "phase", "working_tokens", "working_lower_bound", "working_provenance",
        "full_context", "full_context_lower_bound", "full_context_provenance",
        "reasoning_tokens", "reasoning_lower_bound", "reasoning_provenance",
    },
    "reconciled_semantic_summaries": {
        "classified_working", "unclassified_working", "coverage_value",
        "coverage_provenance", "marker_count", "self_report_missing",
        "semantic_conflicts", "schema_diagnostics",
        "unclassified_working_lower_bound", "unclassified_working_provenance",
        "unclassified_full_context", "unclassified_full_context_lower_bound",
        "unclassified_full_context_provenance", "unclassified_reasoning",
        "unclassified_reasoning_lower_bound", "unclassified_reasoning_provenance",
    },
    "reconciled_task_diagnostics": {
        "project_id", "root_key", "diagnostic_code", "occurrence_count",
    },
}
