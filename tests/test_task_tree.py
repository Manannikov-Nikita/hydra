from __future__ import annotations

from datetime import datetime, timezone
import unittest

from hydra_codex.exact_time import require_exact_timestamp
from hydra_codex.task_tree import (
    ActivityObservation,
    FileObservation,
    LifecycleObservation,
    NormalizedSession,
    ScalarFact,
    TestRunObservation,
    TokenObservation,
    TokenVector,
    TokenVectorFact,
    ToolObservation,
    aggregate_task_tree,
)


def at(second: int) -> datetime:
    return datetime(2026, 7, 21, 0, 0, second, tzinfo=timezone.utc)


class TaskTreeMetricTests(unittest.TestCase):
    def test_descendant_closure_is_cycle_safe_and_uses_root_completion_cutoff(self) -> None:
        sessions = (
            NormalizedSession("root", "child", at(0)),
            NormalizedSession("child", "root", at(2)),
            NormalizedSession("after-cutoff", "root", at(11)),
            NormalizedSession("unrelated", None, at(1)),
        )
        tokens = (
            TokenObservation("root", at(9), 1, TokenVector(100, 20, 10, 5)),
            TokenObservation("root", at(12), 2, TokenVector(900, 100, 90, 50)),
            TokenObservation("child", at(2), 1, TokenVector(30, 10, 2, 1)),
            TokenObservation("child", at(8), 2, TokenVector(50, 15, 4, 2)),
            TokenObservation("after-cutoff", at(12), 1, TokenVector(70, 10, 3, 1)),
            TokenObservation("unrelated", at(5), 1, TokenVector(80, 20, 2, 1)),
        )
        lifecycle = (LifecycleObservation("root", "task_complete", at(10)),)
        activities = tuple(
            ActivityObservation(session_id, observed_at)
            for session_id, observed_at in (
                ("root", at(10)), ("root", at(12)), ("child", at(8)),
                ("after-cutoff", at(12)), ("unrelated", at(5)),
            )
        )

        metrics = aggregate_task_tree(
            root_id="root", sessions=sessions, tokens=tokens,
            lifecycle=lifecycle, activities=activities,
        )

        self.assertEqual(metrics.session_ids, ("child", "root"))
        self.assertEqual(metrics.sessions.value, 2)
        self.assertEqual(metrics.subagents.value, 1)
        self.assertEqual(metrics.cycle_edges, 1)
        self.assertEqual(metrics.cutoff_at, at(10))
        self.assertEqual(metrics.recorded.vector, TokenVector(150, 35, 14, 7))
        self.assertEqual(metrics.replay_baseline.vector, TokenVector(30, 10, 2, 1))
        self.assertEqual(metrics.unique.vector, TokenVector(120, 25, 12, 6))
        self.assertEqual((metrics.unique.working_tokens, metrics.unique.full_context), (107, 132))
        self.assertEqual(metrics.unique.provenance, "derived")
        self.assertEqual(metrics.root_wall_clock_ms.value, 10_000)
        self.assertEqual(metrics.agent_time_ms.value, 16_000)

    def test_child_starting_one_hundred_nanoseconds_after_cutoff_is_excluded(self) -> None:
        cutoff = require_exact_timestamp("2026-07-21T00:00:09Z")
        after_cutoff = require_exact_timestamp(
            "2026-07-21T00:00:09.0000001Z",
        )

        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(
                NormalizedSession("root", None, at(0)),
                NormalizedSession(
                    "after-cutoff", "root", after_cutoff.presentation,
                    started_instant=after_cutoff,
                ),
            ),
            tokens=(
                TokenObservation(
                    "root", at(5), 1, TokenVector(100, 0, 0, 0),
                ),
            ),
            lifecycle=(
                LifecycleObservation(
                    "root", "task_complete", cutoff.presentation,
                    observed_instant=cutoff,
                ),
            ),
            activities=(),
        )

        self.assertEqual(metrics.session_ids, ("root",))
        self.assertEqual(metrics.sessions.value, 1)
        self.assertEqual(metrics.unique.working_tokens, 100)

    def test_unobserved_child_replay_is_zero_with_explicit_estimated_provenance(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(
                NormalizedSession("root", None, at(0)),
                NormalizedSession("child", "root", at(2)),
            ),
            tokens=(
                TokenObservation("root", at(9), 1, TokenVector(10, 2, 3, 1)),
                TokenObservation("child", at(8), 1, TokenVector(20, 5, 4, 2)),
            ),
            lifecycle=(LifecycleObservation("root", "task_complete", at(10)),),
            activities=(ActivityObservation("root", at(10)), ActivityObservation("child", at(8))),
        )

        self.assertEqual(metrics.replay_baseline.vector, TokenVector.zero())
        self.assertEqual(metrics.replay_baseline.provenance, "estimated")
        self.assertEqual(metrics.replay_baseline.caveats, ("zero_no_observation:1",))
        self.assertEqual(metrics.observed_replay_baselines, 0)
        self.assertEqual(metrics.zero_no_observation, 1)
        self.assertEqual(metrics.unique.provenance, "estimated")
        self.assertEqual(metrics.unique.caveats, ("zero_no_observation:1",))

    def test_token_observation_before_child_start_is_not_a_replay_baseline(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(
                NormalizedSession("root", None, at(0)),
                NormalizedSession("child", "root", at(2)),
            ),
            tokens=(
                TokenObservation("root", at(9), 1, TokenVector(10, 2, 3, 1)),
                TokenObservation("child", at(1), 1, TokenVector(70, 20, 10, 5)),
                TokenObservation("child", at(8), 2, TokenVector(20, 5, 4, 2)),
            ),
            lifecycle=(LifecycleObservation("root", "task_complete", at(10)),),
            activities=(ActivityObservation("root", at(10)), ActivityObservation("child", at(8))),
        )

        self.assertEqual(metrics.observed_replay_baselines, 0)
        self.assertEqual(metrics.zero_no_observation, 1)
        self.assertEqual(metrics.replay_baseline.vector, TokenVector.zero())
        self.assertEqual(metrics.recorded.vector, TokenVector(30, 7, 7, 3))

    def test_session_without_final_token_is_an_estimated_zero_lower_bound(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(
                NormalizedSession("root", None, at(0)),
                NormalizedSession("child", "root", at(2)),
            ),
            tokens=(TokenObservation("root", at(9), 1, TokenVector(10, 2, 3, 1)),),
            lifecycle=(LifecycleObservation("root", "task_complete", at(10)),),
            activities=(ActivityObservation("root", at(10)), ActivityObservation("child", at(8))),
        )

        self.assertEqual(metrics.recorded.provenance, "estimated")
        self.assertEqual(metrics.recorded.caveats, ("missing_final_token:1",))
        self.assertEqual(metrics.unique.provenance, "estimated")
        self.assertIn("missing_final_token:1", metrics.unique.caveats)

    def test_counter_reset_preserves_each_cumulative_epoch_and_reasoning_is_not_double_counted(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(NormalizedSession("root", None, at(0)),),
            tokens=(
                TokenObservation("root", at(2), 1, TokenVector(100, 60, 10, 8)),
                TokenObservation("root", at(3), 2, TokenVector(120, 70, 15, 9)),
                TokenObservation("root", at(4), 3, TokenVector(20, 5, 3, 2)),
                TokenObservation("root", at(5), 4, TokenVector(30, 8, 4, 3)),
            ),
            lifecycle=(LifecycleObservation("root", "task_complete", at(6)),),
            activities=(ActivityObservation("root", at(6)),),
        )

        self.assertEqual(metrics.recorded.vector, TokenVector(150, 78, 19, 12))
        self.assertEqual(metrics.unique.working_tokens, 91)
        self.assertEqual(metrics.unique.full_context, 169)
        self.assertEqual(metrics.unique.reasoning_output_tokens, 12)

    def test_missing_root_completion_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "root task_complete"):
            aggregate_task_tree(
                root_id="root",
                sessions=(NormalizedSession("root", None, at(0)),),
                tokens=(), lifecycle=(), activities=(),
            )

    def test_explicit_cutoff_supports_incomplete_task_without_fabricating_completion(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(
                NormalizedSession("root", None, at(0)),
                NormalizedSession("child", "root", at(2), edge_confidence_kind="inferred", edge_confidence=0.6),
                NormalizedSession("ambiguous", "root", at(1), edge_confidence_kind="ambiguous", edge_confidence=0.4),
            ),
            tokens=(
                TokenObservation("root", at(7), 1, TokenVector(10, 2, 3, 1)),
                TokenObservation("child", at(8), 1, TokenVector(20, 5, 4, 2)),
                TokenObservation("ambiguous", at(6), 1, TokenVector(99, 0, 1, 0)),
                TokenObservation("root", at(10), 2, TokenVector(30, 3, 5, 2)),
            ),
            lifecycle=(),
            activities=(ActivityObservation("root", at(8)),),
            cutoff_at=at(8),
            include_ambiguous_lineage=False,
        )

        self.assertEqual(metrics.cutoff_at, at(8))
        self.assertEqual(metrics.session_ids, ("child", "root"))
        self.assertEqual(metrics.recorded.vector, TokenVector(30, 7, 7, 3))

    def test_authoritative_abort_excludes_only_cross_source_timestampless_totals(self) -> None:
        session = (NormalizedSession("root", None, at(0)),)
        timestamped = TokenObservation(
            "root", at(5), 1, TokenVector(50, 10, 5, 2),
        )
        cross_source_app = TokenObservation(
            "root", None, 2, TokenVector(100, 20, 10, 5), 0,
            "estimated", "logical-app", 1, "exact",
            "app_total_timestamp_missing", True,
        )
        same_source_app = TokenObservation(
            "root", None, 2, TokenVector(100, 20, 10, 5), 0,
            "estimated", "logical-rollout", 2, "exact",
            "app_total_timestamp_missing", True,
        )
        abort = LifecycleObservation(
            "root", "turn_aborted", at(10), "logical-rollout", 3, "turn-a",
        )

        excluded = aggregate_task_tree(
            root_id="root", sessions=session,
            tokens=(timestamped, cross_source_app), lifecycle=(abort,),
            activities=(),
        )
        retained = aggregate_task_tree(
            root_id="root", sessions=session,
            tokens=(timestamped, same_source_app), lifecycle=(abort,),
            activities=(),
        )

        self.assertIsNone(excluded.recorded.input.value)
        self.assertEqual(excluded.recorded.input.known_lower_bound, 50)
        self.assertIn("ambiguous_timestamp_token:1", excluded.recorded.caveats)
        self.assertEqual(retained.recorded.input.value, 100)
        self.assertEqual(retained.recorded.working.value, 90)
        self.assertIn("timestamp_missing_token:1", retained.recorded.caveats)

    def test_non_abort_cutoffs_keep_cross_source_timestampless_app_total(self) -> None:
        session = (NormalizedSession("root", None, at(0)),)
        tokens = (
            TokenObservation("root", at(5), 1, TokenVector(50, 10, 5, 2)),
            TokenObservation(
                "root", None, 2, TokenVector(100, 20, 10, 5), 0,
                "estimated", "logical-app", 1, "exact",
                "app_total_timestamp_missing", True,
            ),
        )
        cases = (
            (
                "completed",
                (LifecycleObservation(
                    "root", "task_complete", at(10), "logical-rollout", 3,
                    "turn-a",
                ),),
                None,
            ),
            ("open", (), at(10)),
            (
                "reopened",
                (
                    LifecycleObservation(
                        "root", "turn_aborted", at(8), "logical-rollout", 3,
                        "turn-a",
                    ),
                    LifecycleObservation(
                        "root", "task_started", at(9), "logical-rollout", 4,
                        "turn-b",
                    ),
                ),
                at(10),
            ),
        )
        for name, lifecycle, cutoff in cases:
            with self.subTest(name=name):
                metrics = aggregate_task_tree(
                    root_id="root", sessions=session, tokens=tokens,
                    lifecycle=lifecycle, activities=(), cutoff_at=cutoff,
                )
                self.assertEqual(metrics.recorded.input.value, 100)
                self.assertEqual(metrics.recorded.working.value, 90)
                self.assertNotIn(
                    "ambiguous_timestamp_token:1", metrics.recorded.caveats,
                )

    def test_cached_input_cannot_exceed_total_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "cached_input_tokens"):
            TokenVector(5, 6, 1, 0)

    def test_missing_token_components_stay_nullable_with_per_field_lower_bounds(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(NormalizedSession("root", None, at(0)),),
            tokens=(TokenObservation("root", at(5), 1, TokenVector(100, None, 7, None)),),
            lifecycle=(LifecycleObservation("root", "task_complete", at(6)),),
            activities=(),
        )

        self.assertEqual(metrics.recorded.vector, TokenVector(100, None, 7, None))
        self.assertEqual(metrics.recorded.input.value, 100)
        self.assertEqual(metrics.recorded.input.provenance, "exact")
        self.assertIsNone(metrics.recorded.cached_input.value)
        self.assertEqual(metrics.recorded.cached_input.known_lower_bound, 0)
        self.assertEqual(metrics.recorded.cached_input.provenance, "estimated")
        self.assertIn("missing_cached_input_component", metrics.recorded.cached_input.caveats)
        self.assertIsNone(metrics.recorded.working.value)
        self.assertEqual(metrics.recorded.working.known_lower_bound, 7)
        self.assertIsNone(metrics.recorded.reasoning.value)

    def test_only_confirmed_full_confidence_edges_are_replay_eligible(self) -> None:
        for kind, confidence in (("inferred", 0.6), ("ambiguous", 0.4), ("confirmed", 0.9)):
            with self.subTest(kind=kind, confidence=confidence):
                metrics = aggregate_task_tree(
                    root_id="root",
                    sessions=(
                        NormalizedSession("root", None, at(0)),
                        NormalizedSession(
                            "child", "root", at(2),
                            edge_confidence_kind=kind, edge_confidence=confidence,
                        ),
                    ),
                    tokens=(
                        TokenObservation("root", at(9), 1, TokenVector(10, 2, 3, 1)),
                        TokenObservation("child", at(2), 1, TokenVector(30, 10, 2, 1)),
                        TokenObservation("child", at(8), 2, TokenVector(50, 15, 4, 2)),
                    ),
                    lifecycle=(LifecycleObservation("root", "task_complete", at(10)),),
                    activities=(),
                )

                self.assertEqual(metrics.replay_baseline.vector, TokenVector.zero())
                self.assertEqual(metrics.unique.vector, metrics.recorded.vector)
                self.assertEqual(metrics.unconfirmed_replay_edges, 1)
                self.assertEqual(metrics.unique.provenance, "estimated")
                self.assertIn(f"unconfirmed_replay_edge:{kind}:1", metrics.unique.caveats)

    def test_operational_facts_cover_tools_instrumentation_files_tests_and_retries(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(
                NormalizedSession("root", None, at(0)),
                NormalizedSession("child", "root", at(1)),
            ),
            tokens=(
                TokenObservation("root", at(8), 1, TokenVector(10, 2, 3, 1)),
                TokenObservation("child", at(7), 1, TokenVector(20, 5, 4, 2)),
            ),
            lifecycle=(LifecycleObservation("root", "task_complete", at(10)),),
            activities=(),
            tools=(
                ToolObservation("root", "call-1", "opaque_exec", at(2)),
                ToolObservation("child", "call-2", "instrumentation", at(3)),
                ToolObservation("root", "after", "instrumentation", at(11)),
            ),
            files=(
                FileObservation("root", "read-1", "read", at(4)),
                FileObservation("child", "write-1", "write", at(5)),
                FileObservation("root", "after", "write", at(11)),
            ),
            tests=(
                TestRunObservation("root", "test-1", "targeted", "none", at(6)),
                TestRunObservation("child", "test-2", "full", "flaky_retry", at(7)),
                TestRunObservation("root", "after", "full", "infra_recovery", at(11)),
            ),
        )

        self.assertEqual(metrics.tool_calls.value, 2)
        self.assertEqual(metrics.instrumentation_calls.value, 1)
        self.assertEqual(metrics.file_reads.value, None)
        self.assertEqual(metrics.file_reads.known_lower_bound, 1)
        self.assertEqual(metrics.file_writes.known_lower_bound, 1)
        self.assertEqual(metrics.test_runs.value, 2)
        self.assertEqual(metrics.targeted_test_runs.value, 1)
        self.assertEqual(metrics.full_test_runs.value, 1)
        self.assertEqual(metrics.test_retries.value, 1)
        self.assertEqual(metrics.file_reads.provenance, "estimated")
        self.assertIn("observed_file_lower_bound", metrics.file_reads.caveats)

    def test_timestampless_operational_events_are_not_overclaimed_as_exact_counts(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(NormalizedSession("root", None, at(0)),),
            tokens=(TokenObservation("root", at(5), 1, TokenVector(10, 2, 3, 1)),),
            lifecycle=(LifecycleObservation("root", "task_complete", at(10)),),
            activities=(),
            tools=(
                ToolObservation("root", "known", "tool", at(4)),
                ToolObservation("root", "unknown", "instrumentation", None),
            ),
            tests=(
                TestRunObservation("root", "known", "targeted", "none", at(6)),
                TestRunObservation("root", "unknown", "full", "infra_recovery", None),
            ),
        )

        self.assertIsNone(metrics.tool_calls.value)
        self.assertEqual(metrics.tool_calls.known_lower_bound, 1)
        self.assertIn("timestamp_missing_tool:1", metrics.tool_calls.caveats)
        self.assertIsNone(metrics.test_runs.value)
        self.assertEqual(metrics.test_runs.known_lower_bound, 1)
        self.assertIn("timestamp_missing_test:1", metrics.test_runs.caveats)

    def test_operational_events_before_session_start_are_excluded(self) -> None:
        metrics = aggregate_task_tree(
            root_id="root",
            sessions=(NormalizedSession("root", None, at(2)),),
            tokens=(TokenObservation("root", at(5), 1, TokenVector(10, 2, 3, 1)),),
            lifecycle=(LifecycleObservation("root", "task_complete", at(10)),),
            activities=(),
            tools=(
                ToolObservation("root", "before", "tool", at(1)),
                ToolObservation("root", "valid", "tool", at(3)),
            ),
            files=(
                FileObservation("root", "before", "read", at(1)),
                FileObservation("root", "valid", "read", at(3)),
            ),
            tests=(
                TestRunObservation("root", "before", "full", "none", at(1)),
                TestRunObservation("root", "valid", "full", "none", at(3)),
            ),
        )

        self.assertEqual(metrics.tool_calls.value, 1)
        self.assertEqual(metrics.file_reads.known_lower_bound, 1)
        self.assertEqual(metrics.test_runs.value, 1)
        self.assertEqual(metrics.full_test_runs.value, 1)

    def test_exposed_task_facts_reject_invalid_provenance_and_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "provenance"):
            ScalarFact(1, "unsafe")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ScalarFact(-1, "exact")
        with self.assertRaisesRegex(ValueError, "lower bound"):
            ScalarFact(1, "exact", known_lower_bound=2)
        exact = ScalarFact(0, "exact")
        with self.assertRaisesRegex(ValueError, "provenance"):
            TokenVectorFact(
                TokenVector.zero(), exact, exact, exact, exact, exact, exact,
                "unsafe",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
