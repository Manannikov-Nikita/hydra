"""Persist honest pilot cohorts and immutable verification receipts."""

from __future__ import annotations


P16_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (32, (
        """CREATE TABLE pilot_runs (
               pilot_id TEXT PRIMARY KEY,
               project_id TEXT NOT NULL,
               started_at TEXT NOT NULL,
               closed_at TEXT,
               target INTEGER NOT NULL CHECK(target > 0),
               task_family TEXT NOT NULL,
               thresholds_json TEXT NOT NULL,
               state TEXT NOT NULL CHECK(state IN ('open','closed')),
               CHECK((state='open' AND closed_at IS NULL) OR
                     (state='closed' AND closed_at IS NOT NULL))
           )""",
        """CREATE UNIQUE INDEX pilot_runs_one_open_project
               ON pilot_runs(project_id) WHERE state='open'""",
        """CREATE TABLE pilot_tasks (
               pilot_id TEXT NOT NULL REFERENCES pilot_runs(pilot_id)
                   ON DELETE RESTRICT,
               task_ref TEXT NOT NULL,
               completed_at TEXT NOT NULL,
               task_family TEXT,
               scope_change TEXT NOT NULL,
               instrumented INTEGER NOT NULL CHECK(instrumented IN (0,1)),
               initial_missing INTEGER NOT NULL CHECK(initial_missing IN (0,1)),
               finish_missing INTEGER NOT NULL CHECK(finish_missing IN (0,1)),
               delivery_failures INTEGER NOT NULL CHECK(delivery_failures >= 0),
               semantic_conflicts INTEGER NOT NULL CHECK(semantic_conflicts >= 0),
               schema_diagnostics INTEGER NOT NULL CHECK(schema_diagnostics >= 0),
               coverage_value REAL,
               accepted_transport_events INTEGER NOT NULL
                   CHECK(accepted_transport_events >= 0),
               staging_latency_p95_ms INTEGER,
               trend_eligible INTEGER NOT NULL CHECK(trend_eligible IN (0,1)),
               task_input_digest TEXT NOT NULL,
               PRIMARY KEY(pilot_id,task_ref)
           )""",
        """CREATE TABLE pilot_receipts (
               receipt_id TEXT PRIMARY KEY,
               pilot_id TEXT NOT NULL UNIQUE REFERENCES pilot_runs(pilot_id)
                   ON DELETE RESTRICT,
               created_at TEXT NOT NULL,
               decision TEXT NOT NULL CHECK(decision IN ('verified','rejected')),
               task_refs_json TEXT NOT NULL,
               reconciliation_version INTEGER NOT NULL,
               schema_version INTEGER NOT NULL,
               thresholds_json TEXT NOT NULL,
               observed_facts_json TEXT NOT NULL,
               snapshot_digest TEXT NOT NULL,
               audit_sha256 TEXT NOT NULL
           )""",
        """CREATE TRIGGER pilot_receipts_immutable_update
               BEFORE UPDATE ON pilot_receipts BEGIN
                   SELECT RAISE(ABORT, 'pilot receipts are immutable');
               END""",
        """CREATE TRIGGER pilot_receipts_immutable_delete
               BEFORE DELETE ON pilot_receipts BEGIN
                   SELECT RAISE(ABORT, 'pilot receipts are immutable');
               END""",
    )),
)


P16_REQUIRED_SCHEMA = {
    "pilot_runs": {
        "pilot_id", "project_id", "started_at", "closed_at", "target",
        "task_family", "thresholds_json", "state",
    },
    "pilot_tasks": {
        "pilot_id", "task_ref", "completed_at", "task_family",
        "scope_change", "instrumented", "initial_missing", "finish_missing",
        "delivery_failures", "semantic_conflicts", "schema_diagnostics",
        "coverage_value", "accepted_transport_events",
        "staging_latency_p95_ms", "trend_eligible", "task_input_digest",
    },
    "pilot_receipts": {
        "receipt_id", "pilot_id", "created_at", "decision",
        "task_refs_json", "reconciliation_version", "schema_version",
        "thresholds_json", "observed_facts_json", "snapshot_digest",
        "audit_sha256",
    },
}
