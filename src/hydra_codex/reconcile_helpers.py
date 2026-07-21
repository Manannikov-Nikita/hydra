"""Small read-only helpers shared by reconciliation fact assembly."""

from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Iterable

from .lifecycle_timing import is_later_attempt_start
from .reconcile_types import TaskPlan
from .task_tree_storage import _optional_timestamp
from .task_tree_types import LifecycleObservation


def has_later_root_start(
    root: str, lifecycle: Iterable[LifecycleObservation],
    completion: LifecycleObservation,
) -> bool:
    return any(
        item.session_id == root and item.kind == "task_started"
        and is_later_attempt_start(item, completion)
        for item in lifecycle
    )


def task_family(
    connection: sqlite3.Connection, project_id: str,
    sessions: tuple[str, ...], cutoff: datetime,
) -> tuple[str | None, bool, int, int]:
    placeholders = ",".join("?" for _ in sessions)
    rows = list(connection.execute(
        f"""SELECT a.task_family,a.observed_at,a.sequence,a.annotation_id
               FROM annotations a
              WHERE a.project_id=? AND a.session_id IN ({placeholders})""",
        (project_id, *sessions),
    ))
    invalid = sum(_optional_timestamp(row[1]) is None for row in rows)
    valid = [
        (observed, int(row[2]), str(row[3]), str(row[0]))
        for row in rows
        if (observed := _optional_timestamp(row[1])) is not None and observed <= cutoff
    ]
    valid.sort()
    real = [item for item in valid if item[3] != "unclassified"]
    families = {item[3] for item in real}
    return (real[-1][3] if real else None), len(families) > 1, invalid, len(valid)


def within_task_cutoff(
    plan: TaskPlan, observed_value: object,
    logical_source: object, source_ordinal: object,
) -> bool:
    observed = _optional_timestamp(observed_value)
    if observed is not None:
        return observed <= plan.cutoff_at
    return bool(
        plan.cutoff_source_key is not None
        and plan.cutoff_source_ordinal is not None
        and logical_source == plan.cutoff_source_key
        and isinstance(source_ordinal, int)
        and source_ordinal <= plan.cutoff_source_ordinal
    )
