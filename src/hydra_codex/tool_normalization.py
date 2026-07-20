"""Conservative, non-executing normalization for current custom exec records."""

from __future__ import annotations

from dataclasses import dataclass
import re


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
    match = re.search(rf"(?:\b{re.escape(field)}\b|['\"]{re.escape(field)}['\"])\s*:\s*", arguments)
    if match is None:
        return None
    value, _ = _decode_string(arguments, match.end())
    return value


def _bindings(source: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*", source):
        value, _ = _decode_string(source, match.end())
        if value is not None:
            found[match.group(1)] = value
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


def _call(name: str, arguments: str, bindings: dict[str, str]) -> NestedToolCall | None:
    if name == "exec_command":
        command = _field_literal(arguments, "cmd")
        return NestedToolCall(name, command, ()) if command is not None else None
    if name == "apply_patch":
        patch, _ = _decode_string(arguments.strip())
        if patch is None:
            patch = _field_literal(arguments, "patch")
        if patch is None and re.fullmatch(r"[A-Za-z_]\w*", arguments.strip()):
            patch = bindings.get(arguments.strip())
        if patch is None:
            return None
        paths = tuple(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: ([^\n]+)", patch, re.MULTILINE))
        return NestedToolCall(name, None, paths)
    return NestedToolCall(name, None, ())


def scan_custom_exec_details(source: str) -> CustomExecScan:
    """Find only unconditional literal calls; report uncertainty without storing source."""
    calls: list[NestedToolCall] = []
    diagnostics: list[str] = []
    bindings, index, quote, braces, dead = _bindings(source), 0, None, 0, False
    while index < len(source):
        if quote:
            if source[index] == "\\": index += 2; continue
            if source[index] == quote: quote = None
            index += 1; continue
        if source.startswith("//", index):
            newline = source.find("\n", index); index = len(source) if newline < 0 else newline; continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2); index = len(source) if end < 0 else end + 2; continue
        if source[index] in "'\"`": quote = source[index]; index += 1; continue
        if source[index] == "{": braces += 1; index += 1; continue
        if source[index] == "}": braces = max(0, braces - 1); index += 1; continue
        if re.match(r"\breturn\b", source[index:]): dead = True; index += len("return"); continue
        match = re.match(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\(", source[index:])
        if match is None: index += 1; continue
        arguments, cursor = _balanced_call(source, index + match.end())
        if arguments is None: diagnostics.append("unresolved_arguments"); break
        nested = _call(match.group(1), arguments, bindings)
        prefix = source[max(source.rfind(";", 0, index), source.rfind("\n", 0, index)) + 1:index]
        if nested is None:
            diagnostics.append("unresolved_arguments")
        elif braces or dead or re.search(r"\b(?:if|for|while|switch|catch)\b", prefix):
            diagnostics.append("conditional_or_dead")
        else:
            calls.append(nested)
        index = cursor
    return CustomExecScan(tuple(calls), tuple(diagnostics))


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
