#!/usr/bin/env python3.12
"""Privacy-safe, fail-open cooperative UserPromptSubmit and Stop hook runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
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
from .annotation_spool import drain_annotations
from .project import ProjectResolution, resolve_project
from .rollout_identity import Pseudonymizer
from .platform_paths import default_installation_key_path
from .storage import HydraStore, StorageUnavailable
from .incremental_sync import RepairRequired, TrustedSourceRoots
from .sync_state import SyncStateRepository, validate_root_relative_locator
from .runtime_entrypoint import runtime_command_prefix


Clock = Callable[[], datetime]
StoreFactory = Callable[[Path | None], HydraStore]
KeyLoader = Callable[[Path], Pseudonymizer]
ProjectResolver = Callable[[Path | str], ProjectResolution]

_INITIAL_REQUEST_DOMAIN = "codex-hook-initial-understand-v1"
_CAPABILITY_LIFETIME = timedelta(hours=24)
_PLUGIN_SOURCE = "plugin"
_PROJECT_HOOK_COMMANDS = frozenset({
    'python3.12 "$(git rev-parse --show-toplevel)/integrations/codex/hook.py"',
    "hydra-codex-hook",
})


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
    return default_installation_key_path(home, environ=environ)


def _trusted_source_roots(environ: Mapping[str, str]) -> TrustedSourceRoots:
    home_value = environ.get("HOME")
    home = Path(home_value).expanduser() if isinstance(home_value, str) and home_value else Path.home()
    return TrustedSourceRoots(
        sessions=home / ".codex" / "sessions",
        archived_sessions=home / ".codex" / "archived_sessions",
    )


def _trusted_transcript_locator(
    payload: Mapping[str, Any], roots: TrustedSourceRoots,
) -> tuple[str, str] | None:
    """Convert only a verified trusted-root transcript path to a private locator."""
    raw = payload.get("transcript_path")
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None
    for root_kind in ("sessions", "archived_sessions"):
        try:
            root = roots.root_for(root_kind)
            locator = validate_root_relative_locator(candidate.relative_to(root).as_posix())
            roots.resolve(root_kind, locator)
            return root_kind, locator
        except (ValueError, OSError, RepairRequired):
            continue
    return None


def _enqueue_hook_source(
    payload: Mapping[str, Any], project: ProjectResolution, store: HydraStore,
    keys: Pseudonymizer, environ: Mapping[str, str], observed_at: str,
) -> tuple[str, str] | None:
    """Find one trusted transcript, never retaining a caller-controlled path."""
    repository = SyncStateRepository(store)
    session_key = keys.digest("identity", _required_text(payload, "session_id"))
    roots = _trusted_source_roots(environ)
    located = _trusted_transcript_locator(payload, roots)
    if located is not None:
        return located
    # Some hook envelopes omit a path.  A prior repair/tail registration can
    # still bind the private session digest to exactly one trusted locator.
    matches = [
        source for source in repository.list_sources() if source.session_key == session_key
    ]
    if len(matches) == 1:
        return matches[0].root_kind, matches[0].source_locator
    return None


def _hook_safe_fact(payload: Mapping[str, Any], event: str) -> tuple[str, str | None, str | None, int | None]:
    event_kind = {"UserPromptSubmit": "prompt", "PostToolUse": "post_tool", "Stop": "stop"}[event]
    if event != "PostToolUse":
        return event_kind, None, None, None
    raw_category = payload.get("tool_category")
    category = raw_category if raw_category in {"shell", "read", "write", "search", "browser"} else "other"
    raw_status = payload.get("tool_status")
    status = raw_status if raw_status in {"success", "failure"} else "unknown"
    duration = payload.get("duration_ms")
    safe_duration = duration if isinstance(duration, int) and not isinstance(duration, bool) and 0 <= duration <= 86_400_000 else None
    return event_kind, category, status, safe_duration


def _private_tool_event_identity(
    payload: Mapping[str, Any], keys: Pseudonymizer, session_key: str, turn_key: str,
    category: str | None, status: str | None, duration: int | None,
) -> str:
    """Return a non-reversible tool identity; id-less identical facts coalesce."""
    for field in ("tool_use_id", "tool_call_id", "call_id"):
        value = payload.get(field)
        if (
            isinstance(value, str) and 1 <= len(value) <= 512
            and not any(character in value for character in ("\0", "\r", "\n"))
        ):
            return keys.digest(
                "event", f"hook-tool-call-id/v1/{session_key}/{turn_key}/{value}",
            )
    # Without a stable hook call id, do not use arbitrary payload values.
    # This intentionally coalesces retries and indistinguishable safe facts.
    return keys.digest(
        "event", f"hook-tool-fallback/v1/{session_key}/{turn_key}/{category}/{status}/{duration}",
    )


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
        worktree_path=project.worktree_path.as_posix(),
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
            if (
                hook.get("type") == "command"
                and isinstance(command, str)
                and command.strip() in _PROJECT_HOOK_COMMANDS
            ):
                return True
    return False


def _annotation_command(command_prefix: tuple[str, ...]) -> str:
    if (
        not command_prefix
        or any(not isinstance(part, str) or not part for part in command_prefix)
    ):
        raise ValueError("invalid Hydra runtime command")
    return (
        "HYDRA_TURN_CAPABILITY={capability} "
        + shlex.join((*command_prefix, "annotate"))
    )


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


def _finish_command(capability: str, annotation_command: str) -> str:
    return (
        annotation_command.replace("{capability}", capability)
        + " --kind finish --phase test_full --cause final_verification"
        + " --outcome success --scope-change none --task-family unclassified"
        + ' --confidence 1 --note "done"'
    )


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
    annotation_command: str,
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
        command = _finish_command(issued.token, annotation_command)
        return {
            "decision": "block",
            "reason": f"Hydra: run `{command}` before ending this turn.",
        }
    return {}


def handle_event(
    payload: object,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    store_factory: StoreFactory = HydraStore,
    key_loader: KeyLoader = Pseudonymizer.installation_key,
    project_resolver: ProjectResolver = resolve_project,
    annotation_command: str | None = None,
    command_prefix: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Handle one configured Codex hook envelope; every error fails open.

    The envelope is hook-attested in the normal Codex workflow, not
    cryptographically authenticated against a local process invoking this CLI.
    """
    try:
        if not isinstance(payload, Mapping):
            return {}
        event = payload.get("hook_event_name")
        if event not in {"UserPromptSubmit", "PostToolUse", "Stop"}:
            return {}
        cwd = _required_text(payload, "cwd")
        _required_text(payload, "session_id")
        _required_text(payload, "turn_id")
        if event == "Stop" and not isinstance(payload.get("stop_hook_active"), bool):
            return {}
        if annotation_command is not None and command_prefix is not None:
            return {}
        selected_command = (
            _annotation_command(
                runtime_command_prefix() if command_prefix is None else command_prefix,
            )
            if annotation_command is None
            else annotation_command
        )
        if (
            not selected_command.strip()
            or len(selected_command) > 512
            or selected_command.count("{capability}") != 1
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
            drain_annotations(
                environment,
                store,
                keys,
                project_id=project.project_id,
                session_id=_required_text(payload, "session_id"),
                turn_id=_required_text(payload, "turn_id"),
                observed_at=observed_at,
                allow_session_turns=event == "UserPromptSubmit",
            )
            source = _enqueue_hook_source(payload, project, store, keys, environment, observed_at)
            event_kind, category, status, duration = _hook_safe_fact(payload, event)
            session_key = keys.digest("identity", _required_text(payload, "session_id"))
            turn_key = keys.digest("turn", _required_text(payload, "turn_id"))
            event_identity = (
                _private_tool_event_identity(
                    payload, keys, session_key, turn_key, category, status, duration,
                )
                if event == "PostToolUse" else turn_key
            )
            event_key = keys.digest(
                "event", f"hook-outbox/v1/{event_kind}/{session_key}/{event_identity}",
            )
            SyncStateRepository(store).record_hook_event_and_enqueue(
                event_key=event_key, project_id=project.project_id, session_key=session_key,
                turn_key=turn_key, event_kind=event_kind, observed_at=observed_at,
                tool_category=category, tool_status=status, duration_ms=duration, source=source,
            )
            if event == "PostToolUse":
                return {}
            if event == "UserPromptSubmit":
                return _handle_prompt(
                    payload, project, store, keys, now, observed_at, selected_command,
                )
            return _handle_stop(
                payload, project, store, keys, now, observed_at, selected_command,
            )
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
    annotation_command: str | None = None,
    command_prefix: tuple[str, ...] | None = None,
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
        command_prefix=command_prefix,
    )
    output_stream.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def main() -> int:
    """Run the installed hook entrypoint."""
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
