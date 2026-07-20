from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hydra_codex.project import resolve_project


PROJECT_CONFIG = 'project_id = "hprj_4db8fca38ef042f3"\ntelemetry = "hybrid"\n'


class ProjectResolutionTests(unittest.TestCase):
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
