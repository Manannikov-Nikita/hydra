"""Public report adaptation for persisted reconciled task facts."""

from __future__ import annotations

from dataclasses import replace

from .reconcile_types import ReconciledTask
from .report_operations import evaluate_report_trends, report_from_task_tree
from .report_semantics import (
    DETERMINISTIC_TEST_CAUSES,
    FINISH_OUTCOMES,
    SCOPE_CHANGES,
    SEMANTIC_CAUSES,
    SEMANTIC_KINDS,
    SEMANTIC_PHASES,
    SemanticAnnotationSummary,
    SemanticBreakdown,
    SemanticMarkerSummary,
    SemanticTokenFacts,
    TestEvidenceRow,
    TestEvidenceSummary,
)
from .reporting import NumericFact, PilotHealth, TaskReport, _from_scalar
from .storage import HydraStore
from .task_tree_types import ScalarFact


def _project_pilot_health(tasks: tuple[ReconciledTask, ...]) -> PilotHealth:
    cohort = tuple(item for item in tasks if item.semantic.annotations.instrumented)
    task_count = len(cohort)
    missing = sum(
        item.semantic.annotations.finish_count == 0 or item.semantic.self_report_missing > 0
        for item in cohort
    )
    denominators = [item.metrics.unique.working_tokens for item in cohort]
    classified = sum(item.semantic.classified_working for item in cohort)
    if all(value is not None for value in denominators):
        denominator = sum(value for value in denominators if value is not None)
        provenance = (
            "derived" if all(
                item.metrics.unique.working.provenance in {"exact", "derived"}
                for item in cohort
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
    instrumentation_values = [item.metrics.instrumentation_calls.value for item in cohort]
    if all(value is not None for value in instrumentation_values):
        instrumentation = NumericFact(
            int(sum(value for value in instrumentation_values if value is not None)),
            "count", "derived",
        )
    else:
        instrumentation = NumericFact(
            None, "count", "estimated", ("instrumentation_count_unavailable",),
            sum(item.metrics.instrumentation_calls.known_lower_bound for item in cohort),
        )
    return PilotHealth(
        NumericFact(task_count, "count", "exact", lower_bound=task_count),
        NumericFact(
            0.0 if task_count == 0 else missing / task_count,
            "ratio", "derived", ("task_level_marker_rate",),
        ),
        coverage,
        NumericFact(
            sum(item.semantic.self_report_missing for item in cohort),
            "count", "exact",
        ),
        NumericFact(
            sum(item.semantic.semantic_conflicts for item in cohort),
            "count", "derived",
        ),
        instrumentation,
        NumericFact(
            None, "tokens", "estimated", ("instrumentation_overhead_not_calibrated",),
        ),
        NumericFact(
            sum(item.semantic.schema_diagnostics for item in cohort),
            "count", "derived",
        ),
        "not_started" if task_count == 0 else "measuring" if task_count < 5 else "awaiting_receipt",
        False,
        ("pilot_receipt_required",),
    )


def _annotation_summary(task: ReconciledTask) -> SemanticAnnotationSummary:
    source = task.semantic.annotations

    def model_group(names: tuple[str, ...], counts: dict[str, int]) -> dict[str, NumericFact]:
        return {
            name: NumericFact(
                counts.get(name, 0), "count", "model_reported", ("model_annotation_count",),
            )
            for name in names
        }

    deterministic = {
        name: NumericFact(
            source.deterministic_test_causes.get(name, 0), "count", "derived",
            ("deterministic_test_evidence_wins",),
        )
        for name in DETERMINISTIC_TEST_CAUSES
    }
    timeline = tuple(
        SemanticMarkerSummary(
            item.kind, item.phase, item.cause, item.scope_change, item.outcome,
            item.confidence, item.note, item.provenance,
        )
        for item in source.timeline
    )
    evidence_rows = tuple(
        TestEvidenceRow(
            item.scope, item.failure_cause, item.retry_kind, item.phase, item.cause,
            NumericFact(
                item.count, "count", "derived", ("deterministic_test_evidence",),
                lower_bound=item.count,
            ),
        )
        for item in source.test_evidence
    )
    test_evidence = TestEvidenceSummary(
        NumericFact(
            sum(item.count for item in source.test_evidence), "count", "derived",
            ("deterministic_test_evidence",),
            lower_bound=sum(item.count for item in source.test_evidence),
        ),
        evidence_rows,
    )
    return SemanticAnnotationSummary(
        NumericFact(sum(source.kind_counts.values()), "count", "model_reported"),
        model_group(SEMANTIC_KINDS, source.kind_counts),
        model_group(SEMANTIC_CAUSES, source.cause_counts),
        model_group(SCOPE_CHANGES, source.scope_change_counts),
        model_group(FINISH_OUTCOMES, source.finish_outcome_counts),
        deterministic,
        test_evidence,
        timeline,
        NumericFact(source.truncated_count, "count", "derived"),
        ("timeline_truncated",) if source.truncated_count else (),
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
        _annotation_summary(task),
    )


def list_reconciled_reports(
    store: HydraStore, project_id: str, limit: int | None = None,
) -> tuple[TaskReport, ...]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    from .reconcile_engine import list_reconciled_tasks

    tasks = list_reconciled_tasks(store, project_id=project_id)
    pilot = _project_pilot_health(tasks)
    reports = []
    for task in tasks:
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
    evaluated = evaluate_report_trends(reports)
    return evaluated if limit is None else evaluated[:limit]


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
