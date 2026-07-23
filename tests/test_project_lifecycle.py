from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from hydra_codex.project_config import ProjectConfigError, read_project_config
from hydra_codex.project_lifecycle import (
    ProjectConfirmationError,
    UnsafeProjectTarget,
    initialize_project,
    uninitialize_project,
)
from hydra_codex.status import collect_status


def config_path(project: Path) -> Path:
    return project / ".hydra" / "project.toml"


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_concurrent_init_converges_on_one_identity(self) -> None:
        barrier = threading.Barrier(2)
        identities = iter(
            ("hprj_1111111111111111", "hprj_2222222222222222"),
        )
        identity_lock = threading.Lock()

        def project_id_factory() -> str:
            with identity_lock:
                project_id = next(identities)
            barrier.wait(timeout=5)
            return project_id

        def initialize():
            return initialize_project(
                self.project,
                project_id_factory=project_id_factory,
                home=self.home,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: initialize(), range(2)))

        stored = read_project_config(config_path(self.project)).project_id
        self.assertEqual({result.project_id for result in results}, {stored})
        self.assertEqual(sum(result.changed for result in results), 1)
        self.assertEqual(list((self.project / ".hydra").glob(".project.toml-*")), [])

    def test_repeated_init_preserves_bytes_and_legacy_schema(self) -> None:
        first = initialize_project(self.project, home=self.home)
        original = config_path(self.project).read_bytes()
        second = initialize_project(self.project, name="ignored", home=self.home)

        self.assertFalse(second.changed)
        self.assertEqual(config_path(self.project).read_bytes(), original)
        self.assertEqual(second.project_id, first.project_id)

        legacy = self.root / "legacy"
        (legacy / ".hydra").mkdir(parents=True)
        legacy_bytes = b'project_id = "hprj_0123456789abcdef"\n'
        config_path(legacy).write_bytes(legacy_bytes)
        result = initialize_project(legacy, home=self.home)
        self.assertFalse(result.changed)
        self.assertEqual(config_path(legacy).read_bytes(), legacy_bytes)

    def test_new_config_is_private_and_normalizes_display_name(self) -> None:
        result = initialize_project(
            self.project,
            name="  Hydra   Caf\u0065\u0301  ",
            project_id_factory=lambda: "hprj_0123456789abcdef",
            home=self.home,
        )

        self.assertTrue(result.changed)
        self.assertEqual(stat.S_IMODE(config_path(self.project).stat().st_mode), 0o600)
        parsed = read_project_config(config_path(self.project))
        self.assertEqual(parsed.display_name, "Hydra Caf\u00e9")
        self.assertEqual(parsed.telemetry, "hybrid")
        self.assertEqual(parsed.schema_version, 1)

    def test_target_resolution_prefers_hydra_then_git_then_exact_directory(self) -> None:
        hydra = self.root / "hydra"
        nested = hydra / "src" / "feature"
        nested.mkdir(parents=True)
        initialize_project(hydra, home=self.home)
        self.assertEqual(
            initialize_project(nested, home=self.home).project_root,
            hydra.resolve(),
        )

        repository = self.root / "repository"
        (repository / ".git").mkdir(parents=True)
        repository_nested = repository / "src" / "feature"
        repository_nested.mkdir(parents=True)
        self.assertEqual(
            initialize_project(repository_nested, home=self.home).project_root,
            repository.resolve(),
        )

        exact = self.root / "plain" / "nested"
        exact.mkdir(parents=True)
        self.assertEqual(
            initialize_project(exact, home=self.home).project_root,
            exact.resolve(),
        )

    def test_protected_and_symlinked_targets_are_rejected_without_mutation(self) -> None:
        for target in (Path("/"), self.home):
            with self.subTest(target=target), self.assertRaises(UnsafeProjectTarget):
                initialize_project(target, home=self.home)

        external = self.root / "external"
        external.mkdir()
        (self.project / ".hydra").symlink_to(external, target_is_directory=True)
        with self.assertRaises(UnsafeProjectTarget):
            initialize_project(self.project, home=self.home)
        self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_target_and_project_descendant_are_rejected_before_resolution(
        self,
    ) -> None:
        external_target = self.root / "external-target"
        external_target.mkdir()
        target_link = self.root / "target-link"
        target_link.symlink_to(external_target, target_is_directory=True)

        with self.assertRaises(UnsafeProjectTarget):
            initialize_project(target_link, home=self.home)
        self.assertEqual(list(external_target.iterdir()), [])

        (self.project / ".git").mkdir()
        external_descendant = self.root / "external-descendant"
        (external_descendant / "nested").mkdir(parents=True)
        (self.project / "linked").symlink_to(
            external_descendant,
            target_is_directory=True,
        )

        with self.assertRaises(UnsafeProjectTarget):
            initialize_project(self.project / "linked" / "nested", home=self.home)
        self.assertFalse((self.project / ".hydra").exists())
        self.assertEqual(list(external_descendant.iterdir()), [external_descendant / "nested"])

    def test_symlinked_prefix_above_project_boundary_is_tolerated(self) -> None:
        real_prefix = self.root / "real-prefix"
        project = real_prefix / "project"
        project.mkdir(parents=True)
        alias = self.root / "prefix-alias"
        alias.symlink_to(real_prefix, target_is_directory=True)

        result = initialize_project(alias / "project", home=self.home)

        self.assertEqual(result.project_root, project.resolve())
        self.assertTrue(config_path(project).is_file())

    def test_malformed_existing_config_is_preserved_and_rejected(self) -> None:
        (self.project / ".hydra").mkdir()
        original = b'project_id = "/private/not-canonical"\n'
        config_path(self.project).write_bytes(original)

        with self.assertRaises(ProjectConfigError):
            initialize_project(self.project, home=self.home)

        self.assertEqual(config_path(self.project).read_bytes(), original)

    def test_publish_failure_removes_its_temporary_file_and_owned_empty_directory(
        self,
    ) -> None:
        with mock.patch(
            "hydra_codex.project_lifecycle.os.link",
            side_effect=OSError("link failed"),
        ):
            with self.assertRaises(OSError):
                initialize_project(self.project, home=self.home)

        hydra = self.project / ".hydra"
        self.assertFalse(hydra.exists())
        self.assertFalse(config_path(self.project).exists())

    def test_publish_failure_never_removes_preexisting_or_newly_nonempty_directory(
        self,
    ) -> None:
        hydra = self.project / ".hydra"
        hydra.mkdir()
        with mock.patch(
            "hydra_codex.project_lifecycle.os.link",
            side_effect=OSError("link failed"),
        ):
            with self.assertRaises(OSError):
                initialize_project(self.project, home=self.home)
        self.assertTrue(hydra.is_dir())
        self.assertEqual(list(hydra.iterdir()), [])

        hydra.rmdir()
        sidecar = hydra / "concurrent-entry"

        def fail_after_concurrent_write(*_args) -> None:
            sidecar.write_text("keep", encoding="utf-8")
            raise OSError("link failed")

        with mock.patch(
            "hydra_codex.project_lifecycle.os.link",
            side_effect=fail_after_concurrent_write,
        ):
            with self.assertRaises(OSError):
                initialize_project(self.project, home=self.home)
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "keep")

    def test_config_symlink_fails_closed_for_init_status_and_uninit(self) -> None:
        hydra = self.project / ".hydra"
        hydra.mkdir()
        external = self.root / "external-project.toml"
        original = b'project_id = "hprj_0123456789abcdef"\n'
        external.write_bytes(original)
        config_path(self.project).symlink_to(external)

        operations = (
            lambda: initialize_project(self.project, home=self.home),
            lambda: collect_status(self.project, environ={"HOME": str(self.home)}),
            lambda: uninitialize_project(
                self.project,
                confirmation="remove hydra project",
                home=self.home,
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ProjectConfigError):
                operation()

        self.assertTrue(config_path(self.project).is_symlink())
        self.assertEqual(external.read_bytes(), original)

    def test_fifo_and_socket_configs_fail_closed_without_opening(self) -> None:
        hydra = self.project / ".hydra"
        hydra.mkdir()
        path = config_path(self.project)
        operations = (
            lambda: initialize_project(self.project, home=self.home),
            lambda: collect_status(self.project, environ={"HOME": str(self.home)}),
            lambda: uninitialize_project(
                self.project,
                confirmation="remove hydra project",
                home=self.home,
            ),
        )

        os.mkfifo(path)
        try:
            with mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("nonregular config must not be opened"),
            ):
                for operation in operations:
                    with self.subTest(kind="fifo", operation=operation):
                        with self.assertRaises(ProjectConfigError):
                            operation()
        finally:
            path.unlink(missing_ok=True)

        path.touch()
        regular_metadata = path.lstat()
        socket_metadata = os.stat_result(
            (stat.S_IFSOCK | 0o600, *regular_metadata[1:]),
        )
        real_lstat = Path.lstat

        def socket_lstat(candidate: Path):
            if candidate == path:
                return socket_metadata
            return real_lstat(candidate)

        with (
            mock.patch.object(Path, "lstat", autospec=True, side_effect=socket_lstat),
            mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("nonregular config must not be opened"),
            ),
        ):
            for operation in operations:
                with self.subTest(kind="socket", operation=operation):
                    with self.assertRaises(ProjectConfigError):
                        operation()

    def test_uninit_requires_exact_confirmation_and_valid_config(self) -> None:
        initialized = initialize_project(self.project, home=self.home)
        for confirmation in ("", "remove Hydra project", " remove hydra project"):
            with self.subTest(confirmation=confirmation):
                with self.assertRaises(ProjectConfirmationError):
                    uninitialize_project(
                        self.project,
                        confirmation=confirmation,
                        home=self.home,
                    )
        self.assertTrue(config_path(self.project).exists())

        result = uninitialize_project(
            self.project,
            confirmation="remove hydra project",
            home=self.home,
        )
        self.assertTrue(result.changed)
        self.assertEqual(result.project_id, initialized.project_id)
        self.assertFalse((self.project / ".hydra").exists())

        repeated = uninitialize_project(
            self.project,
            confirmation="remove hydra project",
            home=self.home,
        )
        self.assertFalse(repeated.changed)
        self.assertEqual(repeated.project_id, "")

    def test_uninit_removes_only_config_and_preserves_nonempty_hydra_directory(self) -> None:
        initialized = initialize_project(self.project, home=self.home)
        sidecar = self.project / ".hydra" / "keep.txt"
        sidecar.write_text("keep", encoding="utf-8")

        result = uninitialize_project(
            self.project,
            confirmation="remove hydra project",
            home=self.home,
        )

        self.assertEqual(result.project_id, initialized.project_id)
        self.assertTrue(sidecar.is_file())
        self.assertFalse(config_path(self.project).exists())

    def test_uninit_refuses_malformed_config_without_removing_it(self) -> None:
        (self.project / ".hydra").mkdir()
        config_path(self.project).write_text("unknown = true\n", encoding="utf-8")

        with self.assertRaises(ProjectConfigError):
            uninitialize_project(
                self.project,
                confirmation="remove hydra project",
                home=self.home,
            )

        self.assertTrue(config_path(self.project).exists())


if __name__ == "__main__":
    unittest.main()
