"""Deterministic reconciliation for turn attempts and cumulative token epochs."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Callable


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


def _timestamp_order(row: sqlite3.Row) -> tuple[int, str, str, int]:
    value = row[7]
    return (0 if value is not None else 1, value or "", row[0], int(row[1]))


def reconcile_token_epochs(connection: sqlite3.Connection, project_id: str) -> None:
    rows = list(connection.execute(
        """SELECT source_digest,line_number,session_key,input_tokens,cached_input_tokens,
                  output_tokens,reasoning_tokens,observed_at,cache_write_tokens
             FROM token_snapshots WHERE project_id=?""",
        (project_id,),
    ))
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
            for index, value in enumerate(current):
                if value is not None:
                    last[index] = value
            connection.execute(
                "UPDATE token_snapshots SET epoch=? WHERE source_digest=? AND line_number=?",
                (epoch, row[0], row[1]),
            )
