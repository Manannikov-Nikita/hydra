"""Durable dashboard sync jobs over the incremental telemetry queue."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import threading
import uuid
import re

from .exact_time import public_timestamp
from .incremental_sync import IncrementalSyncWorker, ResumableRepair, TrustedSourceRoots
from .reconcile_engine import reconcile_project
from .storage import HydraStore
from .sync_state import SyncJob, SyncStateRepository


_TERMINAL = frozenset({"succeeded", "partial", "failed"})
_SYNC_REF = re.compile(r"sync_[0-9a-f]{32}\Z")


def _summary(job: SyncJob | None) -> dict[str, object]:
    """Serialize a job without leaking its private queue/frontier details."""
    if job is None:
        return {
            "schema_version": "hydra.dashboard-sync/v1", "sync_ref": None,
            "kind": None, "state": "idle", "started_at": None,
            "finished_at": None,
            "progress": {"sources_queued": 0, "sources_processed": 0, "new_bytes": 0},
        }
    if (_SYNC_REF.fullmatch(job.job_id) is None or job.job_kind not in {"sync", "repair", "backfill"}
            or job.state not in {"queued", "running", *_TERMINAL}
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                   for value in (job.sources_discovered, job.sources_completed, job.bytes_processed))
            or job.sources_completed > job.sources_discovered):
        raise ValueError("persisted dashboard sync job is invalid")
    return {
        "schema_version": "hydra.dashboard-sync/v1", "sync_ref": job.job_id,
        "kind": job.job_kind, "state": job.state, "started_at": job.created_at,
        "finished_at": job.completed_at,
        "progress": {
            "sources_queued": job.sources_discovered,
            "sources_processed": job.sources_completed,
            "new_bytes": job.bytes_processed,
        },
    }


class DashboardSyncController:
    """Start/resume persisted sync jobs; never enumerate rollout directories normally."""

    def __init__(
        self, *, store_factory: Callable[[], HydraStore], roots: TrustedSourceRoots | None,
        installation_key: bytes, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        auto_activate: bool = True,
    ) -> None:
        if (
            not callable(store_factory)
            or not callable(clock)
            or not isinstance(auto_activate, bool)
        ):
            raise TypeError("dashboard sync dependencies must be callable")
        if not isinstance(installation_key, bytes) or len(installation_key) < 16:
            raise ValueError("dashboard sync installation key is invalid")
        self._store_factory, self._roots = store_factory, roots
        self._key, self._clock = installation_key, clock
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._activation_lock = threading.RLock()
        self._closed = threading.Event()
        self._watcher: threading.Thread | None = None
        self._validated_store_factory: Callable[[], HydraStore] | None = None
        if auto_activate:
            self.activate()

    def _now(self) -> str:
        return public_timestamp(self._clock())

    def _validated_factory(self) -> Callable[[], HydraStore]:
        factory = self._validated_store_factory
        if factory is not None:
            return factory
        with self._activation_lock:
            factory = self._validated_store_factory
            if factory is not None:
                return factory
            if self._closed.is_set():
                raise RuntimeError("dashboard sync controller is closed")
            validated = self._store_factory()
            try:
                factory = validated.validated_reopener()
            finally:
                validated.close()
            self._validated_store_factory = factory
            return factory

    def activate(self) -> None:
        """Lazily validate storage and start the watcher on first live use.

        Dashboard launch can bind and serve its materialized/unavailable
        bootstrap before any potentially expensive or failing full database
        validation. MCP explicitly activates the same controller at startup.
        """
        self._validated_factory()
        if self._roots is None or self._watcher is not None:
            return
        with self._activation_lock:
            if self._watcher is not None:
                return
            if self._closed.is_set():
                raise RuntimeError("dashboard sync controller is closed")
            self._resume_persisted_job()
            self._watcher = threading.Thread(
                target=self._watch_for_work,
                name="hydra-dashboard-sync-watcher", daemon=True,
            )
            self._watcher.start()

    def _repository(self) -> tuple[HydraStore, SyncStateRepository]:
        store = self._validated_factory()()
        return store, SyncStateRepository(store)

    @staticmethod
    def _latest(repository: SyncStateRepository, kind: str | None = None) -> SyncJob | None:
        if kind is not None:
            active = repository.current_job(kind)
            if active is not None:
                return active
        active = repository.latest_active_job()
        return active if active is not None else repository.latest_job()

    def current(self) -> dict[str, object]:
        self.activate()
        store, repository = self._repository()
        try:
            job = self._latest(repository)
        finally:
            store.close()
        if (
            self._roots is not None
            and job is not None
            and job.state in {"queued", "running"}
        ):
            self._ensure_thread(job.job_kind, job.job_id)
        return _summary(job)

    def get(self, sync_ref: str) -> dict[str, object]:
        self.activate()
        store, repository = self._repository()
        try:
            job = repository.get_job(sync_ref)
            if job is None:
                raise KeyError("unknown sync job")
            return _summary(job)
        finally:
            store.close()

    def changes(self, after: int) -> dict[str, object]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("dashboard revision must be non-negative")
        self.activate()
        store, repository = self._repository()
        try:
            store.connection.execute("BEGIN")
            try:
                revision = repository.data_revision()
                result = {
                    "schema_version": "hydra.dashboard-changes/v1",
                    "data_revision": revision, "changed": revision > after,
                    "sync": _summary(self._latest(repository)),
                }
            finally:
                store.connection.rollback()
            return result
        finally:
            store.close()

    def start_sync(self) -> tuple[dict[str, object], bool]:
        return self._start("sync")

    def start_repair(self) -> tuple[dict[str, object], bool]:
        return self._start("repair")

    def _start(self, kind: str) -> tuple[dict[str, object], bool]:
        if self._roots is None:
            raise RuntimeError("dashboard sync roots are unavailable")
        self.activate()
        now = self._now()
        store, repository = self._repository()
        try:
            job_id, reused = repository.get_or_create_active_job(kind, now)
            job = repository.get_job(job_id)
            assert job is not None
        finally:
            store.close()
        self._ensure_thread(job.job_kind, job_id)
        return _summary(job), reused

    def _resume_persisted_job(self) -> None:
        try:
            store, repository = self._repository()
            try:
                active = repository.latest_active_job()
            finally:
                store.close()
        except Exception:
            # Launch fallback owns storage diagnostics.  Constructor recovery
            # must not turn an unavailable/corrupt store into a process crash.
            return
        if active is not None:
            self._ensure_thread(active.job_kind, active.job_id)

    def _watch_for_work(self) -> None:
        """Wake normal sync for newly queued hook work while the dashboard lives."""
        while not self._closed.wait(0.5):
            try:
                store, repository = self._repository()
                try:
                    active = repository.latest_active_job()
                    if active is None and repository.pending_work(self._now()).total:
                        job_id, _reused = repository.get_or_create_active_job(
                            "sync", self._now(),
                        )
                        active = repository.get_job(job_id)
                finally:
                    store.close()
                if active is not None:
                    self._ensure_thread(active.job_kind, active.job_id)
            except Exception:
                # A transient busy/unavailable store is retried by the bounded
                # watcher interval; dashboard reads retain their own diagnostics.
                continue

    def _ensure_thread(self, kind: str, job_id: str) -> bool:
        """Atomically register one local observer for a durable job."""
        with self._lock:
            local = self._threads.get(job_id)
            if local is not None and local.is_alive():
                return False
            thread = threading.Thread(
                target=self._run, args=(kind, job_id),
                name="hydra-dashboard-sync", daemon=True,
            )
            self._threads[job_id] = thread
            try:
                thread.start()
            except Exception:
                self._threads.pop(job_id, None)
                self._fail(job_id)
                return False
            return True

    def _run(self, kind: str, job_id: str) -> None:
        try:
            if self._closed.is_set():
                return
            if kind == "sync":
                self._run_sync(job_id)
            else:
                self._run_repair(job_id)
        except Exception:
            self._fail(job_id)
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def _run_sync(self, job_id: str) -> None:
        assert self._roots is not None
        store, repository = self._repository()
        try:
            current = repository.get_job(job_id)
            assert current is not None
            if current.state in _TERMINAL:
                return
            queued = repository.queue_count()
            discovered = max(
                current.sources_discovered, current.sources_completed + queued,
            )
            repository.update_job(
                job_id, state="running", sources_discovered=discovered,
                sources_completed=current.sources_completed,
                bytes_processed=current.bytes_processed,
                updated_at=self._now(),
            )
            worker = IncrementalSyncWorker(
                store, self._roots,
                reconcile=lambda project_id: reconcile_project(store, project_id, self._key),
                clock=self._clock,
            )
            while not self._closed.is_set():
                current = repository.get_job(job_id)
                assert current is not None
                if current.state in _TERMINAL:
                    return
                now = self._now()
                pending = repository.pending_work(now)
                if pending.total and not pending.eligible:
                    self._closed.wait(self._eligibility_delay(pending.next_eligible_at))
                    continue
                expiry = public_timestamp(self._clock() + timedelta(seconds=300))
                owner = "dashboard-" + uuid.uuid4().hex
                try:
                    result = worker.sync_once(
                        owner, now, expiry, maximum_sources=1000,
                        release_lease=False, job_id=job_id,
                    )
                except Exception:
                    repository.release_lease(owner, self._now())
                    raise
                if not result.lease_acquired:
                    self._closed.wait(0.5)
                    continue
                finished = False
                try:
                    terminal = repository.finish_job_if_idle(
                        job_id, updated_at=self._now(),
                    )
                    finished = terminal is not None
                finally:
                    repository.release_lease(owner, self._now())
                if finished:
                    return
        finally:
            store.close()

    def _eligibility_delay(self, next_eligible_at: str | None) -> float:
        """Sleep until the next work boundary, polling at most once per second."""
        if next_eligible_at is None:
            return 1.0
        eligible = datetime.fromisoformat(next_eligible_at.replace("Z", "+00:00"))
        delay = (eligible - self._clock()).total_seconds()
        return min(1.0, max(0.05, delay))

    def _run_repair(self, job_id: str) -> None:
        assert self._roots is not None
        store, repository = self._repository()
        try:
            current = repository.get_job(job_id)
            assert current is not None
            repository.update_job(
                job_id, state="running",
                sources_discovered=current.sources_discovered,
                sources_completed=current.sources_completed,
                bytes_processed=current.bytes_processed, updated_at=self._now(),
            )
            repair = ResumableRepair(
                store, self._roots, clock=self._clock,
            )
            if not repository.list_frontier(job_id):
                now = self._now()
                for root_kind in ("sessions", "archived_sessions"):
                    try:
                        self._roots.root_for(root_kind)
                    except Exception:
                        continue
                    repository.save_frontier(
                        job_id=job_id, root_kind=root_kind, directory_locator="@root",
                        state="pending", discovered_count=0, updated_at=now,
                    )
            while not self._closed.is_set():
                now = self._now()
                result = repair.run_batch(job_id, now)
                job = repository.get_job(job_id)
                assert job is not None
                if job.state in _TERMINAL or result.completed:
                    return
                if result.discovered == 0 and result.directories_scanned == 0:
                    self._closed.wait(0.1)
        finally:
            store.close()

    def _fail(self, job_id: str) -> None:
        try:
            store, repository = self._repository()
            try:
                job = repository.get_job(job_id)
                if job is not None and job.state not in _TERMINAL:
                    repository.update_job(
                        job_id, state="failed", sources_discovered=job.sources_discovered,
                        sources_completed=job.sources_completed, bytes_processed=job.bytes_processed,
                        updated_at=self._now(), completed_at=self._now(),
                    )
            finally:
                store.close()
        except Exception:
            return

    def close(self, timeout: float = 5.0) -> None:
        self._closed.set()
        with self._lock:
            threads = tuple(self._threads.values())
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout)
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout)
