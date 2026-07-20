"""Read-only adapter for the `hydra.codex-rollout/v1` JSONL boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .project import ProjectNotFound, resolve_project
from .rollout_identity import ACTIVE_HASHER, IngestReport, Pseudonymizer, RolloutRoot, discover_rollouts, opaque
from .rollout_observations import fingerprint as observation_fingerprint
from .rollout_observations import parse_arguments, path_key, safe_int, usage as parse_usage
from .rollout_privacy import (
    KNOWN_ENVELOPES, KNOWN_EVENT_TYPES, KNOWN_RESPONSE_TYPES,
    canonical_timestamp, nonempty_string, safe_envelope_kind,
)
from .rollout_persistence import duration_ms as _duration_ms
from .rollout_persistence import insert_diagnostic as _persist_diagnostic
from .rollout_persistence import persist_file as _persist_file
from .rollout_persistence import tool_end_state as _tool_end_state
from .rollout_reconcile import reconcile_token_epochs, reconcile_turn_attempts
from .rollout_sources import line_fingerprint, located_lineage, prefix_lineage, relation_to, revision_lines, scan_source
from .storage import HydraStore
from .test_evidence import TestEvidenceBuffer
from .tool_normalization import custom_exec_outcome, nested_span_name, scan_custom_exec_details
from .tool_spans import persist_tool_end, persist_tool_start


def _insert_diagnostic(connection: Any, source: str, line: int, kind: str, payload: Any, *, unsafe_value: Any = None) -> None:
    _persist_diagnostic(
        connection, source, line, kind, payload,
        fingerprint=lambda value: observation_fingerprint(value, opaque), unsafe_value=unsafe_value,
    )


def _parse_source(
    store: HydraStore, path: Path, source: str, project_root: Path, project_id: str,
    model_causes: dict[str, str], *, logical_source: str | None = None,
    line_fingerprints: tuple[str, ...] = (), authoritative_identity: str | None = None,
) -> int:
    diagnostics = 0
    session_key: str | None = None
    session_meta_at: str | None = None
    session_meta_epoch: float | None = None
    current_turn: str | None = None
    seen_session = False
    with store.rollout_transaction() as connection, path.open("rb") as handle:
        test_evidence = TestEvidenceBuffer(connection, source, model_causes, opaque)
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
                    continue
                session_key = next_key
                payload_time = canonical_timestamp(payload.get("timestamp"))
                if not seen_session:
                    session_meta_at = payload_time.text or observed_at
                    session_meta_epoch = payload_time.epoch if payload_time.epoch is not None else timestamp.epoch
                session_path = path_key(payload.get("cwd"), project_root, opaque)
                connection.execute(
                    """INSERT INTO rollout_sessions(
                           session_key,project_id,path_key,resume_segments,conversation_key,started_at,last_activity_at)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET
                         started_at=CASE WHEN rollout_sessions.started_at IS NULL OR excluded.started_at < rollout_sessions.started_at
                                         THEN excluded.started_at ELSE rollout_sessions.started_at END,
                         last_activity_at=CASE WHEN rollout_sessions.last_activity_at IS NULL OR excluded.last_activity_at > rollout_sessions.last_activity_at
                                              THEN excluded.last_activity_at ELSE rollout_sessions.last_activity_at END""",
                    (session_key, project_id, session_path, 1, opaque("conversation", conversation) if conversation else next_key,
                     session_meta_at, observed_at),
                )
                if not seen_session:
                    persisted_start = connection.execute(
                        "SELECT started_at FROM rollout_sessions WHERE session_key=?", (session_key,),
                    ).fetchone()[0]
                    session_meta_at = persisted_start
                    session_meta_epoch = canonical_timestamp(persisted_start).epoch
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
                        connection.execute(
                            """INSERT INTO session_edges(child_key, parent_key, baseline_working_tokens, confidence_kind, confidence)
                               VALUES (?, ?, ?, ?, ?) ON CONFLICT(child_key) DO UPDATE SET
                                 parent_key = excluded.parent_key, baseline_working_tokens = NULL,
                                 confidence_kind = 'confirmed', confidence = 1.0
                               WHERE session_edges.confidence_kind != 'confirmed'""",
                            (session_key, opaque("identity", parent), None, "confirmed", 1.0),
                        )
                seen_session = True
                continue
            if kind == "turn_context" and isinstance(payload.get("turn_id"), str):
                current_turn = payload["turn_id"]
                continue
            if known_event is not None:
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
                edge = connection.execute(
                    "SELECT parent_key FROM session_edges WHERE child_key = ?", (session_key,)
                ).fetchone()
                timely = (
                    session_meta_epoch is not None and timestamp.epoch is not None
                    and 0 <= timestamp.epoch - session_meta_epoch <= 1
                )
                if usage["complete"] and timely and edge is not None and connection.execute(
                    "SELECT confidence_kind FROM session_edges WHERE child_key = ?", (session_key,)
                ).fetchone()[0] == "confirmed":
                    connection.execute(
                        """INSERT INTO fork_baselines(child_key, source_digest, line_number, input_tokens, cached_input_tokens,
                           output_tokens, reasoning_tokens, cache_write_tokens, provenance, observed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'exact', ?) ON CONFLICT(child_key) DO UPDATE SET
                             source_digest=excluded.source_digest,line_number=excluded.line_number,input_tokens=excluded.input_tokens,
                             cached_input_tokens=excluded.cached_input_tokens,output_tokens=excluded.output_tokens,
                             reasoning_tokens=excluded.reasoning_tokens,cache_write_tokens=excluded.cache_write_tokens,observed_at=excluded.observed_at
                           WHERE excluded.observed_at > fork_baselines.observed_at""",
                        (session_key, source, line_number, usage["input"], usage["cached"], usage["output"], usage["reasoning"], usage["cache_write"], observed_at),
                    )
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
                if session_key is not None and isinstance(payload.get("call_id"), str):
                    call_key = opaque("call", payload["call_id"])
                    persist_tool_start(
                        connection, session_key=session_key, call_key=call_key, category="opaque_exec", tool_name="custom_exec",
                        started_at=observed_at,
                        turn_key=opaque("turn", current_turn) if current_turn else None, source_digest=source, source_ordinal=line_number,
                    )
                    if payload.get("name") == "exec" and isinstance(payload.get("input"), str):
                        scan = scan_custom_exec_details(payload["input"])
                        for reason in scan.diagnostics:
                            diagnostics += 1
                            _insert_diagnostic(connection, source, line_number, "custom_exec_" + reason, {"reason": reason})
                        for index, nested in enumerate(scan.calls):
                            for nested_path in nested.paths:
                                _persist_file(
                                    connection, source, line_number, session_key, "write", nested_path, project_root,
                                    observed_at, opaque("turn", current_turn) if current_turn else None,
                                )
                            if nested.command is not None:
                                test_evidence.intent(
                                    logical_call_id=f"{payload['call_id']}:{index}", model_call_id=payload["call_id"],
                                    command=nested.command, session_key=session_key, line_number=line_number,
                                    observed_at=observed_at,
                                    turn_key=opaque("turn", current_turn) if current_turn else None,
                                    tool_call_key=call_key,
                                )
                            tool_name = nested_span_name(nested)
                            if tool_name is None:
                                continue
                            nested_key = opaque("call", f"{payload['call_id']}:{index}:{nested.name}")
                            persist_tool_start(
                                connection, session_key=session_key, call_key=nested_key, category="tool", tool_name=tool_name,
                                started_at=observed_at,
                                turn_key=opaque("turn", current_turn) if current_turn else None, source_digest=source, source_ordinal=line_number,
                                provenance="lower_bound",
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
                name = payload.get("name") if isinstance(payload.get("name"), str) else "unknown"
                category = "instrumentation" if "hydra" in name.lower() else "tool"
                call_key = opaque("call", payload["call_id"])
                persist_tool_start(
                    connection, session_key=session_key, call_key=call_key, category=category, tool_name="function",
                    started_at=observed_at,
                    turn_key=opaque("turn", current_turn) if current_turn else None, source_digest=source, source_ordinal=line_number,
                )
                arguments = parse_arguments(payload.get("arguments"))
                if "path" in arguments:
                    operation = "read" if "read" in name.lower() else "write"
                    _persist_file(
                        connection, source, line_number, session_key, operation, arguments["path"], project_root,
                        observed_at, opaque("turn", current_turn) if current_turn else None,
                    )
                command = arguments.get("cmd")
                if name == "exec_command" and isinstance(command, str):
                    test_evidence.intent(
                        logical_call_id=payload["call_id"], model_call_id=payload["call_id"], command=command,
                        session_key=session_key, line_number=line_number, observed_at=observed_at,
                        turn_key=opaque("turn", current_turn) if current_turn else None, tool_call_key=call_key,
                    )
                continue
            if kind == "response_item" and payload.get("type") == "function_call_output":
                call_id = payload.get("call_id")
                if session_key is not None and isinstance(call_id, str):
                    persist_tool_end(
                        connection, session_key=session_key, call_key=opaque("call", call_id), category="tool", tool_name="function",
                        finished_at=observed_at,
                        terminal_state="unknown", latency_ms=None, turn_key=opaque("turn", current_turn) if current_turn else None,
                        source_digest=source, source_ordinal=line_number,
                    )
                if isinstance(call_id, str):
                    test_evidence.result(call_id, payload.get("output"), observed_at)
                continue
            if kind == "event_msg" and event_type in {"mcp_tool_call_end", "patch_apply_end", "web_search_end"}:
                call_id = payload.get("call_id")
                if session_key is not None and isinstance(call_id, str):
                    kind_name = {"mcp_tool_call_end": "mcp", "patch_apply_end": "patch", "web_search_end": "web"}[event_type]
                    persist_tool_end(
                        connection, session_key=session_key, call_key=opaque("call", call_id),
                        category="web" if kind_name == "web" else "tool", tool_name=kind_name,
                        finished_at=observed_at,
                        terminal_state=_tool_end_state(payload), latency_ms=_duration_ms(payload.get("duration")),
                        turn_key=opaque("turn", current_turn) if current_turn else None, source_digest=source, source_ordinal=line_number,
                    )
                if event_type == "patch_apply_end" and session_key is not None:
                    changes = payload.get("changes")
                    if isinstance(changes, dict):
                        for changed_path in changes:
                            _persist_file(
                                connection, source, line_number, session_key, "write", changed_path, project_root,
                                observed_at, opaque("turn", current_turn) if current_turn else None,
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
        reconcile_token_epochs(
            connection, project_id,
            lambda digest, ordinal, kind: _insert_diagnostic(connection, digest, ordinal, kind, {}),
        )
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
            scan = scan_source(path, hasher.key, opaque)
            digest = opaque("source", f"revision/{project_id}/{scan.revision_digest}")
            unique.add(digest)
            location = opaque("source", str(path))
            label = next(
                (item.label for item in root_specs if isinstance(item, RolloutRoot)
                 and path.is_relative_to(Path(item.path).resolve())),
                "explicit",
            )
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
                    "SELECT canonical_revision_digest FROM rollout_logical_sources WHERE logical_source_key=?", (logical,),
                ).fetchone()
                canonical = logical_row[0] if logical_row is not None else None
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
                else:
                    diagnostics += _parse_source(
                        store, path, digest, root, project_id, model_causes or {},
                        logical_source=logical, line_fingerprints=scan.line_fingerprints,
                        authoritative_identity=scan.identity,
                    )
                    connection.execute(
                        "UPDATE rollout_logical_sources SET canonical_revision_digest=? WHERE logical_source_key=?",
                        (digest, logical),
                    )
                connection.execute("UPDATE rollout_sources SET materialized=1 WHERE source_digest=?", (digest,))
        return IngestReport(len(files), len(unique), diagnostics)
    finally:
        ACTIVE_HASHER.reset(hash_token)
