"""Pure adapters, comparisons, and trend-window selection for public reports."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from .task_tree_types import TaskTreeMetrics

if TYPE_CHECKING:
    from .report_semantics import SemanticBreakdown, TrendAssessment
    from .reporting import ComparisonReport, NumericFact, TaskReport, TrendWindow


def report_from_task_tree(
    metrics: TaskTreeMetrics,
    *,
    public_ref: str,
    complete: bool = True,
    task_family: str | None = None,
    semantic_breakdown: SemanticBreakdown | None = None,
    semantic_conflicts: NumericFact | None = None,
    schema_diagnostics: NumericFact | None = None,
    instrumentation_overhead: NumericFact | None = None,
) -> TaskReport:
    """Adapt trusted task-tree facts without retaining its private root or session list."""
    from .reporting import (
        REPORT_SCHEMA,
        NumericFact,
        PilotHealth,
        TaskReport,
        TokenFacts,
        TrendInput,
        _from_scalar,
        _safe_family,
        _unavailable,
    )
    from .report_semantics import SemanticBreakdown, TrendAssessment

    safe_family = None if task_family is None else _safe_family(task_family)
    breakdown = semantic_breakdown or SemanticBreakdown.empty()
    conflicts = semantic_conflicts or _unavailable("count", "semantic_conflicts_unavailable")
    diagnostics = schema_diagnostics or _unavailable("count", "schema_diagnostics_unavailable")
    overhead = instrumentation_overhead or _unavailable(
        "tokens", "instrumentation_overhead_not_calibrated",
    )
    semantic_coverage = _from_scalar(metrics.semantic_coverage, "ratio")
    instrumentation_calls = _from_scalar(metrics.instrumentation_calls, "count")
    pilot = PilotHealth(
        NumericFact(0, "count", "exact", lower_bound=0),
        NumericFact(0.0, "ratio", "derived", ("no_instrumented_tasks",)),
        semantic_coverage,
        NumericFact(0, "count", "exact"),
        conflicts,
        instrumentation_calls,
        overhead,
        diagnostics,
        "not_started",
        False,
        ("pilot_receipt_required",),
    )
    trend = TrendInput(
        public_ref, safe_family, bool(complete),
        _from_scalar(metrics.unique.working, "tokens"),
        _from_scalar(metrics.test_retries, "count"),
        _unavailable("count", "read_amplification_unavailable"),
        _unavailable("count", "review_fix_cycles_unavailable"),
        _unavailable("count", "compactions_unavailable"),
    )
    return TaskReport(
        REPORT_SCHEMA, public_ref, "complete" if complete else "incomplete",
        metrics.cutoff_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), safe_family,
        TokenFacts.from_tree(metrics.recorded), TokenFacts.from_tree(metrics.unique),
        _from_scalar(metrics.root_wall_clock_ms, "milliseconds"),
        _from_scalar(metrics.agent_time_ms, "milliseconds"),
        _from_scalar(metrics.sessions, "count"), _from_scalar(metrics.subagents, "count"),
        _from_scalar(metrics.tool_calls, "count"), instrumentation_calls,
        _from_scalar(metrics.file_reads, "count"), _from_scalar(metrics.file_writes, "count"),
        _from_scalar(metrics.test_runs, "count"), _from_scalar(metrics.targeted_test_runs, "count"),
        _from_scalar(metrics.full_test_runs, "count"), _from_scalar(metrics.test_retries, "count"),
        semantic_coverage, breakdown, conflicts, diagnostics, overhead, pilot, trend,
        TrendAssessment.unavailable(),
    )


def _comparison(baseline: NumericFact, current: NumericFact) -> MetricComparison:
    from .reporting import MetricComparison, NumericFact, _unavailable

    if baseline.unit != current.unit:
        raise ValueError("comparison units differ")
    if baseline.value is None or current.value is None:
        unavailable = _unavailable(baseline.unit, "comparison_value_unavailable")
        percentage = _unavailable("percent", "comparison_value_unavailable")
        return MetricComparison(baseline, current, unavailable, percentage)
    delta_value = current.value - baseline.value
    source_caveats = tuple(dict.fromkeys(
        tuple(f"baseline:{item}" for item in baseline.caveats)
        + tuple(f"current:{item}" for item in current.caveats)
    ))
    provenance = (
        "derived" if baseline.provenance in {"exact", "derived"}
        and current.provenance in {"exact", "derived"} else "estimated"
    )
    delta = NumericFact(delta_value, baseline.unit, provenance, source_caveats)
    if baseline.value == 0:
        percentage = _unavailable("percent", "zero_baseline_percentage_unavailable")
    else:
        percentage = NumericFact(
            delta_value / baseline.value * 100, "percent", provenance, source_caveats,
        )
    return MetricComparison(baseline, current, delta, percentage)


def compare_reports(baseline: TaskReport, current: TaskReport) -> ComparisonReport:
    """Compare public facts without subtracting Hydra instrumentation from tokens."""
    from .reporting import COMPARISON_SCHEMA, ComparisonReport

    left = baseline.public_facts()
    right = current.public_facts()
    if left.keys() != right.keys():
        raise ValueError("report metric sets differ")
    verdict, reasons = _comparison_verdict(baseline, current)
    return ComparisonReport(
        COMPARISON_SCHEMA, baseline.task_ref, current.task_ref,
        verdict, reasons,
        {name: _comparison(left[name], right[name]) for name in left},
        ("instrumentation_not_subtracted",),
    )


def _usable_comparison_fact(value: NumericFact) -> bool:
    return (
        value.value is not None
        and value.provenance in {"exact", "derived"}
        and value.lower_bound in {None, value.value}
    )


def _comparison_verdict(
    baseline: TaskReport, current: TaskReport,
) -> tuple[str, tuple[str, ...]]:
    """Gate interpretation while retaining every raw metric comparison."""
    if (
        baseline.task_family is not None
        and current.task_family is not None
        and baseline.task_family != current.task_family
    ):
        return "not_comparable", ("task_family_mismatch",)
    if (
        (
            baseline.task_family is not None
            and baseline.trend_input.task_family is None
        )
        or (
            current.task_family is not None
            and current.trend_input.task_family is None
        )
    ):
        return "not_comparable", ("automatic_comparison_excluded",)

    unknown: list[str] = []
    if baseline.task_family is None or current.task_family is None:
        unknown.append("task_family_unavailable")
    if not baseline.completed or not current.completed:
        unknown.append("task_incomplete")
    if unknown:
        return "unknown", tuple(unknown)

    partial: list[str] = []
    if any(
        not item.pilot_health.receipt_verified
        or item.pilot_health.status != "verified"
        for item in (baseline, current)
    ):
        partial.append("pilot_receipt_unverified")
    required = tuple(
        getattr(item.trend_input, name)
        for item in (baseline, current)
        for name in (
            "working_tokens", "test_retries", "read_amplification",
            "review_fix_cycles", "compactions",
        )
    )
    if any(not _usable_comparison_fact(value) for value in required):
        partial.append("evidence_partial")
    if partial:
        return "partial", tuple(partial)
    return "comparable", ()


def _instant(item: TaskReport) -> float:
    return datetime.fromisoformat(
        item.last_activity_at.replace("Z", "+00:00"),
    ).timestamp()


def _order(item: TaskReport) -> tuple[float, str, int | float]:
    value = item.deduplicated_tokens.working.value
    return (
        _instant(item),
        item.task_ref, value if value is not None else -1,
    )


def build_trend_window(current: TaskReport, history: Iterable[TaskReport]) -> TrendWindow:
    """Return deterministic current plus at most four earlier comparable inputs."""
    from .reporting import TrendWindow

    if not current.completed or current.trend_input.task_family is None:
        return TrendWindow(current.trend_input, ())
    unique = {item.task_ref: item for item in sorted(history, key=_order)}
    current_instant = _instant(current)
    comparable = tuple(sorted((
        item for item in unique.values()
        if (
            item.task_ref != current.task_ref
            and item.completed
            and item.trend_input.task_family == current.trend_input.task_family
            and _instant(item) < current_instant
        )
    ), key=_order))
    return TrendWindow(current.trend_input, tuple(item.trend_input for item in comparable[-4:]))


def _comparable(value: TrendInput):
    from .semantic import ComparableTask

    if value.task_family is None:
        return None
    retries_complete = (
        value.test_retries.value is not None
        and value.test_retries.lower_bound == value.test_retries.value
        and value.test_retries.provenance == "derived"
        and value.test_retries.caveats == ("reconciled_test_retries",)
    )
    return ComparableTask(
        value.task_family,
        value.completed,
        value.working_tokens.value,
        value.test_retries.value,
        value.read_amplification.value,
        value.review_fix_cycles.value,
        value.compactions.value,
        value.compactions.value is not None and value.compactions.provenance == "exact",
        value.working_tokens.value is not None,
        value.working_tokens.provenance,
        value.test_retries.provenance,
        value.read_amplification.provenance,
        value.review_fix_cycles.provenance,
        value.compactions.provenance,
        retries_complete,
    )


def _assessment(window: TrendWindow):
    from .report_semantics import TrendAssessment
    from .reporting import NumericFact
    from .semantic import evaluate_trend

    current = _comparable(window.current)
    if current is None:
        return TrendAssessment.unavailable("task_family_unavailable")
    prior = tuple(
        candidate for item in window.prior if (candidate := _comparable(item)) is not None
    )
    result = evaluate_trend(current, prior)
    signal = "test_retries" if result.corroborating_signal == "test_reruns" else result.corroborating_signal
    return TrendAssessment(
        result.warning,
        signal,
        NumericFact(
            result.baseline_working_tokens.value, "tokens",
            result.baseline_working_tokens.provenance, result.baseline_working_tokens.caveats,
        ),
        NumericFact(
            result.token_growth.value, "tokens",
            result.token_growth.provenance, result.token_growth.caveats,
        ),
        NumericFact(
            result.signal_growth.value, "count",
            result.signal_growth.provenance, result.signal_growth.caveats,
        ),
        result.caveats,
    )


def evaluate_report_trends(reports: Iterable[TaskReport]) -> tuple[TaskReport, ...]:
    """Attach conservative assessments using only four earlier comparable tasks."""
    supplied = tuple(reports)
    chronological = tuple(sorted(supplied, key=_order))
    evaluated = {
        current.task_ref: replace(
            current,
            trend_result=_assessment(build_trend_window(current, chronological[:index])),
        )
        for index, current in enumerate(chronological)
    }
    return tuple(evaluated[item.task_ref] for item in supplied)
