"""Concrete local command services for the Hydra CLI."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import uuid

from .annotation_spool import stage_annotation
from .contracts import ModelAnnotationInput
from .project import ProjectResolution, resolve_project
from .report_operations import compare_reports
from .report_renderers import (
    render_html,
    render_json,
    render_markdown,
    render_report_collection,
)
from .rollout_identity import Pseudonymizer
from .platform_paths import default_installation_key_path
from .storage import HydraStore
from .sync_state import DirtyRoot, SyncStateRepository


Clock = Callable[[], datetime]


@contextmanager
def _consistent_read(connection: sqlite3.Connection) -> Iterator[None]:
    """Pin related report reads to one WAL snapshot without committing writes."""
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction and connection.in_transaction:
            connection.rollback()


def configured_database_path(
    environ: Mapping[str, str], explicit: Path | None,
) -> Path | None:
    """Use one database selection rule for hooks and every CLI command."""
    if explicit is not None:
        return explicit.expanduser()
    configured = environ.get("HYDRA_DATABASE_PATH")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    return None


def configured_installation_key_path(
    environ: Mapping[str, str], injected: Path | None,
) -> Path:
    """Use one installation identity for ingestion, annotations, and reports."""
    if injected is not None:
        return injected.expanduser()
    configured = environ.get("HYDRA_INSTALLATION_KEY_PATH")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    home_value = environ.get("HOME")
    home = (
        Path(home_value).expanduser()
        if isinstance(home_value, str) and home_value
        else Path.home()
    )
    return default_installation_key_path(home, environ=environ)


def _utc_now(clock: Clock) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("service clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload(annotation: ModelAnnotationInput) -> dict[str, object]:
    return {
        "kind": annotation.kind.value,
        "phase": annotation.phase.value,
        "cause": annotation.cause.value,
        "scope_change": annotation.scope_change.value,
        "task_family": annotation.task_family,
        "confidence": annotation.confidence,
        "note": annotation.note,
        **({"task_label": annotation.task_label} if annotation.task_label is not None else {}),
        **(
            {"outcome": annotation.outcome.value}
            if annotation.outcome is not None
            else {}
        ),
    }


def _renderer(output_format: str):
    renderers = {
        "json": render_json,
        "markdown": render_markdown,
        "html": render_html,
    }
    try:
        return renderers[output_format]
    except KeyError as error:
        raise ValueError("unsupported report format") from error


class LocalCommandServices:
    """Resolve trusted local context and execute CLI operations against SQLite."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str],
        installation_key_path: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._environ = environ
        self._installation_key_path = installation_key_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _project(self, cwd: Path) -> ProjectResolution:
        return resolve_project(cwd)

    def _database_path(self, explicit: Path | None) -> Path | None:
        return configured_database_path(self._environ, explicit)

    def _key_path(self) -> Path:
        return configured_installation_key_path(
            self._environ, self._installation_key_path,
        )

    def _keys(self) -> Pseudonymizer:
        return Pseudonymizer.installation_key(self._key_path())

    def annotate(
        self,
        annotation: ModelAnnotationInput,
        capability: str,
        database_path: Path | None,
        cwd: Path,
    ) -> object:
        _ = database_path, cwd
        return stage_annotation(self._environ, capability, _payload(annotation))

    def reconcile(self, database_path: Path | None, cwd: Path) -> object:
        from .reconcile_engine import reconcile_project

        project = self._project(cwd)
        keys = self._keys()
        store = HydraStore(self._database_path(database_path))
        try:
            return reconcile_project(store, project.project_id, keys.key)
        finally:
            store.close()

    def _sync_roots(self) -> object:
        from .incremental_sync import TrustedSourceRoots

        home = Path(self._environ.get("HOME", str(Path.home()))).expanduser()
        return TrustedSourceRoots(
            sessions=home / ".codex" / "sessions",
            archived_sessions=home / ".codex" / "archived_sessions",
        )

    def sync(self, database_path: Path | None, cwd: Path) -> dict[str, object]:
        """Process only queued source locators; directory discovery is never reachable here."""
        _ = cwd
        from .incremental_sync import IncrementalSyncWorker

        now = _utc_now(self._clock)
        expiry = (datetime.fromisoformat(now.replace("Z", "+00:00"))
                  + timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
        store = HydraStore.open_bounded_writer(
            self._database_path(database_path),
        )
        try:
            result = IncrementalSyncWorker(
                store, self._sync_roots(),
                reconcile=lambda project_id, roots: self._reconcile_store(
                    store, project_id, roots,
                ),
            ).sync_once("cli-" + uuid.uuid4().hex, now, expiry)
            return {
                "command": "sync", "status": "ok", "claimed": result.claimed,
                "completed": result.completed, "repair_required": result.repair_required,
                "bytes_processed": result.bytes_processed,
            }
        finally:
            store.close()

    def _reconcile_store(
        self,
        store: HydraStore,
        project_id: str,
        expected_dirty_roots: tuple[DirtyRoot, ...],
    ) -> object:
        from .reconcile_engine import reconcile_project

        return reconcile_project(
            store,
            project_id,
            self._keys().key,
            expected_dirty_roots=expected_dirty_roots,
        )

    def repair(self, database_path: Path | None, cwd: Path) -> dict[str, object]:
        """Run every bounded batch of the explicit resumable history repair."""
        _ = cwd
        from .incremental_sync import ResumableRepair

        now = _utc_now(self._clock)
        store = HydraStore(self._database_path(database_path))
        try:
            repair = ResumableRepair(store, self._sync_roots())
            job_id = repair.start_backfill(now, job_kind="repair")
            repository = SyncStateRepository(store)
            directories_scanned = sources_discovered = batches = 0
            while True:
                before = self._repair_progress(store, job_id)
                result = repair.run_batch(
                    job_id, _utc_now(self._clock), directory_limit=100,
                )
                batches += 1
                directories_scanned += result.directories_scanned
                sources_discovered += result.discovered
                job = repository.get_job(job_id)
                if job is None:
                    raise RuntimeError("repair job disappeared")
                if result.completed or job.state in {"succeeded", "partial", "failed"}:
                    status = {
                        "succeeded": "complete",
                        "partial": "partial",
                        "failed": "failed",
                    }.get(job.state, "complete")
                    payload: dict[str, object] = {
                        "command": "repair", "status": status,
                        "directories_scanned": directories_scanned,
                        "sources_discovered": sources_discovered,
                        "batches": batches,
                    }
                    if job.state == "partial":
                        payload["diagnostic"] = "repair_required"
                    return payload
                if not result.lease_acquired:
                    return {
                        "command": "repair",
                        "status": (
                            job.state
                            if job.state in {"queued", "running"}
                            else "queued"
                        ),
                        "diagnostic": "lease_busy",
                        "directories_scanned": directories_scanned,
                        "sources_discovered": sources_discovered,
                        "batches": batches,
                    }
                if self._repair_progress(store, job_id) == before:
                    # Another process may hold the singleton lease.  Leave the
                    # durable frontier active for a later invocation instead
                    # of spinning or pretending the explicit --all completed.
                    return {
                        "command": "repair", "status": "partial",
                        "diagnostic": "no_progress",
                        "directories_scanned": directories_scanned,
                        "sources_discovered": sources_discovered,
                        "batches": batches,
                    }
        finally:
            store.close()

    @staticmethod
    def _repair_progress(store: HydraStore, job_id: str) -> tuple[object, ...]:
        """Return path-free durable progress markers for repair loop liveness."""
        job = SyncStateRepository(store).get_job(job_id)
        if job is None:
            raise RuntimeError("repair job disappeared")
        frontier = tuple(
            (str(row[0]), int(row[1]))
            for row in store.connection.execute(
                """SELECT state,COUNT(*) FROM sync_backfill_frontier
                     WHERE job_id=? GROUP BY state ORDER BY state""",
                (job_id,),
            )
        )
        dirty = int(store.connection.execute(
            "SELECT COUNT(*) FROM sync_dirty_roots",
        ).fetchone()[0])
        return (
            job.state, job.sources_discovered, job.sources_completed,
            job.bytes_processed, frontier, dirty,
        )

    def report(
        self,
        last: int,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .reconcile_engine import ReconciliationStale, render_materialized_report_collection

        project = self._project(cwd)
        store = HydraStore.open_current(self._database_path(database_path))
        try:
            with _consistent_read(store.connection):
                freshness = self._sync_freshness(store, project.project_id)
                try:
                    return render_materialized_report_collection(
                        store, project.project_id, last, output_format, freshness,
                    )
                except ReconciliationStale:
                    return render_report_collection(
                        (), output_format,
                        sync_freshness={**freshness, "state": "reconcile_required"},
                    )
        finally:
            store.close()

    def _sync_freshness(
        self,
        store: HydraStore,
        project_id: str,
    ) -> dict[str, object]:
        from .reconcile_engine import source_fact_fence_current

        repository = SyncStateRepository(store)
        connection = store.connection
        now = _utc_now(self._clock)
        repair = connection.execute(
            """SELECT 1 FROM sync_source_registry
                 WHERE source_state='repair_required' AND project_id=?
                 LIMIT 1""",
            (project_id,),
        ).fetchone() is not None
        queued = connection.execute(
            """SELECT 1 FROM sync_ingest_queue AS queue
                 JOIN sync_source_registry AS source
                   ON source.root_kind=queue.root_kind
                  AND source.source_locator=queue.source_locator
                 WHERE source.project_id=?
                   AND CASE
                         WHEN queue.queue_state='queued' THEN 1
                         WHEN queue.queue_state='claimed'
                              AND queue.claim_expires_at IS NULL THEN 1
                         WHEN queue.queue_state='claimed' THEN
                              hydra_rfc3339_micros(queue.claim_expires_at)
                              <=hydra_rfc3339_micros(?)
                         ELSE 0
                       END=1
                 LIMIT 1""", (project_id, now),
        ).fetchone() is not None or connection.execute(
            """SELECT 1 FROM hook_event_outbox
                 WHERE project_id=? AND acknowledged_at IS NULL
                   AND CASE
                         WHEN claimed_by IS NULL OR claim_expires_at IS NULL THEN 1
                         ELSE hydra_rfc3339_micros(claim_expires_at)
                              <=hydra_rfc3339_micros(?)
                       END=1
                 LIMIT 1""", (project_id, now),
        ).fetchone() is not None or connection.execute(
            """SELECT 1 FROM sync_dirty_roots
                 WHERE project_id=?
                   AND (
                     claim_owner IS NULL
                     OR claim_expires_at IS NULL
                     OR hydra_rfc3339_micros(claim_expires_at)
                        <=hydra_rfc3339_micros(?)
                   )
                 LIMIT 1""",
            (project_id, now),
        ).fetchone() is not None
        running = connection.execute(
            """SELECT 1 FROM sync_ingest_queue AS queue
                 JOIN sync_source_registry AS source
                   ON source.root_kind=queue.root_kind
                  AND source.source_locator=queue.source_locator
                 WHERE source.project_id=?
                   AND queue.queue_state='claimed'
                   AND queue.claim_expires_at IS NOT NULL
                   AND hydra_rfc3339_micros(queue.claim_expires_at)
                       >hydra_rfc3339_micros(?)
                 LIMIT 1""", (project_id, now),
        ).fetchone() is not None or connection.execute(
            """SELECT 1 FROM hook_event_outbox
                 WHERE project_id=? AND acknowledged_at IS NULL
                   AND CASE
                         WHEN claimed_by IS NULL OR claim_expires_at IS NULL THEN 0
                         ELSE hydra_rfc3339_micros(claim_expires_at)
                              >hydra_rfc3339_micros(?)
                       END=1
                 LIMIT 1""", (project_id, now),
        ).fetchone() is not None or connection.execute(
            """SELECT 1 FROM sync_dirty_roots
                 WHERE project_id=?
                   AND claim_owner IS NOT NULL
                   AND claim_expires_at IS NOT NULL
                   AND hydra_rfc3339_micros(claim_expires_at)
                       >hydra_rfc3339_micros(?)
                 LIMIT 1""",
            (project_id, now),
        ).fetchone() is not None
        state = (
            "repair_required" if repair else "queued" if queued else "running" if running else "current"
        )
        if state == "current" and not source_fact_fence_current(
            connection, project_id,
        ):
            state = "reconcile_required"
        return {
            "schema_version": "hydra.sync-freshness/v1",
            "state": state,
            "data_revision": repository.data_revision(),
        }

    def compare(
        self,
        left: str,
        right: str,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .reconcile_engine import get_reconciled_report

        project = self._project(cwd)
        store = HydraStore.open_current(self._database_path(database_path))
        try:
            with store.read_transaction():
                baseline = get_reconciled_report(
                    store, project.project_id, left,
                )
                current = get_reconciled_report(
                    store, project.project_id, right,
                )
                return _renderer(output_format)(
                    compare_reports(baseline, current),
                )
        finally:
            store.close()

    def pilot_start(
        self,
        target: int,
        task_family: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        import json

        from .pilot import start_pilot

        project = self._project(cwd)
        store = HydraStore(self._database_path(database_path))
        try:
            run = start_pilot(
                store,
                project_id=project.project_id,
                target=target,
                task_family=task_family,
                now=self._clock(),
            )
            return json.dumps({
                "command": "pilot start",
                "pilot_id": run.pilot_id,
                "started_at": _utc_now(lambda: run.started_at),
                "state": run.state,
                "target": run.target,
                "task_family": run.task_family,
            }, sort_keys=True, separators=(",", ":"))
        finally:
            store.close()

    def pilot_status(
        self,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .pilot import pilot_status
        from .pilot_renderers import render_pilot_status

        project = self._project(cwd)
        store = HydraStore.open_current(self._database_path(database_path))
        try:
            row = store.connection.execute(
                """SELECT pilot_id FROM pilot_runs WHERE project_id=?
                     ORDER BY CASE state WHEN 'open' THEN 0 ELSE 1 END,
                              started_at DESC,pilot_id DESC LIMIT 1""",
                (project.project_id,),
            ).fetchone()
            if row is None:
                raise ValueError("project has no pilot")
            return render_pilot_status(
                pilot_status(store, project.project_id, str(row[0])),
                output_format,
            )
        finally:
            store.close()

    def pilot_close(
        self,
        pilot_id: str,
        audit_json: Path,
        decision: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        import json

        from .pilot import close_pilot

        project = self._project(cwd)
        audit_path = audit_json.expanduser()
        if not audit_path.is_absolute():
            audit_path = cwd / audit_path
        store = HydraStore(self._database_path(database_path))
        try:
            receipt = close_pilot(
                store,
                project_id=project.project_id,
                pilot_id=pilot_id,
                audit_json=audit_path,
                decision=decision,
                now=self._clock(),
            )
            return json.dumps(
                receipt.as_dict(), sort_keys=True, separators=(",", ":"),
            )
        finally:
            store.close()

    def audit(
        self,
        pilot_id: str,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .audit_service import generate_audit

        return generate_audit(
            environ=self._environ,
            database_path=self._database_path(database_path),
            installation_key_path=self._key_path(),
            cwd=cwd,
            pilot_id=pilot_id,
            output_format=output_format,
            observed_at=self._clock(),
        )

    def doctor(
        self,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .diagnostics import render_doctor, run_doctor

        return render_doctor(
            run_doctor(
                cwd=cwd,
                database_path=self._database_path(database_path),
            ),
            output_format,
        )

    def storage_status(
        self,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .storage_health import render_storage_status, storage_status

        project = self._project(cwd)
        store = HydraStore(self._database_path(database_path))
        try:
            return render_storage_status(
                storage_status(store, project.project_id), output_format,
            )
        finally:
            store.close()

    def storage_compact(
        self,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .storage_health import compact_storage, render_storage_compaction

        self._project(cwd)
        store = HydraStore(self._database_path(database_path))
        try:
            return render_storage_compaction(
                compact_storage(store), output_format,
            )
        finally:
            store.close()
