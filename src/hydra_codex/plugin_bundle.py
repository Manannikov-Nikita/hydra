"""Locate or materialize the canonical Hydra Codex plugin bundle."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import shutil
import sys
from typing import Iterator, Sequence, TextIO


_DISTRIBUTION_NAME = "hydra-codex"
_MANIFEST_SUFFIX = (".codex-plugin", "plugin.json")
_REQUIRED_FILES = (
    Path(".codex-plugin/plugin.json"),
    Path(".mcp.json"),
    Path("README.md"),
    Path("hooks/hooks.json"),
    Path("skills/hydra-report/SKILL.md"),
    Path("skills/hydra-report/agents/openai.yaml"),
    Path("skills/hydra-report/references/report-schema.md"),
)


class PluginBundleUnavailable(FileNotFoundError):
    """Raised when the installed distribution has no complete plugin bundle."""


def _is_complete_bundle(candidate: Path) -> bool:
    return candidate.is_dir() and all(
        (candidate / relative).is_file() for relative in _REQUIRED_FILES
    )


def _distribution_candidates() -> Iterator[Path]:
    try:
        distribution = metadata.distribution(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return
    for entry in distribution.files or ():
        if tuple(entry.parts[-2:]) != _MANIFEST_SUFFIX:
            continue
        manifest = Path(distribution.locate_file(entry))
        yield manifest.parent.parent


def _checkout_candidate() -> Path:
    return Path(__file__).resolve().parents[2] / "plugins" / "hydra-codex"


def plugin_bundle_path() -> Path:
    """Return the complete installed bundle, or the canonical checkout bundle."""
    candidates = (_checkout_candidate(), *_distribution_candidates())
    for candidate in candidates:
        resolved = candidate.resolve()
        if _is_complete_bundle(resolved):
            return resolved
    raise PluginBundleUnavailable("Hydra Codex plugin bundle is unavailable")


def materialize_plugin_bundle(destination: Path | str) -> Path:
    """Copy the complete bundle to a new operator-selected directory."""
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError("plugin bundle destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(plugin_bundle_path(), target)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydra-codex-plugin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("path", help="print the installed plugin bundle path")
    materialize = commands.add_parser(
        "materialize", help="copy the plugin bundle to a new directory",
    )
    materialize.add_argument("destination")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the supported installed plugin bundle command."""
    output = sys.stdout if stdout is None else stdout
    error = sys.stderr if stderr is None else stderr
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "path":
            result = plugin_bundle_path()
        else:
            result = materialize_plugin_bundle(arguments.destination)
    except (FileExistsError, OSError, PluginBundleUnavailable):
        error.write("hydra-codex-plugin: operation failed\n")
        return 1
    output.write(str(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
