"""Immutable, privacy-safe public contracts for the local dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from types import MappingProxyType

from .dashboard_contract import (
    validate_freshness,
    validate_project_payload,
    validate_task_report,
)
from .public_payload import reject_private_fields
from .reporting import NumericFact


DASHBOARD_SCHEMA = "hydra.dashboard/v2"
TASK_LIST_SCHEMA = "hydra.dashboard-task-list/v1"
_PROJECT_REF = re.compile(r"project_[0-9a-f]{12,64}\Z")
_TASK_REF = re.compile(r"task_[0-9a-f]{1,64}\Z")
_REFRESH_REF = re.compile(r"refresh_[0-9a-f]{12,64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_REFRESH_STATES = frozenset({
    "idle", "queued", "running", "succeeded", "partial", "failed",
})
_REFRESH_STAGES = frozenset({"discover", "inspect", "scan", "reconcile"})
_FRESHNESS_STATES = frozenset({"current", "stale", "refreshing", "unavailable"})


def canonical_json(value: object) -> str:
    """Encode one browser-safe object deterministically."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _timestamp(value: str | None, field: str) -> None:
    if value is None:
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _project_ref(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_REF.fullmatch(value) is None:
        raise ValueError("project_ref must be an opaque public reference")
    return value


def _task_ref(value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or _TASK_REF.fullmatch(value) is None:
        raise ValueError("task reference must be an opaque public reference")
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("public mappings require string keys")
        return MappingProxyType({key: _freeze(value[key]) for key in sorted(value)})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw(nested) for nested in value]
    return value


def _canonical_object(value: str, field: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be canonical JSON") from error
    if not isinstance(decoded, dict) or canonical_json(decoded) != value:
        raise ValueError(f"{field} must be a canonical JSON object")
    reject_private_fields(decoded)
    return decoded


@dataclass(frozen=True)
class DashboardRefreshView:
    refresh_ref: str | None
    state: str
    stage: str | None
    started_at: str | None
    finished_at: str | None
    progress: Mapping[str, NumericFact]
    diagnostic_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.refresh_ref is not None and _REFRESH_REF.fullmatch(self.refresh_ref) is None:
            raise ValueError("refresh_ref must be an opaque public reference")
        if self.state not in _REFRESH_STATES:
            raise ValueError("invalid dashboard refresh state")
        if self.stage is not None and self.stage not in _REFRESH_STAGES:
            raise ValueError("invalid dashboard refresh stage")
        if self.state == "idle" and any(
            value is not None
            for value in (self.refresh_ref, self.stage, self.started_at, self.finished_at)
        ):
            raise ValueError("idle refresh cannot carry job state")
        _timestamp(self.started_at, "refresh started_at")
        _timestamp(self.finished_at, "refresh finished_at")
        if not isinstance(self.progress, Mapping) or any(
            not isinstance(key, str) or _SAFE_CODE.fullmatch(key) is None
            or not isinstance(value, NumericFact)
            for key, value in self.progress.items()
        ):
            raise ValueError("refresh progress must contain public numeric facts")
        if not isinstance(self.diagnostic_codes, tuple) or any(
            not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None
            for item in self.diagnostic_codes
        ):
            raise ValueError("refresh diagnostics must contain privacy-safe codes")
        object.__setattr__(self, "progress", MappingProxyType(dict(sorted(self.progress.items()))))
        object.__setattr__(self, "diagnostic_codes", tuple(sorted(set(self.diagnostic_codes))))

    def as_dict(self) -> dict[str, object]:
        payload = {
            "refresh_ref": self.refresh_ref, "state": self.state,
            "stage": self.stage, "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": {key: value.as_dict() for key, value in self.progress.items()},
            "diagnostic_codes": list(self.diagnostic_codes),
        }
        reject_private_fields(payload)
        return payload


@dataclass(frozen=True)
class DashboardProjectSummary:
    project_ref: str
    display_name: str
    last_activity_at: str | None
    freshness_state: str
    task_count: NumericFact

    def __post_init__(self) -> None:
        _project_ref(self.project_ref)
        if not isinstance(self.display_name, str) or not self.display_name.strip() or len(self.display_name) > 120:
            raise ValueError("display_name must be non-empty text up to 120 characters")
        _timestamp(self.last_activity_at, "project last_activity_at")
        if self.freshness_state not in _FRESHNESS_STATES:
            raise ValueError("invalid project freshness state")
        if not isinstance(self.task_count, NumericFact) or self.task_count.unit != "count":
            raise ValueError("task_count must be a count NumericFact")
        if any(
            value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            )
            for value in (self.task_count.value, self.task_count.lower_bound)
        ):
            raise ValueError("task_count must contain non-negative integers")

    def as_dict(self) -> dict[str, object]:
        payload = {
            "project_ref": self.project_ref, "display_name": self.display_name,
            "last_activity_at": self.last_activity_at,
            "freshness_state": self.freshness_state,
            "task_count": self.task_count.as_dict(),
        }
        reject_private_fields(payload)
        return payload


@dataclass(frozen=True)
class DashboardProjectCatalog:
    """One strictly validated project collection shared by bulk snapshots."""

    projects: tuple[DashboardProjectSummary, ...]
    project_refs: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.projects, tuple) or any(
            not isinstance(item, DashboardProjectSummary) for item in self.projects
        ):
            raise ValueError("projects must contain dashboard project summaries")
        project_refs = frozenset(item.project_ref for item in self.projects)
        if len(project_refs) != len(self.projects):
            raise ValueError("project references must be unique")
        object.__setattr__(self, "projects", tuple(sorted(
            self.projects, key=lambda item: item.project_ref,
        )))
        object.__setattr__(self, "project_refs", project_refs)


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: str
    freshness: Mapping[str, object]
    projects: tuple[DashboardProjectSummary, ...] | DashboardProjectCatalog
    selected_project_ref: str | None
    project_json: str | None
    selected_task_json: str | None
    refresh: DashboardRefreshView
    data_revision: int = 0
    sync: Mapping[str, object] = field(default_factory=lambda: {
        "schema_version": "hydra.dashboard-sync/v1", "sync_ref": None,
        "kind": None, "state": "idle", "started_at": None, "finished_at": None,
        "progress": {"sources_queued": 0, "sources_processed": 0, "new_bytes": 0},
    })

    def __post_init__(self) -> None:
        _timestamp(self.generated_at, "dashboard generated_at")
        if not isinstance(self.freshness, Mapping):
            raise ValueError("freshness must be a public mapping")
        frozen_freshness = _freeze(self.freshness)
        thawed_freshness = _thaw(frozen_freshness)
        validate_freshness(thawed_freshness)
        reject_private_fields(thawed_freshness)
        object.__setattr__(self, "freshness", frozen_freshness)
        project_catalog = (
            self.projects
            if isinstance(self.projects, DashboardProjectCatalog)
            else DashboardProjectCatalog(self.projects)
        )
        object.__setattr__(self, "projects", project_catalog.projects)
        if self.selected_project_ref is not None:
            _project_ref(self.selected_project_ref)
            if self.selected_project_ref not in project_catalog.project_refs:
                raise ValueError("selected project is not in the catalog")
        if (self.selected_project_ref is None) != (self.project_json is None):
            raise ValueError("selected project and project payload must appear together")
        if self.project_json is not None:
            project = _canonical_object(self.project_json, "dashboard project")
            validate_project_payload(project)
            if project.get("project_ref") != self.selected_project_ref:
                raise ValueError("project payload does not match selected project")
        if self.selected_task_json is not None:
            if self.selected_project_ref is None:
                raise ValueError("selected task requires a selected project")
            task = _canonical_object(self.selected_task_json, "dashboard selected task")
            validate_task_report(task)
            _task_ref(task.get("task_ref"))
        if not isinstance(self.refresh, DashboardRefreshView):
            raise ValueError("refresh must be a DashboardRefreshView")
        if isinstance(self.data_revision, bool) or not isinstance(self.data_revision, int) or self.data_revision < 0:
            raise ValueError("dashboard revision must be non-negative")
        if not isinstance(self.sync, Mapping):
            raise ValueError("dashboard sync summary must be public mapping")
        frozen_sync = _freeze(self.sync)
        reject_private_fields(_thaw(frozen_sync))
        object.__setattr__(self, "sync", frozen_sync)

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": DASHBOARD_SCHEMA, "generated_at": self.generated_at,
            "freshness": _thaw(self.freshness),
            "projects": [item.as_dict() for item in self.projects],
            "selected_project_ref": self.selected_project_ref,
            "project": None if self.project_json is None else json.loads(self.project_json),
            "selected_task": None if self.selected_task_json is None else json.loads(self.selected_task_json),
            "refresh": self.refresh.as_dict(),
            "data_revision": self.data_revision, "sync": _thaw(self.sync),
        }
        reject_private_fields(payload)
        return payload


@dataclass(frozen=True)
class DashboardTaskPage:
    generated_at: str
    project_ref: str
    items_json: tuple[str, ...]
    limit: int
    next_cursor: str | None

    def __post_init__(self) -> None:
        _timestamp(self.generated_at, "task page generated_at")
        _project_ref(self.project_ref)
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        _task_ref(self.next_cursor, allow_none=True)
        if not isinstance(self.items_json, tuple):
            raise ValueError("task page items must be a tuple")
        if len(self.items_json) > self.limit:
            raise ValueError("task page contains more items than its limit")
        seen: set[str] = set()
        ordered_refs: list[str] = []
        for item in self.items_json:
            payload = _canonical_object(item, "dashboard task")
            validate_task_report(payload)
            task_ref = _task_ref(payload.get("task_ref"))
            assert task_ref is not None
            if task_ref in seen:
                raise ValueError("dashboard task references must be unique")
            seen.add(task_ref)
            ordered_refs.append(task_ref)
        if self.next_cursor is not None and (
            not ordered_refs or self.next_cursor != ordered_refs[-1]
        ):
            raise ValueError("next_cursor must reference the final page item")

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": TASK_LIST_SCHEMA, "generated_at": self.generated_at,
            "project_ref": self.project_ref,
            "items": [json.loads(item) for item in self.items_json],
            "page": {"limit": self.limit, "next_cursor": self.next_cursor,
                     "has_more": self.next_cursor is not None},
        }
        reject_private_fields(payload)
        return payload
