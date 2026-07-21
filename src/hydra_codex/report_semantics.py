"""Versioned public semantic token breakdown for reconciled task reports."""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from .contracts import AnnotationCause, AnnotationKind, Outcome, ScopeChange
from .redaction import redact_note

if TYPE_CHECKING:
    from .reporting import NumericFact


SEMANTIC_PHASES = (
    "understand",
    "research",
    "design",
    "implement",
    "test_targeted",
    "test_full",
    "review",
    "fix",
    "docs",
    "browser_qa",
    "release",
    "wait_external",
)
SEMANTIC_KINDS = tuple(item.value for item in AnnotationKind)
SEMANTIC_CAUSES = tuple(item.value for item in AnnotationCause)
SCOPE_CHANGES = tuple(item.value for item in ScopeChange)
FINISH_OUTCOMES = tuple(item.value for item in Outcome)
DETERMINISTIC_TEST_CAUSES = ("test_failure", "infra_failure")
TEST_SCOPES = ("targeted", "full", "unknown")
TEST_FAILURE_CAUSES = ("none", "product_failure", "infra_failure", "unknown")
TEST_RETRY_KINDS = (
    "none", "flaky_retry", "product_fix_verification", "infra_recovery",
    "unknown_recovery",
)
TEST_SEMANTIC_PHASES = SEMANTIC_PHASES + ("unclassified",)
TEST_SEMANTIC_CAUSES = SEMANTIC_CAUSES + ("unclassified",)
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True)
class SemanticMarkerSummary:
    kind: str
    phase: str
    cause: str
    scope_change: str
    outcome: str | None
    confidence: float
    note: str
    provenance: str = "model_reported"

    def __post_init__(self) -> None:
        if self.kind not in SEMANTIC_KINDS or self.phase not in SEMANTIC_PHASES:
            raise ValueError("invalid public semantic marker")
        if self.cause not in SEMANTIC_CAUSES or self.scope_change not in SCOPE_CHANGES:
            raise ValueError("invalid public semantic marker")
        if self.outcome is not None and self.outcome not in FINISH_OUTCOMES:
            raise ValueError("invalid public finish outcome")
        if self.provenance != "model_reported":
            raise ValueError("public model marker provenance must be model_reported")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("invalid marker confidence")
        if not isinstance(self.note, str) or len(self.note) > 240 or redact_note(self.note) != self.note:
            raise ValueError("marker note must already be privacy-safe")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "phase": self.phase,
            "cause": self.cause,
            "scope_change": self.scope_change,
            "outcome": self.outcome,
            "confidence": self.confidence,
            "note": self.note,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class TestEvidenceRow:
    scope: str
    failure_cause: str
    retry_kind: str
    phase: str
    cause: str
    count: NumericFact

    def __post_init__(self) -> None:
        from .reporting import _expect_fact

        if (
            self.scope not in TEST_SCOPES
            or self.failure_cause not in TEST_FAILURE_CAUSES
            or self.retry_kind not in TEST_RETRY_KINDS
            or self.phase not in TEST_SEMANTIC_PHASES
            or self.cause not in TEST_SEMANTIC_CAUSES
        ):
            raise ValueError("invalid deterministic test evidence row")
        _expect_fact(self.count, "test evidence count", "count", integer=True)

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return self.scope, self.failure_cause, self.retry_kind, self.phase, self.cause

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "failure_cause": self.failure_cause,
            "retry_kind": self.retry_kind,
            "phase": self.phase,
            "cause": self.cause,
            "count": self.count.as_dict(),
        }


@dataclass(frozen=True)
class TestEvidenceSummary:
    total_count: NumericFact
    rows: tuple[TestEvidenceRow, ...]
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from .reporting import _expect_fact

        _expect_fact(self.total_count, "test evidence total", "count", integer=True)
        if not isinstance(self.rows, tuple) or any(
            not isinstance(item, TestEvidenceRow) for item in self.rows
        ):
            raise ValueError("test evidence rows must be a tuple")
        keys = tuple(item.key for item in self.rows)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("test evidence rows must be unique and sorted")
        values = [item.count.value for item in self.rows]
        if self.total_count.value is not None and all(value is not None for value in values):
            if self.total_count.value != sum(value for value in values if value is not None):
                raise ValueError("test evidence total does not match rows")
        if any(not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None for item in self.caveats):
            raise ValueError("test evidence caveats must be privacy-safe")

    @classmethod
    def unavailable(cls) -> "TestEvidenceSummary":
        from .reporting import _unavailable

        return cls(
            _unavailable("count", "test_evidence_unavailable"), (),
            ("test_evidence_unavailable",),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "total_count": self.total_count.as_dict(),
            "rows": [item.as_dict() for item in self.rows],
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True)
class SemanticAnnotationSummary:
    total_count: NumericFact
    kind_counts: Mapping[str, NumericFact]
    cause_counts: Mapping[str, NumericFact]
    scope_change_counts: Mapping[str, NumericFact]
    finish_outcome_counts: Mapping[str, NumericFact]
    deterministic_test_causes: Mapping[str, NumericFact]
    test_evidence: TestEvidenceSummary
    timeline: tuple[SemanticMarkerSummary, ...]
    truncated_count: NumericFact
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from .reporting import NumericFact, _expect_fact

        groups = (
            ("kind_counts", self.kind_counts, SEMANTIC_KINDS),
            ("cause_counts", self.cause_counts, SEMANTIC_CAUSES),
            ("scope_change_counts", self.scope_change_counts, SCOPE_CHANGES),
            ("finish_outcome_counts", self.finish_outcome_counts, FINISH_OUTCOMES),
            (
                "deterministic_test_causes",
                self.deterministic_test_causes,
                DETERMINISTIC_TEST_CAUSES,
            ),
        )
        for field, supplied, expected in groups:
            if set(supplied) != set(expected):
                raise ValueError("semantic annotation count vocabulary mismatch")
            for name, fact in supplied.items():
                _expect_fact(fact, name, "count", integer=True)
            object.__setattr__(
                self, field,
                MappingProxyType({name: supplied[name] for name in expected}),
            )
        _expect_fact(self.total_count, "total_count", "count", integer=True)
        _expect_fact(self.truncated_count, "truncated_count", "count", integer=True)
        if not isinstance(self.test_evidence, TestEvidenceSummary):
            raise ValueError("test evidence summary is required")
        if len(self.timeline) > 20 or any(not isinstance(item, SemanticMarkerSummary) for item in self.timeline):
            raise ValueError("semantic timeline must contain at most twenty markers")
        if any(not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None for item in self.caveats):
            raise ValueError("semantic annotation caveats must be privacy-safe")

    @classmethod
    def unavailable(cls) -> "SemanticAnnotationSummary":
        from .reporting import _unavailable

        def group(names: tuple[str, ...]) -> dict[str, NumericFact]:
            return {name: _unavailable("count", "semantic_annotations_unavailable") for name in names}

        return cls(
            _unavailable("count", "semantic_annotations_unavailable"),
            group(SEMANTIC_KINDS), group(SEMANTIC_CAUSES), group(SCOPE_CHANGES),
            group(FINISH_OUTCOMES), group(DETERMINISTIC_TEST_CAUSES),
            TestEvidenceSummary.unavailable(), (),
            _unavailable("count", "semantic_annotations_unavailable"),
            ("semantic_annotations_unavailable",),
        )

    def public_facts(self) -> dict[str, NumericFact]:
        facts = {
            "semantic.annotations.total": self.total_count,
            "semantic.annotations.truncated": self.truncated_count,
            "semantic.test_evidence.total": self.test_evidence.total_count,
        }
        for prefix, values in (
            ("kind", self.kind_counts),
            ("cause", self.cause_counts),
            ("scope_change", self.scope_change_counts),
            ("finish_outcome", self.finish_outcome_counts),
            ("deterministic_test_cause", self.deterministic_test_causes),
        ):
            facts.update({f"semantic.annotations.{prefix}.{name}": fact for name, fact in values.items()})
        return facts

    def as_dict(self) -> dict[str, object]:
        return {
            "total_count": self.total_count.as_dict(),
            "kind_counts": {name: value.as_dict() for name, value in self.kind_counts.items()},
            "cause_counts": {name: value.as_dict() for name, value in self.cause_counts.items()},
            "scope_change_counts": {
                name: value.as_dict() for name, value in self.scope_change_counts.items()
            },
            "finish_outcome_counts": {
                name: value.as_dict() for name, value in self.finish_outcome_counts.items()
            },
            "deterministic_test_causes": {
                name: value.as_dict() for name, value in self.deterministic_test_causes.items()
            },
            "test_evidence": self.test_evidence.as_dict(),
            "timeline": [item.as_dict() for item in self.timeline],
            "truncated_count": self.truncated_count.as_dict(),
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True)
class TrendAssessment:
    warning: bool
    corroborating_signal: str | None
    baseline_working_tokens: NumericFact
    token_growth: NumericFact
    signal_growth: NumericFact
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        from .reporting import NumericFact

        if not isinstance(self.warning, bool):
            raise ValueError("trend warning must be boolean")
        if self.corroborating_signal is not None and self.corroborating_signal not in {
            "test_retries", "read_amplification", "review_fix_cycles", "compactions",
        }:
            raise ValueError("invalid trend corroborating signal")
        for fact, unit in (
            (self.baseline_working_tokens, "tokens"),
            (self.token_growth, "tokens"),
            (self.signal_growth, "count"),
        ):
            if not isinstance(fact, NumericFact) or fact.unit != unit:
                raise ValueError("invalid trend metric")
        if any(not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None for item in self.caveats):
            raise ValueError("trend caveats must be privacy-safe")

    @classmethod
    def unavailable(cls, caveat: str = "trend_not_evaluated") -> "TrendAssessment":
        from .reporting import _unavailable

        return cls(
            False, None, _unavailable("tokens", caveat), _unavailable("tokens", caveat),
            _unavailable("count", caveat), (caveat,),
        )

    def public_facts(self) -> dict[str, NumericFact]:
        return {
            "trend.result.baseline_working_tokens": self.baseline_working_tokens,
            "trend.result.token_growth": self.token_growth,
            "trend.result.signal_growth": self.signal_growth,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "warning": self.warning,
            "corroborating_signal": self.corroborating_signal,
            "baseline_working_tokens": self.baseline_working_tokens.as_dict(),
            "token_growth": self.token_growth.as_dict(),
            "signal_growth": self.signal_growth.as_dict(),
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True)
class SemanticTokenFacts:
    working: NumericFact
    full_context: NumericFact
    reasoning: NumericFact

    def __post_init__(self) -> None:
        from .reporting import _expect_fact

        for name in ("working", "full_context", "reasoning"):
            _expect_fact(getattr(self, name), name, "tokens", integer=True)

    @classmethod
    def unavailable(cls) -> "SemanticTokenFacts":
        from .reporting import _unavailable

        return cls(*(
            _unavailable("tokens", "semantic_breakdown_unavailable")
            for _ in range(3)
        ))

    def as_dict(self) -> dict[str, object]:
        return {
            "working": self.working.as_dict(),
            "full_context": self.full_context.as_dict(),
            "reasoning": self.reasoning.as_dict(),
        }


@dataclass(frozen=True)
class SemanticBreakdown:
    phases: Mapping[str, SemanticTokenFacts]
    unclassified: SemanticTokenFacts
    marker_count: NumericFact
    self_report_missing: NumericFact
    annotations: SemanticAnnotationSummary

    def __post_init__(self) -> None:
        from .reporting import NumericFact, _expect_fact

        if set(self.phases) != set(SEMANTIC_PHASES) or any(
            not isinstance(value, SemanticTokenFacts) for value in self.phases.values()
        ):
            raise ValueError("semantic phases must exactly match the public phase vocabulary")
        if not isinstance(self.unclassified, SemanticTokenFacts):
            raise ValueError("semantic unclassified facts are required")
        if not isinstance(self.annotations, SemanticAnnotationSummary):
            raise ValueError("semantic annotations summary is required")
        for name in ("marker_count", "self_report_missing"):
            value = getattr(self, name)
            if not isinstance(value, NumericFact):
                raise ValueError(f"{name} must be a NumericFact")
            _expect_fact(value, name, "count", integer=True)
        object.__setattr__(
            self,
            "phases",
            MappingProxyType({name: self.phases[name] for name in SEMANTIC_PHASES}),
        )

    @classmethod
    def empty(cls) -> "SemanticBreakdown":
        from .reporting import _unavailable

        return cls(
            {phase: SemanticTokenFacts.unavailable() for phase in SEMANTIC_PHASES},
            SemanticTokenFacts.unavailable(),
            _unavailable("count", "semantic_marker_count_unavailable"),
            _unavailable("count", "self_report_missing_unavailable"),
            SemanticAnnotationSummary.unavailable(),
        )

    def public_facts(self) -> dict[str, NumericFact]:
        facts: dict[str, NumericFact] = {}
        for phase in SEMANTIC_PHASES:
            values = self.phases[phase]
            for name in ("working", "full_context", "reasoning"):
                facts[f"semantic.phase.{phase}.{name}"] = getattr(values, name)
        for name in ("working", "full_context", "reasoning"):
            facts[f"semantic.unclassified.{name}"] = getattr(self.unclassified, name)
        facts["semantic.marker_count"] = self.marker_count
        facts["semantic.self_report_missing"] = self.self_report_missing
        facts.update(self.annotations.public_facts())
        return facts

    def as_dict(self) -> dict[str, object]:
        return {
            "phases": {
                phase: self.phases[phase].as_dict()
                for phase in SEMANTIC_PHASES
            },
            "unclassified": self.unclassified.as_dict(),
            "marker_count": self.marker_count.as_dict(),
            "self_report_missing": self.self_report_missing.as_dict(),
        }
