"""Public report adaptation for persisted reconciled task facts."""

from __future__ import annotations

from dataclasses import replace

from .reconcile_types import ReconciledTask
from .report_operations import report_from_task_tree
from .report_semantics import SEMANTIC_PHASES, SemanticBreakdown, SemanticTokenFacts
from .reporting import NumericFact, PilotHealth, TaskReport, _from_scalar
from .storage import HydraStore
from .task_tree_types import ScalarFact


def _project_pilot_health(tasks: tuple[ReconciledTask, ...]) -> PilotHealth:
    task_count = len(tasks)
    missing = sum(item.semantic.marker_count == 0 for item in tasks)
    denominators = [item.metrics.unique.working_tokens for item in tasks]
    classified = sum(item.semantic.classified_working for item in tasks)
    if all(value is not None for value in denominators):
        denominator = sum(value for value in denominators if value is not None)
        provenance = (
            "derived" if all(
                item.metrics.unique.working.provenance in {"exact", "derived"}
                for item in tasks
            ) else "estimated"
        )
        coverage = NumericFact(
            0.0 if denominator == 0 else classified / denominator,
            "ratio", provenance,
            ("working_token_delta_weighted",) + (
                ("uncertain_unique_tokens",) if provenance == "estimated" else ()
            ),
        )
    else:
        coverage = NumericFact(None, "ratio", "estimated", ("unknown_working_tokens",))
    instrumentation_values = [item.metrics.instrumentation_calls.value for item in tasks]
    if all(value is not None for value in instrumentation_values):
        instrumentation = NumericFact(
            int(sum(value for value in instrumentation_values if value is not None)),
            "count", "derived",
        )
    else:
        instrumentation = NumericFact(
            None, "count", "estimated", ("instrumentation_count_unavailable",),
            sum(item.metrics.instrumentation_calls.known_lower_bound for item in tasks),
        )
    return PilotHealth(
        NumericFact(task_count, "count", "exact", lower_bound=task_count),
        NumericFact(
            0.0 if task_count == 0 else missing / task_count,
            "ratio", "derived", ("task_level_marker_rate",),
        ),
        coverage,
        NumericFact(
            sum(item.semantic.self_report_missing for item in tasks),
            "count", "exact",
        ),
        NumericFact(
            sum(item.semantic.semantic_conflicts for item in tasks),
            "count", "derived",
        ),
        instrumentation,
        NumericFact(
            None, "tokens", "estimated", ("instrumentation_overhead_not_calibrated",),
        ),
        NumericFact(
            sum(item.semantic.schema_diagnostics for item in tasks),
            "count", "derived",
        ),
    )


def _semantic_breakdown(task: ReconciledTask) -> SemanticBreakdown:
    semantic = task.semantic
    zero = ScalarFact(0, "derived", ("semantic_interval_allocation",))
    phases = {
        phase: SemanticTokenFacts(
            _from_scalar(semantic.phase_working.get(phase, zero), "tokens"),
            _from_scalar(semantic.phase_full_context.get(phase, zero), "tokens"),
            _from_scalar(semantic.phase_reasoning.get(phase, zero), "tokens"),
        )
        for phase in SEMANTIC_PHASES
    }
    return SemanticBreakdown(
        phases,
        SemanticTokenFacts(
            _from_scalar(semantic.unclassified_working, "tokens"),
            _from_scalar(semantic.unclassified_full_context, "tokens"),
            _from_scalar(semantic.unclassified_reasoning, "tokens"),
        ),
        NumericFact(semantic.marker_count, "count", "derived"),
        NumericFact(semantic.self_report_missing, "count", "derived"),
    )


def list_reconciled_reports(
    store: HydraStore, project_id: str, limit: int | None = None,
) -> tuple[TaskReport, ...]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    from .reconcile_engine import list_reconciled_tasks

    tasks = list_reconciled_tasks(store, project_id=project_id)
    pilot = _project_pilot_health(tasks)
    selected = tasks if limit is None else tasks[:limit]
    reports = []
    for task in selected:
        report = report_from_task_tree(
            task.metrics,
            public_ref=task.public_ref,
            complete=task.status == "complete",
            task_family=task.semantic.task_family,
            semantic_breakdown=_semantic_breakdown(task),
            semantic_conflicts=NumericFact(
                task.semantic.semantic_conflicts, "count", "derived",
            ),
            schema_diagnostics=NumericFact(
                task.semantic.schema_diagnostics, "count", "derived",
            ),
        )
        reports.append(replace(report, pilot_health=pilot))
    return tuple(reports)


def get_reconciled_report(
    store: HydraStore, project_id: str, public_ref: str,
) -> TaskReport:
    match = next((
        item for item in list_reconciled_reports(store, project_id=project_id)
        if item.task_ref == public_ref
    ), None)
    if match is None:
        raise KeyError("unknown public task reference")
    return match
