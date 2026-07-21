#!/usr/bin/env python3.12
"""Checkout-local wrapper for Hydra's packaged Codex hook runtime."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sys
from typing import Any, TextIO


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hydra_codex.hook_runtime import (  # noqa: E402
    Clock,
    KeyLoader,
    ProjectResolver,
    StoreFactory,
    handle_event as _handle_event,
    run as _run,
)


_CHECKOUT_ANNOTATION_COMMAND = (
    'env PYTHONPATH="$(git rev-parse --show-toplevel)/src" '
    "HYDRA_TURN_CAPABILITY={capability} python3.12 -m hydra_codex annotate"
)


def handle_event(
    payload: object,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    store_factory: StoreFactory | None = None,
    key_loader: KeyLoader | None = None,
    project_resolver: ProjectResolver | None = None,
) -> dict[str, object]:
    """Handle an event with the source-tree annotation fallback."""
    options: dict[str, Any] = {
        "environ": environ,
        "clock": clock,
        "annotation_command": _CHECKOUT_ANNOTATION_COMMAND,
    }
    if store_factory is not None:
        options["store_factory"] = store_factory
    if key_loader is not None:
        options["key_loader"] = key_loader
    if project_resolver is not None:
        options["project_resolver"] = project_resolver
    return _handle_event(payload, **options)


def run(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
) -> int:
    """Run the checkout-local hook wrapper."""
    return _run(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environ=environ,
        clock=clock,
        annotation_command=_CHECKOUT_ANNOTATION_COMMAND,
    )


if __name__ == "__main__":
    raise SystemExit(run())
