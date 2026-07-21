"""Read-only adapter for the `hydra.codex-rollout/v1` JSONL boundary."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .custom_tool_persistence import persist_custom_tool_call
from .function_normalization import normalize_function_call
from .lineage import (
    assert_session_project,
    persist_confirmed_parent,
    persist_inferred_parent,
)
from .project import ProjectNotFound, resolve_project
from .rollout_identity import ACTIVE_HASHER, IngestReport, Pseudonymizer, RolloutRoot, discover_rollouts, opaque
from .rollout_observations import fingerprint as observation_fingerprint
from .rollout_observations import path_key, safe_int, usage as parse_usage
from .rollout_privacy import (
    KNOWN_ENVELOPES, KNOWN_EVENT_TYPES, KNOWN_RESPONSE_TYPES,
    canonical_timestamp, nonempty_string, safe_envelope_kind,
)
from .rollout_persistence import duration_ms as _duration_ms
from .rollout_persistence import insert_diagnostic as _persist_diagnostic
from .rollout_persistence import persist_file as _persist_file
from .rollout_persistence import tool_end_state as _tool_end_state
from .rollout_reconcile import reconcile_fork_baselines, reconcile_token_epochs, reconcile_turn_attempts
from .rollout_sources import line_fingerprint, located_lineage, prefix_lineage, relation_to, revision_lines, scan_source
from .storage import HydraStore
from .test_evidence import TestEvidenceBuffer, parse_structured_result
from .tool_normalization import custom_exec_outcome
from .tool_spans import persist_tool_end, persist_tool_start


SOURCE_SCANNER_VERSION = 1


def _source_stat(path: Path) -> tuple[int, int, int, int, int]:
    details = path.stat()
    return (
        int(details.st_dev), int(details.st_ino), int(details.st_size),
        int(details.st_mtime_ns), int(details.st_ctime_ns),
    )


def _unchanged_location(
    connection: Any, project_id: str, location: str,
    source_stat: tuple[int, int, int, int, int],
) -> tuple[str, str] | None:
    row = connection.execute(
        """SELECT state.logical_source_key,state.revision_digest
             FROM rollout_source_location_states AS state
             JOIN rollout_logical_sources AS logical
               ON logical.logical_source_key=state.logical_source_key
              AND logical.project_id=state.project_id
             JOIN rollout_sources AS revision
               ON revision.source_digest=state.revision_digest
              AND revision.logical_source_key=state.logical_source_key
              AND revision.materialized=1
             JOIN rollout_source_locations AS location
               ON location.logical_source_key=state.logical_source_key
              AND location.location_key=state.location_key
              AND location.revision_digest=state.revision_digest
            WHERE state.project_id=? AND state.location_key=?
              AND state.st_dev=? AND state.st_ino=? AND state.st_size=?
              AND state.st_mtime_ns=? AND state.st_ctime_ns=?
              AND state.scanner_version=?""",
        (project_id, location, *source_stat, SOURCE_SCANNER_VERSION),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _persist_location_state(
    connection: Any, project_id: str, location: str, logical: str,
    revision: str, source_stat: tuple[int, int, int, int, int],
) -> None:
    connection.execute(
        """INSERT INTO rollout_source_location_states(
               project_id,location_key,logical_source_key,revision_digest,
               st_dev,st_ino,st_size,st_mtime_ns,st_ctime_ns,scanner_version)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id,location_key) DO UPDATE SET
             logical_source_key=excluded.logical_source_key,
             revision_digest=excluded.revision_digest,
             st_dev=excluded.st_dev,
             st_ino=excluded.st_ino,
             st_size=excluded.st_size,
             st_mtime_ns=excluded.st_mtime_ns,
             st_ctime_ns=excluded.st_ctime_ns,
             scanner_version=excluded.scanner_version""",
        (
            project_id, location, logical, revision, *source_stat,
            SOURCE_SCANNER_VERSION,
        ),
    )


def _insert_diagnostic(connection: Any, source: str, line: int, kind: str, payload: Any, *, unsafe_value: Any = None) -> None:
    _persist_diagnostic(
        connection, source, line, kind, payload,
        fingerprint=lambda value: observation_fingerprint(value, opaque), unsafe_value=unsafe_value,
    )


def _trusted_hook_binding(
    connection: Any, session_key: str,
) -> tuple[str, str] | None:
    """Return a hook-attested project/worktree binding, never a rollout claim."""
    row = connection.execute(
        """SELECT sessions.project_id,sessions.worktree_path
             FROM sessions JOIN trusted_turn_bindings
               ON trusted_turn_bindings.session_key=sessions.session_id
              AND trusted_turn_bindings.project_id=sessions.project_id
            WHERE sessions.session_id=?
            ORDER BY trusted_turn_bindings.created_at LIMIT 1""",
        (session_key,),
    ).fetchone()
    if row is None or not isinstance(row[1], str):
        return None
    path = PurePosixPath(row[1])
    if (
        not row[1] or path.is_absolute() or ".." in path.parts
        or bool(path.parts and path.parts[0].endswith(":"))
        or any(character in row[1] for character in ("\\", "\0", "\r", "\n"))
    ):
        return None
    return str(row[0]), row[1]


def _parse_source(
    store: HydraStore, path: Path, source: str, project_root: Path, project_id: str,
    model_causes: dict[str, str], *, logical_source: str | None = None,
    line_fingerprints: tuple[str, ...] = (), authoritative_identity: str | None = None,
) -> int:
    diagnostics = 0
    session_key: str | None = None
    session_meta_at: str | None = None
    current_turn: str | None = None
    seen_session = False
    with store.rollout_transaction() as connection, path.open("rb") as handle:
        logical_binding = (
            connection.execute(
                "SELECT session_key FROM rollout_logical_sources "
                "WHERE logical_source_key=?",
                (logical_source,),
            ).fetchone()
            if logical_source is not None else None
        )
        retry_unbound = bool(
            logical_binding is not None and logical_binding[0] is None
        )
        test_evidence = TestEvidenceBuffer(connection, source, model_causes, opaque)
        # File operands from fallible direct tools stay in memory until the
        # matching structured terminal record proves success.  Values contain
        # only already-normalized relative paths, never raw arguments.
        pending_file_calls: dict[
            str, tuple[str, str, str, tuple[tuple[str, str], ...]]
        ] = {}
        successful_exec_calls: dict[str, tuple[int, str | None, str | None]] = {}
        successful_patch_calls: dict[str, tuple[int, str | None, str | None]] = {}

        def flush_file_call(
            call_id: str, expected_tool: str,
            terminal_line: int, terminal_at: str | None, terminal_turn: str | None,
        ) -> None:
            pending = pending_file_calls.get(call_id)
            if pending is None or pending[0] != expected_tool:
                return
            _, pending_session, call_key, accesses = pending
            for operation, relative_path in accesses:
                _persist_file(
                    connection, source, terminal_line, pending_session,
                    operation, relative_path, project_root, terminal_at,
                    opaque("turn", terminal_turn) if terminal_turn else None,
                    observation_call_key=call_key,
                    observation_tool_name=expected_tool,
                    requires_success=True,
                )
            del pending_file_calls[call_id]

        parsed_lines = 0
        for line_number, raw_bytes in enumerate(handle, start=1):
            parsed_lines = line_number
            active_hasher = ACTIVE_HASHER.get()
            if active_hasher is None or line_number > len(line_fingerprints) or line_fingerprint(raw_bytes, active_hasher.key) != line_fingerprints[line_number - 1]:
                raise RuntimeError("rollout source changed during ingest")
            raw_line = raw_bytes.decode("utf-8", errors="replace")
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
            timestamp = canonical_timestamp(envelope.get("timestamp"))
            if timestamp.quality == "invalid":
                diagnostics += 1
                _insert_diagnostic(
                    connection, source, line_number, "invalid_timestamp", {}, unsafe_value=envelope.get("timestamp"),
                )
            observed_at = timestamp.text
            fingerprint = line_fingerprints[line_number - 1] if line_number <= len(line_fingerprints) else opaque("event", raw_line)
            logical = logical_source or opaque("source", source)
            event_key = opaque("event", f"{logical}/{line_number}/{fingerprint}")
            known_event = connection.execute("SELECT 1 FROM rollout_events WHERE event_key = ?", (event_key,)).fetchone()
            if known_event is None:
                connection.execute(
                    "INSERT INTO rollout_event_keys(event_key, source_digest, source_ordinal) VALUES (?, ?, ?)",
                    (event_key, source, line_number),
                )
                connection.execute(
                    """INSERT INTO rollout_events(
                           event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,timestamp_quality,fingerprint)
                       VALUES (?,?,?,?,?,?,?)""",
                    (event_key, logical, line_number, safe_envelope_kind(kind), observed_at,
                     timestamp.quality, opaque("diagnostic", fingerprint)),
                )
            connection.execute(
                """INSERT INTO rollout_revision_events(revision_digest,event_key,source_ordinal)
                   VALUES (?,?,?) ON CONFLICT DO NOTHING""",
                (source, event_key, line_number),
            )
            if session_key is not None and observed_at is not None:
                connection.execute(
                    """UPDATE rollout_sessions SET last_activity_at=CASE
                           WHEN last_activity_at IS NULL OR ? > last_activity_at THEN ? ELSE last_activity_at END
                       WHERE session_key=?""", (observed_at, observed_at, session_key),
                )
            if kind not in KNOWN_ENVELOPES:
                diagnostics += 1
                _insert_diagnostic(connection, source, line_number, "unknown_envelope", payload, unsafe_value=kind)
                continue
            if kind == "session_meta":
                identity = nonempty_string(payload.get("id"), payload.get("session_id"))
                if identity is None:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "session_meta", payload)
                    continue
                if authoritative_identity is not None and identity != authoritative_identity:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "multiple_sessions", payload)
                    continue
                next_key = opaque("identity", identity)
                conversation = nonempty_string(payload.get("session_id"), identity)
                trusted_binding = _trusted_hook_binding(connection, next_key)
                try:
                    resolved = resolve_project(payload.get("cwd"))
                except (ProjectNotFound, OSError):
                    if trusted_binding is None:
                        diagnostics += 1
                        _insert_diagnostic(
                            connection, source, line_number,
                            "unresolved_project", payload,
                        )
                        continue
                    if trusted_binding[0] != project_id:
                        diagnostics += 1
                        _insert_diagnostic(
                            connection, source, line_number,
                            "unrelated_project", payload,
                        )
                        continue
                    session_path = trusted_binding[1]
                except (TypeError, ValueError):
                    diagnostics += 1
                    _insert_diagnostic(
                        connection, source, line_number,
                        "unresolved_project", payload,
                    )
                    continue
                else:
                    if resolved.project_id != project_id:
                        diagnostics += 1
                        _insert_diagnostic(
                            connection, source, line_number,
                            "unrelated_project", payload,
                        )
                        continue
                    if (
                        trusted_binding is not None
                        and trusted_binding[0] != resolved.project_id
                    ):
                        diagnostics += 1
                        _insert_diagnostic(
                            connection, source, line_number,
                            "unrelated_project", payload,
                        )
                        continue
                    session_path = path_key(payload.get("cwd"), project_root, opaque)
                if trusted_binding is not None and trusted_binding[0] != project_id:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "unrelated_project", payload)
                    continue
                if seen_session and next_key != session_key:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "multiple_sessions", payload)
                    continue
                session_key = next_key
                payload_time = canonical_timestamp(payload.get("timestamp"))
                if not seen_session:
                    session_meta_at = payload_time.text or observed_at
                connection.execute(
                    """INSERT INTO rollout_sessions(
                           session_key,project_id,path_key,resume_segments,conversation_key,started_at,last_activity_at)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_key) DO NOTHING""",
                    (session_key, project_id, session_path, 1, opaque("conversation", conversation) if conversation else next_key,
                     session_meta_at, observed_at),
                )
                assert_session_project(
                    connection, session_key=session_key, project_id=project_id,
                )
                connection.execute(
                    """UPDATE rollout_sessions SET
                         started_at=CASE WHEN started_at IS NULL
                                              OR julianday(?) < julianday(started_at)
                                         THEN ? ELSE started_at END,
                         last_activity_at=CASE WHEN last_activity_at IS NULL
                                                   OR julianday(?) > julianday(last_activity_at)
                                              THEN ? ELSE last_activity_at END
                       WHERE session_key=?""",
                    (
                        session_meta_at, session_meta_at,
                        observed_at, observed_at, session_key,
                    ),
                )
                if not seen_session:
                    persisted_start = connection.execute(
                        "SELECT started_at FROM rollout_sessions WHERE session_key=?", (session_key,),
                    ).fetchone()[0]
                    session_meta_at = persisted_start
                connection.execute(
                    "UPDATE rollout_logical_sources SET session_key=?,project_id=? WHERE logical_source_key=?",
                    (session_key, project_id, logical),
                )
                connection.execute(
                    """INSERT INTO rollout_session_segments(session_key,logical_source_key)
                       VALUES (?,?) ON CONFLICT DO NOTHING""", (session_key, logical),
                )
                connection.execute(
                    """UPDATE rollout_sessions SET resume_segments=(
                           SELECT COUNT(*) FROM rollout_session_segments WHERE session_key=?) WHERE session_key=?""",
                    (session_key, session_key),
                )
                if payload.get("parent_thread_id") is not None:
                    parent = payload.get("parent_thread_id")
                    if isinstance(parent, str):
                        persist_confirmed_parent(
                            connection, child_key=session_key,
                            parent_key=opaque("identity", parent),
                            project_id=project_id,
                        )
                seen_session = True
                continue
            if kind == "turn_context" and isinstance(payload.get("turn_id"), str):
                current_turn = payload["turn_id"]
                continue
            if known_event is not None and not retry_unbound:
                continue
            if kind == "event_msg" and payload.get("type") == "token_count":
                usage = parse_usage(payload)
                if usage is None or session_key is None:
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "token_count", payload)
                    continue
                completeness = "complete" if usage["complete"] else "partial"
                connection.execute(
                    """INSERT INTO token_snapshots(source_digest, line_number, session_key, project_id, epoch, input_tokens,
                       cached_input_tokens, output_tokens, reasoning_tokens, cache_write_tokens, vendor_total, context_window, completeness, observed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                    (source, line_number, session_key, project_id, 0, usage["input"], usage["cached"], usage["output"],
                     usage["reasoning"], usage["cache_write"], usage["vendor_total"], usage["context_window"], completeness, observed_at),
                )
                connection.execute("UPDATE token_snapshots SET turn_key = ? WHERE source_digest = ? AND line_number = ?", (opaque("turn", current_turn) if current_turn else None, source, line_number))
                continue
            event_type = payload.get("type") if kind == "event_msg" else None
            if kind == "event_msg" and event_type == "sub_agent_activity":
                child_identity = payload.get("agent_thread_id")
                if session_key is None or not isinstance(child_identity, str):
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "sub_agent_activity", payload)
                    continue
                child_key = opaque("identity", child_identity)
                connection.execute(
                    """INSERT INTO rollout_sessions(session_key, project_id, path_key, resume_segments)
                       VALUES (?, ?, 'unresolved', 1) ON CONFLICT DO NOTHING""", (child_key, project_id)
                )
                assert_session_project(
                    connection, session_key=child_key, project_id=project_id,
                )
                persist_inferred_parent(
                    connection, child_key=child_key, parent_key=session_key,
                    project_id=project_id,
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
                    connection.execute(
                        """INSERT INTO rollout_sessions(
                               session_key,project_id,path_key,resume_segments,conversation_key)
                           VALUES (?,?,'unresolved',1,'') ON CONFLICT DO NOTHING""",
                        (session_key, project_id),
                    )
                connection.execute(
                    """INSERT INTO turn_lifecycle_events(
                           event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                           emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
                       VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (event_key, session_key, opaque("turn", turn),
                     {"task_started": "started", "task_complete": "completed", "turn_aborted": "aborted"}[event_type],
                     observed_at, timestamp.epoch, safe_int(payload.get("duration_ms")), source, logical, line_number),
                )
                continue
            if kind == "response_item" and payload.get("type") == "custom_tool_call":
                if session_key is not None:
                    diagnostics += persist_custom_tool_call(
                        connection, payload=payload, session_key=session_key,
                        source_digest=source, source_ordinal=line_number,
                        observed_at=observed_at, current_turn=current_turn,
                        project_root=project_root, test_evidence=test_evidence,
                        diagnose=lambda reason: _insert_diagnostic(
                            connection, source, line_number, reason, {}, unsafe_value=reason,
                        ),
                    )
                continue
            if kind == "response_item" and payload.get("type") == "custom_tool_call_output":
                call_id = payload.get("call_id")
                if session_key is not None and isinstance(call_id, str):
                    terminal, latency = custom_exec_outcome(payload.get("output"))
                    persist_tool_end(
                        connection, session_key=session_key, call_key=opaque("call", call_id), category="opaque_exec", tool_name="custom_exec",
                        finished_at=observed_at,
                        terminal_state=terminal, latency_ms=latency, turn_key=opaque("turn", current_turn) if current_turn else None,
                        source_digest=source, source_ordinal=line_number,
                    )
                continue
            if kind == "response_item" and payload.get("type") == "function_call":
                if session_key is None or not isinstance(payload.get("call_id"), str):
                    diagnostics += 1
                    _insert_diagnostic(connection, source, line_number, "function_call", payload)
                    continue
                normalized = normalize_function_call(
                    payload.get("name"), payload.get("arguments"), project_root=project_root,
                )
                call_key = opaque("call", payload["call_id"])
                persist_tool_start(
                    connection, session_key=session_key, call_key=call_key,
                    category=normalized.category, tool_name=normalized.safe_name,
                    started_at=observed_at,
                    turn_key=opaque("turn", current_turn) if current_turn else None,
                    source_digest=source, source_ordinal=line_number,
                    provenance=normalized.provenance,
                )
                accesses = tuple(
                    (access.operation, access.relative_path)
                    for access in normalized.file_accesses
                )
                if normalized.safe_name in {"exec_command", "apply_patch"}:
                    pending_file_calls[payload["call_id"]] = (
                        normalized.safe_name, session_key, call_key, accesses,
                    )
                    completed = (
                        successful_exec_calls.get(payload["call_id"])
                        if normalized.safe_name == "exec_command"
                        else successful_patch_calls.get(payload["call_id"])
                    )
                    if completed is not None:
                        flush_file_call(
                            payload["call_id"], normalized.safe_name, *completed,
                        )
                else:
                    for operation, relative_path in accesses:
                        _persist_file(
                            connection, source, line_number, session_key,
                            operation, relative_path, project_root,
                            observed_at, opaque("turn", current_turn) if current_turn else None,
                            observation_call_key=call_key,
                            observation_tool_name=normalized.safe_name,
                            requires_success=operation == "write",
                        )
                if normalized.ephemeral_command is not None:
                    test_evidence.intent(
                        logical_call_id=payload["call_id"], model_call_id=payload["call_id"],
                        command=normalized.ephemeral_command,
                        session_key=session_key, line_number=line_number, observed_at=observed_at,
                        turn_key=opaque("turn", current_turn) if current_turn else None, tool_call_key=call_key,
                    )
                continue
            if kind == "response_item" and payload.get("type") == "function_call_output":
                call_id = payload.get("call_id")
                result = parse_structured_result(payload.get("output"))
                if isinstance(call_id, str) and result.exit_status == 0:
                    terminal = (line_number, observed_at, current_turn)
                    successful_exec_calls[call_id] = terminal
                    flush_file_call(call_id, "exec_command", *terminal)
                if session_key is not None and isinstance(call_id, str):
                    terminal_state = (
                        "success" if result.exit_status == 0
                        else "failed" if result.exit_status is not None
                        else "unknown"
                    )
                    persist_tool_end(
                        connection, session_key=session_key, call_key=opaque("call", call_id), category="tool", tool_name="function",
                        finished_at=observed_at,
                        terminal_state=terminal_state, latency_ms=None, turn_key=opaque("turn", current_turn) if current_turn else None,
                        source_digest=source, source_ordinal=line_number,
                    )
                if isinstance(call_id, str):
                    test_evidence.result(
                        call_id, payload.get("output"), observed_at,
                        session_key=session_key, line_number=line_number,
                        turn_key=opaque("turn", current_turn) if current_turn else None,
                        tool_call_key=opaque("call", call_id),
                    )
                continue
            if kind == "event_msg" and event_type in {"mcp_tool_call_end", "patch_apply_end", "web_search_end"}:
                call_id = payload.get("call_id")
                terminal_state = _tool_end_state(payload)
                if session_key is not None and isinstance(call_id, str):
                    kind_name = {"mcp_tool_call_end": "mcp", "patch_apply_end": "patch", "web_search_end": "web"}[event_type]
                    persist_tool_end(
                        connection, session_key=session_key, call_key=opaque("call", call_id),
                        category="web" if kind_name == "web" else "tool", tool_name=kind_name,
                        finished_at=observed_at,
                        terminal_state=terminal_state, latency_ms=_duration_ms(payload.get("duration")),
                        turn_key=opaque("turn", current_turn) if current_turn else None, source_digest=source, source_ordinal=line_number,
                    )
                if (
                    event_type == "patch_apply_end" and session_key is not None
                    and isinstance(call_id, str) and terminal_state == "success"
                ):
                    terminal = (line_number, observed_at, current_turn)
                    successful_patch_calls[call_id] = terminal
                    flush_file_call(call_id, "apply_patch", *terminal)
                    changes = payload.get("changes")
                    if isinstance(changes, dict):
                        for changed_path in changes:
                            _persist_file(
                                connection, source, line_number, session_key, "write", changed_path, project_root,
                                observed_at, opaque("turn", current_turn) if current_turn else None,
                                observation_call_key=opaque("call", call_id),
                                observation_tool_name="apply_patch",
                                requires_success=True,
                            )
                continue
            if kind == "event_msg" and event_type not in KNOWN_EVENT_TYPES:
                diagnostics += 1
                _insert_diagnostic(connection, source, line_number, "unknown_event_type", payload, unsafe_value=event_type)
                continue
            if kind == "response_item" and payload.get("type") not in KNOWN_RESPONSE_TYPES:
                diagnostics += 1
                _insert_diagnostic(
                    connection, source, line_number, "unknown_response_type", payload,
                    unsafe_value=payload.get("type"),
                )
        test_evidence.flush()
        if parsed_lines != len(line_fingerprints):
            raise RuntimeError("rollout source changed during ingest")
        reconcile_turn_attempts(
            connection,
            lambda digest, ordinal, kind: _insert_diagnostic(connection, digest, ordinal, kind, {}),
        )
    return diagnostics


def ingest_rollouts(
    store: HydraStore, roots: Iterable[Path | str | RolloutRoot], project_root: Path | str, project_id: str,
    model_causes: dict[str, str] | None = None, hash_key: bytes | None = None,
) -> IngestReport:
    """Ingest explicit v1 JSONL roots idempotently, storing only normalized safe facts."""
    root = Path(project_root)
    if hash_key is not None and len(hash_key) != 32:
        raise ValueError("hash_key must be exactly 32 bytes")
    hasher = Pseudonymizer(hash_key) if hash_key is not None else Pseudonymizer.installation(store.database_path.parent)
    hash_token = ACTIVE_HASHER.set(hasher)
    try:
        root_specs = tuple(roots)
        files = discover_rollouts(root_specs)
        diagnostics = 0
        unique: set[str] = set()
        for path in files:
            location = opaque("source", str(path))
            label = next(
                (item.label for item in root_specs if isinstance(item, RolloutRoot)
                 and path.is_relative_to(Path(item.path).resolve())),
                "explicit",
            )
            before_stat = _source_stat(path)
            with store.rollout_transaction() as connection:
                unchanged = _unchanged_location(
                    connection, project_id, location, before_stat,
                )
                if unchanged is not None:
                    after_candidate_stat = _source_stat(path)
                    if after_candidate_stat == before_stat:
                        logical, digest = unchanged
                        connection.execute(
                            """UPDATE rollout_source_locations SET location_type=?
                                WHERE logical_source_key=? AND location_key=?""",
                            (label, logical, location),
                        )
                        unique.add(digest)
                        continue
                    before_stat = after_candidate_stat
            scan = scan_source(path, hasher.key, opaque)
            digest = opaque("source", f"revision/{project_id}/{scan.revision_digest}")
            unique.add(digest)
            with store.rollout_transaction() as connection:
                known = connection.execute(
                    "SELECT logical_source_key,materialized FROM rollout_sources WHERE source_digest=?", (digest,),
                ).fetchone()
                if known is not None:
                    connection.execute(
                        """INSERT INTO rollout_source_locations(
                               logical_source_key,location_key,location_type,revision_digest)
                           VALUES (?,?,?,?) ON CONFLICT(logical_source_key,location_key) DO UPDATE SET
                             location_type=excluded.location_type,revision_digest=excluded.revision_digest""",
                        (known[0], location, label, digest),
                    )
                    if known[1]:
                        after_stat = _source_stat(path)
                        if after_stat != before_stat:
                            raise RuntimeError("rollout source changed during ingest")
                        _persist_location_state(
                            connection, project_id, location, str(known[0]),
                            digest, after_stat,
                        )
                        continue
                identity_key = opaque("identity", scan.identity) if scan.identity else opaque("identity", "unresolved/" + digest)
                located = located_lineage(connection, location, project_id)
                relocated = None if known is not None or located is not None else prefix_lineage(
                    connection, identity_key, project_id, scan.line_fingerprints,
                )
                logical = known[0] if known is not None else (
                    located if located is not None else relocated or opaque(
                        "source", f"segment/{project_id}/{identity_key}/{scan.segment_marker}",
                    )
                )
                logical_row = connection.execute(
                    "SELECT canonical_revision_digest,session_key "
                    "FROM rollout_logical_sources WHERE logical_source_key=?",
                    (logical,),
                ).fetchone()
                canonical = logical_row[0] if logical_row is not None else None
                bound_before = logical_row[1] if logical_row is not None else None
                relation = "initial" if canonical is None else relation_to(revision_lines(connection, canonical), scan.line_fingerprints)
                connection.execute(
                    """INSERT INTO rollout_logical_sources(
                           logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
                       VALUES (?,?,NULL,NULL,'clean') ON CONFLICT DO NOTHING""",
                    (logical, project_id),
                )
                connection.execute(
                    """INSERT INTO rollout_sources(
                           source_digest,source_type,logical_source_key,relation,line_count,byte_count,chain_digest,materialized)
                       VALUES (?,'jsonl',?,?,?,?,?,0) ON CONFLICT DO NOTHING""",
                    (digest, logical, relation, scan.line_count, scan.byte_count, scan.chain_digest),
                )
                connection.executemany(
                    """INSERT INTO rollout_revision_lines(revision_digest,line_number,line_fingerprint)
                       VALUES (?,?,?) ON CONFLICT DO NOTHING""",
                    ((digest, index, fingerprint) for index, fingerprint in enumerate(scan.line_fingerprints, start=1)),
                )
                connection.execute(
                    """INSERT INTO rollout_source_locations(
                           logical_source_key,location_key,location_type,revision_digest)
                       VALUES (?,?,?,?) ON CONFLICT(logical_source_key,location_key) DO UPDATE SET
                         location_type=excluded.location_type,revision_digest=excluded.revision_digest""",
                    (logical, location, label, digest),
                )
                if relation == "truncate":
                    diagnostics += 1
                    _insert_diagnostic(connection, digest, 0, "source_truncate", {})
                elif relation == "rewrite":
                    diagnostics += 1
                    connection.execute(
                        "UPDATE rollout_logical_sources SET lineage_state='conflicted' WHERE logical_source_key=?",
                        (logical,),
                    )
                    _insert_diagnostic(connection, digest, 0, "source_rewrite", {})
                if relation not in {"truncate", "rewrite"} or bound_before is None:
                    diagnostics += _parse_source(
                        store, path, digest, root, project_id, model_causes or {},
                        logical_source=logical, line_fingerprints=scan.line_fingerprints,
                        authoritative_identity=scan.identity,
                    )
                    bound = connection.execute(
                        "SELECT session_key FROM rollout_logical_sources "
                        "WHERE logical_source_key=?",
                        (logical,),
                    ).fetchone()
                    if bound is not None and bound[0] is not None:
                        connection.execute(
                            "UPDATE rollout_logical_sources SET "
                            "canonical_revision_digest=?,lineage_state='clean' "
                            "WHERE logical_source_key=?",
                            (digest, logical),
                        )
                bound = connection.execute(
                    "SELECT session_key FROM rollout_logical_sources "
                    "WHERE logical_source_key=?",
                    (logical,),
                ).fetchone()
                if bound is not None and bound[0] is not None:
                    connection.execute(
                        "UPDATE rollout_sources SET materialized=1 "
                        "WHERE source_digest=?", (digest,),
                    )
                from .token_selection import refresh_token_source_selection

                refresh_token_source_selection(connection, project_id)
                reconcile_token_epochs(
                    connection, project_id,
                    lambda source_digest, ordinal, kind: _insert_diagnostic(
                        connection, source_digest, ordinal, kind, {},
                    ),
                )
                reconcile_fork_baselines(connection, project_id)
                materialized = connection.execute(
                    "SELECT materialized FROM rollout_sources WHERE source_digest=?",
                    (digest,),
                ).fetchone()
                if materialized is not None and materialized[0]:
                    after_stat = _source_stat(path)
                    if after_stat != before_stat:
                        raise RuntimeError("rollout source changed during ingest")
                    _persist_location_state(
                        connection, project_id, location, logical, digest, after_stat,
                    )
        with store.rollout_transaction() as connection:
            from .token_selection import refresh_token_source_selection

            refresh_token_source_selection(connection, project_id)
            reconcile_token_epochs(
                connection, project_id,
                lambda source_digest, ordinal, kind: _insert_diagnostic(
                    connection, source_digest, ordinal, kind, {},
                ),
            )
            reconcile_fork_baselines(connection, project_id)
        return IngestReport(len(files), len(unique), diagnostics)
    finally:
        ACTIVE_HASHER.reset(hash_token)
