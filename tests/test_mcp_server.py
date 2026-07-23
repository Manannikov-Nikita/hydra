from __future__ import annotations

from dataclasses import dataclass
import io
import json
import sys
import threading
import time
import unittest
from unittest.mock import patch

from hydra_codex.mcp_server import (
    ProcessResult,
    StdioMcpServer,
    SubprocessRunner,
    serve,
)


@dataclass
class FakeRunner:
    results: list[ProcessResult]

    def __post_init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run(self, command: tuple[str, ...], stdin_text: str | None = None) -> ProcessResult:
        self.calls.append((command, stdin_text))
        return self.results.pop(0)


def request(identifier: int, method: str, params: object | None = None) -> dict[str, object]:
    value: dict[str, object] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        value["params"] = params
    return value


class StdioMcpServerTests(unittest.TestCase):
    def test_injected_runtime_prefix_is_used_for_every_internal_command(self) -> None:
        runner = FakeRunner([
            ProcessResult(0, '{"command":"ingest","status":"ok"}\n', ""),
            ProcessResult(0, '{"status":"ok"}\n', ""),
            ProcessResult(0, "{}\n", ""),
        ])
        prefix = ("/trusted/python", "-m", "hydra_codex")
        server = StdioMcpServer(runner, command_prefix=prefix)

        response = server.handle(request(30, "tools/call", {
            "name": "hydra.report",
            "arguments": {"last": 1, "format": "json"},
        }))

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(
            [call[0][:3] for call in runner.calls],
            [prefix, prefix, prefix],
        )
        self.assertEqual(
            [call[0][3] for call in runner.calls],
            ["ingest", "reconcile", "report"],
        )

    def test_subprocess_runner_is_shell_free_bounded_and_times_out(self) -> None:
        runner = SubprocessRunner(
            timeout_seconds=1,
            output_max_bytes=1_024,
        )
        command = (
            sys.executable,
            "-c",
            "import sys; "
            "sys.stdout.write('x' * 200000); "
            "sys.stderr.write('y' * 200000)",
        )
        from hydra_codex import mcp_server

        with patch(
            "hydra_codex.mcp_server.subprocess.Popen",
            wraps=mcp_server.subprocess.Popen,
        ) as popen:
            result = runner.run(command)

        self.assertEqual(len(result.stdout.encode("utf-8")), 1_024)
        self.assertEqual(len(result.stderr.encode("utf-8")), 1_024)
        self.assertFalse(popen.call_args.kwargs["shell"])

        timed_out = SubprocessRunner(timeout_seconds=0.05).run((
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
        ))
        self.assertEqual(timed_out, ProcessResult(1, "", ""))

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "process-group cleanup is required on macOS and Linux",
    )
    def test_subprocess_timeout_bounds_inherited_pipe_cleanup(self) -> None:
        runner = SubprocessRunner(timeout_seconds=0.1)
        command = (
            sys.executable,
            "-c",
            "import subprocess, sys; "
            "sys.stdout.write('private child output'); sys.stdout.flush(); "
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(2)'])",
        )

        started = time.monotonic()
        result = runner.run(command)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.75)
        self.assertEqual(result, ProcessResult(1, "", ""))

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "process-group cleanup is required on macOS and Linux",
    )
    def test_subprocess_timeout_preserves_worker_descriptor_ownership(self) -> None:
        class DelayedCloseRunner(SubprocessRunner):
            def __init__(self) -> None:
                # Hosted x86 runners can spend more than 300 ms starting a
                # Python child under load.  This test exercises descriptor
                # ownership, not the minimum timeout threshold.
                super().__init__(timeout_seconds=1.0)
                self.allow_owned_close = threading.Event()
                self._close_condition = threading.Condition()
                self._delayed_closes = 0

            def _close(self, stream) -> None:
                with self._close_condition:
                    should_delay = self._delayed_closes < 2
                    if should_delay:
                        self._delayed_closes += 1
                        self._close_condition.notify_all()
                if should_delay:
                    self.allow_owned_close.wait(timeout=2)
                super()._close(stream)

            def wait_for_delayed_closes(self) -> bool:
                with self._close_condition:
                    return self._close_condition.wait_for(
                        lambda: self._delayed_closes == 2,
                        timeout=1,
                    )

        runner = DelayedCloseRunner()
        inherited_pipe_command = (
            sys.executable,
            "-c",
            "import subprocess, sys; "
            "sys.stdout.write('private child output'); sys.stdout.flush(); "
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(2)'])",
        )
        normal_capture_command = (
            sys.executable,
            "-c",
            "import sys, time; time.sleep(0.1); "
            "sys.stdout.write('normal stdout\\n'); sys.stdout.flush(); "
            "sys.stderr.write('normal stderr\\n'); sys.stderr.flush()",
        )
        capture: dict[str, ProcessResult] = {}
        second_process_started = threading.Event()
        popen_calls = 0
        popen_lock = threading.Lock()
        from hydra_codex import mcp_server

        real_popen = mcp_server.subprocess.Popen

        def tracked_popen(*args, **kwargs):
            nonlocal popen_calls
            process = real_popen(*args, **kwargs)
            with popen_lock:
                popen_calls += 1
                if popen_calls == 2:
                    second_process_started.set()
            return process

        capture_thread = threading.Thread(
            target=lambda: capture.setdefault(
                "result",
                runner.run(normal_capture_command),
            ),
        )
        try:
            with patch(
                "hydra_codex.mcp_server.subprocess.Popen",
                side_effect=tracked_popen,
            ):
                self.assertEqual(
                    runner.run(inherited_pipe_command),
                    ProcessResult(1, "", ""),
                )
                self.assertTrue(runner.wait_for_delayed_closes())
                capture_thread.start()
                self.assertTrue(second_process_started.wait(timeout=2))
                runner.allow_owned_close.set()
                capture_thread.join(timeout=2)
        finally:
            runner.allow_owned_close.set()
            if capture_thread.is_alive():
                capture_thread.join(timeout=2)

        self.assertFalse(capture_thread.is_alive())
        self.assertEqual(
            capture.get("result"),
            ProcessResult(0, "normal stdout\n", "normal stderr\n"),
        )
        self.assertEqual(
            [
                thread.name
                for thread in threading.enumerate()
                if thread.name.startswith("hydra-codex-pipe-")
            ],
            [],
        )

    def test_initialize_and_tool_list_are_stable(self) -> None:
        server = StdioMcpServer(FakeRunner([]))
        initialized = server.handle(request(1, "initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        tools = server.handle(request(2, "tools/list"))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["hydra.report"])
        report_schema = tools[0]["inputSchema"]
        self.assertEqual(
            set(report_schema["properties"]), {"last", "pilot", "format"},
        )
        self.assertEqual(
            report_schema["oneOf"],
            [{"required": ["last"]}, {"required": ["pilot"]}],
        )

        trusted = StdioMcpServer(FakeRunner([]), annotation_enabled=True)
        tools = trusted.handle(request(12, "tools/list"))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["hydra.annotate", "hydra.report"])
        annotation_schema = tools[0]["inputSchema"]
        self.assertFalse(annotation_schema["additionalProperties"])
        for forbidden in ("tokens", "session_id", "turn_id", "timestamp"):
            self.assertNotIn(forbidden, annotation_schema["properties"])

        unsupported = server.handle(request(10, "initialize", {"protocolVersion": "future-version"}))
        self.assertEqual(unsupported["result"]["protocolVersion"], "2025-06-18")

    def test_annotation_is_validated_and_forwarded_only_as_json(self) -> None:
        runner = FakeRunner([ProcessResult(0, '{"status":"ok"}\n', "")])
        server = StdioMcpServer(
            runner, executable="hydra-test", annotation_enabled=True,
        )
        arguments = {
            "kind": "phase",
            "phase": "implement",
            "cause": "plan",
            "scope_change": "none",
            "task_family": "telemetry",
            "confidence": 0.9,
            "note": "Wire the command service.",
        }
        response = server.handle(request(3, "tools/call", {
            "name": "hydra.annotate", "arguments": arguments,
        }))
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(runner.calls[0][0], ("hydra-test", "annotate"))
        self.assertEqual(json.loads(runner.calls[0][1]), arguments)

    def test_annotation_rejects_identity_and_measurement_fields_without_running(self) -> None:
        runner = FakeRunner([])
        response = StdioMcpServer(runner, annotation_enabled=True).handle(request(4, "tools/call", {
            "name": "hydra.annotate",
            "arguments": {
                "kind": "phase", "phase": "test_full", "cause": "plan",
                "scope_change": "none", "task_family": "telemetry",
                "confidence": 1, "note": "Full checks", "session_id": "secret",
            },
        }))
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(runner.calls, [])
        self.assertNotIn("secret", response["result"]["content"][0]["text"])

    def test_annotation_is_not_callable_without_trusted_turn_transport(self) -> None:
        runner = FakeRunner([])
        response = StdioMcpServer(runner).handle(request(14, "tools/call", {
            "name": "hydra.annotate", "arguments": {
                "kind": "phase", "phase": "implement", "cause": "plan",
                "scope_change": "none", "task_family": "telemetry",
                "confidence": 1, "note": "must not run",
            },
        }))

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(runner.calls, [])

    def test_report_ingests_reconciles_then_renders_and_never_accepts_paths(self) -> None:
        runner = FakeRunner([
            ProcessResult(0, '{"command":"ingest","status":"ok"}\n', ""),
            ProcessResult(0, '{"status":"ok"}\n', ""),
            ProcessResult(0, "# Hydra task report\n", ""),
        ])
        server = StdioMcpServer(runner, executable="hydra-test")
        response = server.handle(request(5, "tools/call", {
            "name": "hydra.report", "arguments": {"last": 5, "format": "markdown"},
        }))
        self.assertEqual(runner.calls, [
            (("hydra-test", "ingest"), None),
            (("hydra-test", "reconcile"), None),
            (("hydra-test", "report", "--last", "5", "--format", "markdown"), None),
        ])
        self.assertEqual(response["result"]["content"][0]["text"], "# Hydra task report\n")
        invalid = server.handle(request(6, "tools/call", {
            "name": "hydra.report", "arguments": {"last": 1, "cwd": "/tmp/private"},
        }))
        self.assertTrue(invalid["result"]["isError"])

    def test_report_fails_closed_when_fresh_ingest_fails(self) -> None:
        runner = FakeRunner([ProcessResult(1, "", "private ingest failure")])

        response = StdioMcpServer(runner, executable="hydra-test").handle(request(
            15, "tools/call", {
                "name": "hydra.report", "arguments": {"last": 1, "format": "json"},
            },
        ))

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(runner.calls, [(("hydra-test", "ingest"), None)])
        self.assertNotIn("private", response["result"]["content"][0]["text"])

    def test_report_audit_mode_delegates_to_one_shot_audit_command(self) -> None:
        pilot_id = "hpilot_v1_0123456789abcdef0123456789abcdef"
        runner = FakeRunner([
            ProcessResult(0, '{"schema_version":"hydra.audit/v1"}\n', ""),
        ])
        server = StdioMcpServer(runner, executable="hydra-test")

        response = server.handle(request(16, "tools/call", {
            "name": "hydra.report",
            "arguments": {"pilot": pilot_id, "format": "json"},
        }))

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(runner.calls, [(
            ("hydra-test", "audit", "--pilot", pilot_id, "--format", "json"),
            None,
        )])

    def test_report_modes_are_mutually_exclusive_and_pilot_is_opaque(self) -> None:
        runner = FakeRunner([])
        server = StdioMcpServer(runner)
        cases = (
            {},
            {"last": 1, "pilot": "hpilot_v1_0123456789abcdef0123456789abcdef"},
            {"pilot": "raw-project-or-path"},
        )

        for index, arguments in enumerate(cases, start=20):
            with self.subTest(arguments=arguments):
                response = server.handle(request(index, "tools/call", {
                    "name": "hydra.report", "arguments": arguments,
                }))
                self.assertTrue(response["result"]["isError"])
        self.assertEqual(runner.calls, [])

    def test_subprocess_errors_are_generic_and_do_not_echo_stderr(self) -> None:
        runner = FakeRunner([ProcessResult(1, "", "raw secret from database")])
        response = StdioMcpServer(runner, annotation_enabled=True).handle(request(7, "tools/call", {
            "name": "hydra.annotate",
            "arguments": {
                "kind": "finish", "phase": "implement", "cause": "plan",
                "scope_change": "none", "task_family": "telemetry",
                "confidence": 0.8, "note": "Done", "outcome": "success",
            },
        }))
        text = response["result"]["content"][0]["text"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(text, "Hydra command failed.")
        self.assertNotIn("secret", text)

    def test_jsonl_loop_ignores_notifications_and_survives_invalid_messages(self) -> None:
        input_stream = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + "not json\n"
            + json.dumps(request(9, "ping")) + "\n"
        )
        output_stream = io.StringIO()
        serve(input_stream, output_stream, StdioMcpServer(FakeRunner([])))
        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_jsonl_loop_turns_unexpected_handler_failures_into_generic_errors(self) -> None:
        class BrokenServer:
            def handle(self, _message):
                raise RuntimeError("private implementation detail")

        output_stream = io.StringIO()
        serve(io.StringIO(json.dumps(request(11, "ping")) + "\n"), output_stream, BrokenServer())
        response = json.loads(output_stream.getvalue())
        self.assertEqual(response["error"], {"code": -32603, "message": "Internal error"})
        self.assertNotIn("private", output_stream.getvalue())


if __name__ == "__main__":
    unittest.main()
