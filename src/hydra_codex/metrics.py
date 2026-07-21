"""Pure, deterministic rollout metric calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


@dataclass(frozen=True)
class _FinalEpochVector:
    vector: tuple[int | None, ...]
    component_lower_bounds: tuple[int, ...]
    working_lower_bound: int
    full_lower_bound: int
    timestamp_ambiguous: bool


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


def _aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _final_epoch_vectors(
    connection: sqlite3.Connection, project_id: str,
) -> tuple[_FinalEpochVector, ...]:
    rows = connection.execute(
        """SELECT t.session_key,t.epoch,t.input_tokens,t.cached_input_tokens,t.output_tokens,
                  t.reasoning_tokens,t.cache_write_tokens,t.observed_at,
                  COALESCE(s.logical_source_key,t.source_digest),t.line_number
             FROM token_snapshots t
             LEFT JOIN rollout_sources s ON s.source_digest=t.source_digest
            WHERE t.project_id=? AND t.contributes_total=1 AND t.vector_valid=1
            ORDER BY t.session_key,t.epoch,t.source_digest,t.line_number""", (project_id,),
    ).fetchall()
    grouped: dict[tuple[str, int], list[tuple[object, ...]]] = {}
    for row in rows:
        grouped.setdefault((str(row[0]), int(row[1])), []).append(tuple(row))
    result: list[_FinalEpochVector] = []
    for key in sorted(grouped):
        observations = grouped[key]
        parsed = [(_aware_timestamp(row[7]), str(row[8]), int(row[9]), row) for row in observations]
        ambiguous = any(item[0] is None for item in parsed)
        ordered = sorted(
            parsed,
            key=lambda item: (
                item[0] is None,
                item[0].timestamp() if item[0] is not None else 0.0,
                item[1], item[2],
            ),
        )
        vector: list[int | None] = [None, None, None, None, None]
        for _, _, _, row in ordered:
            for index in range(5):
                if row[index + 2] is not None:
                    vector[index] = int(row[index + 2])
        if ambiguous:
            component_lower = tuple(
                max((int(row[index + 2]) for row in observations if row[index + 2] is not None), default=0)
                for index in range(5)
            )
            working_lower = max((
                (int(row[2]) - int(row[3]) if row[2] is not None and row[3] is not None else 0)
                + (int(row[4]) if row[4] is not None else 0)
                for row in observations
            ), default=0)
            full_lower = max((
                (int(row[2]) if row[2] is not None else 0)
                + (int(row[4]) if row[4] is not None else 0)
                for row in observations
            ), default=0)
        else:
            component_lower = tuple(value or 0 for value in vector)
            working_lower = (
                (vector[0] - vector[1] if vector[0] is not None and vector[1] is not None else 0)
                + (vector[2] or 0)
            )
            full_lower = (vector[0] or 0) + (vector[2] or 0)
        result.append(_FinalEpochVector(
            tuple(vector), component_lower, working_lower, full_lower, ambiguous,
        ))
    return tuple(result)


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
    epochs = _final_epoch_vectors(connection, project_id)
    invalid_only = bool(not epochs and connection.execute(
        "SELECT 1 FROM token_snapshots WHERE project_id=? AND vector_valid=0 LIMIT 1",
        (project_id,),
    ).fetchone())
    vectors = tuple(epoch.vector[:4] for epoch in epochs)
    timestamp_ambiguous = any(epoch.timestamp_ambiguous for epoch in epochs)
    def component(index: int) -> MetricFact:
        known = sum(epoch.component_lower_bounds[index] for epoch in epochs)
        missing = invalid_only or any(row[index] is None for row in vectors)
        caveats = tuple(
            caveat for caveat, present in (
                ("missing_component", missing),
                ("timestamp_ambiguous", timestamp_ambiguous),
                ("invalid_legacy_token_vector", invalid_only),
            ) if present
        )
        unavailable = missing or timestamp_ambiguous
        return MetricFact(
            None if unavailable else known, known,
            "estimated" if unavailable else "exact", caveats,
        )
    inputs, cached, outputs, reasoning = component(0), component(1), component(2), component(3)
    working_ready = (
        not invalid_only and not timestamp_ambiguous
        and all(row[0] is not None and row[1] is not None and row[2] is not None for row in vectors)
    )
    full_ready = (
        not invalid_only and not timestamp_ambiguous
        and all(row[0] is not None and row[2] is not None for row in vectors)
    )
    working_lower = sum(epoch.working_lower_bound for epoch in epochs)
    full_lower = sum(epoch.full_lower_bound for epoch in epochs)
    working_caveats = tuple(
        caveat for caveat, present in (
            ("missing_core_component", not all(
                row[0] is not None and row[1] is not None and row[2] is not None for row in vectors
            )),
            ("timestamp_ambiguous", timestamp_ambiguous),
            ("invalid_legacy_token_vector", invalid_only),
        ) if present
    )
    full_caveats = tuple(
        caveat for caveat, present in (
            ("missing_core_component", not all(
                row[0] is not None and row[2] is not None for row in vectors
            )),
            ("timestamp_ambiguous", timestamp_ambiguous),
            ("invalid_legacy_token_vector", invalid_only),
        ) if present
    )
    working = MetricFact(
        working_lower if working_ready else None, working_lower,
        "exact" if working_ready else "estimated", working_caveats,
    )
    full = MetricFact(
        full_lower if full_ready else None, full_lower,
        "exact" if full_ready else "estimated", full_caveats,
    )
    baseline_rows = connection.execute(
        """SELECT e.child_key,s.started_at,b.observed_at,b.input_tokens,
                  b.cached_input_tokens,b.output_tokens,b.reasoning_tokens
             FROM session_edges e
            JOIN rollout_sessions s ON s.session_key=e.child_key
             LEFT JOIN fork_baselines b
               ON b.child_key=e.child_key AND b.vector_valid=1
            WHERE s.project_id=? AND e.parent_key IS NOT NULL
              AND e.confidence_kind='confirmed' AND e.confidence=1.0""",
        (project_id,),
    ).fetchall()
    confirmed_missing = ineligible_baseline = 0
    eligible_baselines: list[tuple[int, int, int, int]] = []
    for row in baseline_rows:
        if row[2] is None and row[3] is None:
            confirmed_missing += 1
            continue
        started_at = _aware_timestamp(row[1])
        observed_at = _aware_timestamp(row[2])
        if (
            started_at is None or observed_at is None
            or not started_at <= observed_at <= started_at + timedelta(seconds=1)
        ):
            ineligible_baseline += 1
            continue
        eligible_baselines.append(tuple(int(value) for value in row[3:7]))
    baseline_input, baseline_cached, baseline_output, baseline_reasoning = (
        sum(row[index] for row in eligible_baselines) for index in range(4)
    )
    uncertain_edges = int(connection.execute("""SELECT COUNT(*) FROM session_edges e JOIN rollout_sessions s ON s.session_key=e.child_key
       WHERE s.project_id=? AND (
             e.parent_key IS NULL OR e.confidence_kind!='confirmed' OR e.confidence!=1.0
       )""", (project_id,)).fetchone()[0])
    invalid_baseline = (
        any(value < 0 for value in (baseline_input, baseline_cached, baseline_output, baseline_reasoning))
        or baseline_cached > baseline_input
    )
    if invalid_baseline:
        baseline_input = baseline_cached = baseline_output = baseline_reasoning = 0
    caveat = tuple(
        item for item, present in (
            ("zero_no_observation", confirmed_missing),
            ("ineligible_replay_baseline", ineligible_baseline),
            ("inferred_parent_no_dedup", uncertain_edges),
            ("invalid_replay_baseline", invalid_baseline),
        ) if present
    )

    def deduplicate(recorded: MetricFact, baseline_value: int) -> MetricFact:
        local_caveats = tuple(dict.fromkeys(recorded.caveats + caveat))
        value = (
            None if recorded.value is None or invalid_baseline or local_caveats
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
