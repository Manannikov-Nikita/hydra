from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReadmeContractTests(unittest.TestCase):
    def test_readme_describes_the_shipped_mvp_without_overclaiming_the_pilot(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.lower().split())

        for command in (
            "hydra-codex ingest",
            "hydra-codex annotate",
            "hydra-codex reconcile",
            "hydra-codex report",
            "hydra-codex compare",
            "hydra-codex audit",
        ):
            self.assertIn(command, normalized)
        self.assertIn("~/library/application support/hydra/hydra.sqlite3", normalized)
        self.assertIn("working_tokens = input_tokens - cached_input_tokens + output_tokens", normalized)
        self.assertIn("full_context = input_tokens + output_tokens", normalized)
        self.assertIn("hydra.report/v3", normalized)
        self.assertIn("--event-source app-server-v2=", normalized)
        self.assertIn("--event-source otel-v1=", normalized)
        self.assertIn("five subsequent real codex tasks", normalized)
        self.assertIn("does not advertise `hydra.annotate`", normalized)
        self.assertIn("deterministic adapters do not store raw prompts", normalized)
        self.assertIn("not a general-purpose content classifier", normalized)
        self.assertIn("test-evidence cross-tab", normalized)
        self.assertIn("awaiting_receipt", normalized)
        self.assertIn("hydra.audit/v1", normalized)
        self.assertIn("does not drain pending annotations", normalized)
        self.assertIn("ambiguous shell expressions contribute no guessed file facts", normalized)
        self.assertIn("python -m pip install -e .", normalized)
        self.assertNotIn("pip install --no-build-isolation", normalized)
        self.assertNotIn("rendering, hooks, mcp, and plugin behavior remain intentionally out of scope", normalized)

    def test_readme_links_the_authoritative_codex_surfaces(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for url in (
            "https://learn.chatgpt.com/docs/hooks",
            "https://learn.chatgpt.com/docs/app-server",
            "https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry",
            "https://learn.chatgpt.com/docs/build-skills",
        ):
            self.assertIn(url, readme)


if __name__ == "__main__":
    unittest.main()
