"""Read-only, extensible Hydra installation and project status aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3

from . import __version__
from .codex_integration import (
    CodexClient,
    CodexCommandClient,
    IncompatibleCodexError,
    inspect_codex,
)
from .platform_paths import default_database_path, default_installation_key_path
from .plugin_bundle import PluginBundleUnavailable, marketplace_root_path
from .project_config import ProjectConfigError, read_project_config


@dataclass(frozen=True)
class ProjectStatus:
    initialized: bool
    identity_valid: bool | None
    config_schema_version: int | None
    project_root: Path | None = field(repr=False)


def _starting_directory(path: Path | str) -> Path:
    candidate = Path(path).expanduser().resolve()
    return candidate.parent if candidate.is_file() else candidate


def _project_status(path: Path | str) -> ProjectStatus:
    start = _starting_directory(path)
    for directory in (start, *start.parents):
        hydra = directory / ".hydra"
        config_path = hydra / "project.toml"
        if hydra.is_symlink():
            raise ProjectConfigError("invalid Hydra project configuration")
        try:
            config_path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        else:
            config = read_project_config(config_path)
            return ProjectStatus(True, True, config.schema_version, directory)
    return ProjectStatus(False, None, None, None)


def _home(environ: Mapping[str, str]) -> Path:
    value = environ.get("HOME")
    return Path(value).expanduser() if isinstance(value, str) and value else Path.home()


def _database_schema(path: Path) -> int | None:
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations",
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()
    value = None if row is None else row[0]
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _unavailable_codex(*, available: bool) -> dict[str, object]:
    return {
        "available": available,
        "compatible": False,
        "marketplace_installed": None,
        "plugin_installed": None,
        "plugin_version": None,
        "version_matches": None,
        "new_task_required": False,
        "next_actions": [
            "Install or update Codex with plugin marketplace support.",
        ],
    }


def _codex_status(
    *,
    environ: Mapping[str, str],
    client: CodexClient | None,
    marketplace_root: Path | None,
) -> dict[str, object]:
    try:
        adapter = CodexCommandClient(environ=environ) if client is None else client
    except IncompatibleCodexError:
        return _unavailable_codex(available=False)
    try:
        adapter.version()
    except Exception:
        return _unavailable_codex(available=False)
    try:
        desired_source = (
            marketplace_root_path()
            if marketplace_root is None
            else Path(marketplace_root).expanduser().resolve()
        )
    except (OSError, PluginBundleUnavailable):
        desired_source = None
    try:
        state = inspect_codex(adapter)
    except Exception:
        return _unavailable_codex(available=True)

    marketplace_installed = state.marketplace is not None
    plugin_installed = bool(state.plugin is not None and state.plugin.installed)
    plugin_version = (
        state.plugin.version if plugin_installed and state.plugin is not None else None
    )
    version_matches = (
        plugin_version == __version__ if plugin_version is not None else None
    )
    owned_source = bool(
        state.marketplace is not None
        and desired_source is not None
        and state.marketplace.source == desired_source
    )
    if marketplace_installed and not owned_source:
        actions = ["Resolve the existing Hydra marketplace ownership before installing."]
    elif not marketplace_installed or not plugin_installed:
        actions = ["Run hydra-codex install -y."]
    elif not version_matches:
        actions = ["Run hydra-codex install -y --refresh."]
    else:
        actions = ["Start a new Codex task to load Hydra."]
    return {
        "available": True,
        "compatible": True,
        "marketplace_installed": marketplace_installed,
        "plugin_installed": plugin_installed,
        "plugin_version": plugin_version,
        "version_matches": version_matches,
        "new_task_required": bool(plugin_installed and version_matches),
        "next_actions": actions,
    }


def collect_status(
    path: Path | str = ".",
    *,
    environ: Mapping[str, str],
    codex_client: CodexClient | None = None,
    marketplace_root: Path | None = None,
) -> dict[str, object]:
    """Collect privacy-safe state without creating files or opening writable SQLite."""
    project = _project_status(path)
    home = _home(environ)
    database = default_database_path(home, environ=environ)
    installation_key = default_installation_key_path(home, environ=environ)
    return {
        "project": {
            "initialized": project.initialized,
            "identity_valid": project.identity_valid,
            "config_schema_version": project.config_schema_version,
        },
        "storage": {
            "exists": database.is_file(),
            "schema_version": _database_schema(database),
        },
        "installation": {
            "identity_key_exists": installation_key.is_file(),
        },
        "codex": _codex_status(
            environ=environ,
            client=codex_client,
            marketplace_root=marketplace_root,
        ),
    }
