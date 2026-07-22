"""Trusted global refresh planning and immutable dashboard snapshot publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import re
import secrets
import sqlite3
import threading
from types import MappingProxyType
from typing import Literal, Protocol

from .dashboard_model import DashboardRefreshView, DashboardSnapshot
from .dashboard_queries import (
    DashboardQueryService,
    observe_resolved_project,
    sync_project_catalog,
)
from .dashboard_refresh_plan import (
    AttributedRollout,
    CachedRollout,
    GlobalRolloutPlan,
    ProjectPartition,
    WorktreePartition,
    plan_global_rollout_ingest,
    refresh_cached_location,
    trusted_rollout_roots,
)
from .exact_time import public_timestamp
from .reconcile_engine import ReconciliationStale, reconcile_project
from .reporting import NumericFact
from .rollout import ingest_rollouts
from .rollout_identity import RolloutRoot
from .rollout_sources import SourceChanged
from .storage import HydraStore, StorageUnavailable


RefreshState = Literal["queued", "running", "succeeded", "partial", "failed"]
RefreshStage = Literal["discover", "inspect", "scan", "reconcile"]
_STAGES = ("discover", "inspect", "scan", "reconcile")
_TERMINAL = frozenset({"succeeded", "partial", "failed"})
_DIAGNOSTICS = frozenset({
    "storage_unavailable", "source_changed", "project_root_unavailable",
    "reconciliation_stale", "database_busy", "event_attribution_unavailable",
    "event_attribution_ambiguous", "internal_failure",
})
_PROJECT_REF = re.compile(r"project_[0-9a-f]{12,64}\Z")


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
        if self.stage is not None and self.stage not in _STAGES:
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
        if any(_PROJECT_REF.fullmatch(key) is None for key in self.snapshots):
            raise ValueError("snapshot cache keys must be public project references")
        if any(
            not isinstance(value, DashboardSnapshot)
            for value in self.snapshots.values()
        ):
            raise TypeError("refresh snapshots must be DashboardSnapshot values")
        object.__setattr__(self, "snapshots", MappingProxyType(dict(self.snapshots)))
        object.__setattr__(self, "diagnostic_codes", tuple(sorted(set(self.diagnostic_codes))))


class DashboardSnapshotCache:
    """One immutable public-ref map and its shared refresh synchronization lock."""

    def __init__(self, snapshots: Mapping[str, DashboardSnapshot] | None = None) -> None:
        self._lock = threading.Lock()
        self._snapshots = self._freeze({} if snapshots is None else snapshots)

    @staticmethod
    def _freeze(snapshots: Mapping[str, DashboardSnapshot]) -> Mapping[str, DashboardSnapshot]:
        if any(_PROJECT_REF.fullmatch(key) is None for key in snapshots):
            raise ValueError("snapshot cache keys must be public project references")
        if any(not isinstance(value, DashboardSnapshot) for value in snapshots.values()):
            raise TypeError("snapshot cache values must be DashboardSnapshot")
        return MappingProxyType(dict(sorted(snapshots.items())))

    def get(self, project_ref: str) -> DashboardSnapshot | None:
        with self._lock:
            return self._snapshots.get(project_ref)

    def refs(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._snapshots)

    def _publish_locked(
        self, snapshots: Mapping[str, DashboardSnapshot], replace_all: bool,
    ) -> None:
        if replace_all:
            self._snapshots = self._freeze(snapshots)
            return
        merged = dict(self._snapshots)
        merged.update(
            (key, value) for key, value in snapshots.items() if key in merged
        )
        self._snapshots = self._freeze(merged)

    def __repr__(self) -> str:
        return f"DashboardSnapshotCache(snapshot_count={len(self.refs())})"


class RefreshRunner(Protocol):
    def run(
        self, progress: Callable[[RefreshStage, RefreshProgress], None],
    ) -> RefreshResult: ...


class RefreshController:
    def __init__(
        self,
        cache: DashboardSnapshotCache,
        runner: RefreshRunner,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        ref_factory: Callable[[], str] = lambda: "refresh_" + secrets.token_hex(12),
    ) -> None:
        self._cache = cache
        self._runner = runner
        self._clock = clock
        self._ref_factory = ref_factory
        self._current: RefreshSnapshot | None = None
        self._worker: threading.Thread | None = None

    def start(self) -> tuple[RefreshSnapshot, bool]:
        with self._cache._lock:
            if self._current is not None and self._current.state in {"queued", "running"}:
                return self._current, True
            queued = RefreshSnapshot(
                self._ref_factory(), "queued", None,
                public_timestamp(self._clock()), None, RefreshProgress(), (),
            )
            worker = threading.Thread(
                target=self._run, args=(queued,), name="hydra-dashboard-refresh",
                daemon=True,
            )
            self._current = queued
            self._worker = worker
        worker.start()
        return queued, False

    def _report(self, stage: RefreshStage, value: RefreshProgress) -> None:
        with self._cache._lock:
            current = self._current
            if current is None or current.state not in {"queued", "running"}:
                raise RuntimeError("refresh progress has no active job")
            previous_stage = current.stage or "discover"
            if _STAGES.index(stage) < _STAGES.index(previous_stage):
                raise ValueError("refresh stages must be monotonic")
            if any(new < old for new, old in zip(
                value.values(), current.progress.values(), strict=True,
            )):
                raise ValueError("refresh counters must be monotonic")
            self._current = replace(current, state="running", stage=stage, progress=value)

    def _run(self, queued: RefreshSnapshot) -> None:
        try:
            self._report("discover", RefreshProgress())
            result = self._runner.run(self._report)
        except Exception:
            result = RefreshResult({}, False, ("internal_failure",), 0, 0, 0)
        state: RefreshState = (
            "succeeded" if result.replace_all else
            "partial" if result.projects_refreshed else "failed"
        )
        with self._cache._lock:
            current = self._current
            if current is None or current.refresh_ref != queued.refresh_ref:
                return
            terminal_progress = replace(
                current.progress,
                projects_total=max(current.progress.projects_total, result.projects_total),
                projects_completed=max(
                    current.progress.projects_completed, result.projects_completed,
                ),
                projects_refreshed=max(
                    current.progress.projects_refreshed, result.projects_refreshed,
                ),
            )
            terminal = replace(
                current, state=state, stage=None, finished_at=public_timestamp(self._clock()),
                progress=terminal_progress, diagnostic_codes=result.diagnostic_codes,
            )
            published = {
                key: replace(value, refresh=terminal.to_view())
                for key, value in result.snapshots.items()
            }
            self._cache._publish_locked(published, result.replace_all)
            self._current = terminal

    def get(self, refresh_ref: str) -> RefreshSnapshot:
        with self._cache._lock:
            if self._current is None or self._current.refresh_ref != refresh_ref:
                raise KeyError("unknown refresh reference")
            return self._current

    def current(self) -> RefreshSnapshot | None:
        with self._cache._lock:
            return self._current

    def snapshot(self, project_ref: str) -> DashboardSnapshot | None:
        with self._cache._lock:
            base = self._cache._snapshots.get(project_ref)
            current = self._current
            if base is None or current is None:
                return base
            view = current.to_view()
            return base if base.refresh == view else replace(base, refresh=view)

    def close(self, timeout: float = 5.0) -> None:
        with self._cache._lock:
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)

    def __repr__(self) -> str:
        current = self.current()
        return f"RefreshController(state={None if current is None else current.state!r})"


class GlobalRefreshRunner:
    """Serial, project-atomic refresh over trusted prepared rollout sources."""

    def __init__(
        self,
        store_factory: Callable[[], HydraStore],
        installation_key: bytes,
        query_service: DashboardQueryService,
        *,
        roots: Iterable[RolloutRoot] = (),
        event_sources: Iterable[object] = (),
        planner: Callable[..., GlobalRolloutPlan] = plan_global_rollout_ingest,
        ingester: Callable[..., object] = ingest_rollouts,
        reconciler: Callable[..., object] = reconcile_project,
        cached_refresher: Callable[[HydraStore, CachedRollout], None] = refresh_cached_location,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store_factory = store_factory
        self._key = installation_key
        self._query = query_service
        self._roots = tuple(roots)
        self._event_sources = tuple(event_sources)
        self._planner = planner
        self._ingest = ingester
        self._reconcile = reconciler
        self._cached_refresher = cached_refresher
        self._clock = clock

    @staticmethod
    def _code(error: BaseException) -> str:
        if isinstance(error, StorageUnavailable):
            return "storage_unavailable"
        if isinstance(error, SourceChanged):
            return "source_changed"
        if isinstance(error, ReconciliationStale):
            return "reconciliation_stale"
        if isinstance(error, sqlite3.OperationalError):
            code = getattr(error, "sqlite_errorcode", None)
            if isinstance(code, int) and code & 0xFF in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            }:
                return "database_busy"
        return "internal_failure"

    @staticmethod
    def _idle_view() -> DashboardRefreshView:
        return DashboardRefreshView(None, "idle", None, None, None, {}, ())

    def run(
        self, report: Callable[[RefreshStage, RefreshProgress], None],
    ) -> RefreshResult:
        try:
            store = self._store_factory()
        except Exception as error:
            return RefreshResult({}, False, (self._code(error),), 0, 0, 0)
        try:
            observed = RefreshProgress()

            def planned(stage: str, current: int, total: int) -> None:
                nonlocal observed
                if stage == "discover":
                    observed = replace(observed, sources_discovered=total)
                elif stage == "inspect":
                    observed = replace(observed, sources_inspected=current)
                elif stage == "scan":
                    observed = replace(observed, sources_scanned=current)
                report(stage, observed)  # type: ignore[arg-type]

            try:
                plan = self._planner(store, self._roots, self._key, planned)
            except Exception as error:
                return RefreshResult(
                    {}, False, (self._code(error),), 0, 0, 0,
                )
            diagnostics = set(plan.diagnostic_codes)
            if self._event_sources:
                diagnostics.add("event_attribution_unavailable")
            observed = replace(observed, projects_total=len(plan.partitions))
            successful: set[str] = set()
            ordered_partitions = tuple(sorted(
                plan.partitions, key=lambda item: item.project_id,
            ))
            for partition in ordered_partitions:
                try:
                    with store.rollout_transaction():
                        for cached in partition.cached:
                            self._cached_refresher(store, cached)
                        for worktree in sorted(
                            partition.worktrees, key=lambda item: str(item.project_root),
                        ):
                            roots = tuple(
                                RolloutRoot(item.candidate.path, item.candidate.label)
                                for item in worktree.sources
                            )
                            scans = {
                                item.candidate.path: item.scan for item in worktree.sources
                            }
                            self._ingest(
                                store, roots, worktree.project_root,
                                partition.project_id, hash_key=self._key,
                                prepared_scans=scans,
                            )
                        self._reconcile(store, partition.project_id, self._key)
                    successful.add(partition.project_id)
                except Exception as error:
                    diagnostics.add(self._code(error))
                observed = replace(
                    observed,
                    projects_completed=observed.projects_completed + 1,
                    projects_refreshed=len(successful),
                )
                report("reconcile", observed)
            complete = not diagnostics
            try:
                with store.rollout_transaction():
                    if complete:
                        observed_at = public_timestamp(self._clock())
                        for partition in ordered_partitions:
                            for worktree in sorted(
                                partition.worktrees,
                                key=lambda item: str(item.project_root),
                            ):
                                observe_resolved_project(
                                    store, worktree.resolution, observed_at,
                                )
                        sync_project_catalog(store, observed_at)
                    snapshots = self._query._refresh_snapshots_from_store(
                        store,
                        refresh=self._idle_view(),
                        project_ids=None if complete else successful,
                    )
            except Exception as error:
                diagnostics.add(self._code(error))
                snapshots = {}
                complete = False
            return RefreshResult(
                snapshots, complete, tuple(sorted(diagnostics)),
                len(plan.partitions), observed.projects_completed, len(successful),
            )
        finally:
            store.close()

    def __repr__(self) -> str:
        return (
            "GlobalRefreshRunner(rollout_roots="
            f"{len(self._roots)}, event_sources={len(self._event_sources)})"
        )
