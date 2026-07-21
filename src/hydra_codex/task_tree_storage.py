"""Read-only adapter from persisted normalized rows to pure task-tree facts."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from .task_tree import aggregate_task_tree
from .task_tree_types import (
    ActivityObservation,
    FileObservation,
    LifecycleObservation,
    NormalizedSession,
    ReplayBaselineObservation,
    TaskTreeMetrics,
    TestRunObservation,
    TokenObservation,
    TokenVector,
    ToolObservation,
)


def _optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _sessions(connection: sqlite3.Connection, project_id: str) -> tuple[NormalizedSession, ...]:
    rows = connection.execute(
        """SELECT s.session_key,e.parent_key,s.started_at,e.confidence_kind,e.confidence
             FROM rollout_sessions s LEFT JOIN session_edges e ON e.child_key=s.session_key
            WHERE s.project_id=? ORDER BY s.session_key""",
        (project_id,),
    )
    return tuple(
        NormalizedSession(
            str(row[0]), row[1], _optional_timestamp(row[2]),
            edge_confidence_kind=str(row[3] or "confirmed"),
            edge_confidence=float(row[4] if row[4] is not None else 1.0),
        )
        for row in rows
    )


def _tokens(connection: sqlite3.Connection, project_id: str) -> tuple[TokenObservation, ...]:
    rows = connection.execute(
        """SELECT t.session_key,t.observed_at,t.epoch,t.input_tokens,t.cached_input_tokens,
                  t.output_tokens,t.reasoning_tokens,s.logical_source_key,t.line_number,
                  t.selection_provenance,t.selection_caveat,t.source_family
             FROM token_snapshots t
             LEFT JOIN rollout_sources s ON s.source_digest=t.source_digest
            WHERE t.project_id=? AND t.contributes_total=1 AND t.vector_valid=1
            ORDER BY CASE WHEN observed_at IS NULL THEN 1 ELSE 0 END,
                     julianday(observed_at),t.source_digest,t.line_number""",
        (project_id,),
    )
    observations: list[TokenObservation] = []
    for sequence, row in enumerate(rows):
        observed_at = _optional_timestamp(row[1])
        observations.append(TokenObservation(
            str(row[0]), observed_at, sequence,
            TokenVector(row[3], row[4], row[5], row[6]), int(row[2]),
            "estimated" if observed_at is None else "exact",
            str(row[7]) if row[7] is not None else None,
            int(row[8]) if row[7] is not None else None,
            str(row[9]), str(row[10]) if row[10] is not None else None,
            row[11] == "app_server",
        ))
    return tuple(observations)


def _baselines(connection: sqlite3.Connection, project_id: str) -> tuple[ReplayBaselineObservation, ...]:
    rows = connection.execute(
        """SELECT b.child_key,b.observed_at,b.input_tokens,b.cached_input_tokens,
                  b.output_tokens,b.reasoning_tokens
             FROM fork_baselines b JOIN rollout_sessions s ON s.session_key=b.child_key
            WHERE s.project_id=? AND b.vector_valid=1 ORDER BY b.child_key""",
        (project_id,),
    )
    observations: list[ReplayBaselineObservation] = []
    for row in rows:
        observed_at = _optional_timestamp(row[1])
        if observed_at is not None:
            observations.append(ReplayBaselineObservation(
                str(row[0]), observed_at, TokenVector(row[2], row[3], row[4], row[5]),
            ))
    return tuple(observations)


def _lifecycle(connection: sqlite3.Connection, project_id: str) -> tuple[LifecycleObservation, ...]:
    mapping = {"completed": "task_complete", "aborted": "turn_aborted"}
    rows = connection.execute(
        """SELECT a.session_key,a.turn_key,a.attempt_ordinal,a.state,
                  a.started_at,a.finished_at,
                  a.started_logical_source_key,a.terminal_logical_source_key,
                  a.started_source_ordinal,a.terminal_source_ordinal,
                  started_receipt.observed_at,terminal_receipt.observed_at
             FROM turn_attempts a
             JOIN rollout_sessions s ON s.session_key=a.session_key
             LEFT JOIN rollout_events started_receipt
                    ON started_receipt.event_key=a.started_event_key
             LEFT JOIN rollout_events terminal_receipt
                    ON terminal_receipt.event_key=a.terminal_event_key
            WHERE s.project_id=?
            ORDER BY a.session_key,a.turn_key,a.attempt_ordinal""",
        (project_id,),
    )
    observations: list[LifecycleObservation] = []
    terminal_attempts: dict[
        tuple[str, str], list[LifecycleObservation]
    ] = {}
    for row in rows:
        attempt_key = f"{row[1]}/{row[2]}"
        started_at = _optional_timestamp(row[4])
        started_provenance = "exact"
        if started_at is None:
            started_at = _optional_timestamp(row[10])
            started_provenance = "estimated"
        if started_at is not None:
            observations.append(LifecycleObservation(
                str(row[0]), "task_started", started_at,
                str(row[6]) if row[6] is not None else None,
                int(row[8]) if row[8] is not None else None,
                attempt_key, started_provenance,
            ))
        if row[3] not in mapping:
            continue
        terminal_at = _optional_timestamp(row[5])
        terminal_provenance = "exact"
        if terminal_at is None:
            terminal_at = _optional_timestamp(row[11])
            terminal_provenance = "estimated"
        if terminal_at is not None:
            terminal = LifecycleObservation(
                str(row[0]), mapping[str(row[3])], terminal_at,
                str(row[7]) if row[7] is not None else None,
                int(row[9]) if row[9] is not None else None,
                attempt_key, terminal_provenance,
            )
            observations.append(terminal)
            terminal_attempts.setdefault((str(row[0]), str(row[1])), []).append(
                terminal,
            )

    # Keep an explicitly estimated timing-conflict receipt for discarded App
    # terminals.  It can contribute a caveat, but sharing the canonical
    # attempt key ensures it can never replace the exact reconciled boundary.
    discarded = connection.execute(
        """SELECT e.session_key,e.event_kind,r.observed_at,
                  e.logical_source_key,e.source_ordinal,e.turn_key
             FROM turn_lifecycle_events e
             JOIN rollout_sessions s ON s.session_key=e.session_key
             JOIN rollout_events r ON r.event_key=e.event_key
            WHERE s.project_id=? AND e.timestamp_epoch IS NULL
              AND e.event_kind IN ('completed','aborted')
              AND NOT EXISTS (
                  SELECT 1 FROM turn_attempts a
                   WHERE a.terminal_event_key=e.event_key
              )""",
        (project_id,),
    )
    for row in discarded:
        observed_at = _optional_timestamp(row[2])
        candidates = terminal_attempts.get((str(row[0]), str(row[5])), ())
        if observed_at is None or not candidates:
            continue
        prior = tuple(
            candidate for candidate in candidates
            if candidate.observed_at <= observed_at
        )
        canonical = max(
            prior or tuple(candidates), key=lambda item: item.observed_at,
        )
        observations.append(LifecycleObservation(
            str(row[0]), mapping[str(row[1])], observed_at,
            str(row[3]), int(row[4]), canonical.turn_key, "estimated",
        ))
    return tuple(observations)


def _activities(connection: sqlite3.Connection, project_id: str) -> tuple[ActivityObservation, ...]:
    rows = connection.execute(
        """SELECT ls.session_key,e.observed_at
             FROM rollout_events e
             JOIN rollout_logical_sources ls
                  ON ls.logical_source_key=e.logical_source_key
             LEFT JOIN turn_lifecycle_events lifecycle
                  ON lifecycle.event_key=e.event_key
            WHERE ls.project_id=? AND ls.session_key IS NOT NULL
              AND lifecycle.event_key IS NULL
            UNION ALL
           SELECT a.session_key,COALESCE(a.started_at,r.observed_at)
             FROM turn_attempts a
             JOIN rollout_sessions s ON s.session_key=a.session_key
             LEFT JOIN rollout_events r ON r.event_key=a.started_event_key
            WHERE s.project_id=? AND COALESCE(a.started_at,r.observed_at) IS NOT NULL
            UNION ALL
           SELECT a.session_key,COALESCE(a.finished_at,r.observed_at)
             FROM turn_attempts a
             JOIN rollout_sessions s ON s.session_key=a.session_key
             LEFT JOIN rollout_events r ON r.event_key=a.terminal_event_key
            WHERE s.project_id=? AND COALESCE(a.finished_at,r.observed_at) IS NOT NULL
            UNION ALL
           SELECT t.session_key,t.observed_at
             FROM token_snapshots t WHERE t.project_id=?
            UNION ALL
           SELECT t.session_key,COALESCE(t.finished_at,t.started_at)
             FROM tool_spans t JOIN rollout_sessions s ON s.session_key=t.session_key
            WHERE s.project_id=?
            UNION ALL
           SELECT f.session_key,f.observed_at
             FROM file_observations f JOIN rollout_sessions s ON s.session_key=f.session_key
            WHERE s.project_id=?
            UNION ALL
           SELECT t.session_key,t.observed_at
             FROM rollout_test_runs t JOIN rollout_sessions s ON s.session_key=t.session_key
            WHERE s.project_id=?""",
        (project_id,) * 7,
    )
    return tuple(
        ActivityObservation(str(row[0]), observed_at)
        for row in rows if (observed_at := _optional_timestamp(row[1])) is not None
    )


def _trusted_semantic_activities(
    connection: sqlite3.Connection, project_id: str,
) -> tuple[ActivityObservation, ...]:
    rows = connection.execute(
        """SELECT session_id,observed_at FROM annotations WHERE project_id=?
           UNION ALL SELECT session_key,created_at FROM trusted_turn_bindings WHERE project_id=?
           UNION ALL SELECT session_key,finished_at FROM trusted_turn_bindings
                      WHERE project_id=? AND finished_at IS NOT NULL
           UNION ALL SELECT session_key,first_stop_at FROM trusted_turn_bindings
                      WHERE project_id=? AND first_stop_at IS NOT NULL
           UNION ALL SELECT session_key,observed_at FROM semantic_fact_staging WHERE project_id=?""",
        (project_id,) * 5,
    )
    return tuple(
        ActivityObservation(str(session_id), observed)
        for session_id, value in rows
        if (observed := _optional_timestamp(value)) is not None
    )


def _tools(connection: sqlite3.Connection, project_id: str) -> tuple[ToolObservation, ...]:
    rows = connection.execute(
        """SELECT t.session_key,t.call_key,t.category,COALESCE(t.finished_at,t.started_at)
             FROM tool_spans t JOIN rollout_sessions s ON s.session_key=t.session_key
            WHERE s.project_id=?
              AND NOT EXISTS (
                    SELECT 1 FROM tool_span_roles r
                     WHERE r.session_key=t.session_key
                       AND r.call_key=t.call_key
                       AND r.role='nested_inferred'
              )""",
        (project_id,),
    )
    return tuple(
        ToolObservation(str(row[0]), str(row[1]), str(row[2]), _optional_timestamp(row[3]))
        for row in rows
    )


def _files(connection: sqlite3.Connection, project_id: str) -> tuple[FileObservation, ...]:
    rows = connection.execute(
        """SELECT f.session_key,f.source_digest,f.line_number,f.operation,f.path_hash,f.observed_at
             FROM file_observations f JOIN rollout_sessions s ON s.session_key=f.session_key
            WHERE s.project_id=?""",
        (project_id,),
    )
    return tuple(
        FileObservation(
            str(row[0]), f"{row[1]}:{row[2]}:{row[3]}:{row[4]}", str(row[3]),
            _optional_timestamp(row[5]),
        )
        for row in rows
    )


def _tests(connection: sqlite3.Connection, project_id: str) -> tuple[TestRunObservation, ...]:
    rows = connection.execute(
        """SELECT t.session_key,t.evidence_key,t.scope,t.retry_kind,t.observed_at
             FROM rollout_test_runs t JOIN rollout_sessions s ON s.session_key=t.session_key
            WHERE s.project_id=?""",
        (project_id,),
    )
    return tuple(
        TestRunObservation(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), _optional_timestamp(row[4]),
        )
        for row in rows
    )


def aggregate_stored_task_tree(
    connection: sqlite3.Connection, *, project_id: str, root_id: str,
    classified_working_tokens: int = 0,
    cutoff_at: datetime | None = None,
    cutoff_timing_provenance: str = "exact",
    include_ambiguous_lineage: bool = True,
) -> TaskTreeMetrics:
    """Build a task tree exclusively from normalized persisted observations."""
    return aggregate_task_tree(
        root_id=root_id,
        sessions=_sessions(connection, project_id),
        tokens=_tokens(connection, project_id),
        lifecycle=_lifecycle(connection, project_id),
        activities=(
            *_activities(connection, project_id),
            *_trusted_semantic_activities(connection, project_id),
        ),
        classified_working_tokens=classified_working_tokens,
        replay_baselines=_baselines(connection, project_id),
        tools=_tools(connection, project_id),
        files=_files(connection, project_id),
        tests=_tests(connection, project_id),
        cutoff_at=cutoff_at,
        cutoff_timing_provenance=cutoff_timing_provenance,
        include_ambiguous_lineage=include_ambiguous_lineage,
    )
