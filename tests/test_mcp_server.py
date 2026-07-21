from __future__ import annotations

from dataclasses import dataclass
import io
import json
import unittest

from hydra_codex.mcp_server import ProcessResult, StdioMcpServer, serve


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
    def test_initialize_and_tool_list_are_stable(self) -> None:
        server = StdioMcpServer(FakeRunner([]))
        initialized = server.handle(request(1, "initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        tools = server.handle(request(2, "tools/list"))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["hydra.annotate", "hydra.report"])
        annotation_schema = tools[0]["inputSchema"]
        self.assertFalse(annotation_schema["additionalProperties"])
        for forbidden in ("tokens", "session_id", "turn_id", "timestamp"):
            self.assertNotIn(forbidden, annotation_schema["properties"])

        unsupported = server.handle(request(10, "initialize", {"protocolVersion": "future-version"}))
        self.assertEqual(unsupported["result"]["protocolVersion"], "2025-06-18")

    def test_annotation_is_validated_and_forwarded_only_as_json(self) -> None:
        runner = FakeRunner([ProcessResult(0, '{"status":"ok"}\n', "")])
        server = StdioMcpServer(runner, executable="hydra-test")
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
        response = StdioMcpServer(runner).handle(request(4, "tools/call", {
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

    def test_report_reconciles_then_renders_and_never_accepts_paths(self) -> None:
        runner = FakeRunner([
            ProcessResult(0, '{"status":"ok"}\n', ""),
            ProcessResult(0, "# Hydra task report\n", ""),
        ])
        server = StdioMcpServer(runner, executable="hydra-test")
        response = server.handle(request(5, "tools/call", {
            "name": "hydra.report", "arguments": {"last": 5, "format": "markdown"},
        }))
        self.assertEqual(runner.calls, [
            (("hydra-test", "reconcile"), None),
            (("hydra-test", "report", "--last", "5", "--format", "markdown"), None),
        ])
        self.assertEqual(response["result"]["content"][0]["text"], "# Hydra task report\n")
        invalid = server.handle(request(6, "tools/call", {
            "name": "hydra.report", "arguments": {"last": 1, "cwd": "/tmp/private"},
        }))
        self.assertTrue(invalid["result"]["isError"])

    def test_subprocess_errors_are_generic_and_do_not_echo_stderr(self) -> None:
        runner = FakeRunner([ProcessResult(1, "", "raw secret from database")])
        response = StdioMcpServer(runner).handle(request(7, "tools/call", {
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
