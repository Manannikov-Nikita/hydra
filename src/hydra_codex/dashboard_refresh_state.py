"""Validated public state for one dashboard refresh job."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import re
from types import MappingProxyType
from typing import Literal

from .dashboard_model import DashboardRefreshView, DashboardSnapshot
from .reporting import NumericFact


RefreshState = Literal["queued", "running", "succeeded", "partial", "failed"]
RefreshStage = Literal["discover", "inspect", "scan", "reconcile"]
REFRESH_STAGES = ("discover", "inspect", "scan", "reconcile")
_TERMINAL = frozenset({"succeeded", "partial", "failed"})
_DIAGNOSTICS = frozenset({
    "storage_unavailable", "source_changed", "project_root_unavailable",
    "reconciliation_stale", "database_busy", "event_attribution_unavailable",
    "event_attribution_ambiguous", "internal_failure",
})
PROJECT_REF_PATTERN = re.compile(r"project_[0-9a-f]{12,64}\Z")


def _timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


@dataclass(frozen=True)
class RefreshProgress:
    sources_discovered: int = 0
    sources_inspected: int = 0
    sources_scanned: int = 0
    projects_total: int = 0
    projects_completed: int = 0
    projects_refreshed: int = 0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.values()
        ):
            raise ValueError("refresh progress must contain non-negative integers")
        if self.sources_inspected > self.sources_discovered:
            raise ValueError("inspected sources cannot exceed discovered sources")
        if self.sources_scanned > self.sources_inspected:
            raise ValueError("scanned sources cannot exceed inspected sources")
        if self.projects_completed > self.projects_total:
            raise ValueError("completed projects cannot exceed total projects")
        if self.projects_refreshed > self.projects_completed:
            raise ValueError("refreshed projects cannot exceed completed projects")

    def values(self) -> tuple[int, ...]:
        return (
            self.sources_discovered, self.sources_inspected, self.sources_scanned,
            self.projects_total, self.projects_completed, self.projects_refreshed,
        )

    def facts(self) -> dict[str, NumericFact]:
        names = (
            "sources_discovered", "sources_inspected", "sources_scanned",
            "projects_total", "projects_completed", "projects_refreshed",
        )
        return {
            name: NumericFact(value, "count", "derived")
            for name, value in zip(names, self.values(), strict=True)
        }


@dataclass(frozen=True)
class RefreshSnapshot:
    refresh_ref: str
    state: RefreshState
    stage: RefreshStage | None
    started_at: str
    finished_at: str | None
    progress: RefreshProgress
    diagnostic_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"refresh_[0-9a-f]{12,64}", self.refresh_ref) is None:
            raise ValueError("refresh_ref must be an opaque public reference")
        if self.state not in {"queued", "running", *_TERMINAL}:
            raise ValueError("invalid refresh state")
        if (self.state == "running") != (self.stage is not None):
            raise ValueError("only running refreshes carry a stage")
        if self.stage is not None and self.stage not in REFRESH_STAGES:
            raise ValueError("invalid refresh stage")
        if (self.state in _TERMINAL) != (self.finished_at is not None):
            raise ValueError("only terminal refreshes carry finished_at")
        started = _timestamp(self.started_at, "refresh started_at")
        if self.finished_at is not None and _timestamp(
            self.finished_at, "refresh finished_at",
        ) < started:
            raise ValueError("refresh finished_at cannot precede started_at")
        if not isinstance(self.progress, RefreshProgress):
            raise TypeError("progress must be RefreshProgress")
        codes = tuple(sorted(set(self.diagnostic_codes)))
        if any(code not in _DIAGNOSTICS for code in codes):
            raise ValueError("refresh diagnostics must be categorical")
        object.__setattr__(self, "diagnostic_codes", codes)

    def to_view(self) -> DashboardRefreshView:
        return DashboardRefreshView(
            self.refresh_ref, self.state, self.stage, self.started_at,
            self.finished_at, self.progress.facts(), self.diagnostic_codes,
        )

    def as_dict(self) -> dict[str, object]:
        return self.to_view().as_dict()


@dataclass(frozen=True)
class RefreshResult:
    snapshots: Mapping[str, DashboardSnapshot] = field(repr=False)
    replace_all: bool
    diagnostic_codes: tuple[str, ...]
    projects_total: int
    projects_completed: int
    projects_refreshed: int

    def __post_init__(self) -> None:
        if not isinstance(self.replace_all, bool):
            raise TypeError("replace_all must be boolean")
        if any(code not in _DIAGNOSTICS for code in self.diagnostic_codes):
            raise ValueError("refresh diagnostics must be categorical")
        RefreshProgress(
            projects_total=self.projects_total,
            projects_completed=self.projects_completed,
            projects_refreshed=self.projects_refreshed,
        )
        if self.replace_all and (
            self.diagnostic_codes
            or self.projects_completed != self.projects_total
            or self.projects_refreshed != self.projects_total
        ):
            raise ValueError("full replacement requires a complete successful refresh")
        if any(PROJECT_REF_PATTERN.fullmatch(key) is None for key in self.snapshots):
            raise ValueError("snapshot cache keys must be public project references")
        if any(not isinstance(value, DashboardSnapshot) for value in self.snapshots.values()):
            raise TypeError("refresh snapshots must be DashboardSnapshot values")
        object.__setattr__(self, "snapshots", MappingProxyType(dict(self.snapshots)))
        object.__setattr__(
            self, "diagnostic_codes", tuple(sorted(set(self.diagnostic_codes))),
        )
