"""Safe value objects for trusted semantic annotation writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re

from .rollout_identity import Pseudonymizer


class CapabilityRejected(RuntimeError):
    """Raised when a capability cannot authorize an annotation."""


class CapabilityExpired(CapabilityRejected):
    """Raised when an otherwise valid capability has expired."""


class AnnotationConflict(RuntimeError):
    """Raised after a conflicting trusted annotation request is diagnosed."""


class AnnotationDisposition(str, Enum):
    INSERTED = "inserted"
    RETRIED = "retried"


class StopState(str, Enum):
    FINISHED = "finished"
    RETRY_REQUIRED = "retry_required"
    SELF_REPORT_MISSING = "self_report_missing"


@dataclass(frozen=True)
class TrustedTurnContext:
    project_id: str
    session_id: str
    turn_id: str
    observed_at: str

    def __post_init__(self) -> None:
        trusted_text(self.project_id, "project_id")
        trusted_text(self.session_id, "session_id")
        trusted_text(self.turn_id, "turn_id")
        timestamp(self.observed_at)


@dataclass(frozen=True)
class TrustedAnnotationContext:
    request_key: str
    sequence: int
    observed_at: str

    def __post_init__(self) -> None:
        trusted_text(self.request_key, "request_key")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        timestamp(self.observed_at)


@dataclass(frozen=True)
class IssuedCapability:
    token: str
    expires_at: str


@dataclass(frozen=True)
class AnnotationWrite:
    annotation_id: str
    sequence: int
    disposition: AnnotationDisposition


@dataclass(frozen=True)
class ConflictDecision:
    message: str


CAPABILITY_PATTERN = re.compile(r"^hcap_v1_[A-Za-z0-9_-]{43}$")


def trusted_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def timestamp(value: str) -> datetime:
    trusted_text(value, "observed_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("trusted timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError("trusted timestamp must include an offset")
    return parsed


def capability_digest(keys: Pseudonymizer, capability: str) -> str:
    if not isinstance(capability, str) or CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise CapabilityRejected("capability is unavailable")
    return "hcapd_v1_" + keys.digest("capability", capability)
