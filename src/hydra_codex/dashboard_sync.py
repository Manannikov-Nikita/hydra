"""Durable dashboard sync jobs over the incremental telemetry queue."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import threading
import uuid

from .exact_time import public_timestamp
from .incremental_sync import IncrementalSyncWorker, ResumableRepair, TrustedSourceRoots
from .reconcile_engine import reconcile_project
from .storage import HydraStore
from .sync_state import SyncJob, SyncStateRepository


_TERMINAL = frozenset({"succeeded", "partial", "failed"})


def _summary(job: SyncJob | None) -> dict[str, object]:
    """Serialize a job without leaking its private queue/frontier details."""
    if job is None:
        return {
            "schema_version": "hydra.dashboard-sync/v1", "sync_ref": None,
            "kind": None, "state": "idle", "started_at": None,
            "finished_at": None,
            "progress": {"sources_queued": 0, "sources_processed": 0, "new_bytes": 0},
        }
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
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()

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
        jobs = repository.list_jobs(1)
        return jobs[0] if jobs else None

    def current(self) -> dict[str, object]:
        store, repository = self._repository()
        try:
            return _summary(self._latest(repository))
        finally:
            store.close()

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
            revision = repository.data_revision()
            return {
                "schema_version": "hydra.dashboard-changes/v1",
                "data_revision": revision, "changed": revision > after,
                "sync": _summary(self._latest(repository)),
            }
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
        if not reused:
            thread = threading.Thread(
                target=self._run, args=(kind, job_id), name="hydra-dashboard-sync", daemon=True,
            )
            with self._lock:
                self._threads.add(thread)
            try:
                thread.start()
            except Exception:
                with self._lock:
                    self._threads.discard(thread)
                self._fail(job_id)
                return self.get(job_id), False
        return _summary(job), reused

    def _run(self, kind: str, job_id: str) -> None:
        try:
            if kind == "sync":
                self._run_sync(job_id)
            else:
                self._run_repair(job_id)
        except Exception:
            self._fail(job_id)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _run_sync(self, job_id: str) -> None:
        assert self._roots is not None
        store, repository = self._repository()
        try:
            now = self._now()
            queued = len(repository.list_queue(1000))
            repository.update_job(
                job_id, state="running", sources_discovered=queued,
                sources_completed=0, bytes_processed=0, updated_at=now,
            )
            expiry = public_timestamp(self._clock() + timedelta(seconds=300))
            worker = IncrementalSyncWorker(
                store, self._roots,
                reconcile=lambda project_id: reconcile_project(store, project_id, self._key),
            )
            result = worker.sync_once("dashboard-" + uuid.uuid4().hex, now, expiry)
            current = repository.get_job(job_id)
            assert current is not None
            repository.update_job(
                job_id,
                state="partial" if result.repair_required else "succeeded",
                sources_discovered=max(current.sources_discovered, result.claimed),
                sources_completed=result.completed,
                bytes_processed=result.bytes_processed,
                updated_at=self._now(), completed_at=self._now(),
            )
        finally:
            store.close()

    def _run_repair(self, job_id: str) -> None:
        assert self._roots is not None
        store, repository = self._repository()
        try:
            now = self._now()
            repository.update_job(
                job_id, state="running", sources_discovered=0,
                sources_completed=0, bytes_processed=0, updated_at=now,
            )
            repair = ResumableRepair(store, self._roots)
            if not repository.list_frontier(job_id):
                for root_kind in ("sessions", "archived_sessions"):
                    try:
                        self._roots.root_for(root_kind)
                    except Exception:
                        continue
                    repository.save_frontier(
                        job_id=job_id, root_kind=root_kind, directory_locator="@root",
                        state="pending", discovered_count=0, updated_at=now,
                    )
            repair.run_batch(job_id, now)
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
        with self._lock:
            threads = tuple(self._threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout)
