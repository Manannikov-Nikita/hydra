"""Read-only adapter for the `hydra.codex-rollout/v1` JSONL boundary."""

from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from typing import Any, Iterable

from .classifier import classify_test_command, classify_test_outcome
from .project import ProjectNotFound, resolve_project
from .storage import HydraStore


KNOWN_ENVELOPES = {"session_meta", "turn_context", "event_msg", "response_item"}
_ACTIVE_HASHER: ContextVar["Pseudonymizer | None"] = ContextVar("hydra_rollout_hasher", default=None)


@dataclass(frozen=True)
class IngestReport:
    files_seen: int
    unique_sources: int
    diagnostics: int


@dataclass(frozen=True)
class RolloutRoot:
    path: Path | str
    label: str = "explicit_root"


@dataclass(frozen=True)
class Pseudonymizer:
    key: bytes

    @classmethod
    def installation(cls, directory: Path) -> "Pseudonymizer":
        path = directory / "rollout-hmac.key"
        if path.exists():
            return cls(path.read_bytes())
        key = secrets.token_bytes(32)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return cls(key)

    def digest(self, value: str) -> str:
        return hmac.new(self.key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def opaque(value: str) -> str:
    hasher = _ACTIVE_HASHER.get()
    if hasher is None:
        raise RuntimeError("rollout pseudonymizer is required")
    return hasher.digest(value)


def discover_rollouts(roots: Iterable[Path | str | RolloutRoot]) -> tuple[Path, ...]:
    """Discover only explicit JSONL roots; no SQLite or rollout mutation occurs."""
    found: set[Path] = set()
    for root in roots:
        path = Path(root.path if isinstance(root, RolloutRoot) else root)
        if path.is_file() and path.suffix == ".jsonl":
            found.add(path.resolve())
        elif path.is_dir():
            found.update(candidate.resolve() for candidate in path.rglob("*.jsonl") if candidate.is_file())
    return tuple(sorted(found))


def _safe_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _fingerprint(value: Any) -> str:
    if not isinstance(value, dict):
        return type(value).__name__
    shape = ",".join(f"{key}:{type(item).__name__}" for key, item in sorted(value.items()))
    return "shape/" + opaque(shape)[:32]


def _path_key(value: Any, project_root: Path) -> str:
    if not isinstance(value, str):
        return "unknown"
    candidate = Path(value)
    if not candidate.is_absolute() and ".." not in candidate.parts:
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return "external/" + opaque(value)[:20]


def _usage(payload: dict[str, Any]) -> dict[str, int] | None:
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        return None
    return {
        "input": _safe_int(usage.get("input_tokens")), "cached": _safe_int(usage.get("cached_input_tokens")),
        "output": _safe_int(usage.get("output_tokens")), "reasoning": _safe_int(usage.get("reasoning_output_tokens")),
        "cache_write": _safe_int(usage.get("cache_write_input_tokens")),
        "vendor_total": _safe_int(usage.get("total_tokens")),
        "context_window": _safe_int(info.get("model_context_window")),
        "complete": int(all(field in usage for field in ("input_tokens", "cached_input_tokens", "output_tokens"))),
    }


def _insert_diagnostic(connection: Any, source: str, line: int, kind: str, payload: Any) -> None:
    connection.execute(
        """INSERT INTO rollout_diagnostics(source_digest, line_number, envelope_kind, fingerprint)
           VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        (source, line, kind, _fingerprint(payload)),
    )


def _upsert_turn(connection: Any, session: str, turn: str, state: str, timestamp: Any, duration: int | None = None) -> None:
    connection.execute(
        """INSERT INTO turn_attempts(session_key, turn_key, attempt_ordinal, state, emitted_duration_ms, wall_duration_ms, started_at, finished_at)
           VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
           ON CONFLICT(session_key, turn_key, attempt_ordinal) DO UPDATE SET
             state = excluded.state, emitted_duration_ms = COALESCE(excluded.emitted_duration_ms, turn_attempts.emitted_duration_ms),
             started_at = COALESCE(turn_attempts.started_at, excluded.started_at), finished_at = COALESCE(excluded.finished_at, turn_attempts.finished_at)""",
        (session, opaque(turn), state, duration, timestamp if state == "open" else None, timestamp if state != "open" else None),
    )


def _tool_end_state(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if result == "Ok" or (isinstance(result, dict) and "Ok" in result) or payload.get("success") is True:
        return "success"
    if result is not None or payload.get("success") is False:
        return "failed"
    return "unknown"


def _duration_ms(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    seconds, nanos = _safe_int(value.get("secs")), _safe_int(value.get("nanos"))
    return seconds * 1000 + nanos // 1_000_000


def _parse_arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_file(connection: Any, source: str, line: int, session: str, operation: str, value: Any, project_root: Path) -> None:
    path = _path_key(value, project_root)
    if path == "unknown":
        return
    connection.execute(
        """INSERT INTO file_observations(source_digest, line_number, session_key, operation, relative_path, path_hash)
           VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        (source, line, session, operation, path, opaque(path)),
    )


def _parse_source(
    store: HydraStore, path: Path, source: str, project_root: Path, project_id: str,
    model_causes: dict[str, str],
) -> int:
    diagnostics = 0
    session_key: str | None = None
    seen_session = False
    epochs: dict[str, tuple[int, tuple[int, int, int, int, int]]] = {}
    failed_commands: set[str] = set()
    test_calls: dict[str, tuple[str, str, str]] = {}
    with store.rollout_transaction() as connection, path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            try:
                envelope = json.loads(raw_line)
            except json.JSONDecodeError:
                diagnostics += 1
                _insert_diagnostic(connection, source, line_number, "malformed", {})
                continue
            if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
                diagnostics += 1
                _insert_diagnostic(connection, source, line_number, "malformed", envelope)
                continue
            kind, payload = envelope.get("type"), envelope["payload"]
            event_key = opaque("event/" + (session_key or "") + "/" + json.dumps(envelope, sort_keys=True, separators=(",", ":")))
            known_event = connection.execute("SELECT 1 FROM rollout_event_keys WHERE event_key = ?", (event_key,)).fetchone()
            if known_event is not None and kind != "session_meta":
                continue
            if known_event is None:
                connection.execute(
                    "INSERT INTO rollout_event_keys(event_key, source_digest, source_ordinal) VALUES (?, ?, ?)",
                    (event_key, source, line_number),
                )
            if kind not in KNOWN_ENVELOPES:
                diagnostics += 1
                _insert_diagnostic(connection, source, line_number, str(kind), payload)
                continue
            if kind == "session_meta":
                identity = payload.get("session_id", payload.get("id"))
                identity = payload.get("id", payload.get("session_id"))
                if not isinstance(identity, str) or not identity:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "session_meta", payload)
                    continue
                next_key = opaque(identity)
                conversation = payload.get("session_id", identity)
                try:
                    resolved = resolve_project(payload.get("cwd"))
                except (ProjectNotFound, TypeError, ValueError, OSError):
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "unresolved_project", payload)
                    continue
                if resolved.project_id != project_id:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "unrelated_project", payload)
                    continue
                if seen_session and next_key != session_key:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "multiple_sessions", payload)
                session_key = next_key
                existing = connection.execute("SELECT resume_segments FROM rollout_sessions WHERE session_key = ?", (session_key,)).fetchone()
                path_key = _path_key(payload.get("cwd"), project_root)
                connection.execute(
                    """INSERT INTO rollout_sessions(session_key, project_id, path_key, resume_segments, conversation_key)
                       VALUES (?, ?, ?, 1, ?) ON CONFLICT(session_key) DO UPDATE SET
                         resume_segments = rollout_sessions.resume_segments + 1""",
                    (session_key, project_id, path_key, opaque(conversation) if isinstance(conversation, str) else next_key),
                )
                if existing is None:
                    parent = payload.get("parent_thread_id")
                    if isinstance(parent, str):
                        connection.execute(
                            """INSERT INTO session_edges(child_key, parent_key, baseline_working_tokens, confidence_kind, confidence)
                               VALUES (?, ?, ?, ?, ?) ON CONFLICT(child_key) DO NOTHING""",
                            (session_key, opaque(parent), None, "confirmed", 1.0),
                        )
                seen_session = True
                continue
            if kind == "event_msg" and payload.get("type") == "token_count":
                usage = _usage(payload)
                if usage is None or session_key is None:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "token_count", payload)
                    continue
                vector = (usage["input"], usage["cached"], usage["output"], usage["reasoning"], usage["cache_write"])
                prior_epoch, prior = epochs.get(session_key, (0, vector))
                epoch = prior_epoch + 1 if any(current < previous for current, previous in zip(vector, prior)) else prior_epoch
                if epoch > prior_epoch:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "counter_reset", {"fields": "token_vector"})
                epochs[session_key] = (epoch, vector)
                completeness = "complete" if usage["complete"] else "partial"
                connection.execute(
                    """INSERT INTO token_snapshots(source_digest, line_number, session_key, project_id, epoch, input_tokens,
                       cached_input_tokens, output_tokens, reasoning_tokens, cache_write_tokens, vendor_total, context_window, completeness)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                    (source, line_number, session_key, project_id, epoch, usage["input"], usage["cached"], usage["output"],
                     usage["reasoning"], usage["cache_write"], usage["vendor_total"] or None, usage["context_window"] or None, completeness),
                )
                edge = connection.execute(
                    "SELECT parent_key FROM session_edges WHERE child_key = ?", (session_key,)
                ).fetchone()
                if edge is not None and connection.execute(
                    "SELECT confidence_kind FROM session_edges WHERE child_key = ?", (session_key,)
                ).fetchone()[0] == "confirmed":
                    connection.execute(
                        """INSERT INTO fork_baselines(child_key, source_digest, line_number, input_tokens, cached_input_tokens,
                           output_tokens, reasoning_tokens, cache_write_tokens, provenance)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'exact') ON CONFLICT(child_key) DO NOTHING""",
                        (session_key, source, line_number, usage["input"], usage["cached"], usage["output"], usage["reasoning"], usage["cache_write"]),
                    )
                continue
            event_type = payload.get("type") if kind == "event_msg" else None
            if kind == "event_msg" and event_type == "sub_agent_activity":
                child_identity = payload.get("agent_thread_id")
                if session_key is None or not isinstance(child_identity, str):
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "sub_agent_activity", payload)
                    continue
                child_key = opaque(child_identity)
                connection.execute(
                    """INSERT INTO rollout_sessions(session_key, project_id, path_key, resume_segments)
                       VALUES (?, ?, 'unresolved', 1) ON CONFLICT DO NOTHING""", (child_key, project_id)
                )
                connection.execute(
                    """INSERT INTO session_edges(child_key, parent_key, baseline_working_tokens, confidence_kind, confidence)
                       VALUES (?, ?, NULL, 'inferred', 0.6) ON CONFLICT(child_key) DO NOTHING""",
                    (child_key, session_key),
                )
                continue
            if kind == "event_msg" and event_type in {"task_started", "task_complete", "turn_aborted"}:
                turn = payload.get("turn_id")
                if not isinstance(turn, str):
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "turn_event", payload)
                    continue
                if session_key is None:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "out_of_order", payload)
                    session_key = "unresolved/" + source[:20]
                _upsert_turn(connection, session_key, turn, {"task_started": "open", "task_complete": "completed", "turn_aborted": "aborted"}[event_type], envelope.get("timestamp"), _safe_int(payload.get("duration_ms")) or None)
                continue
            if kind == "response_item" and payload.get("type") == "custom_tool_call":
                if session_key is not None and isinstance(payload.get("call_id"), str):
                    connection.execute(
                        """INSERT INTO tool_spans(session_key, call_key, category, terminal_state, latency_ms)
                           VALUES (?, ?, 'opaque_exec', 'unknown', NULL) ON CONFLICT DO NOTHING""",
                        (session_key, opaque(payload["call_id"])),
                    )
                continue
            if kind == "response_item" and payload.get("type") == "custom_tool_call_output":
                continue
            if kind == "response_item" and payload.get("type") == "function_call":
                if session_key is None or not isinstance(payload.get("call_id"), str):
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "function_call", payload)
                    continue
                name = payload.get("name") if isinstance(payload.get("name"), str) else "unknown"
                category = "instrumentation" if "hydra" in name.lower() else "tool"
                call_key = opaque(payload["call_id"])
                connection.execute(
                    """INSERT INTO tool_spans(session_key, call_key, category, terminal_state, latency_ms)
                       VALUES (?, ?, ?, 'unknown', NULL) ON CONFLICT DO NOTHING""", (session_key, call_key, category))
                arguments = _parse_arguments(payload.get("arguments"))
                if "path" in arguments:
                    operation = "read" if "read" in name.lower() else "write"
                    _persist_file(connection, source, line_number, session_key, operation, arguments["path"], project_root)
                command = arguments.get("cmd")
                if name == "exec_command" and isinstance(command, str):
                    runner, scope = classify_test_command(command)
                    if runner != "unknown":
                        test_calls[payload["call_id"]] = (opaque(command), runner, scope)
                continue
            if kind == "response_item" and payload.get("type") == "function_call_output":
                call_id = payload.get("call_id")
                pending = test_calls.get(call_id) if isinstance(call_id, str) else None
                if pending is None or session_key is None:
                    continue
                try:
                    result = json.loads(payload.get("output", ""))
                except (TypeError, json.JSONDecodeError):
                    result = {}
                result = result if isinstance(result, dict) else {}
                command_hash, runner, scope = pending
                text = " ".join(str(result.get(field, "")) for field in ("stdout", "stderr", "message"))
                classification, outcome = classify_test_outcome(
                    result.get("exit_code"), text, (command_hash,) if command_hash in failed_commands else ()
                )
                if outcome != "success":
                    failed_commands.add(command_hash)
                connection.execute(
                    """INSERT INTO rollout_test_runs(source_digest, line_number, session_key, command_hash, runner, scope, classification, outcome)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                    (source, line_number, session_key, command_hash, runner, scope, classification, outcome),
                )
                model_cause = model_causes.get(call_id) if isinstance(call_id, str) else None
                if isinstance(model_cause, str) and model_cause != classification:
                    connection.execute(
                        """INSERT INTO semantic_conflicts(conflict_key, source_digest, line_number, deterministic_cause, model_cause)
                           VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                        (opaque(f"{source}:{line_number}"), source, line_number, classification, model_cause),
                    )
                continue
            if kind == "event_msg" and event_type in {"mcp_tool_call_end", "patch_apply_end", "web_search_end"}:
                call_id = payload.get("call_id")
                if session_key is not None and isinstance(call_id, str):
                    connection.execute(
                        """UPDATE tool_spans SET terminal_state = ?, latency_ms = ? WHERE session_key = ? AND call_key = ?""",
                        (_tool_end_state(payload), _duration_ms(payload.get("duration")), session_key, opaque(call_id)),
                    )
                if event_type == "patch_apply_end" and session_key is not None:
                    changes = payload.get("changes")
                    if isinstance(changes, dict):
                        for changed_path in changes:
                            _persist_file(connection, source, line_number, session_key, "write", changed_path, project_root)
                continue
    return diagnostics


def ingest_rollouts(
    store: HydraStore, roots: Iterable[Path | str | RolloutRoot], project_root: Path | str, project_id: str,
    model_causes: dict[str, str] | None = None, hash_key: bytes | None = None,
) -> IngestReport:
    """Ingest explicit v1 JSONL roots idempotently, storing only normalized safe facts."""
    root = Path(project_root)
    _ACTIVE_HASHER.set(Pseudonymizer(hash_key) if hash_key is not None else Pseudonymizer.installation(store.database_path.parent))
    root_specs = tuple(roots)
    files = discover_rollouts(root_specs)
    diagnostics = 0
    unique: set[str] = set()
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        unique.add(digest)
        location = opaque(str(path))
        label = next((item.label for item in root_specs if isinstance(item, RolloutRoot) and path.is_relative_to(Path(item.path).resolve())), "explicit_root")
        with store.rollout_transaction() as connection:
            known = connection.execute("SELECT 1 FROM rollout_sources WHERE source_digest = ?", (digest,)).fetchone() is not None
            connection.execute("INSERT INTO rollout_sources(source_digest, source_type) VALUES (?, 'jsonl') ON CONFLICT DO NOTHING", (digest,))
            connection.execute(
                """INSERT INTO rollout_source_locations(source_digest, location_key, location_type)
                   VALUES (?, ?, ?) ON CONFLICT DO NOTHING""", (digest, location, label),
            )
        if not known:
            diagnostics += _parse_source(store, path, digest, root, project_id, model_causes or {})
    return IngestReport(len(files), len(unique), diagnostics)
