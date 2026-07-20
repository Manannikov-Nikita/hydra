"""Pure task-tree aggregation over privacy-safe normalized observations."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Iterable

from .task_tree_types import (
    ActivityObservation,
    FileObservation,
    LifecycleObservation,
    NormalizedSession,
    Provenance,
    ReplayBaselineObservation,
    ScalarFact,
    TaskTreeMetrics,
    TestRunObservation,
    TokenObservation,
    TokenVector,
    TokenVectorFact,
    ToolObservation,
    _Amount,
    _Bounds,
)


def _bounds(vector: TokenVector) -> _Bounds:
    input_tokens = vector.input_tokens or 0
    cached = vector.cached_input_tokens or 0
    output = vector.output_tokens or 0
    working = (
        vector.input_tokens - vector.cached_input_tokens
        if vector.input_tokens is not None and vector.cached_input_tokens is not None else 0
    ) + output
    return _Bounds(input_tokens, cached, output, vector.reasoning_output_tokens or 0, working, input_tokens + output)


def _combine(amounts: Iterable[_Amount]) -> _Amount:
    items = tuple(amounts)
    if not items:
        return _Amount(TokenVector.zero(), _bounds(TokenVector.zero()))
    vector = items[0].vector
    for item in items[1:]:
        vector = vector + item.vector
    return _Amount(vector, _Bounds(*(
        sum(getattr(item.bounds, field) for item in items)
        for field in ("input", "cached", "output", "reasoning", "working", "full")
    )))


def _token_fact(amount: _Amount, provenance: Provenance, caveats: tuple[str, ...]) -> TokenVectorFact:
    names = ("input", "cached_input", "output", "reasoning")
    fields = ("input", "cached", "output", "reasoning")
    components: list[ScalarFact] = []
    for name, field, value in zip(names, fields, amount.vector.values):
        missing = value is None
        component_caveats = caveats + ((f"missing_{name}_component",) if missing else ())
        components.append(ScalarFact(
            value, "estimated" if missing or provenance == "estimated" else provenance,
            component_caveats, getattr(amount.bounds, field),
        ))
    working_missing = amount.vector.working_tokens is None
    full_missing = amount.vector.full_context is None
    working = ScalarFact(
        amount.vector.working_tokens, "estimated" if working_missing or provenance == "estimated" else provenance,
        caveats + (("missing_working_component",) if working_missing else ()), amount.bounds.working,
    )
    full = ScalarFact(
        amount.vector.full_context, "estimated" if full_missing or provenance == "estimated" else provenance,
        caveats + (("missing_full_component",) if full_missing else ()), amount.bounds.full,
    )
    overall: Provenance = "estimated" if provenance == "estimated" or any(value is None for value in amount.vector.values) else provenance
    return TokenVectorFact(amount.vector, *components, working, full, overall, caveats)


def _epoch_vectors(observations: list[TokenObservation]) -> tuple[TokenVector, ...]:
    if not observations:
        return (TokenVector.unknown(),)
    if all(item.epoch is not None for item in observations):
        grouped: dict[int, TokenVector] = {}
        for item in observations:
            epoch = int(item.epoch)
            grouped[epoch] = grouped.get(epoch, TokenVector.unknown()).merge_known(item.vector)
        return tuple(grouped[key] for key in sorted(grouped))
    vectors: list[TokenVector] = []
    current = TokenVector.unknown()
    for item in observations:
        if item.vector.decreased_from(current):
            vectors.append(current)
            current = TokenVector.unknown()
        current = current.merge_known(item.vector)
    vectors.append(current)
    return tuple(vectors)


def _session_amount(observations: list[TokenObservation]) -> _Amount:
    return _combine(_Amount(vector, _bounds(vector)) for vector in _epoch_vectors(observations))


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


def _fact_count(value: int, caveat: str | None = None, *, lower_only: bool = False) -> ScalarFact:
    caveats = (caveat,) if caveat else ()
    return ScalarFact(None if lower_only else value, "estimated" if lower_only else "derived", caveats, value)


def aggregate_task_tree(
    *, root_id: str, sessions: Iterable[NormalizedSession],
    tokens: Iterable[TokenObservation], lifecycle: Iterable[LifecycleObservation],
    activities: Iterable[ActivityObservation], classified_working_tokens: int = 0,
    replay_baselines: Iterable[ReplayBaselineObservation] | None = None,
    tools: Iterable[ToolObservation] = (), files: Iterable[FileObservation] = (),
    tests: Iterable[TestRunObservation] = (),
) -> TaskTreeMetrics:
    """Aggregate one opaque root through its final root completion event."""
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
        item.observed_at for item in lifecycle_items
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
        if item.session_id in included and session_map[item.session_id].started_at <= item.observed_at <= cutoff:
            token_by_session[item.session_id].append(item)
    for items in token_by_session.values():
        items.sort(key=lambda item: (item.observed_at, item.sequence))
    recorded_by_session = {
        session_id: _session_amount(token_by_session.get(session_id, []))
        for session_id in session_ids
    }
    recorded = _combine(recorded_by_session.values())
    missing_finals = sum(not token_by_session.get(session_id) for session_id in session_ids)

    explicit = None if replay_baselines is None else {
        item.session_id: item for item in replay_baselines
        if item.session_id in included and item.observed_at <= cutoff
    }
    replay_by_session: dict[str, _Amount] = {}
    observed_baselines = zero_baselines = unconfirmed_edges = 0
    uncertain_sessions: set[str] = set()
    unconfirmed_kinds: dict[str, int] = defaultdict(int)
    for session_id in session_ids:
        if session_id == root_id:
            continue
        session = session_map[session_id]
        if not session.replay_eligible:
            unconfirmed_edges += 1
            uncertain_sessions.add(session_id)
            unconfirmed_kinds[session.edge_confidence_kind] += 1
            replay_by_session[session_id] = _Amount(TokenVector.zero(), _bounds(TokenVector.zero()))
            continue
        baseline = explicit.get(session_id) if explicit is not None else None
        if explicit is None:
            threshold = session.started_at + timedelta(seconds=1)
            candidates = tuple(
                item for item in token_by_session.get(session_id, ())
                if session.started_at <= item.observed_at <= threshold
            )
            baseline_vector = candidates[-1].vector if candidates else None
        else:
            baseline_vector = (
                baseline.vector
                if baseline is not None
                and session.started_at <= baseline.observed_at <= session.started_at + timedelta(seconds=1)
                else None
            )
        if baseline_vector is None:
            zero_baselines += 1
            uncertain_sessions.add(session_id)
            replay_by_session[session_id] = _Amount(TokenVector.zero(), _bounds(TokenVector.zero()))
        else:
            observed_baselines += 1
            replay_by_session[session_id] = _Amount(baseline_vector, _bounds(baseline_vector))
    replay = _combine(replay_by_session.values())

    unique_amounts: list[_Amount] = []
    for session_id in session_ids:
        amount = recorded_by_session[session_id]
        baseline = replay_by_session.get(session_id)
        if baseline is None:
            unique_amounts.append(amount)
            continue
        vector = amount.vector.subtract(baseline.vector)
        trustworthy = session_id not in uncertain_sessions
        unique_amounts.append(_Amount(vector, _bounds(vector) if trustworthy else _Bounds(0, 0, 0, 0, 0, 0)))
    unique = _combine(unique_amounts)

    uncertainty = tuple(
        f"unconfirmed_replay_edge:{kind}:{unconfirmed_kinds[kind]}"
        for kind in sorted(unconfirmed_kinds)
    )
    recorded_caveats = (f"missing_final_token:{missing_finals}",) if missing_finals else ()
    baseline_caveats = (f"zero_no_observation:{zero_baselines}",) if zero_baselines else ()
    unique_caveats = list(baseline_caveats + recorded_caveats)
    unique_caveats.extend(uncertainty)
    if cycle_edges:
        unique_caveats.append(f"cycle_edges:{cycle_edges}")
    baseline_uncertain = bool(zero_baselines or unconfirmed_edges)
    unique_uncertain = bool(baseline_uncertain or missing_finals)

    last_activity = {session_id: session_map[session_id].started_at for session_id in session_ids}
    all_activity = list(activities)
    all_activity.extend(ActivityObservation(item.session_id, item.observed_at) for item in lifecycle_items)
    all_activity.extend(ActivityObservation(item.session_id, item.observed_at) for item in tokens)
    for item in all_activity:
        if item.session_id in included and item.observed_at <= cutoff:
            last_activity[item.session_id] = max(last_activity[item.session_id], item.observed_at)
    agent_time_ms = sum(
        max(0, int((last_activity[key] - session_map[key].started_at).total_seconds() * 1000))
        for key in session_ids
    )
    wall_clock_ms = int((cutoff - root.started_at).total_seconds() * 1000)

    if isinstance(classified_working_tokens, bool) or classified_working_tokens < 0:
        raise ValueError("classified_working_tokens must be non-negative")
    if unique.vector.working_tokens is not None and classified_working_tokens > unique.vector.working_tokens:
        raise ValueError("classified_working_tokens exceeds unique working tokens")
    coverage = (
        classified_working_tokens / unique.vector.working_tokens
        if unique.vector.working_tokens not in (None, 0) else (0.0 if unique.vector.working_tokens == 0 else None)
    )

    def observed(items: Iterable[object]) -> tuple[object, ...]:
        return tuple(
            item for item in items
            if getattr(item, "session_id") in included
            and getattr(item, "observed_at") is not None
            and session_map[getattr(item, "session_id")].started_at <= getattr(item, "observed_at") <= cutoff
        )
    tool_items = observed(tools)
    file_items = observed(files)
    test_items = observed(tests)
    tool_keys = {(item.session_id, item.observation_id) for item in tool_items}
    instrumentation = {
        (item.session_id, item.observation_id) for item in tool_items
        if item.category == "instrumentation"
    }
    file_reads = {(item.session_id, item.observation_id) for item in file_items if item.operation == "read"}
    file_writes = {(item.session_id, item.observation_id) for item in file_items if item.operation == "write"}
    test_keys = {(item.session_id, item.observation_id) for item in test_items}

    return TaskTreeMetrics(
        root_id, cutoff, session_ids,
        _token_fact(recorded, "estimated" if missing_finals else "exact", recorded_caveats),
        _token_fact(replay, "estimated" if baseline_uncertain else "exact", baseline_caveats + uncertainty),
        _token_fact(unique, "estimated" if unique_uncertain else "derived", tuple(unique_caveats)),
        ScalarFact(len(session_ids), "exact"), ScalarFact(max(0, len(session_ids) - 1), "derived"),
        ScalarFact(wall_clock_ms, "derived"), ScalarFact(agent_time_ms, "derived"),
        ScalarFact(coverage, "derived" if coverage is not None else "estimated", () if coverage is not None else ("unknown_working_tokens",)),
        _fact_count(len(tool_keys), "observed_normalized_tool_spans"),
        _fact_count(len(instrumentation), "observed_instrumentation_spans"),
        _fact_count(len(file_reads), "observed_file_lower_bound", lower_only=True),
        _fact_count(len(file_writes), "observed_file_lower_bound", lower_only=True),
        _fact_count(len(test_keys), "detected_test_commands"),
        _fact_count(sum(item.scope == "targeted" for item in test_items), "detected_test_commands"),
        _fact_count(sum(item.scope == "full" for item in test_items), "detected_test_commands"),
        _fact_count(sum(item.retry_kind != "none" for item in test_items), "reconciled_test_retries"),
        observed_baselines, zero_baselines, unconfirmed_edges, cycle_edges,
    )
