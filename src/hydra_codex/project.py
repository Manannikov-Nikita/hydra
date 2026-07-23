"""Project identity discovery for telemetry observations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
import unicodedata


class ProjectNotFound(FileNotFoundError):
    """Raised when no Hydra project configuration is reachable from a cwd."""


@dataclass(frozen=True)
class ProjectResolution:
    project_id: str
    project_root: Path
    worktree_path: Path
    display_name: str | None = None


def normalize_project_display_name(value: object) -> str | None:
    """Return the canonical safe presentation name used by project config."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("display_name must be text")
    normalized = unicodedata.normalize("NFC", value)
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            raise ValueError("display_name contains unsafe characters")
        if unicodedata.bidirectional(character) in {
            "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI",
        }:
            raise ValueError("display_name contains unsafe characters")
    collapsed = " ".join(normalized.split())
    if not 1 <= len(collapsed) <= 80:
        raise ValueError("display_name must contain 1 to 80 characters")
    return collapsed


def _trusted_display_name(value: object, config_path: Path) -> str | None:
    try:
        return normalize_project_display_name(value)
    except ValueError as error:
        raise ValueError(f"{config_path} {error}") from error


def resolve_project(cwd: Path | str) -> ProjectResolution:
    """Find `.hydra/project.toml` from *cwd* upward without relying on Git state."""
    current = Path(cwd).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        config_path = directory / ".hydra" / "project.toml"
        if config_path.is_file():
            from .project_config import (
                ProjectConfig,
                ProjectConfigError,
                read_project_config,
            )

            try:
                config = read_project_config(config_path)
            except ProjectConfigError as strict_error:
                # Older internal Hydra builds accepted opaque, non-canonical IDs.
                # Keep those schema-less files usable by existing commands while
                # init/status/uninit enforce the public canonical contract.
                try:
                    with config_path.open("rb") as config_file:
                        legacy = tomllib.load(config_file)
                    if (
                        set(legacy) - {"project_id", "display_name", "telemetry"}
                        or "schema_version" in legacy
                    ):
                        raise strict_error
                    project_id = legacy.get("project_id")
                    telemetry = legacy.get("telemetry")
                    if (
                        not isinstance(project_id, str)
                        or not project_id.strip()
                        or telemetry not in {None, "hybrid"}
                    ):
                        raise strict_error
                    config = ProjectConfig(
                        None,
                        project_id,
                        _trusted_display_name(
                            legacy.get("display_name"),
                            Path("project.toml"),
                        ),
                        telemetry,
                    )
                except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
                    raise strict_error from None
            return ProjectResolution(
                config.project_id,
                directory,
                current.relative_to(directory),
                config.display_name,
            )
    raise ProjectNotFound(f"no .hydra/project.toml found from {cwd}")
