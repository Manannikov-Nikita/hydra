from __future__ import annotations

import unittest
import sqlite3

from hydra_codex.exact_time import parse_exact_timestamp
from hydra_codex.reconcile_annotations import load_intervals, phase_at
from hydra_codex.task_tree_storage import _attempt_instant


class ExactTimestampTests(unittest.TestCase):
    def test_parses_and_orders_one_through_nine_fractional_digits(self) -> None:
        whole = parse_exact_timestamp("2026-07-21T00:00:09Z")
        tenth = parse_exact_timestamp("2026-07-21T00:00:09.1Z")
        microsecond = parse_exact_timestamp("2026-07-21T00:00:09.123456Z")
        seventh = parse_exact_timestamp("2026-07-21T00:00:09.1234567Z")
        nanosecond = parse_exact_timestamp("2026-07-21T00:00:09.123456789Z")

        self.assertIsNotNone(whole)
        self.assertIsNotNone(tenth)
        self.assertIsNotNone(microsecond)
        self.assertIsNotNone(seventh)
        self.assertIsNotNone(nanosecond)
        assert whole and tenth and microsecond and seventh and nanosecond
        self.assertLess(whole, tenth)
        self.assertLess(tenth, microsecond)
        self.assertLess(microsecond, seventh)
        self.assertLess(seventh, nanosecond)
        self.assertEqual(tenth.canonical, "2026-07-21T00:00:09.100000Z")
        self.assertEqual(seventh.canonical, "2026-07-21T00:00:09.1234567Z")
        self.assertEqual(
            nanosecond.canonical, "2026-07-21T00:00:09.123456789Z",
        )
        self.assertEqual(seventh.presentation, microsecond.presentation)

    def test_normalizes_offsets_and_handles_pre_epoch_nanoseconds(self) -> None:
        utc = parse_exact_timestamp("2026-07-21T00:00:09.0000001Z")
        offset = parse_exact_timestamp("2026-07-21T02:00:09.0000001+02:00")
        pre_epoch = parse_exact_timestamp("1969-12-31T23:59:59.999999999Z")

        self.assertEqual(offset, utc)
        self.assertEqual(offset.canonical if offset else None, utc.canonical if utc else None)
        self.assertEqual(pre_epoch.epoch_nanoseconds if pre_epoch else None, -1)

    def test_rejects_non_rfc3339_and_unsupported_offsets(self) -> None:
        invalid = (
            "2026-07-21 00:00:09Z",
            "2026-07-21T00:00:09",
            "2026-07-21T00:00:09.1234567890Z",
            "2026-07-21T00:00:09-00:00",
            "2026-07-21T00:00:09+24:00",
            "2026-07-21T00:00:09+00:60",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(parse_exact_timestamp(value))

    def test_attempt_receipt_recovers_only_matching_public_timestamp(self) -> None:
        matching, provenance = _attempt_instant(
            "2026-07-21T00:00:09Z",
            "2026-07-21T00:00:09.0000001Z",
        )
        mismatched, mismatch_provenance = _attempt_instant(
            "2026-07-21T00:00:09Z",
            "2026-07-21T00:00:10.0000001Z",
        )

        self.assertEqual(provenance, "exact")
        self.assertEqual(
            matching.canonical if matching else None,
            "2026-07-21T00:00:09.0000001Z",
        )
        self.assertEqual(mismatch_provenance, "exact")
        self.assertEqual(
            mismatched.canonical if mismatched else None,
            "2026-07-21T00:00:09Z",
        )

    def test_exact_attempt_is_not_replaced_by_same_microsecond_receipt(self) -> None:
        authoritative, provenance = _attempt_instant(
            "2026-07-21T00:00:09.0000001Z",
            "2026-07-21T00:00:09.0000009Z",
        )
        exact_microsecond, _ = _attempt_instant(
            "2026-07-21T00:00:09.123456000Z",
            "2026-07-21T00:00:09.1234569Z",
        )

        self.assertEqual(provenance, "exact")
        self.assertEqual(
            authoritative.canonical if authoritative else None,
            "2026-07-21T00:00:09.0000001Z",
        )
        self.assertEqual(
            exact_microsecond.canonical if exact_microsecond else None,
            "2026-07-21T00:00:09.123456Z",
        )

    def test_semantic_interval_membership_keeps_submicrosecond_edges(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                """CREATE TABLE semantic_intervals(
                       session_key TEXT,project_id TEXT,started_at TEXT,
                       ended_at TEXT,phase TEXT,cause TEXT)"""
            )
            connection.executemany(
                """INSERT INTO semantic_intervals(
                       session_key,project_id,started_at,ended_at,phase,cause)
                   VALUES (?,'project',?,?,?,?)""",
                (
                    (
                        "starts-after", "2026-07-21T00:00:09.0000002Z", None,
                        "implement", "coding",
                    ),
                    (
                        "ends-after", "2026-07-21T00:00:08Z",
                        "2026-07-21T00:00:09.0000002Z", "verify", "tests",
                    ),
                ),
            )
            cutoff = parse_exact_timestamp("2026-07-21T00:00:10Z")
            observed = parse_exact_timestamp("2026-07-21T00:00:09.0000001Z")
            assert cutoff is not None and observed is not None

            starts_after, invalid_start = load_intervals(
                connection, "project", ("starts-after",),
                cutoff.presentation, cutoff,
            )
            ends_after, invalid_end = load_intervals(
                connection, "project", ("ends-after",),
                cutoff.presentation, cutoff,
            )

            self.assertEqual(invalid_start + invalid_end, 0)
            self.assertEqual(
                phase_at(starts_after["starts-after"], observed),
                (None, None, False),
            )
            self.assertEqual(
                phase_at(ends_after["ends-after"], observed),
                ("verify", "tests", False),
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
