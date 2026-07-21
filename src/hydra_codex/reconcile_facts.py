"""Read-only deterministic fact assembly for project task reconciliation."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3
from typing import Iterable

from .lifecycle_timing import select_lifecycle_boundary
from .reconcile_annotations import (
    AnnotationFacts,
    build_annotation_facts,
    load_intervals,
    phase_at,
)
from .task_tree_storage import (
    _activities,
    _lifecycle,
    _optional_timestamp,
    _sessions,
    _trusted_semantic_activities,
)
from .reconcile_types import TaskPlan
from .reconcile_helpers import has_later_root_start, task_family as _task_family
from .reconcile_helpers import within_task_cutoff as _within_task_cutoff
from .task_tree_types import (
    LifecycleObservation,
    NormalizedSession,
    ScalarFact,
    TaskTreeMetrics,
    TokenVector,
)


@dataclass(frozen=True)
class DeltaFact:
    session_key: str
    event_key: str
    observed_at: datetime | None
    ordinal: int
    vector: TokenVector
    provenance: str
    phase: str | None
    cause: str | None


@dataclass(frozen=True)
class SemanticAssembly:
    task_family: str | None
    annotations: AnnotationFacts
    coverage: ScalarFact
    classified_working: int
    unclassified_working: ScalarFact
    unclassified_full_context: ScalarFact
    unclassified_reasoning: ScalarFact
    phase_working: dict[str, ScalarFact]
    phase_full_context: dict[str, ScalarFact]
    phase_reasoning: dict[str, ScalarFact]
    marker_count: int
    self_report_missing: int
    semantic_conflicts: int
    schema_diagnostics: int
    diagnostics: tuple[str, ...]
    diagnostic_counts: dict[str, int]


def _effective_parent(session: NormalizedSession, known: set[str]) -> str | None:
    if (
        session.parent_id in known
        and session.edge_confidence_kind in {"confirmed", "inferred"}
        and session.parent_id != session.session_id
    ):
        return session.parent_id
    return None


def _canonical_root(key: str, parents: dict[str, str | None]) -> str:
    path: list[str] = []
    positions: dict[str, int] = {}
    current = key
    while current not in positions and parents.get(current) is not None:
        positions[current] = len(path)
        path.append(current)
        current = parents[current]  # type: ignore[assignment]
    if current in positions:
        return min(path[positions[current]:])
    return current


def discover_task_plans(connection: sqlite3.Connection, project_id: str) -> tuple[TaskPlan, ...]:
    sessions = {item.session_id: item for item in _sessions(connection, project_id)}
    if not sessions:
        return ()
    parents = {key: _effective_parent(item, set(sessions)) for key, item in sessions.items()}
    grouped: dict[str, list[str]] = defaultdict(list)
    for key in sessions:
        grouped[_canonical_root(key, parents)].append(key)
    lifecycle = _lifecycle(connection, project_id)
    activity = _activities(connection, project_id)
    activity_by_session: dict[str, list[datetime]] = defaultdict(list)
    for item in activity:
        activity_by_session[item.session_id].append(item.observed_at)
    for item in _trusted_semantic_activities(connection, project_id):
        if item.session_id in sessions:
            activity_by_session[item.session_id].append(item.observed_at)
    completions: dict[str, list[LifecycleObservation]] = defaultdict(list)
    starts: dict[str, list[LifecycleObservation]] = defaultdict(list)
    for item in lifecycle:
        if item.kind == "task_complete":
            completions[item.session_id].append(item)
        elif item.kind == "task_started":
            starts[item.session_id].append(item)
    plans: list[TaskPlan] = []
    for root, members in grouped.items():
        eligible_completions = [
            completion for completion in completions.get(root, ())
            if (
                sessions[root].started_at is not None
                and sessions[root].started_at <= completion.observed_at
            ) or any(
                start.observed_at <= completion.observed_at
                for start in starts.get(root, ())
            )
        ]
        completion, _timing_conflicts = select_lifecycle_boundary(
            eligible_completions, starts.get(root, ()),
        )
        if completion is not None and not has_later_root_start(
            root, lifecycle, completion,
        ):
            cutoff = completion.observed_at
            status = "complete"
            cutoff_source = completion.logical_source_key
            cutoff_ordinal = completion.source_ordinal
            cutoff_timing_provenance = completion.timing_provenance
        else:
            candidates = [
                value for member in members
                for value in (
                    *((sessions[member].started_at,) if sessions[member].started_at is not None else ()),
                    *activity_by_session.get(member, ()),
                )
            ]
            if not candidates:
                continue
            cutoff = max(candidates)
            status = "incomplete"
            cutoff_source = None
            cutoff_ordinal = None
            cutoff_timing_provenance = "derived"
        included = tuple(sorted(
            member for member in members
            if sessions[member].started_at is None or sessions[member].started_at <= cutoff
        ))
        if root in included:
            plans.append(TaskPlan(
                root, status, cutoff, included, cutoff_source, cutoff_ordinal,
                cutoff_timing_provenance,
            ))
    return tuple(sorted(plans, key=lambda item: item.root_key))


def _subtract(current: TokenVector, previous: TokenVector) -> TokenVector | None:
    try:
        return current.subtract(previous)
    except ValueError:
        return None


def build_token_deltas(
    connection: sqlite3.Connection, project_id: str, plan: TaskPlan,
) -> tuple[tuple[DeltaFact, ...], Counter[str]]:
    placeholders = ",".join("?" for _ in plan.session_ids)
    rows = list(connection.execute(
        f"""SELECT t.session_key,t.source_digest,t.line_number,t.epoch,t.observed_at,
                    t.input_tokens,t.cached_input_tokens,t.output_tokens,t.reasoning_tokens
                    ,r.logical_source_key,t.event_key,t.source_family,
                    t.selection_provenance,t.selection_caveat
               FROM token_snapshots t LEFT JOIN rollout_sources r ON r.source_digest=t.source_digest
              WHERE t.project_id=? AND t.contributes_total=1 AND t.vector_valid=1
                AND t.session_key IN ({placeholders})
              ORDER BY t.session_key,t.epoch,
                       CASE WHEN t.observed_at IS NULL THEN 1 ELSE 0 END,
                       julianday(t.observed_at),
                       t.source_digest,t.line_number""",
        (project_id, *plan.session_ids),
    ))
    sessions = {item.session_id: item for item in _sessions(connection, project_id)}
    baselines = {
        str(row[0]): (TokenVector(row[2], row[3], row[4], row[5]), _optional_timestamp(row[1]))
        for row in connection.execute(
            f"""SELECT child_key,observed_at,input_tokens,cached_input_tokens,
                       output_tokens,reasoning_tokens FROM fork_baselines
                  WHERE child_key IN ({placeholders}) AND provenance='exact'
                    AND vector_valid=1""",
            plan.session_ids,
        )
    }
    intervals, _invalid_intervals = load_intervals(
        connection, project_id, plan.session_ids, plan.cutoff_at,
    )
    diagnostics: Counter[str] = Counter()
    previous: dict[tuple[str, int], TokenVector] = {}
    deltas: list[DeltaFact] = []
    for ordinal, row in enumerate(rows, start=1):
        observed = _optional_timestamp(row[4])
        session_key = str(row[0])
        session = sessions[session_key]
        if observed is not None and observed > plan.cutoff_at:
            continue
        if (
            observed is not None and session.started_at is not None
            and observed < session.started_at
        ):
            diagnostics["token_before_session_start"] += 1
            continue
        if observed is None:
            same_cutoff_source = (
                plan.cutoff_source_key is not None
                and plan.cutoff_source_ordinal is not None
                and row[9] == plan.cutoff_source_key
            )
            beyond_same_source_cutoff = (
                same_cutoff_source and int(row[2]) > plan.cutoff_source_ordinal
            )
            safely_ordered = (
                same_cutoff_source and int(row[2]) <= plan.cutoff_source_ordinal
            )
            if beyond_same_source_cutoff or not safely_ordered and row[11] != "app_server":
                diagnostics["ambiguous_token_placement"] += 1
                continue
        epoch = int(row[3])
        vector = TokenVector(row[5], row[6], row[7], row[8])
        state_key = (session_key, epoch)
        provenance = (
            "exact" if observed is not None and None not in vector.values
            and row[12] != "estimated" else "estimated"
        )
        amount_estimated = False
        if state_key not in previous:
            before = TokenVector.zero()
            if session_key != plan.root_key and epoch == 0:
                baseline = baselines.get(session_key)
                valid = (
                    session.replay_eligible and baseline is not None
                    and session.started_at is not None and baseline[1] is not None
                    and session.started_at <= baseline[1] <= session.started_at + timedelta(seconds=1)
                    and baseline[1] <= plan.cutoff_at
                )
                if valid:
                    before = baseline[0]
                else:
                    provenance = "estimated"
                    amount_estimated = True
                    diagnostics["replay_baseline_unavailable"] += 1
        else:
            before = previous[state_key]
        delta = _subtract(vector, before)
        previous[state_key] = vector
        if delta is None:
            diagnostics["nonmonotonic_token_snapshot"] += 1
            provenance = "estimated"
            amount_estimated = True
            delta = TokenVector.unknown()
        if delta == TokenVector.zero():
            continue
        phase = cause = None
        if observed is not None and not amount_estimated:
            phase, cause, overlap = phase_at(intervals.get(session_key, []), observed)
            if overlap:
                diagnostics["overlapping_semantic_intervals"] += 1
                phase = cause = None
                provenance = "estimated"
        deltas.append(DeltaFact(
            session_key, str(row[10] or f"{row[1]}:{row[2]}"), observed, ordinal,
            delta, provenance, phase, cause,
        ))
    from .otel_allocation import allocate_otel_hints

    allocation = allocate_otel_hints(
        connection, project_id, plan.session_ids, plan.cutoff_at, deltas,
        lambda session, observed: phase_at(intervals.get(session, []), observed),
    )
    if allocation.replaced_sessions:
        deltas = [
            item for item in deltas
            if item.session_key not in allocation.replaced_sessions
        ]
        deltas.extend(DeltaFact(
            item.session_key, item.event_key, item.observed_at, 0, item.vector,
            item.provenance, item.phase, item.cause,
        ) for item in allocation.facts)
        deltas.sort(key=lambda item: (
            item.observed_at is None, item.observed_at or plan.cutoff_at,
            item.session_key, item.event_key,
        ))
        deltas = [
            DeltaFact(
                item.session_key, item.event_key, item.observed_at, ordinal,
                item.vector, item.provenance, item.phase, item.cause,
            )
            for ordinal, item in enumerate(deltas, start=1)
        ]
    diagnostics.update(allocation.diagnostics)
    return tuple(deltas), diagnostics


def semantic_assembly(
    connection: sqlite3.Connection, project_id: str, plan: TaskPlan,
    metrics: TaskTreeMetrics, deltas: Iterable[DeltaFact], initial: Counter[str],
) -> SemanticAssembly:
    diagnostics = Counter(initial)
    phase_values: dict[str, dict[str, int]] = {
        name: defaultdict(int) for name in ("working", "full_context", "reasoning")
    }
    phase_unknown: dict[str, set[str]] = {
        name: set() for name in phase_values
    }
    unclassified_values = {name: 0 for name in phase_values}
    unclassified_unknown = {name: False for name in phase_values}
    classified = 0
    for delta in deltas:
        values = {
            "working": delta.vector.working_tokens,
            "full_context": delta.vector.full_context,
            "reasoning": delta.vector.reasoning_output_tokens,
        }
        phase = delta.phase
        for component, value in values.items():
            if phase is not None:
                if value is None:
                    phase_unknown[component].add(phase)
                    diagnostics[f"incomplete_{component}_delta"] += 1
                else:
                    phase_values[component][phase] += value
            elif value is None:
                unclassified_unknown[component] = True
                diagnostics[f"incomplete_{component}_delta"] += 1
            else:
                unclassified_values[component] += value
        if phase is not None and values["working"] is not None:
            classified += values["working"]
    denominator = metrics.unique.working_tokens
    if denominator is None:
        coverage = ScalarFact(None, "estimated", ("unknown_working_tokens",))
    elif classified > denominator:
        diagnostics["semantic_allocation_exceeds_unique_tokens"] += 1
        coverage = ScalarFact(None, "estimated", ("semantic_allocation_conflict",), 0)
    else:
        provenance = "derived" if metrics.unique.working.provenance in {"exact", "derived"} else "estimated"
        caveats = () if provenance == "derived" else ("uncertain_unique_tokens",)
        coverage = ScalarFact(0.0 if denominator == 0 else classified / denominator, provenance, caveats)

    def phase_facts(component: str) -> dict[str, ScalarFact]:
        phases = set(phase_values[component]) | phase_unknown[component]
        return {
            phase: ScalarFact(
                None, "estimated", ("incomplete_phase_token_component",),
                phase_values[component].get(phase, 0),
            ) if phase in phase_unknown[component] else ScalarFact(
                phase_values[component].get(phase, 0), "derived",
                ("semantic_interval_allocation",),
            )
            for phase in sorted(phases)
        }

    def unclassified_fact(component: str, total: ScalarFact) -> ScalarFact:
        known = unclassified_values[component]
        phase_known = sum(phase_values[component].values())
        if total.value is not None and not phase_unknown[component]:
            remainder = int(total.value) - phase_known
            if remainder >= 0:
                provenance = (
                    "derived" if total.provenance in {"exact", "derived"} else "estimated"
                )
                caveats = (
                    ("semantic_unclassified_remainder",)
                    if provenance == "derived"
                    else ("semantic_unclassified_remainder", "uncertain_unique_tokens")
                )
                return ScalarFact(max(known, remainder), provenance, caveats)
            diagnostics[f"semantic_{component}_allocation_exceeds_unique"] += 1
        caveats = {"unknown_unique_token_component"}
        if unclassified_unknown[component]:
            caveats.add("incomplete_unclassified_token_component")
        if phase_unknown[component]:
            caveats.add("incomplete_phase_token_component")
        lower_bound = known
        if not phase_unknown[component]:
            lower_bound = max(
                lower_bound, int(total.known_lower_bound) - phase_known,
            )
        return ScalarFact(None, "estimated", tuple(sorted(caveats)), lower_bound)

    phase_working = phase_facts("working")
    phase_full = phase_facts("full_context")
    phase_reasoning = phase_facts("reasoning")
    unclassified = unclassified_fact("working", metrics.unique.working)
    unclassified_full = unclassified_fact("full_context", metrics.unique.full)
    unclassified_reasoning = unclassified_fact("reasoning", metrics.unique.reasoning)
    placeholders = ",".join("?" for _ in plan.session_ids)
    staged: Counter[str] = Counter()
    for kind, observed_at in connection.execute(
        f"""SELECT fact_kind,observed_at FROM semantic_fact_staging
              WHERE project_id=? AND session_key IN ({placeholders})""",
        (project_id, *plan.session_ids),
    ):
        observed = _optional_timestamp(observed_at)
        if observed is None:
            diagnostics["semantic:invalid_fact_timestamp"] += 1
        elif observed <= plan.cutoff_at:
            staged[str(kind)] += 1
    old_conflict_rows = list(connection.execute(
        f"""SELECT c.source_digest,c.line_number,e.observed_at,l.logical_source_key
              FROM semantic_conflicts c
              JOIN rollout_sources r ON r.source_digest=c.source_digest
              JOIN rollout_logical_sources l ON l.logical_source_key=r.logical_source_key
              LEFT JOIN rollout_revision_events re
                ON re.revision_digest=c.source_digest AND re.source_ordinal=c.line_number
              LEFT JOIN rollout_events e ON e.event_key=re.event_key
             WHERE l.project_id=? AND l.session_key IN ({placeholders})""",
        (project_id, *plan.session_ids),
    ))
    old_conflicts: set[str] = set()
    for source, line, value, logical_source in old_conflict_rows:
        if _within_task_cutoff(plan, value, logical_source, line):
            old_conflicts.add(f"{source}:{line}")
        else:
            diagnostics["ambiguous_legacy_conflict_placement"] += 1
    annotations = build_annotation_facts(
        connection, project_id, plan.session_ids, plan.cutoff_at,
    )
    if annotations.invalid_test_times:
        diagnostics["semantic:invalid_test_timestamp"] += len(annotations.invalid_test_times)
    schema_rows = list(connection.execute(
        f"""SELECT d.envelope_kind,e.observed_at,l.logical_source_key,d.line_number
              FROM rollout_diagnostics d
              JOIN rollout_sources r ON r.source_digest=d.source_digest
              JOIN rollout_logical_sources l ON l.logical_source_key=r.logical_source_key
              LEFT JOIN rollout_revision_events re
                ON re.revision_digest=d.source_digest AND re.source_ordinal=d.line_number
              LEFT JOIN rollout_events e ON e.event_key=re.event_key
             WHERE l.project_id=? AND l.session_key IN ({placeholders})
             """,
        (project_id, *plan.session_ids),
    ))
    for code, value, logical_source, line in schema_rows:
        if _within_task_cutoff(plan, value, logical_source, line):
            diagnostics[f"schema:{code}"] += 1
        else:
            diagnostics["ambiguous_schema_diagnostic_placement"] += 1
    event_schema_rows = list(connection.execute(
        f"""SELECT i.issue_code,e.observed_at,e.session_key
              FROM codex_event_issues i
              JOIN codex_event_sources s ON s.source_digest=i.source_digest
              LEFT JOIN codex_events e
                ON e.source_digest=i.source_digest
               AND e.source_ordinal=i.source_ordinal
             WHERE s.project_id=? AND e.session_key IN ({placeholders})""",
        (project_id, *plan.session_ids),
    ))
    for code, value, _session_key in event_schema_rows:
        observed = _optional_timestamp(value)
        if observed is not None and observed <= plan.cutoff_at:
            diagnostics[f"schema:event:{code}"] += 1
        else:
            diagnostics["ambiguous_event_schema_diagnostic_placement"] += 1
    for code, count in staged.items():
        if code not in {"self_report_missing", "semantic_conflict"}:
            diagnostics[f"semantic:{code}"] += int(count)
    if annotations.family_conflict:
        diagnostics["task_family_conflict"] += 1
    if annotations.invalid_annotation_timestamps:
        diagnostics["semantic:invalid_annotation_timestamp"] += annotations.invalid_annotation_timestamps
    if annotations.invalid_interval_timestamps:
        diagnostics["semantic:invalid_interval_timestamp"] += annotations.invalid_interval_timestamps
    schema_count = sum(count for code, count in diagnostics.items() if code.startswith(("schema:", "semantic:")))
    return SemanticAssembly(
        annotations.task_family, annotations, coverage, classified, unclassified, unclassified_full,
        unclassified_reasoning, dict(phase_working), dict(phase_full),
        dict(phase_reasoning), annotations.marker_count, staged["self_report_missing"],
        staged["semantic_conflict"] + len(old_conflicts | set(annotations.detected_conflicts)), schema_count,
        tuple(sorted(diagnostics)), dict(diagnostics),
    )
