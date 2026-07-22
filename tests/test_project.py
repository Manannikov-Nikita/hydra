from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hydra_codex.project import resolve_project


PROJECT_CONFIG = 'project_id = "hprj_4db8fca38ef042f3"\ntelemetry = "hybrid"\n'


class ProjectResolutionTests(unittest.TestCase):
    def project(self, project_id: str, *, display_name: str | None = None) -> Path:
        root = Path(self.temporary_directory.name) / project_id
        config = root / ".hydra" / "project.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        suffix = "" if display_name is None else f"display_name = {json.dumps(display_name)}\n"
        config.write_text(
            f'project_id = {project_id!r}\n{suffix}', encoding="utf-8",
        )
        return root

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_project_config_accepts_sanitized_display_name(self) -> None:
        root = self.project("project-a", display_name="  Hydra   Core  ")

        self.assertEqual(resolve_project(root).display_name, "Hydra Core")
        normalized = self.project("project-b", display_name=" Cafe\u0301 ")
        self.assertEqual(resolve_project(normalized).display_name, "Café")

    def test_project_config_rejects_control_bidi_and_overlong_names(self) -> None:
        for value in ("Hydra\nCore", "Hydra\u202eCore", "x" * 81):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                resolve_project(self.project("project-a", display_name=value))

    def test_resolves_project_from_a_nested_worktree_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project"
            config = root / ".hydra" / "project.toml"
            config.parent.mkdir(parents=True)
            config.write_text(PROJECT_CONFIG, encoding="utf-8")
            nested = root / "src" / "feature"
            nested.mkdir(parents=True)

            resolved = resolve_project(nested)

            self.assertEqual(resolved.project_id, "hprj_4db8fca38ef042f3")
            self.assertEqual(resolved.project_root, root.resolve())
            self.assertEqual(resolved.worktree_path, Path("src/feature"))

    def test_two_worktrees_share_identity_but_preserve_their_observed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            first = base / "first-worktree"
            second = base / "second-worktree"
            for worktree in (first, second):
                config = worktree / ".hydra" / "project.toml"
                config.parent.mkdir(parents=True)
                config.write_text(PROJECT_CONFIG, encoding="utf-8")
            (first / "feature" / "alpha").mkdir(parents=True)
            (second / "review" / "beta").mkdir(parents=True)

            first_resolution = resolve_project(first / "feature" / "alpha")
            second_resolution = resolve_project(second / "review" / "beta")

            self.assertEqual(first_resolution.project_id, second_resolution.project_id)
            self.assertEqual(first_resolution.worktree_path, Path("feature/alpha"))
            self.assertEqual(second_resolution.worktree_path, Path("review/beta"))
