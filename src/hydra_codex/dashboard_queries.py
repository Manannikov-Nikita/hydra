"""Privacy-safe storage queries shared by the dashboard refresh flow."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import sqlite3
from typing import Protocol, cast

from .audit_model import AuditEvidence
from .audit_service import build_pilot_audit
from .dashboard_model import (
    DashboardProjectCatalog,
    DashboardProjectSummary,
    DashboardRefreshView,
    DashboardSnapshot,
    DashboardTaskPage,
    canonical_json,
)
from .dashboard_projections import project_pilot_status, project_storage_status
from .diagnostics import DoctorReport
from .exact_time import public_timestamp
from .pilot import read_only_pilot_statuses, read_pilot_status
from .project import ProjectResolution
from .public_payload import is_safe_dashboard_display_name, reject_private_fields
from .public_refs import project_catalog_references
from .reconcile_engine import ReconciliationStale, list_reconciled_reports
from .report_operations import compare_reports
from .reporting import ComparisonReport, NumericFact, TaskReport
from .storage import HydraStore
from .storage_health import storage_status


@dataclass(frozen=True)
class CatalogProject:
    """A private catalog row containing no filesystem location fields."""

    project_id: str = field(repr=False)
    display_name: str | None
    first_seen_at: str
    last_seen_at: str
    display_name_provenance: str | None = None


def _catalog_rows(connection: sqlite3.Connection) -> tuple[CatalogProject, ...]:
    return tuple(
        CatalogProject(
            str(row["project_id"]),
            None if row["display_name"] is None else str(row["display_name"]),
            str(row["first_seen_at"]), str(row["last_seen_at"]),
            None if row["display_name_provenance"] is None else str(row["display_name_provenance"]),
        )
        for row in connection.execute(
            """SELECT project_id,display_name,first_seen_at,last_seen_at,display_name_provenance
                 FROM dashboard_projects ORDER BY project_id""",
        )
    )


def sync_project_catalog(
    store: HydraStore, observed_at: str,
) -> tuple[CatalogProject, ...]:
    """Derive catalog identities/timestamps without copying stored path fields."""
    rows = store.connection.execute(
        """WITH observations(project_id, seen_at) AS (
            SELECT project_id, started_at FROM sessions
            UNION ALL SELECT project_id, COALESCE(last_activity_at, started_at)
              FROM rollout_sessions
            UNION ALL SELECT project_id, started_at FROM reconciliation_runs
            UNION ALL SELECT project_id, started_at FROM pilot_runs
            UNION ALL SELECT project_id, observed_at FROM storage_audit_snapshots
        )
        SELECT project_id, MIN(COALESCE(seen_at, ?)), MAX(COALESCE(seen_at, ?))
          FROM observations WHERE project_id <> '' GROUP BY project_id
          ORDER BY project_id""",
        (observed_at, observed_at),
    ).fetchall()
    with store.rollout_transaction() as connection:
        for project_id, first_seen, last_seen in rows:
            connection.execute(
                """INSERT INTO dashboard_projects(
                       project_id,display_name,first_seen_at,last_seen_at,display_name_provenance)
                   VALUES (?,NULL,?,?,NULL)
                   ON CONFLICT(project_id) DO UPDATE SET
                     first_seen_at=MIN(first_seen_at,excluded.first_seen_at),
                     last_seen_at=MAX(last_seen_at,excluded.last_seen_at)""",
                (project_id, first_seen, last_seen),
            )
    return _catalog_rows(store.connection)


def observe_resolved_project(
    store: HydraStore, resolution: ProjectResolution, observed_at: str,
) -> None:
    """Remember a local project's optional trusted display name and recency."""
    with store.rollout_transaction() as connection:
        connection.execute(
            """INSERT INTO dashboard_projects(
                   project_id,display_name,first_seen_at,last_seen_at,display_name_provenance)
               VALUES (?,?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET
                 display_name=CASE WHEN excluded.display_name_provenance='config'
                     THEN excluded.display_name ELSE COALESCE(display_name,excluded.display_name) END,
                 display_name_provenance=CASE WHEN excluded.display_name_provenance='config'
                     THEN 'config' ELSE COALESCE(display_name_provenance,excluded.display_name_provenance) END,
                 last_seen_at=MAX(last_seen_at,excluded.last_seen_at)""",
            (
                resolution.project_id, resolution.display_name, observed_at, observed_at,
                resolution.display_name_provenance,
            ),
        )


StoreFactory = Callable[[], HydraStore]
Clock = Callable[[], datetime]


class _ConnectionSource(Protocol):
    connection: sqlite3.Connection


@dataclass(frozen=True)
class _BootstrapStore:
    connection: sqlite3.Connection


class DashboardQueryService:
    """Read-only project-scoped queries over already-refreshed Hydra data."""

    def __init__(
        self,
        store_factory: StoreFactory,
        installation_key: bytes,
        clock: Clock,
        doctor_report: DoctorReport,
    ) -> None:
        if not callable(store_factory) or not callable(clock):
            raise TypeError("dashboard store factory and clock must be callable")
        if not isinstance(installation_key, bytes) or len(installation_key) < 16:
            raise ValueError("installation key must contain at least 16 bytes")
        if not isinstance(doctor_report, DoctorReport):
            raise TypeError("doctor_report must be a DoctorReport")
        self._store_factory = store_factory
        self._installation_key = installation_key
        self._clock = clock
        self._doctor_report = doctor_report

    def _generated_at(self) -> str:
        return public_timestamp(self._clock())

    def _catalog(
        self, store: _ConnectionSource,
    ) -> tuple[tuple[CatalogProject, str], ...]:
        catalog = _catalog_rows(store.connection)
        projection = project_catalog_references(
            (item.project_id for item in catalog), self._installation_key,
        )
        return tuple((item, projection[item.project_id]) for item in catalog)

    @staticmethod
    def _unknown() -> KeyError:
        return KeyError("unknown public reference")

    def _resolve_project(
        self,
        catalog: tuple[tuple[CatalogProject, str], ...],
        project_ref: str,
    ) -> CatalogProject:
        match = next(
            (item for item, public_ref in catalog if public_ref == project_ref),
            None,
        )
        if match is None:
            raise self._unknown()
        return match

    @staticmethod
    def _ordered_reports(reports: tuple[TaskReport, ...]) -> tuple[TaskReport, ...]:
        def ordering(report: TaskReport) -> tuple[float, str]:
            observed = datetime.fromisoformat(
                report.last_activity_at.replace("Z", "+00:00"),
            )
            return (-observed.timestamp(), report.task_ref)

        return tuple(sorted(reports, key=ordering))

    def _reports(
        self, store: HydraStore, project_id: str,
    ) -> tuple[tuple[TaskReport, ...], str]:
        try:
            with read_only_pilot_statuses():
                reports = cast(
                    tuple[TaskReport, ...],
                    list_reconciled_reports(store, project_id),
                )
        except ReconciliationStale:
            return (), "stale"
        return self._ordered_reports(reports), "current"

    @staticmethod
    def _display_name(item: CatalogProject, project_ref: str) -> str:
        if item.display_name and is_safe_dashboard_display_name(
            item.display_name, item.project_id,
        ):
            return item.display_name
        return f"Project {project_ref.removeprefix('project_')[:8]}"

    @staticmethod
    def _unavailable(
        unit: str, caveat: str = "no_reconciled_tasks",
    ) -> NumericFact:
        return NumericFact(None, unit, "estimated", (caveat,))

    @staticmethod
    def _bootstrap_storage() -> dict[str, object]:
        """Return an explicit unknown storage view without scanning event tables."""
        caveat = "dashboard_refresh_required"

        def fact(unit: str) -> dict[str, object]:
            if unit == "bytes":
                return {
                    "value": None,
                    "unit": "bytes",
                    "provenance": "estimated",
                    "caveats": [caveat],
                    "lower_bound": None,
                }
            return NumericFact(None, unit, "estimated", (caveat,)).as_dict()

        return {
            "baseline_state": "unavailable",
            "current": {
                "database_bytes": fact("bytes"),
                "wal_bytes": fact("bytes"),
                "rollout_sources": fact("count"),
                "rollout_events": fact("count"),
                "codex_event_sources": fact("count"),
                "codex_events": fact("count"),
                "schema_version": fact("count"),
            },
            "baseline": None,
            "growth": None,
            "diagnostics": [
                {"code": caveat, "severity": "info"},
            ],
        }

    @staticmethod
    def _recent_task(report: TaskReport) -> dict[str, object]:
        return {
            "task_ref": report.task_ref,
            "status": report.status,
            "last_activity_at": report.last_activity_at,
            "task_family": report.task_family,
            "headline": {
                "working_tokens": report.deduplicated_tokens.working.as_dict(),
                "full_context_tokens": report.deduplicated_tokens.full_context.as_dict(),
                "wall_clock_ms": report.wall_clock.as_dict(),
            },
        }

    def _project_payload(
        self,
        store: HydraStore | _BootstrapStore,
        item: CatalogProject,
        project_ref: str,
        reports: tuple[TaskReport, ...],
        freshness_state: str,
        *,
        bootstrap: bool = False,
    ) -> dict[str, object]:
        latest = reports[0] if reports else None
        if latest is None:
            unavailable_caveat = (
                "dashboard_refresh_required"
                if bootstrap and freshness_state == "stale"
                else "no_reconciled_tasks"
            )
            headline = {
                "working_tokens": self._unavailable(
                    "tokens", unavailable_caveat,
                ).as_dict(),
                "full_context_tokens": self._unavailable(
                    "tokens", unavailable_caveat,
                ).as_dict(),
                "wall_clock_ms": self._unavailable(
                    "milliseconds", unavailable_caveat,
                ).as_dict(),
            }
            phase_allocation: object = None
        else:
            headline = {
                "working_tokens": latest.deduplicated_tokens.working.as_dict(),
                "full_context_tokens": latest.deduplicated_tokens.full_context.as_dict(),
                "wall_clock_ms": latest.wall_clock.as_dict(),
            }
            phase_allocation = latest.semantic_breakdown.as_dict()
        if bootstrap:
            pilot = None
        else:
            pilot_row = store.connection.execute(
                """SELECT pilot_id FROM pilot_runs WHERE project_id=?
                     ORDER BY started_at DESC,pilot_id DESC LIMIT 1""",
                (item.project_id,),
            ).fetchone()
            pilot = (
                None
                if pilot_row is None
                else project_pilot_status(
                    read_pilot_status(
                        store, item.project_id, str(pilot_row[0]),
                    ).as_dict(),
                )
            )
        payload: dict[str, object] = {
            "project_ref": project_ref,
            "display_name": self._display_name(item, project_ref),
            "last_activity_at": (
                latest.last_activity_at if latest is not None else item.last_seen_at
            ),
            "freshness_state": freshness_state,
            "overview": {
                "basis": {
                    "kind": "latest_task",
                    "task_ref": None if latest is None else latest.task_ref,
                },
                "headline": headline,
                "phase_allocation": phase_allocation,
            },
            "recent_tasks": [self._recent_task(report) for report in reports[:10]],
            "pilot": pilot,
            "storage": (
                self._bootstrap_storage()
                if bootstrap
                else project_storage_status(storage_status(
                    cast(HydraStore, store), item.project_id,
                ))
            ),
            "system_health": {
                "scope": "global_launch_context",
                "doctor": self._doctor_report.as_dict(),
            },
        }
        reject_private_fields(payload)
        return payload

    def snapshot(
        self,
        *,
        project_ref: str | None,
        task_ref: str | None,
        refresh: DashboardRefreshView,
    ) -> DashboardSnapshot:
        store = self._store_factory()
        try:
            return self._snapshot_from_store(
                store, project_ref=project_ref, task_ref=task_ref, refresh=refresh,
            )
        finally:
            store.close()

    def bootstrap_snapshots(
        self, *, refresh: DashboardRefreshView,
    ) -> tuple[dict[str, DashboardSnapshot], DashboardSnapshot | None]:
        """Build a bounded launch cache from persisted catalog metadata only.

        Strict report reconstruction intentionally remains behind explicit Refresh:
        on a large history it validates every task tree and is not a safe startup
        dependency.  Launch data therefore marks projects with reconciled rows as
        stale and keeps their metrics unavailable until Refresh publishes a fully
        validated replacement.
        """
        store = self._store_factory()
        try:
            return self._bootstrap_snapshots_from_source(store, refresh=refresh)
        finally:
            store.close()

    def bootstrap_snapshots_from_connection(
        self,
        connection: sqlite3.Connection,
        *,
        refresh: DashboardRefreshView,
    ) -> tuple[dict[str, DashboardSnapshot], DashboardSnapshot | None]:
        """Build launch DTOs from a caller-owned, bounded read-only connection."""
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("dashboard bootstrap requires a SQLite connection")
        return self._bootstrap_snapshots_from_source(
            _BootstrapStore(connection), refresh=refresh,
        )

    def _bootstrap_snapshots_from_source(
        self,
        store: HydraStore | _BootstrapStore,
        *,
        refresh: DashboardRefreshView,
    ) -> tuple[dict[str, DashboardSnapshot], DashboardSnapshot | None]:
        catalog = self._catalog(store)
        by_project: dict[str, tuple[tuple[TaskReport, ...], str]] = {}
        summaries: list[DashboardProjectSummary] = []
        for item, public_ref in catalog:
            state = "stale"
            by_project[item.project_id] = ((), state)
            summaries.append(DashboardProjectSummary(
                public_ref,
                self._display_name(item, public_ref),
                item.last_seen_at,
                state,
                NumericFact(
                    None, "count", "estimated", ("dashboard_refresh_required",),
                ),
            ))
        project_catalog = DashboardProjectCatalog(tuple(summaries))
        generated_at = self._generated_at()
        if not catalog:
            empty = self._assemble_snapshot(
                store, catalog, by_project, project_catalog,
                project_ref=None, task_ref=None, refresh=refresh,
                generated_at=generated_at,
                bootstrap=True,
            )
            return {}, empty
        snapshots = {
            public_ref: self._assemble_snapshot(
                store, catalog, by_project, project_catalog,
                project_ref=public_ref, task_ref=None, refresh=refresh,
                generated_at=generated_at,
                bootstrap=True,
                resolved_project=item,
            )
            for item, public_ref in catalog
        }
        return snapshots, None

    def _snapshot_from_store(
        self,
        store: HydraStore | _BootstrapStore,
        *,
        project_ref: str | None,
        task_ref: str | None,
        refresh: DashboardRefreshView,
        generated_at: str | None = None,
    ) -> DashboardSnapshot:
        """Build one immutable DTO without taking ownership of *store*."""
        catalog, by_project, summaries = self._prepare_snapshot_state(store)
        return self._assemble_snapshot(
            store,
            catalog,
            by_project,
            summaries,
            project_ref=project_ref,
            task_ref=task_ref,
            refresh=refresh,
            generated_at=generated_at or self._generated_at(),
        )

    def _prepare_snapshot_state(
        self, store: HydraStore,
    ) -> tuple[
        tuple[tuple[CatalogProject, str], ...],
        dict[str, tuple[tuple[TaskReport, ...], str]],
        DashboardProjectCatalog,
    ]:
        catalog = self._catalog(store)
        by_project: dict[str, tuple[tuple[TaskReport, ...], str]] = {}
        summaries: list[DashboardProjectSummary] = []
        for item, public_ref in catalog:
            reports, state = self._reports(store, item.project_id)
            by_project[item.project_id] = (reports, state)
            count = int(store.connection.execute(
                "SELECT COUNT(*) FROM reconciled_tasks WHERE project_id=?",
                (item.project_id,),
            ).fetchone()[0])
            summaries.append(DashboardProjectSummary(
                public_ref,
                self._display_name(item, public_ref),
                reports[0].last_activity_at if reports else item.last_seen_at,
                state,
                NumericFact(count, "count", "derived"),
            ))
        return catalog, by_project, DashboardProjectCatalog(tuple(summaries))

    def _assemble_snapshot(
        self,
        store: HydraStore,
        catalog: tuple[tuple[CatalogProject, str], ...],
        by_project: Mapping[str, tuple[tuple[TaskReport, ...], str]],
        summaries: tuple[DashboardProjectSummary, ...] | DashboardProjectCatalog,
        *,
        project_ref: str | None,
        task_ref: str | None,
        refresh: DashboardRefreshView,
        generated_at: str,
        bootstrap: bool = False,
        resolved_project: CatalogProject | None = None,
    ) -> DashboardSnapshot:
        if project_ref is None:
            selected_ref = min((public_ref for _item, public_ref in catalog), default=None)
            selected_item = None
        else:
            selected_ref = project_ref
            selected_item = (
                resolved_project
                if resolved_project is not None
                else self._resolve_project(catalog, project_ref)
            )
        if task_ref is not None and selected_ref is None:
            raise self._unknown()
        project_json: str | None = None
        selected_task_json: str | None = None
        selected_state = "unavailable" if selected_ref is None else "current"
        if selected_ref is not None:
            item = (
                selected_item
                if selected_item is not None
                else self._resolve_project(catalog, selected_ref)
            )
            reports, selected_state = by_project[item.project_id]
            project_json = canonical_json(self._project_payload(
                store, item, selected_ref, reports, selected_state,
                bootstrap=bootstrap,
            ))
            if task_ref is not None:
                selected = next(
                    (report for report in reports if report.task_ref == task_ref),
                    None,
                )
                if selected is None:
                    raise self._unknown()
                selected_task_json = canonical_json(selected.as_dict())
        freshness = {
            "state": selected_state,
            "doctor": {
                "scope": "global_launch_context",
                "report": self._doctor_report.as_dict(),
            },
        }
        return DashboardSnapshot(
            generated_at, freshness, summaries,
            selected_ref, project_json, selected_task_json, refresh,
        )

    def _refresh_snapshots_from_store(
        self,
        store: HydraStore,
        *,
        refresh: DashboardRefreshView,
        project_ids: set[str] | None,
    ) -> dict[str, DashboardSnapshot]:
        """Build a same-instant public-ref map inside a caller transaction."""
        catalog, by_project, summaries = self._prepare_snapshot_state(store)
        generated_at = self._generated_at()
        return {
            public_ref: self._assemble_snapshot(
                store,
                catalog,
                by_project,
                summaries,
                project_ref=public_ref,
                task_ref=None,
                refresh=refresh,
                generated_at=generated_at,
                resolved_project=item,
            )
            for item, public_ref in catalog
            if project_ids is None or item.project_id in project_ids
        }

    def tasks(
        self,
        project_ref: str,
        *,
        cursor: str | None,
        limit: int = 50,
    ) -> DashboardTaskPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        generated_at = self._generated_at()
        store = self._store_factory()
        try:
            catalog = self._catalog(store)
            item = self._resolve_project(catalog, project_ref)
            reports, _state = self._reports(store, item.project_id)
            start = 0
            if cursor is not None:
                try:
                    start = next(
                        index for index, report in enumerate(reports)
                        if report.task_ref == cursor
                    ) + 1
                except StopIteration:
                    raise self._unknown() from None
            selected = reports[start:start + limit]
            has_more = start + len(selected) < len(reports)
            next_cursor = selected[-1].task_ref if selected and has_more else None
            return DashboardTaskPage(
                generated_at,
                project_ref,
                tuple(canonical_json(report.as_dict()) for report in selected),
                limit,
                next_cursor,
            )
        finally:
            store.close()

    def compare(
        self,
        project_ref: str,
        left: str,
        right: str,
    ) -> ComparisonReport:
        store = self._store_factory()
        try:
            item = self._resolve_project(self._catalog(store), project_ref)
            reports, _state = self._reports(store, item.project_id)
            by_ref = {report.task_ref: report for report in reports}
            try:
                baseline, current = by_ref[left], by_ref[right]
            except KeyError:
                raise self._unknown() from None
            comparison = compare_reports(baseline, current)
            reject_private_fields(comparison.as_dict())
            return comparison
        finally:
            store.close()

    def evidence(
        self,
        project_ref: str,
        evidence_id: str,
    ) -> AuditEvidence:
        store = self._store_factory()
        try:
            item = self._resolve_project(self._catalog(store), project_ref)
            row = store.connection.execute(
                """SELECT pilot_id FROM pilot_runs WHERE project_id=?
                     ORDER BY started_at DESC,pilot_id DESC LIMIT 1""",
                (item.project_id,),
            ).fetchone()
            if row is None:
                raise self._unknown()
            audit = build_pilot_audit(
                store,
                project_id=item.project_id,
                pilot_id=str(row[0]),
                refresh_enrollment=False,
            )
            match = next(
                (
                    evidence for evidence in audit.evidence_appendix
                    if evidence.evidence_id == evidence_id
                ),
                None,
            )
            if match is None:
                raise self._unknown()
            reject_private_fields(match.as_dict())
            return match
        finally:
            store.close()
