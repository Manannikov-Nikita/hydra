from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
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


def _inventory(root: Path) -> tuple[tuple[str, int, bytes | None], ...]:
    if not root.exists():
        return ()
    rows = []
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        content = path.read_bytes() if stat.S_ISREG(mode) else None
        rows.append((path.relative_to(root).as_posix(), mode, content))
    return tuple(sorted(rows))


class ReleaseManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.user_home = self.base / "user"
        self.user_home.mkdir()
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
            environ=self.environ,
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
                    activate_version(
                        layout,
                        roots=self.roots,
                        environ=self.environ,
                    )
        self.assertFalse(self.roots.versions.exists())

    def test_activation_rejects_nonportable_but_complete_version_bundles(self) -> None:
        for version in ("bad version", "v" * 129, "rélease"):
            with self.subTest(version=version):
                layout = _bundle(self.base, version)
                with self.assertRaises(ValueError):
                    activate_version(
                        layout,
                        roots=self.roots,
                        environ=self.environ,
                    )
                self.assertTrue(layout.root.exists())
        self.assertFalse(self.roots.versions.exists())

    def test_activation_refuses_unrelated_launcher_before_any_mutation(self) -> None:
        self.roots.launcher.parent.mkdir(parents=True)
        self.roots.launcher.write_text("# foreign\n", encoding="utf-8")
        layout = _bundle(self.base, "0.1.0")

        with self.assertRaises(InstallOwnershipError):
            activate_version(layout, roots=self.roots, environ=self.environ)

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
            activate_version(layout, roots=self.roots, environ=self.environ)

        self.assertEqual(list(foreign.iterdir()), [])
        self.assertTrue(layout.root.exists())

    def test_activation_refuses_a_symlinked_selected_home(self) -> None:
        actual_home = self.base / "actual-home"
        actual_home.mkdir()
        selected_home = self.base / "selected-home"
        selected_home.symlink_to(actual_home)
        roots = default_install_roots(selected_home)
        layout = _bundle(self.base, "0.1.0")

        with self.assertRaises(InstallOwnershipError) as raised:
            activate_version(
                layout,
                roots=roots,
                environ={"HOME": str(roots.home.parent)},
            )

        self.assertEqual(_inventory(actual_home), ())
        self.assertNotIn(str(actual_home), str(raised.exception))

    def test_activation_refuses_publicly_writable_hydra_home(self) -> None:
        self.roots.home.mkdir(parents=True)
        self.roots.home.chmod(0o777)
        before = _inventory(self.user_home)
        layout = _bundle(self.base, "0.1.0")

        with self.assertRaises(InstallOwnershipError) as raised:
            activate_version(layout, roots=self.roots, environ=self.environ)

        self.assertEqual(_inventory(self.user_home), before)
        self.assertNotIn(str(self.user_home), str(raised.exception))

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
                        activate_version(
                            layout,
                            roots=roots,
                            environ={"HOME": str(roots.home.parent)},
                        )
                    self.assertTrue(layout.root.exists())

    def test_activation_is_atomic_owned_and_existing_versions_are_immutable(self) -> None:
        installed = self.activate("0.1.0")

        self.assertEqual(self.roots.current.resolve(), installed.resolve())
        self.assertEqual(
            os.readlink(self.roots.launcher),
            str(self.roots.current / "bin" / "hydra-codex"),
        )
        self.assertEqual(
            (installed / "bin" / "hydra-codex").read_text(encoding="utf-8"),
            "#!/bin/sh\n# original\n",
        )

        replacement = _bundle(self.base, "0.1.0", marker="replacement")
        repeated = activate_version(
            replacement,
            roots=self.roots,
            environ=self.environ,
        )
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
            activate_version(replacement, roots=self.roots, environ=self.environ)

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
        self.assertEqual(
            json.loads(
                (self.roots.home / "release-journal.json").read_text(
                    encoding="utf-8",
                ),
            ),
            {
                "new_version": "0.2.0",
                "phase": "refreshing",
                "previous_version": "0.1.0",
                "schema_version": 1,
            },
        )

    def test_cross_layer_rollback_state_is_retained_until_retry_converges(
        self,
    ) -> None:
        previous = self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")
        integration_journal = self.roots.home / "integration-journal.test"

        def fail_refresh(_layout: BundleLayout) -> None:
            integration_journal.write_text("rollback-required\n", encoding="utf-8")
            raise IntegrationError("private integration failure")

        with self.assertRaises(IntegrationError):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=candidate,
                expected_current_version="0.1.0",
                refresh_integration=fail_refresh,
                roots=self.roots,
            )

        self.assertEqual(self.roots.current.resolve(), previous.resolve())
        self.assertTrue(integration_journal.exists())
        release_journal = self.roots.home / "release-journal.json"
        self.assertEqual(
            json.loads(release_journal.read_text(encoding="utf-8"))["phase"],
            "refreshing",
        )

        refreshed: list[str] = []

        def converge(layout: BundleLayout) -> None:
            integration_journal.unlink(missing_ok=True)
            refreshed.append(layout.version)

        retry = _bundle(self.base, "0.2.0", marker="retry")
        upgrade(
            check=False,
            environ=self.environ,
            stdout=io.StringIO(),
            verified_candidate=retry,
            expected_current_version="0.1.0",
            refresh_integration=converge,
            roots=self.roots,
        )

        self.assertEqual(self.roots.current.resolve().name, "0.2.0")
        self.assertFalse(integration_journal.exists())
        self.assertFalse(release_journal.exists())
        self.assertEqual(refreshed, ["0.2.0"])

    def test_upgrade_rejects_active_version_drift_and_downgrades(self) -> None:
        active = self.activate("0.2.0")
        newer = _bundle(self.base, "0.3.0")

        with self.assertRaises(InstallOwnershipError):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=newer,
                expected_current_version="0.1.0",
                refresh_integration=lambda _layout: None,
                roots=self.roots,
            )
        self.assertEqual(self.roots.current.resolve(), active.resolve())
        self.assertTrue(newer.root.exists())

        older = _bundle(self.base, "0.1.0")
        with self.assertRaises(InstallOwnershipError):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=older,
                expected_current_version="0.2.0",
                refresh_integration=lambda _layout: None,
                roots=self.roots,
            )
        self.assertEqual(self.roots.current.resolve(), active.resolve())
        self.assertTrue(older.root.exists())

    def test_recovery_cannot_be_followed_by_an_older_candidate(self) -> None:
        self.activate("0.1.0")
        pending = _bundle(self.base, "0.3.0")
        with self.assertRaises(IntegrationError):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=pending,
                expected_current_version="0.1.0",
                refresh_integration=lambda _layout: (_ for _ in ()).throw(
                    IntegrationError("refresh failed"),
                ),
                roots=self.roots,
            )

        older = _bundle(self.base, "0.2.0")
        reconciled: list[str] = []
        with self.assertRaises(InstallOwnershipError):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=older,
                expected_current_version="0.1.0",
                refresh_integration=lambda layout: reconciled.append(layout.version),
                roots=self.roots,
            )

        self.assertEqual(reconciled, ["0.3.0"])
        self.assertEqual(self.roots.current.resolve().name, "0.3.0")
        self.assertTrue(older.root.exists())
        self.assertFalse((self.roots.home / "release-journal.json").exists())

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
        self.assertEqual(refreshed, ["0.2.0"])

    def test_interruption_after_bundle_move_recovers_previous_release(self) -> None:
        previous = self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")

        with patch(
            "hydra_codex.release_management._atomic_symlink",
            side_effect=SimulatedCrash,
        ):
            with self.assertRaises(SimulatedCrash):
                activate_version(candidate, roots=self.roots, environ=self.environ)
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
            activate_version(malformed, roots=self.roots, environ=self.environ)
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
                activate_version(candidate, roots=self.roots, environ=self.environ)
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
            activate_version(malformed, roots=self.roots, environ=self.environ)
        self.assertEqual(self.roots.current.resolve().name, "0.2.0")
        self.assertFalse((self.roots.home / "release-journal.json").exists())

    def test_crash_at_first_refresh_commit_write_reconciles_candidate(self) -> None:
        self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")
        plugin = ["0.1.0"]
        refreshes: list[str] = []
        from hydra_codex import release_management

        real_write = release_management._write_journal
        crashed = False

        def refresh(layout: BundleLayout) -> None:
            plugin[0] = layout.version
            refreshes.append(layout.version)

        def crash_first_commit(roots: InstallRoots, journal) -> None:
            nonlocal crashed
            if journal.phase == "refresh_committed" and not crashed:
                crashed = True
                self.assertEqual(plugin[0], "0.2.0")
                raise SimulatedCrash
            real_write(roots, journal)

        with patch(
            "hydra_codex.release_management._write_journal",
            side_effect=crash_first_commit,
        ):
            with self.assertRaises(SimulatedCrash):
                upgrade(
                    check=False,
                    environ=self.environ,
                    stdout=io.StringIO(),
                    verified_candidate=candidate,
                    refresh_integration=refresh,
                    roots=self.roots,
                )

        plugin[0] = "0.1.0"
        installed = validate_bundle(self.roots.versions / "0.2.0")
        upgrade(
            check=False,
            environ=self.environ,
            stdout=io.StringIO(),
            verified_candidate=installed,
            refresh_integration=refresh,
            roots=self.roots,
        )
        self.assertEqual(plugin[0], "0.2.0")
        self.assertEqual(self.roots.current.resolve().name, "0.2.0")
        self.assertEqual(refreshes, ["0.2.0", "0.2.0"])

    def test_crash_during_initial_refresh_is_reconciled_on_retry(self) -> None:
        self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")

        def crash(_layout: BundleLayout) -> None:
            raise SimulatedCrash

        with self.assertRaises(SimulatedCrash):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=candidate,
                refresh_integration=crash,
                roots=self.roots,
            )

        reconciled: list[str] = []
        installed = validate_bundle(self.roots.versions / "0.2.0")
        upgrade(
            check=False,
            environ=self.environ,
            stdout=io.StringIO(),
            verified_candidate=installed,
            refresh_integration=lambda layout: reconciled.append(layout.version),
            roots=self.roots,
        )
        self.assertEqual(self.roots.current.resolve().name, "0.2.0")
        self.assertEqual(reconciled, ["0.2.0"])

    def test_failed_recovery_retry_preserves_candidate_for_next_retry(self) -> None:
        self.activate("0.1.0")
        candidate = _bundle(self.base, "0.2.0")

        with self.assertRaises(SimulatedCrash):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=candidate,
                refresh_integration=lambda _layout: (_ for _ in ()).throw(
                    SimulatedCrash,
                ),
                roots=self.roots,
            )
        installed = validate_bundle(self.roots.versions / "0.2.0")
        with self.assertRaises(IntegrationError):
            upgrade(
                check=False,
                environ=self.environ,
                stdout=io.StringIO(),
                verified_candidate=installed,
                refresh_integration=lambda _layout: (_ for _ in ()).throw(
                    IntegrationError("retry failed"),
                ),
                roots=self.roots,
            )
        self.assertEqual(self.roots.current.resolve().name, "0.1.0")
        self.assertTrue((self.roots.home / "release-journal.json").exists())

        upgrade(
            check=False,
            environ=self.environ,
            stdout=io.StringIO(),
            verified_candidate=installed,
            refresh_integration=lambda _layout: None,
            roots=self.roots,
        )
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

    def test_concurrent_empty_uninstalls_share_home_coordination(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        before = _inventory(self.user_home)

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
        self.assertEqual(_inventory(self.user_home), before)

    def test_empty_uninstall_and_activation_share_home_lock_with_same_xdg(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        candidate = _bundle(self.base, "0.1.0")
        xdg_data = self.base / "same-xdg"
        environ = {
            "HOME": str(self.user_home),
            "XDG_DATA_HOME": str(xdg_data),
        }
        before = _inventory(self.user_home)
        platform = patch("hydra_codex.platform_paths.sys.platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)

        def holding_detach() -> None:
            entered.set()
            release.wait(5)

        def first() -> None:
            try:
                uninstall(
                    keep_cli=True,
                    environ=environ,
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
                activate_version(candidate, roots=self.roots, environ=environ)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(candidate.root.exists())
        self.assertFalse(self.roots.current.exists())
        self.assertEqual(_inventory(self.user_home), before)
        self.assertFalse(xdg_data.exists())

    def test_empty_uninstall_and_activation_share_home_lock_across_xdg(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        candidate = _bundle(self.base, "0.1.0")
        first_xdg = self.base / "first-xdg"
        second_xdg = self.base / "second-xdg"
        first_environ = {
            "HOME": str(self.user_home),
            "XDG_DATA_HOME": str(first_xdg),
        }
        second_environ = {
            "HOME": str(self.user_home),
            "XDG_DATA_HOME": str(second_xdg),
        }
        before = _inventory(self.user_home)
        platform = patch("hydra_codex.platform_paths.sys.platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)

        def holding_detach() -> None:
            entered.set()
            release.wait(5)

        def first() -> None:
            try:
                uninstall(
                    keep_cli=True,
                    environ=first_environ,
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
                activate_version(
                    candidate,
                    roots=self.roots,
                    environ=second_environ,
                )
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(candidate.root.exists())
        self.assertFalse(self.roots.current.exists())
        self.assertEqual(_inventory(self.user_home), before)
        self.assertFalse(first_xdg.exists())
        self.assertFalse(second_xdg.exists())

    def test_detach_failure_makes_zero_cli_mutations(self) -> None:
        installed = self.activate("0.1.0")
        before = _inventory(self.user_home)

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
        self.assertEqual(_inventory(self.user_home), before)

    def test_detach_failure_from_empty_home_creates_zero_cli_paths(self) -> None:
        before = _inventory(self.user_home)

        with self.assertRaises(IntegrationError):
            uninstall(
                keep_cli=False,
                environ=self.environ,
                detach_integration=lambda: (_ for _ in ()).throw(
                    IntegrationError("detach failed"),
                ),
                roots=self.roots,
            )

        self.assertEqual(_inventory(self.user_home), before)
        self.assertFalse(self.roots.home.exists())

    def test_detach_failure_from_legacy_home_creates_zero_cli_paths(self) -> None:
        legacy = self.roots.home / "legacy-note"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("preserve", encoding="utf-8")
        legacy.parent.chmod(0o700)
        before = _inventory(self.user_home)

        with self.assertRaises(IntegrationError):
            uninstall(
                keep_cli=True,
                environ=self.environ,
                detach_integration=lambda: (_ for _ in ()).throw(
                    IntegrationError("detach failed"),
                ),
                roots=self.roots,
            )

        self.assertEqual(_inventory(self.user_home), before)

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

    def test_keep_cli_refuses_foreign_cli_before_detachment(self) -> None:
        self.roots.launcher.parent.mkdir(parents=True)
        self.roots.launcher.write_text("foreign", encoding="utf-8")
        detached: list[bool] = []

        with self.assertRaises(InstallOwnershipError):
            uninstall(
                keep_cli=True,
                environ=self.environ,
                detach_integration=lambda: detached.append(True),
                roots=self.roots,
            )

        self.assertEqual(detached, [])
        self.assertEqual(
            self.roots.launcher.read_text(encoding="utf-8"),
            "foreign",
        )

    def test_wrong_uid_home_lock_is_rejected_before_detachment(self) -> None:
        self.activate("0.1.0")
        detached: list[bool] = []
        real_fstat = os.fstat

        def foreign_fstat(descriptor: int):
            value = real_fstat(descriptor)
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_uid=os.getuid() + 1,
            )

        with patch(
            "hydra_codex.release_management.os.fstat",
            side_effect=foreign_fstat,
        ):
            with self.assertRaises(InstallOwnershipError) as raised:
                uninstall(
                    keep_cli=True,
                    environ=self.environ,
                    detach_integration=lambda: detached.append(True),
                    roots=self.roots,
                )

        self.assertEqual(detached, [])
        self.assertNotIn(str(self.user_home), str(raised.exception))

    def test_uninstall_rejects_unowned_versions_root_before_detach_or_unlink(self) -> None:
        self.activate("0.1.0")
        actual_versions = self.roots.home / "actual-versions"
        self.roots.versions.rename(actual_versions)
        self.roots.versions.symlink_to(actual_versions)
        current_relation = os.readlink(self.roots.current)
        launcher_relation = os.readlink(self.roots.launcher)
        detached: list[bool] = []

        with self.assertRaises(InstallOwnershipError):
            uninstall(
                keep_cli=False,
                environ=self.environ,
                detach_integration=lambda: detached.append(True),
                roots=self.roots,
            )

        self.assertEqual(detached, [])
        self.assertEqual(os.readlink(self.roots.current), current_relation)
        self.assertEqual(os.readlink(self.roots.launcher), launcher_relation)

    def test_uninstall_refuses_nonregular_versions_root_before_detachment(self) -> None:
        self.roots.home.mkdir(parents=True)
        self.roots.home.chmod(0o700)
        self.roots.versions.write_text("foreign", encoding="utf-8")
        before = _inventory(self.user_home)
        detached: list[bool] = []

        with self.assertRaises(InstallOwnershipError):
            uninstall(
                keep_cli=False,
                environ=self.environ,
                detach_integration=lambda: detached.append(True),
                roots=self.roots,
            )

        self.assertEqual(detached, [])
        self.assertEqual(_inventory(self.user_home), before)

    def test_uninstall_refuses_foreign_uid_versions_root_before_detachment(self) -> None:
        self.activate("0.1.0")
        detached: list[bool] = []
        real_lstat = Path.lstat

        def foreign_versions(path: Path):
            value = real_lstat(path)
            if path == self.roots.versions:
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_uid=os.getuid() + 1,
                    st_size=value.st_size,
                )
            return value

        with patch.object(Path, "lstat", autospec=True, side_effect=foreign_versions):
            with self.assertRaises(InstallOwnershipError):
                uninstall(
                    keep_cli=False,
                    environ=self.environ,
                    detach_integration=lambda: detached.append(True),
                    roots=self.roots,
                )

        self.assertEqual(detached, [])

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
