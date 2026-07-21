"""Thin, privacy-safe command shell for Hydra Codex telemetry."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Protocol, Sequence, TextIO

from .contracts import ModelAnnotationInput
from .project import ProjectNotFound, resolve_project
from .rollout import ingest_rollouts
from .rollout_identity import Pseudonymizer, RolloutRoot
from .storage import HydraStore, StorageUnavailable


class CommandServices(Protocol):
    def annotate(
        self, annotation: ModelAnnotationInput, capability: str,
        database_path: Path | None, cwd: Path,
    ) -> object: ...

    def reconcile(self, database_path: Path | None, cwd: Path) -> object: ...

    def report(
        self, last: int, output_format: str, database_path: Path | None, cwd: Path,
    ) -> str: ...

    def compare(
        self, left: str, right: str, output_format: str,
        database_path: Path | None, cwd: Path,
    ) -> str: ...


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "hydra-codex: invalid arguments\n")


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("positive integer required") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("positive integer required")
    return parsed


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db")
    parser.add_argument("--cwd")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="hydra-codex")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest")
    _common_options(ingest)
    ingest.add_argument("--source", action="append", default=[])

    annotate = commands.add_parser("annotate")
    _common_options(annotate)
    for field in ("kind", "phase", "cause", "outcome", "scope-change", "task-family", "note"):
        annotate.add_argument("--" + field)
    annotate.add_argument("--confidence", type=float)

    reconcile = commands.add_parser("reconcile")
    _common_options(reconcile)

    report = commands.add_parser("report")
    _common_options(report)
    report.add_argument("--last", type=_positive_integer, required=True)
    report.add_argument("--format", choices=("json", "markdown", "html"), default="json")
    report.add_argument("--output")

    compare = commands.add_parser("compare")
    _common_options(compare)
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--format", choices=("json", "markdown", "html"), default="json")
    compare.add_argument("--output")
    return parser


def _paths(arguments: argparse.Namespace) -> tuple[Path | None, Path]:
    database = Path(arguments.db).expanduser() if arguments.db else None
    cwd = Path(arguments.cwd).expanduser() if arguments.cwd else Path.cwd()
    return database, cwd


def _source(value: str, cwd: Path) -> RolloutRoot:
    if "=" not in value:
        raise ValueError("source must have a label")
    label, raw_path = value.split("=", 1)
    if label not in {"active", "archived", "explicit"} or not raw_path:
        raise ValueError("invalid source")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    if not path.exists() or not (path.is_dir() or path.is_file() and path.suffix == ".jsonl"):
        raise ValueError("source is unavailable")
    return RolloutRoot(path, label)


def _default_sources(environ: Mapping[str, str]) -> tuple[RolloutRoot, ...]:
    home = Path(environ["HOME"]).expanduser() if environ.get("HOME") else Path.home()
    candidates = (
        (home / ".codex" / "sessions", "active"),
        (home / ".codex" / "archived_sessions", "archived"),
    )
    return tuple(RolloutRoot(path, label) for path, label in candidates if path.is_dir())


def _run_ingest(
    arguments: argparse.Namespace,
    environ: Mapping[str, str],
    installation_key_path: Path | None,
) -> dict[str, object]:
    from .services import (
        configured_database_path,
        configured_installation_key_path,
    )

    database, cwd = _paths(arguments)
    project = resolve_project(cwd)
    roots = _default_sources(environ) + tuple(_source(value, project.project_root) for value in arguments.source)
    store = HydraStore(configured_database_path(environ, database))
    try:
        hash_key = Pseudonymizer.installation_key(
            configured_installation_key_path(environ, installation_key_path),
        ).key
        report = ingest_rollouts(
            store, roots, project.project_root, project.project_id, hash_key=hash_key,
        )
    finally:
        store.close()
    return {
        "command": "ingest",
        "status": "ok",
        "files_seen": report.files_seen,
        "unique_sources": report.unique_sources,
        "diagnostics": report.diagnostics,
    }


def _stdin_text(stdin: TextIO) -> str:
    try:
        if stdin.isatty():
            return ""
    except (AttributeError, OSError):
        pass
    return stdin.read()


def _annotation(arguments: argparse.Namespace, stdin: TextIO) -> ModelAnnotationInput:
    flag_fields = {
        "kind": arguments.kind,
        "phase": arguments.phase,
        "cause": arguments.cause,
        "outcome": arguments.outcome,
        "scope_change": arguments.scope_change,
        "task_family": arguments.task_family,
        "confidence": arguments.confidence,
        "note": arguments.note,
    }
    supplied = {key: value for key, value in flag_fields.items() if value is not None}
    if supplied:
        payload: Any = supplied
    else:
        body = _stdin_text(stdin).strip()
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid annotation") from error
    if not isinstance(payload, dict):
        raise ValueError("invalid annotation")
    return ModelAnnotationInput.from_mapping(payload)


def _write_json(stdout: TextIO, value: Mapping[str, object]) -> None:
    stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _write_rendered(stdout: TextIO, content: str) -> None:
    stdout.write(content)
    if not content.endswith("\n"):
        stdout.write("\n")


def atomic_write(path: Path | str, content: str) -> None:
    """Explicitly overwrite one output via a private, fsynced sibling temp file."""
    target = Path(path).expanduser()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".hydra-output-", dir=target.parent)
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor_open = False
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _render(
    arguments: argparse.Namespace,
    stdout: TextIO,
    services: CommandServices,
) -> None:
    database, cwd = _paths(arguments)
    if arguments.command == "report":
        content = services.report(arguments.last, arguments.format, database, cwd)
    else:
        content = services.compare(
            arguments.left, arguments.right, arguments.format, database, cwd,
        )
    if not isinstance(content, str):
        raise RuntimeError("render service returned invalid content")
    if arguments.output:
        atomic_write(arguments.output, content)
    else:
        _write_rendered(stdout, content)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    services: CommandServices | None = None,
    installation_key_path: Path | None = None,
) -> int:
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    environment = os.environ if environ is None else environ
    if services is None:
        from .services import LocalCommandServices

        command_services: CommandServices = LocalCommandServices(
            environ=environment,
            installation_key_path=installation_key_path,
        )
    else:
        command_services = services
    with redirect_stdout(output_stream), redirect_stderr(error_stream):
        try:
            arguments = build_parser().parse_args(argv)
        except SystemExit as exit_request:
            return int(exit_request.code or 0)
    try:
        if arguments.command == "ingest":
            _write_json(
                output_stream,
                _run_ingest(arguments, environment, installation_key_path),
            )
        elif arguments.command == "annotate":
            capability = environment.get("HYDRA_TURN_CAPABILITY")
            if not isinstance(capability, str) or not capability:
                raise ValueError("missing annotation capability")
            annotation = _annotation(arguments, input_stream)
            database, cwd = _paths(arguments)
            command_services.annotate(annotation, capability, database, cwd)
            _write_json(output_stream, {"command": "annotate", "status": "ok"})
        elif arguments.command == "reconcile":
            database, cwd = _paths(arguments)
            command_services.reconcile(database, cwd)
            _write_json(output_stream, {"command": "reconcile", "status": "ok"})
        else:
            _render(arguments, output_stream, command_services)
        return 0
    except StorageUnavailable:
        error_stream.write("hydra-codex: storage unavailable\n")
    except (ProjectNotFound, ValueError, json.JSONDecodeError):
        error_stream.write("hydra-codex: validation failed\n")
    except OSError:
        error_stream.write("hydra-codex: I/O operation failed\n")
    except Exception:
        error_stream.write("hydra-codex: command failed\n")
    return 1
