"""Pure, deterministic rollout metric calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Iterable


@dataclass(frozen=True)
class TokenSnapshot:
    session_key: str
    line_number: int
    epoch: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class SessionEdge:
    child_key: str
    parent_key: str | None
    baseline_working_tokens: int | None
    confidence_kind: str
    confidence: float


@dataclass(frozen=True)
class TokenTotals:
    working_tokens: int
    full_context: int
    reasoning_tokens: int
    epochs: int
    provenance: str


@dataclass(frozen=True)
class TreeContribution:
    working_tokens: int | None
    full_context: int | None
    provenance: str
    confidence: float


@dataclass(frozen=True)
class ProjectMetrics:
    working_tokens: int | None
    full_context: int | None
    reasoning_tokens: int | None
    recorded_working_tokens: int | None
    recorded_full_context: int | None
    recorded_reasoning_tokens: int | None
    sessions: int
    semantic_coverage: float
    provenance: str = "exact"


@dataclass(frozen=True)
class TurnAttempt:
    turn_key: str
    started_at: str | None
    finished_at: str | None
    emitted_duration_ms: int | None


@dataclass(frozen=True)
class TurnTotals:
    wall_clock_ms: int
    agent_time_ms: int
    provenance: str


def aggregate_turns(attempts: Iterable[TurnAttempt]) -> TurnTotals:
    intervals = []
    agent = 0
    for item in attempts:
        agent += item.emitted_duration_ms or 0
        if item.started_at and item.finished_at:
            intervals.append((datetime.fromisoformat(item.started_at.replace("Z", "+00:00")), datetime.fromisoformat(item.finished_at.replace("Z", "+00:00"))))
    wall = int((max(end for _, end in intervals) - min(start for start, _ in intervals)).total_seconds() * 1000) if intervals else 0
    return TurnTotals(wall, agent, "derived")


def aggregate_tokens(snapshots: Iterable[TokenSnapshot]) -> TokenTotals:
    """Use the final cumulative vector from each session counter epoch."""
    final: dict[tuple[str, int], TokenSnapshot] = {}
    for item in sorted(snapshots, key=lambda value: (value.session_key, value.epoch, value.line_number)):
        final[(item.session_key, item.epoch)] = item
    values = tuple(final.values())
    return TokenTotals(
        sum(item.input_tokens - item.cached_input_tokens + item.output_tokens for item in values),
        sum(item.input_tokens + item.output_tokens for item in values),
        sum(item.reasoning_tokens for item in values), len(values), "exact",
    )


def tree_contribution(totals: TokenTotals, edge: SessionEdge | None) -> TreeContribution:
    """Subtract only a confirmed baseline that was recorded by the rollout."""
    if edge is None:
        return TreeContribution(totals.working_tokens, totals.full_context, "exact", 1.0)
    if edge.parent_key is None or edge.baseline_working_tokens is None or edge.confidence_kind != "confirmed":
        return TreeContribution(None, None, "estimated", edge.confidence)
    return TreeContribution(max(0, totals.working_tokens - edge.baseline_working_tokens), totals.full_context, "derived", edge.confidence)


def aggregate_project(connection: sqlite3.Connection, project_id: str) -> ProjectMetrics:
    rows = connection.execute(
        """SELECT session_key, line_number, epoch, input_tokens, cached_input_tokens,
                  output_tokens, reasoning_tokens, cache_write_tokens
           FROM token_snapshots WHERE project_id = ?""", (project_id,)
    ).fetchall()
    partial = int(connection.execute("SELECT COUNT(*) FROM token_snapshots WHERE project_id = ? AND completeness != 'complete'", (project_id,)).fetchone()[0])
    recorded = aggregate_tokens(TokenSnapshot(*tuple(row)) for row in rows)
    baselines = connection.execute(
        """SELECT fork_baselines.input_tokens, fork_baselines.cached_input_tokens,
                  fork_baselines.output_tokens, fork_baselines.reasoning_tokens
           FROM fork_baselines JOIN rollout_sessions ON rollout_sessions.session_key = fork_baselines.child_key
           WHERE project_id = ?""", (project_id,)
    ).fetchall()
    baseline_working = sum(row[0] - row[1] + row[2] for row in baselines)
    baseline_full = sum(row[0] + row[2] for row in baselines)
    baseline_reasoning = sum(row[3] for row in baselines)
    sessions = int(connection.execute("SELECT COUNT(*) FROM rollout_sessions WHERE project_id = ?", (project_id,)).fetchone()[0])
    annotations = int(connection.execute("SELECT COUNT(*) FROM annotations WHERE project_id = ?", (project_id,)).fetchone()[0])
    if partial:
        return ProjectMetrics(None, None, None, None, None, None, sessions, 0.0 if not annotations else 1.0, "estimated")
    return ProjectMetrics(
        recorded.working_tokens - baseline_working, recorded.full_context - baseline_full,
        recorded.reasoning_tokens - baseline_reasoning, recorded.working_tokens,
        recorded.full_context, recorded.reasoning_tokens, sessions,
        0.0 if not annotations else 1.0, "derived" if baselines else "exact",
    )
