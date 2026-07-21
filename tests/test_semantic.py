from __future__ import annotations

import unittest

from hydra_codex.semantic import (
    ComparableTask,
    SemanticMark,
    TokenDelta,
    evaluate_trend,
    reconcile_semantics,
    resolve_semantic_cause,
)


class SemanticReconciliationTests(unittest.TestCase):
    def test_phase_blocker_finish_allocate_only_timestamped_deltas(self) -> None:
        deltas = (
            TokenDelta("2026-07-20T00:00:01Z", 10, 12, 2),
            TokenDelta("2026-07-20T00:00:03Z", 20, 25, 3),
            TokenDelta("2026-07-20T00:00:05Z", 30, 38, 4),
            TokenDelta("2026-07-20T00:00:07Z", 40, 50, 5),
            TokenDelta(None, 50, 60, 6),
        )
        marks = (
            SemanticMark("phase", "implement", "plan", "2026-07-20T00:00:02Z", 1),
            SemanticMark("blocker", "implement", "infra_failure", "2026-07-20T00:00:04Z", 2),
            SemanticMark("phase", "test_targeted", "plan", "2026-07-20T00:00:06Z", 3),
            SemanticMark("finish", "test_targeted", "final_verification", "2026-07-20T00:00:08Z", 4),
        )

        result = reconcile_semantics(deltas, marks)

        self.assertEqual(result.phase_tokens["implement"].value, 50)
        self.assertEqual(result.phase_tokens["test_targeted"].value, 40)
        self.assertEqual(result.unclassified_tokens.value, 60)
        self.assertEqual(result.coverage.value, 0.6)
        self.assertEqual(result.coverage.provenance, "derived")
        self.assertIn("unusable_timestamp_in_coverage_denominator", result.coverage.caveats)
        self.assertIn("missing_timestamp", result.unclassified_tokens.caveats)
        self.assertEqual([interval.phase for interval in result.intervals], ["implement", "implement", "test_targeted"])

    def test_unusable_timestamp_tokens_remain_in_coverage_denominator(self) -> None:
        marks = (
            SemanticMark("phase", "implement", "plan", "2026-07-20T00:00:00Z", 1),
            SemanticMark("finish", "implement", "final_verification", "2026-07-20T00:00:02Z", 2),
        )

        for unusable_timestamp in (None, "not-a-timestamp"):
            with self.subTest(observed_at=unusable_timestamp):
                result = reconcile_semantics(
                    (
                        TokenDelta("2026-07-20T00:00:01Z", 50, 60, 5),
                        TokenDelta(unusable_timestamp, 50, 60, 5),
                    ),
                    marks,
                )

                self.assertEqual(result.phase_tokens["implement"].value, 50)
                self.assertEqual(result.unclassified_tokens.value, 50)
                self.assertEqual(result.coverage.value, 0.5)
                self.assertEqual(result.coverage.provenance, "derived")
                self.assertIn("unusable_timestamp_in_coverage_denominator", result.coverage.caveats)

    def test_zero_working_token_total_has_no_claimed_coverage(self) -> None:
        result = reconcile_semantics(
            (TokenDelta("2026-07-20T00:00:01Z", 0, 0, 0),),
            (
                SemanticMark("phase", "understand", "prompt", "2026-07-20T00:00:00Z", 1),
                SemanticMark("finish", "understand", "final_verification", "2026-07-20T00:00:02Z", 2),
            ),
        )

        self.assertIsNone(result.coverage.value)
        self.assertEqual(result.coverage.provenance, "estimated")
        self.assertIn("coverage_denominator_unavailable", result.coverage.caveats)

    def test_missing_phase_and_legacy_tree_remain_unclassified(self) -> None:
        deltas = (TokenDelta("2026-07-20T00:00:01Z", 7, 9, 1),)
        blocker_only = (SemanticMark("blocker", "review", "review_finding", "2026-07-20T00:00:00Z", 1),)

        blocked = reconcile_semantics(deltas, blocker_only)
        legacy = reconcile_semantics(deltas, ())

        self.assertEqual(blocked.phase_tokens, {})
        self.assertEqual(blocked.unclassified_tokens.value, 7)
        self.assertEqual(blocked.coverage.value, 0.0)
        self.assertEqual(legacy.coverage.value, 0.0)
        self.assertIn("no_active_phase", blocked.diagnostics)

    def test_duplicate_and_out_of_order_marks_are_deterministic(self) -> None:
        marks = (
            SemanticMark("finish", "test_full", "final_verification", "2026-07-20T00:00:05Z", 3),
            SemanticMark("phase", "implement", "plan", "2026-07-20T00:00:01Z", 1),
            SemanticMark("phase", "implement", "plan", "2026-07-20T00:00:01Z", 1),
            SemanticMark("phase", "test_full", "test_failure", "2026-07-20T00:00:03Z", 2),
        )
        deltas = (
            TokenDelta("2026-07-20T00:00:02Z", 11, 12, 1),
            TokenDelta("2026-07-20T00:00:04Z", 13, 14, 1),
        )

        first = reconcile_semantics(deltas, marks)
        second = reconcile_semantics(reversed(deltas), reversed(marks))

        self.assertEqual(first, second)
        self.assertEqual(first.phase_tokens["implement"].value, 11)
        self.assertEqual(first.phase_tokens["test_full"].value, 13)
        self.assertIn("duplicate_annotation", first.diagnostics)

    def test_deterministic_cause_wins_and_conflict_is_explicit(self) -> None:
        matching = resolve_semantic_cause("infra_failure", "infra_failure")
        conflict = resolve_semantic_cause("infra_failure", "test_failure")
        model_only = resolve_semantic_cause(None, "review_finding")

        self.assertEqual((matching.value, matching.provenance, matching.conflict), ("infra_failure", "derived", False))
        self.assertEqual((conflict.value, conflict.provenance, conflict.conflict), ("infra_failure", "derived", True))
        self.assertEqual((model_only.value, model_only.provenance, model_only.conflict), ("review_finding", "model_reported", False))

    def test_conflicting_keyed_token_deltas_choose_a_canonical_observation(self) -> None:
        marks = (
            SemanticMark("phase", "implement", "plan", "2026-07-20T00:00:00Z", 1),
            SemanticMark("finish", "implement", "final_verification", "2026-07-20T00:00:03Z", 4),
        )
        earlier = TokenDelta("2026-07-20T00:00:01Z", 10, 12, 2, event_key="delta-1", ordinal=2)
        later = TokenDelta("2026-07-20T00:00:02Z", 99, 120, 9, event_key="delta-1", ordinal=3)

        first = reconcile_semantics((later, earlier), marks)
        second = reconcile_semantics((earlier, later), marks)

        self.assertEqual(first, second)
        self.assertEqual(first.phase_tokens["implement"].value, 10)
        self.assertIn("conflicting_token_delta", first.diagnostics)

    def test_token_timestamp_and_trusted_ordinal_disagreement_is_diagnostic(self) -> None:
        deltas = (
            TokenDelta("2026-07-20T00:00:01Z", 10, 12, 2, event_key="delta-1", ordinal=2),
            TokenDelta("2026-07-20T00:00:02Z", 20, 24, 3, event_key="delta-2", ordinal=1),
        )

        result = reconcile_semantics(deltas, ())

        self.assertIn("out_of_order_token_delta", result.diagnostics)


class TrendRuleTests(unittest.TestCase):
    @staticmethod
    def task(
        tokens: int,
        *,
        reruns: int = 0,
        completed: bool = True,
        family: str = "quiz",
        working_tokens_provenance: str = "exact",
        test_reruns_provenance: str = "exact",
    ) -> ComparableTask:
        return ComparableTask(
            task_family=family,
            completed=completed,
            working_tokens=tokens,
            test_reruns=reruns,
            read_amplification=0,
            review_fix_cycles=0,
            compactions=0,
            compaction_normalized=False,
            metrics_complete=True,
            working_tokens_provenance=working_tokens_provenance,
            test_reruns_provenance=test_reruns_provenance,
        )

    def test_trend_requires_four_prior_tasks_and_second_exact_signal(self) -> None:
        history = tuple(self.task(value, reruns=1) for value in (100, 110, 90, 100))

        no_signal = evaluate_trend(self.task(200, reruns=1), history)
        warning = evaluate_trend(self.task(200, reruns=3), history)
        too_early = evaluate_trend(self.task(200, reruns=3), history[:3])

        self.assertFalse(no_signal.warning)
        self.assertTrue(warning.warning)
        self.assertEqual(warning.corroborating_signal, "test_reruns")
        self.assertFalse(too_early.warning)
        self.assertIn("insufficient_baseline", too_early.caveats)

    def test_incomplete_metrics_wrong_family_and_unnormalized_compaction_do_not_warn(self) -> None:
        history = tuple(self.task(100) for _ in range(4))
        current = ComparableTask(
            task_family="quiz", completed=True, working_tokens=200,
            test_reruns=0, read_amplification=0, review_fix_cycles=0,
            compactions=5, compaction_normalized=False, metrics_complete=True,
        )
        incomplete = ComparableTask(**{**current.__dict__, "metrics_complete": False, "test_reruns": 4})

        self.assertFalse(evaluate_trend(current, history).warning)
        self.assertFalse(evaluate_trend(incomplete, history).warning)
        self.assertFalse(evaluate_trend(current, tuple(self.task(100, family="essay") for _ in range(4))).warning)

    def test_trend_accepts_deterministic_derived_working_tokens(self) -> None:
        history = tuple(
            self.task(value, reruns=1, working_tokens_provenance="derived")
            for value in (100, 110, 90, 100)
        )
        current = self.task(200, reruns=3, working_tokens_provenance="derived")

        result = evaluate_trend(current, history)

        self.assertTrue(result.warning)
        self.assertEqual(result.corroborating_signal, "test_reruns")

    def test_trend_rejects_model_or_estimated_working_tokens(self) -> None:
        for provenance in ("model_reported", "estimated"):
            with self.subTest(provenance=provenance):
                history = tuple(
                    self.task(value, reruns=1, working_tokens_provenance=provenance)
                    for value in (100, 110, 90, 100)
                )
                current = self.task(200, reruns=3, working_tokens_provenance=provenance)

                result = evaluate_trend(current, history)

                self.assertFalse(result.warning)
                self.assertIn("incomparable_working_tokens", result.caveats)

    def test_trend_rejects_derived_signal_without_completeness_proof(self) -> None:
        history = tuple(
            self.task(
                value,
                reruns=1,
                working_tokens_provenance="derived",
                test_reruns_provenance="derived",
            )
            for value in (100, 110, 90, 100)
        )
        current = self.task(
            200,
            reruns=3,
            working_tokens_provenance="derived",
            test_reruns_provenance="derived",
        )

        result = evaluate_trend(current, history)

        self.assertFalse(result.warning)
        self.assertIn("incomparable_test_reruns", result.caveats)
        self.assertIn("no_exact_corroborating_signal", result.caveats)

    def test_comparable_task_rejects_negative_observed_metrics(self) -> None:
        with self.assertRaisesRegex(ValueError, "working_tokens"):
            self.task(-1)


if __name__ == "__main__":
    unittest.main()
