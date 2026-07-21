"""Conservative, non-executing normalization for current custom exec records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import shlex

from .js_literal_fields import static_literal_field
from .shell_facts import shell_file_facts


CUSTOM_EXEC_DIAGNOSTICS = frozenset({
    "conditional", "dynamic", "dead_code", "unsupported", "unbalanced",
})
_SAFE_TOOL_NAMES = frozenset({
    "apply_patch", "exec_command", "hydra", "mcp", "view_image", "web",
})


@dataclass(frozen=True)
class NestedToolCall:
    name: str
    command: str | None
    paths: tuple[str, ...]


@dataclass(frozen=True)
class CustomExecScan:
    calls: tuple[NestedToolCall, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedToolCall:
    """A persistence-safe nested tool fact plus an in-memory classifier hint."""

    safe_name: str
    category: str
    instrumentation: bool
    relative_paths: tuple[str, ...]
    ephemeral_command: str | None = field(default=None, repr=False, compare=False)
    file_observations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.safe_name not in _SAFE_TOOL_NAMES:
            raise ValueError("unsupported safe tool name")
        if self.category not in {"instrumentation", "tool", "web"}:
            raise ValueError("unsupported tool category")
        if self.instrumentation != (self.category == "instrumentation"):
            raise ValueError("instrumentation flag and category disagree")
        expected = "instrumentation" if self.safe_name == "hydra" else "web" if self.safe_name == "web" else None
        if expected is not None and self.category != expected:
            raise ValueError("safe tool family and category disagree")
        if self.relative_paths and self.safe_name not in {"apply_patch", "view_image"}:
            raise ValueError("tool family cannot own relative paths")
        if self.file_observations and self.safe_name != "exec_command":
            raise ValueError("tool family cannot own shell file observations")
        if any(
            operation not in {"read", "write"} or _relative_path(path, None) != path
            for operation, path in self.file_observations
        ):
            raise ValueError("invalid shell file observation")


@dataclass(frozen=True)
class CustomExecNormalization:
    calls: tuple[NormalizedToolCall, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class JoinedToolSpan:
    call_id: str
    name: str | None
    started_at: str | None
    finished_at: str | None
    terminal_state: str
    completeness: str


class ToolSpanJoin:
    """Pure terminal-monotonic join used by storage adapters."""

    def end(self, call_id: str, finished_at: str, terminal_state: str = "unknown") -> JoinedToolSpan:
        return JoinedToolSpan(call_id, None, None, finished_at, terminal_state, "incomplete")

    def start_after_end(self, span: JoinedToolSpan, name: str, started_at: str) -> JoinedToolSpan:
        return JoinedToolSpan(span.call_id, name, started_at, span.finished_at, span.terminal_state, span.completeness)


def _decode_string(source: str, index: int = 0) -> tuple[str | None, int]:
    if index >= len(source) or source[index] not in "'\"":
        return None, index
    quote, cursor, value = source[index], index + 1, []
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while cursor < len(source):
        char = source[cursor]
        if char == quote:
            return "".join(value), cursor + 1
        if char != "\\":
            value.append(char); cursor += 1; continue
        cursor += 1
        if cursor >= len(source):
            return None, cursor
        escaped = source[cursor]
        if escaped == "x" and cursor + 2 < len(source):
            try: value.append(chr(int(source[cursor + 1:cursor + 3], 16))); cursor += 3; continue
            except ValueError: return None, cursor
        if escaped == "u" and cursor + 4 < len(source):
            try: value.append(chr(int(source[cursor + 1:cursor + 5], 16))); cursor += 5; continue
            except ValueError: return None, cursor
        value.append(escapes.get(escaped, escaped)); cursor += 1
    return None, cursor


def _field_literal(arguments: str, field: str) -> str | None:
    result = static_literal_field(arguments, field)
    return result.value if result.valid and result.present else None


def _bindings(source: str) -> dict[str, str]:
    found: dict[str, str] = {}
    cursor = 0
    while cursor < len(source):
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor)
            cursor = len(source) if newline < 0 else newline
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            cursor = len(source) if end < 0 else end + 2
            continue
        if source[cursor] in "'\"`":
            _, end = _decode_string(source, cursor) if source[cursor] != "`" else (None, cursor + 1)
            if source[cursor] == "`":
                end = source.find("`", cursor + 1)
                end = len(source) if end < 0 else end + 1
            cursor = max(cursor + 1, end)
            continue
        match = re.match(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*", source[cursor:])
        if match is None:
            cursor += 1
            continue
        value, end = _decode_string(source, cursor + match.end())
        if value is not None:
            found[match.group(1)] = value
        cursor = max(cursor + match.end(), end)
    return found


def _balanced_call(source: str, start: int) -> tuple[str | None, int]:
    depth, cursor, quote = 1, start, None
    while cursor < len(source) and depth:
        char = source[cursor]
        if quote:
            if char == "\\": cursor += 2; continue
            if char == quote: quote = None
        elif char in "'\"`": quote = char
        elif char == "(": depth += 1
        elif char == ")": depth -= 1
        cursor += 1
    return (source[start:cursor - 1], cursor) if depth == 0 else (None, cursor)


def _relative_path(value: str, project_root: Path | None) -> str | None:
    if not value or any(character in value for character in ("\0", "\r", "\n")):
        return None
    portable = value.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", portable):
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


def _patch_literal(arguments: str, bindings: dict[str, str]) -> str | None:
    patch, _ = _decode_string(arguments.strip())
    if patch is None:
        patch = _field_literal(arguments, "patch")
    if patch is None and re.fullmatch(r"[A-Za-z_]\w*", arguments.strip()):
        patch = bindings.get(arguments.strip())
    return patch


def _hydra_command(command: str) -> bool:
    if any(marker in command for marker in (";", "|", "&", "<", ">", "`", "\n", "\r")):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    checkout_path = "PYTHONPATH=$(git rev-parse --show-toplevel)/src"
    if any("$" in token and token != checkout_path for token in tokens):
        return False
    index = 0
    if tokens and tokens[0].rsplit("/", 1)[-1] == "env":
        index = 1
    while index < len(tokens) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index], re.DOTALL
    ):
        index += 1
    if index >= len(tokens):
        return False
    commands = {"annotate", "compare", "ingest", "reconcile", "report"}
    executable = tokens[index].rsplit("/", 1)[-1]
    if executable == "hydra-codex":
        return index + 1 < len(tokens) and tokens[index + 1] in commands
    return (
        executable == "python3.12"
        and index + 3 < len(tokens)
        and tokens[index + 1:index + 3] == ["-m", "hydra_codex"]
        and tokens[index + 3] in commands
    )


def _normalized_call(
    name: str,
    arguments: str,
    bindings: dict[str, str],
    project_root: Path | None,
) -> tuple[NormalizedToolCall | None, str | None]:
    if name == "exec_command":
        command_field = static_literal_field(arguments, "cmd")
        if not command_field.valid or not command_field.present or command_field.value is None:
            return None, "dynamic"
        command = command_field.value
        instrumentation = _hydra_command(command)
        workdir_field = static_literal_field(arguments, "workdir")
        observations = () if not workdir_field.valid else shell_file_facts(
            command, project_root=project_root, workdir=workdir_field.value,
        )
        return NormalizedToolCall(
            "exec_command", "instrumentation" if instrumentation else "tool",
            instrumentation, (), command, observations,
        ), None
    if name == "apply_patch":
        patch = _patch_literal(arguments, bindings)
        if patch is None:
            return None, "dynamic"
        raw_paths = re.findall(
            r"^\*\*\* (?:Update|Add|Delete) File: ([^\n]+)", patch, re.MULTILINE
        )
        paths = tuple(_relative_path(path, project_root) or "" for path in raw_paths)
        if any(not path for path in paths):
            return None, "unsupported"
        return NormalizedToolCall("apply_patch", "tool", False, paths), None
    if name == "view_image":
        path = _field_literal(arguments, "path")
        if path is None:
            return None, "dynamic"
        relative = _relative_path(path, project_root)
        if relative is None:
            return None, "unsupported"
        return NormalizedToolCall("view_image", "tool", False, (relative,)), None
    if name.startswith("web__"):
        return NormalizedToolCall("web", "web", False, ()), None
    if name in {"hydra__annotate", "hydra__report", "hydra_annotate", "hydra_report"}:
        return NormalizedToolCall("hydra", "instrumentation", True, ()), None
    if name.startswith("mcp__"):
        pieces = name.split("__")
        instrumentation = (
            len(pieces) == 3
            and pieces[1] == "hydra"
            and pieces[2] in {"annotate", "report"}
        )
        return NormalizedToolCall(
            "hydra" if instrumentation else "mcp",
            "instrumentation" if instrumentation else "tool",
            instrumentation,
            (),
        ), None
    return None, "unsupported"


def normalize_custom_exec(
    source: str,
    *,
    project_root: Path | None = None,
) -> CustomExecNormalization:
    """Normalize direct literal custom-exec calls without retaining their arguments."""
    calls: list[NormalizedToolCall] = []
    diagnostics: list[str] = []
    bindings, index, quote, braces, dead = _bindings(source), 0, None, 0, False
    last_significant: str | None = None
    while index < len(source):
        if quote:
            if source[index] == "\\":
                index += 2
                continue
            if source[index] == quote:
                quote = None
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        if source[index] in "'\"`":
            quote = source[index]
            last_significant = "v"
            index += 1
            continue
        if source[index] == "{":
            braces += 1
            last_significant = "{"
            index += 1
            continue
        if source[index] == "}":
            braces = max(0, braces - 1)
            last_significant = "}"
            index += 1
            continue
        keyword = re.match(r"(?:return|throw)\b", source[index:])
        identifier_prefix = (
            last_significant is not None
            and (last_significant.isalnum() or last_significant in "_$.")
        )
        if keyword is not None and not identifier_prefix:
            dead = True
            last_significant = keyword.group(0)[-1]
            index += len(keyword.group(0))
            continue
        computed = re.match(r"tools\s*\[", source[index:])
        if computed is not None:
            diagnostics.append("dead_code" if dead else "conditional" if braces else "dynamic")
            semicolon = source.find(";", index)
            index = len(source) if semicolon < 0 else semicolon + 1
            last_significant = ";"
            continue
        match = re.match(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\(", source[index:])
        if match is None:
            if not source[index].isspace():
                last_significant = source[index]
            index += 1
            continue
        arguments, cursor = _balanced_call(source, index + match.end())
        if arguments is None:
            diagnostics.append("unbalanced")
            break
        prefix = source[max(source.rfind(";", 0, index), source.rfind("\n", 0, index)) + 1:index]
        if dead:
            diagnostics.append("dead_code")
        elif (
            braces
            or re.search(r"\b(?:if|for|while|switch|catch)\b", prefix)
            or any(marker in prefix for marker in ("&&", "||", "?", "=>"))
        ):
            diagnostics.append("conditional")
        else:
            call, diagnostic = _normalized_call(
                match.group(1), arguments, bindings, project_root
            )
            if call is not None:
                calls.append(call)
            elif diagnostic is not None:
                diagnostics.append(diagnostic)
        index = cursor
        last_significant = ")"
    return CustomExecNormalization(tuple(calls), tuple(diagnostics))


def scan_custom_exec_details(source: str) -> CustomExecScan:
    """Compatibility view for command/test extraction and patch write hints."""
    normalized = normalize_custom_exec(source)
    calls = tuple(
        NestedToolCall(
            call.safe_name,
            call.ephemeral_command,
            call.relative_paths if call.safe_name == "apply_patch" else (),
        )
        for call in normalized.calls
        if call.safe_name in {"exec_command", "apply_patch"}
    )
    return CustomExecScan(calls, normalized.diagnostics)


def scan_custom_exec(source: str) -> tuple[NestedToolCall, ...]:
    return scan_custom_exec_details(source).calls


def nested_span_name(call: NestedToolCall) -> str | None:
    """Only executable command intent is safely inferred; patch/MCP ends are canonical facts."""
    return "nested_exec" if call.name == "exec_command" else None


def custom_exec_outcome(output: object) -> tuple[str, int | None]:
    """Inspect only the first wrapper in memory and return normalized completion facts."""
    wrapper = output[0] if isinstance(output, list) and output else None
    text = wrapper.get("text") if isinstance(wrapper, dict) and wrapper.get("type") == "input_text" else None
    if not isinstance(text, str):
        return "unknown", None
    elapsed = re.search(r"(?:wall time|elapsed)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|seconds)", text, re.IGNORECASE)
    latency = None if elapsed is None else round(float(elapsed.group(1)) * (1 if elapsed.group(2).lower().startswith("m") else 1000))
    lowered = text.lower()
    if "script completed" in lowered:
        return "success", latency
    if re.search(r"\b(?:failed|error)\b|exit code\s*[1-9]", lowered):
        return "failed", latency
    return "unknown", latency
