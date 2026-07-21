"""Fail-closed extraction of one static string field from a JS object literal."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class StaticLiteralField:
    value: str | None
    present: bool
    valid: bool


def _decode_string(source: str, index: int) -> tuple[str | None, int]:
    if index >= len(source) or source[index] not in "'\"":
        return None, index
    quote, cursor, value = source[index], index + 1, []
    escapes = {
        "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
        "v": "\v", "0": "\0",
    }
    while cursor < len(source):
        char = source[cursor]
        if char == quote:
            return "".join(value), cursor + 1
        if char != "\\":
            value.append(char)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(source):
            return None, cursor
        escaped = source[cursor]
        if escaped == "x" and cursor + 2 < len(source):
            try:
                value.append(chr(int(source[cursor + 1:cursor + 3], 16)))
            except ValueError:
                return None, cursor
            cursor += 3
            continue
        if escaped == "u" and cursor + 4 < len(source):
            try:
                value.append(chr(int(source[cursor + 1:cursor + 5], 16)))
            except ValueError:
                return None, cursor
            cursor += 5
            continue
        value.append(escapes.get(escaped, escaped))
        cursor += 1
    return None, cursor


def _skip_trivia(source: str, index: int) -> tuple[int, bool]:
    while index < len(source):
        if source[index].isspace():
            index += 1
        elif source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                return len(source), False
            index = end + 2
        else:
            break
    return index, True


def _properties(source: str) -> tuple[str, ...] | None:
    text = source.strip()
    if len(text) < 2 or text[0] != "{" or text[-1] != "}":
        return None
    inner = text[1:-1]
    properties: list[str] = []
    current: list[str] = []
    stack: list[str] = []
    quote: str | None = None
    index = 0
    pairs = {")": "(", "]": "[", "}": "{"}
    while index < len(inner):
        char = inner[index]
        if quote is not None:
            current.append(char)
            if char == "\\":
                index += 1
                if index < len(inner):
                    current.append(inner[index])
            elif char == quote:
                quote = None
            index += 1
            continue
        if inner.startswith("//", index):
            newline = inner.find("\n", index + 2)
            current.append(" ")
            index = len(inner) if newline < 0 else newline + 1
            continue
        if inner.startswith("/*", index):
            end = inner.find("*/", index + 2)
            if end < 0:
                return None
            current.append(" ")
            index = end + 2
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
        elif char in "([{":
            stack.append(char)
            current.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                return None
            current.append(char)
        elif char == "," and not stack:
            properties.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if quote is not None or stack:
        return None
    properties.append("".join(current))
    return tuple(properties)


def static_literal_field(source: str, field: str) -> StaticLiteralField:
    """Require a unique, complete string literal and reject dynamic overrides."""
    properties = _properties(source)
    if properties is None:
        return StaticLiteralField(None, False, False)
    occurrences: list[str | None] = []
    for raw in properties:
        index, trivia_ok = _skip_trivia(raw, 0)
        if not trivia_ok:
            return StaticLiteralField(None, False, False)
        if index == len(raw):
            continue
        if raw.startswith("...", index) or raw[index] == "[":
            return StaticLiteralField(None, False, False)
        key: str | None
        if raw[index] in "'\"":
            key, cursor = _decode_string(raw, index)
        else:
            match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", raw[index:])
            key = None if match is None else match.group(0)
            cursor = index if match is None else index + match.end()
        if key is None:
            return StaticLiteralField(None, False, False)
        cursor, trivia_ok = _skip_trivia(raw, cursor)
        if not trivia_ok:
            return StaticLiteralField(None, False, False)
        if cursor >= len(raw) or raw[cursor] != ":":
            if key == field:
                occurrences.append(None)
            if cursor != len(raw):
                return StaticLiteralField(None, False, False)
            continue
        if key != field:
            continue
        cursor, trivia_ok = _skip_trivia(raw, cursor + 1)
        if not trivia_ok:
            occurrences.append(None)
            continue
        value, end = _decode_string(raw, cursor)
        end, trivia_ok = _skip_trivia(raw, end)
        occurrences.append(value if trivia_ok and end == len(raw) else None)
    if not occurrences:
        return StaticLiteralField(None, False, True)
    if len(occurrences) != 1 or occurrences[0] is None:
        return StaticLiteralField(None, True, False)
    return StaticLiteralField(occurrences[0], True, True)
