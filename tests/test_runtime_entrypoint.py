from __future__ import annotations

import io
from pathlib import Path
import unittest
from unittest.mock import ANY, patch

from hydra_codex.cli import main as cli_main
from hydra_codex.runtime_entrypoint import runtime_command_prefix


class RuntimeEntrypointTests(unittest.TestCase):
    def test_source_runtime_uses_current_interpreter_module(self) -> None:
        self.assertEqual(
            runtime_command_prefix(
                executable=Path("/venv/bin/python"),
                frozen=False,
            ),
            ("/venv/bin/python", "-m", "hydra_codex"),
        )

    def test_frozen_runtime_uses_single_executable(self) -> None:
        self.assertEqual(
            runtime_command_prefix(
                executable=Path("/bundle/bin/hydra-codex"),
                frozen=True,
            ),
            ("/bundle/bin/hydra-codex",),
        )


class RuntimeCliRouteTests(unittest.TestCase):
    def test_hook_route_uses_injected_current_runtime_without_services(self) -> None:
        stdin = io.StringIO("{}")
        stdout = io.StringIO()
        stderr = io.StringIO()
        prefix = ("/trusted/python", "-m", "hydra_codex")

        with (
            patch(
                "hydra_codex.runtime_entrypoint.runtime_command_prefix",
                return_value=prefix,
            ),
            patch("hydra_codex.hook_runtime.run", return_value=0) as hook_run,
        ):
            status = cli_main(
                ["hook"],
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                services=object(),
            )

        self.assertEqual(status, 0)
        hook_run.assert_called_once_with(
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environ=ANY,
            command_prefix=prefix,
        )

    def test_mcp_route_uses_injected_current_runtime_without_services(self) -> None:
        stdin = io.StringIO("")
        stdout = io.StringIO()
        stderr = io.StringIO()
        prefix = ("/trusted/python", "-m", "hydra_codex")

        with (
            patch(
                "hydra_codex.runtime_entrypoint.runtime_command_prefix",
                return_value=prefix,
            ),
            patch("hydra_codex.mcp_server.main", return_value=0) as mcp_main,
        ):
            status = cli_main(
                ["mcp"],
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                services=object(),
            )

        self.assertEqual(status, 0)
        mcp_main.assert_called_once_with(
            input_stream=stdin,
            output_stream=stdout,
            command_prefix=prefix,
            environ=ANY,
        )
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
