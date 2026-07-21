#!/usr/bin/env python3.12
"""Privacy-safe, fail-open cooperative UserPromptSubmit and Stop hook runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO

from .annotation_core import (
    StopState,
    TrustedAnnotationContext,
    TrustedTurnContext,
    issue_capability,
    observe_stop,
    record_initial_understand,
)
from .project import ProjectResolution, resolve_project
from .rollout_identity import Pseudonymizer
from .storage import HydraStore, StorageUnavailable, default_database_path


Clock = Callable[[], datetime]
StoreFactory = Callable[[Path | None], HydraStore]
KeyLoader = Callable[[Path], Pseudonymizer]
ProjectResolver = Callable[[Path | str], ProjectResolution]

_INITIAL_REQUEST_DOMAIN = "codex-hook-initial-understand-v1"
_CAPABILITY_LIFETIME = timedelta(hours=24)
_STOP_REASON = "Hydra: add one finish annotation with outcome before ending this turn."
_INSTALLED_ANNOTATION_COMMAND = (
    "HYDRA_TURN_CAPABILITY={capability} hydra-codex annotate"
)
_PLUGIN_SOURCE = "plugin"


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


def _project_hook_owns_event(
    project: ProjectResolution,
    event: str,
    environ: Mapping[str, str],
) -> bool:
    """Let an explicit project Hydra hook shadow the post-pilot plugin hook."""
    if environ.get("HYDRA_CODEX_HOOK_SOURCE") != _PLUGIN_SOURCE:
        return False
    manifest_path = project.project_root / ".codex" / "hooks.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, Mapping):
        return False
    hooks = manifest.get("hooks")
    if not isinstance(hooks, Mapping):
        return False
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return False
    for group in groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
            continue
        for hook in group["hooks"]:
            if not isinstance(hook, Mapping):
                continue
            command = hook.get("command")
            if isinstance(command, str) and (
                "integrations/codex/hook.py" in command
                or command.strip() == "hydra-codex-hook"
            ):
                return True
    return False


def _instruction(capability: str, annotation_command: str) -> dict[str, object]:
    command = annotation_command.replace("{capability}", capability)
    message = (
        "Hydra telemetry for this turn. On each phase change run "
        f"`{command} "
        "--kind phase --phase implement --cause plan --scope-change none "
        "--task-family unclassified --confidence 0.9 --note \"phase change\"`. "
        "Before the final answer run "
        f"`{command} "
        "--kind finish --phase test_full --cause final_verification --outcome success "
        "--scope-change none --task-family unclassified --confidence 1 --note \"done\"`. "
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
    keys: Pseudonymizer, now: datetime, observed_at: str, annotation_command: str,
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
    return _instruction(issued.token, annotation_command)


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
    state = observe_stop(
        store,
        keys,
        issued.token,
        observed_at=observed_at,
        retry_active=active,
        retry_expires_at=issued.expires_at,
    )
    if state is StopState.RETRY_REQUIRED:
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
    annotation_command: str = _INSTALLED_ANNOTATION_COMMAND,
) -> dict[str, object]:
    """Handle one configured Codex hook envelope; every error fails open.

    The envelope is hook-attested in the normal Codex workflow, not
    cryptographically authenticated against a local process invoking this CLI.
    """
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
        if (
            not annotation_command.strip()
            or len(annotation_command) > 512
            or annotation_command.count("{capability}") != 1
        ):
            return {}
        environment = os.environ if environ is None else environ
        now, observed_at = _observed_at(
            (lambda: datetime.now(timezone.utc)) if clock is None else clock,
        )
        project = project_resolver(cwd)
        if _project_hook_owns_event(project, event, environment):
            return {}
        keys = key_loader(_key_path(environment))
        store = _open_store(store_factory, _database_path(environment))
        try:
            if event == "UserPromptSubmit":
                return _handle_prompt(
                    payload, project, store, keys, now, observed_at, annotation_command,
                )
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
    annotation_command: str = _INSTALLED_ANNOTATION_COMMAND,
) -> int:
    """Read one hook JSON envelope and always emit one private JSON response."""
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    _ = stderr  # Errors are deliberately not surfaced by fail-open telemetry.
    try:
        payload: object = json.load(input_stream)
    except Exception:
        payload = None
    response = handle_event(
        payload,
        environ=environ,
        clock=clock,
        annotation_command=annotation_command,
    )
    output_stream.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def main() -> int:
    """Run the installed hook entrypoint."""
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
