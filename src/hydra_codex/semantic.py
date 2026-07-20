"""Pure semantic reconciliation and conservative trend detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Iterable


_PROVENANCE = {"exact", "derived", "model_reported", "estimated"}
_MARK_KINDS = {"phase", "blocker", "finish"}


def _non_negative(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class NumericMetric:
    value: int | float | None
    provenance: str
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.provenance not in _PROVENANCE:
            raise ValueError(f"invalid provenance: {self.provenance!r}")


@dataclass(frozen=True)
class TokenDelta:
    observed_at: str | None
    working_tokens: int
    full_context: int
    reasoning_tokens: int
    event_key: str | None = None
    ordinal: int = 0
    provenance: str = "exact"

    def __post_init__(self) -> None:
        for field in ("working_tokens", "full_context", "reasoning_tokens", "ordinal"):
            object.__setattr__(self, field, _non_negative(getattr(self, field), field))
        if self.provenance not in _PROVENANCE:
            raise ValueError(f"invalid provenance: {self.provenance!r}")


@dataclass(frozen=True)
class SemanticMark:
    kind: str
    phase: str
    cause: str
    observed_at: str
    sequence: int
    annotation_key: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _MARK_KINDS:
            raise ValueError(f"invalid semantic mark kind: {self.kind!r}")
        if not isinstance(self.phase, str) or not self.phase:
            raise ValueError("phase must be non-empty text")
        if not isinstance(self.cause, str) or not self.cause:
            raise ValueError("cause must be non-empty text")
        object.__setattr__(self, "sequence", _non_negative(self.sequence, "sequence"))


@dataclass(frozen=True)
class SemanticInterval:
    phase: str
    started_at: str
    finished_at: str | None
    opened_by: str
    closed_by: str | None
    provenance: str = "model_reported"


@dataclass(frozen=True)
class SemanticResult:
    intervals: tuple[SemanticInterval, ...]
    phase_tokens: dict[str, NumericMetric]
    phase_full_context: dict[str, NumericMetric]
    phase_reasoning: dict[str, NumericMetric]
    unclassified_tokens: NumericMetric
    unclassified_full_context: NumericMetric
    unclassified_reasoning: NumericMetric
    coverage: NumericMetric
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class CauseResolution:
    value: str | None
    provenance: str
    conflict: bool
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComparableTask:
    task_family: str
    completed: bool
    working_tokens: int | None
    test_reruns: int | None
    read_amplification: int | None
    review_fix_cycles: int | None
    compactions: int | None
    compaction_normalized: bool
    metrics_complete: bool
    working_tokens_provenance: str = "exact"
    test_reruns_provenance: str = "exact"
    read_amplification_provenance: str = "exact"
    review_fix_cycles_provenance: str = "exact"
    compactions_provenance: str = "exact"

    def __post_init__(self) -> None:
        if not isinstance(self.task_family, str) or not self.task_family:
            raise ValueError("task_family must be non-empty text")
        for field in ("completed", "compaction_normalized", "metrics_complete"):
            if not isinstance(getattr(self, field), bool):
                raise ValueError(f"{field} must be boolean")
        for field in (
            "working_tokens", "test_reruns", "read_amplification",
            "review_fix_cycles", "compactions",
        ):
            value = getattr(self, field)
            if value is not None:
                _non_negative(value, field)
        for field in (
            "working_tokens_provenance", "test_reruns_provenance",
            "read_amplification_provenance", "review_fix_cycles_provenance",
            "compactions_provenance",
        ):
            if getattr(self, field) not in _PROVENANCE:
                raise ValueError(f"invalid {field}: {getattr(self, field)!r}")


@dataclass(frozen=True)
class TrendResult:
    warning: bool
    corroborating_signal: str | None
    baseline_working_tokens: NumericMetric
    token_growth: NumericMetric
    signal_growth: NumericMetric
    caveats: tuple[str, ...]


@dataclass(frozen=True)
class _BoundedInterval:
    result: SemanticInterval
    start: tuple[datetime, int]
    end: tuple[datetime, int] | None


def resolve_semantic_cause(deterministic: str | None, model_reported: str | None) -> CauseResolution:
    """Prefer deterministic evidence while retaining an explicit conflict flag."""
    if deterministic is not None:
        conflict = model_reported is not None and deterministic != model_reported
        return CauseResolution(
            deterministic,
            "derived",
            conflict,
            ("deterministic_evidence_wins", "semantic_conflict") if conflict else ("deterministic_evidence_wins",),
        )
    if model_reported is not None:
        return CauseResolution(model_reported, "model_reported", False)
    return CauseResolution(None, "estimated", False, ("semantic_cause_unavailable",))


def _normalize_marks(marks: Iterable[SemanticMark]) -> tuple[tuple[SemanticMark, datetime], tuple[str, ...]]:
    diagnostics: set[str] = set()
    unique = set(marks)
    materialized = tuple(marks) if not isinstance(marks, tuple) else marks
    if len(unique) != len(materialized):
        diagnostics.add("duplicate_annotation")

    by_identity: dict[tuple[str, object], list[SemanticMark]] = {}
    for mark in unique:
        identity = ("key", mark.annotation_key) if mark.annotation_key is not None else ("sequence", mark.sequence)
        by_identity.setdefault(identity, []).append(mark)

    chosen: list[tuple[SemanticMark, datetime]] = []
    for candidates in by_identity.values():
        valid = [(mark, _timestamp(mark.observed_at)) for mark in candidates]
        valid = [(mark, observed) for mark, observed in valid if observed is not None]
        if not valid:
            diagnostics.add("invalid_annotation_timestamp")
            continue
        if len(candidates) > 1:
            diagnostics.add("conflicting_annotation")
        chosen.append(min(valid, key=lambda item: (item[1], item[0].sequence, item[0].kind, item[0].phase, item[0].cause)))

    chosen.sort(key=lambda item: (item[1], item[0].sequence, item[0].kind, item[0].phase, item[0].cause))
    if any(left[0].sequence >= right[0].sequence for left, right in zip(chosen, chosen[1:])):
        diagnostics.add("out_of_order_annotation")
    return tuple(chosen), tuple(sorted(diagnostics))


def _build_intervals(marks: tuple[tuple[SemanticMark, datetime], ...]) -> tuple[tuple[_BoundedInterval, ...], tuple[str, ...]]:
    diagnostics: set[str] = set()
    intervals: list[_BoundedInterval] = []
    active: tuple[str, tuple[datetime, int], str, str] | None = None
    finished = False

    def close(mark: SemanticMark, observed: datetime) -> None:
        nonlocal active
        if active is None:
            diagnostics.add("no_active_phase")
            return
        phase, start, started_at, opened_by = active
        end = (observed, mark.sequence)
        if start < end:
            intervals.append(_BoundedInterval(
                SemanticInterval(phase, started_at, mark.observed_at, opened_by, mark.kind), start, end,
            ))

    for mark, observed in marks:
        position = (observed, mark.sequence)
        if finished:
            diagnostics.add("annotation_after_finish")
            continue
        if mark.kind == "phase":
            if active is not None:
                close(mark, observed)
            active = (mark.phase, position, mark.observed_at, "phase")
        elif mark.kind == "blocker":
            if active is None:
                diagnostics.add("no_active_phase")
                continue
            retained_phase = active[0]
            close(mark, observed)
            active = (retained_phase, position, mark.observed_at, "blocker")
        else:
            close(mark, observed)
            active = None
            finished = True

    if active is not None:
        phase, start, started_at, opened_by = active
        intervals.append(_BoundedInterval(
            SemanticInterval(phase, started_at, None, opened_by, None), start, None,
        ))
    return tuple(intervals), tuple(sorted(diagnostics))


def _metric_map(values: dict[str, int]) -> dict[str, NumericMetric]:
    return {
        phase: NumericMetric(value, "derived", ("semantic_interval_allocation",))
        for phase, value in sorted(values.items())
    }


def reconcile_semantics(deltas: Iterable[TokenDelta], marks: Iterable[SemanticMark]) -> SemanticResult:
    """Allocate exact timestamped token deltas to deterministic semantic intervals."""
    normalized_marks, mark_diagnostics = _normalize_marks(tuple(marks))
    bounded, interval_diagnostics = _build_intervals(normalized_marks)
    diagnostics = set(mark_diagnostics) | set(interval_diagnostics)
    phase_working: dict[str, int] = {}
    phase_full: dict[str, int] = {}
    phase_reasoning: dict[str, int] = {}
    unclassified = [0, 0, 0]
    exact_timestamped_total = 0
    classified_total = 0
    keyed: dict[str, list[TokenDelta]] = {}
    normalized_deltas: list[TokenDelta] = []
    for delta in deltas:
        if delta.event_key is None:
            normalized_deltas.append(delta)
        else:
            keyed.setdefault(delta.event_key, []).append(delta)
    for observations in keyed.values():
        candidates = set(observations)
        if len(observations) > len(candidates):
            diagnostics.add("duplicate_token_delta")
        if len(candidates) > 1:
            diagnostics.add("conflicting_token_delta")
            diagnostics.add("semantic_conflict")
        normalized_deltas.append(min(candidates, key=lambda item: (
            _timestamp(item.observed_at) is None,
            _timestamp(item.observed_at) or datetime(9999, 12, 31, tzinfo=timezone.utc),
            item.ordinal,
            item.working_tokens,
            item.full_context,
            item.reasoning_tokens,
            item.provenance,
        )))

    normalized_deltas.sort(key=lambda item: (
        _timestamp(item.observed_at) is None,
        _timestamp(item.observed_at) or datetime(9999, 12, 31, tzinfo=timezone.utc),
        item.ordinal,
        item.event_key or "",
        item.working_tokens,
    ))
    trusted_ordinals = [item.ordinal for item in normalized_deltas if _timestamp(item.observed_at) is not None and item.ordinal > 0]
    if any(left >= right for left, right in zip(trusted_ordinals, trusted_ordinals[1:])):
        diagnostics.add("out_of_order_token_delta")

    for delta in normalized_deltas:
        observed = _timestamp(delta.observed_at)
        if observed is None:
            diagnostics.add("missing_timestamp" if delta.observed_at is None else "invalid_token_timestamp")
        if delta.provenance != "exact":
            diagnostics.add("inexact_token_delta")
        usable = observed is not None and delta.provenance == "exact"
        if usable:
            exact_timestamped_total += delta.working_tokens
        position = None if observed is None else (observed, delta.ordinal)
        interval = next((candidate for candidate in bounded if position is not None and candidate.start <= position and (candidate.end is None or position < candidate.end)), None)
        if usable and interval is not None:
            phase = interval.result.phase
            phase_working[phase] = phase_working.get(phase, 0) + delta.working_tokens
            phase_full[phase] = phase_full.get(phase, 0) + delta.full_context
            phase_reasoning[phase] = phase_reasoning.get(phase, 0) + delta.reasoning_tokens
            classified_total += delta.working_tokens
        else:
            unclassified[0] += delta.working_tokens
            unclassified[1] += delta.full_context
            unclassified[2] += delta.reasoning_tokens

    unclassified_caveats = tuple(sorted(diagnostics & {
        "missing_timestamp", "invalid_token_timestamp", "inexact_token_delta",
    }))
    if unclassified[0] and not unclassified_caveats:
        unclassified_caveats = ("outside_labeled_interval",)
    if exact_timestamped_total == 0:
        coverage = NumericMetric(None, "estimated", ("coverage_denominator_unavailable",))
    else:
        coverage = NumericMetric(classified_total / exact_timestamped_total, "derived", ("working_token_delta_weighted",))
    return SemanticResult(
        tuple(item.result for item in bounded),
        _metric_map(phase_working),
        _metric_map(phase_full),
        _metric_map(phase_reasoning),
        NumericMetric(unclassified[0], "derived", unclassified_caveats),
        NumericMetric(unclassified[1], "derived", unclassified_caveats),
        NumericMetric(unclassified[2], "derived", unclassified_caveats),
        coverage,
        tuple(sorted(diagnostics)),
    )


def _unavailable_trend(*caveats: str) -> TrendResult:
    unavailable = NumericMetric(None, "estimated", tuple(caveats))
    return TrendResult(False, None, unavailable, unavailable, unavailable, tuple(caveats))


def evaluate_trend(current: ComparableTask, history: Iterable[ComparableTask]) -> TrendResult:
    """Warn only on exact token growth plus one exact independent signal."""
    if not current.completed:
        return _unavailable_trend("current_task_incomplete")
    comparable = [item for item in history if item.completed and item.task_family == current.task_family]
    if len(comparable) < 4:
        return _unavailable_trend("insufficient_baseline")
    baseline = comparable[-4:]
    cohort = [*baseline, current]
    if not all(item.metrics_complete for item in cohort):
        return _unavailable_trend("incomplete_metrics")
    if any(item.working_tokens is None or item.working_tokens_provenance != "exact" for item in cohort):
        return _unavailable_trend("incomparable_working_tokens")

    baseline_tokens = median(item.working_tokens for item in baseline if item.working_tokens is not None)
    token_growth = current.working_tokens - baseline_tokens  # type: ignore[operator]
    baseline_metric = NumericMetric(baseline_tokens, "derived", ("median_of_four_prior_tasks",))
    growth_metric = NumericMetric(token_growth, "derived", ("current_minus_baseline_median",))
    caveats: set[str] = set()
    signal_name: str | None = None
    signal_growth: int | float | None = None

    signals = (
        ("test_reruns", "test_reruns_provenance"),
        ("read_amplification", "read_amplification_provenance"),
        ("review_fix_cycles", "review_fix_cycles_provenance"),
        ("compactions", "compactions_provenance"),
    )
    for field, provenance_field in signals:
        if field == "compactions" and not all(item.compaction_normalized for item in cohort):
            caveats.add("compaction_normalization_unavailable")
            continue
        values = [getattr(item, field) for item in cohort]
        provenances = [getattr(item, provenance_field) for item in cohort]
        if any(value is None for value in values) or any(value != "exact" for value in provenances):
            caveats.add(f"incomparable_{field}")
            continue
        signal_baseline = median(value for value in values[:-1] if value is not None)
        growth = values[-1] - signal_baseline  # type: ignore[operator]
        if growth > 0:
            signal_name = field
            signal_growth = growth
            break

    warning = token_growth > 0 and signal_name is not None
    if token_growth <= 0:
        caveats.add("no_token_growth")
    if signal_name is None:
        caveats.add("no_exact_corroborating_signal")
    signal_metric = NumericMetric(
        signal_growth,
        "derived" if signal_growth is not None else "estimated",
        (f"current_minus_{signal_name}_median",) if signal_name is not None else ("signal_growth_unavailable",),
    )
    return TrendResult(warning, signal_name, baseline_metric, growth_metric, signal_metric, tuple(sorted(caveats)))
