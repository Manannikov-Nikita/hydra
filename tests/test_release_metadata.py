from __future__ import annotations

import io
import json
from pathlib import Path
import tomllib
import unittest

from hydra_codex import __version__
from hydra_codex.cli import main as run_cli
from hydra_codex.mcp_server import StdioMcpServer


_ROOT = Path(__file__).parents[1]


def load_pyproject() -> dict[str, object]:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


class ReleaseMetadataTests(unittest.TestCase):
    def test_plugin_manifest_version_matches_canonical_package_version(self) -> None:
        manifest = json.loads(
            (_ROOT / "plugins" / "hydra-codex" / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8",
            ),
        )

        self.assertEqual(manifest["version"], __version__)

    def test_cli_and_mcp_use_package_version(self) -> None:
        stdout = io.StringIO()

        self.assertEqual(run_cli(["--version"], stdout=stdout), 0)
        self.assertEqual(stdout.getvalue(), f"hydra-codex {__version__}\n")
        initialized = StdioMcpServer().handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(initialized["result"]["serverInfo"]["version"], __version__)

    def test_public_metadata_and_license_are_declared_for_distributions(self) -> None:
        metadata = load_pyproject()
        configuration = tomllib.loads(
            (_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )

        self.assertEqual(metadata["license"], "MIT")
        self.assertEqual(metadata["license-files"], ["LICENSE"])
        self.assertNotIn(
            "License :: OSI Approved :: MIT License",
            metadata["classifiers"],
        )
        self.assertEqual(
            metadata["urls"]["Repository"],
            "https://github.com/Manannikov-Nikita/hydra",
        )
        self.assertNotIn("license-files", configuration.get("tool", {}).get("setuptools", {}))
        self.assertTrue((_ROOT / "LICENSE").is_file())
