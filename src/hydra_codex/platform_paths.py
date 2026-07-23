"""Platform-specific locations for Hydra's private runtime state."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys


def default_data_directory(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Return Hydra's private data directory for a supported platform."""
    values = dict(os.environ if environ is None else environ)
    resolved_home = Path.home() if home is None else Path(home)
    current_platform = sys.platform if platform is None else platform
    if current_platform == "darwin":
        return resolved_home / "Library/Application Support/Hydra"
    if current_platform.startswith("linux"):
        candidate = values.get("XDG_DATA_HOME")
        base = (
            Path(candidate)
            if candidate and Path(candidate).is_absolute()
            else resolved_home / ".local/share"
        )
        return base / "hydra"
    raise RuntimeError(f"unsupported platform: {current_platform}")


def default_database_path(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Return the default SQLite database path for a supported platform."""
    return default_data_directory(home, environ=environ, platform=platform) / "hydra.sqlite3"


def default_installation_key_path(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Return the default installation identity key path for a supported platform."""
    return default_data_directory(home, environ=environ, platform=platform) / "rollout-hmac.key"
