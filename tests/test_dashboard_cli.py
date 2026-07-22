from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hydra_codex.cli import build_parser, main


class DashboardCliTests(unittest.TestCase):
    def invoke(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("hydra_codex.dashboard_launch.run_dashboard") as runner:
            code = main(
                argv,
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=stderr,
                environ={"HOME": "/private/nonexistent"},
                services=object(),
                installation_key_path=Path("/private/key"),
            )
        return code, stdout.getvalue(), stderr.getvalue(), runner

    def test_parser_has_port_and_no_open_without_host(self) -> None:
        arguments = build_parser().parse_args([
            "dashboard", "--port", "0", "--no-open", "--db", "hydra.db",
            "--cwd", ".",
        ])

        self.assertEqual((arguments.port, arguments.no_open), (0, True))
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["dashboard", "--host", "0.0.0.0"])

    def test_dashboard_calls_direct_runner_without_command_services(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, stdout, stderr, runner = self.invoke([
                "dashboard", "--port", "43125", "--no-open",
                "--db", str(Path(temporary) / "hydra.sqlite3"),
                "--cwd", temporary,
            ])

        self.assertEqual((code, stdout, stderr), (0, "", ""))
        runner.assert_called_once()
        arguments = runner.call_args.kwargs
        self.assertEqual(arguments["port"], 43125)
        self.assertTrue(arguments["no_open"])
        self.assertEqual(arguments["cwd"], Path(temporary))
        self.assertTrue(callable(arguments["stdout"].write))

    def test_invalid_ports_fail_without_echoing_value(self) -> None:
        for value in ("-1", "65536", "private-value"):
            with self.subTest(value=value):
                code, stdout, stderr, runner = self.invoke([
                    "dashboard", "--port", value,
                ])
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("hydra-codex: invalid arguments", stderr)
                self.assertNotIn(value, stderr)
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
