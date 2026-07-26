"""Project identity discovery for telemetry observations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
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
    display_name_provenance: str | None = None


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


def _basename_display_name(directory: Path) -> str | None:
    """Use a validated basename only for legacy projects with no configured name."""
    try:
        return normalize_project_display_name(directory.name)
    except ValueError:
        return None


def resolve_project(cwd: Path | str) -> ProjectResolution:
    """Find `.hydra/project.toml` from *cwd* upward without relying on Git state."""
    current = Path(cwd).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        config_path = directory / ".hydra" / "project.toml"
        try:
            metadata = config_path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        from .project_config import (
            ProjectConfig,
            ProjectConfigError,
            _read_project_config_bytes,
            parse_project_config,
        )
        if not stat.S_ISREG(metadata.st_mode):
            raise ProjectConfigError("invalid Hydra project configuration")

        raw = _read_project_config_bytes(config_path)
        try:
            config = parse_project_config(raw, source=config_path)
        except ProjectConfigError as strict_error:
            # Older internal Hydra builds accepted opaque, non-canonical IDs.
            # Keep those schema-less files usable by existing commands while
            # init/status/uninit enforce the public canonical contract.
            try:
                legacy = tomllib.loads(raw.decode("utf-8"))
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
        display_name = config.display_name or _basename_display_name(directory)
        return ProjectResolution(
            config.project_id,
            directory,
            current.relative_to(directory),
            display_name,
            "config" if config.display_name is not None else "repo_basename" if display_name else None,
        )
    raise ProjectNotFound(f"no .hydra/project.toml found from {cwd}")
