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
    ) -> None:
        if not callable(store_factory) or not callable(clock):
            raise TypeError("dashboard sync dependencies must be callable")
        if not isinstance(installation_key, bytes) or len(installation_key) < 16:
            raise ValueError("dashboard sync installation key is invalid")
        self._store_factory, self._roots = store_factory, roots
        self._key, self._clock = installation_key, clock
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        if self._roots is not None:
            self._resume_persisted_job()

    def _now(self) -> str:
        return public_timestamp(self._clock())

    def _repository(self) -> tuple[HydraStore, SyncStateRepository]:
        store = self._store_factory()
        return store, SyncStateRepository(store)

    @staticmethod
    def _latest(repository: SyncStateRepository, kind: str | None = None) -> SyncJob | None:
        if kind is not None:
            active = repository.current_job(kind)
            if active is not None:
                return active
        jobs = repository.list_jobs()
        active = next(
            (job for job in jobs if job.state in {"queued", "running"}),
            None,
        )
        return active if active is not None else jobs[0] if jobs else None

    def current(self) -> dict[str, object]:
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
                active = next(
                    (
                        job for job in repository.list_jobs()
                        if job.state in {"queued", "running"}
                    ),
                    None,
                )
            finally:
                store.close()
        except Exception:
            # Launch fallback owns storage diagnostics.  Constructor recovery
            # must not turn an unavailable/corrupt store into a process crash.
            return
        if active is not None:
            self._ensure_thread(active.job_kind, active.job_id)

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
            queued = repository.queue_count()
            completed = current.sources_completed
            processed = current.bytes_processed
            repairs = max(
                0, current.sources_discovered - current.sources_completed - queued,
            )
            discovered = max(
                current.sources_discovered, completed + repairs + queued,
            )
            repository.update_job(
                job_id, state="running", sources_discovered=discovered,
                sources_completed=completed, bytes_processed=processed,
                updated_at=self._now(),
            )
            worker = IncrementalSyncWorker(
                store, self._roots,
                reconcile=lambda project_id: reconcile_project(store, project_id, self._key),
            )
            while not self._closed.is_set():
                now = self._now()
                expiry = public_timestamp(self._clock() + timedelta(seconds=300))
                result = worker.sync_once("dashboard-" + uuid.uuid4().hex, now, expiry, maximum_sources=1000)
                if not result.lease_acquired:
                    self._closed.wait(0.1)
                    continue
                completed += result.completed
                processed += result.bytes_processed
                repairs += result.repair_required
                remaining = repository.queue_count()
                current = repository.get_job(job_id)
                assert current is not None
                discovered = max(
                    discovered, current.sources_discovered,
                    completed + repairs + remaining,
                )
                if remaining == 0:
                    finished_at = self._now()
                    repository.update_job(job_id, state="partial" if repairs else "succeeded",
                                          sources_discovered=discovered,
                                          sources_completed=completed, bytes_processed=processed,
                                          updated_at=finished_at, completed_at=finished_at)
                    return
                repository.update_job(job_id, state="running",
                                      sources_discovered=discovered,
                                      sources_completed=completed, bytes_processed=processed, updated_at=self._now())
                if result.claimed == 0:
                    self._closed.wait(0.1)
        finally:
            store.close()

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
            repair = ResumableRepair(store, self._roots)
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
