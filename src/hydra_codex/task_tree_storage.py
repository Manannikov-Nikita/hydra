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
                  t.output_tokens,t.reasoning_tokens,s.logical_source_key,t.line_number
             FROM token_snapshots t
             LEFT JOIN rollout_sources s ON s.source_digest=t.source_digest
            WHERE t.project_id=?
            ORDER BY CASE WHEN observed_at IS NULL THEN 1 ELSE 0 END,
                     observed_at,t.source_digest,t.line_number""",
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
        ))
    return tuple(observations)


def _baselines(connection: sqlite3.Connection, project_id: str) -> tuple[ReplayBaselineObservation, ...]:
    rows = connection.execute(
        """SELECT b.child_key,b.observed_at,b.input_tokens,b.cached_input_tokens,
                  b.output_tokens,b.reasoning_tokens
             FROM fork_baselines b JOIN rollout_sessions s ON s.session_key=b.child_key
            WHERE s.project_id=? ORDER BY b.child_key""",
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
    mapping = {"started": "task_started", "completed": "task_complete", "aborted": "turn_aborted"}
    rows = connection.execute(
        """SELECT e.session_key,e.event_kind,e.observed_at,
                  e.logical_source_key,e.source_ordinal
             FROM turn_lifecycle_events e
             JOIN rollout_sessions s ON s.session_key=e.session_key
            WHERE s.project_id=? ORDER BY e.source_digest,e.source_ordinal""",
        (project_id,),
    )
    observations: list[LifecycleObservation] = []
    for row in rows:
        observed_at = _optional_timestamp(row[2])
        if observed_at is not None:
            observations.append(LifecycleObservation(
                str(row[0]), mapping[str(row[1])], observed_at,
                str(row[3]), int(row[4]),
            ))
    return tuple(observations)


def _activities(connection: sqlite3.Connection, project_id: str) -> tuple[ActivityObservation, ...]:
    rows = connection.execute(
        """SELECT ls.session_key,e.observed_at
             FROM rollout_events e
             JOIN rollout_logical_sources ls ON ls.logical_source_key=e.logical_source_key
            WHERE ls.project_id=? AND ls.session_key IS NOT NULL
            UNION ALL
           SELECT e.session_key,e.observed_at
             FROM turn_lifecycle_events e
             JOIN rollout_sessions s ON s.session_key=e.session_key
            WHERE s.project_id=?
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
        (project_id,) * 6,
    )
    return tuple(
        ActivityObservation(str(row[0]), observed_at)
        for row in rows if (observed_at := _optional_timestamp(row[1])) is not None
    )


def _tools(connection: sqlite3.Connection, project_id: str) -> tuple[ToolObservation, ...]:
    rows = connection.execute(
        """SELECT t.session_key,t.call_key,t.category,COALESCE(t.finished_at,t.started_at)
             FROM tool_spans t JOIN rollout_sessions s ON s.session_key=t.session_key
            WHERE s.project_id=?""",
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
) -> TaskTreeMetrics:
    """Build a task tree exclusively from normalized persisted observations."""
    return aggregate_task_tree(
        root_id=root_id,
        sessions=_sessions(connection, project_id),
        tokens=_tokens(connection, project_id),
        lifecycle=_lifecycle(connection, project_id),
        activities=_activities(connection, project_id),
        classified_working_tokens=classified_working_tokens,
        replay_baselines=_baselines(connection, project_id),
        tools=_tools(connection, project_id),
        files=_files(connection, project_id),
        tests=_tests(connection, project_id),
    )
