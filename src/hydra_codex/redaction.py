"""Fail-closed note redaction shared by storage migrations and writes."""

from __future__ import annotations

import re


_SAFE_TASK_FAMILY = re.compile(
    r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,7}\Z"
)
_PRIVATE_IDENTIFIER = re.compile(
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.IGNORECASE,
)
_PRIVATE_FAMILY_SEGMENTS = frozenset({
    "account", "client", "credential", "customer", "email", "name", "password",
    "person", "phone", "private", "secret", "token", "user",
})
_SINGLE_FAMILY_CATEGORIES = frozenset({
    "analysis", "architecture", "auth", "cleanup", "docs", "essay", "foundation",
    "legacy", "migration", "quiz", "refactor", "release", "telemetry", "test",
    "unclassified", "workflow",
})
_FAMILY_CATEGORY_SUFFIXES = _SINGLE_FAMILY_CATEGORIES | frozenset({
    "accounting", "audit", "browser", "core", "feature", "fix", "hardening",
    "hooks", "integration", "monitoring", "report", "review", "runtime", "service",
    "services", "tests", "tooling", "ui",
})


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
        r"(?<![\w+])(?:\+?\d[\s().-]*){10,15}(?!\w)",
        r"(?<!\w)/(?:Users|home|private|var|etc|tmp|opt|Volumes)(?:/\S*)?",
        r"(?<![\w-])[A-Za-z0-9+/=_-]{20,}(?![\w-])",
    )
    if not normalized or any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in sensitive_patterns):
        return "[redacted]"
    return normalized


def validate_task_family(value: object) -> str:
    """Accept only short categorical codes safe to persist and cohort."""
    segments = () if not isinstance(value, str) else tuple(
        segment for segment in re.split(r"[._-]", value) if segment
    )
    if (
        not isinstance(value, str)
        or len(value) > 80
        or _SAFE_TASK_FAMILY.fullmatch(value) is None
        or _PRIVATE_IDENTIFIER.fullmatch(value) is not None
        or any(segment in _PRIVATE_FAMILY_SEGMENTS for segment in segments)
        or any(re.search(r"\d{3,}", segment) for segment in segments)
        or (
            len(segments) == 1
            and segments[0] not in _SINGLE_FAMILY_CATEGORIES
        )
        or (
            len(segments) > 1
            and segments[-1] not in _FAMILY_CATEGORY_SUFFIXES
        )
    ):
        raise ValueError("task_family must be a privacy-safe category")
    return value


def project_task_family(value: object) -> str | None:
    """Hide unsafe legacy values instead of collapsing them into one cohort."""
    try:
        return validate_task_family(value)
    except ValueError:
        return None
