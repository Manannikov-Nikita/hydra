"""Privacy-safe public report, comparison, and trend-input contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from types import MappingProxyType
from typing import Mapping

from .public_refs import PublicReferenceProjection, project_public_references
from .redaction import project_task_family
from .report_semantics import SemanticBreakdown, TrendAssessment
from .task_tree_types import ScalarFact, TokenVectorFact, validate_provenance


REPORT_SCHEMA = "hydra.report/v3"
COMPARISON_SCHEMA = "hydra.comparison/v2"
_PUBLIC_REF = re.compile(r"task_[0-9a-f]{1,64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_UNITS = frozenset({"tokens", "milliseconds", "count", "ratio", "percent"})


def _finite(value: object, field: str, *, allow_none: bool) -> int | float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number" + (" or null" if allow_none else ""))
    return value


def _caveats(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(
        not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None for item in values
    ):
        raise ValueError("caveats must be privacy-safe codes")
    return values


def _safe_family(value: str) -> str | None:
    return project_task_family(value)


def _expect_fact(
    value: object, field: str, unit: str, *, integer: bool = False, maximum: float | None = None,
) -> None:
    if not isinstance(value, NumericFact) or value.unit != unit:
        raise ValueError(f"{field} must be a {unit} NumericFact")
    for candidate in (value.value, value.lower_bound):
        if candidate is None:
            continue
        if candidate < 0 or integer and not isinstance(candidate, int):
            raise ValueError(f"{field} must be non-negative" + (" integers" if integer else ""))
        if maximum is not None and candidate > maximum:
            raise ValueError(f"{field} exceeds its maximum")


@dataclass(frozen=True)
class NumericFact:
    """The only numeric representation exposed by the public report schema."""

    value: int | float | None
    unit: str
    provenance: str
    caveats: tuple[str, ...] = ()
    lower_bound: int | float | None = None

    def __post_init__(self) -> None:
        value = _finite(self.value, "value", allow_none=True)
        lower = _finite(self.lower_bound, "lower bound", allow_none=True)
        if self.unit not in _UNITS:
            raise ValueError(f"invalid numeric unit: {self.unit!r}")
        validate_provenance(self.provenance)
        _caveats(self.caveats)
        if value is None and self.provenance != "estimated":
            raise ValueError("unavailable facts must use estimated provenance")
        if lower is not None and lower < 0:
            raise ValueError("lower bound must be non-negative")
        if value is not None and value >= 0 and lower is not None and lower > value:
            raise ValueError("lower bound cannot exceed value")

    def as_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "unit": self.unit,
            "provenance": self.provenance,
            "caveats": list(self.caveats),
            "lower_bound": self.lower_bound,
        }


def _unavailable(unit: str, caveat: str) -> NumericFact:
    return NumericFact(None, unit, "estimated", (caveat,))


def _from_scalar(value: ScalarFact, unit: str) -> NumericFact:
    return NumericFact(
        value.value, unit, value.provenance, value.caveats, value.known_lower_bound,
    )


@dataclass(frozen=True)
class TokenFacts:
    input: NumericFact
    cached_input: NumericFact
    output: NumericFact
    reasoning: NumericFact
    working: NumericFact
    full_context: NumericFact

    def __post_init__(self) -> None:
        for field in ("input", "cached_input", "output", "reasoning", "working", "full_context"):
            _expect_fact(getattr(self, field), field, "tokens", integer=True)

    @classmethod
    def from_tree(cls, value: TokenVectorFact) -> "TokenFacts":
        return cls(*(
            _from_scalar(item, "tokens")
            for item in (
                value.input, value.cached_input, value.output,
                value.reasoning, value.working, value.full,
            )
        ))

    def as_dict(self) -> dict[str, object]:
        return {
            "input": self.input.as_dict(),
            "cached_input": self.cached_input.as_dict(),
            "output": self.output.as_dict(),
            "reasoning": self.reasoning.as_dict(),
            "working": self.working.as_dict(),
            "full_context": self.full_context.as_dict(),
        }


@dataclass(frozen=True)
class PilotHealth:
    task_count: NumericFact
    missing_marker_rate: NumericFact
    semantic_coverage: NumericFact
    self_report_missing: NumericFact
    semantic_conflicts: NumericFact
    instrumentation_calls: NumericFact
    instrumentation_overhead: NumericFact
    schema_diagnostics: NumericFact
    status: str
    receipt_verified: bool
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "task_count", "self_report_missing", "semantic_conflicts",
            "instrumentation_calls", "schema_diagnostics",
        ):
            _expect_fact(getattr(self, field), field, "count", integer=True)
        for field in ("missing_marker_rate", "semantic_coverage"):
            _expect_fact(getattr(self, field), field, "ratio", maximum=1)
        _expect_fact(self.instrumentation_overhead, "instrumentation_overhead", "tokens", integer=True)
        if self.status not in {"not_started", "measuring", "awaiting_receipt", "verified", "unverified"}:
            raise ValueError("invalid pilot status")
        if not isinstance(self.receipt_verified, bool):
            raise ValueError("pilot receipt flag must be boolean")
        _caveats(self.caveats)
        if self.status == "verified" and not self.receipt_verified:
            raise ValueError("verified pilot requires a receipt")

    def as_dict(self) -> dict[str, object]:
        result = {
            field: getattr(self, field).as_dict()
            for field in (
                "task_count", "missing_marker_rate", "semantic_coverage",
                "self_report_missing", "semantic_conflicts", "instrumentation_calls",
                "instrumentation_overhead", "schema_diagnostics",
            )
        }
        result.update({
            "status": self.status,
            "receipt_verified": self.receipt_verified,
            "caveats": list(self.caveats),
        })
        return result


@dataclass(frozen=True)
class TrendInput:
    task_ref: str
    task_family: str | None
    completed: bool
    working_tokens: NumericFact
    test_retries: NumericFact
    read_amplification: NumericFact
    review_fix_cycles: NumericFact
    compactions: NumericFact

    def __post_init__(self) -> None:
        if _PUBLIC_REF.fullmatch(self.task_ref) is None:
            raise ValueError("trend task_ref must be an opaque public reference")
        if not isinstance(self.completed, bool):
            raise ValueError("trend completed must be boolean")
        if self.task_family is not None and _safe_family(self.task_family) != self.task_family:
            raise ValueError("trend task_family is not privacy-safe")
        _expect_fact(self.working_tokens, "working_tokens", "tokens", integer=True)
        for field in ("test_retries", "read_amplification", "review_fix_cycles", "compactions"):
            _expect_fact(getattr(self, field), field, "count", integer=True)

    def as_dict(self) -> dict[str, object]:
        return {
            "task_ref": self.task_ref,
            "task_family": self.task_family,
            "completed": self.completed,
            "working_tokens": self.working_tokens.as_dict(),
            "test_retries": self.test_retries.as_dict(),
            "read_amplification": self.read_amplification.as_dict(),
            "review_fix_cycles": self.review_fix_cycles.as_dict(),
            "compactions": self.compactions.as_dict(),
        }


@dataclass(frozen=True)
class TaskReport:
    schema_version: str
    task_ref: str
    status: str
    last_activity_at: str
    task_family: str | None
    recorded_tokens: TokenFacts
    deduplicated_tokens: TokenFacts
    wall_clock: NumericFact
    agent_time: NumericFact
    sessions: NumericFact
    subagents: NumericFact
    tool_calls: NumericFact
    instrumentation_calls: NumericFact
    file_reads: NumericFact
    file_writes: NumericFact
    test_runs: NumericFact
    targeted_test_runs: NumericFact
    full_test_runs: NumericFact
    test_retries: NumericFact
    semantic_coverage: NumericFact
    semantic_breakdown: SemanticBreakdown
    semantic_conflicts: NumericFact
    schema_diagnostics: NumericFact
    instrumentation_overhead: NumericFact
    pilot_health: PilotHealth
    trend_input: TrendInput
    trend_result: TrendAssessment

    def __post_init__(self) -> None:
        if self.schema_version != REPORT_SCHEMA:
            raise ValueError("unsupported report schema")
        if _PUBLIC_REF.fullmatch(self.task_ref) is None:
            raise ValueError("task_ref must be an opaque public reference")
        if self.status not in {"complete", "incomplete"}:
            raise ValueError("invalid task status")
        try:
            observed = datetime.fromisoformat(self.last_activity_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ValueError("last_activity_at must be an ISO timestamp") from error
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("last_activity_at must be timezone-aware")
        if self.task_family is not None and (
            not isinstance(self.task_family, str) or not self.task_family.strip()
            or len(self.task_family) > 80
        ):
            raise ValueError("task_family must be non-empty text up to 80 characters")
        if self.task_family is not None and _safe_family(self.task_family) != self.task_family:
            raise ValueError("task_family is not privacy-safe")
        if not isinstance(self.recorded_tokens, TokenFacts) or not isinstance(self.deduplicated_tokens, TokenFacts):
            raise ValueError("report token groups must be TokenFacts")
        for field in ("wall_clock", "agent_time"):
            _expect_fact(getattr(self, field), field, "milliseconds", integer=True)
        for field in (
            "sessions", "subagents", "tool_calls", "instrumentation_calls", "file_reads",
            "file_writes", "test_runs", "targeted_test_runs", "full_test_runs", "test_retries",
            "semantic_conflicts", "schema_diagnostics",
        ):
            _expect_fact(getattr(self, field), field, "count", integer=True)
        _expect_fact(self.semantic_coverage, "semantic_coverage", "ratio", maximum=1)
        if not isinstance(self.semantic_breakdown, SemanticBreakdown):
            raise ValueError("semantic_breakdown must be a SemanticBreakdown")
        _expect_fact(self.instrumentation_overhead, "instrumentation_overhead", "tokens", integer=True)
        if (
            not isinstance(self.pilot_health, PilotHealth)
            or not isinstance(self.trend_input, TrendInput)
            or not isinstance(self.trend_result, TrendAssessment)
        ):
            raise ValueError("report pilot and trend contracts are required")
        if (
            self.trend_input.task_ref != self.task_ref
            or self.trend_input.completed != self.completed
            or self.trend_input.task_family not in {None, self.task_family}
        ):
            raise ValueError("trend input must describe this report")

    @property
    def completed(self) -> bool:
        return self.status == "complete"

    def numeric_metrics(self) -> dict[str, NumericFact]:
        metrics = {
            f"recorded_{name}_tokens": getattr(self.recorded_tokens, name)
            for name in ("input", "cached_input", "output", "reasoning", "working", "full_context")
        }
        metrics.update({
            f"deduplicated_{name}_tokens": getattr(self.deduplicated_tokens, name)
            for name in ("input", "cached_input", "output", "reasoning", "working", "full_context")
        })
        metrics.update({
            "wall_clock_ms": self.wall_clock,
            "agent_time_ms": self.agent_time,
            "sessions": self.sessions,
            "subagents": self.subagents,
            "tool_calls": self.tool_calls,
            "instrumentation_calls": self.instrumentation_calls,
            "file_reads": self.file_reads,
            "file_writes": self.file_writes,
            "test_runs": self.test_runs,
            "targeted_test_runs": self.targeted_test_runs,
            "full_test_runs": self.full_test_runs,
            "test_retries": self.test_retries,
            "semantic_coverage": self.semantic_coverage,
            "semantic_conflicts": self.semantic_conflicts,
            "schema_diagnostics": self.schema_diagnostics,
            "instrumentation_overhead_tokens": self.instrumentation_overhead,
        })
        metrics.update(self.semantic_breakdown.public_facts())
        metrics.update(self.trend_result.public_facts())
        return dict(sorted(metrics.items()))

    def public_facts(self) -> dict[str, NumericFact]:
        facts = self.numeric_metrics()
        facts.update({
            f"pilot_health.{name}": getattr(self.pilot_health, name)
            for name in (
                "task_count", "missing_marker_rate", "semantic_coverage",
                "self_report_missing", "semantic_conflicts", "instrumentation_calls",
                "instrumentation_overhead", "schema_diagnostics",
            )
        })
        facts.update({
            f"trend.{name}": getattr(self.trend_input, name)
            for name in (
                "working_tokens", "test_retries", "read_amplification",
                "review_fix_cycles", "compactions",
            )
        })
        return dict(sorted(facts.items()))

    def as_dict(self) -> dict[str, object]:
        counts = {
            name: getattr(self, name).as_dict()
            for name in (
                "sessions", "subagents", "tool_calls", "instrumentation_calls",
                "file_reads", "file_writes", "test_runs", "targeted_test_runs",
                "full_test_runs", "test_retries",
            )
        }
        return {
            "schema_version": self.schema_version,
            "task_ref": self.task_ref,
            "status": self.status,
            "last_activity_at": self.last_activity_at,
            "task_family": self.task_family,
            "recorded_tokens": self.recorded_tokens.as_dict(),
            "deduplicated_tokens": self.deduplicated_tokens.as_dict(),
            "timing": {
                "wall_clock": self.wall_clock.as_dict(),
                "agent_time": self.agent_time.as_dict(),
            },
            "counts": counts,
            "semantic": {
                "coverage": self.semantic_coverage.as_dict(),
                "breakdown": self.semantic_breakdown.as_dict(),
                "annotations": self.semantic_breakdown.annotations.as_dict(),
                "conflicts": self.semantic_conflicts.as_dict(),
                "schema_diagnostics": self.schema_diagnostics.as_dict(),
            },
            "instrumentation_overhead": self.instrumentation_overhead.as_dict(),
            "pilot_health": self.pilot_health.as_dict(),
            "trend": {
                "input": self.trend_input.as_dict(),
                "result": self.trend_result.as_dict(),
            },
        }


@dataclass(frozen=True)
class MetricComparison:
    baseline: NumericFact
    current: NumericFact
    delta: NumericFact
    percent_change: NumericFact

    def __post_init__(self) -> None:
        if not all(isinstance(item, NumericFact) for item in (
            self.baseline, self.current, self.delta, self.percent_change,
        )):
            raise ValueError("comparison values must be NumericFacts")
        if not self.baseline.unit == self.current.unit == self.delta.unit:
            raise ValueError("comparison units differ")
        if self.percent_change.unit != "percent":
            raise ValueError("percent change must use percent units")

    def as_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline.as_dict(),
            "current": self.current.as_dict(),
            "delta": self.delta.as_dict(),
            "percent_change": self.percent_change.as_dict(),
        }


@dataclass(frozen=True)
class ComparisonReport:
    schema_version: str
    baseline_ref: str
    current_ref: str
    verdict: str
    reasons: tuple[str, ...]
    metrics: Mapping[str, MetricComparison]
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != COMPARISON_SCHEMA:
            raise ValueError("unsupported comparison schema")
        if any(_PUBLIC_REF.fullmatch(value) is None for value in (self.baseline_ref, self.current_ref)):
            raise ValueError("comparison references must be opaque")
        if self.verdict not in {"comparable", "partial", "not_comparable", "unknown"}:
            raise ValueError("invalid comparison verdict")
        _caveats(self.reasons)
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("comparison reasons must be unique")
        if self.verdict == "comparable" and self.reasons:
            raise ValueError("comparable verdict cannot have reasons")
        if self.verdict != "comparable" and not self.reasons:
            raise ValueError("non-comparable verdict requires reasons")
        _caveats(self.caveats)
        if any(_SAFE_CODE.fullmatch(name) is None for name in self.metrics):
            raise ValueError("comparison metric names must be privacy-safe codes")
        if any(not isinstance(item, MetricComparison) for item in self.metrics.values()):
            raise ValueError("comparison metrics must be MetricComparison values")
        object.__setattr__(self, "metrics", MappingProxyType(dict(sorted(self.metrics.items()))))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "baseline_ref": self.baseline_ref,
            "current_ref": self.current_ref,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "metrics": {name: fact.as_dict() for name, fact in self.metrics.items()},
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True)
class TrendWindow:
    current: TrendInput
    prior: tuple[TrendInput, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.current, TrendInput):
            raise ValueError("trend current must be a TrendInput")
        if not isinstance(self.prior, tuple) or len(self.prior) > 4 or any(
            not isinstance(item, TrendInput) for item in self.prior
        ):
            raise ValueError("trend prior must contain at most four TrendInput values")
        if len({item.task_ref for item in self.prior}) != len(self.prior):
            raise ValueError("trend prior references must be unique")
        if self.prior and (
            not self.current.completed
            or self.current.task_family is None
            or any(
                not item.completed or item.task_family != self.current.task_family
                for item in self.prior
            )
        ):
            raise ValueError("trend prior must be completed and comparable to current")

    def as_dict(self) -> dict[str, object]:
        return {
            "current": self.current.as_dict(),
            "prior": [item.as_dict() for item in self.prior],
        }


from .report_operations import (
    build_trend_window,
    compare_reports,
    evaluate_report_trends,
    report_from_task_tree,
)
