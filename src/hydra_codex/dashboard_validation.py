"""Shared primitives for strict dashboard-only public DTO validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import math
import re

from .redaction import project_task_family
from .reporting import NumericFact
from .task_tree_types import validate_provenance


FACT_KEYS = frozenset({"value", "unit", "provenance", "caveats", "lower_bound"})
_TASK_REF = re.compile(r"task_[0-9a-f]{1,64}\Z")
_PROJECT_REF = re.compile(r"project_[0-9a-f]{12,64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")


def object_with_keys(
    value: object, keys: set[str] | frozenset[str], label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{label} has an invalid schema")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} requires text keys")
    return value


def array(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    return value


def text(value: object, label: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")


def timestamp(value: object, label: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    text(value, label)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def task_ref(value: object, label: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or _TASK_REF.fullmatch(value) is None:
        raise ValueError(f"{label} must be an opaque task reference")


def project_ref(value: object, label: str) -> None:
    if not isinstance(value, str) or _PROJECT_REF.fullmatch(value) is None:
        raise ValueError(f"{label} must be an opaque project reference")


def task_family(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or project_task_family(value) != value:
        raise ValueError(f"{label} must be privacy-safe")


def safe_codes(value: object, label: str) -> Sequence[object]:
    values = array(value, label)
    if any(
        not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None
        for item in values
    ):
        raise ValueError(f"{label} must contain privacy-safe codes")
    return values


def validate_numeric_fact(
    value: object,
    unit: str | None = None,
    *,
    integer: bool = False,
    nonnegative: bool = False,
    maximum: float | None = None,
    provenance: str | None = None,
) -> None:
    fact = object_with_keys(value, FACT_KEYS, "numeric fact")
    caveats = fact["caveats"]
    safe_codes(caveats, "numeric fact caveats")
    supplied_unit = fact["unit"]
    if not isinstance(supplied_unit, str):
        raise ValueError("numeric fact unit must be text")
    if supplied_unit == "bytes":
        for candidate in (fact["value"], fact["lower_bound"]):
            if candidate is not None and (
                isinstance(candidate, bool)
                or not isinstance(candidate, (int, float))
                or not math.isfinite(candidate)
            ):
                raise ValueError("byte fact values must be finite numbers or null")
        if not isinstance(fact["provenance"], str):
            raise ValueError("byte fact provenance must be text")
        validate_provenance(fact["provenance"])
        if fact["value"] is None and fact["provenance"] != "estimated":
            raise ValueError("unavailable byte facts must use estimated provenance")
        lower = fact["lower_bound"]
        current = fact["value"]
        if lower is not None and lower < 0:
            raise ValueError("byte fact lower bound must be non-negative")
        if current is not None and current >= 0 and lower is not None and lower > current:
            raise ValueError("byte fact lower bound cannot exceed value")
        parsed_unit = "bytes"
    else:
        if not isinstance(fact["provenance"], str):
            raise ValueError("numeric fact provenance must be text")
        parsed = NumericFact(
            fact["value"], supplied_unit, fact["provenance"],
            tuple(caveats), fact["lower_bound"],
        )
        parsed_unit = parsed.unit
    if unit is not None and parsed_unit != unit:
        raise ValueError("numeric fact has an invalid unit")
    if provenance is not None and fact["provenance"] != provenance:
        raise ValueError("numeric fact has invalid provenance")
    for candidate in (fact["value"], fact["lower_bound"]):
        if candidate is None:
            continue
        if integer and not isinstance(candidate, int):
            raise ValueError("numeric fact must contain integers")
        if nonnegative and candidate < 0:
            raise ValueError("numeric fact must be non-negative")
        if maximum is not None and candidate > maximum:
            raise ValueError("numeric fact exceeds its maximum")


def fact_value(value: object) -> int | float | None:
    fact = object_with_keys(value, FACT_KEYS, "numeric fact")
    candidate = fact["value"]
    return candidate if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) else None
