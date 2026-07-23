"""Strict, privacy-safe parsing for the sole project-local Hydra artifact."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import re
import secrets
import tomllib

from .project import normalize_project_display_name


PROJECT_CONFIG_SCHEMA_VERSION = 1
PROJECT_ID_PATTERN = re.compile(r"\Ahprj_[0-9a-f]{16}\Z")
_FIELDS = frozenset({"schema_version", "project_id", "display_name", "telemetry"})


class ProjectConfigError(ValueError):
    """Raised when project configuration is not canonical and trustworthy."""


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int | None
    project_id: str
    display_name: str | None
    telemetry: str | None


def generate_project_id(
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> str:
    """Generate one canonical, opaque project identity."""
    return f"hprj_{random_bytes(8).hex()}"


def _fail() -> ProjectConfigError:
    return ProjectConfigError("invalid Hydra project configuration")


def parse_project_config(raw: bytes, *, source: Path) -> ProjectConfig:
    """Parse one complete project file, rejecting unknown or ambiguous input."""
    _ = source
    try:
        if not isinstance(raw, bytes):
            raise TypeError
        data = tomllib.loads(raw.decode("utf-8"))
    except (TypeError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise _fail() from error
    if set(data) - _FIELDS:
        raise _fail()

    schema_version = data.get("schema_version")
    if (
        schema_version is not None
        and (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != PROJECT_CONFIG_SCHEMA_VERSION
        )
    ):
        raise _fail()

    project_id = data.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise _fail()

    display_name = data.get("display_name")
    try:
        normalized_name = normalize_project_display_name(display_name)
    except ValueError as error:
        raise _fail() from error

    telemetry = data.get("telemetry")
    if telemetry is not None and telemetry != "hybrid":
        raise _fail()

    return ProjectConfig(
        schema_version=schema_version,
        project_id=project_id,
        display_name=normalized_name,
        telemetry=telemetry,
    )


def render_project_config(config: ProjectConfig) -> bytes:
    """Render a validated project configuration in one deterministic form."""
    if not isinstance(config, ProjectConfig):
        raise _fail()
    lines: list[str] = []
    if config.schema_version is not None:
        lines.append(f"schema_version = {config.schema_version}")
    lines.append(f"project_id = {json.dumps(config.project_id, ensure_ascii=True)}")
    if config.display_name is not None:
        lines.append(
            f"display_name = {json.dumps(config.display_name, ensure_ascii=True)}",
        )
    if config.telemetry is not None:
        lines.append(f"telemetry = {json.dumps(config.telemetry)}")
    rendered = ("\n".join(lines) + "\n").encode("utf-8")
    parsed = parse_project_config(rendered, source=Path("project.toml"))
    if parsed != config:
        raise _fail()
    return rendered


def read_project_config(path: Path) -> ProjectConfig:
    """Read and strictly parse one project configuration."""
    try:
        with path.open("rb") as config_file:
            raw = config_file.read()
    except OSError:
        raise
    return parse_project_config(raw, source=path)
