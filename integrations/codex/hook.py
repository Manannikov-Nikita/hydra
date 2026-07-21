#!/usr/bin/env python3.12
"""Privacy-safe, fail-open UserPromptSubmit and Stop hooks for Codex."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hydra_codex.annotation_core import (  # noqa: E402
    StopState,
    TrustedAnnotationContext,
    TrustedTurnContext,
    issue_capability,
    observe_stop,
    record_initial_understand,
)
from hydra_codex.project import ProjectResolution, resolve_project  # noqa: E402
from hydra_codex.rollout_identity import Pseudonymizer  # noqa: E402
from hydra_codex.storage import HydraStore, StorageUnavailable, default_database_path  # noqa: E402


Clock = Callable[[], datetime]
StoreFactory = Callable[[Path | None], HydraStore]
KeyLoader = Callable[[Path], Pseudonymizer]
ProjectResolver = Callable[[Path | str], ProjectResolution]

_INITIAL_REQUEST_DOMAIN = "codex-hook-initial-understand-v1"
_CAPABILITY_LIFETIME = timedelta(hours=24)
_STOP_REASON = "Hydra: add one finish annotation with outcome before ending this turn."


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"invalid {field}")
    return value


def _observed_at(clock: Clock) -> tuple[datetime, str]:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("hook clock must return an aware datetime")
    utc = value.astimezone(timezone.utc)
    return utc, utc.isoformat().replace("+00:00", "Z")


def _database_path(environ: Mapping[str, str]) -> Path | None:
    value = environ.get("HYDRA_DATABASE_PATH")
    return Path(value).expanduser() if isinstance(value, str) and value else None


def _key_path(environ: Mapping[str, str]) -> Path:
    configured = environ.get("HYDRA_INSTALLATION_KEY_PATH")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    home_value = environ.get("HOME")
    home = Path(home_value).expanduser() if isinstance(home_value, str) and home_value else Path.home()
    return default_database_path(home).parent / "rollout-hmac.key"


def _open_store(factory: StoreFactory, path: Path | None) -> HydraStore:
    # A concurrent first launch can observe a migration loser. Reopening once
    # more sees the winner's committed schema while persistent failures stay quiet.
    last_error: StorageUnavailable | None = None
    for _attempt in range(3):
        try:
            return factory(path)
        except StorageUnavailable as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _turn_context(
    payload: Mapping[str, Any], project: ProjectResolution, observed_at: str,
) -> TrustedTurnContext:
    return TrustedTurnContext(
        project_id=project.project_id,
        session_id=_required_text(payload, "session_id"),
        turn_id=_required_text(payload, "turn_id"),
        observed_at=observed_at,
    )


def _instruction(capability: str) -> dict[str, object]:
    message = (
        "Hydra telemetry for this turn. On each phase change run "
        "`env PYTHONPATH=\"$(git rev-parse --show-toplevel)/src\" "
        f"HYDRA_TURN_CAPABILITY={capability} python3.12 -m hydra_codex annotate "
        "--kind phase --phase implement --cause plan --scope-change none "
        "--task-family task --confidence 0.9 --note \"phase change\"`. "
        "Before the final answer run "
        "`env PYTHONPATH=\"$(git rev-parse --show-toplevel)/src\" "
        f"HYDRA_TURN_CAPABILITY={capability} python3.12 -m hydra_codex annotate "
        "--kind finish --phase test_full --cause final_verification --outcome success "
        "--scope-change none --task-family task --confidence 1 --note \"done\"`. "
        "Replace only semantic values; never submit tokens, time, file/test counts, "
        "session_id, or turn_id."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": message,
        },
    }


def _handle_prompt(
    payload: Mapping[str, Any], project: ProjectResolution, store: HydraStore,
    keys: Pseudonymizer, now: datetime, observed_at: str,
) -> dict[str, object]:
    context = _turn_context(payload, project, observed_at)
    issued = issue_capability(
        store,
        keys,
        context,
        expires_at=(now + _CAPABILITY_LIFETIME).isoformat().replace("+00:00", "Z"),
    )
    request_key = keys.digest(
        "event", f"{_INITIAL_REQUEST_DOMAIN}/{context.session_id}/{context.turn_id}",
    )
    record_initial_understand(
        store,
        keys,
        issued.token,
        TrustedAnnotationContext(
            request_key=request_key,
            sequence=0,
            observed_at=observed_at,
        ),
        task_family="unclassified",
    )
    return _instruction(issued.token)


def _handle_stop(
    payload: Mapping[str, Any], project: ProjectResolution, store: HydraStore,
    keys: Pseudonymizer, now: datetime, observed_at: str,
) -> dict[str, object]:
    active = payload.get("stop_hook_active")
    if not isinstance(active, bool):
        raise ValueError("invalid stop_hook_active")
    context = _turn_context(payload, project, observed_at)
    issued = issue_capability(
        store,
        keys,
        context,
        expires_at=(now + _CAPABILITY_LIFETIME).isoformat().replace("+00:00", "Z"),
    )
    state = observe_stop(store, keys, issued.token, observed_at=observed_at)
    if state is StopState.RETRY_REQUIRED:
        if active:
            observe_stop(store, keys, issued.token, observed_at=observed_at)
            return {}
        return {"decision": "block", "reason": _STOP_REASON}
    return {}


def handle_event(
    payload: object,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    store_factory: StoreFactory = HydraStore,
    key_loader: KeyLoader = Pseudonymizer.installation_key,
    project_resolver: ProjectResolver = resolve_project,
) -> dict[str, object]:
    """Handle one trusted Codex hook envelope; every error fails open."""
    try:
        if not isinstance(payload, Mapping):
            return {}
        event = payload.get("hook_event_name")
        if event not in {"UserPromptSubmit", "Stop"}:
            return {}
        cwd = _required_text(payload, "cwd")
        _required_text(payload, "session_id")
        _required_text(payload, "turn_id")
        if event == "Stop" and not isinstance(payload.get("stop_hook_active"), bool):
            return {}
        environment = os.environ if environ is None else environ
        now, observed_at = _observed_at(
            (lambda: datetime.now(timezone.utc)) if clock is None else clock,
        )
        project = project_resolver(cwd)
        keys = key_loader(_key_path(environment))
        store = _open_store(store_factory, _database_path(environment))
        try:
            if event == "UserPromptSubmit":
                return _handle_prompt(payload, project, store, keys, now, observed_at)
            return _handle_stop(payload, project, store, keys, now, observed_at)
        finally:
            store.close()
    except Exception:
        return {}


def run(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
) -> int:
    """Read one hook JSON envelope and always emit one private JSON response."""
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    _ = stderr  # Errors are deliberately not surfaced by fail-open telemetry.
    try:
        payload: object = json.load(input_stream)
    except Exception:
        payload = None
    response = handle_event(payload, environ=environ, clock=clock)
    output_stream.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
