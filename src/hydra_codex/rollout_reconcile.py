"""Deterministic reconciliation for turn attempts and cumulative token epochs."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Callable

from .rollout_privacy import canonical_timestamp


@dataclass
class _Attempt:
    state: str = "open"
    started_at: str | None = None
    started_epoch: float | None = None
    finished_at: str | None = None
    finished_epoch: float | None = None
    emitted_duration_ms: int | None = None


def _event_order(row: sqlite3.Row) -> tuple[int, float, str, int, str]:
    return (
        0 if row[5] is not None else 1,
        float(row[5] or 0),
        row[8],
        int(row[9]),
        row[0],
    )


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
        source_open: dict[str, sqlite3.Row] = {}
        for event in sorted(events, key=lambda item: (item[8], item[9])):
            if event[3] == "started":
                source_open[event[8]] = event
            elif event[8] in source_open:
                started = source_open.pop(event[8])
                if started[5] is not None and event[5] is not None and event[5] < started[5] and diagnose:
                    diagnose(event[7], int(event[9]), "invalid_turn_interval")
        events.sort(key=_event_order)
        attempts: list[_Attempt] = []
        active: _Attempt | None = None
        for event in events:
            if event[3] == "started":
                if active is None:
                    active = _Attempt(started_at=event[4], started_epoch=event[5])
                    attempts.append(active)
                elif diagnose:
                    diagnose(event[7], int(event[9]), "duplicate_turn_start")
                continue
            state = "completed" if event[3] == "completed" else "aborted"
            if active is None:
                active = _Attempt()
                attempts.append(active)
            active.state = state
            active.finished_at = event[4]
            active.finished_epoch = event[5]
            active.emitted_duration_ms = event[6]
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
                       started_at,finished_at,timing_provenance)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (session, turn, ordinal, attempt.state, attempt.emitted_duration_ms, wall,
                 attempt.started_at, attempt.finished_at, provenance),
            )


def _timestamp_order(row: sqlite3.Row) -> tuple[int, float, str, int]:
    value = row[7]
    epoch = canonical_timestamp(value).epoch
    return (0 if epoch is not None else 1, float(epoch or 0), row[0], int(row[1]))


def reconcile_token_epochs(
    connection: sqlite3.Connection,
    project_id: str,
    diagnose: Callable[[str, int, str], None] | None = None,
) -> None:
    rows = list(connection.execute(
        """SELECT source_digest,line_number,session_key,input_tokens,cached_input_tokens,
                  output_tokens,reasoning_tokens,observed_at,cache_write_tokens
             FROM token_snapshots
            WHERE project_id=? AND contributes_total=1
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
        session_rows.sort(key=_timestamp_order)
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
