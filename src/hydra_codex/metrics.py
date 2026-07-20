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


@dataclass(frozen=True)
class MetricFact:
    value: int | None
    known_lower_bound: int
    provenance: str
    caveats: tuple[str, ...] = ()


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
    partial = int(connection.execute("SELECT COUNT(*) FROM token_snapshots WHERE project_id = ? AND completeness != 'complete'", (project_id,)).fetchone()[0])
    sessions = int(connection.execute("SELECT COUNT(*) FROM rollout_sessions WHERE project_id = ?", (project_id,)).fetchone()[0])
    annotations = int(connection.execute("SELECT COUNT(*) FROM annotations WHERE project_id = ?", (project_id,)).fetchone()[0])
    if partial:
        return ProjectMetrics(None, None, None, None, None, None, sessions, 0.0 if not annotations else 1.0, "estimated")
    rows = connection.execute(
        """SELECT session_key, line_number, epoch, input_tokens, cached_input_tokens,
                  output_tokens, reasoning_tokens, cache_write_tokens
           FROM token_snapshots WHERE project_id = ? ORDER BY observed_at, source_digest, line_number""", (project_id,)
    ).fetchall()
    global_rows = []
    last: dict[str, tuple[int, int, int, int, int]] = {}
    epochs: dict[str, int] = {}
    for row in rows:
        vector = tuple(0 if row[index] is None else row[index] for index in range(3, 8))
        session = row[0]
        if session in last and any(now < before for now, before in zip(vector, last[session])):
            epochs[session] = epochs.get(session, 0) + 1
        last[session] = vector
        global_rows.append(TokenSnapshot(session, len(global_rows), epochs.get(session, 0), *vector))
    recorded = aggregate_tokens(global_rows)
    baselines = connection.execute(
        """SELECT fork_baselines.input_tokens, fork_baselines.cached_input_tokens,
                  fork_baselines.output_tokens, fork_baselines.reasoning_tokens
           FROM fork_baselines JOIN rollout_sessions ON rollout_sessions.session_key = fork_baselines.child_key
           WHERE project_id = ?""", (project_id,)
    ).fetchall()
    baseline_working = sum(row[0] - row[1] + row[2] for row in baselines)
    baseline_full = sum(row[0] + row[2] for row in baselines)
    baseline_reasoning = sum(row[3] for row in baselines)
    return ProjectMetrics(
        recorded.working_tokens - baseline_working, recorded.full_context - baseline_full,
        recorded.reasoning_tokens - baseline_reasoning, recorded.working_tokens,
        recorded.full_context, recorded.reasoning_tokens, sessions,
        0.0 if not annotations else 1.0, "derived" if baselines else "exact",
    )


def aggregate_project_facts(connection: sqlite3.Connection, project_id: str) -> dict[str, MetricFact]:
    """Per-component final cumulative facts; absent parts never become zero."""
    rows = connection.execute(
        """SELECT session_key,epoch,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,observed_at
           FROM token_snapshots WHERE project_id=? ORDER BY observed_at,source_digest,line_number""", (project_id,)
    ).fetchall()
    final: dict[tuple[str, int], sqlite3.Row] = {}
    for row in rows:
        final[(row[0], row[1])] = row
    vectors = tuple(final.values())
    def component(index: int) -> MetricFact:
        known = sum(row[index] or 0 for row in vectors)
        missing = any(row[index] is None for row in vectors)
        return MetricFact(None if missing else known, known, "estimated" if missing else "exact", ("missing_component",) if missing else ())
    inputs, cached, outputs, reasoning = component(2), component(3), component(4), component(5)
    working_ready = all(row[2] is not None and row[3] is not None and row[4] is not None for row in vectors)
    full_ready = all(row[2] is not None and row[4] is not None for row in vectors)
    working_lower = sum((row[2] or 0) - (row[3] or 0) + (row[4] or 0) for row in vectors)
    full_lower = sum((row[2] or 0) + (row[4] or 0) for row in vectors)
    working = MetricFact(working_lower if working_ready else None, working_lower, "exact" if working_ready else "estimated", () if working_ready else ("missing_core_component",))
    full = MetricFact(full_lower if full_ready else None, full_lower, "exact" if full_ready else "estimated", () if full_ready else ("missing_core_component",))
    confirmed_missing = int(connection.execute("""SELECT COUNT(*) FROM session_edges e JOIN rollout_sessions s ON s.session_key=e.child_key
       WHERE s.project_id=? AND e.confidence_kind='confirmed' AND NOT EXISTS (SELECT 1 FROM fork_baselines b WHERE b.child_key=e.child_key)""", (project_id,)).fetchone()[0])
    baseline = connection.execute("""SELECT SUM(input_tokens-cached_input_tokens+output_tokens),SUM(input_tokens+output_tokens)
       FROM fork_baselines b JOIN session_edges e ON e.child_key=b.child_key JOIN rollout_sessions s ON s.session_key=b.child_key
       WHERE s.project_id=? AND e.confidence_kind='confirmed'""", (project_id,)).fetchone()
    baseline_working, baseline_full = baseline[0] or 0, baseline[1] or 0
    caveat = ("zero_no_observation",) if confirmed_missing else ()
    dedup_working = MetricFact(None if working.value is None else working.value-baseline_working, working.known_lower_bound-baseline_working, "derived" if baseline_working else ("estimated" if caveat else working.provenance), caveat)
    dedup_full = MetricFact(None if full.value is None else full.value-baseline_full, full.known_lower_bound-baseline_full, "derived" if baseline_full else ("estimated" if caveat else full.provenance), caveat)
    facts = {
        "input": inputs, "cached_input": cached, "output": outputs, "reasoning": reasoning,
        "working": working, "full": full,
    }
    facts.update({"recorded_working": working, "recorded_full": full, "deduplicated_working": dedup_working, "deduplicated_full": dedup_full})
    return facts
