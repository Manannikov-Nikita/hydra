from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReadmeContractTests(unittest.TestCase):
    def test_primary_install_flow_is_public_standalone_before_developer_setup(
        self,
    ) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        commands = (
            "curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh",
            "hydra-codex install -y",
            "hydra-codex init .",
            "hydra-codex status . --json",
            "hydra-codex dashboard",
        )
        for command in commands:
            self.assertIn(command, readme)
        path_export = 'export PATH="$HOME/.local/bin:$PATH"'
        self.assertIn(path_export, readme)
        self.assertLess(readme.index(commands[0]), readme.index(path_export))
        self.assertLess(readme.index(path_export), readme.index(commands[1]))
        self.assertIn("Developer installation", readme)
        self.assertLess(readme.index(commands[0]), readme.index("Developer installation"))
        self.assertIn(
            "~/Library/Application Support/Hydra/hydra.sqlite3",
            readme,
        )
        self.assertIn("~/.local/share/hydra/hydra.sqlite3", readme)
        self.assertIn("$XDG_DATA_HOME/hydra/hydra.sqlite3", readme)

    def test_readme_links_public_install_upgrade_privacy_and_release_guides(
        self,
    ) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for link in (
            "docs/installation.md",
            "docs/upgrade-and-uninstall.md",
            "docs/privacy.md",
            "docs/troubleshooting.md",
            "docs/release-process.md",
        ):
            self.assertIn(link, readme)
        self.assertIn(
            "hydra-codex dashboard --cwd /path/to/project --port 0 --no-open",
            readme,
        )

    def test_plugin_upgrade_instructions_use_atomic_upgrade_refresh(self) -> None:
        plugin = (
            ROOT / "plugins" / "hydra-codex" / "README.md"
        ).read_text(encoding="utf-8")
        bootstrap = (
            "curl -fsSL "
            "https://raw.githubusercontent.com/Manannikov-Nikita/hydra/"
            "main/install.sh | sh"
        )
        path_export = 'export PATH="$HOME/.local/bin:$PATH"'
        self.assertIn(path_export, plugin)
        self.assertLess(plugin.index(bootstrap), plugin.index(path_export))
        self.assertLess(
            plugin.index(path_export),
            plugin.index("hydra-codex install -y"),
        )
        self.assertNotIn("hydra-codex install -y --refresh", plugin)
        self.assertIn("hydra-codex upgrade", plugin)
        self.assertIn("refreshes the Codex integration atomically", plugin)

    def test_public_installation_names_exact_first_release_os_baselines(self) -> None:
        installation = (ROOT / "docs" / "installation.md").read_text(
            encoding="utf-8",
        )
        troubleshooting = (ROOT / "docs" / "troubleshooting.md").read_text(
            encoding="utf-8",
        )
        for document in (installation, troubleshooting):
            self.assertIn("macOS 15", document)
            self.assertIn("Ubuntu 24.04", document)
            self.assertIn("glibc 2.39", document)
        self.assertNotIn(
            "supports macOS on Apple Silicon and Intel, and\nLinux on x86-64",
            installation,
        )

    def test_release_checksum_recipe_filters_one_exact_archive_entry(self) -> None:
        runbook = (ROOT / "docs" / "release-process.md").read_text(
            encoding="utf-8",
        )
        filter_command = (
            "awk -v file=\"$archive\" '$2 == file {print}' "
            "SHA256SUMS > SHA256SUMS.target"
        )
        count_command = (
            "[ \"$(wc -l < SHA256SUMS.target | tr -d '[:space:]')\" = 1 ]"
        )
        self.assertIn(filter_command, runbook)
        self.assertIn(count_command, runbook)
        self.assertIn("sha256sum -c SHA256SUMS.target", runbook)
        self.assertIn("shasum -a 256 -c SHA256SUMS.target", runbook)
        self.assertNotIn("sha256sum -c SHA256SUMS\n", runbook)
        self.assertNotIn("shasum -a 256 -c SHA256SUMS`", runbook)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = (
                "hydra-codex-0.1.0-darwin-arm64.tar.gz",
                "hydra-codex-0.1.0-darwin-x86_64.tar.gz",
                "hydra-codex-0.1.0-linux-x86_64.tar.gz",
            )
            lines = []
            for index, name in enumerate(names):
                payload = f"archive-{index}".encode()
                (root / name).write_bytes(payload)
                lines.append(f"{sha256(payload).hexdigest()}  {name}")
            (root / "SHA256SUMS").write_text(
                "\n".join(lines) + "\n",
                encoding="ascii",
            )
            script = "\n".join((
                f"archive={names[0]}",
                filter_command,
                count_command,
            ))
            subprocess.run(
                ["sh", "-ceu", script],
                cwd=root,
                check=True,
                timeout=5,
            )
            self.assertEqual(
                (root / "SHA256SUMS.target").read_text(encoding="ascii"),
                lines[0] + "\n",
            )

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
            "hydra-codex doctor",
            "hydra-codex storage status",
            "hydra-codex storage compact",
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
        self.assertIn("hydra.comparison/v2", normalized)
        self.assertIn("comparable", normalized)
        self.assertIn("partial", normalized)
        self.assertIn("not_comparable", normalized)
        self.assertIn("unknown", normalized)
        self.assertIn('confirmation "compact hydra database"', normalized)
        self.assertIn("growth_baseline_unavailable", normalized)
        self.assertIn("pilot close", normalized)
        self.assertIn("`verified` or `rejected`", normalized)
        self.assertIn("does not drain pending annotations", normalized)
        self.assertIn("ambiguous shell expressions contribute no guessed file facts", normalized)
        self.assertIn("python -m pip install -e .", normalized)
        self.assertNotIn("pip install --no-build-isolation", normalized)
        self.assertNotIn("rendering, hooks, mcp, and plugin behavior remain intentionally out of scope", normalized)

    def test_local_diagnostics_remain_outside_the_plugin_mcp_surface(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.lower().split())
        self.assertIn("doctor and storage maintenance remain cli-only", normalized)

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
