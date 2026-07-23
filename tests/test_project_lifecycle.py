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

    def test_malformed_existing_config_is_preserved_and_rejected(self) -> None:
        (self.project / ".hydra").mkdir()
        original = b'project_id = "/private/not-canonical"\n'
        config_path(self.project).write_bytes(original)

        with self.assertRaises(ProjectConfigError):
            initialize_project(self.project, home=self.home)

        self.assertEqual(config_path(self.project).read_bytes(), original)

    def test_publish_failure_removes_its_temporary_file(self) -> None:
        with mock.patch(
            "hydra_codex.project_lifecycle.os.link",
            side_effect=OSError("link failed"),
        ):
            with self.assertRaises(OSError):
                initialize_project(self.project, home=self.home)

        hydra = self.project / ".hydra"
        self.assertEqual(list(hydra.glob(".project.toml-*")), [])
        self.assertFalse(config_path(self.project).exists())

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
