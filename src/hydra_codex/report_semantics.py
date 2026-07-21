"""Versioned public semantic token breakdown for reconciled task reports."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

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

    def __post_init__(self) -> None:
        from .reporting import NumericFact, _expect_fact

        if set(self.phases) != set(SEMANTIC_PHASES) or any(
            not isinstance(value, SemanticTokenFacts) for value in self.phases.values()
        ):
            raise ValueError("semantic phases must exactly match the public phase vocabulary")
        if not isinstance(self.unclassified, SemanticTokenFacts):
            raise ValueError("semantic unclassified facts are required")
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
