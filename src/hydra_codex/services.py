"""Concrete local command services for the Hydra CLI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
import secrets

from .annotation_core import annotate_with_capability, finish_turn
from .annotation_persistence import binding_for_capability
from .annotation_types import CapabilityRejected, TrustedAnnotationContext, capability_digest
from .contracts import AnnotationKind, ModelAnnotationInput
from .project import ProjectResolution, resolve_project
from .report_operations import compare_reports
from .report_renderers import (
    render_html,
    render_json,
    render_markdown,
    render_report_collection,
)
from .rollout_identity import Pseudonymizer
from .storage import HydraStore, default_database_path


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
    return default_database_path(home).parent / "rollout-hmac.key"


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
        project = self._project(cwd)
        keys = self._keys()
        store = HydraStore(self._database_path(database_path))
        try:
            with store.rollout_transaction() as connection:
                observed_at = _utc_now(self._clock)
                binding = binding_for_capability(
                    connection, capability_digest(keys, capability),
                )
                if binding["project_id"] != project.project_id:
                    raise CapabilityRejected(
                        "annotation capability belongs to another project"
                    )
                context = TrustedAnnotationContext(
                    request_key="cli-v1-" + secrets.token_urlsafe(24),
                    sequence=int(binding["last_sequence"]) + 1,
                    observed_at=observed_at,
                )
                payload = _payload(annotation)
                if annotation.kind is AnnotationKind.FINISH:
                    return finish_turn(store, keys, capability, context, payload)
                return annotate_with_capability(
                    store, keys, capability, context, payload,
                )
        finally:
            store.close()

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
