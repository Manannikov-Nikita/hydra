"""Privacy-safe storage queries shared by the dashboard refresh flow."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import json
import sqlite3
from typing import Protocol, cast

from .audit_model import AuditEvidence
from .audit_service import (
    read_materialized_pilot_audit,
    read_materialized_task_reports,
)
from .dashboard_model import (
    DashboardProjectCatalog,
    DashboardProjectSummary,
    DashboardRefreshView,
    DashboardSnapshot,
    DashboardTaskPage,
    canonical_json,
)
from .dashboard_contract import validate_task_report
from .dashboard_projections import project_pilot_status, project_storage_status
from .diagnostics import DoctorReport
from .exact_time import public_timestamp, require_exact_timestamp
from .pilot import read_only_pilot_statuses, read_pilot_status
from .project import ProjectResolution
from .public_payload import is_safe_dashboard_display_name, reject_private_fields
from .public_refs import project_catalog_references
from .reconcile_engine import ReconciliationStale, list_reconciled_reports
from .report_operations import compare_reports
from .reporting import (
    ComparisonReport,
    NumericFact,
    TaskReport,
    normalize_sync_freshness,
)
from .storage import HydraStore, ValidatedStoreProvider
from .storage_health import storage_status
from .sync_state import SyncStateRepository


@dataclass(frozen=True)
class CatalogProject:
    """A private catalog row containing no filesystem location fields."""

    project_id: str = field(repr=False)
    display_name: str | None
    first_seen_at: str
    last_seen_at: str
    display_name_provenance: str | None = None


def _catalog_rows(
    connection: sqlite3.Connection, maximum_revision: int | None = None,
) -> tuple[CatalogProject, ...]:
    materialized_filter = (
        ""
        if maximum_revision is None
        else " WHERE data_revision<=?"
    )
    parameters: tuple[object, ...] = (
        () if maximum_revision is None else (maximum_revision,)
    )
    return tuple(
        CatalogProject(
            str(row["project_id"]),
            None if row["display_name"] is None else str(row["display_name"]),
            str(row["first_seen_at"]), str(row["last_seen_at"]),
            None if row["display_name_provenance"] is None else str(row["display_name_provenance"]),
        )
        for row in connection.execute(
            f"""WITH materialized AS (
                   SELECT project_id,first_reconciled_at AS first_seen_at,
                          COALESCE(last_activity_at,last_reconciled_at)
                              AS last_seen_at
                     FROM materialized_project_stats{materialized_filter}
               ),
               catalog AS (
                   SELECT project_id,display_name,first_seen_at,last_seen_at,
                          display_name_provenance
                     FROM dashboard_projects
                   UNION ALL
                   SELECT materialized.project_id,NULL,materialized.first_seen_at,
                          materialized.last_seen_at,NULL
                     FROM materialized
                    WHERE NOT EXISTS (
                        SELECT 1 FROM dashboard_projects
                         WHERE dashboard_projects.project_id=materialized.project_id
                    )
               )
               SELECT project_id,display_name,first_seen_at,last_seen_at,
                      display_name_provenance
                 FROM catalog ORDER BY project_id""",
            parameters,
        )
    )


@contextmanager
def _consistent_read(connection: sqlite3.Connection) -> Iterator[None]:
    """Pin all reads to one SQLite snapshot without owning caller transactions."""
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction and connection.in_transaction:
            connection.rollback()


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
    SyncStateRepository(store).observe_project(
        project_id=resolution.project_id,
        display_name=resolution.display_name,
        display_name_provenance=(
            resolution.display_name_provenance
            if resolution.display_name_provenance is not None
            else "repo_basename" if resolution.display_name is not None else None
        ),
        observed_at=observed_at,
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
        self._store_factory = ValidatedStoreProvider(store_factory).open
        self._installation_key = installation_key
        self._clock = clock
        self._doctor_report = doctor_report

    def _generated_at(self) -> str:
        return public_timestamp(self._clock())

    @staticmethod
    def _materialized_state(
        connection: sqlite3.Connection,
    ) -> tuple[int, dict[str, object]]:
        """Read the revision and safe worker state in one bounded query."""
        row = connection.execute(
            """SELECT revision,
                      CASE
                        WHEN EXISTS(
                            SELECT 1 FROM sync_source_registry
                             WHERE source_state='repair_required'
                        ) THEN 'repair_required'
                        WHEN EXISTS(
                            SELECT 1 FROM sync_ingest_queue
                             WHERE queue_state='queued'
                        ) OR EXISTS(
                            SELECT 1 FROM hook_event_outbox
                             WHERE acknowledged_at IS NULL
                               AND claimed_by IS NULL
                        ) OR EXISTS(
                            SELECT 1 FROM sync_dirty_roots
                             WHERE claim_owner IS NULL
                        ) OR EXISTS(
                            SELECT 1 FROM sync_jobs WHERE state='queued'
                        ) THEN 'queued'
                        WHEN EXISTS(
                            SELECT 1 FROM sync_ingest_queue
                             WHERE queue_state='claimed'
                        ) OR EXISTS(
                            SELECT 1 FROM hook_event_outbox
                             WHERE acknowledged_at IS NULL
                               AND claimed_by IS NOT NULL
                        ) OR EXISTS(
                            SELECT 1 FROM sync_dirty_roots
                             WHERE claim_owner IS NOT NULL
                        ) OR EXISTS(
                            SELECT 1 FROM sync_jobs WHERE state='running'
                        ) THEN 'running'
                        ELSE 'current'
                      END AS sync_state
                 FROM sync_data_revision WHERE singleton=1""",
        ).fetchone()
        if row is None:
            raise ValueError("materialized sync state is unavailable")
        data_revision = int(row["revision"])
        return data_revision, normalize_sync_freshness({
            "schema_version": "hydra.sync-freshness/v1",
            "state": str(row["sync_state"]),
            "data_revision": data_revision,
        })

    def _catalog(
        self, store: _ConnectionSource, maximum_revision: int | None = None,
    ) -> tuple[tuple[CatalogProject, str], ...]:
        catalog = _catalog_rows(store.connection, maximum_revision)
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
            "display_name": report.display_name,
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
            with _consistent_read(store.connection):
                data_revision = SyncStateRepository(store).data_revision()
                try:
                    snapshots, empty = self._materialized_bootstrap_from_connection(
                        store.connection,
                        refresh=refresh,
                        selected_project_ref=project_ref,
                        selected_task_ref=task_ref,
                        require_materialized=True,
                    )
                except ReconciliationStale:
                    if task_ref is not None:
                        raise
                else:
                    if snapshots:
                        return next(iter(snapshots.values()))
                    if empty is not None:
                        return empty
                return self._snapshot_from_store(
                    store,
                    project_ref=project_ref,
                    task_ref=task_ref,
                    refresh=refresh,
                    data_revision=data_revision,
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
        with _consistent_read(connection):
            return self._materialized_bootstrap_from_connection(
                connection, refresh=refresh,
            )

    @staticmethod
    def _validated_project_stats_row(
        row: sqlite3.Row,
    ) -> tuple[object, ...] | None:
        """Validate one bounded catalog stat row without consulting report history."""
        revision = row["stats_data_revision"]
        count = row["report_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("materialized project stats are invalid")
        if revision is None:
            if count != 0 or any(
                row[field] is not None for field in (
                    "first_reconciled_at", "last_reconciled_at",
                    "first_activity_at", "first_activity_epoch_ns",
                    "last_activity_at", "last_activity_epoch_ns",
                )
            ):
                raise ValueError("materialized project stats are incoherent")
            return None
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError("materialized project stats are invalid")
        try:
            first_reconciled = require_exact_timestamp(
                row["first_reconciled_at"],
                "first materialized reconciliation",
            )
            last_reconciled = require_exact_timestamp(
                row["last_reconciled_at"],
                "last materialized reconciliation",
            )
            if first_reconciled.epoch_nanoseconds > last_reconciled.epoch_nanoseconds:
                raise ValueError
            if count == 0:
                if any(
                    row[field] is not None for field in (
                        "first_activity_at", "first_activity_epoch_ns",
                        "last_activity_at", "last_activity_epoch_ns",
                    )
                ):
                    raise ValueError
                return (
                    count, first_reconciled.canonical,
                    last_reconciled.canonical, None, None, None, None,
                    revision,
                )
            first_activity = require_exact_timestamp(
                row["first_activity_at"], "first materialized activity",
            )
            last_activity = require_exact_timestamp(
                row["last_activity_at"], "last materialized activity",
            )
            if (
                isinstance(row["first_activity_epoch_ns"], bool)
                or not isinstance(row["first_activity_epoch_ns"], int)
                or isinstance(row["last_activity_epoch_ns"], bool)
                or not isinstance(row["last_activity_epoch_ns"], int)
                or first_activity.epoch_nanoseconds
                    != row["first_activity_epoch_ns"]
                or last_activity.epoch_nanoseconds
                    != row["last_activity_epoch_ns"]
                or first_activity.epoch_nanoseconds
                    > last_activity.epoch_nanoseconds
            ):
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError("materialized project stats are invalid") from error
        return (
            count, first_reconciled.canonical, last_reconciled.canonical,
            first_activity.canonical, first_activity.epoch_nanoseconds,
            last_activity.canonical, last_activity.epoch_nanoseconds,
            revision,
        )

    def _materialized_bootstrap_from_connection(
        self,
        connection: sqlite3.Connection,
        *,
        refresh: DashboardRefreshView,
        selected_project_ref: str | None = None,
        selected_task_ref: str | None = None,
        require_materialized: bool = False,
    ) -> tuple[dict[str, DashboardSnapshot], DashboardSnapshot | None]:
        """Serve one warm project plus the full catalog with constant query count."""
        store = _BootstrapStore(connection)
        revision, sync_freshness = self._materialized_state(connection)
        rows = connection.execute(
            """WITH materialized AS (
                   SELECT project_id,report_count,first_reconciled_at,
                          last_reconciled_at,first_activity_at,
                          first_activity_epoch_ns,last_activity_at,
                          last_activity_epoch_ns,
                          data_revision AS stats_data_revision
                     FROM materialized_project_stats
                    WHERE data_revision<=?
               ),
               catalog AS (
                   SELECT projects.project_id,projects.display_name,
                          projects.first_seen_at,projects.last_seen_at,
                          projects.display_name_provenance,
                          COALESCE(materialized.report_count,0) AS report_count,
                          materialized.first_reconciled_at,
                          materialized.last_reconciled_at,
                          materialized.first_activity_at,
                          materialized.first_activity_epoch_ns,
                          materialized.last_activity_at,
                          materialized.last_activity_epoch_ns,
                          materialized.stats_data_revision
                     FROM dashboard_projects AS projects
                     LEFT JOIN materialized
                       ON materialized.project_id=projects.project_id
                   UNION ALL
                   SELECT materialized.project_id,NULL,
                          materialized.first_reconciled_at,
                          materialized.last_reconciled_at,NULL,
                          materialized.report_count,
                          materialized.first_reconciled_at,
                          materialized.last_reconciled_at,
                          materialized.first_activity_at,
                          materialized.first_activity_epoch_ns,
                          materialized.last_activity_at,
                          materialized.last_activity_epoch_ns,
                          materialized.stats_data_revision
                     FROM materialized
                    WHERE NOT EXISTS (
                        SELECT 1 FROM dashboard_projects
                         WHERE dashboard_projects.project_id=materialized.project_id
                    )
               ),
               pending AS (
                   SELECT project_id FROM hook_event_outbox
                    WHERE acknowledged_at IS NULL
                    GROUP BY project_id
                   UNION
                   SELECT project_id FROM sync_dirty_roots
                    GROUP BY project_id
               )
               SELECT catalog.*,
                      EXISTS(
                          SELECT 1 FROM pending
                           WHERE pending.project_id=catalog.project_id
                      ) AS has_pending_work
                 FROM catalog
                ORDER BY project_id""",
            (revision,),
        ).fetchall()
        prepared: list[tuple[CatalogProject, int, str, str]] = []
        stats_by_project: dict[str, tuple[object, ...] | None] = {}
        catalog_items = tuple(
            CatalogProject(
                str(row["project_id"]),
                None if row["display_name"] is None else str(row["display_name"]),
                str(row["first_seen_at"]),
                str(row["last_seen_at"]),
                (
                    None
                    if row["display_name_provenance"] is None
                    else str(row["display_name_provenance"])
                ),
            )
            for row in rows
        )
        projection = project_catalog_references(
            (item.project_id for item in catalog_items), self._installation_key,
        )
        by_id = {item.project_id: item for item in catalog_items}
        for row in rows:
            item = by_id[str(row["project_id"])]
            stats = self._validated_project_stats_row(row)
            stats_by_project[item.project_id] = stats
            count = int(row["report_count"])
            state = (
                "current"
                if count > 0 and not bool(row["has_pending_work"])
                else "stale"
            )
            prepared.append((
                item,
                count,
                state,
                (
                    str(row["last_reconciled_at"])
                    if row["last_reconciled_at"] is not None
                    else item.last_seen_at
                ),
            ))
        catalog = tuple(
            (item, projection[item.project_id])
            for item in catalog_items
        )
        generated_at = self._generated_at()
        if not catalog:
            if selected_project_ref is not None:
                raise self._unknown()
            project_catalog = DashboardProjectCatalog(())
            return {}, self._assemble_snapshot(
                store, catalog, {}, project_catalog,
                project_ref=None, task_ref=None, refresh=refresh,
                generated_at=generated_at, bootstrap=True,
                data_revision=revision,
            )

        if selected_project_ref is None:
            selected_item, selected_ref = min(catalog, key=lambda pair: pair[1])
        else:
            selected_item = self._resolve_project(catalog, selected_project_ref)
            selected_ref = selected_project_ref
        selected_count = next(
            report_count
            for item, report_count, _state, _last_reconciled_at in prepared
            if item.project_id == selected_item.project_id
        )
        if require_materialized and selected_count == 0:
            raise ReconciliationStale("reconcile_required")
        reports: list[dict[str, object]] = []
        selected_stats = stats_by_project[selected_item.project_id]
        expected_revision = (
            None if selected_stats is None else int(selected_stats[7])
        )
        for row in connection.execute(
            """SELECT task_ref,report_json,last_activity_at,
                      last_activity_epoch_ns,data_revision
                 FROM materialized_report_snapshots
                 WHERE project_id=? AND data_revision<=?
                 ORDER BY last_activity_epoch_ns DESC,task_ref
                 LIMIT 10""",
            (selected_item.project_id, revision),
        ):
            payload = self._materialized_payload(
                row, sync_freshness,
                expected_revision=expected_revision,
            )
            reports.append(payload)
        if len(reports) != min(selected_count, 10):
            raise ValueError("materialized project stats are incoherent")
        if selected_stats is not None and selected_count:
            if (
                reports[0]["last_activity_at"] != selected_stats[5]
                or require_exact_timestamp(
                    str(reports[0]["last_activity_at"]),
                    "latest materialized activity",
                ).epoch_nanoseconds != selected_stats[6]
            ):
                raise ValueError("materialized project stats are incoherent")
            if selected_count <= 10 and (
                reports[-1]["last_activity_at"] != selected_stats[3]
            ):
                raise ValueError("materialized project stats are incoherent")
        selected_task_json: str | None = None
        if selected_task_ref is not None:
            selected_task_row = connection.execute(
                """SELECT task_ref,report_json,last_activity_at,
                          last_activity_epoch_ns,data_revision
                     FROM materialized_report_snapshots
                    WHERE project_id=? AND task_ref=? AND data_revision<=?""",
                (selected_item.project_id, selected_task_ref, revision),
            ).fetchone()
            if selected_task_row is None:
                raise self._unknown()
            selected_task_json = canonical_json(self._materialized_payload(
                selected_task_row,
                sync_freshness,
                expected_revision=expected_revision,
            ))
        summaries: list[DashboardProjectSummary] = []
        state_by_project: dict[str, str] = {}
        for item, report_count, state, last_reconciled_at in prepared:
            state_by_project[item.project_id] = state
            last_activity_at = (
                str(reports[0]["last_activity_at"])
                if item.project_id == selected_item.project_id and reports
                else last_reconciled_at
            )
            summaries.append(DashboardProjectSummary(
                projection[item.project_id],
                self._display_name(item, projection[item.project_id]),
                last_activity_at,
                state,
                NumericFact(report_count, "count", "derived"),
            ))
        project_catalog = DashboardProjectCatalog(tuple(summaries))
        latest = reports[0] if reports else None
        unavailable = self._unavailable
        headline = (
            {
                "working_tokens": latest["deduplicated_tokens"]["working"],
                "full_context_tokens": latest["deduplicated_tokens"]["full_context"],
                "wall_clock_ms": latest["timing"]["wall_clock"],
            }
            if latest
            else {
                "working_tokens": unavailable("tokens").as_dict(),
                "full_context_tokens": unavailable("tokens").as_dict(),
                "wall_clock_ms": unavailable("milliseconds").as_dict(),
            }
        )
        selected_state = state_by_project[selected_item.project_id]
        project = {
            "project_ref": selected_ref,
            "display_name": self._display_name(selected_item, selected_ref),
            "last_activity_at": (
                latest["last_activity_at"]
                if latest
                else selected_item.last_seen_at
            ),
            "freshness_state": selected_state,
            "overview": {
                "basis": {
                    "kind": "latest_task",
                    "task_ref": None if latest is None else latest["task_ref"],
                },
                "headline": headline,
                "phase_allocation": (
                    None if latest is None else latest["semantic"]["breakdown"]
                ),
            },
            "recent_tasks": [
                {
                    "task_ref": report["task_ref"],
                    "display_name": report["display_name"],
                    "status": report["status"],
                    "last_activity_at": report["last_activity_at"],
                    "task_family": report["task_family"],
                    "headline": {
                        "working_tokens": report["deduplicated_tokens"]["working"],
                        "full_context_tokens": report["deduplicated_tokens"]["full_context"],
                        "wall_clock_ms": report["timing"]["wall_clock"],
                    },
                }
                for report in reports
            ],
            "pilot": None,
            "storage": self._bootstrap_storage(),
            "system_health": {
                "scope": "global_launch_context",
                "doctor": self._doctor_report.as_dict(),
            },
        }
        snapshot = DashboardSnapshot(
            generated_at,
            {
                "state": selected_state,
                "doctor": {
                    "scope": "global_launch_context",
                    "report": self._doctor_report.as_dict(),
                },
            },
            project_catalog,
            selected_ref,
            canonical_json(project),
            selected_task_json,
            refresh,
            revision,
        )
        return {selected_ref: snapshot}, None

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
        data_revision: int,
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
            data_revision=data_revision,
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
        data_revision: int = 0,
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
            selected_ref, project_json, selected_task_json, refresh, data_revision,
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
            with _consistent_read(store.connection):
                revision, sync_freshness = self._materialized_state(
                    store.connection,
                )
                item = self._resolve_project(self._catalog(store), project_ref)
                stats = self._project_stats(
                    store.connection, item.project_id, revision,
                )
                if stats is None:
                    raise ValueError("materialized project stats are incoherent")
                report_count = int(stats[0])
                stats_revision = int(stats[7])
                cursor_epoch: int | None = None
                if cursor is not None:
                    cursor_row = store.connection.execute(
                        """SELECT task_ref,report_json,last_activity_at,
                                  last_activity_epoch_ns,data_revision
                             FROM materialized_report_snapshots
                            WHERE project_id=? AND task_ref=?
                              AND data_revision<=?""",
                        (item.project_id, cursor, revision),
                    ).fetchone()
                    if cursor_row is None:
                        raise self._unknown()
                    cursor_payload = self._materialized_payload(
                        cursor_row, sync_freshness,
                        expected_revision=stats_revision,
                    )
                    validate_task_report(cursor_payload)
                    cursor_epoch = int(cursor_row["last_activity_epoch_ns"])
                if cursor is None:
                    rows = store.connection.execute(
                        """SELECT task_ref,report_json,last_activity_at,
                                  last_activity_epoch_ns,data_revision
                             FROM materialized_report_snapshots
                            WHERE project_id=? AND data_revision<=?
                            ORDER BY last_activity_epoch_ns DESC,task_ref
                            LIMIT ?""",
                        (item.project_id, revision, limit + 1),
                    ).fetchall()
                else:
                    assert cursor_epoch is not None
                    rows = store.connection.execute(
                        """SELECT task_ref,report_json,last_activity_at,
                                  last_activity_epoch_ns,data_revision
                             FROM materialized_report_snapshots
                            WHERE project_id=? AND data_revision<=?
                              AND (
                                  last_activity_epoch_ns<?
                                  OR (
                                      last_activity_epoch_ns=?
                                      AND task_ref>?
                                  )
                              )
                            ORDER BY last_activity_epoch_ns DESC,task_ref
                            LIMIT ?""",
                        (
                            item.project_id, revision, cursor_epoch,
                            cursor_epoch, cursor, limit + 1,
                        ),
                    ).fetchall()
                payloads = tuple(
                    self._materialized_payload(
                        row, sync_freshness,
                        expected_revision=stats_revision,
                    )
                    for row in rows
                )
                if cursor is None and len(payloads) != min(
                    report_count, limit + 1,
                ):
                    raise ValueError("materialized project stats are incoherent")
                if cursor is None and payloads and (
                    payloads[0]["last_activity_at"] != stats[5]
                ):
                    raise ValueError("materialized project stats are incoherent")
                has_more = len(payloads) > limit
                if has_more:
                    validate_task_report(payloads[-1])
                selected = payloads[:limit]
                next_cursor = (
                    str(selected[-1]["task_ref"])
                    if selected and has_more
                    else None
                )
                return DashboardTaskPage(
                    generated_at,
                    project_ref,
                    tuple(canonical_json(report) for report in selected),
                    limit,
                    next_cursor,
                )
        finally:
            store.close()

    @staticmethod
    def _materialized_payload(
        row: sqlite3.Row,
        sync_freshness: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        try:
            payload = json.loads(str(row["report_json"]))
        except (TypeError, ValueError) as error:
            raise ValueError("materialized task report is invalid") from error
        if (
            not isinstance(payload, dict)
            or payload.get("task_ref") != str(row["task_ref"])
        ):
            raise ValueError("materialized task report identity is invalid")
        validate_task_report(
            payload, allow_legacy_without_sync_freshness=True,
        )
        payload["sync_freshness"] = dict(
            normalize_sync_freshness(sync_freshness),
        )
        try:
            payload_activity = payload.get("last_activity_at")
            stored_activity = row["last_activity_at"]
            stored_epoch = row["last_activity_epoch_ns"]
            stored_revision = row["data_revision"]
            if (
                not isinstance(payload_activity, str)
                or not isinstance(stored_activity, str)
                or isinstance(stored_epoch, bool)
                or not isinstance(stored_epoch, int)
                or payload_activity != stored_activity
                or (
                    expected_revision is not None
                    and stored_revision != expected_revision
                )
            ):
                raise ValueError
            activity = require_exact_timestamp(
                payload_activity, "materialized report activity",
            )
            indexed_activity = require_exact_timestamp(
                stored_activity, "indexed materialized report activity",
            )
            if (
                activity.epoch_nanoseconds != indexed_activity.epoch_nanoseconds
                or activity.epoch_nanoseconds != stored_epoch
            ):
                raise ValueError
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "materialized task report activity is invalid"
            ) from error
        reject_private_fields(payload)
        return payload

    @classmethod
    def _project_stats(
        cls,
        connection: sqlite3.Connection,
        project_id: str,
        maximum_revision: int,
    ) -> tuple[object, ...] | None:
        row = connection.execute(
            """SELECT report_count,first_reconciled_at,last_reconciled_at,
                      first_activity_at,first_activity_epoch_ns,
                      last_activity_at,last_activity_epoch_ns,
                      data_revision AS stats_data_revision
                 FROM materialized_project_stats
                WHERE project_id=? AND data_revision<=?""",
            (project_id, maximum_revision),
        ).fetchone()
        return None if row is None else cls._validated_project_stats_row(row)

    def compare(
        self,
        project_ref: str,
        left: str,
        right: str,
    ) -> ComparisonReport:
        store = self._store_factory()
        try:
            if left == right:
                raise self._unknown()
            with _consistent_read(store.connection):
                revision = SyncStateRepository(store).data_revision()
                item = self._resolve_project(self._catalog(store), project_ref)
                stats = self._project_stats(
                    store.connection, item.project_id, revision,
                )
                if stats is None:
                    raise ValueError("materialized project stats are incoherent")
                selected_rows = tuple(store.connection.execute(
                    """SELECT task_ref,data_revision
                         FROM materialized_report_snapshots
                        WHERE project_id=? AND task_ref IN (?,?)""",
                    (item.project_id, left, right),
                ))
                if any(row["data_revision"] != stats[7] for row in selected_rows):
                    raise ValueError("materialized project stats are incoherent")
                try:
                    baseline, current = read_materialized_task_reports(
                        store, item.project_id, (left, right),
                    )
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
            # Install the exact RFC3339 SQLite scalar used by durable sync
            # ordering before selecting the newest pilot.
            SyncStateRepository(store)
            with _consistent_read(store.connection):
                item = self._resolve_project(self._catalog(store), project_ref)
                row = store.connection.execute(
                    """SELECT pilot_id FROM pilot_runs WHERE project_id=?
                         ORDER BY hydra_rfc3339_micros(started_at) DESC,
                                  started_at DESC,pilot_id DESC LIMIT 1""",
                    (item.project_id,),
                ).fetchone()
                if row is None:
                    raise self._unknown()
                audit = read_materialized_pilot_audit(
                    store,
                    project_id=item.project_id,
                    pilot_id=str(row[0]),
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
