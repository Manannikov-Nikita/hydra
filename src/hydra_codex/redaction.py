"""Fail-closed note redaction shared by storage migrations and writes."""

from __future__ import annotations

import re


def redact_note(note: str) -> str:
    normalized = " ".join("".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in note
    ).split())
    sensitive_patterns = (
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
        r"\bauthorization\s*:\s*(?:bearer|basic)\s+\S+",
        r"\bbearer\s+[A-Za-z0-9._~+/=-]+",
        r"\b(?:authorization|cookie|credential|passwd|password|secret|token|api[-_ ]?key|x[-_ ]?auth[-_ ]?token|access[-_ ]?key)\b\s*(?::|=)?\s*\S+",
        r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@\S+",
        r"(?<!\w)/(?:Users|home|private|var|etc|tmp|opt|Volumes)(?:/\S*)?",
        r"(?<![\w-])[A-Za-z0-9+/=_-]{20,}(?![\w-])",
    )
    if not normalized or any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in sensitive_patterns):
        return "[redacted]"
    return normalized
