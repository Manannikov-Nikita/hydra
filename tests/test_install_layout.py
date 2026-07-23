from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from hydra_codex import __version__
from hydra_codex.install_layout import (
    InvalidBundle,
    UnsupportedTarget,
    frozen_bundle_root,
    platform_target,
    validate_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


class InstallLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.bundle = Path(self.temporary.name) / "hydra-codex"
        (self.bundle / "bin").mkdir(parents=True)
        executable = self.bundle / "bin" / "hydra-codex"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        (self.bundle / "VERSION").write_text(__version__ + "\n", encoding="utf-8")
        (self.bundle / "TARGET").write_text("darwin-arm64\n", encoding="utf-8")
        (self.bundle / "LICENSE").write_text("MIT\n", encoding="utf-8")
        marketplace = self.bundle / "marketplace"
        (marketplace / ".agents" / "plugins").mkdir(parents=True)
        shutil.copy2(
            ROOT / ".agents" / "plugins" / "marketplace.json",
            marketplace / ".agents" / "plugins" / "marketplace.json",
        )
        shutil.copytree(
            ROOT / "plugins" / "hydra-codex",
            marketplace / "plugins" / "hydra-codex",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validate_bundle_requires_matching_version_target_and_marketplace(self) -> None:
        layout = validate_bundle(
            self.bundle,
            expected_version=__version__,
            expected_target="darwin-arm64",
        )

        self.assertEqual(layout.root, self.bundle)
        self.assertEqual(layout.version, __version__)
        self.assertEqual(layout.target, "darwin-arm64")
        self.assertEqual(
            layout.executable,
            self.bundle / "bin" / "hydra-codex",
        )
        self.assertEqual(
            layout.marketplace,
            self.bundle / "marketplace",
        )

    def test_validate_bundle_rejects_mismatch_incomplete_inventory_and_nonexecutable(self) -> None:
        cases = (
            ("version", lambda: (self.bundle / "VERSION").write_text(
                "9.9.9\n", encoding="utf-8",
            )),
            ("target", lambda: (self.bundle / "TARGET").write_text(
                "linux-aarch64\n", encoding="utf-8",
            )),
            ("marketplace", lambda: (
                self.bundle / "marketplace" / ".agents" / "plugins"
                / "marketplace.json"
            ).unlink()),
            ("executable", lambda: (
                self.bundle / "bin" / "hydra-codex"
            ).chmod(0o600)),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temporary:
                    candidate = Path(temporary) / "bundle"
                    shutil.copytree(self.bundle, candidate)
                    original = self.bundle
                    try:
                        self.bundle = candidate
                        mutate()
                        with self.assertRaises(InvalidBundle):
                            validate_bundle(
                                candidate,
                                expected_version=__version__,
                                expected_target="darwin-arm64",
                            )
                    finally:
                        self.bundle = original

    def test_platform_allowlist_maps_alias_and_rejects_unknown_architecture(self) -> None:
        self.assertEqual(platform_target("Darwin", "arm64"), "darwin-arm64")
        self.assertEqual(platform_target("darwin", "x86_64"), "darwin-x86_64")
        self.assertEqual(platform_target("LINUX", "AMD64"), "linux-x86_64")
        with self.assertRaises(UnsupportedTarget):
            platform_target("linux", "aarch64")
        with self.assertRaises(UnsupportedTarget):
            platform_target("windows", "x86_64")

    def test_frozen_bundle_root_supports_public_and_pyinstaller_executables(self) -> None:
        self.assertEqual(
            frozen_bundle_root(self.bundle / "bin" / "hydra-codex"),
            self.bundle,
        )
        self.assertEqual(
            frozen_bundle_root(
                self.bundle / "runtime" / "hydra-codex" / "hydra-codex",
            ),
            self.bundle,
        )
        with patch("hydra_codex.install_layout.sys") as runtime:
            runtime.frozen = False
            runtime.executable = "/venv/bin/python"
            self.assertIsNone(frozen_bundle_root())

    def test_marketplace_inventory_points_to_the_canonical_plugin_and_version(self) -> None:
        marketplace = self.bundle / "marketplace"
        inventory = json.loads(
            (
                marketplace / ".agents" / "plugins" / "marketplace.json"
            ).read_text(encoding="utf-8"),
        )
        self.assertEqual(inventory["name"], "hydra")
        self.assertEqual(len(inventory["plugins"]), 1)
        plugin = inventory["plugins"][0]
        self.assertEqual(plugin["name"], "hydra-codex")
        self.assertEqual(plugin["source"], {
            "source": "local",
            "path": "./plugins/hydra-codex",
        })
        plugin_manifest = json.loads(
            (
                marketplace / "plugins" / "hydra-codex"
                / ".codex-plugin" / "plugin.json"
            ).read_text(encoding="utf-8"),
        )
        self.assertEqual(plugin_manifest["version"], __version__)


if __name__ == "__main__":
    unittest.main()
