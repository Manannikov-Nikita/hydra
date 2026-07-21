"""Deterministic reconciliation for turn attempts and cumulative token epochs."""

from __future__ import annotations

from dataclasses import dataclass, replace
import sqlite3
from typing import Callable

from .rollout_privacy import canonical_timestamp
from .source_authority import SOURCE_AUTHORITY, source_family


@dataclass
class _Attempt:
    state: str = "open"
    started_at: str | None = None
    started_epoch: float | None = None
    finished_at: str | None = None
    finished_epoch: float | None = None
    emitted_duration_ms: int | None = None
    started_event: _LifecycleEvent | None = None
    terminal_event: _LifecycleEvent | None = None


@dataclass(frozen=True)
class _LifecycleEvent:
    event_key: str
    session_key: str
    turn_key: str
    event_kind: str
    observed_at: str | None
    timestamp_epoch: float | None
    emitted_duration_ms: int | None
    source_digest: str
    logical_source_key: str
    source_ordinal: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "_LifecycleEvent":
        return cls(
            event_key=str(row[0]), session_key=str(row[1]), turn_key=str(row[2]),
            event_kind=str(row[3]), observed_at=row[4], timestamp_epoch=row[5],
            emitted_duration_ms=row[6], source_digest=str(row[7]),
            logical_source_key=str(row[8]), source_ordinal=int(row[9]),
        )


def _event_order(event: _LifecycleEvent) -> tuple[int, float, str, int, str]:
    return (
        0 if event.timestamp_epoch is not None else 1,
        float(event.timestamp_epoch or 0),
        event.logical_source_key,
        event.source_ordinal,
        event.event_key,
    )


def _event_authority(
    connection: sqlite3.Connection, event: _LifecycleEvent | None,
) -> int:
    if event is None:
        return -1
    return SOURCE_AUTHORITY[source_family(connection, event.source_digest)]


def _deduplicated_lifecycle_events(
    connection: sqlite3.Connection, rows: list[sqlite3.Row],
) -> list[_LifecycleEvent]:
    """Merge matching cross-adapter facts while preserving adapter-local retries."""
    exact: dict[tuple[str, float], list[_LifecycleEvent]] = {}
    missing: list[_LifecycleEvent] = []
    events = [_LifecycleEvent.from_row(row) for row in rows]
    for event in events:
        if event.timestamp_epoch is None:
            missing.append(event)
            continue
        phase = "started" if event.event_kind == "started" else "terminal"
        exact.setdefault((phase, event.timestamp_epoch), []).append(event)

    merged: list[_LifecycleEvent] = []
    for group in exact.values():
        families: dict[str, list[_LifecycleEvent]] = {}
        for event in group:
            families.setdefault(
                source_family(connection, event.source_digest), [],
            ).append(event)
        for family_events in families.values():
            family_events.sort(key=lambda event: (
                event.logical_source_key, event.source_ordinal, event.event_key,
            ))
        for index in range(max(len(items) for items in families.values())):
            candidates = [
                items[index] for items in families.values() if index < len(items)
            ]
            authoritative = max(candidates, key=lambda event: (
                SOURCE_AUTHORITY[source_family(connection, event.source_digest)],
                event.source_digest, event.source_ordinal, event.event_key,
            ))
            durations = {
                event.emitted_duration_ms for event in candidates
                if event.emitted_duration_ms is not None
            }
            merged_duration = next(iter(durations)) if len(durations) == 1 else None
            merged.append(replace(
                authoritative, emitted_duration_ms=merged_duration,
            ))
    return merged + missing


def reconcile_turn_attempts(
    connection: sqlite3.Connection,
    diagnose: Callable[[str, int, str], None] | None = None,
) -> None:
    rows = list(connection.execute(
        """SELECT event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                  emitted_duration_ms,source_digest,logical_source_key,source_ordinal
             FROM turn_lifecycle_events"""
    ))
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row[1], row[2]), []).append(row)
    for (session, turn), events in groups.items():
        events = _deduplicated_lifecycle_events(connection, events)
        source_open: dict[str, _LifecycleEvent] = {}
        for event in sorted(
            events, key=lambda item: (item.logical_source_key, item.source_ordinal),
        ):
            if event.event_kind == "started":
                source_open[event.logical_source_key] = event
            elif event.logical_source_key in source_open:
                started = source_open.pop(event.logical_source_key)
                if (
                    started.timestamp_epoch is not None
                    and event.timestamp_epoch is not None
                    and event.timestamp_epoch < started.timestamp_epoch
                    and diagnose
                ):
                    diagnose(
                        event.source_digest, event.source_ordinal,
                        "invalid_turn_interval",
                    )
        events.sort(key=_event_order)
        attempts: list[_Attempt] = []
        active: _Attempt | None = None
        for event in events:
            previous = next(
                (
                    attempt for attempt in reversed(attempts)
                    if attempt is not active and attempt.terminal_event is not None
                ),
                None,
            )
            if event.event_kind == "started":
                if active is None:
                    # This loop is scoped to one raw turn key.  A lower-authority
                    # duplicate start must not reopen a turn already closed by a
                    # more authoritative adapter; peers and stronger sources can
                    # still prove a real retry.
                    if (
                        previous is not None
                        and _event_authority(connection, event)
                        < _event_authority(connection, previous.terminal_event)
                    ):
                        continue
                    active = _Attempt(
                        started_at=event.observed_at,
                        started_epoch=event.timestamp_epoch,
                        started_event=event,
                    )
                    attempts.append(active)
                elif diagnose:
                    diagnose(
                        event.source_digest, event.source_ordinal,
                        "duplicate_turn_start",
                    )
                continue
            state = "completed" if event.event_kind == "completed" else "aborted"
            event_authority = _event_authority(connection, event)
            if active is not None:
                # A lower-authority terminal arriving after an authoritative
                # terminal must not close a newer authoritative retry.  A
                # same-source retry remains a real attempt and is preserved.
                if (
                    previous is not None
                    and event_authority
                    < _event_authority(connection, previous.terminal_event)
                    and event_authority
                    < _event_authority(connection, active.started_event)
                ):
                    continue
                target = active
            elif previous is not None:
                previous_authority = _event_authority(
                    connection, previous.terminal_event,
                )
                if event_authority < previous_authority:
                    continue
                if event_authority > previous_authority:
                    target = previous
                else:
                    target = _Attempt()
                    attempts.append(target)
            else:
                target = _Attempt()
                attempts.append(target)
            target.state = state
            target.finished_at = event.observed_at
            target.finished_epoch = event.timestamp_epoch
            target.emitted_duration_ms = event.emitted_duration_ms
            target.terminal_event = event
            if target is active:
                active = None
        connection.execute("DELETE FROM turn_attempts WHERE session_key=? AND turn_key=?", (session, turn))
        for ordinal, attempt in enumerate(attempts, start=1):
            wall = None
            if attempt.started_epoch is not None and attempt.finished_epoch is not None and attempt.finished_epoch >= attempt.started_epoch:
                wall = int(round((attempt.finished_epoch - attempt.started_epoch) * 1000))
            provenance = "derived" if wall is not None else "estimated"
            connection.execute(
                """INSERT INTO turn_attempts(
                       session_key,turn_key,attempt_ordinal,state,emitted_duration_ms,wall_duration_ms,
                       started_at,finished_at,timing_provenance,
                       started_event_key,terminal_event_key,
                       started_logical_source_key,terminal_logical_source_key,
                       started_source_ordinal,terminal_source_ordinal)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session, turn, ordinal, attempt.state, attempt.emitted_duration_ms, wall,
                 attempt.started_at, attempt.finished_at, provenance,
                 attempt.started_event.event_key if attempt.started_event else None,
                 attempt.terminal_event.event_key if attempt.terminal_event else None,
                 attempt.started_event.logical_source_key if attempt.started_event else None,
                 attempt.terminal_event.logical_source_key if attempt.terminal_event else None,
                 attempt.started_event.source_ordinal if attempt.started_event else None,
                 attempt.terminal_event.source_ordinal if attempt.terminal_event else None),
            )


def _timestamp_order(row: sqlite3.Row) -> tuple[int, float, str, int]:
    value = row[7]
    epoch = canonical_timestamp(value).epoch
    return (0 if epoch is not None else 1, float(epoch or 0), row[0], int(row[1]))


def _token_epoch_order(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Preserve progression inside one mixed-quality App cumulative stream."""
    sources = {str(row[0]) for row in rows}
    app_only = all(row[9] == "app_server" for row in rows)
    mixed_time = any(canonical_timestamp(row[7]).epoch is None for row in rows)
    if app_only and len(sources) == 1 and mixed_time:
        return sorted(rows, key=lambda row: int(row[1]))
    return sorted(rows, key=_timestamp_order)


def reconcile_token_epochs(
    connection: sqlite3.Connection,
    project_id: str,
    diagnose: Callable[[str, int, str], None] | None = None,
) -> None:
    rows = list(connection.execute(
        """SELECT source_digest,line_number,session_key,input_tokens,cached_input_tokens,
                  output_tokens,reasoning_tokens,observed_at,cache_write_tokens,
                  source_family
             FROM token_snapshots
            WHERE project_id=? AND contributes_total=1 AND vector_valid=1
              AND source_family IN ('rollout','app_server')""",
        (project_id,),
    ))
    connection.execute(
        """DELETE FROM rollout_diagnostics
              WHERE envelope_kind='counter_reset' AND source_digest IN (
                    SELECT source_digest FROM token_snapshots WHERE project_id=?
              )""",
        (project_id,),
    )
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row[2], []).append(row)
    for session_rows in groups.values():
        session_rows = _token_epoch_order(session_rows)
        epoch = 0
        last: list[int | None] = [None] * 5
        for row in session_rows:
            current = [row[3], row[4], row[5], row[6], row[8]]
            if any(now is not None and before is not None and now < before for now, before in zip(current, last)):
                epoch += 1
                last = [None] * 5
                if diagnose:
                    diagnose(row[0], int(row[1]), "counter_reset")
            for index, value in enumerate(current):
                if value is not None:
                    last[index] = value
            connection.execute(
                "UPDATE token_snapshots SET epoch=? WHERE source_digest=? AND line_number=?",
                (epoch, row[0], row[1]),
            )


def reconcile_fork_baselines(connection: sqlite3.Connection, project_id: str) -> None:
    """Rebuild exact replay baselines against each session's global earliest start."""
    sessions = list(connection.execute(
        """SELECT sessions.session_key,sessions.started_at
             FROM rollout_sessions AS sessions
             JOIN session_edges AS edges ON edges.child_key=sessions.session_key
            WHERE sessions.project_id=? AND edges.confidence_kind='confirmed'
              AND edges.parent_key IS NOT NULL""",
        (project_id,),
    ))
    connection.execute(
        """DELETE FROM fork_baselines WHERE child_key IN (
               SELECT session_key FROM rollout_sessions WHERE project_id=?
           )""",
        (project_id,),
    )
    for session_key, started_at in sessions:
        started_epoch = canonical_timestamp(started_at).epoch
        if started_epoch is None:
            continue
        candidates: list[tuple[tuple[float, str, int], sqlite3.Row]] = []
        for row in connection.execute(
            """SELECT source_digest,line_number,input_tokens,cached_input_tokens,
                      output_tokens,reasoning_tokens,cache_write_tokens,observed_at
                 FROM token_snapshots
                WHERE session_key=? AND project_id=? AND completeness='complete'
                  AND vector_valid=1
                  AND contributes_total=1
                  AND source_family IN ('rollout','app_server')""",
            (session_key, project_id),
        ):
            observed_epoch = canonical_timestamp(row[7]).epoch
            required = (row[2], row[3], row[4], row[5])
            valid_required = all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in required
            )
            cache_write = row[6]
            valid_cache_write = (
                cache_write is None
                or isinstance(cache_write, int) and not isinstance(cache_write, bool) and cache_write >= 0
            )
            if (
                observed_epoch is None or not valid_required or not valid_cache_write
                or row[3] > row[2] or not 0 <= observed_epoch - started_epoch <= 1
            ):
                continue
            candidates.append(((observed_epoch, row[0], int(row[1])), row))
        if not candidates:
            continue
        selected = max(candidates, key=lambda item: item[0])[1]
        connection.execute(
            """INSERT INTO fork_baselines(
                   child_key,source_digest,line_number,input_tokens,cached_input_tokens,
                   output_tokens,reasoning_tokens,cache_write_tokens,provenance,observed_at)
               VALUES (?,?,?,?,?,?,?,?, 'exact', ?)""",
            (
                session_key, selected[0], selected[1], selected[2], selected[3],
                selected[4], selected[5], selected[6], selected[7],
            ),
        )
