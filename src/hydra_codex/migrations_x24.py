"""Privacy-safe labels for reconciled task presentation."""

from __future__ import annotations


X24_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (40, (
        "ALTER TABLE annotations ADD COLUMN task_label TEXT",
        "ALTER TABLE reconciled_tasks ADD COLUMN display_name TEXT",
    )),
)

X24_REQUIRED_SCHEMA = {
    "annotations": {"task_label"},
    "reconciled_tasks": {"display_name"},
}
