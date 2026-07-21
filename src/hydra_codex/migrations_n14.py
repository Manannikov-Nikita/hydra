"""Private annotation transport receipts and categorical diagnostics."""

from __future__ import annotations


N14_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (30, (
        """CREATE TABLE annotation_transport_events (
               transport_key TEXT PRIMARY KEY,
               project_id TEXT NOT NULL,
               session_key TEXT NOT NULL,
               turn_key TEXT NOT NULL,
               request_digest TEXT,
               disposition TEXT NOT NULL
                   CHECK(disposition IN ('accepted','quarantined')),
               diagnostic_category TEXT CHECK(diagnostic_category IS NULL OR
                   diagnostic_category IN (
                       'malformed','expired','duplicate','out_of_order',
                       'wrong_capability'
                   )),
               staged_at TEXT NOT NULL,
               staged_at_ns INTEGER NOT NULL CHECK(staged_at_ns >= 0),
               staged_order TEXT NOT NULL,
               received_at TEXT NOT NULL,
               latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
               provenance TEXT NOT NULL CHECK(provenance='derived'),
               CHECK((disposition='accepted' AND diagnostic_category IS NULL
                      AND request_digest IS NOT NULL) OR
                     (disposition='quarantined' AND diagnostic_category IS NOT NULL))
           )""",
        """CREATE UNIQUE INDEX annotation_transport_accepted_request
               ON annotation_transport_events(request_digest)
               WHERE disposition='accepted'""",
        """CREATE INDEX annotation_transport_turn_order
               ON annotation_transport_events(turn_key,disposition,staged_order)""",
    )),
)


N14_REQUIRED_SCHEMA = {
    "annotation_transport_events": {
        "transport_key", "project_id", "session_key", "turn_key",
        "request_digest", "disposition", "diagnostic_category", "staged_at",
        "staged_at_ns", "staged_order", "received_at", "latency_ms",
        "provenance",
    },
}
