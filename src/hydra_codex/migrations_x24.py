"""Privacy-safe labels for reconciled task presentation."""

from __future__ import annotations


X24_DASHBOARD_PROJECTS_TABLE_SQL = """CREATE TABLE dashboard_projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    display_name_provenance TEXT CHECK(display_name_provenance IN ('config','repo_basename') OR display_name_provenance IS NULL),
    CHECK(display_name IS NULL OR (length(display_name) BETWEEN 1 AND 80))
) WITHOUT ROWID"""

X24_HOOK_SAFE_FACTS_TABLE_SQL = """CREATE TABLE hook_safe_facts (
    event_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK(event_kind IN ('prompt','post_tool','stop')),
    tool_category TEXT CHECK(tool_category IN ('shell','read','write','search','browser','other')),
    tool_status TEXT CHECK(tool_status IN ('success','failure','unknown')),
    duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000),
    observed_at TEXT NOT NULL
) WITHOUT ROWID"""

X24_MATERIALIZED_REPORT_SNAPSHOTS_TABLE_SQL = """CREATE TABLE materialized_report_snapshots (
    project_id TEXT NOT NULL,
    task_ref TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_markdown TEXT NOT NULL,
    report_html TEXT NOT NULL,
    reconciled_at TEXT NOT NULL,
    data_revision INTEGER NOT NULL,
    PRIMARY KEY(project_id,task_ref)
) WITHOUT ROWID"""


X24_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (40, (
        "ALTER TABLE annotations ADD COLUMN task_label TEXT",
        "ALTER TABLE reconciled_tasks ADD COLUMN display_name TEXT",
    )),
    (41, (
        "ALTER TABLE dashboard_projects ADD COLUMN display_name_provenance TEXT CHECK(display_name_provenance IN ('config','repo_basename') OR display_name_provenance IS NULL)",
        """CREATE TABLE hook_event_outbox (
            event_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            turn_key TEXT NOT NULL,
            event_kind TEXT NOT NULL CHECK(event_kind IN ('prompt','post_tool','stop')),
            tool_category TEXT CHECK(tool_category IN ('shell','read','write','search','browser','other')),
            tool_status TEXT CHECK(tool_status IN ('success','failure','unknown')),
            duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000),
            observed_at TEXT NOT NULL
        ) WITHOUT ROWID""",
        "CREATE INDEX hook_event_outbox_project ON hook_event_outbox(project_id,observed_at,event_kind)",
    )),
    (42, (
        "ALTER TABLE hook_event_outbox ADD COLUMN claimed_by TEXT",
        "ALTER TABLE hook_event_outbox ADD COLUMN claimed_at TEXT",
        "ALTER TABLE hook_event_outbox ADD COLUMN claim_expires_at TEXT",
        "ALTER TABLE hook_event_outbox ADD COLUMN acknowledged_at TEXT",
        "CREATE INDEX hook_event_outbox_pending ON hook_event_outbox(acknowledged_at,claim_expires_at,observed_at,event_key)",
        X24_HOOK_SAFE_FACTS_TABLE_SQL,
        "CREATE INDEX hook_safe_facts_project ON hook_safe_facts(project_id,observed_at,event_kind)",
        X24_MATERIALIZED_REPORT_SNAPSHOTS_TABLE_SQL,
        "CREATE INDEX materialized_report_snapshots_project ON materialized_report_snapshots(project_id,reconciled_at DESC,task_ref)",
    )),
)

X24_REQUIRED_SCHEMA = {
    "annotations": {"task_label"},
    "reconciled_tasks": {"display_name"},
    "dashboard_projects": {"display_name_provenance"},
    "hook_event_outbox": {"event_key", "project_id", "session_key", "turn_key", "event_kind", "tool_category", "tool_status", "duration_ms", "observed_at", "claimed_by", "claimed_at", "claim_expires_at", "acknowledged_at"},
    "hook_safe_facts": {"event_key", "project_id", "session_key", "turn_key", "event_kind", "tool_category", "tool_status", "duration_ms", "observed_at"},
    "materialized_report_snapshots": {"project_id", "task_ref", "report_json", "report_markdown", "report_html", "reconciled_at", "data_revision"},
}
