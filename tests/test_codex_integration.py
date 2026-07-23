from __future__ import annotations

import base64
import json
import os
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
    _read_receipt,
    _run_bounded,
    _write_receipt_bytes,
)


class SimulatedCrash(BaseException):
    """Model process termination after a Codex mutation took effect."""


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
        self.fail_sequence: list[tuple[str, object]] = []
        self.crash_after_mutation: int | None = None
        self.mutation_count = 0
        self.inspection_count = 0
        self.replace_marketplace_on_inspection: tuple[int, Path] | None = None

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
        self.inspection_count += 1
        replacement = self.replace_marketplace_on_inspection
        if replacement is not None and self.inspection_count == replacement[0]:
            self.marketplaces["hydra"] = replacement[1]
        return tuple(
            MarketplaceRecord(name, source)
            for name, source in sorted(self.marketplaces.items())
        )

    def add_marketplace(self, root: Path) -> None:
        self._mutate("add_marketplace", root)
        self.marketplaces["hydra"] = root.resolve()
        self._after_mutation()

    def remove_marketplace(self, name: str) -> None:
        self._mutate("remove_marketplace", name)
        self.marketplaces.pop(name, None)
        self._after_mutation()

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
        self._after_mutation()

    def remove_plugin(self, selector: str) -> None:
        self._mutate("remove_plugin", selector)
        self.installed_version = None
        self._after_mutation()

    def _mutate(self, operation: str, argument: object) -> None:
        call = (operation, argument)
        self.calls.append(call)
        if self.fail_sequence and self.fail_sequence[0] == call:
            self.fail_sequence.pop(0)
            raise RuntimeError("private adapter failure")
        if self.fail_on == call:
            self.fail_on = None
            raise RuntimeError("private adapter failure")

    def _after_mutation(self) -> None:
        self.mutation_count += 1
        if self.crash_after_mutation == self.mutation_count:
            self.crash_after_mutation = None
            raise SimulatedCrash


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

    def test_exact_repeat_with_refresh_is_also_a_noop(self) -> None:
        self.install_once()
        self.client.calls.clear()

        report = self.install_once(refresh=True)

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

    def test_crash_recovery_covers_every_configure_mutation_boundary(self) -> None:
        for boundary in range(1, 5):
            with self.subTest(boundary=boundary):
                root = self.root / f"crash-configure-{boundary}"
                old_marketplace = root / "old"
                new_marketplace = root / "new"
                old_marketplace.mkdir(parents=True)
                new_marketplace.mkdir()
                receipt = root / "data" / "codex-integration.json"
                client = StatefulCodexClient()
                client.available_versions[old_marketplace.resolve()] = "0.1.0"
                client.available_versions[new_marketplace.resolve()] = "0.2.0"
                configure_codex(
                    client=client,
                    marketplace_root=old_marketplace,
                    runtime_version="0.1.0",
                    receipt_path=receipt,
                    refresh=False,
                )
                client.mutation_count = 0
                client.crash_after_mutation = boundary

                with self.assertRaises(SimulatedCrash):
                    configure_codex(
                        client=client,
                        marketplace_root=new_marketplace,
                        runtime_version="0.2.0",
                        receipt_path=receipt,
                        refresh=True,
                    )

                journal = receipt.with_name(receipt.name + ".journal")
                self.assertTrue(journal.is_file())
                self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
                report = configure_codex(
                    client=client,
                    marketplace_root=new_marketplace,
                    runtime_version="0.2.0",
                    receipt_path=receipt,
                    refresh=True,
                )
                self.assertTrue(report.changed)
                self.assertEqual(client.marketplaces["hydra"], new_marketplace.resolve())
                self.assertEqual(client.installed_version, "0.2.0")
                self.assertFalse(journal.exists())

    def test_crash_recovery_covers_every_remove_mutation_boundary(self) -> None:
        for boundary in range(1, 3):
            with self.subTest(boundary=boundary):
                root = self.root / f"crash-remove-{boundary}"
                marketplace = root / "marketplace"
                marketplace.mkdir(parents=True)
                receipt = root / "data" / "codex-integration.json"
                client = StatefulCodexClient()
                client.available_versions[marketplace.resolve()] = "0.1.0"
                configure_codex(
                    client=client,
                    marketplace_root=marketplace,
                    runtime_version="0.1.0",
                    receipt_path=receipt,
                    refresh=False,
                )
                client.mutation_count = 0
                client.crash_after_mutation = boundary

                with self.assertRaises(SimulatedCrash):
                    remove_codex_integration(client=client, receipt_path=receipt)

                journal = receipt.with_name(receipt.name + ".journal")
                self.assertTrue(journal.is_file())
                self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
                report = remove_codex_integration(
                    client=client,
                    receipt_path=receipt,
                )
                self.assertTrue(report.changed)
                self.assertNotIn("hydra", client.marketplaces)
                self.assertIsNone(client.installed_version)
                self.assertFalse(receipt.exists())
                self.assertFalse(journal.exists())

    def test_crash_recovery_covers_every_rollback_mutation_boundary(self) -> None:
        for boundary in range(4, 7):
            with self.subTest(boundary=boundary):
                root = self.root / f"crash-rollback-{boundary}"
                old_marketplace = root / "old"
                new_marketplace = root / "new"
                old_marketplace.mkdir(parents=True)
                new_marketplace.mkdir()
                receipt = root / "data" / "codex-integration.json"
                client = StatefulCodexClient()
                client.available_versions[old_marketplace.resolve()] = "0.1.0"
                client.available_versions[new_marketplace.resolve()] = "0.2.0"
                configure_codex(
                    client=client,
                    marketplace_root=old_marketplace,
                    runtime_version="0.1.0",
                    receipt_path=receipt,
                    refresh=False,
                )
                client.mutation_count = 0
                client.fail_on = ("add_plugin", "hydra-codex@hydra")
                client.crash_after_mutation = boundary

                with self.assertRaises(SimulatedCrash):
                    configure_codex(
                        client=client,
                        marketplace_root=new_marketplace,
                        runtime_version="0.2.0",
                        receipt_path=receipt,
                        refresh=True,
                    )

                journal = receipt.with_name(receipt.name + ".journal")
                self.assertTrue(journal.is_file())
                report = configure_codex(
                    client=client,
                    marketplace_root=new_marketplace,
                    runtime_version="0.2.0",
                    receipt_path=receipt,
                    refresh=True,
                )
                self.assertTrue(report.changed)
                self.assertEqual(client.marketplaces["hydra"], new_marketplace.resolve())
                self.assertEqual(client.installed_version, "0.2.0")
                self.assertFalse(journal.exists())

    def test_concurrent_namespace_replacement_fails_before_destructive_mutation(
        self,
    ) -> None:
        self.install_once()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.calls.clear()
        self.client.inspection_count = 0
        self.client.replace_marketplace_on_inspection = (2, Path("/foreign"))

        with self.assertRaises(IntegrationOwnershipError):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.marketplaces["hydra"], Path("/foreign"))

    def test_concurrent_namespace_replacement_during_rollback_is_preserved(
        self,
    ) -> None:
        self.install_once()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.calls.clear()
        self.client.inspection_count = 0
        self.client.fail_on = ("add_plugin", "hydra-codex@hydra")
        self.client.replace_marketplace_on_inspection = (10, Path("/foreign"))

        with self.assertRaises(IntegrationError) as raised:
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        self.assertIn("live rollback failed", str(raised.exception))
        self.assertEqual(self.client.marketplaces["hydra"], Path("/foreign"))
        self.assertTrue(
            self.receipt.with_name(self.receipt.name + ".journal").is_file(),
        )

    def test_live_and_receipt_rollback_failures_are_both_reported_with_evidence(
        self,
    ) -> None:
        self.install_once()
        original_receipt = self.receipt.read_bytes()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.fail_sequence = [
            ("add_plugin", "hydra-codex@hydra"),
            ("add_marketplace", self.marketplace.resolve()),
        ]
        desired_payload = json.loads(original_receipt)
        desired_payload["runtime_version"] = "0.2.0"
        desired_payload["source"] = str(second.resolve())
        desired_receipt = (
            json.dumps(desired_payload, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        real_write = _write_receipt_bytes

        def fail_original_restore(path: Path, content: bytes) -> None:
            if path == journal:
                real_write(path, content)
                real_write(self.receipt, desired_receipt)
                return
            if content == original_receipt:
                raise OSError("private receipt restore failure")
            real_write(path, content)

        with (
            mock.patch(
                "hydra_codex.codex_integration._write_receipt_bytes",
                side_effect=fail_original_restore,
            ),
            self.assertRaises(IntegrationError) as raised,
        ):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        message = str(raised.exception)
        self.assertIn("live rollback failed", message)
        self.assertIn("receipt restore failed", message)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(
            base64.b64decode(payload["prior_receipt_b64"]),
            original_receipt,
        )

    def test_pending_configure_preserves_unknown_receipt_with_known_live_state(
        self,
    ) -> None:
        self.install_once()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.mutation_count = 0
        self.client.crash_after_mutation = 1
        with self.assertRaises(SimulatedCrash):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        replacement = b'{"private":"/secret/configure-replacement"}\n'
        self.receipt.write_bytes(replacement)
        self.receipt.chmod(0o600)
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        self.assertEqual(self.receipt.read_bytes(), replacement)
        self.assertTrue(journal.is_file())
        self.assertEqual(self.client.calls, [])

    def test_pending_configure_preserves_unknown_receipt_when_live_state_is_unknown(
        self,
    ) -> None:
        self.install_once()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.mutation_count = 0
        self.client.crash_after_mutation = 1
        with self.assertRaises(SimulatedCrash):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        replacement = b'{"private":"/secret/configure-live-failure"}\n'
        self.receipt.write_bytes(replacement)
        self.receipt.chmod(0o600)
        self.client.marketplaces["hydra"] = Path("/foreign")
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        self.assertEqual(self.receipt.read_bytes(), replacement)
        self.assertTrue(journal.is_file())
        self.assertEqual(self.client.marketplaces["hydra"], Path("/foreign"))
        self.assertEqual(self.client.calls, [])

    def test_pending_remove_preserves_unknown_receipt_with_known_live_state(
        self,
    ) -> None:
        self.install_once()
        self.client.mutation_count = 0
        self.client.crash_after_mutation = 1
        with self.assertRaises(SimulatedCrash):
            remove_codex_integration(client=self.client, receipt_path=self.receipt)
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        replacement = b'{"private":"/secret/remove-replacement"}\n'
        self.receipt.write_bytes(replacement)
        self.receipt.chmod(0o600)
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            remove_codex_integration(client=self.client, receipt_path=self.receipt)

        self.assertEqual(self.receipt.read_bytes(), replacement)
        self.assertTrue(journal.is_file())
        self.assertEqual(self.client.calls, [])

    def test_pending_remove_preserves_unknown_receipt_when_live_state_is_unknown(
        self,
    ) -> None:
        self.install_once()
        self.client.mutation_count = 0
        self.client.crash_after_mutation = 1
        with self.assertRaises(SimulatedCrash):
            remove_codex_integration(client=self.client, receipt_path=self.receipt)
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        replacement = b'{"private":"/secret/remove-live-failure"}\n'
        self.receipt.write_bytes(replacement)
        self.receipt.chmod(0o600)
        self.client.marketplaces["hydra"] = Path("/foreign")
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            remove_codex_integration(client=self.client, receipt_path=self.receipt)

        self.assertEqual(self.receipt.read_bytes(), replacement)
        self.assertTrue(journal.is_file())
        self.assertEqual(self.client.marketplaces["hydra"], Path("/foreign"))
        self.assertEqual(self.client.calls, [])

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

    @unittest.skipUnless(os.name == "posix", "POSIX ownership contract")
    def test_receipt_reader_rejects_wrong_owner_from_open_descriptor(self) -> None:
        self.install_once()
        real_fstat = os.fstat

        def foreign_owner(descriptor: int):
            result = real_fstat(descriptor)
            return mock.Mock(
                st_mode=result.st_mode,
                st_uid=os.getuid() + 1,
                st_size=result.st_size,
            )

        with (
            mock.patch(
                "hydra_codex.codex_integration.os.fstat",
                side_effect=foreign_owner,
            ),
            self.assertRaises(IntegrationOwnershipError),
        ):
            _read_receipt(self.receipt)

    def test_receipt_reader_rejects_symlink(self) -> None:
        target = self.root / "target-receipt"
        target.write_text("{}", encoding="utf-8")
        self.receipt.parent.mkdir()
        self.receipt.symlink_to(target)

        with self.assertRaises(IntegrationOwnershipError):
            _read_receipt(self.receipt)

    def test_receipt_reader_uses_one_open_descriptor_without_path_reread(self) -> None:
        self.install_once()
        original = self.receipt.read_bytes()
        replacement = self.receipt.with_name("replacement")
        replacement.write_text('{"not":"owned"}', encoding="utf-8")
        replacement.chmod(0o600)
        real_lstat = Path.lstat
        intercepted = False

        def replace_after_metadata(path: Path):
            nonlocal intercepted
            result = real_lstat(path)
            if path == self.receipt and not intercepted:
                intercepted = True
                os.replace(replacement, self.receipt)
            return result

        with mock.patch(
            "hydra_codex.codex_integration.Path.lstat",
            side_effect=replace_after_metadata,
        ):
            receipt, raw = _read_receipt(self.receipt)

        self.assertIsNotNone(receipt)
        self.assertEqual(raw, original)

    def test_pending_journal_symlink_and_nonprivate_mode_fail_closed(self) -> None:
        for case in ("symlink", "mode"):
            with self.subTest(case=case):
                root = self.root / f"journal-{case}"
                old_marketplace = root / "old"
                new_marketplace = root / "new"
                old_marketplace.mkdir(parents=True)
                new_marketplace.mkdir()
                receipt = root / "data" / "codex-integration.json"
                client = StatefulCodexClient()
                client.available_versions[old_marketplace.resolve()] = "0.1.0"
                client.available_versions[new_marketplace.resolve()] = "0.2.0"
                configure_codex(
                    client=client,
                    marketplace_root=old_marketplace,
                    runtime_version="0.1.0",
                    receipt_path=receipt,
                    refresh=False,
                )
                client.mutation_count = 0
                client.crash_after_mutation = 1
                with self.assertRaises(SimulatedCrash):
                    configure_codex(
                        client=client,
                        marketplace_root=new_marketplace,
                        runtime_version="0.2.0",
                        receipt_path=receipt,
                        refresh=True,
                    )
                journal = receipt.with_name(receipt.name + ".journal")
                if case == "mode":
                    journal.chmod(0o644)
                else:
                    saved = journal.with_name("saved-journal")
                    journal.replace(saved)
                    journal.symlink_to(saved)
                client.calls.clear()

                with self.assertRaises(IntegrationOwnershipError):
                    configure_codex(
                        client=client,
                        marketplace_root=new_marketplace,
                        runtime_version="0.2.0",
                        receipt_path=receipt,
                        refresh=True,
                    )

                self.assertEqual(client.calls, [])

    def test_pending_journal_must_bind_desired_receipt_to_desired_state(self) -> None:
        self.install_once()
        second = self.root / "marketplace-v2"
        second.mkdir()
        self.client.available_versions[second.resolve()] = "0.2.0"
        self.client.mutation_count = 0
        self.client.crash_after_mutation = 1
        with self.assertRaises(SimulatedCrash):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["desired_receipt"]["runtime_version"] = "9.9.9"
        journal.write_text(json.dumps(payload), encoding="utf-8")
        journal.chmod(0o600)
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            self.install_once(
                version="0.2.0",
                marketplace=second,
                refresh=True,
            )

        self.assertEqual(self.client.calls, [])

    def test_pending_configure_without_prior_receipt_requires_empty_prior_state(
        self,
    ) -> None:
        self.client.crash_after_mutation = 1
        with self.assertRaises(SimulatedCrash):
            self.install_once()
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["prior_state"] = {
            "marketplace": {
                "name": "hydra",
                "source": str(self.marketplace.resolve()),
            },
            "plugin": {
                "installed": False,
                "marketplace": "hydra",
                "name": "hydra-codex",
                "version": "0.1.0",
            },
        }
        journal.write_text(json.dumps(payload), encoding="utf-8")
        journal.chmod(0o600)
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            self.install_once()

        self.assertEqual(self.client.calls, [])
        self.assertTrue(journal.is_file())

    def test_pending_remove_requires_prior_receipt(self) -> None:
        self.install_once()
        self.client.mutation_count = 0
        self.client.crash_after_mutation = 1
        with self.assertRaises(SimulatedCrash):
            remove_codex_integration(client=self.client, receipt_path=self.receipt)
        journal = self.receipt.with_name(self.receipt.name + ".journal")
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["prior_receipt_b64"] = None
        journal.write_text(json.dumps(payload), encoding="utf-8")
        journal.chmod(0o600)
        self.client.calls.clear()

        with self.assertRaises(IntegrationOwnershipError):
            remove_codex_integration(client=self.client, receipt_path=self.receipt)

        self.assertEqual(self.client.calls, [])
        self.assertTrue(journal.is_file())

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

    def test_explicit_codex_home_is_forwarded_without_normalization(self) -> None:
        supplied = "/private/profile/../codex-profile"
        with (
            mock.patch(
                "hydra_codex.codex_integration.shutil.which",
                return_value="/bin/codex",
            ),
            mock.patch(
                "hydra_codex.codex_integration._run_bounded",
                return_value=(0, b"codex-cli 1.2\n", b""),
            ) as run,
        ):
            client = CodexCommandClient(
                environ={"CODEX_HOME": supplied, "PATH": "/usr/bin"},
            )
            client.version()

        self.assertEqual(
            run.call_args.kwargs["environ"].get("CODEX_HOME"),
            supplied,
        )

    def test_missing_codex_home_is_not_invented(self) -> None:
        with (
            mock.patch(
                "hydra_codex.codex_integration.shutil.which",
                return_value="/bin/codex",
            ),
            mock.patch(
                "hydra_codex.codex_integration._run_bounded",
                return_value=(0, b"codex-cli 1.2\n", b""),
            ) as run,
        ):
            client = CodexCommandClient(environ={"PATH": "/usr/bin"})
            client.version()

        self.assertNotIn("CODEX_HOME", run.call_args.kwargs["environ"])

    def test_invalid_codex_home_is_rejected_without_disclosure(self) -> None:
        values = ("", "/private/profile\0secret", 42)
        for value in values:
            with self.subTest(value=type(value).__name__):
                with mock.patch(
                    "hydra_codex.codex_integration.shutil.which",
                    return_value="/bin/codex",
                ):
                    with self.assertRaises(IncompatibleCodexError) as raised:
                        CodexCommandClient(
                            environ={"CODEX_HOME": value, "PATH": "/usr/bin"},
                        )
                self.assertEqual(
                    str(raised.exception),
                    "Codex integration is unavailable",
                )

    def test_plugin_versions_accept_short_pep440_and_codex_tokens(self) -> None:
        versions = (
            "0.1.0",
            "1!2.0.0rc1.post2.dev3+local.1",
            "0.104.0-alpha.1",
        )
        for version in versions:
            payload = json.dumps([{
                "name": "hydra-codex",
                "marketplace": "hydra",
                "installed": True,
                "version": version,
            }]).encode()
            with (
                self.subTest(version=version),
                mock.patch(
                    "hydra_codex.codex_integration.shutil.which",
                    return_value="/bin/codex",
                ),
                mock.patch(
                    "hydra_codex.codex_integration._run_bounded",
                    return_value=(0, payload, b""),
                ),
            ):
                client = CodexCommandClient(environ={"PATH": "/usr/bin"})
                self.assertEqual(
                    client.list_plugins("hydra", include_available=True)[0].version,
                    version,
                )

    def test_unsafe_plugin_versions_are_rejected_without_echo(self) -> None:
        versions = (
            "/private/profile/plugin",
            "0.1.0\nprivate-control",
            "v" * 129,
        )
        for version in versions:
            payload = json.dumps([{
                "name": "hydra-codex",
                "marketplace": "hydra",
                "installed": True,
                "version": version,
            }]).encode()
            with (
                self.subTest(kind=len(version)),
                mock.patch(
                    "hydra_codex.codex_integration.shutil.which",
                    return_value="/bin/codex",
                ),
                mock.patch(
                    "hydra_codex.codex_integration._run_bounded",
                    return_value=(0, payload, b""),
                ),
            ):
                client = CodexCommandClient(environ={"PATH": "/usr/bin"})
                with self.assertRaises(IncompatibleCodexError) as raised:
                    client.list_plugins("hydra", include_available=True)
                self.assertNotIn(version, str(raised.exception))

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
