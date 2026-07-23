"""Privacy-safe rendering for project and Codex installation commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, TextIO

from . import __version__
from .codex_integration import (
    CodexClient,
    CodexCommandClient,
    configure_codex,
    remove_codex_integration,
    render_codex_config,
)
from .platform_paths import default_data_directory
from .plugin_bundle import marketplace_root_path
from .project_lifecycle import initialize_project, uninitialize_project
from .release_management import (
    default_install_roots,
    uninstall as uninstall_release,
    upgrade as upgrade_release,
)
from .status import collect_status


class ConfirmationRequired(RuntimeError):
    """Raised when a mutating installation command lacks operator approval."""


def _write_json(stdout: TextIO, value: Mapping[str, object]) -> None:
    stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def run_project_lifecycle(
    arguments: object,
    *,
    environ: Mapping[str, str],
    stdout: TextIO,
    codex_client: CodexClient | None = None,
    marketplace_root: Path | None = None,
) -> None:
    """Execute one parsed lifecycle command without constructing app services."""
    command = getattr(arguments, "command")
    path = getattr(arguments, "path")
    home_value = environ.get("HOME")
    home = (
        Path(home_value).expanduser()
        if isinstance(home_value, str) and home_value
        else Path.home()
    )
    if command == "init":
        result = initialize_project(
            path,
            name=getattr(arguments, "name"),
            home=home,
        )
        _write_json(stdout, {
            "changed": result.changed,
            "command": "init",
            "project_id": result.project_id,
            "status": "ok",
        })
        return
    if command == "uninit":
        result = uninitialize_project(
            path,
            confirmation=getattr(arguments, "confirmation"),
            home=home,
        )
        _write_json(stdout, {
            "changed": result.changed,
            "command": "uninit",
            "project_id": result.project_id,
            "status": "ok",
        })
        return

    try:
        status_client = (
            CodexCommandClient(environ=environ)
            if codex_client is None
            else codex_client
        )
    except Exception:
        status_client = None
    status = collect_status(
        path,
        environ=environ,
        codex_client=status_client,
        marketplace_root=marketplace_root,
    )
    if getattr(arguments, "json"):
        _write_json(stdout, status)
        return
    project = status["project"]
    storage = status["storage"]
    installation = status["installation"]
    codex = status["codex"]
    assert isinstance(project, Mapping)
    assert isinstance(storage, Mapping)
    assert isinstance(installation, Mapping)
    assert isinstance(codex, Mapping)
    stdout.write(
        "Hydra status\n"
        f"project initialized: {'yes' if project['initialized'] else 'no'}\n"
        f"project identity valid: {_human(project['identity_valid'])}\n"
        f"project schema: {_human(project['config_schema_version'])}\n"
        f"storage present: {'yes' if storage['exists'] else 'no'}\n"
        f"storage schema: {_human(storage['schema_version'])}\n"
        "installation identity: "
        f"{'present' if installation['identity_key_exists'] else 'missing'}\n"
        f"CLI state: {_human(installation['cli_state'])}\n"
        f"active CLI version: {_human(installation['active_version'])}\n"
        f"active CLI target: {_human(installation['active_target'])}\n"
        f"Codex available: {'yes' if codex['available'] else 'no'}\n"
        f"Codex plugin compatible: {_human(codex['compatible'])}\n"
        f"Hydra plugin installed: {_human(codex['plugin_installed'])}\n"
        f"Hydra plugin version: {_human(codex['plugin_version'])}\n"
        f"Hydra runtime parity: {_human(codex['version_matches'])}\n"
        f"new Codex task required: {'yes' if codex['new_task_required'] else 'no'}\n"
    )
    actions = codex["next_actions"]
    if isinstance(actions, list):
        for action in actions:
            stdout.write(f"next action: {action}\n")


def _home(environ: Mapping[str, str]) -> Path:
    home_value = environ.get("HOME")
    return (
        Path(home_value).expanduser()
        if isinstance(home_value, str) and home_value
        else Path.home()
    )


def _confirmed(arguments: object, stdin: TextIO, stderr: TextIO, prompt: str) -> None:
    if bool(getattr(arguments, "yes", False)):
        return
    try:
        interactive = stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if not interactive:
        raise ConfirmationRequired("confirmation required")
    stderr.write(prompt)
    stderr.flush()
    if stdin.readline().strip().lower() not in {"y", "yes"}:
        raise ConfirmationRequired("confirmation required")


def _receipt_target(
    environ: Mapping[str, str],
    receipt_path: Path | None,
) -> Path:
    return (
        default_data_directory(_home(environ), environ=environ)
        / "codex-integration.json"
        if receipt_path is None
        else Path(receipt_path).expanduser()
    )


def run_release_lifecycle(
    arguments: object,
    *,
    environ: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    client: CodexClient | None = None,
    receipt_path: Path | None = None,
    verified_candidate: object | None = None,
) -> None:
    """Execute upgrade or uninstall through the owned local release tree."""
    from .install_layout import BundleLayout

    command = getattr(arguments, "command")
    roots = default_install_roots(_home(environ))
    if command == "upgrade":
        candidate = (
            verified_candidate
            if isinstance(verified_candidate, BundleLayout)
            else None
        )

        def refresh(layout: BundleLayout) -> None:
            adapter = CodexCommandClient(environ=environ) if client is None else client
            configure_codex(
                client=adapter,
                marketplace_root=layout.marketplace,
                runtime_version=layout.version,
                receipt_path=_receipt_target(environ, receipt_path),
                refresh=True,
            )

        status = upgrade_release(
            check=bool(getattr(arguments, "check")),
            environ=environ,
            stdout=stdout,
            verified_candidate=candidate,
            refresh_integration=None if bool(getattr(arguments, "check")) else refresh,
            roots=roots,
        )
        _write_json(stdout, {
            "command": "upgrade",
            "current_version": status.current_version,
            "latest_version": status.latest_version,
            "status": "ok",
            "update_available": status.update_available,
        })
        return

    _confirmed(
        arguments,
        stdin,
        stderr,
        "Remove Hydra from Codex? [y/N] ",
    )
    adapter = CodexCommandClient(environ=environ) if client is None else client
    detached = None

    def detach() -> None:
        nonlocal detached
        detached = remove_codex_integration(
            client=adapter,
            receipt_path=_receipt_target(environ, receipt_path),
        )

    keep_cli = bool(getattr(arguments, "keep_cli"))
    uninstall_release(
        keep_cli=keep_cli,
        environ=environ,
        detach_integration=detach,
        roots=roots,
    )
    assert detached is not None
    _write_json(stdout, {
        "changed": detached.changed,
        "command": "uninstall",
        "keep_cli": keep_cli,
        "marketplace": detached.marketplace,
        "new_task_required": False,
        "runtime_version": detached.runtime_version,
        "selector": detached.selector,
        "status": "ok",
    })


def run_codex_integration(
    arguments: object,
    *,
    environ: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    client: CodexClient | None = None,
    receipt_path: Path | None = None,
    marketplace_root: Path | None = None,
) -> None:
    """Execute one parsed Codex integration command."""
    command = getattr(arguments, "command")
    root = None
    if command == "install":
        root = (
            marketplace_root_path()
            if marketplace_root is None
            else Path(marketplace_root).expanduser().resolve()
        )
    if command == "install" and getattr(arguments, "print_config") == "codex":
        assert root is not None
        stdout.write(
            render_codex_config(
                marketplace_root=root,
                runtime_version=__version__,
            ),
        )
        return

    _confirmed(
        arguments,
        stdin,
        stderr,
        (
            "Install Hydra into Codex? [y/N] "
            if command == "install"
            else "Remove Hydra from Codex? [y/N] "
        ),
    )
    adapter = CodexCommandClient(environ=environ) if client is None else client
    target = _receipt_target(environ, receipt_path)
    if command == "install":
        assert root is not None
        report = configure_codex(
            client=adapter,
            marketplace_root=root,
            runtime_version=__version__,
            receipt_path=target,
            refresh=bool(getattr(arguments, "refresh")),
        )
        rendered_command = "install"
    else:
        report = remove_codex_integration(
            client=adapter,
            receipt_path=target,
        )
        rendered_command = "uninstall"
    _write_json(stdout, {
        "changed": report.changed,
        "command": rendered_command,
        "marketplace": report.marketplace,
        "new_task_required": bool(
            rendered_command == "install" and report.changed
        ),
        "runtime_version": report.runtime_version,
        "selector": report.selector,
        "status": "ok",
    })


def _human(value: object) -> str:
    if value is None:
        return "unknown"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)
