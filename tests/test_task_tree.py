from __future__ import annotations

from datetime import datetime, timezone
import unittest

from hydra_codex.task_tree import (
    ActivityObservation,
    LifecycleObservation,
    NormalizedSession,
    TokenObservation,
    TokenVector,
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

    def test_cached_input_cannot_exceed_total_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "cached_input_tokens"):
            TokenVector(5, 6, 1, 0)


if __name__ == "__main__":
    unittest.main()
