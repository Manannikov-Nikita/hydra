"""Pure adapters, comparisons, and trend-window selection for public reports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from .task_tree_types import TaskTreeMetrics

if TYPE_CHECKING:
    from .report_semantics import SemanticBreakdown
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
    from .report_semantics import SemanticBreakdown

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
        NumericFact(1, "count", "exact", lower_bound=1),
        _unavailable("ratio", "missing_marker_rate_unavailable"),
        semantic_coverage,
        _unavailable("count", "self_report_missing_unavailable"),
        conflicts,
        instrumentation_calls,
        overhead,
        diagnostics,
    )
    trend = TrendInput(
        public_ref, safe_family, bool(complete),
        _from_scalar(metrics.unique.working, "tokens"),
        _from_scalar(metrics.test_retries, "count"),
        _from_scalar(metrics.file_reads, "count"),
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
    from .reporting import REPORT_SCHEMA, ComparisonReport

    left = baseline.public_facts()
    right = current.public_facts()
    if left.keys() != right.keys():
        raise ValueError("report metric sets differ")
    return ComparisonReport(
        REPORT_SCHEMA, baseline.task_ref, current.task_ref,
        {name: _comparison(left[name], right[name]) for name in left},
        ("instrumentation_not_subtracted",),
    )


def _order(item: TaskReport) -> tuple[float, str, int | float]:
    value = item.deduplicated_tokens.working.value
    return (
        datetime.fromisoformat(item.last_activity_at.replace("Z", "+00:00")).timestamp(),
        item.task_ref, value if value is not None else -1,
    )


def build_trend_window(current: TaskReport, history: Iterable[TaskReport]) -> TrendWindow:
    """Return deterministic current plus at most four earlier comparable inputs."""
    from .reporting import TrendWindow

    if not current.completed or current.task_family is None:
        return TrendWindow(current.trend_input, ())
    unique = {item.task_ref: item for item in sorted(history, key=_order)}
    comparable = tuple(sorted((
        item for item in unique.values()
        if item.task_ref != current.task_ref and item.completed and item.task_family == current.task_family
    ), key=_order))
    return TrendWindow(current.trend_input, tuple(item.trend_input for item in comparable[-4:]))
