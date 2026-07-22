from __future__ import annotations

from dataclasses import replace
import json
import unittest

from hydra_codex.audit_builder import build_audit
from hydra_codex.audit_model import AuditReport
from hydra_codex.audit_renderers import (
    render_audit_html,
    render_audit_json,
    render_audit_markdown,
)
from hydra_codex.report_semantics import (
    SemanticMarkerSummary,
    TestEvidenceRow,
    TestEvidenceSummary,
)
from hydra_codex.reporting import NumericFact
from hydra_codex.pilot import PilotStatus
from tests.test_audit_builder import pilot_snapshot, public_report, storage


def sample_audit():
    reports = (
        public_report("render-a", input_tokens=120, second=10),
        public_report("render-b", input_tokens=220, second=20),
    )
    return build_audit(pilot_snapshot(reports), reports, storage())


def structured_audit():
    report = public_report("structured", input_tokens=120, second=10)
    breakdown = report.semantic_breakdown
    annotations = replace(
        breakdown.annotations,
        total_count=NumericFact(1, "count", "model_reported"),
        test_evidence=TestEvidenceSummary(
            NumericFact(1, "count", "derived", lower_bound=1),
            (
                TestEvidenceRow(
                    "targeted",
                    "product_failure",
                    "product_fix_verification",
                    "test_full",
                    "final_verification",
                    NumericFact(1, "count", "derived", lower_bound=1),
                ),
            ),
        ),
        timeline=(
            SemanticMarkerSummary(
                "blocker",
                "fix",
                "review_finding",
                "none",
                None,
                0.75,
                "[redacted]",
            ),
        ),
        truncated_count=NumericFact(0, "count", "derived"),
    )
    report = replace(
        report,
        semantic_breakdown=replace(
            breakdown,
            marker_count=NumericFact(1, "count", "derived"),
            annotations=annotations,
        ),
    )
    snapshot = pilot_snapshot((report,)).as_dict()
    snapshot["tasks"][0]["instrumented"] = True
    snapshot["facts"]["instrumented_tasks"] = 1
    snapshot["facts"]["enrollment"] = 1.0
    return build_audit(PilotStatus(snapshot), (report,), storage())


class AuditRendererTests(unittest.TestCase):
    def test_json_is_canonical_deterministic_and_close_ready(self) -> None:
        audit = sample_audit()

        first = render_audit_json(audit)
        second = render_audit_json(audit)
        payload = json.loads(first)

        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        self.assertEqual(
            first,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ) + "\n",
        )
        self.assertEqual(payload["pilot_snapshot"], pilot_snapshot((
            public_report("render-a", input_tokens=120, second=10),
            public_report("render-b", input_tokens=220, second=20),
        )).as_dict())

    def test_markdown_renders_each_complete_evidence_record_once(self) -> None:
        audit = sample_audit()

        markdown = render_audit_markdown(audit)

        self.assertIn("# Hydra pilot audit", markdown)
        self.assertIn("## Comparability readiness", markdown)
        self.assertIn("## Task collection", markdown)
        self.assertIn("## Evidence appendix", markdown)
        for item in audit.evidence_appendix:
            marker = f"<!-- evidence-record:{item.evidence_id} -->"
            self.assertEqual(markdown.count(marker), 1)
        self.assertIn("unavailable", markdown.lower())
        self.assertIn(r"host\_context\_unavailable", markdown)

    def test_html_is_self_contained_responsive_dark_print_safe_and_motion_free(self) -> None:
        audit = sample_audit()

        rendered = render_audit_html(audit)
        lowered = rendered.lower()

        self.assertTrue(rendered.startswith("<!doctype html>"))
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            rendered,
        )
        self.assertIn("font-family: system-ui", rendered)
        self.assertIn("color-scheme: light dark", rendered)
        self.assertIn("@media (prefers-color-scheme: dark)", rendered)
        self.assertIn("@media (max-width: 640px)", rendered)
        self.assertIn("@media print", rendered)
        self.assertIn("overflow-wrap: anywhere", rendered)
        self.assertIn(".task-columns > * { min-width: 0; }", rendered)
        self.assertIn("<main", rendered)
        self.assertIn("Evidence appendix", rendered)
        self.assertNotIn("<script", lowered)
        self.assertNotRegex(lowered, r"https?://|//cdn|@import|url\(")
        for forbidden in (
            "linear-gradient",
            "radial-gradient",
            "backdrop-filter",
            "box-shadow",
            "animation:",
            "transition:",
        ):
            self.assertNotIn(forbidden, lowered)
        for item in audit.evidence_appendix:
            marker = f'data-evidence-record="{item.evidence_id}"'
            self.assertEqual(rendered.count(marker), 1)

    def test_human_formats_render_complete_structured_task_evidence(self) -> None:
        original = structured_audit()
        payload = original.as_dict()
        hostile = "<marker>&"
        payload["collection"]["tasks"][0]["semantic_markers"][0]["kind"] = hostile
        audit = AuditReport.create(
            pilot_snapshot=payload["pilot_snapshot"],
            cohort=payload["cohort"],
            collection=payload["collection"],
            storage_health=payload["storage_health"],
            evidence_appendix=original.evidence_appendix,
        )

        markdown = render_audit_markdown(audit)
        html = render_audit_html(audit)

        for label in (
            "Per-task phase allocation",
            "Tool, file, and test evidence",
            "Deterministic test evidence",
            "Issue and marker counts",
            "Semantic marker timeline",
            "Comparability",
            "Baseline working tokens",
        ):
            with self.subTest(label=label):
                self.assertIn(label, markdown)
                self.assertIn(label, html)
        for markdown_label, html_label in (
            (r"product\_failure", "product_failure"),
            (r"product\_fix\_verification", "product_fix_verification"),
            (r"\[redacted\]", "[redacted]"),
        ):
            self.assertIn(markdown_label, markdown)
            self.assertIn(html_label, html)
        self.assertNotIn(hostile, html)
        self.assertNotIn(hostile, markdown)
        self.assertIn("&lt;marker&gt;&amp;", html)
        self.assertIn("&lt;marker&gt;&amp;", markdown)

    def test_print_css_keeps_appendix_records_intact(self) -> None:
        rendered = render_audit_html(sample_audit())

        for contract in (
            "thead { display: table-header-group; }",
            "thead { break-inside: avoid; page-break-inside: avoid; }",
            "tbody { break-inside: auto; }",
            "tr { break-inside: avoid; page-break-inside: avoid; }",
            "tr { break-after: auto; page-break-after: auto; }",
            ".report-section { break-inside: auto; }",
            ".table-wrap { overflow: visible; }",
            "break-inside: avoid-page; page-break-inside: avoid;",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, rendered)
        self.assertNotIn(
            ".task, .report-section, tr { break-inside: avoid; }",
            rendered,
        )

    def test_html_labels_every_appendix_record_inline(self) -> None:
        audit = sample_audit()

        rendered = render_audit_html(audit)
        count = len(audit.evidence_appendix)

        self.assertEqual(rendered.count('<dl class="appendix-record"'), count)
        for label in (
            "Evidence",
            "Fact",
            "Value",
            "Unit",
            "Provenance",
            "Lower bound",
            "Caveats",
        ):
            with self.subTest(label=label):
                self.assertEqual(rendered.count(f"<dt>{label}</dt>"), count)
        self.assertNotIn("appendix-chunk", rendered)
        self.assertNotIn("appendix-grid", rendered)
        self.assertNotIn('class="appendix-table"', rendered)

    def test_html_and_markdown_escape_hostile_dynamic_text(self) -> None:
        original = sample_audit()
        payload = original.as_dict()
        hostile = '</style><script>alert("x")</script>|[link](https://invalid.example)'
        payload["cohort"]["task_family"] = hostile
        audit = AuditReport.create(
            pilot_snapshot=payload["pilot_snapshot"],
            cohort=payload["cohort"],
            collection=payload["collection"],
            storage_health=payload["storage_health"],
            evidence_appendix=original.evidence_appendix,
        )

        html = render_audit_html(audit)
        markdown = render_audit_markdown(audit)

        self.assertNotIn(hostile, html)
        self.assertNotIn("<script", html.lower())
        self.assertIn("&lt;/style&gt;&lt;script&gt;", html)
        self.assertNotIn("[link](https://invalid.example)", markdown)
        self.assertIn(r"\|\[link\]\(https://invalid\.example\)", markdown)

    def test_renderers_do_not_introduce_private_identity_or_content_fields(self) -> None:
        artifacts = (
            render_audit_json(sample_audit()),
            render_audit_markdown(sample_audit()),
            render_audit_html(sample_audit()),
        )
        forbidden = (
            "project_id",
            "database_path",
            "root_key",
            "session_id",
            "turn_id",
            "capability",
            "raw_content",
        )
        for artifact in artifacts:
            with self.subTest(prefix=artifact[:20]):
                for field in forbidden:
                    self.assertNotIn(field, artifact)


if __name__ == "__main__":
    unittest.main()
