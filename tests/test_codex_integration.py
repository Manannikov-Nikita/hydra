from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from hydra_codex.codex_integration import (
    CodexCommandClient,
    IncompatibleCodexError,
    IntegrationError,
    IntegrationOwnershipError,
    MarketplaceRecord,
    PluginRecord,
    configure_codex,
    remove_codex_integration,
    render_codex_config,
    _run_bounded,
)


class StatefulCodexClient:
    """Stateful Codex adapter used without reading or writing the real home."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.marketplaces: dict[str, Path] = {}
        self.available_versions: dict[Path, str] = {}
        self.installed_version: str | None = None
        self.version_supported = True
        self.marketplace_listing_supported = True
        self.plugin_listing_supported = True
        self.fail_on: tuple[str, object] | None = None

    @property
    def mutation_calls(self) -> list[tuple[str, object]]:
        return self.calls

    def version(self) -> str:
        if not self.version_supported:
            raise IncompatibleCodexError("Codex integration is unavailable")
        return "codex-cli test"

    def list_marketplaces(self) -> tuple[MarketplaceRecord, ...]:
        if not self.marketplace_listing_supported:
            raise IncompatibleCodexError("Codex integration is unavailable")
        return tuple(
            MarketplaceRecord(name, source)
            for name, source in sorted(self.marketplaces.items())
        )

    def add_marketplace(self, root: Path) -> None:
        self._mutate("add_marketplace", root)
        self.marketplaces["hydra"] = root.resolve()

    def remove_marketplace(self, name: str) -> None:
        self._mutate("remove_marketplace", name)
        self.marketplaces.pop(name, None)

    def list_plugins(
        self,
        marketplace: str,
        *,
        include_available: bool,
    ) -> tuple[PluginRecord, ...]:
        if not self.plugin_listing_supported:
            raise IncompatibleCodexError("Codex integration is unavailable")
        source = self.marketplaces.get(marketplace)
        if source is None:
            return ()
        version = (
            self.installed_version
            if self.installed_version is not None
            else self.available_versions.get(source)
        )
        if version is None:
            return ()
        return (
            PluginRecord(
                "hydra-codex",
                marketplace,
                self.installed_version is not None,
                version,
            ),
        )

    def add_plugin(self, selector: str) -> None:
        self._mutate("add_plugin", selector)
        source = self.marketplaces["hydra"]
        self.installed_version = self.available_versions[source]

    def remove_plugin(self, selector: str) -> None:
        self._mutate("remove_plugin", selector)
        self.installed_version = None

    def _mutate(self, operation: str, argument: object) -> None:
        call = (operation, argument)
        self.calls.append(call)
        if self.fail_on == call:
            self.fail_on = None
            raise RuntimeError("private adapter failure")


class CodexIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.marketplace = self.root / "marketplace-v1"
        self.marketplace.mkdir()
        self.receipt = self.root / "data" / "codex-integration.json"
        self.client = StatefulCodexClient()
        self.client.available_versions[self.marketplace.resolve()] = "0.1.0"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install_once(
        self,
        *,
        version: str = "0.1.0",
        marketplace: Path | None = None,
        refresh: bool = False,
    ):
        return configure_codex(
            client=self.client,
            marketplace_root=self.marketplace if marketplace is None else marketplace,
            runtime_version=version,
            receipt_path=self.receipt,
            refresh=refresh,
        )

    def test_fresh_install_adds_marketplace_plugin_and_private_receipt(self) -> None:
        report = self.install_once()

        self.assertTrue(report.changed)
        self.assertEqual(
            self.client.calls,
            [
                ("add_marketplace", self.marketplace.resolve()),
                ("add_plugin", "hydra-codex@hydra"),
            ],
        )
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(self.receipt.read_text(encoding="utf-8")),
            {
                "marketplace": "hydra",
                "runtime_version": "0.1.0",
                "schema_version": 1,
                "selector": "hydra-codex@hydra",
                "source": str(self.marketplace.resolve()),
            },
        )

    def test_exact_repeat_is_a_noop(self) -> None:
        self.install_once()
        self.client.calls.clear()

        report = self.install_once()

        self.assertFalse(report.changed)
        self.assertEqual(self.client.calls, [])

    def test_refresh_replaces_exact_owned_state(self) -> None:
        self.install_once()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.calls.clear()

        report = self.install_once(
            version="0.2.0",
            marketplace=second,
            refresh=True,
        )

        self.assertTrue(report.changed)
        self.assertEqual(
            self.client.calls,
            [
                ("remove_plugin", "hydra-codex@hydra"),
                ("remove_marketplace", "hydra"),
                ("add_marketplace", second.resolve()),
                ("add_plugin", "hydra-codex@hydra"),
            ],
        )
        self.assertEqual(self.client.installed_version, "0.2.0")

    def test_owned_plugin_drift_readds_marketplace_and_repairs(self) -> None:
        self.install_once()
        self.client.installed_version = "9.9.9"
        self.client.calls.clear()

        report = self.install_once()

        self.assertTrue(report.changed)
        self.assertEqual(
            self.client.calls,
            [
                ("remove_plugin", "hydra-codex@hydra"),
                ("remove_marketplace", "hydra"),
                ("add_marketplace", self.marketplace.resolve()),
                ("add_plugin", "hydra-codex@hydra"),
            ],
        )
        self.assertEqual(self.client.installed_version, "0.1.0")

    def test_changed_marketplace_source_requires_explicit_refresh(self) -> None:
        self.install_once()
        second = self.root / "moved-marketplace"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.1.0"
        self.client.calls.clear()

        with self.assertRaises(IntegrationError):
            self.install_once(marketplace=second)

        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.marketplaces["hydra"], self.marketplace.resolve())

    def test_refresh_failure_restores_previous_integration_and_receipt(self) -> None:
        original = self.install_once()
        original_receipt = self.receipt.read_bytes()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.fail_on = ("add_plugin", "hydra-codex@hydra")

        with self.assertRaises(IntegrationError):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        self.assertEqual(self.receipt.read_bytes(), original_receipt)
        self.assertEqual(self.client.marketplaces["hydra"], self.marketplace.resolve())
        self.assertEqual(self.client.installed_version, original.runtime_version)

    def test_rollback_restores_the_exact_previous_receipt_bytes(self) -> None:
        self.install_once()
        payload = json.loads(self.receipt.read_text(encoding="utf-8"))
        exact = (json.dumps(payload, indent=2) + "\n").encode()
        self.receipt.write_bytes(exact)
        self.receipt.chmod(0o600)
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.fail_on = ("add_plugin", "hydra-codex@hydra")

        with self.assertRaises(IntegrationError):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        self.assertEqual(self.receipt.read_bytes(), exact)

    def test_uninstall_removes_only_receipted_integration_and_is_repeatable(self) -> None:
        self.install_once()
        self.client.calls.clear()

        first = remove_codex_integration(
            client=self.client,
            receipt_path=self.receipt,
        )
        second = remove_codex_integration(
            client=self.client,
            receipt_path=self.receipt,
        )

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(
            self.client.calls,
            [
                ("remove_plugin", "hydra-codex@hydra"),
                ("remove_marketplace", "hydra"),
            ],
        )
        self.assertFalse(self.receipt.exists())

    def test_uninstall_never_touches_unowned_marketplace(self) -> None:
        self.client.marketplaces["hydra"] = Path("/foreign")

        with self.assertRaises(IntegrationOwnershipError):
            remove_codex_integration(
                client=self.client,
                receipt_path=self.receipt,
            )

        self.assertEqual(self.client.calls, [])

    def test_existing_state_without_receipt_is_ownership_ambiguity(self) -> None:
        self.client.marketplaces["hydra"] = self.marketplace.resolve()
        self.client.installed_version = "0.1.0"

        with self.assertRaises(IntegrationOwnershipError):
            self.install_once()

        self.assertEqual(self.client.calls, [])

    def test_corrupt_receipt_fails_closed_before_mutation(self) -> None:
        self.receipt.parent.mkdir()
        self.receipt.write_text('{"source":"/private/foreign"}', encoding="utf-8")

        with self.assertRaises(IntegrationOwnershipError) as raised:
            self.install_once()

        self.assertEqual(self.client.calls, [])
        self.assertNotIn(str(self.root), str(raised.exception))

    def test_nonprivate_receipt_fails_closed_before_mutation(self) -> None:
        self.install_once()
        self.receipt.chmod(0o644)
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            self.install_once()

        self.assertEqual(self.client.calls, [])

    def test_incompatible_codex_fails_before_mutation(self) -> None:
        self.client.plugin_listing_supported = False

        with self.assertRaises(IncompatibleCodexError):
            self.install_once()

        self.assertEqual(self.client.mutation_calls, [])

    def test_available_plugin_must_match_runtime_before_plugin_mutation(self) -> None:
        self.client.available_versions[self.marketplace.resolve()] = "9.9.9"

        with self.assertRaises(IntegrationError):
            self.install_once()

        self.assertEqual(
            self.client.calls,
            [
                ("add_marketplace", self.marketplace.resolve()),
                ("remove_marketplace", "hydra"),
            ],
        )
        self.assertFalse(self.receipt.exists())

    def test_rendered_config_uses_supported_commands_and_never_config_toml(self) -> None:
        rendered = render_codex_config(
            marketplace_root=self.marketplace,
            runtime_version="0.1.0",
        )

        self.assertIn(
            f"codex plugin marketplace add {self.marketplace.resolve()} --json",
            rendered,
        )
        self.assertIn("codex plugin add hydra-codex@hydra --json", rendered)
        self.assertIn("0.1.0", rendered)
        self.assertNotIn("config.toml", rendered)


class CodexCommandClientTests(unittest.TestCase):
    def test_subprocess_runner_bounds_output_and_time(self) -> None:
        with self.assertRaises(subprocess.SubprocessError):
            _run_bounded(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'x' * 65536)",
                ],
                environ={"PATH": "/usr/bin:/bin"},
                timeout=2.0,
                max_output_bytes=1024,
            )
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_bounded(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                environ={"PATH": "/usr/bin:/bin"},
                timeout=0.01,
                max_output_bytes=1024,
            )

    def test_supported_commands_and_strict_json_shapes(self) -> None:
        completed = [
            (0, b"codex-cli 1.2\n", b""),
            (
                0,
                b'[{"name":"hydra","source":"/marketplace"}]\n',
                b"",
            ),
            (
                0,
                (
                    b'[{"name":"hydra-codex","marketplace":"hydra",'
                    b'"installed":true,"version":"0.1.0"}]\n'
                ),
                b"",
            ),
        ]
        with (
            mock.patch("hydra_codex.codex_integration.shutil.which", return_value="/bin/codex"),
            mock.patch(
                "hydra_codex.codex_integration._run_bounded",
                side_effect=completed,
            ) as run,
        ):
            client = CodexCommandClient(environ={"HOME": "/private/home", "PATH": "/unsafe"})
            self.assertEqual(client.version(), "codex-cli 1.2")
            self.assertEqual(
                client.list_marketplaces(),
                (MarketplaceRecord("hydra", Path("/marketplace")),),
            )
            self.assertEqual(
                client.list_plugins("hydra", include_available=True),
                (PluginRecord("hydra-codex", "hydra", True, "0.1.0"),),
            )

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/bin/codex", "--version"],
                ["/bin/codex", "plugin", "marketplace", "list", "--json"],
                [
                    "/bin/codex", "plugin", "list",
                    "--marketplace", "hydra", "--available", "--json",
                ],
            ],
        )
        for call in run.call_args_list:
            self.assertNotEqual(call.kwargs["environ"]["PATH"], "/unsafe")
            self.assertEqual(call.kwargs["timeout"], 10.0)
            self.assertEqual(call.kwargs["max_output_bytes"], 1024 * 1024)

    def test_failures_and_malformed_output_are_redacted(self) -> None:
        private = "/private/home/secret-output"
        cases = (
            (1, b"", private.encode()),
            (0, private.encode(), b""),
            (0, b'[{"name":"hydra"}]', b""),
            (
                0,
                b'[{"name":"hydra","source":"relative"}]',
                b"",
            ),
        )
        for completed in cases:
            with (
                self.subTest(completed=completed),
                mock.patch(
                    "hydra_codex.codex_integration.shutil.which",
                    return_value="/bin/codex",
                ),
                mock.patch(
                    "hydra_codex.codex_integration._run_bounded",
                    return_value=completed,
                ),
            ):
                client = CodexCommandClient(environ={"HOME": "/private/home"})
                with self.assertRaises(IncompatibleCodexError) as raised:
                    client.list_marketplaces()
                self.assertNotIn(private, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
