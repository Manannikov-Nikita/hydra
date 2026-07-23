"""Privacy-safe rendering for project installation lifecycle commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, TextIO

from .project_lifecycle import initialize_project, uninitialize_project
from .status import collect_status


def _write_json(stdout: TextIO, value: Mapping[str, object]) -> None:
    stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def run_project_lifecycle(
    arguments: object,
    *,
    environ: Mapping[str, str],
    stdout: TextIO,
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

    status = collect_status(path, environ=environ)
    if getattr(arguments, "json"):
        _write_json(stdout, status)
        return
    project = status["project"]
    storage = status["storage"]
    installation = status["installation"]
    assert isinstance(project, Mapping)
    assert isinstance(storage, Mapping)
    assert isinstance(installation, Mapping)
    stdout.write(
        "Hydra status\n"
        f"project initialized: {'yes' if project['initialized'] else 'no'}\n"
        f"project identity valid: {_human(project['identity_valid'])}\n"
        f"project schema: {_human(project['config_schema_version'])}\n"
        f"storage present: {'yes' if storage['exists'] else 'no'}\n"
        f"storage schema: {_human(storage['schema_version'])}\n"
        "installation identity: "
        f"{'present' if installation['identity_key_exists'] else 'missing'}\n"
    )


def _human(value: object) -> str:
    if value is None:
        return "unknown"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)
