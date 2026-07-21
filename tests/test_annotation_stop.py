from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hydra_codex.annotation_core import (
    CapabilityRejected,
    StopState,
    annotate_with_capability,
    finish_turn,
    issue_capability,
    observe_stop,
    record_initial_understand,
)
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore
from tests.test_annotation_core import (
    EXPIRES_AT,
    finish_payload,
    phase_payload,
    request,
    turn_context,
)


class AnnotationStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)
        self.keys = Pseudonymizer(b"a" * 32)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def issue(self):
        return issue_capability(
            self.store,
            self.keys,
            turn_context(),
            expires_at=EXPIRES_AT,
        )

    def test_stop_requests_one_retry_then_stages_missing_finish_without_blocking(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )

        first = observe_stop(
            self.store, self.keys, issued.token, observed_at="2026-07-21T09:01:00Z"
        )
        second = observe_stop(
            self.store, self.keys, issued.token, observed_at="2026-07-21T09:02:00Z"
        )
        third = observe_stop(
            self.store, self.keys, issued.token, observed_at="2026-07-21T09:03:00Z"
        )

        capability = self.store.connection.execute("SELECT stop_retry,revoked_at FROM turn_capabilities").fetchone()
        facts = self.store.connection.execute(
            "SELECT fact_kind,COUNT(*) FROM semantic_fact_staging GROUP BY fact_kind"
        ).fetchall()
        binding = self.store.connection.execute("SELECT state,finished_at FROM trusted_turn_bindings").fetchone()

        self.assertEqual((first, second, third), (
            StopState.RETRY_REQUIRED,
            StopState.SELF_REPORT_MISSING,
            StopState.SELF_REPORT_MISSING,
        ))
        self.assertEqual(tuple(capability), (1, "2026-07-21T09:02:00Z"))
        self.assertEqual([tuple(row) for row in facts], [("self_report_missing", 1)])
        self.assertEqual(tuple(binding), ("finished", "2026-07-21T09:02:00Z"))

    def test_stop_is_monotonic_against_annotations_intervals_and_first_stop(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )
        annotate_with_capability(
            self.store,
            self.keys,
            issued.token,
            request(1, observed_at="2026-07-21T09:05:00Z"),
            phase_payload(),
        )

        with self.assertRaisesRegex(CapabilityRejected, "out of order"):
            observe_stop(
                self.store,
                self.keys,
                issued.token,
                observed_at="2026-07-21T09:04:59Z",
            )
        binding = self.store.connection.execute(
            "SELECT first_stop_at,state FROM trusted_turn_bindings"
        ).fetchone()
        self.assertEqual(tuple(binding), (None, "open"))
        self.assertEqual(
            self.store.connection.execute("SELECT MAX(stop_retry) FROM turn_capabilities").fetchone()[0],
            0,
        )

        self.store.connection.execute(
            "UPDATE semantic_intervals SET started_at='2026-07-21T09:06:00Z' WHERE ended_at IS NULL"
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(CapabilityRejected, "out of order"):
            observe_stop(
                self.store,
                self.keys,
                issued.token,
                observed_at="2026-07-21T09:05:30Z",
            )

        self.assertEqual(
            observe_stop(
                self.store,
                self.keys,
                issued.token,
                observed_at="2026-07-21T09:07:00Z",
            ),
            StopState.RETRY_REQUIRED,
        )
        first_stop = self.store.connection.execute(
            "SELECT first_stop_at FROM trusted_turn_bindings"
        ).fetchone()[0]
        self.assertEqual(first_stop, "2026-07-21T09:07:00Z")
        self.store.close()
        self.store = HydraStore(self.database)

        with self.assertRaisesRegex(CapabilityRejected, "out of order"):
            observe_stop(
                self.store,
                self.keys,
                issued.token,
                observed_at="2026-07-21T09:06:30Z",
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT state FROM trusted_turn_bindings"
            ).fetchone()[0],
            "open",
        )
        self.assertEqual(
            observe_stop(
                self.store,
                self.keys,
                issued.token,
                observed_at="2026-07-21T09:08:00Z",
            ),
            StopState.SELF_REPORT_MISSING,
        )
        with self.assertRaisesRegex(CapabilityRejected, "out of order"):
            observe_stop(
                self.store,
                self.keys,
                issued.token,
                observed_at="2026-07-21T09:06:59Z",
            )

    def test_legacy_consumed_stop_retry_without_timestamp_fails_closed(self) -> None:
        issued = self.issue()
        self.store.connection.execute("UPDATE turn_capabilities SET stop_retry=1")
        self.store.connection.commit()

        with self.assertRaisesRegex(CapabilityRejected, "state is inconsistent"):
            observe_stop(
                self.store,
                self.keys,
                issued.token,
                observed_at="2026-07-21T09:01:00Z",
            )

    def test_finish_makes_stop_complete_without_consuming_retry(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )
        finish_turn(
            self.store, self.keys, issued.token, request(1), finish_payload()
        )

        state = observe_stop(
            self.store, self.keys, issued.token, observed_at="2026-07-21T09:03:00Z"
        )

        retry = self.store.connection.execute("SELECT stop_retry FROM turn_capabilities").fetchone()[0]
        self.assertEqual(state, StopState.FINISHED)
        self.assertEqual(retry, 0)


if __name__ == "__main__":
    unittest.main()
