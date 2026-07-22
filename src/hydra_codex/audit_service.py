"""One-shot canonical audit orchestration over public reconciled models."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .audit_builder import StorageHealthSnapshot, build_audit
from .audit_model import AuditReport
from .audit_renderers import (
    render_audit_html,
    render_audit_json,
    render_audit_markdown,
)
from .pilot import pilot_status
from .project import resolve_project
from .reconcile_engine import list_reconciled_reports, reconcile_project
from .rollout import ingest_rollouts
from .rollout_identity import Pseudonymizer, RolloutRoot
from .storage import HydraStore


def current_storage_health(
    store: HydraStore,
    project_id: str,
) -> StorageHealthSnapshot:
    """Read exact current sizes and project-scoped counts without maintenance."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be non-empty")
    database_path = store.database_path
    wal_path = Path(str(database_path) + "-wal")

    def count(query: str) -> int:
        return int(store.connection.execute(query, (project_id,)).fetchone()[0])

    return StorageHealthSnapshot(
        database_bytes=database_path.stat().st_size,
        wal_bytes=wal_path.stat().st_size if wal_path.is_file() else 0,
        rollout_sources=count(
            """SELECT COUNT(DISTINCT r.source_digest)
                 FROM rollout_sources r
                 JOIN rollout_logical_sources l
                   ON l.logical_source_key=r.logical_source_key
                WHERE l.project_id=?"""
        ),
        rollout_events=count(
            """SELECT COUNT(DISTINCT e.event_key)
                 FROM rollout_events e
                 JOIN rollout_logical_sources l
                   ON l.logical_source_key=e.logical_source_key
                WHERE l.project_id=?"""
        ),
        codex_event_sources=count(
            "SELECT COUNT(*) FROM codex_event_sources WHERE project_id=?"
        ),
        codex_events=count(
            "SELECT COUNT(*) FROM codex_events WHERE project_id=?"
        ),
        schema_version=store.schema_version(),
    )


def build_pilot_audit(
    store: HydraStore,
    *,
    project_id: str,
    pilot_id: str,
) -> AuditReport:
    """Build from PilotStatus and TaskReport only, never raw semantic content."""
    status = pilot_status(store, project_id, pilot_id)
    task_refs = tuple(
        str(item["task_ref"])
        for item in status.as_dict()["tasks"]
    )
    reports_by_ref = {
        report.task_ref: report
        for report in list_reconciled_reports(store, project_id)
    }
    try:
        reports = tuple(reports_by_ref[task_ref] for task_ref in task_refs)
    except KeyError as error:
        raise ValueError("pilot task collection lacks a reconciled public report") from error
    return build_audit(
        status,
        reports,
        current_storage_health(store, project_id),
    )


def render_pilot_audit(audit: AuditReport, output_format: str) -> str:
    renderers = {
        "json": render_audit_json,
        "markdown": render_audit_markdown,
        "html": render_audit_html,
    }
    try:
        renderer = renderers[output_format]
    except KeyError as error:
        raise ValueError("unsupported audit format") from error
    return renderer(audit)


def _default_rollout_roots(environ: Mapping[str, str]) -> tuple[RolloutRoot, ...]:
    home_value = environ.get("HOME")
    home = (
        Path(home_value).expanduser()
        if isinstance(home_value, str) and home_value
        else Path.home()
    )
    candidates = (
        (home / ".codex" / "sessions", "active"),
        (home / ".codex" / "archived_sessions", "archived"),
    )
    return tuple(
        RolloutRoot(path, label)
        for path, label in candidates
        if path.is_dir()
    )


def generate_audit(
    *,
    environ: Mapping[str, str],
    database_path: Path | None,
    installation_key_path: Path,
    cwd: Path,
    pilot_id: str,
    output_format: str,
) -> str:
    """Ingest, reconcile, validate, build, and render one audit.

    This bare CLI/MCP path deliberately cannot drain the annotation spool: it
    has no host-attested session or turn binding. The audit records that limit.
    """
    project = resolve_project(cwd)
    keys = Pseudonymizer.installation_key(installation_key_path)
    store = HydraStore(database_path)
    try:
        ingest_rollouts(
            store,
            _default_rollout_roots(environ),
            project.project_root,
            project.project_id,
            hash_key=keys.key,
        )
        reconcile_project(store, project.project_id, keys.key)
        return render_pilot_audit(
            build_pilot_audit(
                store,
                project_id=project.project_id,
                pilot_id=pilot_id,
            ),
            output_format,
        )
    finally:
        store.close()
