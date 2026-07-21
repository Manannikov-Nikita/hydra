from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from hydra_codex import report_operations
from hydra_codex.report_renderers import (
    render_html,
    render_json,
    render_markdown,
    render_report_collection,
)
from hydra_codex.reporting import (
    REPORT_SCHEMA,
    NumericFact,
    build_trend_window,
    compare_reports,
    project_public_references,
    report_from_task_tree,
)
from hydra_codex.rollout import Pseudonymizer, ingest_rollouts
from hydra_codex.storage import HydraStore
from hydra_codex.task_tree import (
    ActivityObservation,
    LifecycleObservation,
    NormalizedSession,
    TokenObservation,
    TokenVector,
    aggregate_task_tree,
)
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


FIXTURES = Path(__file__).parent / "fixtures" / "historical"


def at(second: int) -> datetime:
    return datetime(2026, 7, 21, 0, 0, second, tzinfo=timezone.utc)


def simple_metrics(
    root: str,
    working_input: int = 100,
    *,
    cached: int = 20,
    output: int = 10,
):
    return aggregate_task_tree(
        root_id=root,
        sessions=(NormalizedSession(root, None, at(0)),),
        tokens=(TokenObservation(root, at(5), 1, TokenVector(working_input, cached, output, 5)),),
        lifecycle=(LifecycleObservation(root, "task_complete", at(10)),),
        activities=(ActivityObservation(root, at(8)),),
    )


def materialize_historical(source: Path, destination: Path, project: Path) -> None:
    destination.mkdir(parents=True)
    for path in sorted(source.glob("*.jsonl")):
        (destination / path.name).write_text(
            path.read_text(encoding="utf-8").replace("__PROJECT_ROOT__", str(project)),
            encoding="utf-8",
        )


class PublicReportContractTests(unittest.TestCase):
    def test_numeric_fact_rejects_nan_and_unavailable_non_estimated_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            NumericFact(math.nan, "tokens", "exact")
        with self.assertRaisesRegex(ValueError, "estimated"):
            NumericFact(None, "tokens", "exact")
        with self.assertRaisesRegex(ValueError, "lower bound"):
            NumericFact(1, "tokens", "exact", lower_bound=2)
        with self.assertRaisesRegex(ValueError, "privacy-safe codes"):
            NumericFact(1, "count", "exact", ("note:/srv/acme/private",))

    def test_public_reference_projection_is_order_independent_and_expands_collisions(self) -> None:
        opaque_ids = tuple(f"opaque-root-{index}" for index in range(64))

        forward = project_public_references(opaque_ids, b"k" * 32, minimum_length=1)
        reverse = project_public_references(reversed(opaque_ids), b"k" * 32, minimum_length=1)

        self.assertEqual(forward, reverse)
        self.assertEqual(len(set(forward.public_references)), len(opaque_ids))
        self.assertTrue(all(value.startswith("task_") for value in forward.public_references))
        self.assertTrue(any(len(value) > len("task_") + 1 for value in forward.public_references))
        self.assertFalse(set(opaque_ids) & set(forward.public_references))
        self.assertNotIn(opaque_ids[0], repr(forward))
        with self.assertRaisesRegex(KeyError, "unknown opaque identifier") as error:
            forward["private-missing-root"]
        self.assertNotIn("private-missing-root", str(error.exception))
        with self.assertRaisesRegex(ValueError, "non-empty text"):
            project_public_references(("valid", 123), b"k" * 32)  # type: ignore[arg-type]

    def test_task_tree_adapter_exposes_only_versioned_public_facts(self) -> None:
        raw_root = "raw-thread-019f-secret"
        public_ref = project_public_references((raw_root,), b"p" * 32)[raw_root]

        report = report_from_task_tree(
            simple_metrics(raw_root), public_ref=public_ref, task_family="quiz",
        )
        payload = json.loads(render_json(report))

        self.assertEqual(REPORT_SCHEMA, "hydra.report/v3")
        self.assertEqual(payload["schema_version"], REPORT_SCHEMA)
        self.assertEqual(payload["task_ref"], public_ref)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(report.recorded_tokens.working.value, 90)
        self.assertEqual(report.deduplicated_tokens.working.value, 90)
        self.assertEqual(report.wall_clock.value, 10_000)
        self.assertEqual(report.agent_time.value, 10_000)
        self.assertEqual(report.instrumentation_overhead.value, None)
        self.assertEqual(report.instrumentation_overhead.provenance, "estimated")
        self.assertIsNone(
            payload["semantic"]["breakdown"]["phases"]["implement"]["working"]["value"]
        )
        self.assertIn(
            "semantic_breakdown_unavailable",
            report.public_facts()["semantic.phase.implement.working"].caveats,
        )
        self.assertNotIn(raw_root, render_json(report))
        self.assertNotIn("root_id", render_json(report))
        self.assertNotIn("session_ids", render_json(report))
        for value in payload["recorded_tokens"].values():
            self.assertEqual(
                set(value), {"caveats", "lower_bound", "provenance", "unit", "value"},
            )

    def test_incomplete_report_keeps_unavailable_facts_and_caveats(self) -> None:
        metrics = simple_metrics("root")
        report = report_from_task_tree(
            metrics, public_ref=project_public_references(("root",), b"i" * 32)["root"],
            complete=False,
        )

        self.assertEqual(report.status, "incomplete")
        self.assertIsNone(report.semantic_conflicts.value)
        self.assertIn("semantic_conflicts_unavailable", report.semantic_conflicts.caveats)
        self.assertIsNone(report.schema_diagnostics.value)
        self.assertIsNone(report.instrumentation_overhead.value)
        self.assertEqual(report.pilot_health.task_count.value, 0)
        self.assertEqual(report.pilot_health.missing_marker_rate.value, 0.0)
        self.assertEqual(report.pilot_health.status, "not_started")

    def test_adapter_rejects_wrong_public_metric_units(self) -> None:
        with self.assertRaisesRegex(ValueError, "semantic_conflicts must be a count"):
            report_from_task_tree(
                simple_metrics("root"),
                public_ref=project_public_references(("root",), b"u" * 32)["root"],
                semantic_conflicts=NumericFact(1, "milliseconds", "exact"),
            )

    def test_historical_adapter_preserves_known_newer_totals_without_raw_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            (project / ".hydra").mkdir(parents=True)
            project_id = "historical-report"
            (project / ".hydra" / "project.toml").write_text(
                f'project_id = "{project_id}"\n', encoding="utf-8",
            )
            rollouts = base / "rollouts"
            materialize_historical(FIXTURES / "newer", rollouts, project)
            manifest = json.loads((FIXTURES / "newer-manifest.json").read_text(encoding="utf-8"))
            store = HydraStore(base / "hydra.sqlite3")
            self.addCleanup(store.close)
            ingest_rollouts(store, (rollouts,), project, project_id, hash_key=b"h" * 32)
            root_key = Pseudonymizer(b"h" * 32).digest("identity", manifest["root"])
            metrics = aggregate_stored_task_tree(
                store.connection, project_id=project_id, root_id=root_key,
            )
            public_ref = project_public_references((root_key,), b"r" * 32)[root_key]

            report = report_from_task_tree(metrics, public_ref=public_ref, task_family="quiz")

            self.assertEqual(report.deduplicated_tokens.working.value, 5_018_356)
            self.assertEqual(report.deduplicated_tokens.full_context.value, 171_325_172)
            self.assertEqual(report.sessions.value, 25)
            self.assertEqual(report.semantic_coverage.value, 0)
            self.assertIn("zero_no_observation:20", report.deduplicated_tokens.working.caveats)
            self.assertNotIn(manifest["root"], render_json(report))


class CompareAndRendererTests(unittest.TestCase):
    def report(
        self,
        root: str,
        input_tokens: int,
        *,
        cached: int = 0,
        output: int = 0,
        complete: bool = True,
        task_family: str = "quiz",
    ):
        public_ref = project_public_references((root,), b"c" * 32)[root]
        return report_from_task_tree(
            simple_metrics(root, input_tokens, cached=cached, output=output),
            public_ref=public_ref,
            complete=complete,
            task_family=task_family,
        )

    def test_compare_has_deterministic_delta_and_zero_base_percentage(self) -> None:
        zero = self.report("zero", 0)
        current = self.report("current", 100)

        comparison = compare_reports(zero, current)
        fact = comparison.metrics["deduplicated_working_tokens"]

        self.assertEqual(comparison.schema_version, REPORT_SCHEMA)
        self.assertEqual(fact.delta.value, 100)
        self.assertIsNone(fact.percent_change.value)
        self.assertIn("zero_baseline_percentage_unavailable", fact.percent_change.caveats)
        self.assertIn("instrumentation_not_subtracted", comparison.caveats)
        self.assertIsNone(comparison.metrics["instrumentation_overhead_tokens"].delta.value)
        self.assertEqual(render_json(comparison), render_json(comparison))

    def test_compare_propagates_unavailable_metrics_without_nan(self) -> None:
        comparison = compare_reports(self.report("a", 10), self.report("b", 20))

        overhead = comparison.metrics["instrumentation_overhead_tokens"]
        payload = render_json(comparison)

        self.assertIsNone(overhead.delta.value)
        self.assertIn("comparison_value_unavailable", overhead.delta.caveats)
        self.assertNotIn("NaN", payload)
        self.assertNotIn("Infinity", payload)

    def test_markdown_and_standalone_html_escape_all_dynamic_text(self) -> None:
        report = self.report("escape", 10, task_family="quiz_family")
        malicious = self.report(
            "malicious", 10, task_family="[click](https://example.invalid) `code`",
        )

        markdown = render_markdown(report)
        html = render_html(report)

        self.assertIn(r"quiz\_family", markdown)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("<style>", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("file://", html)
        self.assertIsNone(malicious.task_family)
        self.assertNotIn("[click]", render_markdown(malicious))
        self.assertNotIn("example.invalid", render_html(malicious))

    def test_rendered_payload_has_no_private_field_names(self) -> None:
        report = self.report("private-root", 10)
        comparison = compare_reports(report, self.report("other-root", 20))
        forbidden = (
            "root_id", "session_ids", "absolute_path", "message", "observed_at", "note_hash",
        )

        for artifact in (
            render_json(report), render_markdown(report), render_html(report),
            render_json(comparison), render_markdown(comparison), render_html(comparison),
        ):
            with self.subTest(prefix=artifact[:20]):
                for private_name in forbidden:
                    self.assertNotIn(private_name, artifact)
                self.assertNotIn("private-root", artifact)
                self.assertNotIn("other-root", artifact)

    def test_sensitive_task_family_is_unavailable_and_cannot_form_false_cohort(self) -> None:
        for private_family in (
            "/workspace/acme/private", r"C:\Users\private\project",
            "019f75d4-5125-7343-8537-49b80f27f286", "raw note with spaces",
        ):
            with self.subTest(private_family=private_family):
                report = self.report("safe-root", 10, task_family=private_family)
                self.assertIsNone(report.task_family)
                self.assertIsNone(report.trend_input.task_family)
                for artifact in (render_json(report), render_markdown(report), render_html(report)):
                    self.assertNotIn(private_family, artifact)

        reports = tuple(
            self.report(
                f"private-{index}", 200 if index == 4 else 100,
                task_family=f"/workspace/private-family-{index % 2}",
            )
            for index in range(5)
        )
        evaluated = report_operations.evaluate_report_trends(reports)
        self.assertFalse(any(item.trend_result.warning for item in evaluated))

    def test_all_formats_and_comparison_share_pilot_and_trend_facts(self) -> None:
        baseline = self.report("baseline", 10)
        current = self.report("current", 20)
        comparison = compare_reports(baseline, current)

        for name in ("pilot_health.missing_marker_rate", "trend.review_fix_cycles"):
            self.assertIn(name, baseline.public_facts())
            self.assertIn(name, comparison.metrics)
            self.assertIn(name.replace("_", r"\_").replace(".", r"\."), render_markdown(baseline))
            self.assertIn(name, render_html(baseline))
        payload = json.loads(render_json(baseline))
        self.assertIn("missing_marker_rate", payload["pilot_health"])
        self.assertIn("review_fix_cycles", payload["trend"]["input"])

    def test_trend_window_selects_current_and_four_prior_completed_family_matches(self) -> None:
        current = replace(
            self.report("current", 200),
            last_activity_at="2026-07-22T00:00:00Z",
        )
        history = [
            self.report(f"quiz-{index}", 100 + index, complete=index != 1)
            for index in range(7)
        ]
        history.append(self.report("essay", 999, task_family="essay"))

        window = build_trend_window(current, reversed(history))
        forward = build_trend_window(current, history)
        eligible = sorted(
            (
                item for item in history
                if item.completed and item.task_family == current.task_family
            ),
            key=lambda item: (item.last_activity_at, item.task_ref),
        )[-4:]

        self.assertEqual(window, forward)
        self.assertEqual(window.current.task_ref, current.task_ref)
        self.assertEqual(len(window.prior), 4)
        self.assertTrue(all(item.completed for item in window.prior))
        self.assertTrue(all(item.task_family == "quiz" for item in window.prior))
        self.assertEqual(
            tuple(item.task_ref for item in window.prior),
            tuple(item.task_ref for item in eligible),
        )

    def test_trend_window_uses_instants_deduplicates_and_rejects_incomplete_current(self) -> None:
        current = replace(
            self.report("current", 200),
            last_activity_at="2026-07-21T10:00:00Z",
        )
        earlier = replace(self.report("earlier", 100), last_activity_at="2026-07-21T10:30:00+02:00")
        later = replace(self.report("later", 110), last_activity_at="2026-07-21T09:00:00Z")
        duplicate_later = replace(later, last_activity_at="2026-07-21T09:30:00Z")

        window = build_trend_window(current, (later, earlier, duplicate_later))
        incomplete = build_trend_window(self.report("incomplete", 200, complete=False), (earlier, later))

        self.assertEqual(len(window.prior), 2)
        self.assertEqual(tuple(item.task_ref for item in window.prior), (
            earlier.task_ref, later.task_ref,
        ))
        self.assertEqual(incomplete.prior, ())
        self.assertEqual(len(window.as_dict()["prior"]), 2)

    def test_report_operations_can_be_imported_without_a_circular_import(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'src'); "
                "from hydra_codex.report_operations import compare_reports, report_from_task_tree",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recent_report_collection_is_versioned_deterministic_and_safe_in_all_formats(self) -> None:
        reports = (self.report("latest-private-root", 20), self.report("older-private-root", 10))

        rendered = {
            output_format: render_report_collection(reports, output_format)
            for output_format in ("json", "markdown", "html")
        }

        payload = json.loads(rendered["json"])
        self.assertEqual(payload["schema_version"], "hydra.report-list/v1")
        self.assertEqual(
            [item["task_ref"] for item in payload["reports"]],
            [item.task_ref for item in reports],
        )
        self.assertEqual(rendered["html"].count("<!doctype html>"), 1)
        self.assertIn("Hydra task reports", rendered["markdown"])
        for artifact in rendered.values():
            self.assertNotIn("latest-private-root", artifact)
            self.assertNotIn("older-private-root", artifact)

    def test_empty_report_collection_is_valid_in_all_formats(self) -> None:
        self.assertEqual(
            json.loads(render_report_collection((), "json")),
            {"reports": [], "schema_version": "hydra.report-list/v1"},
        )
        self.assertIn("No reconciled tasks", render_report_collection((), "markdown"))
        self.assertIn("0 reconciled tasks", render_report_collection((), "html"))
        with self.assertRaisesRegex(ValueError, "format"):
            render_report_collection((), "xml")


if __name__ == "__main__":
    unittest.main()
