"""Concrete local command services for the Hydra CLI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

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


Clock = Callable[[], datetime]


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

    def report(
        self,
        last: int,
        output_format: str,
        database_path: Path | None,
        cwd: Path,
    ) -> str:
        from .reconcile_engine import list_reconciled_reports

        project = self._project(cwd)
        store = HydraStore(self._database_path(database_path))
        try:
            reports = list_reconciled_reports(
                store, project.project_id, limit=last,
            )
            return render_report_collection(reports, output_format)
        finally:
            store.close()

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
        store = HydraStore(self._database_path(database_path))
        try:
            baseline = get_reconciled_report(store, project.project_id, left)
            current = get_reconciled_report(store, project.project_id, right)
            return _renderer(output_format)(compare_reports(baseline, current))
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
        store = HydraStore(self._database_path(database_path))
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
