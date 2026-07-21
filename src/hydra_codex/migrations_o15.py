"""Sanitize legacy annotation transport ordering identities."""

from __future__ import annotations


O15_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (31, (
        """UPDATE annotation_transport_events
              SET staged_order = printf('%020d', staged_at_ns)
                  || ':horder_v1_'
                  || substr(transport_key, length('htransport_v1_') + 1)""",
    )),
)


O15_REQUIRED_SCHEMA: dict[str, set[str]] = {}
