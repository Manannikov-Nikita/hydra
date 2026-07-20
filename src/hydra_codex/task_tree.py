"""Pure task-tree aggregation over privacy-safe normalized observations."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Literal


Provenance = Literal["exact", "derived", "model_reported", "estimated"]


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


@dataclass(frozen=True)
class TokenVector:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.input_tokens, self.cached_input_tokens,
            self.output_tokens, self.reasoning_output_tokens,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("token components must be non-negative integers")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")

    @classmethod
    def zero(cls) -> "TokenVector":
        return cls(0, 0, 0, 0)

    @property
    def working_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens + self.output_tokens

    @property
    def full_context(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenVector") -> "TokenVector":
        return TokenVector(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_output_tokens + other.reasoning_output_tokens,
        )

    def subtract(self, other: "TokenVector") -> "TokenVector":
        values = (
            self.input_tokens - other.input_tokens,
            self.cached_input_tokens - other.cached_input_tokens,
            self.output_tokens - other.output_tokens,
            self.reasoning_output_tokens - other.reasoning_output_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("replay baseline exceeds recorded cumulative usage")
        return TokenVector(*values)

    def decreased_from(self, previous: "TokenVector") -> bool:
        return any(
            current < before
            for current, before in zip(
                (
                    self.input_tokens, self.cached_input_tokens,
                    self.output_tokens, self.reasoning_output_tokens,
                ),
                (
                    previous.input_tokens, previous.cached_input_tokens,
                    previous.output_tokens, previous.reasoning_output_tokens,
                ),
            )
        )


@dataclass(frozen=True)
class NormalizedSession:
    session_id: str
    parent_id: str | None
    started_at: datetime

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        _require_aware(self.started_at, "started_at")


@dataclass(frozen=True)
class TokenObservation:
    session_id: str
    observed_at: datetime
    sequence: int
    vector: TokenVector

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")


@dataclass(frozen=True)
class LifecycleObservation:
    session_id: str
    kind: Literal["task_complete", "task_started", "turn_aborted"]
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class ActivityObservation:
    session_id: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class ScalarFact:
    value: int | float
    provenance: Provenance
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class TokenVectorFact:
    vector: TokenVector
    provenance: Provenance
    caveats: tuple[str, ...] = ()

    @property
    def working_tokens(self) -> int:
        return self.vector.working_tokens

    @property
    def full_context(self) -> int:
        return self.vector.full_context

    @property
    def reasoning_output_tokens(self) -> int:
        return self.vector.reasoning_output_tokens


@dataclass(frozen=True)
class TaskTreeMetrics:
    root_id: str
    cutoff_at: datetime
    session_ids: tuple[str, ...]
    recorded: TokenVectorFact
    replay_baseline: TokenVectorFact
    unique: TokenVectorFact
    sessions: ScalarFact
    subagents: ScalarFact
    root_wall_clock_ms: ScalarFact
    agent_time_ms: ScalarFact
    semantic_coverage: ScalarFact
    observed_replay_baselines: int
    zero_no_observation: int
    cycle_edges: int


def _session_epoch_total(observations: list[TokenObservation]) -> TokenVector:
    if not observations:
        return TokenVector.zero()
    total = TokenVector.zero()
    previous = observations[0].vector
    for item in observations[1:]:
        if item.vector.decreased_from(previous):
            total = total + previous
        previous = item.vector
    return total + previous


def _descendants(
    root_id: str, sessions: dict[str, NormalizedSession], cutoff: datetime,
) -> tuple[tuple[str, ...], int]:
    children: dict[str, list[str]] = defaultdict(list)
    for item in sessions.values():
        if item.parent_id is not None:
            children[item.parent_id].append(item.session_id)
    visited: set[str] = set()
    queue = deque((root_id,))
    cycle_edges = 0
    while queue:
        current = queue.popleft()
        if current in visited:
            cycle_edges += 1
            continue
        session = sessions.get(current)
        if session is None or session.started_at > cutoff:
            continue
        visited.add(current)
        queue.extend(sorted(children.get(current, ())))
    return tuple(sorted(visited)), cycle_edges


def aggregate_task_tree(
    *, root_id: str, sessions: Iterable[NormalizedSession],
    tokens: Iterable[TokenObservation], lifecycle: Iterable[LifecycleObservation],
    activities: Iterable[ActivityObservation], classified_working_tokens: int = 0,
) -> TaskTreeMetrics:
    """Aggregate one root and its descendants through the root completion event.

    Missing observable fork replay is represented as a zero lower bound with
    estimated provenance; Hydra never upgrades that historical fallback to exact.
    """
    session_map: dict[str, NormalizedSession] = {}
    for item in sessions:
        if item.session_id in session_map:
            raise ValueError(f"duplicate normalized session: {item.session_id}")
        session_map[item.session_id] = item
    root = session_map.get(root_id)
    if root is None:
        raise ValueError("root session is missing")
    lifecycle_items = tuple(lifecycle)
    completions = tuple(
        item.observed_at
        for item in lifecycle_items
        if item.session_id == root_id and item.kind == "task_complete"
    )
    if not completions:
        raise ValueError("root task_complete observation is required")
    cutoff = max(completions)
    if root.started_at > cutoff:
        raise ValueError("root starts after its task_complete observation")
    session_ids, cycle_edges = _descendants(root_id, session_map, cutoff)
    included = set(session_ids)

    token_by_session: dict[str, list[TokenObservation]] = defaultdict(list)
    for item in tokens:
        if (
            item.session_id in included
            and session_map[item.session_id].started_at <= item.observed_at <= cutoff
        ):
            token_by_session[item.session_id].append(item)
    for items in token_by_session.values():
        items.sort(key=lambda item: (item.observed_at, item.sequence))

    recorded = TokenVector.zero()
    replay = TokenVector.zero()
    observed_baselines = 0
    zero_baselines = 0
    missing_finals = 0
    for session_id in session_ids:
        items = token_by_session.get(session_id, [])
        if not items:
            missing_finals += 1
        recorded = recorded + _session_epoch_total(items)
        if session_id == root_id:
            continue
        threshold = session_map[session_id].started_at + timedelta(seconds=1)
        candidates = tuple(
            item for item in items
            if session_map[session_id].started_at <= item.observed_at <= threshold
        )
        if candidates:
            replay = replay + candidates[-1].vector
            observed_baselines += 1
        else:
            zero_baselines += 1
    unique = recorded.subtract(replay)

    caveats: list[str] = []
    if zero_baselines:
        caveats.append(f"zero_no_observation:{zero_baselines}")
    if missing_finals:
        caveats.append(f"missing_final_token:{missing_finals}")
    if cycle_edges:
        caveats.append(f"cycle_edges:{cycle_edges}")
    unique_provenance: Provenance = "estimated" if zero_baselines or missing_finals else "derived"
    baseline_caveats = (f"zero_no_observation:{zero_baselines}",) if zero_baselines else ()

    last_activity = {session_id: session_map[session_id].started_at for session_id in session_ids}
    all_activity = list(activities)
    all_activity.extend(ActivityObservation(item.session_id, item.observed_at) for item in lifecycle_items)
    all_activity.extend(ActivityObservation(item.session_id, item.observed_at) for item in tokens)
    for item in all_activity:
        if item.session_id in included and item.observed_at <= cutoff:
            last_activity[item.session_id] = max(last_activity[item.session_id], item.observed_at)
    agent_time_ms = sum(
        max(0, int((last_activity[session_id] - session_map[session_id].started_at).total_seconds() * 1000))
        for session_id in session_ids
    )
    wall_clock_ms = int((cutoff - root.started_at).total_seconds() * 1000)

    if isinstance(classified_working_tokens, bool) or classified_working_tokens < 0:
        raise ValueError("classified_working_tokens must be non-negative")
    if classified_working_tokens > max(0, unique.working_tokens):
        raise ValueError("classified_working_tokens exceeds unique working tokens")
    semantic_coverage = (
        classified_working_tokens / unique.working_tokens
        if unique.working_tokens > 0 else 0.0
    )
    return TaskTreeMetrics(
        root_id=root_id, cutoff_at=cutoff, session_ids=session_ids,
        recorded=TokenVectorFact(
            recorded, "estimated" if missing_finals else "exact",
            (f"missing_final_token:{missing_finals}",) if missing_finals else (),
        ),
        replay_baseline=TokenVectorFact(
            replay, "estimated" if zero_baselines else "exact", baseline_caveats,
        ),
        unique=TokenVectorFact(unique, unique_provenance, tuple(caveats)),
        sessions=ScalarFact(len(session_ids), "exact"),
        subagents=ScalarFact(max(0, len(session_ids) - 1), "derived"),
        root_wall_clock_ms=ScalarFact(wall_clock_ms, "derived"),
        agent_time_ms=ScalarFact(agent_time_ms, "derived"),
        semantic_coverage=ScalarFact(semantic_coverage, "derived"),
        observed_replay_baselines=observed_baselines,
        zero_no_observation=zero_baselines,
        cycle_edges=cycle_edges,
    )
