from __future__ import annotations

from pathlib import Path
import unittest

from hydra_codex.platform_paths import (
    default_data_directory,
    default_database_path,
    default_installation_key_path,
)


class PlatformPathTests(unittest.TestCase):
    def test_macos_preserves_application_support_path(self) -> None:
        self.assertEqual(
            default_data_directory(Path("/Users/test"), platform="darwin"),
            Path("/Users/test/Library/Application Support/Hydra"),
        )

    def test_linux_uses_absolute_xdg_data_home(self) -> None:
        self.assertEqual(
            default_data_directory(
                Path("/home/test"),
                platform="linux",
                environ={"XDG_DATA_HOME": "/data/test"},
            ),
            Path("/data/test/hydra"),
        )

    def test_linux_ignores_relative_xdg_data_home(self) -> None:
        self.assertEqual(
            default_data_directory(
                Path("/home/test"),
                platform="linux",
                environ={"XDG_DATA_HOME": "relative"},
            ),
            Path("/home/test/.local/share/hydra"),
        )

    def test_database_and_key_are_derived_from_platform_directory(self) -> None:
        self.assertEqual(
            default_database_path(Path("/home/test"), platform="linux"),
            Path("/home/test/.local/share/hydra/hydra.sqlite3"),
        )
        self.assertEqual(
            default_installation_key_path(Path("/home/test"), platform="linux"),
            Path("/home/test/.local/share/hydra/rollout-hmac.key"),
        )

    def test_unsupported_platform_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported platform: win32"):
            default_data_directory(Path("/Users/test"), platform="win32")
