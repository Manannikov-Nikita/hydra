"""Immutable public contracts for canonical Hydra audit evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re

from .reporting import NumericFact
from .task_tree_types import validate_provenance


AUDIT_SCHEMA = "hydra.audit/v1"
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,255}\Z")
_EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{16}\Z")
_UNITS = frozenset({
    "bytes", "count", "milliseconds", "percent", "ratio", "tokens",
})


def _finite(value: object, field: str, *, allow_none: bool) -> int | float | None:
    if value is None and allow_none:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{field} must be a finite number or null")
    return value


def _safe_codes(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(
        not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None
        for item in values
    ):
        raise ValueError(f"{field} must contain privacy-safe codes")
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must not contain duplicates")
    return values


@dataclass(frozen=True)
class AuditFact:
    """One numeric fact shape used by audit-only sources such as file sizes."""

    value: int | float | None
    unit: str
    provenance: str
    caveats: tuple[str, ...] = ()
    lower_bound: int | float | None = None

    def __post_init__(self) -> None:
        value = _finite(self.value, "audit fact value", allow_none=True)
        lower = _finite(self.lower_bound, "audit fact lower bound", allow_none=True)
        if self.unit not in _UNITS:
            raise ValueError("audit fact unit is unsupported")
        validate_provenance(self.provenance)
        _safe_codes(self.caveats, "audit fact caveats")
        if value is None and self.provenance != "estimated":
            raise ValueError("unavailable audit facts must be estimated")
        if lower is not None and lower < 0:
            raise ValueError("audit fact lower bound must be non-negative")
        if value is not None and value >= 0 and lower is not None and lower > value:
            raise ValueError("audit fact lower bound cannot exceed value")


@dataclass(frozen=True)
class AuditEvidence:
    """A complete public fact record, present only in the evidence appendix."""

    evidence_id: str
    fact: str
    value: int | float | None
    unit: str
    provenance: str
    caveats: tuple[str, ...] = ()
    lower_bound: int | float | None = None

    def __post_init__(self) -> None:
        if _EVIDENCE_ID.fullmatch(self.evidence_id) is None:
            raise ValueError("invalid audit evidence ID")
        if _SAFE_CODE.fullmatch(self.fact) is None:
            raise ValueError("audit fact path must be a privacy-safe code")
        AuditFact(
            self.value,
            self.unit,
            self.provenance,
            self.caveats,
            self.lower_bound,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "fact": self.fact,
            "value": self.value,
            "unit": self.unit,
            "provenance": self.provenance,
            "caveats": list(self.caveats),
            "lower_bound": self.lower_bound,
        }


class AuditEvidenceRegistry:
    """Build a deterministic exact-once appendix keyed by stable fact paths."""

    def __init__(self) -> None:
        self._by_fact: dict[str, AuditEvidence] = {}
        self._fact_by_id: dict[str, str] = {}

    @staticmethod
    def _evidence_id(fact: str) -> str:
        digest = hashlib.sha256(
            ("hydra.audit/v1/evidence/" + fact).encode("utf-8"),
        ).hexdigest()
        return "ev_" + digest[:16]

    def register(self, fact: str, value: NumericFact | AuditFact) -> str:
        if _SAFE_CODE.fullmatch(fact) is None:
            raise ValueError("audit fact path must be a privacy-safe code")
        if fact in self._by_fact:
            raise ValueError("audit fact is already registered")
        if not isinstance(value, (NumericFact, AuditFact)):
            raise ValueError("audit evidence value must be a public numeric fact")
        evidence_id = self._evidence_id(fact)
        collision = self._fact_by_id.get(evidence_id)
        if collision is not None and collision != fact:
            raise ValueError("audit evidence ID collision")
        record = AuditEvidence(
            evidence_id,
            fact,
            value.value,
            value.unit,
            value.provenance,
            tuple(value.caveats),
            value.lower_bound,
        )
        self._by_fact[fact] = record
        self._fact_by_id[evidence_id] = fact
        return evidence_id

    @property
    def evidence(self) -> tuple[AuditEvidence, ...]:
        return tuple(self._by_fact[name] for name in sorted(self._by_fact))


def _canonical_object(value: object, field: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True)
class AuditReport:
    """Canonical immutable audit presentation assembled from public models."""

    schema_version: str
    pilot_snapshot_json: str
    cohort_json: str
    collection_json: str
    storage_health_json: str
    evidence_appendix: tuple[AuditEvidence, ...]

    def __post_init__(self) -> None:
        if self.schema_version != AUDIT_SCHEMA:
            raise ValueError("unsupported audit schema")
        for value, field in (
            (self.pilot_snapshot_json, "pilot snapshot"),
            (self.cohort_json, "cohort"),
            (self.collection_json, "collection"),
            (self.storage_health_json, "storage health"),
        ):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{field} is not canonical JSON") from error
            if _canonical_object(decoded, field) != value:
                raise ValueError(f"{field} is not canonical JSON")
        if (
            not isinstance(self.evidence_appendix, tuple)
            or any(not isinstance(item, AuditEvidence) for item in self.evidence_appendix)
        ):
            raise ValueError("audit evidence appendix must be an immutable tuple")
        facts = tuple(item.fact for item in self.evidence_appendix)
        evidence_ids = tuple(item.evidence_id for item in self.evidence_appendix)
        if facts != tuple(sorted(facts)) or len(set(facts)) != len(facts):
            raise ValueError("audit evidence facts must be unique and sorted")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("audit evidence IDs must be unique")

    @classmethod
    def create(
        cls,
        *,
        pilot_snapshot: dict[str, object],
        cohort: dict[str, object],
        collection: dict[str, object],
        storage_health: dict[str, object],
        evidence_appendix: tuple[AuditEvidence, ...],
    ) -> "AuditReport":
        return cls(
            AUDIT_SCHEMA,
            _canonical_object(pilot_snapshot, "pilot snapshot"),
            _canonical_object(cohort, "cohort"),
            _canonical_object(collection, "collection"),
            _canonical_object(storage_health, "storage health"),
            evidence_appendix,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "pilot_snapshot": json.loads(self.pilot_snapshot_json),
            "cohort": json.loads(self.cohort_json),
            "collection": json.loads(self.collection_json),
            "storage_health": json.loads(self.storage_health_json),
            "evidence_appendix": [item.as_dict() for item in self.evidence_appendix],
        }
