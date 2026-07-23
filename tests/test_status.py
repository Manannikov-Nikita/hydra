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
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.environ = {"HOME": str(self.home)}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_uninitialized_status_is_successful_read_only_and_path_private(self) -> None:
        before = inventory(self.root)

        result = collect_status(self.project, environ=self.environ)

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

        result = collect_status(self.project / "missing", environ=self.environ)

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
            collect_status(self.project, environ=self.environ)

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
            result = collect_status(self.project, environ=self.environ)

        self.assertEqual(result["storage"], {"exists": True, "schema_version": 22})
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(len(calls), 1)
        self.assertTrue(str(calls[0][0]).startswith("file:"))
        self.assertIn("mode=ro", str(calls[0][0]))
        self.assertEqual(calls[0][1], {"uri": True})

    def test_missing_database_and_installation_key_are_not_created(self) -> None:
        database = default_database_path(self.home, environ=self.environ)
        key = default_installation_key_path(self.home, environ=self.environ)

        result = collect_status(self.project, environ=self.environ)

        self.assertEqual(result["storage"], {"exists": False, "schema_version": None})
        self.assertEqual(result["installation"], {"identity_key_exists": False})
        self.assertFalse(database.exists())
        self.assertFalse(key.exists())
        self.assertFalse(database.parent.exists())

    def test_existing_installation_key_is_reported_without_disclosing_path(self) -> None:
        key = default_installation_key_path(self.home, environ=self.environ)
        key.parent.mkdir(parents=True)
        key.write_bytes(b"k" * 32)

        result = collect_status(self.project, environ=self.environ)

        self.assertEqual(result["installation"], {"identity_key_exists": True})
        self.assertNotIn(str(key), json.dumps(result))


if __name__ == "__main__":
    unittest.main()
