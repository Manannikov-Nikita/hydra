"""Trusted global refresh planning and immutable dashboard snapshot publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
import secrets
import sqlite3
import threading
from types import MappingProxyType
from typing import Protocol

from .codex_event_ingest import ingest_codex_events
from .dashboard_event_refresh import prepare_dashboard_events
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
    revalidate_cached_rollout,
    refresh_cached_location,
    trusted_rollout_roots,
)
from .dashboard_refresh_state import (
    PROJECT_REF_PATTERN,
    REFRESH_STAGES,
    RefreshProgress,
    RefreshResult,
    RefreshSnapshot,
    RefreshStage,
    RefreshState,
)
from .exact_time import public_timestamp
from .prepared_codex_events import (
    PreparedCodexEventSource,
    PreparedEventAttribution,
    attribute_prepared_codex_event_source,
    prepare_codex_event_source,
    revalidate_prepared_event_attribution,
)
from .reconcile_engine import ReconciliationStale, reconcile_project
from .rollout import ingest_rollouts
from .rollout_identity import RolloutRoot
from .rollout_sources import SourceChanged
from .storage import HydraStore, StorageUnavailable


class DashboardSnapshotCache:
    """One immutable public-ref map and its shared refresh synchronization lock."""

    def __init__(self, snapshots: Mapping[str, DashboardSnapshot] | None = None) -> None:
        self._lock = threading.Lock()
        self._snapshots = self._freeze({} if snapshots is None else snapshots)

    @staticmethod
    def _freeze(snapshots: Mapping[str, DashboardSnapshot]) -> Mapping[str, DashboardSnapshot]:
        if any(PROJECT_REF_PATTERN.fullmatch(key) is None for key in snapshots):
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
        self._succeeded_once = False

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
            try:
                worker.start()
            except Exception:
                failed = replace(
                    queued, state="failed", finished_at=queued.started_at,
                    diagnostic_codes=("internal_failure",),
                )
                self._current = failed
                self._worker = None
                return failed, False
            return queued, False

    def _report(self, stage: RefreshStage, value: RefreshProgress) -> None:
        with self._cache._lock:
            current = self._current
            if current is None or current.state not in {"queued", "running"}:
                raise RuntimeError("refresh progress has no active job")
            previous_stage = current.stage or "discover"
            if REFRESH_STAGES.index(stage) < REFRESH_STAGES.index(previous_stage):
                raise ValueError("refresh stages must be monotonic")
            if any(new < old for new, old in zip(
                value.values(), current.progress.values(), strict=True,
            )):
                raise ValueError("refresh counters must be monotonic")
            self._current = replace(current, state="running", stage=stage, progress=value)

    def _run(self, queued: RefreshSnapshot) -> None:
        failed = False
        try:
            self._report("discover", RefreshProgress())
            result = self._runner.run(self._report)
            with self._cache._lock:
                current = self._current
                if current is None or any(
                    new < old for new, old in zip(
                        (
                            result.projects_total, result.projects_completed,
                            result.projects_refreshed,
                        ),
                        current.progress.values()[3:], strict=True,
                    )
                ):
                    raise ValueError("refresh result counters regressed")
        except Exception:
            with self._cache._lock:
                progress = (
                    RefreshProgress() if self._current is None
                    else self._current.progress
                )
            result = RefreshResult(
                {}, False, ("internal_failure",),
                progress.projects_total, progress.projects_completed,
                progress.projects_refreshed,
            )
            failed = True
        state: RefreshState = "failed" if failed else (
            "succeeded" if result.replace_all else
            "partial" if result.projects_refreshed else "failed"
        )
        with self._cache._lock:
            current = self._current
            if current is None or current.refresh_ref != queued.refresh_ref:
                return
            terminal_progress = replace(
                current.progress, projects_total=result.projects_total,
                projects_completed=result.projects_completed,
                projects_refreshed=result.projects_refreshed,
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
            if state == "succeeded":
                self._succeeded_once = True

    def get(self, refresh_ref: str) -> RefreshSnapshot:
        with self._cache._lock:
            if self._current is None or self._current.refresh_ref != refresh_ref:
                raise KeyError("unknown refresh reference")
            return self._current

    def current(self) -> RefreshSnapshot | None:
        with self._cache._lock:
            return self._current

    def succeeded_once(self) -> bool:
        with self._cache._lock:
            return self._succeeded_once

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
            try:
                worker.join(timeout)
            except RuntimeError:
                return

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
        event_ingester: Callable[..., object] = ingest_codex_events,
        event_preparer: Callable[
            ..., PreparedCodexEventSource
        ] = prepare_codex_event_source,
        event_attributor: Callable[
            ..., object
        ] = attribute_prepared_codex_event_source,
        event_revalidator: Callable[
            [PreparedEventAttribution], None
        ] = revalidate_prepared_event_attribution,
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
        self._event_ingest = event_ingester
        self._prepare_event = event_preparer
        self._attribute_event = event_attributor
        self._revalidate_event = event_revalidator
        self._reconcile = reconciler
        self._cached_refresher = cached_refresher
        self._clock = clock

    @staticmethod
    def _code(error: Exception) -> str:
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
            event_count = len(self._event_sources)
            last_stage: RefreshStage = "discover"

            def planned(stage: str, current: int, total: int) -> None:
                nonlocal last_stage, observed
                if stage == "discover":
                    observed = replace(
                        observed, sources_discovered=total + event_count,
                    )
                elif stage == "inspect":
                    observed = replace(observed, sources_inspected=current)
                elif stage == "scan":
                    observed = replace(observed, sources_scanned=current)
                candidate: RefreshStage = stage  # type: ignore[assignment]
                if REFRESH_STAGES.index(candidate) < REFRESH_STAGES.index(last_stage):
                    candidate = last_stage
                report(candidate, observed)
                last_stage = candidate

            try:
                plan = self._planner(store, self._roots, self._key, planned)
            except Exception as error:
                return RefreshResult(
                    {}, False, (self._code(error),), 0, 0, 0,
                )
            diagnostics = set(plan.diagnostic_codes)
            observed = replace(
                observed,
                sources_discovered=max(
                    observed.sources_discovered,
                    getattr(plan, "discovered_count", 0) + event_count,
                ),
                sources_inspected=max(
                    observed.sources_inspected,
                    getattr(plan, "inspected_count", 0),
                ),
                sources_scanned=max(
                    observed.sources_scanned,
                    getattr(plan, "scanned_count", 0),
                ),
            )

            def event_progress(scanned: bool) -> None:
                nonlocal last_stage, observed
                observed = replace(
                    observed,
                    sources_inspected=(
                        observed.sources_inspected + int(not scanned)
                    ),
                    sources_scanned=observed.sources_scanned + int(scanned),
                )
                candidate: RefreshStage = "scan" if scanned else "inspect"
                if REFRESH_STAGES.index(candidate) < REFRESH_STAGES.index(last_stage):
                    candidate = last_stage
                report(candidate, observed)
                last_stage = candidate

            prepared_events = prepare_dashboard_events(
                store.connection,
                self._event_sources,
                plan.partitions,
                self._key,
                progress=event_progress,
                error_code=self._code,
                preparer=self._prepare_event,
                attributor=self._attribute_event,
            )
            diagnostics.update(prepared_events.diagnostic_codes)
            observed = replace(observed, projects_total=len(plan.partitions))
            successful: set[str] = set()
            ordered_partitions = tuple(sorted(
                plan.partitions, key=lambda item: item.project_id,
            ))
            for partition in ordered_partitions:
                report("reconcile", observed)
                try:
                    with store.rollout_transaction():
                        for cached in partition.cached:
                            revalidate_cached_rollout(cached)
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
                        for group in prepared_events.for_project(partition.project_id):
                            for _prepared, attribution in group.attributions:
                                self._revalidate_event(attribution)
                            self._event_ingest(
                                store, (), group.project_root,
                                partition.project_id, hash_key=self._key,
                                prepared_sources=group.prepared_sources,
                            )
                            for _prepared, attribution in group.attributions:
                                self._revalidate_event(attribution)
                        self._reconcile(store, partition.project_id, self._key)
                        for cached in partition.cached:
                            revalidate_cached_rollout(cached)
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
