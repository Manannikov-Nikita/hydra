from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from hydra_codex.cli import atomic_write, main


VALID_ANNOTATION = {
    "kind": "phase",
    "phase": "implement",
    "cause": "plan",
    "scope_change": "none",
    "task_family": "cli-tests",
    "confidence": 0.9,
    "note": "short safe note",
}


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.report_content = "rendered report"
        self.compare_content = "rendered comparison"
        self.pilot_content = "rendered pilot"
        self.audit_content = "rendered audit"
        self.failure: Exception | None = None

    def _fail(self) -> None:
        if self.failure is not None:
            raise self.failure

    def annotate(self, annotation, capability, database_path, cwd):
        self._fail()
        self.calls.append(("annotate", annotation, capability, database_path, cwd))

    def reconcile(self, database_path, cwd):
        self._fail()
        self.calls.append(("reconcile", database_path, cwd))

    def report(self, last, output_format, database_path, cwd):
        self._fail()
        self.calls.append(("report", last, output_format, database_path, cwd))
        return self.report_content

    def compare(self, left, right, output_format, database_path, cwd):
        self._fail()
        self.calls.append(("compare", left, right, output_format, database_path, cwd))
        return self.compare_content

    def pilot_start(self, target, task_family, database_path, cwd):
        self._fail()
        self.calls.append(("pilot_start", target, task_family, database_path, cwd))
        return self.pilot_content

    def pilot_status(self, output_format, database_path, cwd):
        self._fail()
        self.calls.append(("pilot_status", output_format, database_path, cwd))
        return self.pilot_content

    def pilot_close(self, pilot_id, audit_json, decision, database_path, cwd):
        self._fail()
        self.calls.append((
            "pilot_close", pilot_id, audit_json, decision, database_path, cwd,
        ))
        return self.pilot_content

    def audit(self, pilot_id, output_format, database_path, cwd):
        self._fail()
        self.calls.append(("audit", pilot_id, output_format, database_path, cwd))
        return self.audit_content


def invoke(
    argv, *, stdin="", environ=None, services=None, installation_key_path=None,
):
    stdout = io.StringIO()
    stderr = io.StringIO()
    input_stream = io.StringIO(stdin) if isinstance(stdin, str) else stdin
    key_options = (
        {"installation_key_path": installation_key_path}
        if installation_key_path is not None else {}
    )
    code = main(
        argv,
        stdin=input_stream,
        stdout=stdout,
        stderr=stderr,
        environ={} if environ is None else environ,
        services=services,
        **key_options,
    )
    return code, stdout.getvalue(), stderr.getvalue()


class NoReadStdin:
    def isatty(self):
        raise AssertionError("flag annotation must not inspect stdin")

    def read(self):
        raise AssertionError("flag annotation must not read stdin")


class CliParserTests(unittest.TestCase):
    def test_argparse_errors_return_two(self) -> None:
        cases = (
            [],
            ["report", "--last", "0"],
            ["report", "--last", "1", "--format", "xml"],
            ["audit", "--format", "json"],
            ["audit", "--pilot", "hpilot_v1_public", "--format", "xml"],
            ["compare", "task_a"],
            ["annotate", "--capability", "secret"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                code, stdout, stderr = invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("usage:", stderr)
                self.assertNotIn("secret", stderr)

    def test_help_is_success(self) -> None:
        code, stdout, stderr = invoke(["--help"])
        self.assertEqual((code, stderr), (0, ""))
        for command in ("ingest", "annotate", "reconcile", "report", "compare", "audit"):
            self.assertIn(command, stdout)

    def test_module_entrypoint_uses_safe_parser_exit_without_leaking_argv(self) -> None:
        private = "private-argv-value"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

        completed = subprocess.run(
            [
                sys.executable, "-m", "hydra_codex", "annotate",
                "--capability", private,
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("hydra-codex: invalid arguments", completed.stderr)
        self.assertNotIn(private, completed.stderr)


class AnnotateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = FakeServices()
        self.capability = "htr_v1_do-not-echo-this-capability"
        self.environment = {"HYDRA_TURN_CAPABILITY": self.capability}

    def test_accepts_json_stdin_without_echoing_semantic_or_capability_data(self) -> None:
        code, stdout, stderr = invoke(
            ["annotate"], stdin=json.dumps(VALID_ANNOTATION),
            environ=self.environment, services=self.services,
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"command": "annotate", "status": "ok"})
        call = self.services.calls[0]
        self.assertEqual(call[0], "annotate")
        self.assertEqual(call[1].task_family, "cli-tests")
        self.assertEqual(call[2], self.capability)
        self.assertNotIn(self.capability, stdout)
        self.assertNotIn(VALID_ANNOTATION["note"], stdout)

    def test_accepts_exact_semantic_flags_without_reading_stdin(self) -> None:
        argv = [
            "annotate", "--kind", "finish", "--phase", "test_full",
            "--cause", "final_verification", "--scope-change", "none",
            "--task-family", "cli-tests", "--confidence", "1", "--note", "done",
            "--outcome", "success",
        ]
        code, _, stderr = invoke(
            argv, stdin=NoReadStdin(), environ=self.environment, services=self.services,
        )

        self.assertEqual((code, stderr), (0, ""))
        annotation = self.services.calls[0][1]
        self.assertEqual((annotation.kind.value, annotation.outcome.value), ("finish", "success"))

    def test_rejects_forbidden_and_malformed_json_payloads_privately(self) -> None:
        private = "private-note-and-key"
        cases = (
            (["annotate"], json.dumps({**VALID_ANNOTATION, "input_tokens": 12, "note": private})),
            (["annotate"], "not-json-" + private),
            (["annotate"], json.dumps({**VALID_ANNOTATION, "unexpected": private})),
        )
        for argv, stdin in cases:
            with self.subTest(argv=argv, stdin=stdin):
                code, stdout, stderr = invoke(
                    argv, stdin=stdin, environ=self.environment, services=self.services,
                )
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "hydra-codex: validation failed\n")
                self.assertNotIn(private, stderr)

    def test_missing_capability_is_private_domain_failure(self) -> None:
        code, stdout, stderr = invoke(
            ["annotate"], stdin=json.dumps(VALID_ANNOTATION), services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (1, "", "hydra-codex: validation failed\n"))


class ServiceDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = FakeServices()

    def test_reconcile_report_and_compare_delegate_with_formats(self) -> None:
        code, stdout, stderr = invoke(
            ["reconcile", "--cwd", "."], services=self.services,
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"command": "reconcile", "status": "ok"})

        code, stdout, stderr = invoke(
            ["report", "--last", "3", "--format", "markdown"], services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered report\n", ""))

        code, stdout, stderr = invoke(
            ["compare", "htask_a", "htask_b", "--format", "html"], services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered comparison\n", ""))
        self.assertEqual(self.services.calls[-1][:4], ("compare", "htask_a", "htask_b", "html"))

    def test_pilot_commands_delegate_public_lifecycle_arguments(self) -> None:
        code, stdout, stderr = invoke(
            [
                "pilot", "start", "--target", "5",
                "--task-family", "telemetry-analysis", "--db", "pilot.sqlite3",
            ],
            services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered pilot\n", ""))
        self.assertEqual(
            self.services.calls[-1][:3],
            ("pilot_start", 5, "telemetry-analysis"),
        )

        code, stdout, stderr = invoke(
            ["pilot", "status", "--format", "html"], services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered pilot\n", ""))
        self.assertEqual(self.services.calls[-1][0:2], ("pilot_status", "html"))

        code, stdout, stderr = invoke(
            [
                "pilot", "close", "--pilot", "hpilot_v1_public",
                "--audit-json", "audit.json", "--decision", "verified",
            ],
            services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered pilot\n", ""))
        self.assertEqual(
            self.services.calls[-1][0:4],
            ("pilot_close", "hpilot_v1_public", Path("audit.json"), "verified"),
        )

    def test_audit_delegates_pilot_format_and_uses_atomic_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "audit.html"

            code, stdout, stderr = invoke(
                [
                    "audit", "--pilot", "hpilot_v1_public", "--format", "html",
                    "--output", str(target), "--db", "audit.sqlite3", "--cwd", ".",
                ],
                services=self.services,
            )

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            self.assertEqual(target.read_text(encoding="utf-8"), "rendered audit")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(
                self.services.calls[-1][:3],
                ("audit", "hpilot_v1_public", "html"),
            )

    def test_service_errors_do_not_echo_exception_paths_or_secrets(self) -> None:
        private = "/private/database.sqlite3 capability-secret raw-note"
        self.services.failure = RuntimeError(private)

        code, stdout, stderr = invoke(
            ["report", "--last", "1"], services=self.services,
        )

        self.assertEqual((code, stdout, stderr), (1, "", "hydra-codex: command failed\n"))
        self.assertNotIn(private, stderr)


class AtomicOutputTests(unittest.TestCase):
    def test_report_atomically_overwrites_with_mode_0600(self) -> None:
        services = FakeServices()
        services.report_content = "new private-safe report"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "report.md"
            target.write_text("old", encoding="utf-8")
            target.chmod(0o644)

            code, stdout, stderr = invoke(
                ["report", "--last", "1", "--output", str(target)], services=services,
            )

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            self.assertEqual(target.read_text(encoding="utf-8"), services.report_content)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(list(target.parent.glob(".hydra-output-*")), [])

    def test_failed_replace_preserves_old_target_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "report.json"
            target.write_text("old", encoding="utf-8")
            with mock.patch("hydra_codex.cli.os.replace", side_effect=OSError("private-path")):
                with self.assertRaises(OSError):
                    atomic_write(target, "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(target.parent.glob(".hydra-output-*")), [])

    def test_fchmod_failure_closes_descriptor_preserves_target_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "report.json"
            target.write_text("old", encoding="utf-8")
            with (
                mock.patch("hydra_codex.cli.os.fchmod", side_effect=OSError("denied")),
                mock.patch("hydra_codex.cli.os.close", wraps=os.close) as close,
            ):
                with self.assertRaises(OSError):
                    atomic_write(target, "new")

            close.assert_called_once()
            with self.assertRaises(OSError):
                os.fstat(close.call_args.args[0])
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(target.parent.glob(".hydra-output-*")), [])

    def test_fdopen_failure_closes_descriptor_preserves_target_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "report.json"
            target.write_text("old", encoding="utf-8")
            with (
                mock.patch("hydra_codex.cli.os.fdopen", side_effect=OSError("denied")),
                mock.patch("hydra_codex.cli.os.close", wraps=os.close) as close,
            ):
                with self.assertRaises(OSError):
                    atomic_write(target, "new")

            close.assert_called_once()
            with self.assertRaises(OSError):
                os.fstat(close.call_args.args[0])
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(target.parent.glob(".hydra-output-*")), [])

    def test_descriptor_close_error_does_not_mask_failure_or_leave_temp(self) -> None:
        real_close = os.close

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            raise OSError("close failed")

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "report.json"
            target.write_text("old", encoding="utf-8")
            with (
                mock.patch("hydra_codex.cli.os.fchmod", side_effect=OSError("denied")),
                mock.patch("hydra_codex.cli.os.close", side_effect=close_then_fail),
            ):
                with self.assertRaises(OSError) as raised:
                    atomic_write(target, "new")

            self.assertEqual(str(raised.exception), "denied")
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(target.parent.glob(".hydra-output-*")), [])

    def test_write_failure_closes_handle_preserves_target_and_removes_temp(self) -> None:
        class FailingHandle:
            def __init__(self, descriptor: int) -> None:
                self.descriptor = descriptor
                self.closed = False

            def close(self):
                os.close(self.descriptor)
                self.closed = True

            def write(self, _content: str) -> None:
                raise OSError("write failed")

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "report.json"
            target.write_text("old", encoding="utf-8")
            handles: list[FailingHandle] = []

            def failing_fdopen(descriptor, *_args, **_kwargs):
                handle = FailingHandle(descriptor)
                handles.append(handle)
                return handle

            with mock.patch("hydra_codex.cli.os.fdopen", side_effect=failing_fdopen):
                with self.assertRaises(OSError):
                    atomic_write(target, "new")

            self.assertTrue(handles[0].closed)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(target.parent.glob(".hydra-output-*")), [])


class IngestCliTests(unittest.TestCase):
    def create_project(self, base: Path) -> Path:
        project = base / "project"
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text(
            'project_id = "cli-project"\n', encoding="utf-8",
        )
        return project

    def test_missing_default_roots_is_successful_zero_source_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.create_project(base)
            database = base / "hydra.sqlite3"

            code, stdout, stderr = invoke([
                "ingest", "--cwd", str(project), "--db", str(database),
            ], environ={"HOME": str(base / "empty-home")})

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(json.loads(stdout), {
                "command": "ingest", "diagnostics": 0, "files_seen": 0,
                "status": "ok", "unique_sources": 0,
            })

    def test_zero_source_ingest_keeps_db_source_and_project_directories_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.create_project(base)
            project.chmod(0o755)
            home = base / "home"
            active = home / ".codex" / "sessions"
            active.mkdir(parents=True)
            active.chmod(0o755)
            database_parent = base / "shared-db"
            database_parent.mkdir()
            database_parent.chmod(0o755)

            code, stdout, stderr = invoke([
                "ingest", "--cwd", str(project),
                "--db", str(database_parent / "hydra.sqlite3"),
            ], environ={"HOME": str(home)})

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["files_seen"], 0)
            self.assertEqual(stat.S_IMODE(project.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(active.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(database_parent.stat().st_mode), 0o755)
            self.assertEqual(
                sorted(path.relative_to(project).as_posix() for path in project.rglob("*")),
                [".hydra", ".hydra/project.toml"],
            )
            self.assertEqual(list(active.iterdir()), [])
            self.assertNotIn("rollout-hmac.key", {path.name for path in database_parent.iterdir()})
            installation_key = home / "Library" / "Application Support" / "Hydra" / "rollout-hmac.key"
            self.assertTrue(installation_key.is_file())
            self.assertEqual(stat.S_IMODE(installation_key.stat().st_mode), 0o600)

    def test_ingest_can_use_an_injected_installation_key_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.create_project(base)
            home = base / "home"
            key_path = base / "private-keys" / "custom-hydra.key"
            database_parent = base / "database"
            database_parent.mkdir()

            code, stdout, stderr = invoke(
                [
                    "ingest", "--cwd", str(project),
                    "--db", str(database_parent / "hydra.sqlite3"),
                ],
                environ={"HOME": str(home)},
                installation_key_path=key_path,
            )

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["files_seen"], 0)
            self.assertTrue(key_path.is_file())
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
            self.assertFalse(
                (home / "Library" / "Application Support" / "Hydra" / "rollout-hmac.key").exists(),
            )

    def test_explicit_and_default_sources_use_existing_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.create_project(base)
            home = base / "home"
            active = home / ".codex" / "sessions"
            archived = home / ".codex" / "archived_sessions"
            active.mkdir(parents=True)
            archived.mkdir(parents=True)
            event = {
                "timestamp": "2026-07-21T00:00:00Z", "type": "session_meta",
                "payload": {"id": "thread", "cwd": str(project)},
            }
            (active / "active.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            (archived / "archived.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            explicit = base / "explicit.jsonl"
            explicit.write_text(json.dumps(event) + "\n", encoding="utf-8")

            code, stdout, stderr = invoke([
                "ingest", "--cwd", str(project), "--db", str(base / "hydra.sqlite3"),
                "--source", "explicit=" + str(explicit),
            ], environ={"HOME": str(home)})

            self.assertEqual((code, stderr), (0, ""))
            summary = json.loads(stdout)
            self.assertEqual(summary["files_seen"], 3)
            self.assertEqual(summary["unique_sources"], 1)

    def test_unavailable_database_and_missing_explicit_source_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.create_project(base)
            private_db = base / "missing" / "private.sqlite3"
            private_source = base / "missing-private-rollout.jsonl"
            cases = (
                ["ingest", "--cwd", str(project), "--db", str(private_db)],
                [
                    "ingest", "--cwd", str(project), "--db", str(base / "ok.sqlite3"),
                    "--source", "explicit=" + str(private_source),
                ],
            )
            for argv in cases:
                with self.subTest(argv=argv):
                    code, stdout, stderr = invoke(argv, environ={"HOME": str(base / "home")})
                    self.assertEqual((code, stdout), (1, ""))
                    self.assertIn(stderr, {
                        "hydra-codex: storage unavailable\n",
                        "hydra-codex: validation failed\n",
                    })
                    self.assertNotIn(str(private_db), stderr)
                    self.assertNotIn(str(private_source), stderr)


if __name__ == "__main__":
    unittest.main()
