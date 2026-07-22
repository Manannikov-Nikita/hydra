from __future__ import annotations

import json
import unittest

from hydra_codex.audit_builder import build_audit
from hydra_codex.audit_model import AuditReport
from hydra_codex.audit_renderers import (
    render_audit_html,
    render_audit_json,
    render_audit_markdown,
)
from tests.test_audit_builder import pilot_snapshot, public_report, storage


def sample_audit():
    reports = (
        public_report("render-a", input_tokens=120, second=10),
        public_report("render-b", input_tokens=220, second=20),
    )
    return build_audit(pilot_snapshot(reports), reports, storage())


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
