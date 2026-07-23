from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from hydra_codex.platform_paths import (
    default_database_path,
    default_installation_key_path,
)
from hydra_codex.project_config import ProjectConfigError
from hydra_codex.project_lifecycle import initialize_project
from hydra_codex.status import collect_status


def inventory(root: Path) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.lstat().st_mode,
                path.lstat().st_size,
            )
            for path in root.rglob("*")
        ),
    )


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_codex_integration import StatefulCodexClient

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.environ = {"HOME": str(self.home)}
        self.marketplace = self.root / "marketplace"
        self.marketplace.mkdir()
        self.client = StatefulCodexClient()
        self.client.available_versions[self.marketplace.resolve()] = "0.1.0"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def status(self, path: Path | None = None):
        return collect_status(
            self.project if path is None else path,
            environ=self.environ,
            codex_client=self.client,
            marketplace_root=self.marketplace,
        )

    def test_uninitialized_status_is_successful_read_only_and_path_private(self) -> None:
        before = inventory(self.root)

        result = self.status()

        self.assertEqual(
            result["project"],
            {
                "initialized": False,
                "identity_valid": None,
                "config_schema_version": None,
            },
        )
        self.assertEqual(inventory(self.root), before)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_initialized_status_reports_schema_without_project_id_or_path(self) -> None:
        initialize_project(
            self.project,
            project_id_factory=lambda: "hprj_0123456789abcdef",
            home=self.home,
        )

        result = self.status(self.project / "missing")

        self.assertEqual(
            result["project"],
            {
                "initialized": True,
                "identity_valid": True,
                "config_schema_version": 1,
            },
        )
        encoded = json.dumps(result)
        self.assertNotIn("hprj_0123456789abcdef", encoded)
        self.assertNotIn(str(self.root), encoded)

    def test_malformed_initialized_status_fails_closed_and_read_only(self) -> None:
        (self.project / ".hydra").mkdir()
        config = self.project / ".hydra" / "project.toml"
        config.write_text('project_id = "/private/secret"\n', encoding="utf-8")
        before = inventory(self.root)

        with self.assertRaises(ProjectConfigError) as raised:
            self.status()

        self.assertEqual(inventory(self.root), before)
        self.assertNotIn(str(self.root), str(raised.exception))

    def test_existing_database_is_opened_with_read_only_uri(self) -> None:
        database = default_database_path(
            self.home,
            environ=self.environ,
        )
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES (22, 'now')",
            )
            connection.commit()
        finally:
            connection.close()
        before = database.read_bytes()
        real_connect = sqlite3.connect
        calls: list[tuple[object, dict[str, object]]] = []

        def recording_connect(*args, **kwargs):
            calls.append((args[0], dict(kwargs)))
            return real_connect(*args, **kwargs)

        with mock.patch(
            "hydra_codex.status.sqlite3.connect",
            side_effect=recording_connect,
        ):
            result = self.status()

        self.assertEqual(result["storage"], {"exists": True, "schema_version": 22})
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(len(calls), 1)
        self.assertTrue(str(calls[0][0]).startswith("file:"))
        self.assertIn("mode=ro", str(calls[0][0]))
        self.assertEqual(calls[0][1], {"uri": True})

    def test_missing_database_and_installation_key_are_not_created(self) -> None:
        database = default_database_path(self.home, environ=self.environ)
        key = default_installation_key_path(self.home, environ=self.environ)

        result = self.status()

        self.assertEqual(result["storage"], {"exists": False, "schema_version": None})
        self.assertEqual(result["installation"], {"identity_key_exists": False})
        self.assertFalse(database.exists())
        self.assertFalse(key.exists())
        self.assertFalse(database.parent.exists())

    def test_existing_installation_key_is_reported_without_disclosing_path(self) -> None:
        key = default_installation_key_path(self.home, environ=self.environ)
        key.parent.mkdir(parents=True)
        key.write_bytes(b"k" * 32)

        result = self.status()

        self.assertEqual(result["installation"], {"identity_key_exists": True})
        self.assertNotIn(str(key), json.dumps(result))

    def test_codex_status_reports_exact_version_parity_and_new_task_action(self) -> None:
        self.client.marketplaces["hydra"] = self.marketplace.resolve()
        self.client.installed_version = "0.1.0"

        result = self.status()

        self.assertEqual(
            result["codex"],
            {
                "available": True,
                "compatible": True,
                "marketplace_installed": True,
                "plugin_installed": True,
                "plugin_version": "0.1.0",
                "version_matches": True,
                "new_task_required": True,
                "next_actions": ["Start a new Codex task to load Hydra."],
            },
        )
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(self.client.calls, [])

    def test_codex_status_reports_capability_failure_without_mutation(self) -> None:
        self.client.plugin_listing_supported = False
        before = inventory(self.root)

        result = self.status()

        self.assertEqual(
            result["codex"],
            {
                "available": True,
                "compatible": False,
                "marketplace_installed": None,
                "plugin_installed": None,
                "plugin_version": None,
                "version_matches": None,
                "new_task_required": False,
                "next_actions": [
                    "Install or update Codex with plugin marketplace support.",
                ],
            },
        )
        self.assertEqual(inventory(self.root), before)
        self.assertEqual(self.client.calls, [])

    def test_codex_status_never_exposes_unsafe_child_plugin_versions(self) -> None:
        versions = (
            "/private/profile/plugin",
            "0.1.0\nprivate-control",
            "v" * 129,
        )
        self.client.marketplaces["hydra"] = self.marketplace.resolve()
        for version in versions:
            with self.subTest(kind=len(version)):
                self.client.installed_version = version

                result = self.status()

                self.assertEqual(
                    result["codex"],
                    {
                        "available": True,
                        "compatible": False,
                        "marketplace_installed": None,
                        "plugin_installed": None,
                        "plugin_version": None,
                        "version_matches": None,
                        "new_task_required": False,
                        "next_actions": [
                            "Install or update Codex with plugin marketplace support.",
                        ],
                    },
                )
                self.assertNotIn(version, json.dumps(result))

    def test_status_without_an_executable_path_never_runs_or_creates_codex_state(self) -> None:
        before = inventory(self.root)

        with mock.patch(
            "hydra_codex.codex_integration._run_bounded",
            side_effect=AssertionError("Codex must not run without a supplied PATH"),
        ):
            result = collect_status(self.project, environ=self.environ)

        self.assertFalse(result["codex"]["available"])
        self.assertEqual(inventory(self.root), before)


if __name__ == "__main__":
    unittest.main()
