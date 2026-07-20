from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from hydra_codex.metrics import (
    MetricFact,
    ProjectMetrics,
    TokenTotals,
    TreeContribution,
    TurnTotals,
    aggregate_project,
    aggregate_project_facts,
    tree_contribution,
)
from hydra_codex.storage import HydraStore


class MetricFactValidationTests(unittest.TestCase):
    def test_project_metrics_are_adapted_from_component_aware_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            self.addCleanup(store.close)
            store.connection.execute(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,conversation_key)
                   VALUES ('root','project','worktree',1,'')"""
            )
            store.connection.execute(
                """INSERT INTO token_snapshots(
                       source_digest,line_number,session_key,project_id,epoch,input_tokens,
                       cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,completeness)
                   VALUES ('source',1,'root','project',0,10,2,3,NULL,0,'partial')"""
            )
            store.connection.commit()

            facts = aggregate_project_facts(store.connection, "project")
            metrics = aggregate_project(store.connection, "project")

            self.assertEqual(metrics.working_tokens, facts["deduplicated_working"].value)
            self.assertEqual(metrics.full_context, facts["deduplicated_full"].value)
            self.assertEqual(metrics.reasoning_tokens, facts["deduplicated_reasoning"].value)
            self.assertEqual((metrics.working_tokens, metrics.full_context), (11, 13))
            self.assertIsNone(metrics.reasoning_tokens)
            self.assertEqual(metrics.provenance, "estimated")

    def test_corrupt_baseline_cannot_create_negative_metric_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            self.addCleanup(store.close)
            store.connection.execute(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,conversation_key)
                   VALUES ('child','project','worktree',1,'')"""
            )
            store.connection.execute(
                """INSERT INTO session_edges(
                       child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
                   VALUES ('child','parent',NULL,'confirmed',1.0)"""
            )
            store.connection.execute(
                """INSERT INTO token_snapshots(
                       source_digest,line_number,session_key,project_id,epoch,input_tokens,
                       cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,completeness)
                   VALUES ('source',1,'child','project',0,10,2,3,1,0,'complete')"""
            )
            store.connection.execute(
                """INSERT INTO fork_baselines(
                       child_key,source_digest,line_number,input_tokens,cached_input_tokens,
                       output_tokens,reasoning_tokens,cache_write_tokens,provenance)
                   VALUES ('child','source',1,100,0,0,0,0,'exact')"""
            )
            store.connection.commit()

            facts = aggregate_project_facts(store.connection, "project")

            self.assertIsNone(facts["deduplicated_working"].value)
            self.assertEqual(facts["deduplicated_working"].known_lower_bound, 0)
            self.assertEqual(facts["deduplicated_working"].provenance, "estimated")
            self.assertIn("baseline_exceeds_recorded", facts["deduplicated_working"].caveats)

    def test_all_exposed_metric_facts_validate_provenance_and_nonnegative_values(self) -> None:
        invalid_provenance = (
            lambda: MetricFact(1, 0, "unsafe"),
            lambda: TokenTotals(1, 1, 1, 1, "unsafe"),
            lambda: TreeContribution(1, 1, "unsafe", 1.0),
            lambda: ProjectMetrics(1, 1, 1, 1, 1, 1, 1, 0.0, "unsafe"),
            lambda: TurnTotals(1, 1, "unsafe"),
        )
        for factory in invalid_provenance:
            with self.subTest(factory=factory), self.assertRaisesRegex(ValueError, "provenance"):
                factory()
        with self.assertRaisesRegex(ValueError, "non-negative"):
            MetricFact(-1, 0, "exact")
        with self.assertRaisesRegex(ValueError, "lower bound"):
            MetricFact(1, 2, "exact")
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ProjectMetrics(-1, 1, 1, 1, 1, 1, 1, 0.0)

    def test_legacy_tree_contribution_requires_full_confidence(self) -> None:
        from hydra_codex.metrics import SessionEdge

        contribution = tree_contribution(
            TokenTotals(100, 120, 5, 1, "exact"),
            SessionEdge("child", "root", 20, "confirmed", 0.9),
        )

        self.assertIsNone(contribution.working_tokens)
        self.assertEqual(contribution.provenance, "estimated")


if __name__ == "__main__":
    unittest.main()
