"""One-shot canonical audit orchestration over public reconciled models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
from pathlib import Path

from .audit_builder import StorageHealthSnapshot, build_audit
from .audit_model import AuditReport
from .audit_renderers import (
    render_audit_html,
    render_audit_json,
    render_audit_markdown,
)
from .pilot import pilot_status, read_only_pilot_statuses, read_pilot_status
from .project import resolve_project
from .reconcile_engine import list_reconciled_reports, reconcile_project
from .rollout import ingest_rollouts
from .rollout_identity import Pseudonymizer, RolloutRoot
from .storage import HydraStore
from .storage_health import current_storage_health, record_audit_snapshot


def build_pilot_audit(
    store: HydraStore,
    *,
    project_id: str,
    pilot_id: str,
    refresh_enrollment: bool = True,
) -> AuditReport:
    """Build from PilotStatus and TaskReport only, never raw semantic content."""
    return _build_pilot_audit_with_health(
        store, project_id=project_id, pilot_id=pilot_id,
        refresh_enrollment=refresh_enrollment,
    )[0]


def _build_pilot_audit_with_health(
    store: HydraStore,
    *,
    project_id: str,
    pilot_id: str,
    refresh_enrollment: bool = True,
) -> tuple[AuditReport, StorageHealthSnapshot]:
    def build() -> tuple[AuditReport, StorageHealthSnapshot]:
        with store.rollout_transaction():
            status_builder = pilot_status if refresh_enrollment else read_pilot_status
            status = status_builder(store, project_id, pilot_id)
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
                raise ValueError(
                    "pilot task collection lacks a reconciled public report"
                ) from error
            health = current_storage_health(store, project_id)
            return build_audit(status, reports, health), health

    if refresh_enrollment:
        return build()
    with read_only_pilot_statuses():
        return build()


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
    observed_at: datetime,
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
        audit, health = _build_pilot_audit_with_health(
            store,
            project_id=project.project_id,
            pilot_id=pilot_id,
        )
        rendered = render_pilot_audit(audit, output_format)
        canonical_json = render_pilot_audit(audit, "json")
        record_audit_snapshot(
            store,
            project_id=project.project_id,
            audit_sha256=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
            observed_at=observed_at,
            health=health,
        )
        return rendered
    finally:
        store.close()
