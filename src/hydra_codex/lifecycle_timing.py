"""Deterministic authority for lifecycle boundaries with mixed timing quality."""

from __future__ import annotations

from typing import Iterable

from .task_tree_types import LifecycleObservation


_TIMING_AUTHORITY = {
    "estimated": 0,
    "model_reported": 1,
    "derived": 2,
    "exact": 3,
}


def timing_authority(observation: LifecycleObservation) -> int:
    return _TIMING_AUTHORITY[observation.timing_provenance]


def is_later_attempt_start(
    start: LifecycleObservation, completion: LifecycleObservation,
) -> bool:
    """Keep a distinct observed turn open without trusting same-turn receipt delay."""
    distinct_turn = (
        start.turn_key is None or completion.turn_key is None
        or start.turn_key != completion.turn_key
    )
    if not distinct_turn and timing_authority(start) < timing_authority(completion):
        return False
    if start.observed_at > completion.observed_at:
        return True
    if start.observed_at != completion.observed_at:
        return False
    if (
        start.logical_source_key == completion.logical_source_key
        and start.source_ordinal is not None
        and completion.source_ordinal is not None
    ):
        return start.source_ordinal > completion.source_ordinal
    return distinct_turn


def select_lifecycle_boundary(
    observations: Iterable[LifecycleObservation],
    starts: Iterable[LifecycleObservation] = (),
) -> tuple[LifecycleObservation | None, int]:
    """Select authority within one turn, then select the latest started turn."""
    candidates = tuple(observations)
    if not candidates:
        return None, 0
    start_items = tuple(starts)
    grouped: dict[str | None, list[LifecycleObservation]] = {}
    for item in candidates:
        grouped.setdefault(item.turn_key, []).append(item)

    attempts: list[
        tuple[LifecycleObservation, int, LifecycleObservation | None]
    ] = []
    for turn_key, items in grouped.items():
        selected = max(items, key=lambda item: (
            timing_authority(item), item.observed_at,
            item.logical_source_key or "",
            item.source_ordinal if item.source_ordinal is not None else -1,
        ))
        conflicts = sum(
            timing_authority(item) < timing_authority(selected)
            and item.observed_at != selected.observed_at
            for item in items
        )
        matching_starts = tuple(
            item for item in start_items if item.turn_key == turn_key
        )
        started = max(matching_starts, key=lambda item: (
            timing_authority(item), item.observed_at,
            item.logical_source_key or "",
            item.source_ordinal if item.source_ordinal is not None else -1,
        )) if matching_starts else None
        attempts.append((selected, conflicts, started))

    selected, conflicts, _started = max(attempts, key=lambda attempt: (
        (attempt[2] or attempt[0]).observed_at,
        attempt[0].observed_at,
        timing_authority(attempt[0]),
        attempt[0].turn_key or "",
        attempt[0].logical_source_key or "",
        attempt[0].source_ordinal if attempt[0].source_ordinal is not None else -1,
    ))
    return selected, conflicts
