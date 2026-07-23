"""Select the current trusted Hydra runtime for internal self-launches."""

from __future__ import annotations

from pathlib import Path
import sys


def runtime_command_prefix(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> tuple[str, ...]:
    """Return an argv prefix for this exact source or frozen runtime."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    selected = Path(sys.executable) if executable is None else executable
    if is_frozen:
        return (str(selected),)
    return (str(selected), "-m", "hydra_codex")
