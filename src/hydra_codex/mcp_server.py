"""Small stdio MCP facade over the privacy-safe Hydra CLI.

The server deliberately delegates persistence and cooperative identity binding
to the CLI/hooks layer.  MCP arguments can contain semantic annotations or report
options only; they cannot name sessions, turns, paths, or timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import sys
import threading
from typing import Any, Mapping, Protocol, Sequence, TextIO

from . import __version__
from .contracts import ModelAnnotationInput
from .runtime_entrypoint import runtime_command_prefix


MCP_PROTOCOL = "2025-06-18"
_PILOT_ID = re.compile(r"hpilot_v1_[0-9a-f]{32}\Z")
_SUBPROCESS_TIMEOUT_SECONDS = 10
_SUBPROCESS_OUTPUT_MAX_BYTES = 65_536
_SUBPROCESS_INPUT_MAX_BYTES = 65_536


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, command: tuple[str, ...], stdin_text: str | None = None) -> ProcessResult: ...


class SubprocessRunner:
    def __init__(
        self,
        *,
        timeout_seconds: float = _SUBPROCESS_TIMEOUT_SECONDS,
        output_max_bytes: int = _SUBPROCESS_OUTPUT_MAX_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or output_max_bytes <= 0:
            raise ValueError("invalid subprocess limits")
        self._timeout_seconds = timeout_seconds
        self._output_max_bytes = output_max_bytes

    def _drain(self, stream, output: bytearray) -> None:
        try:
            while chunk := stream.read(8192):
                remaining = self._output_max_bytes - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
        except OSError:
            pass
        finally:
            stream.close()

    @staticmethod
    def _write(stream, content: bytes) -> None:
        try:
            stream.write(content)
            stream.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            stream.close()

    def run(self, command: tuple[str, ...], stdin_text: str | None = None) -> ProcessResult:
        input_bytes = None if stdin_text is None else stdin_text.encode("utf-8")
        if input_bytes is not None and len(input_bytes) > _SUBPROCESS_INPUT_MAX_BYTES:
            return ProcessResult(1, "", "")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError:
            return ProcessResult(1, "", "")
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        readers = (
            threading.Thread(target=self._drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=self._drain, args=(process.stderr, stderr), daemon=True),
        )
        for reader in readers:
            reader.start()
        writer = None
        if input_bytes is not None:
            assert process.stdin is not None
            writer = threading.Thread(
                target=self._write,
                args=(process.stdin, input_bytes),
                daemon=True,
            )
            writer.start()
        try:
            returncode = process.wait(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            if writer is not None:
                writer.join()
            for reader in readers:
                reader.join()
            return ProcessResult(1, "", "")
        if writer is not None:
            writer.join()
        for reader in readers:
            reader.join()
        return ProcessResult(
            returncode,
            stdout.decode("utf-8", errors="ignore"),
            stderr.decode("utf-8", errors="ignore"),
        )


_ANNOTATION_PROPERTIES: dict[str, dict[str, object]] = {
    "kind": {"type": "string", "enum": ["phase", "blocker", "finish"]},
    "phase": {"type": "string", "enum": [
        "understand", "research", "design", "implement", "test_targeted",
        "test_full", "review", "fix", "docs", "browser_qa", "release",
        "wait_external",
    ]},
    "cause": {"type": "string", "enum": [
        "prompt", "plan", "test_failure", "review_finding", "user_change",
        "infra_failure", "final_verification", "other",
    ]},
    "outcome": {"type": "string", "enum": [
        "success", "partial", "blocked", "failed", "cancelled",
    ]},
    "scope_change": {"type": "string", "enum": [
        "none", "narrowed", "expanded", "redefined",
    ]},
    "task_family": {
        "type": "string", "minLength": 1, "maxLength": 80,
        "pattern": "^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,7}$",
    },
    "note": {"type": "string", "maxLength": 240},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
}
_ANNOTATION_REQUIRED = (
    "kind", "phase", "cause", "scope_change", "task_family", "note", "confidence",
)


def _tool_definitions(*, annotation_enabled: bool) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    if annotation_enabled:
        tools.append({
            "name": "hydra.annotate",
            "description": (
                "Record only the current task phase, cause, scope change, or finish outcome. "
                "A configured hook binds identity and time outside model arguments; "
                "this is not an authenticated local-process boundary."
            ),
            "inputSchema": {
                "type": "object", "properties": _ANNOTATION_PROPERTIES,
                "required": list(_ANNOTATION_REQUIRED), "additionalProperties": False,
            },
        })
    tools.append({
        "name": "hydra.report",
        "description": (
            "Reconcile local telemetry and render either recent privacy-safe task reports "
            "or one canonical pilot audit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "last": {"type": "integer", "minimum": 1, "maximum": 100},
                "pilot": {
                    "type": "string",
                    "pattern": "^hpilot_v1_[0-9a-f]{32}$",
                },
                "format": {"type": "string", "enum": ["json", "markdown", "html"]},
            },
            "oneOf": [{"required": ["last"]}, {"required": ["pilot"]}],
            "additionalProperties": False,
        },
        })
    return tools


def _result_text(text: str, *, error: bool = False) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _protocol_error(identifier: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0", "id": identifier,
        "error": {"code": code, "message": message},
    }


def _call_error(identifier: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "result": _result_text(
        "Invalid Hydra tool arguments.", error=True,
    )}


class StdioMcpServer:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        command_prefix: Sequence[str] | None = None,
        executable: str | None = None,
        annotation_enabled: bool = False,
    ) -> None:
        self._runner = SubprocessRunner() if runner is None else runner
        if command_prefix is not None and executable is not None:
            raise ValueError("choose one Hydra command prefix")
        selected = (
            (executable,)
            if executable is not None
            else (
                runtime_command_prefix()
                if command_prefix is None
                else tuple(command_prefix)
            )
        )
        if not selected or any(
            not isinstance(part, str) or not part for part in selected
        ):
            raise ValueError("invalid Hydra command prefix")
        self._command_prefix = tuple(selected)
        self._annotation_enabled = annotation_enabled

    def _command(self, *arguments: str) -> tuple[str, ...]:
        return (*self._command_prefix, *arguments)

    def _annotate(self, arguments: object) -> dict[str, object]:
        if not isinstance(arguments, Mapping):
            raise ValueError("invalid arguments")
        model = ModelAnnotationInput.from_mapping(arguments)
        payload = {
            "kind": model.kind.value,
            "phase": model.phase.value,
            "cause": model.cause.value,
            "scope_change": model.scope_change.value,
            "task_family": model.task_family,
            "confidence": model.confidence,
            "note": model.note,
        }
        if model.outcome is not None:
            payload["outcome"] = model.outcome.value
        result = self._runner.run(
            self._command("annotate"),
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
        if result.returncode != 0:
            return _result_text("Hydra command failed.", error=True)
        return _result_text(result.stdout or '{"status":"ok"}\n')

    def _report(self, arguments: object) -> dict[str, object]:
        if not isinstance(arguments, Mapping) or set(arguments) - {"last", "pilot", "format"}:
            raise ValueError("invalid arguments")
        has_last = "last" in arguments
        has_pilot = "pilot" in arguments
        if has_last == has_pilot:
            raise ValueError("exactly one report mode is required")
        output_format = arguments.get("format", "json")
        if output_format not in {"json", "markdown", "html"}:
            raise ValueError("invalid format")
        if has_pilot:
            pilot_id = arguments.get("pilot")
            if not isinstance(pilot_id, str) or _PILOT_ID.fullmatch(pilot_id) is None:
                raise ValueError("invalid pilot")
            rendered = self._runner.run(self._command(
                "audit",
                "--pilot",
                pilot_id,
                "--format",
                str(output_format),
            ))
            if rendered.returncode != 0:
                return _result_text("Hydra command failed.", error=True)
            return _result_text(rendered.stdout)
        count = arguments.get("last")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            raise ValueError("invalid last")
        ingested = self._runner.run(self._command("ingest"))
        if ingested.returncode != 0:
            return _result_text("Hydra command failed.", error=True)
        reconciled = self._runner.run(self._command("reconcile"))
        if reconciled.returncode != 0:
            return _result_text("Hydra command failed.", error=True)
        rendered = self._runner.run(self._command(
            "report", "--last", str(count), "--format", str(output_format),
        ))
        if rendered.returncode != 0:
            return _result_text("Hydra command failed.", error=True)
        return _result_text(rendered.stdout)

    def handle(self, message: object) -> dict[str, object] | None:
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return _protocol_error(None, -32600, "Invalid Request")
        identifier = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return _protocol_error(identifier, -32600, "Invalid Request")
        if identifier is None:
            return None
        if method == "initialize":
            params = message.get("params", {})
            version = params.get("protocolVersion") if isinstance(params, Mapping) else None
            if version != MCP_PROTOCOL:
                version = MCP_PROTOCOL
            result: object = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "hydra-codex", "version": __version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_definitions(annotation_enabled=self._annotation_enabled)}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, Mapping) or not isinstance(params.get("name"), str):
                return _call_error(identifier)
            try:
                if params["name"] == "hydra.annotate" and self._annotation_enabled:
                    result = self._annotate(params.get("arguments", {}))
                elif params["name"] == "hydra.annotate":
                    return _call_error(identifier)
                elif params["name"] == "hydra.report":
                    result = self._report(params.get("arguments", {}))
                else:
                    return _protocol_error(identifier, -32601, "Method not found")
            except (TypeError, ValueError):
                return _call_error(identifier)
        else:
            return _protocol_error(identifier, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}


def serve(input_stream: TextIO, output_stream: TextIO, server: StdioMcpServer) -> None:
    for line in input_stream:
        try:
            message = json.loads(line)
        except (TypeError, ValueError):
            response = _protocol_error(None, -32700, "Parse error")
        else:
            try:
                response = server.handle(message)
            except Exception:
                identifier = message.get("id") if isinstance(message, Mapping) else None
                response = _protocol_error(identifier, -32603, "Internal error")
        if response is not None:
            output_stream.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
            output_stream.flush()


def main(
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    command_prefix: Sequence[str] | None = None,
) -> int:
    serve(
        sys.stdin if input_stream is None else input_stream,
        sys.stdout if output_stream is None else output_stream,
        StdioMcpServer(command_prefix=command_prefix),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
