from __future__ import annotations

from contextlib import nullcontext
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

from hydra_codex import __version__
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
        self.doctor_content = "rendered doctor"
        self.storage_content = "rendered storage"
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

    def doctor(self, output_format, database_path, cwd):
        self._fail()
        self.calls.append(("doctor", output_format, database_path, cwd))
        return self.doctor_content

    def storage_status(self, output_format, database_path, cwd):
        self._fail()
        self.calls.append(("storage_status", output_format, database_path, cwd))
        return self.storage_content

    def storage_compact(self, output_format, database_path, cwd):
        self._fail()
        self.calls.append(("storage_compact", output_format, database_path, cwd))
        return self.storage_content


def invoke(
    argv, *, stdin="", environ=None, services=None, installation_key_path=None,
    codex_client=None, integration_receipt_path=None, marketplace_root=None,
    verified_candidate=None,
):
    stdout = io.StringIO()
    stderr = io.StringIO()
    input_stream = io.StringIO(stdin) if isinstance(stdin, str) else stdin
    key_options = (
        {"installation_key_path": installation_key_path}
        if installation_key_path is not None else {}
    )
    integration_options = {}
    if codex_client is not None:
        integration_options["codex_client"] = codex_client
    if integration_receipt_path is not None:
        integration_options["integration_receipt_path"] = integration_receipt_path
    if marketplace_root is not None:
        integration_options["marketplace_root"] = marketplace_root
    if verified_candidate is not None:
        integration_options["verified_candidate"] = verified_candidate
    code = main(
        argv,
        stdin=input_stream,
        stdout=stdout,
        stderr=stderr,
        environ={} if environ is None else environ,
        services=services,
        **key_options,
        **integration_options,
    )
    return code, stdout.getvalue(), stderr.getvalue()


class NoReadStdin:
    def isatty(self):
        raise AssertionError("flag annotation must not inspect stdin")

    def read(self):
        raise AssertionError("flag annotation must not read stdin")


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class CodexInstallationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_codex_integration import StatefulCodexClient

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.marketplace = self.root / "marketplace"
        self.marketplace.mkdir()
        self.receipt = self.root / "private" / "codex-integration.json"
        self.client = StatefulCodexClient()
        self.client.available_versions[self.marketplace.resolve()] = __version__

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, argv, *, stdin=""):
        return invoke(
            argv,
            stdin=stdin,
            environ={"HOME": str(self.home)},
            codex_client=self.client,
            integration_receipt_path=self.receipt,
            marketplace_root=self.marketplace,
        )

    def test_print_config_is_read_only_without_codex_or_confirmation(self) -> None:
        class UnusableClient:
            def version(self):
                raise AssertionError("Codex client must not be used")

        code, stdout, stderr = invoke(
            ["install", "--print-config", "codex"],
            stdin=NoReadStdin(),
            environ={"HOME": str(self.home)},
            codex_client=UnusableClient(),
            integration_receipt_path=self.receipt,
            marketplace_root=self.marketplace,
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("codex plugin marketplace add", stdout)
        self.assertIn("hydra-codex@hydra", stdout)
        self.assertEqual(list(self.root.rglob("codex-integration.json")), [])

    def test_noninteractive_install_requires_yes_and_never_mutates(self) -> None:
        code, stdout, stderr = self.call(["install"])

        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "hydra-codex: confirmation required\n")
        self.assertEqual(self.client.calls, [])

    def test_confirmed_install_and_repeat_are_idempotent(self) -> None:
        first = self.call(["install", "-y"])
        self.client.calls.clear()
        second = self.call(["install", "-y"])

        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        self.assertEqual(json.loads(first[1])["changed"], True)
        self.assertEqual(json.loads(second[1])["changed"], False)
        self.assertEqual(self.client.calls, [])

    def test_interactive_confirmation_allows_install(self) -> None:
        confirmed_input = TtyBuffer("yes\n")

        code, stdout, stderr = self.call(["install"], stdin=confirmed_input)

        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["changed"])
        self.assertIn("Install Hydra into Codex?", stderr)

    def test_uninstall_keep_cli_detaches_only_owned_integration(self) -> None:
        self.call(["install", "-y"])
        data = self.root / "telemetry.sqlite3"
        data.write_bytes(b"preserve")

        code, stdout, stderr = self.call(["uninstall", "-y", "--keep-cli"])

        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["changed"])
        self.assertEqual(data.read_bytes(), b"preserve")
        self.assertFalse(self.receipt.exists())

    def test_uninstall_does_not_require_the_current_marketplace_bundle(self) -> None:
        self.call(["install", "-y"])

        with mock.patch(
            "hydra_codex.installation_cli.marketplace_root_path",
            side_effect=FileNotFoundError("private missing bundle"),
        ):
            code, stdout, stderr = invoke(
                ["uninstall", "-y", "--keep-cli"],
                environ={"HOME": str(self.home)},
                codex_client=self.client,
                integration_receipt_path=self.receipt,
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["changed"])
        self.assertFalse(self.receipt.exists())


class ReleaseLifecycleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_codex_integration import StatefulCodexClient

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environ = {"HOME": str(self.home)}
        self.receipt = self.root / "private" / "codex-integration.json"
        self.client = StatefulCodexClient()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_upgrade_check_is_read_only_path_free_and_reports_candidate(self) -> None:
        from hydra_codex.release_acquisition import ResolvedRelease
        from tests.test_release_management import _bundle
        from hydra_codex.release_management import (
            activate_version,
            default_install_roots,
        )

        roots = default_install_roots(self.home)
        activate_version(
            _bundle(self.root, "0.1.0"),
            roots=roots,
            environ=self.environ,
        )
        before = tuple(
            sorted(
                (path.relative_to(self.root).as_posix(), path.lstat().st_size)
                for path in self.root.rglob("*")
            ),
        )

        with mock.patch(
            "hydra_codex.installation_cli.resolve_latest_release",
            return_value=ResolvedRelease("0.1.0", "0.2.0"),
        ) as resolve, mock.patch(
            "hydra_codex.installation_cli.acquire_release_candidate",
            side_effect=AssertionError("check must not download a release"),
        ) as acquire:
            code, stdout, stderr = invoke(
                ["upgrade", "--check"],
                environ=self.environ,
                codex_client=self.client,
                integration_receipt_path=self.receipt,
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout.count("\n"), 1)
        self.assertEqual(json.loads(stdout), {
            "command": "upgrade",
            "current_version": "0.1.0",
            "latest_version": "0.2.0",
            "status": "ok",
            "update_available": True,
        })
        after = tuple(
            sorted(
                (path.relative_to(self.root).as_posix(), path.lstat().st_size)
                for path in self.root.rglob("*")
            ),
        )
        self.assertEqual(after, before)
        self.assertNotIn(str(self.root), stdout)
        self.assertEqual(self.client.calls, [])
        resolve.assert_called_once_with(environ=self.environ)
        acquire.assert_not_called()

    def test_upgrade_refreshes_codex_to_the_activated_candidate(self) -> None:
        from hydra_codex.release_acquisition import AcquiredRelease
        from tests.test_release_management import _bundle
        from hydra_codex.release_management import (
            activate_version,
            default_install_roots,
        )

        roots = default_install_roots(self.home)
        first = _bundle(self.root, __version__)
        activate_version(first, roots=roots, environ=self.environ)
        active_marketplace = roots.current.resolve() / "marketplace"
        self.client.available_versions[active_marketplace.resolve()] = __version__
        installed = invoke(
            ["install", "-y"],
            environ=self.environ,
            codex_client=self.client,
            integration_receipt_path=self.receipt,
            marketplace_root=active_marketplace,
        )
        self.assertEqual(installed[0], 0)
        candidate = _bundle(self.root, "0.2.0")
        self.client.available_versions[
            (roots.versions / "0.2.0" / "marketplace").resolve()
        ] = "0.2.0"

        with mock.patch(
            "hydra_codex.installation_cli.acquire_release_candidate",
            return_value=nullcontext(AcquiredRelease(candidate, __version__)),
        ):
            code, stdout, stderr = invoke(
                ["upgrade"],
                environ=self.environ,
                codex_client=self.client,
                integration_receipt_path=self.receipt,
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {
            "command": "upgrade",
            "current_version": "0.2.0",
            "latest_version": "0.2.0",
            "status": "ok",
            "update_available": False,
        })
        self.assertEqual(roots.current.resolve().name, "0.2.0")
        self.assertEqual(self.client.installed_version, "0.2.0")
        self.assertNotIn(str(self.root), stdout)

    def test_upgrade_acquisition_failure_is_generic_and_path_free(self) -> None:
        from hydra_codex.release_acquisition import ReleaseAcquisitionError
        from tests.test_release_management import _bundle
        from hydra_codex.release_management import (
            activate_version,
            default_install_roots,
        )

        roots = default_install_roots(self.home)
        activate_version(
            _bundle(self.root, "0.1.0"),
            roots=roots,
            environ=self.environ,
        )
        private = str(self.root / "private-acquisition-path")
        with mock.patch(
            "hydra_codex.installation_cli.acquire_release_candidate",
            side_effect=ReleaseAcquisitionError(private),
        ):
            code, stdout, stderr = invoke(
                ["upgrade"],
                environ=self.environ,
                codex_client=self.client,
                integration_receipt_path=self.receipt,
            )

        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "hydra-codex: command failed\n")
        self.assertNotIn(private, stderr)

    def test_public_upgrade_rolls_back_refresh_failure_then_retries(self) -> None:
        from hydra_codex.release_acquisition import AcquiredRelease
        from tests.test_release_management import _bundle
        from hydra_codex.release_management import (
            activate_version,
            default_install_roots,
        )

        roots = default_install_roots(self.home)
        first = _bundle(self.root, __version__)
        activate_version(first, roots=roots, environ=self.environ)
        active_marketplace = roots.current.resolve() / "marketplace"
        self.client.available_versions[active_marketplace.resolve()] = __version__
        self.assertEqual(
            invoke(
                ["install", "-y"],
                environ=self.environ,
                codex_client=self.client,
                integration_receipt_path=self.receipt,
                marketplace_root=active_marketplace,
            )[0],
            0,
        )

        candidate = _bundle(self.root, "0.2.0")
        installed_marketplace = roots.versions / "0.2.0" / "marketplace"
        self.client.available_versions[installed_marketplace.resolve()] = "0.2.0"
        self.client.fail_on = ("add_plugin", "hydra-codex@hydra")
        with mock.patch(
            "hydra_codex.installation_cli.acquire_release_candidate",
            return_value=nullcontext(AcquiredRelease(candidate, __version__)),
        ):
            failed = invoke(
                ["upgrade"],
                environ=self.environ,
                codex_client=self.client,
                integration_receipt_path=self.receipt,
            )

        self.assertEqual(
            failed,
            (1, "", "hydra-codex: command failed\n"),
        )
        self.assertEqual(roots.current.resolve().name, __version__)
        self.assertEqual(self.client.installed_version, __version__)
        self.assertEqual(
            json.loads(self.receipt.read_text(encoding="utf-8"))[
                "runtime_version"
            ],
            __version__,
        )
        release_journal = roots.home / "release-journal.json"
        self.assertEqual(
            json.loads(release_journal.read_text(encoding="utf-8"))["phase"],
            "refreshing",
        )

        retry = _bundle(self.root, "0.2.0", marker="retry")
        with mock.patch(
            "hydra_codex.installation_cli.acquire_release_candidate",
            return_value=nullcontext(AcquiredRelease(retry, __version__)),
        ):
            code, stdout, stderr = invoke(
                ["upgrade"],
                environ=self.environ,
                codex_client=self.client,
                integration_receipt_path=self.receipt,
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["current_version"], "0.2.0")
        self.assertEqual(roots.current.resolve().name, "0.2.0")
        self.assertEqual(self.client.installed_version, "0.2.0")
        self.assertEqual(
            json.loads(self.receipt.read_text(encoding="utf-8"))[
                "runtime_version"
            ],
            "0.2.0",
        )
        self.assertFalse(release_journal.exists())

    def test_full_uninstall_detaches_then_removes_owned_cli(self) -> None:
        from tests.test_release_management import _bundle
        from hydra_codex.release_management import (
            activate_version,
            default_install_roots,
        )

        roots = default_install_roots(self.home)
        first = _bundle(self.root, __version__)
        activate_version(first, roots=roots, environ=self.environ)
        active_marketplace = roots.current.resolve() / "marketplace"
        self.client.available_versions[active_marketplace.resolve()] = __version__
        installed = invoke(
            ["install", "-y"],
            environ=self.environ,
            codex_client=self.client,
            integration_receipt_path=self.receipt,
            marketplace_root=active_marketplace,
        )
        self.assertEqual(installed[0], 0)

        code, stdout, stderr = invoke(
            ["uninstall", "-y"],
            environ=self.environ,
            codex_client=self.client,
            integration_receipt_path=self.receipt,
        )

        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(
            (payload["command"], payload["keep_cli"], payload["status"]),
            ("uninstall", False, "ok"),
        )
        self.assertTrue(payload["changed"])
        self.assertFalse(roots.current.exists())
        self.assertFalse(roots.current.is_symlink())
        self.assertFalse(roots.launcher.exists())
        self.assertFalse(self.receipt.exists())


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
            ["doctor", "--format", "html"],
            ["storage", "compact", "--confirmation", "wrong"],
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
        for command in (
            "ingest", "annotate", "reconcile", "report", "compare", "audit",
            "doctor", "storage", "init", "status", "uninit", "install",
            "uninstall", "upgrade",
        ):
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

    def test_doctor_and_storage_commands_delegate_safe_arguments(self) -> None:
        code, stdout, stderr = invoke(
            ["doctor", "--format", "markdown"], services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered doctor\n", ""))
        self.assertEqual(self.services.calls[-1][:2], ("doctor", "markdown"))

        code, stdout, stderr = invoke(
            ["storage", "status", "--format", "json"], services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered storage\n", ""))
        self.assertEqual(self.services.calls[-1][:2], ("storage_status", "json"))

        code, stdout, stderr = invoke(
            [
                "storage", "compact", "--confirmation", "compact hydra database",
                "--format", "markdown",
            ],
            services=self.services,
        )
        self.assertEqual((code, stdout, stderr), (0, "rendered storage\n", ""))
        self.assertEqual(self.services.calls[-1][:2], ("storage_compact", "markdown"))

    def test_service_errors_do_not_echo_exception_paths_or_secrets(self) -> None:
        private = "/private/database.sqlite3 capability-secret raw-note"
        self.services.failure = RuntimeError(private)

        code, stdout, stderr = invoke(
            ["report", "--last", "1"], services=self.services,
        )

        self.assertEqual((code, stdout, stderr), (1, "", "hydra-codex: command failed\n"))
        self.assertNotIn(private, stderr)


class ProjectLifecycleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_codex_integration import StatefulCodexClient

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.environ = {"HOME": str(self.home)}
        self.codex_client = StatefulCodexClient()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_init_status_and_uninit_round_trip_without_constructing_services(self) -> None:
        services = mock.Mock()

        code, stdout, stderr = invoke(
            ["init", str(self.project), "--name", " Hydra Core "],
            environ=self.environ,
            services=services,
        )
        self.assertEqual((code, stderr), (0, ""))
        initialized = json.loads(stdout)
        self.assertEqual(initialized["command"], "init")
        self.assertEqual(initialized["status"], "ok")
        self.assertTrue(initialized["changed"])
        self.assertRegex(initialized["project_id"], r"^hprj_[0-9a-f]{16}$")

        code, stdout, stderr = invoke(
            ["status", str(self.project), "--json"],
            environ=self.environ,
            services=services,
            codex_client=self.codex_client,
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["project"]["initialized"])

        code, stdout, stderr = invoke(
            [
                "uninit", str(self.project),
                "--confirmation", "remove hydra project",
            ],
            environ=self.environ,
            services=services,
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["changed"])
        self.assertFalse((self.project / ".hydra" / "project.toml").exists())
        services.assert_not_called()

    def test_uninitialized_status_json_is_successful_read_only_and_private(self) -> None:
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

        code, stdout, stderr = invoke(
            ["status", str(self.project), "--json"],
            environ=self.environ,
            codex_client=self.codex_client,
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertFalse(json.loads(stdout)["project"]["initialized"])
        self.assertEqual(
            sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")),
            before,
        )
        self.assertNotIn(str(self.root), stdout)

    def test_status_human_output_is_path_private(self) -> None:
        code, stdout, stderr = invoke(
            ["status", str(self.project)],
            environ=self.environ,
            codex_client=self.codex_client,
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("initialized: no", stdout)
        self.assertNotIn(str(self.root), stdout)

    def test_malformed_project_returns_two_without_leaking_private_path(self) -> None:
        hydra = self.project / ".hydra"
        hydra.mkdir()
        private = str(self.project / "private-secret")
        (hydra / "project.toml").write_text(
            f'project_id = "{private}"\n',
            encoding="utf-8",
        )

        code, stdout, stderr = invoke(
            ["status", str(self.project), "--json"],
            environ=self.environ,
            codex_client=self.codex_client,
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "hydra-codex: invalid project configuration\n")
        self.assertNotIn(str(self.root), stderr)
        self.assertNotIn(private, stderr)

    def test_uninit_confirmation_is_an_argparse_contract(self) -> None:
        for argv in (
            ["uninit", str(self.project)],
            ["uninit", str(self.project), "--confirmation", "wrong"],
        ):
            with self.subTest(argv=argv):
                code, stdout, stderr = invoke(argv, environ=self.environ)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("usage:", stderr)
                self.assertNotIn(str(self.project), stderr)

    def test_init_honors_injected_home_protection(self) -> None:
        code, stdout, stderr = invoke(
            ["init", str(self.home)],
            environ=self.environ,
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "hydra-codex: validation failed\n")
        self.assertFalse((self.home / ".hydra").exists())
        self.assertNotIn(str(self.home), stderr)


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
            'project_id = "hprj_c1c1c1c1c1c1c1c1"\n', encoding="utf-8",
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

    def test_tty_ingest_shows_privacy_safe_progress_without_changing_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.create_project(base)
            home = base / "home"
            active = home / ".codex" / "sessions"
            active.mkdir(parents=True)
            private_name = "private-rollout-name.jsonl"
            event = {
                "timestamp": "2026-07-21T00:00:00Z", "type": "session_meta",
                "payload": {"id": "thread", "cwd": str(project)},
            }
            (active / private_name).write_text(
                json.dumps(event) + "\n", encoding="utf-8",
            )
            output = io.StringIO()
            progress = TtyBuffer()

            code = main(
                [
                    "ingest", "--cwd", str(project),
                    "--db", str(base / "hydra.sqlite3"),
                ],
                stdin=io.StringIO(), stdout=output, stderr=progress,
                environ={"HOME": str(home)},
                installation_key_path=base / "keys" / "rollout-hmac.key",
            )

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["files_seen"], 1)
            rendered = progress.getvalue()
            self.assertIn("hydra-codex: ingest discover 0/1", rendered)
            self.assertIn("hydra-codex: ingest scan 1/1", rendered)
            self.assertIn("hydra-codex: ingest complete 1/1", rendered)
            self.assertNotIn(private_name, rendered)

    def test_linux_zero_source_ingest_keeps_db_source_and_project_directories_unchanged(self) -> None:
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

            with mock.patch("hydra_codex.platform_paths.sys.platform", "linux"):
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
            installation_key = home / ".local" / "share" / "hydra" / "rollout-hmac.key"
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
