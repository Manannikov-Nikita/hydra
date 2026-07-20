"""Pure, deterministic rollout metric calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Iterable

from .task_tree_types import validate_nonnegative, validate_provenance


def _validate_caveats(caveats: tuple[str, ...]) -> None:
    if not isinstance(caveats, tuple) or any(
        not isinstance(item, str) or not item for item in caveats
    ):
        raise ValueError("metric caveats must be non-empty strings")


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

    def __post_init__(self) -> None:
        for field, value in (
            ("line number", self.line_number), ("epoch", self.epoch),
            ("input tokens", self.input_tokens), ("cached input tokens", self.cached_input_tokens),
            ("output tokens", self.output_tokens), ("reasoning tokens", self.reasoning_tokens),
            ("cache write tokens", self.cache_write_tokens),
        ):
            validate_nonnegative(value, field)
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")


@dataclass(frozen=True)
class SessionEdge:
    child_key: str
    parent_key: str | None
    baseline_working_tokens: int | None
    confidence_kind: str
    confidence: float

    def __post_init__(self) -> None:
        validate_nonnegative(self.baseline_working_tokens, "baseline working tokens", allow_none=True)
        if self.confidence_kind not in {"confirmed", "inferred", "ambiguous"}:
            raise ValueError("invalid edge confidence kind")
        validate_nonnegative(self.confidence, "edge confidence")
        if self.confidence > 1:
            raise ValueError("edge confidence cannot exceed one")


@dataclass(frozen=True)
class TokenTotals:
    working_tokens: int
    full_context: int
    reasoning_tokens: int
    epochs: int
    provenance: str

    def __post_init__(self) -> None:
        for field, value in (
            ("working tokens", self.working_tokens), ("full context", self.full_context),
            ("reasoning tokens", self.reasoning_tokens), ("epochs", self.epochs),
        ):
            validate_nonnegative(value, field)
        validate_provenance(self.provenance)


@dataclass(frozen=True)
class TreeContribution:
    working_tokens: int | None
    full_context: int | None
    provenance: str
    confidence: float

    def __post_init__(self) -> None:
        validate_nonnegative(self.working_tokens, "working tokens", allow_none=True)
        validate_nonnegative(self.full_context, "full context", allow_none=True)
        validate_nonnegative(self.confidence, "confidence")
        if self.confidence > 1:
            raise ValueError("confidence cannot exceed one")
        validate_provenance(self.provenance)
        if (self.working_tokens is None or self.full_context is None) and self.provenance != "estimated":
            raise ValueError("unavailable contribution must use estimated provenance")


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

    def __post_init__(self) -> None:
        for field, value in (
            ("working tokens", self.working_tokens), ("full context", self.full_context),
            ("reasoning tokens", self.reasoning_tokens),
            ("recorded working tokens", self.recorded_working_tokens),
            ("recorded full context", self.recorded_full_context),
            ("recorded reasoning tokens", self.recorded_reasoning_tokens),
        ):
            validate_nonnegative(value, field, allow_none=True)
        validate_nonnegative(self.sessions, "sessions")
        validate_nonnegative(self.semantic_coverage, "semantic coverage")
        if self.semantic_coverage > 1:
            raise ValueError("semantic coverage cannot exceed one")
        validate_provenance(self.provenance)
        values = (
            self.working_tokens, self.full_context, self.reasoning_tokens,
            self.recorded_working_tokens, self.recorded_full_context, self.recorded_reasoning_tokens,
        )
        if any(value is None for value in values) and self.provenance != "estimated":
            raise ValueError("partial project metrics must use estimated provenance")


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

    def __post_init__(self) -> None:
        validate_nonnegative(self.wall_clock_ms, "wall clock")
        validate_nonnegative(self.agent_time_ms, "agent time")
        validate_provenance(self.provenance)


@dataclass(frozen=True)
class MetricFact:
    value: int | None
    known_lower_bound: int
    provenance: str
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_nonnegative(self.value, "metric value", allow_none=True)
        validate_nonnegative(self.known_lower_bound, "metric lower bound")
        validate_provenance(self.provenance)
        _validate_caveats(self.caveats)
        if self.value is not None and self.known_lower_bound > self.value:
            raise ValueError("metric lower bound cannot exceed its value")
        if self.value is None and self.provenance != "estimated":
            raise ValueError("unavailable metric value must use estimated provenance")


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
    if (
        edge.parent_key is None or edge.baseline_working_tokens is None
        or edge.confidence_kind != "confirmed" or edge.confidence != 1.0
    ):
        return TreeContribution(None, None, "estimated", edge.confidence)
    return TreeContribution(max(0, totals.working_tokens - edge.baseline_working_tokens), totals.full_context, "derived", edge.confidence)


def _final_epoch_vectors(connection: sqlite3.Connection, project_id: str) -> tuple[tuple[int | None, ...], ...]:
    rows = connection.execute(
        """SELECT session_key,epoch,input_tokens,cached_input_tokens,output_tokens,
                  reasoning_tokens,cache_write_tokens
             FROM token_snapshots WHERE project_id=?
             ORDER BY observed_at,source_digest,line_number""", (project_id,),
    ).fetchall()
    final: dict[tuple[str, int], list[int | None]] = {}
    for row in rows:
        vector = final.setdefault((row[0], int(row[1])), [None, None, None, None, None])
        for index in range(5):
            if row[index + 2] is not None:
                vector[index] = int(row[index + 2])
    return tuple(tuple(vector) for vector in final.values())


def aggregate_project(connection: sqlite3.Connection, project_id: str) -> ProjectMetrics:
    """Compatibility view over the component-aware project fact reducer."""
    sessions = int(connection.execute("SELECT COUNT(*) FROM rollout_sessions WHERE project_id = ?", (project_id,)).fetchone()[0])
    annotations = int(connection.execute("SELECT COUNT(*) FROM annotations WHERE project_id = ?", (project_id,)).fetchone()[0])
    facts = aggregate_project_facts(connection, project_id)
    selected = (
        facts["deduplicated_working"], facts["deduplicated_full"],
        facts["deduplicated_reasoning"], facts["recorded_working"],
        facts["recorded_full"], facts["recorded_reasoning"],
    )
    provenance_facts = selected + (
        facts["deduplicated_input"], facts["deduplicated_cached_input"],
        facts["deduplicated_output"],
    )
    provenance = (
        "estimated" if any(item.provenance == "estimated" for item in provenance_facts)
        else "derived" if any(item.provenance == "derived" for item in provenance_facts)
        else "exact"
    )
    return ProjectMetrics(
        *(item.value for item in selected), sessions,
        0.0 if not annotations else 1.0, provenance,
    )


def aggregate_project_facts(connection: sqlite3.Connection, project_id: str) -> dict[str, MetricFact]:
    """Per-component final cumulative facts; absent parts never become zero."""
    vectors = tuple(vector[:4] for vector in _final_epoch_vectors(connection, project_id))
    def component(index: int) -> MetricFact:
        known = sum(row[index] or 0 for row in vectors)
        missing = any(row[index] is None for row in vectors)
        return MetricFact(None if missing else known, known, "estimated" if missing else "exact", ("missing_component",) if missing else ())
    inputs, cached, outputs, reasoning = component(0), component(1), component(2), component(3)
    working_ready = all(row[0] is not None and row[1] is not None and row[2] is not None for row in vectors)
    full_ready = all(row[0] is not None and row[2] is not None for row in vectors)
    working_lower = sum(
        (max(0, row[0] - row[1]) if row[0] is not None and row[1] is not None else 0)
        + (row[2] or 0) for row in vectors
    )
    full_lower = sum((row[0] or 0) + (row[2] or 0) for row in vectors)
    working = MetricFact(working_lower if working_ready else None, working_lower, "exact" if working_ready else "estimated", () if working_ready else ("missing_core_component",))
    full = MetricFact(full_lower if full_ready else None, full_lower, "exact" if full_ready else "estimated", () if full_ready else ("missing_core_component",))
    confirmed_missing = int(connection.execute("""SELECT COUNT(*) FROM session_edges e JOIN rollout_sessions s ON s.session_key=e.child_key
       WHERE s.project_id=? AND e.confidence_kind='confirmed' AND e.confidence=1.0
         AND NOT EXISTS (SELECT 1 FROM fork_baselines b WHERE b.child_key=e.child_key)""", (project_id,)).fetchone()[0])
    baseline = connection.execute("""SELECT SUM(input_tokens),SUM(cached_input_tokens),SUM(output_tokens),SUM(reasoning_tokens)
       FROM fork_baselines b JOIN session_edges e ON e.child_key=b.child_key JOIN rollout_sessions s ON s.session_key=b.child_key
       WHERE s.project_id=? AND e.confidence_kind='confirmed' AND e.confidence=1.0""", (project_id,)).fetchone()
    baseline_input, baseline_cached, baseline_output, baseline_reasoning = tuple(value or 0 for value in baseline)
    uncertain_edges = int(connection.execute("""SELECT COUNT(*) FROM session_edges e JOIN rollout_sessions s ON s.session_key=e.child_key
       WHERE s.project_id=? AND (e.confidence_kind!='confirmed' OR e.confidence!=1.0)""", (project_id,)).fetchone()[0])
    invalid_baseline = (
        any(value < 0 for value in (baseline_input, baseline_cached, baseline_output, baseline_reasoning))
        or baseline_cached > baseline_input
    )
    if invalid_baseline:
        baseline_input = baseline_cached = baseline_output = baseline_reasoning = 0
    caveat = tuple(
        item for item, present in (
            ("zero_no_observation", confirmed_missing),
            ("inferred_parent_no_dedup", uncertain_edges),
            ("invalid_replay_baseline", invalid_baseline),
        ) if present
    )

    def deduplicate(recorded: MetricFact, baseline_value: int) -> MetricFact:
        local_caveats = caveat
        value = (
            None if recorded.value is None or invalid_baseline
            else recorded.value - baseline_value
        )
        if value is not None and value < 0:
            value = None
            local_caveats += ("baseline_exceeds_recorded",)
        lower = 0 if local_caveats else max(0, recorded.known_lower_bound - baseline_value)
        provenance = (
            "estimated" if value is None or local_caveats
            else "derived" if baseline_value else recorded.provenance
        )
        return MetricFact(value, lower, provenance, local_caveats)

    baseline_working = baseline_input - baseline_cached + baseline_output
    baseline_full = baseline_input + baseline_output
    deduplicated = {
        "deduplicated_input": deduplicate(inputs, baseline_input),
        "deduplicated_cached_input": deduplicate(cached, baseline_cached),
        "deduplicated_output": deduplicate(outputs, baseline_output),
        "deduplicated_reasoning": deduplicate(reasoning, baseline_reasoning),
        "deduplicated_working": deduplicate(working, baseline_working),
        "deduplicated_full": deduplicate(full, baseline_full),
    }
    facts = {
        "input": inputs, "cached_input": cached, "output": outputs, "reasoning": reasoning,
        "working": working, "full": full,
        "recorded_input": inputs, "recorded_cached_input": cached,
        "recorded_output": outputs, "recorded_reasoning": reasoning,
        "recorded_working": working, "recorded_full": full,
    }
    facts.update(deduplicated)
    return facts
