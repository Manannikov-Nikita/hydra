from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import call, patch

from hydra_codex.dashboard_launch import (
    _bootstrap_database,
    _bootstrap_doctor,
    load_dashboard_assets,
    run_dashboard,
)
from hydra_codex.dashboard_refresh import RefreshController
from hydra_codex.dashboard_queries import DashboardQueryService
from hydra_codex.dashboard_server import DashboardRequest
from hydra_codex.public_refs import project_catalog_references
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore


TOKEN = "A" * 43
EXPECTED_ROUTES = {
    "/",
    "/assets/tokens.css",
    "/assets/dashboard.css",
    "/assets/bootstrap.js",
    "/assets/api.js",
    "/assets/state.js",
    "/assets/dom.js",
    "/assets/app.js",
    "/assets/views/shell.js",
    "/assets/views/overview.js",
    "/assets/views/tasks.js",
    "/assets/views/compare.js",
    "/assets/views/health.js",
    "/assets/views/evidence.js",
}


class FakeServer:
    server_address = ("127.0.0.1", 43125)

    def __init__(self) -> None:
        self.served = 0
        self.closed = 0

    def serve_forever(self) -> None:
        self.served += 1
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed += 1


class RecordingOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class DashboardDistributionTests(unittest.TestCase):
    def test_loader_has_exact_allowlisted_routes_and_bytes(self) -> None:
        assets = load_dashboard_assets()

        self.assertEqual(set(assets), EXPECTED_ROUTES)
        self.assertEqual(assets["/"].content_type, "text/html; charset=utf-8")
        self.assertEqual(assets["/assets/tokens.css"].content_type, "text/css; charset=utf-8")
        self.assertEqual(assets["/assets/app.js"].content_type, "text/javascript; charset=utf-8")
        root = Path(__file__).parents[1] / "src" / "hydra_codex" / "dashboard_assets"
        self.assertEqual(assets["/"].body, (root / "index.html").read_bytes())
        self.assertEqual(
            assets["/assets/views/evidence.js"].body,
            (root / "views" / "evidence.js").read_bytes(),
        )

    def test_package_data_declares_every_asset_level(self) -> None:
        pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("[tool.setuptools.package-data]", pyproject)
        self.assertIn('"dashboard_assets/*.html"', pyproject)
        self.assertIn('"dashboard_assets/*.css"', pyproject)
        self.assertIn('"dashboard_assets/*.js"', pyproject)
        self.assertIn('"dashboard_assets/views/*.js"', pyproject)

    def project(self, root: Path) -> None:
        config = root / ".hydra" / "project.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'project_id = "hprj_4db8fca38ef042f3"\n'
            'display_name = "Hydra Core"\n',
            encoding="utf-8",
        )

    def test_launch_handoff_opens_once_and_stdout_is_token_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            server = FakeServer()
            opened: list[str] = []
            output = io.StringIO()

            with patch("hydra_codex.dashboard_launch.secrets.token_urlsafe", return_value=TOKEN), patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=lambda **_kwargs: server,
            ):
                run_dashboard(
                    port=0,
                    no_open=False,
                    database_path=root / "hydra.sqlite3",
                    environ={"HOME": str(root)},
                    installation_key_path=root / "rollout-hmac.key",
                    cwd=root,
                    stdout=output,
                    browser_open=lambda url: opened.append(url) or True,
                )

            self.assertEqual(opened, [f"http://127.0.0.1:43125/#token={TOKEN}"])
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertIn("http://127.0.0.1:43125/", output.getvalue())
            self.assertEqual((server.served, server.closed), (1, 1))

    def test_no_open_prints_initial_handoff_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            server = FakeServer()
            output = RecordingOutput()
            with patch("hydra_codex.dashboard_launch.secrets.token_urlsafe", return_value=TOKEN), patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=lambda **_kwargs: server,
            ):
                run_dashboard(
                    port=43125,
                    no_open=True,
                    database_path=root / "hydra.sqlite3",
                    environ={"HOME": str(root)},
                    installation_key_path=root / "rollout-hmac.key",
                    cwd=root,
                    stdout=output,
                    browser_open=lambda _url: self.fail("browser must remain closed"),
                )

            rendered = output.getvalue()
            self.assertEqual(rendered.count(TOKEN), 1)
            self.assertEqual(rendered, f"http://127.0.0.1:43125/#token={TOKEN}\n")
            self.assertEqual(output.flushes, 1)

    def test_server_creation_failure_still_closes_refresh_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            with patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=OSError("private bind failure"),
            ), patch.object(RefreshController, "close", autospec=True) as close:
                with self.assertRaises(OSError):
                    run_dashboard(
                        port=0,
                        no_open=True,
                        database_path=root / "hydra.sqlite3",
                        environ={"HOME": str(root)},
                        installation_key_path=root / "rollout-hmac.key",
                        cwd=root,
                        stdout=io.StringIO(),
                    )
            close.assert_called_once()

    def test_launch_binds_before_any_hydra_store_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            database = root / "hydra.sqlite3"
            store = HydraStore(database)
            try:
                store.connection.execute(
                    """INSERT INTO dashboard_projects(
                           project_id,display_name,first_seen_at,last_seen_at)
                       VALUES ('project-a','Hydra Core',
                               '2026-07-22T00:00:00Z','2026-07-22T01:00:00Z')""",
                )
                store.connection.execute(
                    """INSERT INTO reconciled_tasks(
                           project_id,root_key,public_ref,status,cutoff_at,
                           last_activity_at,task_family,reconciliation_version,input_digest)
                       VALUES ('project-a','root-a','task_0123456789ab','complete',
                               '2026-07-22T01:00:00Z','2026-07-22T01:00:00Z',
                               'telemetry-analysis',1,'digest-a')""",
                )
                store.connection.commit()
            finally:
                store.close()
            server = FakeServer()
            with patch(
                "hydra_codex.dashboard_launch.HydraStore",
                side_effect=AssertionError("HydraStore must remain behind Refresh"),
            ), patch(
                "hydra_codex.dashboard_queries.list_reconciled_reports",
                side_effect=AssertionError("launch must not reconstruct reports"),
            ), patch(
                "hydra_codex.dashboard_queries.read_pilot_status",
                side_effect=AssertionError("launch must not rebuild pilot status"),
            ), patch(
                "hydra_codex.dashboard_queries.storage_status",
                side_effect=AssertionError("launch must not scan raw storage facts"),
            ), patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=lambda **_kwargs: server,
            ):
                run_dashboard(
                    port=0,
                    no_open=True,
                    database_path=database,
                    environ={"HOME": str(root)},
                    installation_key_path=root / "rollout-hmac.key",
                    cwd=root,
                    stdout=io.StringIO(),
                )

            self.assertEqual((server.served, server.closed), (1, 1))

    def test_launch_observes_basename_and_defaults_to_the_cwd_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_root = root / "hydra-fixture"
            config = project_root / ".hydra" / "project.toml"
            config.parent.mkdir(parents=True)
            current_project_id = "hprj_4db8fca38ef042f3"
            config.write_text(
                f'project_id = "{current_project_id}"\n',
                encoding="utf-8",
            )
            database = root / "hydra.sqlite3"
            key_path = root / "rollout-hmac.key"
            key = Pseudonymizer.installation_key(key_path).key
            other_project_id = ""
            current_ref = ""
            other_ref = ""
            for index in range(1000):
                candidate = f"hprj_other_{index:04d}"
                projection = project_catalog_references(
                    (current_project_id, candidate), key,
                )
                if projection[candidate] < projection[current_project_id]:
                    other_project_id = candidate
                    current_ref = projection[current_project_id]
                    other_ref = projection[candidate]
                    break
            self.assertTrue(other_project_id)
            self.assertLess(other_ref, current_ref)

            store = HydraStore(database)
            try:
                store.connection.executemany(
                    """INSERT INTO dashboard_projects(
                           project_id,display_name,first_seen_at,last_seen_at,
                           display_name_provenance)
                       VALUES (?,?,?,?,?)""",
                    (
                        (
                            current_project_id, None,
                            "2026-07-29T00:00:00Z",
                            "2026-07-29T01:00:00Z",
                            None,
                        ),
                        (
                            other_project_id, "Other Project",
                            "2026-07-29T00:00:00Z",
                            "2026-07-29T01:00:00Z",
                            "config",
                        ),
                    ),
                )
                store.connection.commit()
            finally:
                store.close()

            captured: dict[str, object] = {}

            class ProbeServer(FakeServer):
                def serve_forever(self) -> None:
                    self.served += 1
                    application = captured["application"].bound_to(
                        "127.0.0.1:43125",
                    )
                    response = application.handle(DashboardRequest(
                        "GET",
                        "/api/v1/snapshot",
                        (
                            ("Host", "127.0.0.1:43125"),
                            ("Authorization", f"Bearer {TOKEN}"),
                        ),
                    ))
                    captured["response"] = response
                    raise KeyboardInterrupt

            server = ProbeServer()

            def create(**kwargs):
                application = kwargs["application"]
                application._sync_controller = None
                captured["application"] = application
                return server

            with patch(
                "hydra_codex.dashboard_launch.secrets.token_urlsafe",
                return_value=TOKEN,
            ), patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=create,
            ):
                run_dashboard(
                    port=0,
                    no_open=True,
                    database_path=database,
                    environ={"HOME": str(root)},
                    installation_key_path=key_path,
                    cwd=project_root,
                    stdout=io.StringIO(),
                )

            response = captured["response"]
            self.assertEqual(response.status, 200)
            payload = json.loads(response.body)
            self.assertEqual(payload["selected_project_ref"], current_ref)
            self.assertEqual(payload["project"]["display_name"], "hydra-fixture")
            self.assertEqual(payload["data_revision"], 1)
            serialized = json.dumps(payload, sort_keys=True)
            for private in (
                current_project_id,
                other_project_id,
                str(root),
                str(project_root),
            ):
                self.assertNotIn(private, serialized)
            reopened = HydraStore.open_current(database)
            try:
                row = reopened.connection.execute(
                    """SELECT display_name,display_name_provenance
                         FROM dashboard_projects WHERE project_id=?""",
                    (current_project_id,),
                ).fetchone()
                revision = reopened.connection.execute(
                    "SELECT revision FROM sync_data_revision WHERE singleton=1",
                ).fetchone()[0]
            finally:
                reopened.close()
            self.assertEqual(tuple(row), ("hydra-fixture", "repo_basename"))
            self.assertEqual(revision, 1)
            self.assertEqual((server.served, server.closed), (1, 1))

    def test_corrupt_database_still_serves_truthful_unavailable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            database = root / "corrupt.sqlite3"
            database.write_bytes(b"not a sqlite database")
            server = FakeServer()
            captured = {}

            def create(**kwargs):
                captured["application"] = kwargs["application"]
                return server

            with patch(
                "hydra_codex.dashboard_launch.secrets.token_urlsafe",
                return_value=TOKEN,
            ), patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=create,
            ):
                run_dashboard(
                    port=0,
                    no_open=True,
                    database_path=database,
                    environ={"HOME": str(root)},
                    installation_key_path=root / "rollout-hmac.key",
                    cwd=root,
                    stdout=io.StringIO(),
                )

            application = captured["application"].bound_to("127.0.0.1:43125")
            response = application.handle(DashboardRequest(
                "GET", "/api/v1/snapshot",
                (("Host", "127.0.0.1:43125"), ("Authorization", f"Bearer {TOKEN}")),
            ))
            self.assertEqual(response.status, 503)
            payload = json.loads(response.body)
            self.assertEqual(payload["error"]["code"], "storage_unavailable")
            self.assertEqual(database.read_bytes(), b"not a sqlite database")

    def test_bootstrap_metadata_failure_is_categorical_storage_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            database = root / "hydra.sqlite3"
            HydraStore(database).close()
            server = FakeServer()
            captured = {}

            def create(**kwargs):
                captured["application"] = kwargs["application"]
                return server

            with patch(
                "hydra_codex.dashboard_launch.secrets.token_urlsafe",
                return_value=TOKEN,
            ), patch.object(
                DashboardQueryService,
                "bootstrap_snapshots_from_connection",
                side_effect=sqlite3.DatabaseError("private catalog page"),
            ), patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=create,
            ):
                run_dashboard(
                    port=0,
                    no_open=True,
                    database_path=database,
                    environ={"HOME": str(root)},
                    installation_key_path=root / "rollout-hmac.key",
                    cwd=root,
                    stdout=io.StringIO(),
                )

            response = captured["application"].bound_to(
                "127.0.0.1:43125",
            ).handle(DashboardRequest(
                "GET", "/api/v1/snapshot",
                (("Host", "127.0.0.1:43125"), ("Authorization", f"Bearer {TOKEN}")),
            ))
            self.assertEqual(response.status, 503)
            self.assertEqual(
                json.loads(response.body)["error"]["code"],
                "storage_unavailable",
            )

    def test_bootstrap_stat_permission_error_is_not_treated_as_missing(self) -> None:
        path = Path("/private/unreadable/hydra.sqlite3")
        with patch.object(Path, "stat", side_effect=PermissionError("private")):
            connection, state = _bootstrap_database(path)

        self.assertIsNone(connection)
        self.assertEqual(state, "unavailable")

    def test_bootstrap_doctor_checks_database_wal_and_shm_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            database = root / "hydra.sqlite3"
            database.write_bytes(b"sqlite")
            database.chmod(0o600)
            Path(str(database) + "-shm").write_bytes(b"shm")
            Path(str(database) + "-shm").chmod(0o600)
            Path(str(database) + "-wal").write_bytes(b"wal")
            Path(str(database) + "-wal").chmod(0o644)

            report = _bootstrap_doctor(
                cwd=root, database_path=database, database_state="current",
            ).as_dict()

        permissions = next(
            item for item in report["checks"]
            if item["code"] == "storage_permissions_restricted"
        )
        self.assertEqual(permissions["status"], "failed")

    def test_default_refresh_factory_preserves_implicit_database_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            server = FakeServer()
            captured = {}

            def create(**kwargs):
                captured["application"] = kwargs["application"]
                return server

            with patch(
                "hydra_codex.dashboard_launch.default_database_path",
                return_value=root / "missing-parent" / "hydra.sqlite3",
            ), patch(
                "hydra_codex.dashboard_launch.HydraStore.open_current",
            ) as open_current, patch(
                "hydra_codex.dashboard_launch.create_dashboard_server",
                side_effect=create,
            ):
                run_dashboard(
                    port=0,
                    no_open=True,
                    database_path=None,
                    environ={"HOME": str(root)},
                    installation_key_path=root / "rollout-hmac.key",
                    cwd=root,
                    stdout=io.StringIO(),
                )
                captured["application"]._controller._runner._store_factory()

            self.assertEqual(open_current.call_args_list, [call(None)])


if __name__ == "__main__":
    unittest.main()
