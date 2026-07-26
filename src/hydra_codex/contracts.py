"""Validated, dependency-free contracts for hybrid telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping
import unicodedata

from .redaction import validate_task_family


class AnnotationKind(str, Enum):
    PHASE = "phase"
    BLOCKER = "blocker"
    FINISH = "finish"


class AnnotationPhase(str, Enum):
    UNDERSTAND = "understand"
    RESEARCH = "research"
    DESIGN = "design"
    IMPLEMENT = "implement"
    TEST_TARGETED = "test_targeted"
    TEST_FULL = "test_full"
    REVIEW = "review"
    FIX = "fix"
    DOCS = "docs"
    BROWSER_QA = "browser_qa"
    RELEASE = "release"
    WAIT_EXTERNAL = "wait_external"


class AnnotationCause(str, Enum):
    PROMPT = "prompt"
    PLAN = "plan"
    TEST_FAILURE = "test_failure"
    REVIEW_FINDING = "review_finding"
    USER_CHANGE = "user_change"
    INFRA_FAILURE = "infra_failure"
    FINAL_VERIFICATION = "final_verification"
    OTHER = "other"


class Outcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScopeChange(str, Enum):
    NONE = "none"
    NARROWED = "narrowed"
    EXPANDED = "expanded"
    REDEFINED = "redefined"


class Provenance(str, Enum):
    EXACT = "exact"
    DERIVED = "derived"
    MODEL_REPORTED = "model_reported"
    ESTIMATED = "estimated"


def _enum(value: Any, enum_type: type[Enum], field: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}: {value!r}") from error


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _note(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("note must be text")
    if len(value) > 240:
        raise ValueError("note must not exceed 240 characters")
    return value


def _task_family(value: str) -> str:
    return validate_task_family(value)


_LABEL_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE)
_LABEL_REF = re.compile(r"(?:task|hprj|hcap|hann|hreq|hpay|hpilot)_v?[0-9a-f]{8,}|task_[0-9a-f]{8,}", re.IGNORECASE)
_LABEL_SECRET = re.compile(r"(?:api[_-]?key|secret|password|token|authorization)\s*[:=]", re.IGNORECASE)


def normalize_task_label(value: object) -> str | None:
    """Validate a small, presentation-only model label without retaining identifiers."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("task_label must be text")
    normalized = unicodedata.normalize("NFC", value)
    if not 1 <= len(normalized) <= 80:
        raise ValueError("task_label must contain 1 to 80 characters")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.bidirectional(character) in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
        for character in normalized
    ):
        raise ValueError("task_label contains unsafe characters")
    collapsed = " ".join(normalized.split())
    lowered = collapsed.casefold()
    if (
        not collapsed or "/" in collapsed or "\\" in collapsed or ".." in collapsed
        or "://" in lowered or lowered.startswith(("file:", "http:", "https:"))
        or "@" in collapsed or _LABEL_UUID.search(collapsed) is not None
        or _LABEL_REF.search(collapsed) is not None or _LABEL_SECRET.search(collapsed) is not None
    ):
        raise ValueError("task_label is not privacy-safe")
    return collapsed


def _non_negative_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError("confidence must be a number from 0 to 1")
    return float(value)


def _outcome_for_kind(kind: AnnotationKind, outcome: Outcome | None) -> Outcome | None:
    if kind is AnnotationKind.FINISH and outcome is None:
        raise ValueError("finish annotations require an outcome")
    if kind is not AnnotationKind.FINISH and outcome is not None:
        raise ValueError("only finish annotations may have an outcome")
    return outcome


@dataclass(frozen=True)
class ModelAnnotationInput:
    """Small semantic annotation supplied by a model, without observed metrics."""

    kind: AnnotationKind
    phase: AnnotationPhase
    cause: AnnotationCause
    scope_change: ScopeChange
    task_family: str
    confidence: float
    note: str
    outcome: Outcome | None = None
    task_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum(self.kind, AnnotationKind, "kind"))
        object.__setattr__(self, "phase", _enum(self.phase, AnnotationPhase, "phase"))
        object.__setattr__(self, "cause", _enum(self.cause, AnnotationCause, "cause"))
        object.__setattr__(self, "scope_change", _enum(self.scope_change, ScopeChange, "scope_change"))
        object.__setattr__(self, "task_family", _task_family(self.task_family))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        if self.outcome is not None:
            object.__setattr__(self, "outcome", _enum(self.outcome, Outcome, "outcome"))
        object.__setattr__(self, "note", _note(self.note))
        object.__setattr__(self, "task_label", normalize_task_label(self.task_label))
        _outcome_for_kind(self.kind, self.outcome)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ModelAnnotationInput":
        allowed = {"kind", "phase", "cause", "scope_change", "task_family", "confidence", "note", "outcome", "task_label"}
        forbidden = {
            "tokens", "token_count", "input_tokens", "output_tokens",
            "duration_ms", "elapsed_ms", "timing", "file_count", "files_changed",
            "test_count", "tests", "session_id", "thread_id", "turn_id",
            "timestamp", "observed_at", "started_at", "finished_at",
        }
        fields = set(payload)
        prohibited = fields & forbidden
        if prohibited:
            raise ValueError(f"model annotation contains forbidden fields: {sorted(prohibited)!r}")
        unexpected = fields - allowed
        if unexpected:
            raise ValueError(f"model annotation contains unexpected fields: {sorted(unexpected)!r}")
        missing = allowed - {"outcome", "task_label"} - fields
        if missing:
            raise ValueError(f"model annotation is missing fields: {sorted(missing)!r}")
        return cls(**dict(payload))


@dataclass(frozen=True)
class AnnotationContext:
    """Observed identity and timing supplied by the configured integration path."""

    annotation_id: str
    project_id: str
    session_id: str
    turn_id: str
    sequence: int
    observed_at: str
    provenance: Provenance = Provenance.EXACT

    def __post_init__(self) -> None:
        for field in ("annotation_id", "project_id", "session_id", "turn_id", "observed_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "sequence", _non_negative_integer(self.sequence, "sequence"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))


@dataclass(frozen=True)
class ThreadSessionRecord:
    session_id: str
    project_id: str
    worktree_path: str
    started_at: str
    provenance: Provenance = Provenance.EXACT

    def __post_init__(self) -> None:
        for field in ("session_id", "project_id", "worktree_path", "started_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))


@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    session_id: str
    ordinal: int
    observed_at: str
    provenance: Provenance = Provenance.EXACT

    def __post_init__(self) -> None:
        for field in ("turn_id", "session_id", "observed_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "ordinal", _non_negative_integer(self.ordinal, "ordinal"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))


@dataclass(frozen=True)
class AnnotationRecord:
    annotation_id: str
    project_id: str
    session_id: str
    turn_id: str
    sequence: int
    observed_at: str
    kind: AnnotationKind
    phase: AnnotationPhase
    cause: AnnotationCause
    scope_change: ScopeChange
    task_family: str
    confidence: float
    note: str
    outcome: Outcome | None = None
    provenance: Provenance = Provenance.MODEL_REPORTED
    task_label: str | None = None

    def __post_init__(self) -> None:
        for field in ("annotation_id", "project_id", "session_id", "turn_id", "observed_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "sequence", _non_negative_integer(self.sequence, "sequence"))
        for field, enum_type in (
            ("kind", AnnotationKind), ("phase", AnnotationPhase), ("cause", AnnotationCause),
            ("scope_change", ScopeChange), ("provenance", Provenance),
        ):
            object.__setattr__(self, field, _enum(getattr(self, field), enum_type, field))
        if self.outcome is not None:
            object.__setattr__(self, "outcome", _enum(self.outcome, Outcome, "outcome"))
        object.__setattr__(self, "task_family", _task_family(self.task_family))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "note", _note(self.note))
        object.__setattr__(self, "task_label", normalize_task_label(self.task_label))
        _outcome_for_kind(self.kind, self.outcome)


def materialize_annotation(model: ModelAnnotationInput, context: AnnotationContext) -> AnnotationRecord:
    """Join model semantics with cooperative hook/CLI observation context."""
    if not isinstance(model, ModelAnnotationInput) or not isinstance(context, AnnotationContext):
        raise ValueError("annotation requires validated model input and integration context")
    return AnnotationRecord(
        annotation_id=context.annotation_id,
        project_id=context.project_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        sequence=context.sequence,
        observed_at=context.observed_at,
        kind=model.kind,
        phase=model.phase,
        cause=model.cause,
        scope_change=model.scope_change,
        task_family=model.task_family,
        confidence=model.confidence,
        outcome=model.outcome,
        note=model.note,
    )


@dataclass(frozen=True)
class TokenSampleRecord:
    sample_id: str
    session_id: str
    turn_id: str
    observed_at: str
    input_tokens: int
    output_tokens: int
    provenance: Provenance

    def __post_init__(self) -> None:
        for field in ("sample_id", "session_id", "turn_id", "observed_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "input_tokens", _non_negative_integer(self.input_tokens, "input_tokens"))
        object.__setattr__(self, "output_tokens", _non_negative_integer(self.output_tokens, "output_tokens"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    session_id: str
    turn_id: str
    tool_name: str
    observed_at: str
    outcome: Outcome
    provenance: Provenance

    def __post_init__(self) -> None:
        for field in ("call_id", "session_id", "turn_id", "tool_name", "observed_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "outcome", _enum(self.outcome, Outcome, "outcome"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))


@dataclass(frozen=True)
class TestRunRecord:
    test_run_id: str
    session_id: str
    turn_id: str
    observed_at: str
    outcome: Outcome
    provenance: Provenance

    def __post_init__(self) -> None:
        for field in ("test_run_id", "session_id", "turn_id", "observed_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "outcome", _enum(self.outcome, Outcome, "outcome"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))


@dataclass(frozen=True)
class IngestSourceRecord:
    source_id: str
    source_type: str
    observed_at: str
    digest: str
    provenance: Provenance

    def __post_init__(self) -> None:
        for field in ("source_id", "source_type", "observed_at", "digest"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))


@dataclass(frozen=True)
class ConflictRecord:
    conflict_id: str
    record_id: str
    existing_hash: str
    incoming_hash: str
    observed_at: str

    def __post_init__(self) -> None:
        for field in ("conflict_id", "record_id", "existing_hash", "incoming_hash", "observed_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))


@dataclass(frozen=True)
class ReconciliationRunRecord:
    run_id: str
    project_id: str
    started_at: str
    outcome: Outcome
    provenance: Provenance

    def __post_init__(self) -> None:
        for field in ("run_id", "project_id", "started_at"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "outcome", _enum(self.outcome, Outcome, "outcome"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))
