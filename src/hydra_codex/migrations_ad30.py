"""Schemas 48-50: project-scoped revisions and exact compatibility recovery."""

from __future__ import annotations

from collections.abc import Callable
import sqlite3


_MAX_REVISION = 9_223_372_036_854_775_807
_MIGRATION_BASELINE = "1970-01-01T00:00:00Z"


AD30_PROJECT_SOURCE_FACT_REVISIONS_TABLE_SQL = """CREATE TABLE sync_project_source_fact_revisions (
    project_id TEXT PRIMARY KEY
        CHECK(
            typeof(project_id)='text'
            AND length(project_id) BETWEEN 1 AND 160
        ),
    revision INTEGER NOT NULL
        CHECK(typeof(revision)='integer' AND revision >= 0)
) WITHOUT ROWID"""

AD30_LEGACY_SOURCE_FACT_REVISIONS_TABLE_SQL = """CREATE TABLE sync_source_fact_revisions (
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('project','unattributed')),
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL
        CHECK(typeof(revision) = 'integer' AND revision >= 0),
    PRIMARY KEY(scope_kind,project_id),
    CHECK(
        (scope_kind='project' AND length(project_id) BETWEEN 1 AND 160)
        OR (scope_kind='unattributed' AND project_id='')
    )
) WITHOUT ROWID"""

AD30_UNATTRIBUTED_SOURCE_FACT_REVISION_TABLE_SQL = """CREATE TABLE sync_unattributed_source_fact_revision (
    singleton INTEGER PRIMARY KEY
        CHECK(typeof(singleton)='integer' AND singleton=1),
    revision INTEGER NOT NULL
        CHECK(typeof(revision)='integer' AND revision >= 0)
) WITHOUT ROWID"""

AD30_UNATTRIBUTED_SOURCE_FACT_REVISION_SEED_SQL = """INSERT INTO sync_unattributed_source_fact_revision(
    singleton,revision
) VALUES (1,0)"""

AD30_PROJECT_RECONCILE_FENCES_TABLE_SQL = """CREATE TABLE sync_project_reconcile_fences (
    project_id TEXT PRIMARY KEY
        CHECK(
            typeof(project_id)='text'
            AND length(project_id) BETWEEN 1 AND 160
        ),
    project_revision INTEGER NOT NULL
        CHECK(typeof(project_revision)='integer' AND project_revision >= 0),
    unattributed_revision INTEGER NOT NULL
        CHECK(typeof(unattributed_revision)='integer' AND unattributed_revision >= 0),
    storage_schema_version INTEGER NOT NULL
        CHECK(typeof(storage_schema_version)='integer' AND storage_schema_version >= 0),
    storage_schema_cookie INTEGER NOT NULL
        CHECK(typeof(storage_schema_cookie)='integer' AND storage_schema_cookie >= 0),
    reconciliation_version INTEGER NOT NULL
        CHECK(typeof(reconciliation_version)='integer' AND reconciliation_version >= 0),
    input_digest TEXT NOT NULL
        CHECK(
            typeof(input_digest)='text'
            AND length(input_digest)=64
            AND input_digest NOT GLOB '*[^0-9a-f]*'
        )
) WITHOUT ROWID"""

AD30_RECONCILIATION_RUNS_SOURCE_FENCE_INDEX_SQL = """CREATE INDEX reconciliation_runs_source_fence_lookup
ON reconciliation_runs(project_id,outcome,reconciliation_version,input_digest)"""

AD30_RECONCILE_EXISTING_PROJECTS_SQL = f"""INSERT INTO sync_dirty_roots(
    project_id,root_key,root_kind,observed_at,claim_owner,claim_expires_at,
    eligible_epoch_ns,claim_token
)
SELECT project_id,project_id,'project','{_MIGRATION_BASELINE}',NULL,NULL,0,NULL
  FROM (
      SELECT project_id FROM dashboard_projects
      UNION
      SELECT project_id FROM materialized_project_stats
      UNION
      SELECT project_id FROM reconciliation_runs WHERE outcome='success'
      UNION
      SELECT project_id FROM sync_project_source_fact_revisions
      UNION
      SELECT project_id FROM sync_project_reconcile_fences
  ) AS known_projects
 WHERE typeof(project_id)='text'
   AND length(project_id) BETWEEN 1 AND 160
ON CONFLICT(project_id,root_key,root_kind) DO NOTHING"""

AD30_RECONCILE_EXISTING_PROJECTS_REVISION_SQL = f"""UPDATE sync_data_revision
   SET revision=revision+1,
       updated_at=MAX(updated_at,'{_MIGRATION_BASELINE}')
 WHERE singleton=1
   AND revision < {_MAX_REVISION}"""

# Kept as a source-compatible alias while callers move from the composite
# revision table to the exact project-scoped table.
AD30_SOURCE_FACT_REVISIONS_TABLE_SQL = (
    AD30_PROJECT_SOURCE_FACT_REVISIONS_TABLE_SQL
)
AD30_RECONCILE_FENCES_TABLE_SQL = AD30_PROJECT_RECONCILE_FENCES_TABLE_SQL


def _union(*mappings: str) -> str:
    return "SELECT project_id FROM (" + " UNION ".join(mappings) + ")"


def _direct(row: str) -> str:
    return f"SELECT {row}.project_id AS project_id"


def _session(row: str, column: str = "session_key") -> str:
    return (
        "SELECT project_id FROM rollout_sessions "
        f"WHERE session_key={row}.{column}"
    )


def _rollout_logical(row: str, column: str = "logical_source_key") -> str:
    return (
        "SELECT project_id FROM rollout_logical_sources "
        f"WHERE logical_source_key={row}.{column}"
    )


def _rollout_source(row: str, column: str = "source_digest") -> str:
    return (
        "SELECT logical.project_id "
        "FROM rollout_sources AS source "
        "JOIN rollout_logical_sources AS logical "
        "  ON logical.logical_source_key=source.logical_source_key "
        f"WHERE source.source_digest={row}.{column}"
    )


def _codex_source(row: str, column: str = "source_digest") -> str:
    return (
        "SELECT project_id FROM codex_event_sources "
        f"WHERE source_digest={row}.{column}"
    )


def _any_source(row: str, column: str = "source_digest") -> str:
    return _union(
        _rollout_source(row, column),
        _codex_source(row, column),
    )


def _rollout_event(row: str, column: str = "event_key") -> str:
    return (
        "SELECT logical.project_id "
        "FROM rollout_events AS event "
        "JOIN rollout_logical_sources AS logical "
        "  ON logical.logical_source_key=event.logical_source_key "
        f"WHERE event.event_key={row}.{column}"
    )


def _session_and_source(
    row: str,
    session_column: str = "session_key",
    source_column: str = "source_digest",
) -> str:
    return _union(
        _session(row, session_column),
        _any_source(row, source_column),
    )


def _tool_span_role(row: str) -> str:
    return _union(
        _session(row),
        (
            "SELECT session.project_id "
            "FROM tool_spans AS span "
            "JOIN rollout_sessions AS session "
            "  ON session.session_key=span.session_key "
            f"WHERE span.session_key={row}.session_key "
            f"  AND span.call_key={row}.call_key"
        ),
        (
            "SELECT logical.project_id "
            "FROM tool_spans AS span "
            "JOIN rollout_sources AS source "
            "  ON source.source_digest=span.source_digest "
            "JOIN rollout_logical_sources AS logical "
            "  ON logical.logical_source_key=source.logical_source_key "
            f"WHERE span.session_key={row}.session_key "
            f"  AND span.call_key={row}.call_key"
        ),
        (
            "SELECT codex.project_id "
            "FROM tool_spans AS span "
            "JOIN codex_event_sources AS codex "
            "  ON codex.source_digest=span.source_digest "
            f"WHERE span.session_key={row}.session_key "
            f"  AND span.call_key={row}.call_key"
        ),
    )


def _pilot(row: str) -> str:
    return (
        "SELECT project_id FROM pilot_runs "
        f"WHERE pilot_id={row}.pilot_id"
    )


def _revision_event(row: str) -> str:
    return _union(
        _rollout_source(row, "revision_digest"),
        _rollout_event(row),
    )


def _session_edge(row: str) -> str:
    return _union(
        _session(row, "child_key"),
        _session(row, "parent_key"),
    )


def _turn_attempt(row: str) -> str:
    return _union(
        _session(row),
        _rollout_logical(row, "started_logical_source_key"),
        _rollout_logical(row, "terminal_logical_source_key"),
    )


def _turn_lifecycle(row: str) -> str:
    return _union(
        _session(row),
        _rollout_logical(row),
        _rollout_source(row),
    )


AD30_SOURCE_FACT_PROJECT_SELECTORS: dict[str, Callable[[str], str]] = {
    "annotation_transport_events": _direct,
    "annotations": _direct,
    "codex_event_issues": _codex_source,
    "codex_event_sources": _direct,
    "codex_events": _direct,
    "file_observation_candidates": _session_and_source,
    "file_observations": _session_and_source,
    "fork_baselines": lambda row: _session_and_source(row, "child_key"),
    "hook_safe_facts": _direct,
    "lineage_claim_candidates": _direct,
    "pilot_receipts": _pilot,
    "pilot_runs": _direct,
    "pilot_tasks": _pilot,
    "rollout_diagnostics": _rollout_source,
    "rollout_events": _rollout_logical,
    "rollout_logical_sources": _direct,
    "rollout_revision_events": _revision_event,
    "rollout_sessions": _direct,
    "rollout_sources": _rollout_logical,
    "rollout_test_runs": _session_and_source,
    "semantic_conflicts": _any_source,
    "semantic_fact_staging": _direct,
    "semantic_intervals": _direct,
    "session_edges": _session_edge,
    "test_evidence_candidates": _session_and_source,
    "token_snapshots": _direct,
    "tool_span_candidates": _session_and_source,
    "tool_span_roles": _tool_span_role,
    "tool_spans": _session_and_source,
    "trusted_turn_bindings": _direct,
    "turn_attempts": _turn_attempt,
    "turn_lifecycle_events": _turn_lifecycle,
}

AD30_LEGACY_SOURCE_FACT_PROJECT_SELECTORS: dict[
    str, Callable[[str], str]
] = {
    "annotation_transport_events": _direct,
    "annotations": _direct,
    "codex_event_issues": _codex_source,
    "codex_event_sources": _direct,
    "codex_events": _direct,
    "file_observation_candidates": _session,
    "file_observations": _session,
    "fork_baselines": lambda row: _session(row, "child_key"),
    "hook_safe_facts": _direct,
    "lineage_claim_candidates": _direct,
    "pilot_receipts": _pilot,
    "pilot_runs": _direct,
    "pilot_tasks": _pilot,
    "rollout_diagnostics": _rollout_source,
    "rollout_events": _rollout_logical,
    "rollout_logical_sources": _direct,
    "rollout_revision_events":
        lambda row: _rollout_source(row, "revision_digest"),
    "rollout_sessions": _direct,
    "rollout_sources": _rollout_logical,
    "rollout_test_runs": _session,
    "semantic_conflicts": _rollout_source,
    "semantic_fact_staging": _direct,
    "semantic_intervals": _direct,
    "session_edges": lambda row: _session(row, "child_key"),
    "test_evidence_candidates": _session,
    "token_snapshots": _direct,
    "tool_span_candidates": _session,
    "tool_span_roles": _session,
    "tool_spans": _session,
    "trusted_turn_bindings": _direct,
    "turn_attempts": _session,
    "turn_lifecycle_events": _session,
}

AD30_SOURCE_FACT_TABLES = tuple(sorted(AD30_SOURCE_FACT_PROJECT_SELECTORS))


def _mapping_sql(
    table: str,
    operation: str,
    selectors: dict[str, Callable[[str], str]] | None = None,
) -> str:
    mappings = _operation_mappings(table, operation, selectors)
    if len(mappings) == 1:
        return mappings[0]
    return _union(*mappings)


def _operation_mappings(
    table: str,
    operation: str,
    selectors: dict[str, Callable[[str], str]] | None = None,
) -> tuple[str, ...]:
    selector = (
        AD30_SOURCE_FACT_PROJECT_SELECTORS
        if selectors is None
        else selectors
    )[table]
    if operation == "UPDATE":
        return selector("OLD"), selector("NEW")
    return (selector("NEW" if operation == "INSERT" else "OLD"),)


def _valid_mapping(mapping: str) -> str:
    return (
        f"SELECT 1 FROM ({mapping}) AS mapped "
        "WHERE typeof(project_id)='text' "
        "AND length(project_id) BETWEEN 1 AND 160"
    )


def _bump_expression() -> str:
    return (
        "CASE WHEN revision="
        f"{_MAX_REVISION} "
        "THEN RAISE(ABORT,'source fact revision exhausted') "
        "ELSE revision+1 END"
    )


def _revision_trigger_sql(table: str, operation: str) -> str:
    timing = "AFTER" if operation == "INSERT" else "BEFORE"
    mapping = _mapping_sql(table, operation)
    missing_mapping = " OR ".join(
        f"NOT EXISTS ({_valid_mapping(item)})"
        for item in _operation_mappings(table, operation)
    )
    bump = _bump_expression()
    return f"""CREATE TRIGGER source_fact_revision_{table}_{operation.lower()}
{timing} {operation} ON {table}
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM sync_unattributed_source_fact_revision
             WHERE singleton=1
        )
        THEN RAISE(ABORT,'unattributed source fact revision missing')
    END;
    INSERT INTO sync_project_source_fact_revisions(project_id,revision)
    SELECT DISTINCT project_id,1
      FROM ({mapping}) AS mapped
     WHERE typeof(project_id)='text'
       AND length(project_id) BETWEEN 1 AND 160
    ON CONFLICT(project_id) DO UPDATE SET
        revision={bump};
    UPDATE sync_unattributed_source_fact_revision
       SET revision={bump}
     WHERE singleton=1
       AND ({missing_mapping});
    UPDATE sync_data_revision
       SET revision={bump}
     WHERE singleton=1;
    SELECT CASE
        WHEN changes()!=1
        THEN RAISE(ABORT,'data revision unavailable')
    END;
END"""


def _legacy_revision_trigger_sql(table: str, operation: str) -> str:
    timing = "AFTER" if operation == "INSERT" else "BEFORE"
    mapping = _mapping_sql(
        table, operation, AD30_LEGACY_SOURCE_FACT_PROJECT_SELECTORS,
    )
    bump = _bump_expression()
    return f"""CREATE TRIGGER source_fact_revision_{table}_{operation.lower()}
{timing} {operation} ON {table}
BEGIN
    INSERT INTO sync_source_fact_revisions(scope_kind,project_id,revision)
    SELECT 'project',project_id,1
      FROM ({mapping}) AS mapped
     WHERE typeof(project_id)='text'
       AND length(project_id) BETWEEN 1 AND 160
    ON CONFLICT(scope_kind,project_id) DO UPDATE SET
        revision={bump};
    INSERT INTO sync_source_fact_revisions(scope_kind,project_id,revision)
    SELECT 'unattributed','',1
     WHERE NOT EXISTS (
        SELECT 1 FROM ({mapping}) AS mapped
         WHERE typeof(project_id)='text'
           AND length(project_id) BETWEEN 1 AND 160
     )
    ON CONFLICT(scope_kind,project_id) DO UPDATE SET
        revision={bump};
END"""


AD30_REQUIRED_TRIGGER_SQL = {
    f"source_fact_revision_{table}_{operation.lower()}":
        _revision_trigger_sql(table, operation)
    for table in AD30_SOURCE_FACT_TABLES
    for operation in ("INSERT", "UPDATE", "DELETE")
}

AD30_LEGACY_REQUIRED_TRIGGER_SQL = {
    f"source_fact_revision_{table}_{operation.lower()}":
        _legacy_revision_trigger_sql(table, operation)
    for table in AD30_SOURCE_FACT_TABLES
    for operation in ("INSERT", "UPDATE", "DELETE")
}

AD30_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        48,
        (
            AD30_PROJECT_SOURCE_FACT_REVISIONS_TABLE_SQL,
            AD30_UNATTRIBUTED_SOURCE_FACT_REVISION_TABLE_SQL,
            AD30_UNATTRIBUTED_SOURCE_FACT_REVISION_SEED_SQL,
            AD30_PROJECT_RECONCILE_FENCES_TABLE_SQL,
            AD30_RECONCILIATION_RUNS_SOURCE_FENCE_INDEX_SQL,
            *AD30_REQUIRED_TRIGGER_SQL.values(),
        ),
    ),
    (49, ()),
    (50, ()),
)

AD30_REQUIRED_SCHEMA = {
    "sync_project_source_fact_revisions": {
        "project_id",
        "revision",
    },
    "sync_unattributed_source_fact_revision": {
        "singleton",
        "revision",
    },
    "sync_project_reconcile_fences": {
        "project_id",
        "project_revision",
        "unattributed_revision",
        "storage_schema_version",
        "storage_schema_cookie",
        "reconciliation_version",
        "input_digest",
    },
}


_FINAL_TABLE_SQL = {
    "sync_project_source_fact_revisions":
        AD30_PROJECT_SOURCE_FACT_REVISIONS_TABLE_SQL,
    "sync_unattributed_source_fact_revision":
        AD30_UNATTRIBUTED_SOURCE_FACT_REVISION_TABLE_SQL,
    "sync_project_reconcile_fences":
        AD30_PROJECT_RECONCILE_FENCES_TABLE_SQL,
}
_LEGACY_TABLE = "sync_source_fact_revisions"
_SOURCE_REVISION_TABLE_PREFIXES = (
    "sync_source_fact_revision",
    "sync_project_source_fact_revision",
    "sync_unattributed_source_fact_revision",
    "sync_project_reconcile_fence",
)
_SOURCE_REVISION_INDEX_PREFIX = "reconciliation_runs_source_fence_"


def _normalized_schema_sql(statement: str) -> str:
    return " ".join(statement.casefold().split()).rstrip(";")


def _schema_objects(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str | None], ...]:
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            None if row[3] is None else str(row[3]),
        )
        for row in connection.execute(
            """SELECT type,name,tbl_name,sql
                 FROM sqlite_master
                WHERE type IN ('table','index','trigger')"""
        )
    )


def _exact_trigger_inventory(
    objects: tuple[tuple[str, str, str, str | None], ...],
    expected: dict[str, str],
) -> bool:
    protected_tables = (*_FINAL_TABLE_SQL, _LEGACY_TABLE, "sync_data_revision")
    actual = {
        name: sql
        for object_type, name, _table, sql in objects
        if object_type == "trigger"
        and sql is not None
        and (
            name.startswith("source_fact_revision_")
            or any(
                table in _normalized_schema_sql(sql)
                for table in protected_tables
            )
        )
    }
    return (
        set(actual) == set(expected)
        and all(
            _normalized_schema_sql(actual[name])
            == _normalized_schema_sql(statement)
            for name, statement in expected.items()
        )
    )


def _relevant_explicit_indexes(
    objects: tuple[tuple[str, str, str, str | None], ...],
) -> dict[str, str]:
    protected_tables = {*_FINAL_TABLE_SQL, _LEGACY_TABLE}
    return {
        name: sql
        for object_type, name, table, sql in objects
        if object_type == "index"
        and sql is not None
        and (
            table in protected_tables
            or name.startswith(_SOURCE_REVISION_INDEX_PREFIX)
        )
    }


def _classify_v48_source_revision_shape(
    connection: sqlite3.Connection,
) -> str:
    """Recognize only the final/legacy layouts observed at v48 or v49."""
    objects = _schema_objects(connection)
    relevant_tables = {
        name: sql
        for object_type, name, _table, sql in objects
        if object_type == "table"
        and any(name.startswith(prefix) for prefix in _SOURCE_REVISION_TABLE_PREFIXES)
    }
    final_names = set(_FINAL_TABLE_SQL)
    actual_names = set(relevant_tables)
    explicit_indexes = _relevant_explicit_indexes(objects)
    expected_index = {
        "reconciliation_runs_source_fence_lookup":
            AD30_RECONCILIATION_RUNS_SOURCE_FENCE_INDEX_SQL,
    }

    if actual_names == final_names:
        exact_tables = all(
            sql is not None
            and _normalized_schema_sql(sql)
            == _normalized_schema_sql(_FINAL_TABLE_SQL[name])
            for name, sql in relevant_tables.items()
        )
        exact_indexes = (
            set(explicit_indexes) == set(expected_index)
            and all(
                _normalized_schema_sql(explicit_indexes[name])
                == _normalized_schema_sql(statement)
                for name, statement in expected_index.items()
            )
        )
        if (
            exact_tables
            and exact_indexes
            and _exact_trigger_inventory(objects, AD30_REQUIRED_TRIGGER_SQL)
        ):
            fallback = connection.execute(
                """SELECT revision
                     FROM sync_unattributed_source_fact_revision
                    WHERE singleton=1"""
            ).fetchall()
            if len(fallback) == 1:
                return "final"
        raise sqlite3.IntegrityError(
            "unsupported final v48 source-fact revision shape"
        )

    if actual_names == {_LEGACY_TABLE}:
        legacy_sql = relevant_tables[_LEGACY_TABLE]
        exact_table = (
            legacy_sql is not None
            and _normalized_schema_sql(legacy_sql)
            == _normalized_schema_sql(AD30_LEGACY_SOURCE_FACT_REVISIONS_TABLE_SQL)
        )
        fallback = connection.execute(
            """SELECT revision FROM sync_source_fact_revisions
                WHERE scope_kind='unattributed' AND project_id=''"""
        ).fetchall()
        if (
            exact_table
            and not explicit_indexes
            and len(fallback) == 1
            and _exact_trigger_inventory(
                objects, AD30_LEGACY_REQUIRED_TRIGGER_SQL,
            )
        ):
            return "legacy"
        raise sqlite3.IntegrityError(
            "unsupported intermediate v48 source-fact revision shape"
        )

    raise sqlite3.IntegrityError(
        "mixed or incomplete v48 source-fact revision shape"
    )


def _convert_legacy_source_revisions(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(AD30_PROJECT_SOURCE_FACT_REVISIONS_TABLE_SQL)
    connection.execute(AD30_UNATTRIBUTED_SOURCE_FACT_REVISION_TABLE_SQL)
    connection.execute(AD30_PROJECT_RECONCILE_FENCES_TABLE_SQL)
    connection.execute(AD30_RECONCILIATION_RUNS_SOURCE_FENCE_INDEX_SQL)
    connection.execute(
        """INSERT INTO sync_project_source_fact_revisions(
               project_id,revision
           )
           SELECT project_id,revision
             FROM sync_source_fact_revisions
            WHERE scope_kind='project'"""
    )
    connection.execute(
        """INSERT INTO sync_unattributed_source_fact_revision(
               singleton,revision
           )
           SELECT 1,revision
             FROM sync_source_fact_revisions
            WHERE scope_kind='unattributed' AND project_id=''"""
    )
    for name in AD30_LEGACY_REQUIRED_TRIGGER_SQL:
        connection.execute(f"DROP TRIGGER {name}")
    connection.execute("DROP TABLE sync_source_fact_revisions")
    for statement in AD30_REQUIRED_TRIGGER_SQL.values():
        connection.execute(statement)


def _seed_existing_project_reconciliation(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(AD30_RECONCILE_EXISTING_PROJECTS_SQL)
    inserted = int(connection.execute("SELECT changes()").fetchone()[0])
    if inserted == 0:
        return
    connection.execute(AD30_RECONCILE_EXISTING_PROJECTS_REVISION_SQL)
    if int(connection.execute("SELECT changes()").fetchone()[0]) != 1:
        raise sqlite3.IntegrityError(
            "data revision unavailable while seeding project reconciliation"
        )


def _migrate_source_fact_revisions(
    connection: sqlite3.Connection,
    *,
    expected_previous_version: int,
) -> None:
    """Normalize one exact legacy/final shape and queue known projects."""
    migration_history = [
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ]
    if migration_history != list(range(1, expected_previous_version + 1)):
        raise sqlite3.IntegrityError(
            "source-fact compatibility migration requires exact history"
        )
    shape = _classify_v48_source_revision_shape(connection)
    if shape == "legacy":
        _convert_legacy_source_revisions(connection)
    _seed_existing_project_reconciliation(connection)
    validate_v49_source_fact_revision_shape(connection)


def migrate_v49_source_fact_revisions(
    connection: sqlite3.Connection,
) -> None:
    """Normalize an exact final/legacy v48 before recording schema v49."""
    _migrate_source_fact_revisions(
        connection, expected_previous_version=48,
    )


def migrate_v50_source_fact_revisions(
    connection: sqlite3.Connection,
) -> None:
    """Recover an exact final/legacy v49 before recording schema v50."""
    _migrate_source_fact_revisions(
        connection, expected_previous_version=49,
    )


def validate_v49_source_fact_revision_shape(
    connection: sqlite3.Connection,
) -> None:
    """Require the one exact current source-revision layout."""
    if _classify_v48_source_revision_shape(connection) != "final":
        raise sqlite3.IntegrityError(
            "Hydra source-fact revision shape is not current"
        )
