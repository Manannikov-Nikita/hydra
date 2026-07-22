"""Shared privacy guard for every browser-facing Hydra payload."""

from __future__ import annotations

from collections.abc import Mapping


PRIVATE_FIELDS = frozenset({
    "absolute_path", "arguments", "capability", "command", "content",
    "database_path", "message", "path", "project_id", "prompt", "raw",
    "root", "root_id", "root_key", "session_id", "session_ids",
    "session_key", "source_root", "tool_output", "turn_id", "turn_key",
    "worktree_path",
})


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
