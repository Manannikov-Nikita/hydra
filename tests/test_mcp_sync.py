from __future__ import annotations

from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from hydra_codex.mcp_server import main as mcp_main
from hydra_codex.mcp_sync import create_mcp_sync_controller
from hydra_codex.storage import HydraStore
from hydra_codex.sync_state import SyncStateRepository


class McpSyncControllerTests(unittest.TestCase):
    def test_mcp_main_owns_and_closes_the_background_sync_controller(self) -> None:
        controller = mock.Mock()
        environ = {"HOME": "/private/mcp-test-home"}
        with mock.patch(
            "hydra_codex.mcp_sync.create_mcp_sync_controller",
            return_value=controller,
        ) as create:
            status = mcp_main(
                input_stream=io.StringIO(""),
                output_stream=io.StringIO(),
                command_prefix=("/trusted/python", "-m", "hydra_codex"),
                environ=environ,
            )

        self.assertEqual(status, 0)
        create.assert_called_once_with(environ)
        controller.close.assert_called_once_with()

    def test_mcp_factory_activates_background_sync_before_returning(self) -> None:
        controller = mock.Mock()
        environ = {"HOME": "/private/mcp-test-home"}
        with (
            mock.patch(
                "hydra_codex.mcp_sync.DashboardSyncController",
                return_value=controller,
            ),
            mock.patch(
                "hydra_codex.mcp_sync.Pseudonymizer.installation_key",
                return_value=mock.Mock(key=b"k" * 32),
            ),
        ):
            created = create_mcp_sync_controller(environ)

        self.assertIs(created, controller)
        controller.activate.assert_called_once_with()

    def test_current_database_mcp_startup_skips_full_database_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "hydra.sqlite3"
            HydraStore(database).close()
            environ = {
                "HOME": str(root),
                "HYDRA_DATABASE_PATH": str(database),
                "HYDRA_INSTALLATION_KEY_PATH": str(root / "installation.key"),
            }
            with mock.patch.object(
                HydraStore,
                "_validate_schema",
                side_effect=AssertionError(
                    "MCP startup repeated the full database audit",
                ),
            ):
                controller = create_mcp_sync_controller(environ)
            controller.close()

    def test_running_mcp_host_drains_fresh_hook_source_without_report_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            database = root / "hydra.sqlite3"
            source = sessions / "fresh.jsonl"
            source.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            environ = {
                "HOME": str(root),
                "HYDRA_DATABASE_PATH": str(database),
                "HYDRA_INSTALLATION_KEY_PATH": str(root / "installation.key"),
            }
            controller = create_mcp_sync_controller(
                environ,
                clock=lambda: datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc),
            )
            store = HydraStore(database)
            repository = SyncStateRepository(store)
            started_at = time.monotonic()
            try:
                repository.register_and_enqueue(
                    root_kind="sessions",
                    source_locator="fresh.jsonl",
                    project_id="project-mcp",
                    observed_at="2026-07-27T10:00:00Z",
                )
                terminal = None
                deadline = started_at + 2
                while time.monotonic() < deadline:
                    jobs = repository.list_jobs()
                    if jobs and jobs[0].state in {"succeeded", "partial", "failed"}:
                        terminal = jobs[0]
                        break
                    time.sleep(0.01)
            finally:
                controller.close()
                store.close()

            self.assertIsNotNone(terminal)
            self.assertEqual(terminal.state, "succeeded")
            self.assertLess(time.monotonic() - started_at, 2)
            self.assertEqual(terminal.sources_completed, 1)
