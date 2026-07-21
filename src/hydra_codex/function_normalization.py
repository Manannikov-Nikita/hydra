"""Privacy-safe normalization for direct Codex function-call records."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .shell_facts import shell_file_facts


_HYDRA_NAMES = frozenset({
    "hydra.annotate", "hydra.report", "hydra_annotate", "hydra_report",
    "mcp__hydra__annotate", "mcp__hydra__report",
})
_KNOWN_TOOL_NAMES = frozenset({
    "apply_patch", "exec_command", "file_read", "file_write", "hydra", "mcp",
    "read_mcp_resource", "unknown", "view_image", "web",
})


@dataclass(frozen=True)
class NormalizedFileAccess:
    operation: str
    relative_path: str

    def __post_init__(self) -> None:
        if self.operation not in {"read", "write"}:
            raise ValueError("unsupported file operation")
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or any(character in self.relative_path for character in ("\0", "\r", "\n"))
        ):
            raise ValueError("file path must be relative")


@dataclass(frozen=True)
class NormalizedFunctionCall:
    safe_name: str
    category: str
    instrumentation: bool
    file_accesses: tuple[NormalizedFileAccess, ...]
    provenance: str
    terminal_state: str = "unknown"
    ephemeral_command: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.safe_name not in _KNOWN_TOOL_NAMES:
            raise ValueError("unsupported safe tool name")
        if self.category not in {"instrumentation", "tool", "web"}:
            raise ValueError("unsupported category")
        if self.instrumentation != (self.category == "instrumentation"):
            raise ValueError("instrumentation flag and category disagree")
        expected_category = (
            "instrumentation" if self.safe_name == "hydra"
            else "web" if self.safe_name == "web"
            else "tool"
        )
        if self.category != expected_category:
            raise ValueError("safe tool family and category disagree")
        if self.provenance not in {"exact", "lower_bound"}:
            raise ValueError("unsupported provenance")
        if (self.safe_name == "unknown") != (self.provenance == "lower_bound"):
            raise ValueError("safe tool family and provenance disagree")
        if self.terminal_state != "unknown":
            raise ValueError("a start-only function call has unknown terminal state")
        allowed_operations = {
            "apply_patch": "write", "file_write": "write",
            "file_read": "read", "view_image": "read",
        }
        expected_operation = allowed_operations.get(self.safe_name)
        if self.file_accesses and expected_operation is None and self.safe_name != "exec_command":
            raise ValueError("safe tool family cannot own file accesses")
        if self.safe_name != "exec_command" and any(
            access.operation != expected_operation for access in self.file_accesses
        ):
            raise ValueError("file operation disagrees with safe tool family")


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _relative_path(value: Any, project_root: Path | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if any(character in value for character in ("\0", "\r", "\n")):
        return None
    portable = value.replace("\\", "/")
    if portable.startswith("~") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", portable):
        return None
    candidate = PurePosixPath(portable)
    if candidate.is_absolute():
        if project_root is None:
            return None
        root = PurePosixPath(str(project_root).replace("\\", "/"))
        if not root.is_absolute():
            return None
        try:
            candidate = candidate.relative_to(root)
        except ValueError:
            return None
    if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate.as_posix()


def _family(name: Any) -> tuple[str, str, bool, str]:
    if not isinstance(name, str):
        return "unknown", "tool", False, "lower_bound"
    if name in _HYDRA_NAMES:
        return "hydra", "instrumentation", True, "exact"
    if name in {"web__run", "web.run"}:
        return "web", "web", False, "exact"
    if name.startswith("mcp__"):
        return "mcp", "tool", False, "exact"
    if name in {"write_file", "file_write"}:
        return "file_write", "tool", False, "exact"
    if name in {
        "apply_patch", "exec_command", "file_read", "read_mcp_resource", "view_image",
    }:
        return name, "tool", False, "exact"
    return "unknown", "tool", False, "lower_bound"


def _file_accesses(
    safe_name: str,
    arguments: dict[str, Any],
    project_root: Path | None,
) -> tuple[NormalizedFileAccess, ...]:
    if safe_name == "exec_command":
        command = arguments.get("cmd")
        workdir = arguments.get("workdir")
        if not isinstance(command, str) or not (
            workdir is None or isinstance(workdir, str)
        ):
            return ()
        return tuple(
            NormalizedFileAccess(operation, path)
            for operation, path in shell_file_facts(
                command, project_root=project_root, workdir=workdir,
            )
        )
    if safe_name in {"view_image", "file_read"}:
        path = _relative_path(arguments.get("path"), project_root)
        return (NormalizedFileAccess("read", path),) if path is not None else ()
    if safe_name == "file_write":
        path = _relative_path(arguments.get("path"), project_root)
        return (NormalizedFileAccess("write", path),) if path is not None else ()
    if safe_name != "apply_patch" or not isinstance(arguments.get("patch"), str):
        return ()
    paths: list[NormalizedFileAccess] = []
    seen: set[str] = set()
    for raw_path in re.findall(
        r"^\*\*\* (?:Update|Add|Delete) File: ([^\n]+)",
        arguments["patch"],
        re.MULTILINE,
    ):
        path = _relative_path(raw_path, project_root)
        if path is not None and path not in seen:
            paths.append(NormalizedFileAccess("write", path))
            seen.add(path)
    return tuple(paths)


def normalize_function_call(
    name: Any,
    arguments: Any,
    *,
    project_root: Path | None = None,
) -> NormalizedFunctionCall:
    """Return allowlisted direct-call facts; raw name and arguments stay in memory only."""
    safe_name, category, instrumentation, provenance = _family(name)
    decoded = _arguments(arguments)
    command = decoded.get("cmd") if safe_name == "exec_command" else None
    return NormalizedFunctionCall(
        safe_name=safe_name,
        category=category,
        instrumentation=instrumentation,
        file_accesses=_file_accesses(safe_name, decoded, project_root),
        provenance=provenance,
        ephemeral_command=command if isinstance(command, str) else None,
    )
