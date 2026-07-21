"""Privacy-safe public values produced by deterministic reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from .reconcile_annotations import AnnotationFacts
from .task_tree_types import Provenance, ScalarFact, TaskTreeMetrics, validate_provenance


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class TaskPlan:
    root_key: str
    status: str
    cutoff_at: datetime
    session_ids: tuple[str, ...]
    cutoff_source_key: str | None = None
    cutoff_source_ordinal: int | None = None
    cutoff_timing_provenance: Provenance = "exact"

    def __post_init__(self) -> None:
        _aware(self.cutoff_at, "cutoff_at")
        validate_provenance(
            self.cutoff_timing_provenance, "cutoff timing provenance",
        )


@dataclass(frozen=True)
class SemanticTaskFacts:
    task_family: str | None
    annotations: AnnotationFacts
    coverage: ScalarFact
    classified_working: int
    unclassified_working: ScalarFact
    unclassified_full_context: ScalarFact
    unclassified_reasoning: ScalarFact
    phase_working: Mapping[str, ScalarFact] = field(default_factory=dict)
    phase_full_context: Mapping[str, ScalarFact] = field(default_factory=dict)
    phase_reasoning: Mapping[str, ScalarFact] = field(default_factory=dict)
    marker_count: int = 0
    self_report_missing: int = 0
    semantic_conflicts: int = 0
    schema_diagnostics: int = 0
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.annotations, AnnotationFacts):
            raise ValueError("annotations must be AnnotationFacts")
        for name in (
            "classified_working", "marker_count", "self_report_missing",
            "semantic_conflicts", "schema_diagnostics",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "unclassified_working", "unclassified_full_context", "unclassified_reasoning",
        ):
            if not isinstance(getattr(self, name), ScalarFact):
                raise ValueError(f"{name} must be a ScalarFact")
        for name in ("phase_working", "phase_full_context", "phase_reasoning"):
            supplied = getattr(self, name)
            if any(
                not isinstance(key, str) or not key or not isinstance(value, ScalarFact)
                for key, value in supplied.items()
            ):
                raise ValueError(f"{name} must contain phase ScalarFact values")
            object.__setattr__(self, name, MappingProxyType(dict(sorted(supplied.items()))))
        if tuple(sorted(set(self.diagnostics))) != self.diagnostics:
            raise ValueError("diagnostics must be sorted and unique")


@dataclass(frozen=True, repr=False)
class ReconciledTask:
    public_ref: str
    status: str
    last_activity_at: datetime
    metrics: TaskTreeMetrics
    semantic: SemanticTaskFacts

    def __post_init__(self) -> None:
        if not self.public_ref.startswith("task_"):
            raise ValueError("public_ref must be opaque")
        if self.status not in {"complete", "incomplete"}:
            raise ValueError("invalid reconciled task status")
        _aware(self.last_activity_at, "last_activity_at")
        if self.last_activity_at != self.metrics.cutoff_at:
            raise ValueError("task activity and metric cutoff must match")

    def __repr__(self) -> str:
        return f"ReconciledTask(public_ref={self.public_ref!r}, status={self.status!r})"


@dataclass(frozen=True)
class ReconciliationSummary:
    run_id: str
    project_id: str
    reconciliation_version: int
    task_count: int
    complete_count: int
    incomplete_count: int

    def __post_init__(self) -> None:
        if not self.run_id.startswith("hrec_v1_"):
            raise ValueError("invalid reconciliation run id")
        for name in ("reconciliation_version", "task_count", "complete_count", "incomplete_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.complete_count + self.incomplete_count != self.task_count:
            raise ValueError("task status counts must equal task_count")
