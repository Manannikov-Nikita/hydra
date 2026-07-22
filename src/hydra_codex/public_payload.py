"""Shared privacy guard for every browser-facing Hydra payload."""

from __future__ import annotations

from collections.abc import Mapping
import re

from .redaction import redact_note
from .project import normalize_project_display_name


PRIVATE_FIELDS = frozenset({
    "absolute_path", "arguments", "capability", "command", "content",
    "database_path", "message", "path", "project_id", "prompt", "raw",
    "root", "root_id", "root_key", "session_id", "session_ids",
    "session_key", "source_root", "tool_output", "turn_id", "turn_key",
    "worktree_path",
})

_PRIVATE_PATH_FRAGMENT = re.compile(r"(?<![A-Za-z0-9/])/(?![/\s])")
_BACKSLASH_FRAGMENT = re.compile(r"\\")
_WINDOWS_PATH_FRAGMENT = re.compile(r"(?<!\w)[A-Za-z]:[\\/]")
_UNC_PATH_FRAGMENT = re.compile(r"(?:^|\s)\\\\[^\s\\]+[\\/]")
_DOUBLE_SLASH_FRAGMENT = re.compile(r"(?<![:/])//[^/\s]+/")
_FILE_URI_FRAGMENT = re.compile(r"\bfile://", re.IGNORECASE)
_TILDE_PATH_FRAGMENT = re.compile(r"(?:^|\s)~/")
_UUID_FRAGMENT = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f])",
    re.IGNORECASE,
)
_LONG_TOKEN_FRAGMENT = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{20,}(?![A-Za-z0-9+/=_-])",
)
_HEX_IDENTIFIER_FRAGMENT = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{32,64}(?![0-9a-f])", re.IGNORECASE,
)
_INTERNAL_ID_FRAGMENT = re.compile(
    r"\b(?:session|turn|project|task|rollout|refresh|hpilot(?:_v1)?|hstorage(?:_v1)?)"
    r"[_-][A-Za-z0-9_-]+\b",
    re.IGNORECASE,
)
_EXCEPTION_FRAGMENT = re.compile(
    r"\b(?:Traceback|Errno|[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b",
    re.IGNORECASE,
)
_SHELL_FRAGMENT = re.compile(
    r"(?:^|\s)(?:sudo|rm|curl|wget|python(?:3(?:\.\d+)?)?|bash|zsh|sh|git|npm|npx|env|"
    r"hydra-codex|uv|docker|gh|pytest|ruff|make|cat|sed|rg|cp|mv|ssh|ls)\s+",
    re.IGNORECASE,
)
_SHELL_OPERATOR_FRAGMENT = re.compile(
    r";|\||&&|\|\||\$\(|`|(?:^|\s)(?:>>?|<)(?=\s|$)",
)


def _starts_with_uppercase_letter(value: str) -> bool:
    for character in value:
        if character.isalpha() and character.lower() != character.upper():
            return character == character.upper()
    return False


def _contains_opaque_long_token(value: str) -> bool:
    if _HEX_IDENTIFIER_FRAGMENT.search(value) is not None:
        return True
    return any(
        any(character.isdigit() for character in match.group())
        for match in _LONG_TOKEN_FRAGMENT.finditer(value)
    )


def _redaction_probe(value: str) -> str:
    """Keep long letters-only title words from looking like opaque secrets."""
    return _LONG_TOKEN_FRAGMENT.sub(
        lambda match: "Word" if match.group().isalpha() else match.group(),
        value,
    )


def is_safe_dashboard_display_name(value: object, private_project_id: str) -> bool:
    """Validate the one trusted browser text field against private fragments."""
    try:
        normalized = normalize_project_display_name(value)
    except ValueError:
        return False
    if (
        not isinstance(value, str)
        or normalized != value
        or not isinstance(private_project_id, str)
        or not private_project_id
        or not _starts_with_uppercase_letter(value)
        or redact_note(_redaction_probe(value)) != _redaction_probe(value)
        or _contains_opaque_long_token(value)
        or re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(private_project_id)}(?![A-Za-z0-9_-])",
            value,
        ) is not None
    ):
        return False
    return not any(pattern.search(value) for pattern in (
        _PRIVATE_PATH_FRAGMENT,
        _BACKSLASH_FRAGMENT,
        _WINDOWS_PATH_FRAGMENT,
        _UNC_PATH_FRAGMENT,
        _DOUBLE_SLASH_FRAGMENT,
        _FILE_URI_FRAGMENT,
        _TILDE_PATH_FRAGMENT,
        _UUID_FRAGMENT,
        _INTERNAL_ID_FRAGMENT,
        _EXCEPTION_FRAGMENT,
        _SHELL_FRAGMENT,
        _SHELL_OPERATOR_FRAGMENT,
    ))


def reject_private_fields(value: object, *, _enum_keys: bool = False) -> None:
    """Reject private field names at any depth in a public structure."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("public payload field names must be text")
            if not _enum_keys and key in PRIVATE_FIELDS:
                raise ValueError("public payload contains a private field")
            # Canonical report count maps use semantic vocabulary (for example
            # ``prompt``) as dimension values, not as browser field names.
            reject_private_fields(nested, _enum_keys=key.endswith("_counts"))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            reject_private_fields(nested, _enum_keys=_enum_keys)
