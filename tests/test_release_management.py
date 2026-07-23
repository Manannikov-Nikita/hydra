from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from hydra_codex.cli import build_parser
from hydra_codex.codex_integration import IntegrationError
from hydra_codex.install_layout import (
    BundleLayout,
    CANONICAL_PLUGIN_FILES,
    InvalidBundle,
    validate_bundle,
)
from hydra_codex.release_management import (
    InstallOwnershipError,
    InstallRoots,
    LifecycleBusyError,
    activate_version,
    default_install_roots,
    uninstall,
    upgrade,
)


class SimulatedCrash(BaseException):
    pass


def _bundle(parent: Path, version: str, *, marker: str = "original") -> BundleLayout:
    root = parent / f"bundle-{version.replace('/', '_')}-{marker}"
    (root / "bin").mkdir(parents=True)
    executable = root / "bin" / "hydra-codex"
    executable.write_text(f"#!/bin/sh\n# {marker}\n", encoding="utf-8")
    executable.chmod(0o700)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "TARGET").write_text("darwin-arm64\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")

    marketplace = root / "marketplace"
    inventory = marketplace / ".agents" / "plugins" / "marketplace.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps({
            "name": "hydra",
            "plugins": [{
                "name": "hydra-codex",
                "source": {"source": "local", "path": "./plugins/hydra-codex"},
            }],
        }),
        encoding="utf-8",
    )
    plugin = marketplace / "plugins" / "hydra-codex"
    for relative in CANONICAL_PLUGIN_FILES:
        target = plugin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == Path(".codex-plugin/plugin.json"):
            content = json.dumps({"name": "hydra-codex", "version": version})
        else:
            content = f"{relative.as_posix()}\n"
        target.write_text(content, encoding="utf-8")
    return validate_bundle(root)


class ReleaseManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.user_home = self.base / "user"
        hydra_home = self.user_home / ".hydra"
        self.roots = InstallRoots(
            home=hydra_home,
            versions=hydra_home / "versions",
            current=hydra_home / "current",
            launcher=self.user_home / ".local" / "bin" / "hydra-codex",
        )
        self.environ = {"HOME": str(self.user_home)}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def activate(self, version: str, *, marker: str = "original") -> Path:
        return activate_version(
            _bundle(self.base, version, marker=marker),
            roots=self.roots,
        )

    def test_default_roots_use_only_the_selected_home(self) -> None:
        roots = default_install_roots(self.user_home)
        self.assertEqual(roots.home, self.user_home / ".hydra")
        self.assertEqual(roots.versions, roots.home / "versions")
        self.assertEqual(roots.current, roots.home / "current")
        self.assertEqual(
            roots.launcher,
            self.user_home / ".local" / "bin" / "hydra-codex",
        )

    def test_parser_accepts_upgrade_check_and_uninstall_keep_cli(self) -> None:
        upgrade_args = build_parser().parse_args(["upgrade", "--check"])
        uninstall_args = build_parser().parse_args(["uninstall", "--keep-cli"])
        self.assertEqual(upgrade_args.command, "upgrade")
        self.assertTrue(upgrade_args.check)
        self.assertTrue(uninstall_args.keep_cli)

    def test_activation_rejects_malformed_version_components(self) -> None:
        for version in ("", ".", "..", "../escape", "nested/release", "bad\\name"):
            with self.subTest(version=version):
                root = self.base / ("malformed-" + str(len(version)))
                root.mkdir(exist_ok=True)
                layout = BundleLayout(
                    root=root,
                    version=version,
                    target="darwin-arm64",
                    executable=root / "bin" / "hydra-codex",
                    marketplace=root / "marketplace",
                )
                with self.assertRaises(ValueError):
                    activate_version(layout, roots=self.roots)
        self.assertFalse(self.roots.versions.exists())

    def test_activation_rejects_nonportable_but_complete_version_bundles(self) -> None:
        for version in ("bad version", "v" * 129, "rélease"):
            with self.subTest(version=version):
                layout = _bundle(self.base, version)
                with self.assertRaises(ValueError):
                    activate_version(layout, roots=self.roots)
                self.assertTrue(layout.root.exists())
        self.assertFalse(self.roots.versions.exists())

    def test_activation_refuses_unrelated_launcher_before_any_mutation(self) -> None:
        self.roots.launcher.parent.mkdir(parents=True)
        self.roots.launcher.write_text("# foreign\n", encoding="utf-8")
        layout = _bundle(self.base, "0.1.0")

        with self.assertRaises(InstallOwnershipError):
            activate_version(layout, roots=self.roots)

        self.assertEqual(
            self.roots.launcher.read_text(encoding="utf-8"),
            "# foreign\n",
        )
        self.assertTrue(layout.root.exists())
        self.assertFalse(self.roots.versions.exists())

    def test_activation_refuses_symlinked_launcher_ancestor(self) -> None:
        foreign = self.base / "foreign-local"
        foreign.mkdir()
        (self.user_home / ".local").parent.mkdir(parents=True, exist_ok=True)
        (self.user_home / ".local").symlink_to(foreign)
        layout = _bundle(self.base, "0.1.0")

        with self.assertRaises(InstallOwnershipError):
            activate_version(layout, roots=self.roots)

        self.assertEqual(list(foreign.iterdir()), [])
        self.assertTrue(layout.root.exists())

    def test_activation_refuses_foreign_or_malformed_current(self) -> None:
        foreign = self.base / "foreign"
        foreign.mkdir()
        cases = (
            "regular",
            "directory",
            "foreign-link",
            "dangling-link",
            "malformed-version-link",
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(dir=self.base) as selected:
                    local = Path(selected)
                    roots = InstallRoots(
                        local / ".hydra",
                        local / ".hydra" / "versions",
                        local / ".hydra" / "current",
                        local / ".local" / "bin" / "hydra-codex",
                    )
                    roots.home.mkdir(parents=True)
                    if case == "regular":
                        roots.current.write_text("foreign", encoding="utf-8")
                    elif case == "directory":
                        roots.current.mkdir()
                    elif case == "foreign-link":
                        roots.current.symlink_to(foreign)
                    elif case == "dangling-link":
                        roots.current.symlink_to(local / "missing")
                    else:
                        roots.versions.mkdir()
                        roots.current.symlink_to(roots.versions / "..")
                    layout = _bundle(local, "0.1.0")
                    with self.assertRaises(InstallOwnershipError):
                        activate_version(layout, roots=roots)
                    self.assertTrue(layout.root.exists())

    def test_activation_is_atomic_owned_and_existing_versions_are_immutable(self) -> None:
        installed = self.activate("0.1.0")

        self.assertEqual(self.roots.current.resolve(), installed.resolve())
        self.assertEqual(
            os.readlink(self.roots.launcher),
            str(self.roots.current / "bin" / "hydra-codex"),
        )
        lock = self.roots.home / "release.lock"
        self.assertTrue(stat.S_ISREG(lock.lstat().st_mode))
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertEqual(
            (installed / "bin" / "hydra-codex").read_text(encoding="utf-8"),
            "#!/bin/sh\n# original\n",
        )

        replacement = _bundle(self.base, "0.1.0", marker="replacement")
        repeated = activate_version(replacement, roots=self.roots)
        self.assertEqual(repeated, installed)
        self.assertEqual(
            (installed / "bin" / "hydra-codex").read_text(encoding="utf-8"),
            "#!/bin/sh\n# original\n",
        )
        self.assertTrue(replacement.root.exists())

    def test_activation_rejects_an_invalid_existing_version(self) -> None:
        installed = self.activate("0.1.0")
        invalid = self.roots.versions / "0.2.0"
        invalid.mkdir()
        (invalid / "VERSION").write_text("0.2.0\n", encoding="utf-8")
        replacement = _bundle(self.base, "0.2.0", marker="replacement")

        with self.assertRaises(InvalidBundle):
            activate_version(replacement, roots=self.roots)

        self.assertTrue(replacement.root.exists())
        self.assertEqual(self.roots.current.resolve(), installed.resolve())

    def test_refresh_failure_rolls_current_back_and_retains_candidate(self) -> None:
        previous = self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")

        def fail_refresh(_layout: BundleLayout) -> None:
            raise IntegrationError("private path must not be rendered")

        with self.assertRaises(IntegrationError):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=candidate,
                refresh_integration=fail_refresh,
                roots=self.roots,
            )

        self.assertEqual(self.roots.current.resolve(), previous.resolve())
        self.assertTrue((self.roots.versions / "0.2.0").is_dir())
        self.assertEqual(
            os.readlink(self.roots.launcher),
            str(self.roots.current / "bin" / "hydra-codex"),
        )

    def test_upgrade_check_success_and_repeat_are_idempotent(self) -> None:
        self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")
        refreshed: list[str] = []

        checked = upgrade(
            check=True,
            environ=self.environ,
            stdout=io.StringIO(),
            verified_candidate=candidate,
            roots=self.roots,
        )
        self.assertEqual(
            (checked.current_version, checked.latest_version, checked.update_available),
            ("0.1.0", "0.2.0", True),
        )
        self.assertTrue(candidate.root.exists())

        upgraded = upgrade(
            check=False,
            environ=self.environ,
            stdout=io.StringIO(),
            verified_candidate=candidate,
            refresh_integration=lambda layout: refreshed.append(layout.version),
            roots=self.roots,
        )
        repeated = upgrade(
            check=False,
            environ=self.environ,
            stdout=io.StringIO(),
            verified_candidate=validate_bundle(
                self.roots.versions / "0.2.0",
                expected_version="0.2.0",
            ),
            refresh_integration=lambda layout: refreshed.append(layout.version),
            roots=self.roots,
        )
        self.assertEqual(upgraded.current_version, "0.2.0")
        self.assertEqual(repeated.current_version, "0.2.0")
        self.assertEqual(refreshed, ["0.2.0", "0.2.0"])

    def test_interruption_after_bundle_move_recovers_previous_release(self) -> None:
        previous = self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")

        with patch(
            "hydra_codex.release_management._atomic_symlink",
            side_effect=SimulatedCrash,
        ):
            with self.assertRaises(SimulatedCrash):
                activate_version(candidate, roots=self.roots)
        self.assertTrue((self.roots.versions / "0.2.0").is_dir())

        malformed_root = self.base / "malformed-recovery"
        malformed_root.mkdir()
        malformed = BundleLayout(
            malformed_root,
            "../invalid",
            "darwin-arm64",
            malformed_root / "bin" / "hydra-codex",
            malformed_root / "marketplace",
        )
        with self.assertRaises(ValueError):
            activate_version(malformed, roots=self.roots)
        self.assertEqual(self.roots.current.resolve(), previous.resolve())
        self.assertFalse((self.roots.home / "release-journal.json").exists())

    def test_interruption_after_current_switch_recovers_previous_release(self) -> None:
        previous = self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")
        from hydra_codex import release_management

        real_link = release_management._atomic_symlink
        calls = 0

        def crash_on_launcher(link: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SimulatedCrash
            real_link(link, target)

        with patch(
            "hydra_codex.release_management._atomic_symlink",
            side_effect=crash_on_launcher,
        ):
            with self.assertRaises(SimulatedCrash):
                activate_version(candidate, roots=self.roots)
        self.assertEqual(self.roots.current.resolve().name, "0.2.0")

        self.activate("0.1.0", marker="unused")
        self.assertEqual(self.roots.current.resolve(), previous.resolve())
        self.assertTrue((self.roots.versions / "0.2.0").is_dir())

    def test_interruption_after_refresh_commit_keeps_the_new_release(self) -> None:
        self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")
        refreshed: list[str] = []

        with patch(
            "hydra_codex.release_management._clear_journal",
            side_effect=SimulatedCrash,
        ):
            with self.assertRaises(SimulatedCrash):
                upgrade(
                    check=False,
                    environ=self.environ,
                    stdout=io.StringIO(),
                    verified_candidate=candidate,
                    refresh_integration=lambda layout: refreshed.append(layout.version),
                    roots=self.roots,
                )

        self.assertEqual(refreshed, ["0.2.0"])
        self.assertEqual(self.roots.current.resolve().name, "0.2.0")
        malformed_root = self.base / "post-refresh-invalid"
        malformed_root.mkdir()
        malformed = BundleLayout(
            malformed_root,
            "../invalid",
            "darwin-arm64",
            malformed_root / "bin" / "hydra-codex",
            malformed_root / "marketplace",
        )
        with self.assertRaises(ValueError):
            activate_version(malformed, roots=self.roots)
        self.assertEqual(self.roots.current.resolve().name, "0.2.0")
        self.assertFalse((self.roots.home / "release-journal.json").exists())

    def test_concurrent_lifecycle_calls_are_refused(self) -> None:
        self.activate("0.1.0")
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def holding_detach() -> None:
            entered.set()
            release.wait(5)

        def first() -> None:
            try:
                uninstall(
                    keep_cli=True,
                    environ=self.environ,
                    detach_integration=holding_detach,
                    roots=self.roots,
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(LifecycleBusyError):
                uninstall(
                    keep_cli=True,
                    environ=self.environ,
                    detach_integration=lambda: None,
                    roots=self.roots,
                )
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(errors, [])

    def test_detach_failure_makes_zero_cli_mutations(self) -> None:
        installed = self.activate("0.1.0")
        current_relation = os.readlink(self.roots.current)
        launcher_relation = os.readlink(self.roots.launcher)

        def fail() -> None:
            raise IntegrationError("detach failed")

        with self.assertRaises(IntegrationError):
            uninstall(
                keep_cli=False,
                environ=self.environ,
                detach_integration=fail,
                roots=self.roots,
            )

        self.assertEqual(self.roots.current.resolve(), installed.resolve())
        self.assertEqual(os.readlink(self.roots.current), current_relation)
        self.assertEqual(os.readlink(self.roots.launcher), launcher_relation)

    def test_keep_cli_stops_after_successful_detachment(self) -> None:
        installed = self.activate("0.1.0")
        detached: list[bool] = []
        uninstall(
            keep_cli=True,
            environ=self.environ,
            detach_integration=lambda: detached.append(True),
            roots=self.roots,
        )
        self.assertEqual(detached, [True])
        self.assertEqual(self.roots.current.resolve(), installed.resolve())
        self.assertTrue(self.roots.launcher.is_symlink())

    def test_keep_cli_detaches_even_when_cli_state_is_foreign(self) -> None:
        self.roots.launcher.parent.mkdir(parents=True)
        self.roots.launcher.write_text("foreign", encoding="utf-8")
        detached: list[bool] = []

        uninstall(
            keep_cli=True,
            environ=self.environ,
            detach_integration=lambda: detached.append(True),
            roots=self.roots,
        )

        self.assertEqual(detached, [True])
        self.assertEqual(
            self.roots.launcher.read_text(encoding="utf-8"),
            "foreign",
        )

    def test_uninstall_preserves_private_data_project_and_unknown_entries(self) -> None:
        installed = self.activate("0.1.0")
        data = self.user_home / ".local" / "share" / "hydra"
        project = self.base / "project" / ".hydra"
        for path, content in (
            (data / "telemetry.sqlite3", "db"),
            (data / "installation.key", "key"),
            (data / "spool" / "event.json", "event"),
            (data / "receipts" / "pilot.json", "receipt"),
            (project / "project.toml", "schema_version = 1"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        unknown_file = self.roots.versions / "operator-notes.txt"
        unknown_file.write_text("preserve", encoding="utf-8")
        unknown_directory = self.roots.versions / "future-layout"
        unknown_directory.mkdir()
        before = {
            path: path.read_bytes()
            for path in (*data.rglob("*"), *project.rglob("*"))
            if path.is_file()
        }
        detached: list[bool] = []

        uninstall(
            keep_cli=False,
            environ=self.environ,
            detach_integration=lambda: detached.append(True),
            roots=self.roots,
        )

        self.assertEqual(detached, [True])
        self.assertFalse(self.roots.current.exists())
        self.assertFalse(self.roots.current.is_symlink())
        self.assertFalse(self.roots.launcher.exists())
        self.assertFalse(self.roots.launcher.is_symlink())
        self.assertFalse(installed.exists())
        self.assertEqual(unknown_file.read_text(encoding="utf-8"), "preserve")
        self.assertTrue(unknown_directory.is_dir())
        self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == "__main__":
    unittest.main()
