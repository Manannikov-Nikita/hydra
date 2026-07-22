from __future__ import annotations

from dataclasses import replace
import json
import unittest

from hydra_codex.report_renderers import render_json, render_markdown
from hydra_codex.reporting import (
    NumericFact,
    compare_reports,
    project_public_references,
    report_from_task_tree,
)
from tests.test_reporting import simple_metrics


COMPARISON_SCHEMA = "hydra.comparison/v2"


def report(root: str, tokens: int, *, family: str | None = "quiz", complete: bool = True):
    public_ref = project_public_references((root,), b"v" * 32)[root]
    token_options = {"cached": 0, "output": 0} if tokens == 0 else {}
    return report_from_task_tree(
        simple_metrics(root, tokens, **token_options),
        public_ref=public_ref,
        complete=complete,
        task_family=family,
    )


def verified(value):
    exact_count = NumericFact(0, "count", "derived")
    trend = replace(
        value.trend_input,
        working_tokens=value.deduplicated_tokens.working,
        test_retries=exact_count,
        read_amplification=exact_count,
        review_fix_cycles=exact_count,
        compactions=NumericFact(0, "count", "exact"),
    )
    pilot = replace(
        value.pilot_health,
        status="verified",
        receipt_verified=True,
        caveats=(),
    )
    return replace(value, trend_input=trend, pilot_health=pilot)


class ComparisonV2Tests(unittest.TestCase):
    def test_verified_complete_same_family_pair_is_comparable(self) -> None:
        baseline = verified(report("baseline", 100))
        current = verified(report("current", 125))

        comparison = compare_reports(baseline, current)
        payload = json.loads(render_json(comparison))

        self.assertEqual(comparison.schema_version, COMPARISON_SCHEMA)
        self.assertEqual(comparison.verdict, "comparable")
        self.assertEqual(comparison.reasons, ())
        self.assertEqual(payload["verdict"], "comparable")
        self.assertEqual(payload["reasons"], [])
        self.assertEqual(
            comparison.metrics["deduplicated_working_tokens"].delta.value,
            25,
        )
        self.assertIn("Raw percent change", render_markdown(comparison))
        self.assertNotIn("improvement", render_markdown(comparison).lower())

    def test_same_family_without_verified_receipt_is_partial(self) -> None:
        comparison = compare_reports(report("left", 100), report("right", 120))

        self.assertEqual(comparison.verdict, "partial")
        self.assertIn("pilot_receipt_unverified", comparison.reasons)
        self.assertIn("evidence_partial", comparison.reasons)
        self.assertIn("instrumentation_not_subtracted", comparison.caveats)
        self.assertEqual(
            set(comparison.metrics),
            set(report("left-again", 100).public_facts()),
        )

    def test_different_family_and_scope_exclusion_are_not_comparable(self) -> None:
        mismatch = compare_reports(
            verified(report("quiz", 100, family="quiz")),
            verified(report("essay", 100, family="essay")),
        )
        self.assertEqual(mismatch.verdict, "not_comparable")
        self.assertEqual(mismatch.reasons, ("task_family_mismatch",))

        current = verified(report("scoped", 100, family="quiz"))
        current = replace(
            current,
            trend_input=replace(current.trend_input, task_family=None),
        )
        excluded = compare_reports(verified(report("base", 100)), current)
        self.assertEqual(excluded.verdict, "not_comparable")
        self.assertEqual(excluded.reasons, ("automatic_comparison_excluded",))

    def test_missing_family_or_incomplete_task_is_unknown(self) -> None:
        missing = compare_reports(
            report("missing", 100, family=None),
            report("known", 100),
        )
        self.assertEqual(missing.verdict, "unknown")
        self.assertIn("task_family_unavailable", missing.reasons)

        incomplete = compare_reports(
            report("complete", 100),
            report("incomplete", 100, complete=False),
        )
        self.assertEqual(incomplete.verdict, "unknown")
        self.assertIn("task_incomplete", incomplete.reasons)

    def test_unavailable_metrics_remain_raw_evidence_without_false_claim(self) -> None:
        comparison = compare_reports(report("zero", 0), report("value", 100))
        payload = comparison.as_dict()

        self.assertEqual(comparison.verdict, "partial")
        self.assertIsNone(
            comparison.metrics["instrumentation_overhead_tokens"].delta.value,
        )
        self.assertIsNone(
            comparison.metrics["deduplicated_working_tokens"].percent_change.value,
        )
        self.assertNotIn("improved", json.dumps(payload).lower())
        self.assertNotIn("reduced", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
