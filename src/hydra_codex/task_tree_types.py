"""Typed, privacy-safe observations and facts used by task-tree aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Literal


Provenance = Literal["exact", "derived", "model_reported", "estimated"]
EdgeConfidence = Literal["confirmed", "inferred", "ambiguous"]
PROVENANCE_VALUES = frozenset({"exact", "derived", "model_reported", "estimated"})


def validate_provenance(value: object, field: str = "provenance") -> None:
    if value not in PROVENANCE_VALUES:
        raise ValueError(f"invalid {field}: {value!r}")


def validate_nonnegative(value: object, field: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if (
        not isinstance(value, (int, float)) or isinstance(value, bool)
        or not math.isfinite(value) or value < 0
    ):
        suffix = " or null" if allow_none else ""
        raise ValueError(f"{field} must be a non-negative number{suffix}")


def _validate_caveats(caveats: tuple[str, ...]) -> None:
    if not isinstance(caveats, tuple) or any(
        not isinstance(item, str) or not item for item in caveats
    ):
        raise ValueError("metric caveats must be non-empty strings")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _valid_count(value: int | None) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


@dataclass(frozen=True)
class TokenVector:
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None

    def __post_init__(self) -> None:
        values = (
            self.input_tokens, self.cached_input_tokens,
            self.output_tokens, self.reasoning_output_tokens,
        )
        if any(not _valid_count(value) for value in values):
            raise ValueError("token components must be non-negative integers or null")
        if (
            self.input_tokens is not None and self.cached_input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached_input_tokens cannot exceed input_tokens")

    @classmethod
    def zero(cls) -> "TokenVector":
        return cls(0, 0, 0, 0)

    @classmethod
    def unknown(cls) -> "TokenVector":
        return cls(None, None, None, None)

    @property
    def working_tokens(self) -> int | None:
        if None in (self.input_tokens, self.cached_input_tokens, self.output_tokens):
            return None
        return self.input_tokens - self.cached_input_tokens + self.output_tokens

    @property
    def full_context(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenVector") -> "TokenVector":
        def add(left: int | None, right: int | None) -> int | None:
            return None if left is None or right is None else left + right
        return TokenVector(
            add(self.input_tokens, other.input_tokens),
            add(self.cached_input_tokens, other.cached_input_tokens),
            add(self.output_tokens, other.output_tokens),
            add(self.reasoning_output_tokens, other.reasoning_output_tokens),
        )

    def subtract(self, other: "TokenVector") -> "TokenVector":
        def subtract(left: int | None, right: int | None) -> int | None:
            if left is None or right is None:
                return None
            if right > left:
                raise ValueError("replay baseline exceeds recorded cumulative usage")
            return left - right
        return TokenVector(
            subtract(self.input_tokens, other.input_tokens),
            subtract(self.cached_input_tokens, other.cached_input_tokens),
            subtract(self.output_tokens, other.output_tokens),
            subtract(self.reasoning_output_tokens, other.reasoning_output_tokens),
        )

    def merge_known(self, other: "TokenVector") -> "TokenVector":
        return TokenVector(*(
            current if current is not None else previous
            for previous, current in zip(self.values, other.values)
        ))

    @property
    def values(self) -> tuple[int | None, int | None, int | None, int | None]:
        return (
            self.input_tokens, self.cached_input_tokens,
            self.output_tokens, self.reasoning_output_tokens,
        )

    def decreased_from(self, previous: "TokenVector") -> bool:
        return any(
            current is not None and before is not None and current < before
            for current, before in zip(self.values, previous.values)
        )


@dataclass(frozen=True)
class NormalizedSession:
    session_id: str
    parent_id: str | None
    started_at: datetime | None
    edge_confidence_kind: EdgeConfidence = "confirmed"
    edge_confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.started_at is not None:
            _require_aware(self.started_at, "started_at")
        if self.edge_confidence_kind not in ("confirmed", "inferred", "ambiguous"):
            raise ValueError("invalid edge confidence kind")
        if not isinstance(self.edge_confidence, (int, float)) or isinstance(self.edge_confidence, bool):
            raise ValueError("edge confidence must be numeric")
        if not 0 <= float(self.edge_confidence) <= 1:
            raise ValueError("edge confidence must be between zero and one")

    @property
    def replay_eligible(self) -> bool:
        return (
            self.parent_id is not None
            and self.started_at is not None
            and self.edge_confidence_kind == "confirmed"
            and float(self.edge_confidence) == 1.0
        )


@dataclass(frozen=True)
class TokenObservation:
    session_id: str
    observed_at: datetime | None
    sequence: int
    vector: TokenVector
    epoch: int | None = None
    placement_provenance: Provenance = "exact"
    logical_source_key: str | None = None
    source_ordinal: int | None = None

    def __post_init__(self) -> None:
        if self.observed_at is not None:
            _require_aware(self.observed_at, "observed_at")
        validate_provenance(self.placement_provenance, "placement provenance")
        if self.observed_at is None and self.placement_provenance != "estimated":
            raise ValueError("timestamp-missing token placement must be estimated")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if self.epoch is not None and (isinstance(self.epoch, bool) or self.epoch < 0):
            raise ValueError("epoch must be a non-negative integer or null")
        if self.source_ordinal is not None and (
            isinstance(self.source_ordinal, bool)
            or not isinstance(self.source_ordinal, int)
            or self.source_ordinal < 0
        ):
            raise ValueError("source ordinal must be a non-negative integer or null")
        if (self.logical_source_key is None) != (self.source_ordinal is None):
            raise ValueError("token source lineage must provide both key and ordinal")


@dataclass(frozen=True)
class ReplayBaselineObservation:
    session_id: str
    observed_at: datetime
    vector: TokenVector

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class LifecycleObservation:
    session_id: str
    kind: Literal["task_complete", "task_started", "turn_aborted"]
    observed_at: datetime
    logical_source_key: str | None = None
    source_ordinal: int | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.source_ordinal is not None and (
            isinstance(self.source_ordinal, bool)
            or not isinstance(self.source_ordinal, int)
            or self.source_ordinal < 0
        ):
            raise ValueError("source ordinal must be a non-negative integer or null")
        if (self.logical_source_key is None) != (self.source_ordinal is None):
            raise ValueError("lifecycle source lineage must provide both key and ordinal")


@dataclass(frozen=True)
class ActivityObservation:
    session_id: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class ToolObservation:
    session_id: str
    observation_id: str
    category: str
    observed_at: datetime | None

    def __post_init__(self) -> None:
        if not self.session_id or not self.observation_id or not self.category:
            raise ValueError("tool observation fields must not be empty")
        if self.observed_at is not None:
            _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class FileObservation:
    session_id: str
    observation_id: str
    operation: Literal["read", "write"]
    observed_at: datetime | None

    def __post_init__(self) -> None:
        if not self.session_id or not self.observation_id:
            raise ValueError("file observation fields must not be empty")
        if self.operation not in ("read", "write"):
            raise ValueError("invalid file operation")
        if self.observed_at is not None:
            _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class TestRunObservation:
    session_id: str
    observation_id: str
    scope: Literal["targeted", "full", "unknown"]
    retry_kind: str
    observed_at: datetime | None

    def __post_init__(self) -> None:
        if not self.session_id or not self.observation_id or not self.retry_kind:
            raise ValueError("test observation fields must not be empty")
        if self.scope not in ("targeted", "full", "unknown"):
            raise ValueError("invalid test scope")
        if self.observed_at is not None:
            _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class ScalarFact:
    value: int | float | None
    provenance: Provenance
    caveats: tuple[str, ...] = ()
    known_lower_bound: int | float | None = None

    def __post_init__(self) -> None:
        validate_provenance(self.provenance)
        validate_nonnegative(self.value, "metric value", allow_none=True)
        if self.known_lower_bound is None:
            object.__setattr__(self, "known_lower_bound", self.value if self.value is not None else 0)
        validate_nonnegative(self.known_lower_bound, "metric lower bound")
        if self.value is not None and self.known_lower_bound > self.value:
            raise ValueError("metric lower bound cannot exceed its value")
        if self.value is None and self.provenance != "estimated":
            raise ValueError("unavailable metric value must use estimated provenance")
        _validate_caveats(self.caveats)


@dataclass(frozen=True)
class _Bounds:
    input: int
    cached: int
    output: int
    reasoning: int
    working: int
    full: int

    def __post_init__(self) -> None:
        for field, value in zip(
            ("input", "cached", "output", "reasoning", "working", "full"),
            (self.input, self.cached, self.output, self.reasoning, self.working, self.full),
        ):
            validate_nonnegative(value, f"{field} lower bound")


@dataclass(frozen=True)
class _Amount:
    vector: TokenVector
    bounds: _Bounds


@dataclass(frozen=True)
class TokenVectorFact:
    vector: TokenVector
    input: ScalarFact
    cached_input: ScalarFact
    output: ScalarFact
    reasoning: ScalarFact
    working: ScalarFact
    full: ScalarFact
    provenance: Provenance
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_provenance(self.provenance)
        _validate_caveats(self.caveats)
        expected = (
            self.vector.input_tokens, self.vector.cached_input_tokens,
            self.vector.output_tokens, self.vector.reasoning_output_tokens,
            self.vector.working_tokens, self.vector.full_context,
        )
        observed = (
            self.input.value, self.cached_input.value, self.output.value,
            self.reasoning.value, self.working.value, self.full.value,
        )
        if observed != expected:
            raise ValueError("token facts must match their vector")
        if self.provenance != "estimated" and any(item.provenance == "estimated" for item in (
            self.input, self.cached_input, self.output, self.reasoning, self.working, self.full,
        )):
            raise ValueError("token vector provenance cannot exceed component provenance")

    @property
    def working_tokens(self) -> int | None:
        return self.working.value

    @property
    def full_context(self) -> int | None:
        return self.full.value

    @property
    def reasoning_output_tokens(self) -> int | None:
        return self.reasoning.value


@dataclass(frozen=True)
class TaskTreeMetrics:
    root_id: str
    cutoff_at: datetime
    session_ids: tuple[str, ...]
    recorded: TokenVectorFact
    replay_baseline: TokenVectorFact
    unique: TokenVectorFact
    sessions: ScalarFact
    subagents: ScalarFact
    root_wall_clock_ms: ScalarFact
    agent_time_ms: ScalarFact
    semantic_coverage: ScalarFact
    tool_calls: ScalarFact
    instrumentation_calls: ScalarFact
    file_reads: ScalarFact
    file_writes: ScalarFact
    test_runs: ScalarFact
    targeted_test_runs: ScalarFact
    full_test_runs: ScalarFact
    test_retries: ScalarFact
    observed_replay_baselines: int
    zero_no_observation: int
    unconfirmed_replay_edges: int
    cycle_edges: int

    def __post_init__(self) -> None:
        _require_aware(self.cutoff_at, "cutoff_at")
        if not self.root_id or self.root_id not in self.session_ids:
            raise ValueError("task-tree root must be included")
        for field, value in (
            ("observed replay baselines", self.observed_replay_baselines),
            ("zero observations", self.zero_no_observation),
            ("unconfirmed replay edges", self.unconfirmed_replay_edges),
            ("cycle edges", self.cycle_edges),
        ):
            validate_nonnegative(value, field)
