"""Exact RFC3339 instants with nanosecond ordering and datetime presentation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re


NANOSECONDS_PER_SECOND = 1_000_000_000
_RFC3339_NANOSECOND = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt]"
    r"(?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<offset>[Zz]|[+-]\d{2}:\d{2})$"
)


def datetime_nanoseconds(value: datetime) -> int:
    """Return an aware datetime's exact (microsecond-resolution) Unix time."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    observed = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed - epoch
    whole_seconds = delta.days * 86_400 + delta.seconds
    return whole_seconds * NANOSECONDS_PER_SECOND + observed.microsecond * 1_000


def public_timestamp(value: datetime) -> str:
    """Render the established public microsecond-resolution UTC representation."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, order=True)
class ExactInstant:
    """One instant ordered at nanosecond precision with a stable public view."""

    epoch_nanoseconds: int
    presentation: datetime = field(compare=False)
    canonical: str = field(compare=False)
    fractional_digits: int = field(default=6, compare=False, repr=False)

    def __post_init__(self) -> None:
        presented = datetime_nanoseconds(self.presentation)
        if not 0 <= self.epoch_nanoseconds - presented < 1_000:
            raise ValueError("presentation must truncate the exact instant to microseconds")
        if not self.canonical:
            raise ValueError("canonical timestamp must not be empty")
        if (
            isinstance(self.fractional_digits, bool)
            or not isinstance(self.fractional_digits, int)
            or not 0 <= self.fractional_digits <= 9
        ):
            raise ValueError("fractional timestamp precision must be from 0 to 9")


def instant_from_datetime(value: datetime) -> ExactInstant:
    epoch_nanoseconds = datetime_nanoseconds(value)
    presentation = value.astimezone(timezone.utc)
    return ExactInstant(
        epoch_nanoseconds,
        presentation,
        public_timestamp(presentation),
    )


def parse_exact_timestamp(value: object) -> ExactInstant | None:
    """Parse strict RFC3339 with one-to-nine fractional-second digits."""
    if not isinstance(value, str):
        return None
    match = _RFC3339_NANOSECOND.fullmatch(value)
    if match is None:
        return None
    offset = match.group("offset")
    if offset in {"Z", "z"}:
        offset = "+00:00"
    else:
        offset_hour = int(offset[1:3])
        offset_minute = int(offset[4:6])
        if offset_hour > 23 or offset_minute > 59 or offset == "-00:00":
            return None
    try:
        whole = datetime.fromisoformat(
            f'{match.group("date")}T{match.group("clock")}{offset}'
        )
    except ValueError:
        return None
    fraction = match.group("fraction") or ""
    fractional_nanoseconds = int(fraction.ljust(9, "0") or "0")
    epoch_nanoseconds = datetime_nanoseconds(whole) + fractional_nanoseconds
    presentation = whole.astimezone(timezone.utc).replace(
        microsecond=fractional_nanoseconds // 1_000,
    )
    if fractional_nanoseconds % 1_000 == 0:
        canonical = public_timestamp(presentation)
    else:
        clock = presentation.replace(microsecond=0).isoformat().replace(
            "+00:00", ""
        )
        canonical = (
            f"{clock}.{fractional_nanoseconds:09d}".rstrip("0") + "Z"
        )
    return ExactInstant(
        epoch_nanoseconds, presentation, canonical, len(fraction),
    )


def require_exact_timestamp(value: object, field: str = "timestamp") -> ExactInstant:
    parsed = parse_exact_timestamp(value)
    if parsed is None:
        raise ValueError(f"{field} is not a valid RFC3339 timestamp")
    return parsed
