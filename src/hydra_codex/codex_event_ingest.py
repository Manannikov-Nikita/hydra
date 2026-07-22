"""Idempotent persistence for versioned Codex App Server and OTLP facts."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Iterable

from .codex_events import (
    APP_SERVER_V2,
    OTEL_LOG_V1,
    CodexEventBatch,
    CodexEventFact,
    EventAdapterError,
    read_codex_event_jsonl,
)
from .lineage import assert_session_project, persist_confirmed_parent
from .prepared_codex_events import (
    PreparedCodexEventSource,
    validate_prepared_codex_event_sources_key,
)
from .rollout_identity import ACTIVE_HASHER, Pseudonymizer
from .rollout_persistence import persist_file
from .rollout_privacy import canonical_timestamp
from .rollout_sources import SOURCE_CHANGED_MESSAGE, SourceChanged, source_stat
from .rollout_reconcile import (
    reconcile_fork_baselines,
    reconcile_token_epochs,
    reconcile_turn_attempts,
)
from .shell_facts import shell_file_facts
from .storage import HydraStore
from .test_evidence import TestEvidenceBuffer
from .token_selection import refresh_token_source_selection
from .tool_spans import persist_tool_end, persist_tool_start


@dataclass(frozen=True)
class CodexEventSource:
    path: Path | str
    schema: str

    def __post_init__(self) -> None:
        if self.schema not in {APP_SERVER_V2, OTEL_LOG_V1}:
            raise ValueError("unsupported event source schema")


@dataclass(frozen=True)
class CodexEventIngestReport:
    files_seen: int
    unique_sources: int
    events: int
    issues: int


@dataclass(frozen=True)
class _PreparedSource:
    path: Path
    source_digest: str
    location_key: str
    raw_digest: str
    line_count: int
    byte_count: int
    schema: str


def _stream_fingerprint(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    byte_count = 0
    newline_count = 0
    last_byte = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
            newline_count += chunk.count(b"\n")
            last_byte = chunk[-1:]
    line_count = newline_count + int(byte_count > 0 and last_byte != b"\n")
    return digest.hexdigest(), line_count, byte_count


def _prepare(
    source: CodexEventSource, hasher: Pseudonymizer, project_id: str,
) -> _PreparedSource:
    path = Path(source.path).expanduser()
    if not path.is_file():
        raise EventAdapterError("event source must be a regular file")
    raw_digest, line_count, byte_count = _stream_fingerprint(path)
    digest = hasher.digest(
        "source", f"codex-event/{project_id}/{source.schema}/{raw_digest}",
    )
    location = hasher.digest("path", str(path.resolve()))
    return _PreparedSource(
        path, digest, location, raw_digest, line_count, byte_count, source.schema,
    )


def _format(schema: str) -> str:
    return "app_server" if schema == APP_SERVER_V2 else "otel"


def _persist_epoch_diagnostic(
    connection: sqlite3.Connection, source: str, line: int, kind: str,
) -> None:
    connection.execute(
        """INSERT INTO rollout_diagnostics(
               source_digest,line_number,envelope_kind,fingerprint)
           VALUES (?,?,?,'codex-event-token-epoch-v1') ON CONFLICT DO NOTHING""",
        (source, line, kind),
    )


def _iso_min(first: object, second: str | None) -> str | None:
    values = [
        parsed for value in (first, second)
        if (parsed := canonical_timestamp(value)).epoch is not None
    ]
    return min(values, key=lambda item: item.epoch).text if values else None


def _iso_max(first: object, second: str | None) -> str | None:
    values = [
        parsed for value in (first, second)
        if (parsed := canonical_timestamp(value)).epoch is not None
    ]
    return max(values, key=lambda item: item.epoch).text if values else None


def _persist_source(
    connection: sqlite3.Connection, prepared: _PreparedSource, project_id: str,
) -> None:
    connection.execute(
        """INSERT INTO codex_event_sources(
               source_digest,project_id,schema_version,source_format,line_count,byte_count)
           VALUES (?,?,?,?,?,?) ON CONFLICT(source_digest) DO NOTHING""",
        (
            prepared.source_digest, project_id, prepared.schema, _format(prepared.schema),
            prepared.line_count, prepared.byte_count,
        ),
    )
    row = connection.execute(
        "SELECT project_id,schema_version FROM codex_event_sources WHERE source_digest=?",
        (prepared.source_digest,),
    ).fetchone()
    if row is None or tuple(row) != (project_id, prepared.schema):
        raise ValueError("event source identity belongs to another project or schema")
    connection.execute(
        """INSERT INTO codex_event_source_locations(source_digest,location_key)
           VALUES (?,?) ON CONFLICT DO NOTHING""",
        (prepared.source_digest, prepared.location_key),
    )


def _normalized_source(
    connection: sqlite3.Connection, prepared: _PreparedSource, project_id: str,
    session_key: str, hasher: Pseudonymizer,
) -> tuple[str, str]:
    source = hasher.digest(
        "source", f"normalized-event-source/{prepared.source_digest}/{session_key}",
    )
    logical = hasher.digest(
        "source", f"normalized-event-logical/{prepared.source_digest}/{session_key}",
    )
    connection.execute(
        """INSERT INTO rollout_logical_sources(
               logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
           VALUES (?,?,?,?, 'clean') ON CONFLICT DO NOTHING""",
        (logical, project_id, session_key, source),
    )
    connection.execute(
        """INSERT INTO rollout_sources(
               source_digest,source_type,logical_source_key,relation,line_count,
               byte_count,chain_digest,materialized)
           VALUES (?,'explicit',?,'event_adapter',?,?,?,1) ON CONFLICT DO NOTHING""",
        (
            source, logical, prepared.line_count, prepared.byte_count,
            prepared.source_digest,
        ),
    )
    connection.execute(
        """INSERT INTO rollout_session_segments(session_key,logical_source_key)
           VALUES (?,?) ON CONFLICT DO NOTHING""",
        (session_key, logical),
    )
    return source, logical


def _upsert_session(
    connection: sqlite3.Connection, project_id: str, project_root: Path,
    session_key: str, events: tuple[CodexEventFact, ...], hasher: Pseudonymizer,
) -> None:
    observed = [event.observed_at for event in events if event.observed_at is not None]
    explicit_starts = {
        "thread_started", "turn_started", "conversation_started", "user_prompt",
    }
    starts = []
    for event in events:
        if event.event_type not in explicit_starts:
            continue
        app_lifecycle_start = (
            event.source_format == "app_server"
            and event.event_type in {"thread_started", "turn_started"}
        )
        value = event.lifecycle_at if app_lifecycle_start else event.observed_at
        if value is not None:
            starts.append(value)
    started = None
    for value in starts:
        started = _iso_min(started, value)
    latest = None
    for value in observed:
        latest = _iso_max(latest, value)
    path_key = hasher.digest("path", str(project_root.resolve()))
    connection.execute(
        """INSERT INTO rollout_sessions(
               session_key,project_id,path_key,resume_segments,conversation_key,
               started_at,last_activity_at)
           VALUES (?,?,?,1,?,?,?) ON CONFLICT(session_key) DO NOTHING""",
        (session_key, project_id, path_key, session_key, started, latest),
    )
    row = connection.execute(
        "SELECT project_id,started_at,last_activity_at FROM rollout_sessions WHERE session_key=?",
        (session_key,),
    ).fetchone()
    if row is None:
        raise ValueError("canonical session identity belongs to another project")
    assert_session_project(
        connection, session_key=session_key, project_id=project_id,
    )
    connection.execute(
        "UPDATE rollout_sessions SET started_at=?,last_activity_at=? WHERE session_key=?",
        (_iso_min(row[1], started), _iso_max(row[2], latest), session_key),
    )


def _persist_event(
    connection: sqlite3.Connection, prepared: _PreparedSource,
    project_id: str, event: CodexEventFact,
) -> None:
    tool = event.tool
    connection.execute(
        """INSERT INTO codex_events(
               source_digest,source_ordinal,event_key,project_id,source_format,
               schema_version,event_type,observed_at,observed_at_ns,session_key,
               turn_key,duration_ms,status,provenance,tool_call_key,tool_name,
               tool_category,tool_phase,tool_status,tool_duration_ms,tool_exit_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
        (
            prepared.source_digest, event.source_ordinal, event.event_key, project_id,
            event.source_format, event.schema_version, event.event_type,
            event.observed_at, event.observed_at_ns, event.thread_key, event.turn_key,
            event.duration_ms, event.status, event.provenance,
            None if tool is None else tool.call_key,
            None if tool is None else tool.safe_name,
            None if tool is None else tool.category,
            None if tool is None else tool.phase,
            None if tool is None else tool.status,
            None if tool is None else tool.duration_ms,
            None if tool is None else tool.exit_status,
        ),
    )
    connection.executemany(
        """INSERT INTO codex_event_contents(
               source_digest,source_ordinal,field,content_digest,characters,utf8_bytes)
           VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
        (
            (
                prepared.source_digest, event.source_ordinal, content.field,
                content.digest, content.characters, content.utf8_bytes,
            )
            for content in event.contents
        ),
    )


def _persist_normalized_event(
    connection: sqlite3.Connection, prepared: _PreparedSource, project_id: str,
    event: CodexEventFact, normalized_source: str, logical: str,
    hasher: Pseudonymizer, project_root: Path,
) -> None:
    session = event.thread_key
    if session is None:
        return
    normalized_event = hasher.digest(
        "event", f"codex-normalized/{event.event_key}/{session}",
    )
    connection.execute(
        """INSERT INTO rollout_events(
               event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
               timestamp_quality,fingerprint)
           VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
        (
            normalized_event, logical, event.source_ordinal, event.source_format,
            event.observed_at, "valid" if event.observed_at else "missing", event.event_key,
        ),
    )
    connection.execute(
        """INSERT INTO rollout_revision_events(revision_digest,event_key,source_ordinal)
           VALUES (?,?,?) ON CONFLICT DO NOTHING""",
        (normalized_source, normalized_event, event.source_ordinal),
    )
    lifecycle = {
        "turn_started": "started",
        "conversation_started": "started", "user_prompt": "started",
    }.get(event.event_type)
    if event.event_type == "turn_completed":
        lifecycle = (
            "aborted" if event.status in {"interrupted", "cancelled"}
            else "completed"
        )
    app_lifecycle = (
        event.source_format == "app_server"
        and event.event_type in {"thread_started", "turn_started", "turn_completed"}
    )
    lifecycle_at = (
        event.lifecycle_at
        if app_lifecycle else event.lifecycle_at or event.observed_at
    )
    lifecycle_at_ns = (
        event.lifecycle_at_ns
        if app_lifecycle or event.lifecycle_at is not None
        else event.observed_at_ns
    )
    if lifecycle is not None and (lifecycle_at is not None or app_lifecycle):
        connection.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                   emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (
                normalized_event, session, event.turn_key or normalized_event, lifecycle,
                lifecycle_at,
                None if lifecycle_at_ns is None else lifecycle_at_ns / 1_000_000_000,
                event.duration_ms, normalized_source, logical, event.source_ordinal,
            ),
        )
    _persist_tokens(
        connection, prepared, project_id, event, normalized_source, logical, hasher,
    )
    _persist_tool(connection, event, normalized_source, session)
    tool = event.tool
    # App Server repeats the full item on started/completed notifications.
    # Persist file evidence only from the authoritative terminal item so one
    # logical tool call cannot inflate read/write amplification.
    terminal_tool = tool is not None and tool.phase == "completed"
    terminal_status = tool.status in {"completed", "success"} if tool is not None else False
    successful_command = (
        terminal_tool and terminal_status and tool.safe_name == "exec_command"
        and tool.exit_status == 0
    )
    successful_file_change = (
        terminal_tool and terminal_status and tool.safe_name == "apply_patch"
    )
    if successful_command and tool.ephemeral_command is not None:
        for operation, path in shell_file_facts(
            tool.ephemeral_command,
            project_root=project_root,
            workdir=tool.ephemeral_workdir,
        ):
            persist_file(
                connection, normalized_source, event.source_ordinal, session,
                operation, path, project_root, event.observed_at, event.turn_key,
                observation_call_key=tool.call_key,
                observation_tool_name=tool.safe_name,
                requires_success=True,
            )
    if successful_file_change:
        for path in tool.ephemeral_file_writes:
            persist_file(
                connection, normalized_source, event.source_ordinal, session,
                "write", path, project_root, event.observed_at, event.turn_key,
                observation_call_key=tool.call_key,
                observation_tool_name=tool.safe_name,
                requires_success=True,
            )


def _persist_tokens(
    connection: sqlite3.Connection, prepared: _PreparedSource, project_id: str,
    event: CodexEventFact, normalized_source: str, logical: str,
    hasher: Pseudonymizer,
) -> None:
    family = _format(prepared.schema)
    for fact in event.token_snapshots:
        token_key = hasher.digest(
            "event", f"codex-token/{project_id}/{event.event_key}/{fact.counter_scope}",
        )
        connection.execute(
            """INSERT INTO codex_event_tokens(
                   token_key,source_digest,source_ordinal,session_key,turn_key,observed_at,
                   counter_scope,cumulative,input_tokens,cached_input_tokens,output_tokens,
                   reasoning_tokens,reported_total_tokens,source_family,provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (
                token_key, prepared.source_digest, event.source_ordinal, event.thread_key,
                event.turn_key, event.observed_at, fact.counter_scope, int(fact.cumulative),
                fact.input_tokens, fact.cached_input_tokens, fact.output_tokens,
                fact.reasoning_tokens, fact.reported_total_tokens, family, event.provenance,
            ),
        )
        contributes = (
            family == "app_server" and fact.counter_scope == "thread_total"
        ) or (family == "otel" and fact.counter_scope == "model_call")
        if not contributes or event.thread_key is None:
            continue
        epoch = 0 if family == "app_server" else int(token_key[:15], 16) + 1
        connection.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                   completeness,turn_key,observed_at,source_family,counter_scope,event_key,
                   contributes_total,selection_provenance)
               VALUES (?,?,?,?,?,?,?,?,?,0,'complete',?,?,?,?,?,1,'exact')
               ON CONFLICT DO NOTHING""",
            (
                normalized_source, event.source_ordinal, event.thread_key, project_id,
                epoch, fact.input_tokens, fact.cached_input_tokens, fact.output_tokens,
                fact.reasoning_tokens, event.turn_key, event.observed_at, family,
                fact.counter_scope, token_key,
            ),
        )


def _persist_tool(
    connection: sqlite3.Connection, event: CodexEventFact,
    normalized_source: str, session: str,
) -> None:
    tool = event.tool
    if tool is None:
        return
    # Item events without a stable item id are rejected by the adapter.  Keep
    # this boundary fail-closed so a future schema cannot silently turn each
    # notification into a different synthetic call.
    if tool.call_key is None:
        return
    call_key = tool.call_key
    complete = tool.phase == "completed"
    if not complete:
        persist_tool_start(
            connection, session_key=session, call_key=call_key,
            category=tool.category, tool_name=tool.safe_name,
            started_at=event.observed_at, turn_key=event.turn_key,
            source_digest=normalized_source, source_ordinal=event.source_ordinal,
            provenance=event.provenance,
        )
        return
    failed_statuses = {"failed", "interrupted", "cancelled", "declined"}
    if tool.safe_name == "exec_command":
        terminal = (
            "success" if tool.exit_status == 0
            else "failed" if tool.exit_status is not None or tool.status in failed_statuses
            else "unknown"
        )
    else:
        terminal = (
            "success" if tool.status in {"completed", "success"}
            else "failed" if tool.status in failed_statuses
            else "unknown"
        )
    persist_tool_end(
        connection, session_key=session, call_key=call_key,
        category=tool.category, tool_name=tool.safe_name,
        finished_at=event.observed_at, terminal_state=terminal,
        latency_ms=tool.duration_ms, turn_key=event.turn_key,
        source_digest=normalized_source, source_ordinal=event.source_ordinal,
        provenance=event.provenance,
    )


def _persist_batch(
    connection: sqlite3.Connection, prepared: _PreparedSource,
    batch: CodexEventBatch, project_root: Path, project_id: str,
    hasher: Pseudonymizer,
) -> None:
    _persist_source(connection, prepared, project_id)
    grouped: dict[str, list[CodexEventFact]] = {}
    for event in batch.events:
        _persist_event(connection, prepared, project_id, event)
        if event.thread_key is not None:
            grouped.setdefault(event.thread_key, []).append(event)
    test_evidence = TestEvidenceBuffer(
        connection, prepared.source_digest, {}, hasher.digest,
    )
    for session, events in grouped.items():
        _upsert_session(connection, project_id, project_root, session, tuple(events), hasher)
        normalized_source, logical = _normalized_source(
            connection, prepared, project_id, session, hasher,
        )
        for event in events:
            _persist_normalized_event(
                connection, prepared, project_id, event,
                normalized_source, logical, hasher, project_root,
            )
            tool = event.tool
            if (
                tool is not None and tool.call_key is not None
                and tool.safe_name == "exec_command"
            ):
                logical_call = tool.call_key
                rejected_test = (
                    tool.phase == "completed"
                    and tool.exit_status is None
                    and tool.status in {"declined", "cancelled", "interrupted"}
                )
                if rejected_test:
                    test_evidence.reject(
                        logical_call, session_key=session, tool_call_key=logical_call,
                    )
                elif tool.ephemeral_command is not None:
                    test_evidence.intent(
                        logical_call_id=logical_call,
                        model_call_id=logical_call,
                        command=tool.ephemeral_command,
                        session_key=session,
                        line_number=event.source_ordinal,
                        observed_at=event.observed_at,
                        turn_key=event.turn_key,
                        tool_call_key=logical_call,
                    )
                if not rejected_test and tool.phase == "completed":
                    test_evidence.result(
                        logical_call,
                        (
                            {
                                "exit_code": tool.exit_status,
                                "output": tool.ephemeral_output or "",
                            }
                            if tool.exit_status is not None else {}
                        ),
                        event.observed_at,
                        session_key=session,
                        line_number=event.source_ordinal,
                        turn_key=event.turn_key,
                        tool_call_key=logical_call,
                    )
            if event.child_thread_key is not None:
                connection.execute(
                    """INSERT INTO rollout_sessions(
                           session_key,project_id,path_key,resume_segments,
                           conversation_key,started_at,last_activity_at)
                       VALUES (?,?,'unresolved',1,?,?,?) ON CONFLICT DO NOTHING""",
                    (
                        event.child_thread_key, project_id, event.child_thread_key,
                        event.observed_at, event.observed_at,
                    ),
                )
                assert_session_project(
                    connection, session_key=event.child_thread_key,
                    project_id=project_id,
                )
                persist_confirmed_parent(
                    connection, child_key=event.child_thread_key,
                    parent_key=event.parent_thread_key or session,
                    project_id=project_id,
                )
    test_evidence.flush()
    connection.executemany(
        """INSERT INTO codex_event_issues(
               source_digest,source_ordinal,event_key,issue_code)
           VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
        (
            (prepared.source_digest, issue.source_ordinal, issue.event_key, issue.code)
            for issue in batch.issues
        ),
    )


def persist_prepared_codex_event_sources(
    connection: sqlite3.Connection,
    sources: Iterable[PreparedCodexEventSource],
    project_root: Path | str,
    project_id: str,
    *,
    hash_key: bytes,
) -> CodexEventIngestReport:
    """Persist pre-parsed stat-bound sources inside the caller's transaction."""
    items = copy.deepcopy(tuple(sources))
    validate_prepared_codex_event_sources_key(items, hash_key)
    if not connection.in_transaction:
        raise ValueError("prepared event persistence requires an owning transaction")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be non-empty")
    hasher = Pseudonymizer(hash_key)
    hash_token = ACTIVE_HASHER.set(hasher)
    try:
        for item in items:
            if source_stat(item.path) != item.source_stat:
                raise SourceChanged(SOURCE_CHANGED_MESSAGE)
        unique: dict[str, tuple[_PreparedSource, CodexEventBatch]] = {}
        locations: dict[str, set[str]] = {}
        for item in items:
            source_digest = hasher.digest(
                "source",
                f"codex-event/{project_id}/{item.schema}/{item.raw_digest}",
            )
            prepared = _PreparedSource(
                item.path,
                source_digest,
                item.location_key,
                item.raw_digest,
                item.line_count,
                item.byte_count,
                item.schema,
            )
            locations.setdefault(source_digest, set()).add(item.location_key)
            unique.setdefault(source_digest, (prepared, item.batch))
        root = Path(project_root)
        for item, batch in unique.values():
            _persist_batch(connection, item, batch, root, project_id, hasher)
            for location in locations[item.source_digest]:
                connection.execute(
                    """INSERT INTO codex_event_source_locations(
                           source_digest,location_key)
                       VALUES (?,?) ON CONFLICT DO NOTHING""",
                    (item.source_digest, location),
                )
        report = CodexEventIngestReport(
            files_seen=len(items),
            unique_sources=len(unique),
            events=sum(len(batch.events) for _item, batch in unique.values()),
            issues=sum(len(batch.issues) for _item, batch in unique.values()),
        )
        for item in items:
            if source_stat(item.path) != item.source_stat:
                raise SourceChanged(SOURCE_CHANGED_MESSAGE)
        validate_prepared_codex_event_sources_key(items, hash_key)
        return report
    finally:
        ACTIVE_HASHER.reset(hash_token)


def ingest_codex_events(
    store: HydraStore, sources: Iterable[CodexEventSource],
    project_root: Path | str, project_id: str, *, hash_key: bytes,
    prepared_sources: Iterable[PreparedCodexEventSource] = (),
) -> CodexEventIngestReport:
    """Import privacy-safe event facts with content idempotency and canonical identities."""
    prepared_items = copy.deepcopy(tuple(prepared_sources))
    validate_prepared_codex_event_sources_key(prepared_items, hash_key)
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be non-empty")
    hasher = Pseudonymizer(hash_key)
    hash_token = ACTIVE_HASHER.set(hasher)
    try:
        prepared_summary: dict[str, tuple[int, int]] = {}
        for item in prepared_items:
            digest = hasher.digest(
                "source",
                f"codex-event/{project_id}/{item.schema}/{item.raw_digest}",
            )
            prepared_summary.setdefault(
                digest, (len(item.batch.events), len(item.batch.issues)),
            )
        expected_prepared_report = CodexEventIngestReport(
            len(prepared_items),
            len(prepared_summary),
            sum(events for events, _issues in prepared_summary.values()),
            sum(issues for _events, issues in prepared_summary.values()),
        )
        items = tuple(sources)
        prepared = tuple(_prepare(item, hasher, project_id) for item in items)
        unique: dict[str, tuple[_PreparedSource, CodexEventBatch]] = {}
        locations: dict[str, set[str]] = {}
        for item in prepared:
            locations.setdefault(item.source_digest, set()).add(item.location_key)
            if item.source_digest in unique:
                continue
            batch = read_codex_event_jsonl(item.path, schema=item.schema, privacy_key=hash_key)
            fingerprint = _stream_fingerprint(item.path)
            if fingerprint != (item.raw_digest, item.line_count, item.byte_count):
                raise EventAdapterError("event source changed while reading")
            unique[item.source_digest] = (item, batch)
        with store.rollout_transaction() as connection:
            for item, batch in unique.values():
                _persist_batch(
                    connection, item, batch, Path(project_root), project_id, hasher,
                )
                for location in locations[item.source_digest]:
                    connection.execute(
                        """INSERT INTO codex_event_source_locations(source_digest,location_key)
                           VALUES (?,?) ON CONFLICT DO NOTHING""",
                        (item.source_digest, location),
                    )
            prepared_report = persist_prepared_codex_event_sources(
                connection,
                prepared_items,
                project_root,
                project_id,
                hash_key=hash_key,
            )
            if prepared_report != expected_prepared_report:
                raise EventAdapterError("prepared event source report mismatch")
            refresh_token_source_selection(connection, project_id)
            reconcile_token_epochs(
                connection, project_id,
                lambda source, line, kind: _persist_epoch_diagnostic(
                    connection, source, line, kind,
                ),
            )
            reconcile_fork_baselines(connection, project_id)
            reconcile_turn_attempts(
                connection,
                lambda source, line, kind: _persist_epoch_diagnostic(
                    connection, source, line, kind,
                ),
            )
        prepared_only = {
            digest: counts
            for digest, counts in prepared_summary.items()
            if digest not in unique
        }
        return CodexEventIngestReport(
            len(items) + prepared_report.files_seen,
            len(unique) + len(prepared_only),
            sum(len(batch.events) for _item, batch in unique.values())
            + sum(events for events, _issues in prepared_only.values()),
            sum(len(batch.issues) for _item, batch in unique.values())
            + sum(issues for _events, issues in prepared_only.values()),
        )
    finally:
        ACTIVE_HASHER.reset(hash_token)
