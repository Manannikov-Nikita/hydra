"""Cross-model coherence checks for one canonical pilot audit snapshot."""

from __future__ import annotations

from collections.abc import Mapping

from .reporting import NumericFact, TaskReport


def _mismatch() -> ValueError:
    return ValueError("pilot/report task mismatch")


def _known_fact_matches(pilot_value: object, report_fact: NumericFact) -> None:
    if report_fact.value is not None and pilot_value != report_fact.value:
        raise _mismatch()


def validate_task_coherence(
    pilot_task: object,
    report: TaskReport,
) -> None:
    """Reject overlapping public facts from different logical snapshots."""
    if not isinstance(pilot_task, Mapping):
        raise _mismatch()
    if (
        pilot_task.get("task_ref") != report.task_ref
        or report.status != "complete"
        or pilot_task.get("completed_at") != report.last_activity_at
        or pilot_task.get("task_family") != report.task_family
        or pilot_task.get("coverage") != report.semantic_coverage.value
    ):
        raise _mismatch()
    _known_fact_matches(
        pilot_task.get("semantic_conflicts"),
        report.semantic_conflicts,
    )
    _known_fact_matches(
        pilot_task.get("schema_diagnostics"),
        report.schema_diagnostics,
    )

    expected_trend_eligible = report.trend_input.task_family is not None
    if pilot_task.get("trend_eligible") is not expected_trend_eligible:
        raise _mismatch()

    annotations = report.semantic_breakdown.annotations
    marker_total = annotations.total_count.value
    if (
        marker_total is not None
        and pilot_task.get("instrumented") is not (marker_total > 0)
    ):
        raise _mismatch()
    finish_total = annotations.kind_counts["finish"].value
    if (
        finish_total is not None
        and pilot_task.get("finish_missing") is not (finish_total == 0)
    ):
        raise _mismatch()
