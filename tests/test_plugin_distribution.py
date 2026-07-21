from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
import tomllib
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "hydra-codex"
INSTALLED_PREFIX = "share/hydra-codex/plugins/hydra-codex"


def _inventory(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class PluginDistributionContentTests(unittest.TestCase):
    def test_build_manifest_maps_every_canonical_plugin_file_once(self) -> None:
        expected = set(_inventory(PLUGIN_ROOT))
        configuration = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
        data_files = configuration["tool"]["setuptools"]["data-files"]
        mapped: list[str] = []
        for destination, patterns in data_files.items():
            if not destination.startswith(INSTALLED_PREFIX):
                continue
            relative_directory = destination.removeprefix(INSTALLED_PREFIX).lstrip("/")
            for pattern in patterns:
                for source in sorted(ROOT.glob(pattern)):
                    if source.is_file():
                        mapped.append(
                            (Path(relative_directory) / source.name).as_posix(),
                        )
        self.assertEqual(set(mapped), expected)
        self.assertEqual(len(mapped), len(expected))

    def test_wheel_and_sdist_ship_the_complete_canonical_plugin_bundle(self) -> None:
        expected = _inventory(PLUGIN_ROOT)
        self.assertIn(".codex-plugin/plugin.json", expected)
        self.assertIn(".mcp.json", expected)
        self.assertIn("hooks/hooks.json", expected)
        self.assertIn("skills/hydra-report/SKILL.md", expected)
        self.assertIn("skills/hydra-report/agents/openai.yaml", expected)
        self.assertIn("skills/hydra-report/references/report-schema.md", expected)

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            source = temporary_root / "source"
            shutil.copytree(
                ROOT,
                source,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", "__pycache__", ".pytest_cache",
                    "build", "dist", "*.egg-info",
                ),
            )
            output = temporary_root / "dist"
            completed = subprocess.run(
                [
                    sys.executable, "-m", "build", "--no-isolation",
                    "--outdir", str(output),
                ],
                cwd=source,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            wheel = next(output.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                wheel_members = {
                    name: archive.read(name)
                    for name in archive.namelist()
                    if f"/{INSTALLED_PREFIX}/" in f"/{name}"
                }
            wheel_bundle = {
                name.split(f"{INSTALLED_PREFIX}/", 1)[1]: content
                for name, content in wheel_members.items()
            }
            self.assertEqual(wheel_bundle, expected)

            sdist = next(output.glob("*.tar.gz"))
            with tarfile.open(sdist, "r:gz") as archive:
                sdist_members = {
                    member.name: archive.extractfile(member).read()
                    for member in archive.getmembers()
                    if member.isfile() and "/plugins/hydra-codex/" in member.name
                }
            sdist_bundle = {
                name.split("/plugins/hydra-codex/", 1)[1]: content
                for name, content in sdist_members.items()
            }
            self.assertEqual(sdist_bundle, expected)


class PluginBundleApiTests(unittest.TestCase):
    def _api(self):
        try:
            from hydra_codex import plugin_bundle
        except ImportError as error:
            self.fail(f"installed plugin bundle API is unavailable: {error}")
        return plugin_bundle

    def test_api_locates_the_canonical_checkout_bundle(self) -> None:
        plugin_bundle = self._api()

        self.assertEqual(plugin_bundle.plugin_bundle_path(), PLUGIN_ROOT.resolve())

    def test_api_materializes_the_complete_bundle_without_overwriting(self) -> None:
        plugin_bundle = self._api()

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "hydra-codex"
            result = plugin_bundle.materialize_plugin_bundle(target)
            self.assertEqual(result, target.resolve())
            self.assertEqual(_inventory(target), _inventory(PLUGIN_ROOT))
            with self.assertRaises(FileExistsError):
                plugin_bundle.materialize_plugin_bundle(target)

    def test_supported_command_locates_and_materializes_the_bundle(self) -> None:
        plugin_bundle = self._api()
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            'hydra-codex-plugin = "hydra_codex.plugin_bundle:main"', pyproject,
        )

        output = io.StringIO()
        error = io.StringIO()
        self.assertEqual(
            plugin_bundle.main(["path"], stdout=output, stderr=error), 0,
        )
        self.assertEqual(Path(output.getvalue().strip()), PLUGIN_ROOT.resolve())
        self.assertEqual(error.getvalue(), "")

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "activated-plugin"
            output = io.StringIO()
            self.assertEqual(
                plugin_bundle.main(
                    ["materialize", str(target)], stdout=output, stderr=error,
                ),
                0,
            )
            self.assertEqual(Path(output.getvalue().strip()), target.resolve())
            self.assertEqual(_inventory(target), _inventory(PLUGIN_ROOT))


if __name__ == "__main__":
    unittest.main()
