"""Read-only, extensible Hydra installation and project status aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3

from .platform_paths import default_database_path, default_installation_key_path
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


def collect_status(
    path: Path | str = ".",
    *,
    environ: Mapping[str, str],
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
    }
