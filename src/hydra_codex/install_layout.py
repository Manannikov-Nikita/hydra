"""Validate supported platforms and standalone Hydra release layouts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
from typing import Mapping


SUPPORTED_TARGETS = (
    "darwin-arm64",
    "darwin-x86_64",
    "linux-x86_64",
)

_MARKER_MAX_BYTES = 256
_MANIFEST_MAX_BYTES = 64 * 1024
CANONICAL_PLUGIN_FILES = (
    Path(".codex-plugin/plugin.json"),
    Path(".mcp.json"),
    Path("README.md"),
    Path("hooks/hooks.json"),
    Path("skills/hydra-report/SKILL.md"),
    Path("skills/hydra-report/agents/openai.yaml"),
    Path("skills/hydra-report/references/report-schema.md"),
)


class UnsupportedTarget(ValueError):
    """Raised when the current platform has no public Hydra bundle."""


class InvalidBundle(ValueError):
    """Raised when a standalone release is incomplete or inconsistent."""


@dataclass(frozen=True)
class BundleLayout:
    root: Path
    version: str
    target: str
    executable: Path
    marketplace: Path


def platform_target(system: str, machine: str) -> str:
    """Map an operating-system and machine pair to the release allowlist."""
    targets = {
        ("darwin", "arm64"): "darwin-arm64",
        ("darwin", "x86_64"): "darwin-x86_64",
        ("linux", "x86_64"): "linux-x86_64",
        ("linux", "amd64"): "linux-x86_64",
    }
    try:
        return targets[(system.lower(), machine.lower())]
    except (AttributeError, KeyError) as error:
        raise UnsupportedTarget(f"unsupported target: {system}/{machine}") from error


def frozen_bundle_root(executable: Path | None = None) -> Path | None:
    """Locate the public release root from a frozen executable path."""
    if executable is None:
        if not bool(getattr(sys, "frozen", False)):
            return None
        selected = Path(sys.executable)
    else:
        selected = Path(executable)
    if selected.name != "hydra-codex":
        return None
    if selected.parent.name == "bin":
        return selected.parent.parent
    if selected.parent.parent.name == "runtime":
        return selected.parents[2]
    return None


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _read_small_text(path: Path, *, limit: int) -> str:
    if not _regular_file(path):
        raise InvalidBundle("bundle is incomplete")
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
    except OSError as error:
        raise InvalidBundle("bundle is unreadable") from error
    if len(content) > limit or b"\0" in content:
        raise InvalidBundle("bundle metadata is invalid")
    try:
        value = content.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise InvalidBundle("bundle metadata is invalid") from error
    if not value or "\n" in value or "\r" in value:
        raise InvalidBundle("bundle metadata is invalid")
    return value


def _read_manifest(path: Path) -> Mapping[str, object]:
    if not _regular_file(path):
        raise InvalidBundle("bundle manifest is invalid")
    try:
        with path.open("rb") as stream:
            content = stream.read(_MANIFEST_MAX_BYTES + 1)
    except OSError as error:
        raise InvalidBundle("bundle manifest is invalid") from error
    if len(content) > _MANIFEST_MAX_BYTES or b"\0" in content:
        raise InvalidBundle("bundle manifest is invalid")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidBundle("bundle manifest is invalid") from error
    if not isinstance(value, Mapping):
        raise InvalidBundle("bundle manifest is invalid")
    return value


def validate_plugin_bundle(
    plugin: Path,
    *,
    expected_version: str | None = None,
) -> Path:
    """Require the complete canonical plugin inventory and manifest identity."""
    try:
        mode = plugin.lstat().st_mode
    except OSError as error:
        raise InvalidBundle("bundle plugin is unavailable") from error
    if not stat.S_ISDIR(mode) or not all(
        _regular_file(plugin / relative) for relative in CANONICAL_PLUGIN_FILES
    ):
        raise InvalidBundle("bundle plugin is incomplete")
    manifest = _read_manifest(plugin / ".codex-plugin" / "plugin.json")
    if manifest.get("name") != "hydra-codex":
        raise InvalidBundle("bundle plugin manifest is invalid")
    if (
        expected_version is not None
        and manifest.get("version") != expected_version
    ):
        raise InvalidBundle("bundle plugin version does not match")
    return plugin


def validate_marketplace(
    marketplace: Path,
    *,
    expected_version: str | None = None,
) -> Path:
    """Require the canonical marketplace manifest and complete plugin."""
    try:
        mode = marketplace.lstat().st_mode
    except OSError as error:
        raise InvalidBundle("bundle marketplace is unavailable") from error
    if not stat.S_ISDIR(mode):
        raise InvalidBundle("bundle marketplace is unavailable")

    inventory = _read_manifest(
        marketplace / ".agents" / "plugins" / "marketplace.json",
    )
    if inventory.get("name") != "hydra":
        raise InvalidBundle("bundle marketplace is invalid")
    plugins = inventory.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1:
        raise InvalidBundle("bundle marketplace is invalid")
    record = plugins[0]
    if not isinstance(record, Mapping) or record.get("name") != "hydra-codex":
        raise InvalidBundle("bundle marketplace is invalid")
    source = record.get("source")
    if not isinstance(source, Mapping) or source != {
        "source": "local",
        "path": "./plugins/hydra-codex",
    }:
        raise InvalidBundle("bundle marketplace is invalid")

    plugin = marketplace / "plugins" / "hydra-codex"
    validate_plugin_bundle(plugin, expected_version=expected_version)
    return marketplace


def validate_bundle(
    root: Path,
    *,
    expected_version: str | None = None,
    expected_target: str | None = None,
) -> BundleLayout:
    """Validate immutable public release metadata and bundled marketplace."""
    candidate = Path(root).expanduser()
    try:
        root_mode = candidate.lstat().st_mode
    except OSError as error:
        raise InvalidBundle("bundle root is unavailable") from error
    if not stat.S_ISDIR(root_mode):
        raise InvalidBundle("bundle root is unavailable")
    candidate = candidate.absolute()

    version = _read_small_text(candidate / "VERSION", limit=_MARKER_MAX_BYTES)
    target = _read_small_text(candidate / "TARGET", limit=_MARKER_MAX_BYTES)
    if target not in SUPPORTED_TARGETS:
        raise InvalidBundle("bundle target is unsupported")
    if expected_version is not None and version != expected_version:
        raise InvalidBundle("bundle version does not match")
    if expected_target is not None and target != expected_target:
        raise InvalidBundle("bundle target does not match")
    if not _regular_file(candidate / "LICENSE"):
        raise InvalidBundle("bundle license is unavailable")

    executable = candidate / "bin" / "hydra-codex"
    if not _regular_file(executable):
        raise InvalidBundle("bundle executable is unavailable")
    try:
        executable_mode = executable.lstat().st_mode
    except OSError as error:
        raise InvalidBundle("bundle executable is unavailable") from error
    if executable_mode & 0o111 == 0 or not os.access(executable, os.X_OK):
        raise InvalidBundle("bundle executable is not executable")

    marketplace = candidate / "marketplace"
    validate_marketplace(marketplace, expected_version=version)
    return BundleLayout(candidate, version, target, executable, marketplace)
