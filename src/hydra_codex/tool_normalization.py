"""Conservative, non-executing normalization for current custom exec records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import shlex


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
    cursor = 0
    object_depth = 0
    while cursor < len(arguments):
        if arguments.startswith("//", cursor):
            newline = arguments.find("\n", cursor)
            cursor = len(arguments) if newline < 0 else newline
            continue
        if arguments.startswith("/*", cursor):
            end = arguments.find("*/", cursor + 2)
            cursor = len(arguments) if end < 0 else end + 2
            continue
        character = arguments[cursor]
        if character == "{":
            object_depth += 1
            cursor += 1
            continue
        if character == "}":
            object_depth = max(0, object_depth - 1)
            cursor += 1
            continue
        key: str | None = None
        end = cursor
        if character in "'\"":
            key, end = _decode_string(arguments, cursor)
        elif character.isalpha() or character in "_$":
            match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", arguments[cursor:])
            if match is not None:
                key = match.group(0)
                end = cursor + match.end()
        if key is None:
            cursor = max(cursor + 1, end)
            continue
        after = end
        while after < len(arguments) and arguments[after].isspace():
            after += 1
        if object_depth == 1 and after < len(arguments) and arguments[after] == ":":
            value_start = after + 1
            while value_start < len(arguments) and arguments[value_start].isspace():
                value_start += 1
            if key == field:
                value, _ = _decode_string(arguments, value_start)
                return value
        cursor = end
    return None


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
    if any(marker in command for marker in (";", "|", "&", "<", ">", "`", "$")):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    return (
        len(tokens) >= 2
        and tokens[0].rsplit("/", 1)[-1] == "hydra-codex"
        and tokens[1] in {"annotate", "report"}
    )


def _normalized_call(
    name: str,
    arguments: str,
    bindings: dict[str, str],
    project_root: Path | None,
) -> tuple[NormalizedToolCall | None, str | None]:
    if name == "exec_command":
        command = _field_literal(arguments, "cmd")
        if command is None:
            return None, "dynamic"
        instrumentation = _hydra_command(command)
        return NormalizedToolCall(
            "exec_command", "instrumentation" if instrumentation else "tool",
            instrumentation, (), command,
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
            index += 1
            continue
        if source[index] == "{":
            braces += 1
            index += 1
            continue
        if source[index] == "}":
            braces = max(0, braces - 1)
            index += 1
            continue
        if re.match(r"\breturn\b", source[index:]):
            dead = True
            index += len("return")
            continue
        computed = re.match(r"tools\s*\[", source[index:])
        if computed is not None:
            diagnostics.append("dead_code" if dead else "conditional" if braces else "dynamic")
            semicolon = source.find(";", index)
            index = len(source) if semicolon < 0 else semicolon + 1
            continue
        match = re.match(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\(", source[index:])
        if match is None:
            index += 1
            continue
        arguments, cursor = _balanced_call(source, index + match.end())
        if arguments is None:
            diagnostics.append("unbalanced")
            break
        prefix = source[max(source.rfind(";", 0, index), source.rfind("\n", 0, index)) + 1:index]
        if dead:
            diagnostics.append("dead_code")
        elif braces or re.search(r"\b(?:if|for|while|switch|catch)\b", prefix):
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
