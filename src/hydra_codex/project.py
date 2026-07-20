"""Project identity discovery for telemetry observations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ProjectNotFound(FileNotFoundError):
    """Raised when no Hydra project configuration is reachable from a cwd."""


@dataclass(frozen=True)
class ProjectResolution:
    project_id: str
    project_root: Path
    worktree_path: Path


def resolve_project(cwd: Path | str) -> ProjectResolution:
    """Find `.hydra/project.toml` from *cwd* upward without relying on Git state."""
    current = Path(cwd).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        config_path = directory / ".hydra" / "project.toml"
        if config_path.is_file():
            with config_path.open("rb") as config_file:
                data = tomllib.load(config_file)
            project_id = data.get("project_id")
            if not isinstance(project_id, str) or not project_id.strip():
                raise ValueError(f"{config_path} must contain a non-empty project_id")
            return ProjectResolution(project_id, directory, current.relative_to(directory))
    raise ProjectNotFound(f"no .hydra/project.toml found from {cwd}")
