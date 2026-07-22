from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from hydra_codex.diagnostics import render_doctor, run_doctor
from hydra_codex.storage import HydraStore


class DoctorTests(unittest.TestCase):
    def test_healthy_doctor_uses_only_safe_categorical_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "hprj_private_doctor"\n', encoding="utf-8",
            )
            database = root / "private" / "hydra.sqlite3"
            database.parent.mkdir(mode=0o700)
            store = HydraStore(database)
            store.close()

            report = run_doctor(cwd=project, database_path=database)
            rendered = render_doctor(report, "json")

        payload = json.loads(rendered)
        self.assertEqual(payload["schema_version"], "hydra.doctor/v1")
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(
            [item["code"] for item in payload["checks"]],
            [
                "project_resolution", "storage_available", "schema_current",
                "foreign_keys_ok", "integrity_ok",
                "storage_permissions_restricted",
            ],
        )
        self.assertTrue(all(item["status"] == "ok" for item in payload["checks"]))
        for private in (str(project), str(database), "hprj_private_doctor", "exception"):
            self.assertNotIn(private, rendered)

    def test_missing_project_is_degraded_without_paths_or_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = run_doctor(
                cwd=root / "missing-private-project",
                database_path=root / "private-database.sqlite3",
            )

        payload = report.as_dict()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["checks"][0], {
            "code": "project_resolution", "status": "failed",
        })
        self.assertTrue(all(
            item["status"] == "unavailable" for item in payload["checks"][1:]
        ))
        rendered = render_doctor(report, "markdown")
        self.assertNotIn("missing-private-project", rendered)
        self.assertNotIn("private-database", rendered)

    def test_open_storage_with_public_permissions_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "hprj_permissions"\n', encoding="utf-8",
            )
            database = root / "hydra.sqlite3"
            store = HydraStore(database)
            store.close()
            database.chmod(0o644)

            payload = run_doctor(cwd=project, database_path=database).as_dict()

        checks = {item["code"]: item["status"] for item in payload["checks"]}
        self.assertEqual(checks["storage_permissions_restricted"], "failed")
        self.assertEqual(payload["status"], "degraded")

    def test_missing_storage_is_reported_without_creating_or_migrating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "hprj_missing_storage"\n', encoding="utf-8",
            )
            database = root / "missing" / "hydra.sqlite3"

            payload = run_doctor(cwd=project, database_path=database).as_dict()

            self.assertFalse(database.exists())
            self.assertFalse(database.parent.exists())
        checks = {item["code"]: item["status"] for item in payload["checks"]}
        self.assertEqual(checks["project_resolution"], "ok")
        self.assertEqual(checks["storage_available"], "failed")
        self.assertTrue(all(
            checks[code] == "unavailable" for code in (
                "schema_current", "foreign_keys_ok", "integrity_ok",
                "storage_permissions_restricted",
            )
        ))

    def test_outdated_storage_is_inspected_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "hprj_old_storage"\n', encoding="utf-8",
            )
            database = root / "old.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE legacy(value TEXT)")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()
            before = database.read_bytes()

            payload = run_doctor(cwd=project, database_path=database).as_dict()

            self.assertEqual(database.read_bytes(), before)
        checks = {item["code"]: item["status"] for item in payload["checks"]}
        self.assertEqual(checks["storage_available"], "ok")
        self.assertEqual(checks["schema_current"], "failed")

    def test_failed_read_only_setup_closes_the_partial_connection(self) -> None:
        class PartialConnection:
            closed = False

            def execute(self, _statement):
                raise sqlite3.OperationalError("private setup failure")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "hprj_partial_connection"\n', encoding="utf-8",
            )
            database = root / "hydra.sqlite3"
            database.write_bytes(b"safe placeholder")
            connection = PartialConnection()

            with mock.patch(
                "hydra_codex.diagnostics.sqlite3.connect",
                return_value=connection,
            ):
                payload = run_doctor(
                    cwd=project, database_path=database,
                ).as_dict()

        self.assertTrue(connection.closed)
        self.assertEqual(payload["checks"][1], {
            "code": "storage_available", "status": "failed",
        })


if __name__ == "__main__":
    unittest.main()
