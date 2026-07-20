"""Conservative, non-executing scanner for current custom exec tool inputs."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class NestedToolCall:
    name: str
    command: str | None
    paths: tuple[str, ...]


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
    def end(self, call_id: str, finished_at: str) -> JoinedToolSpan:
        return JoinedToolSpan(call_id, None, None, finished_at, "success", "incomplete")

    def start_after_end(self, span: JoinedToolSpan, name: str, started_at: str) -> JoinedToolSpan:
        return JoinedToolSpan(span.call_id, name, started_at, span.finished_at, span.terminal_state, span.completeness)


def _literal(arguments: str, field: str) -> str | None:
    match = re.search(rf"\b{re.escape(field)}\s*:\s*(['\"])(.*?)\1", arguments, re.DOTALL)
    return match.group(2) if match else None


def scan_custom_exec(source: str) -> tuple[NestedToolCall, ...]:
    calls: list[NestedToolCall] = []
    bindings = {name: value for name, _, value in re.findall(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*(['\"])(.*?)\2", source, re.DOTALL)}
    index = 0
    quote: str | None = None
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
        match = re.match(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\(", source[index:])
        if not match: index += 1; continue
        name, start, depth, cursor = match.group(1), index + match.end(), 1, index + match.end()
        inner_quote: str | None = None
        while cursor < len(source) and depth:
            char = source[cursor]
            if inner_quote:
                if char == "\\": cursor += 2; continue
                if char == inner_quote: inner_quote = None
            elif char in "'\"": inner_quote = char
            elif char == "(": depth += 1
            elif char == ")": depth -= 1
            cursor += 1
        if depth: break
        arguments = source[start:cursor - 1]
        patch = _literal(arguments, "patch")
        if patch is None and name == "apply_patch":
            patch = bindings.get(arguments.strip())
        patch = (patch or "").replace("\\n", "\n")
        paths = tuple(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: ([^\n]+)", patch, re.MULTILINE))
        calls.append(NestedToolCall(name, _literal(arguments, "cmd"), paths))
        index = cursor
    return tuple(calls)
