from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import platform
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from hydra_codex.archive_validation import UnsafeArchive, validate_tar_members
from hydra_codex.install_layout import (
    CANONICAL_PLUGIN_FILES,
    platform_target,
    validate_bundle,
)


ROOT = Path(__file__).parents[1]
BUILDER_PATH = ROOT / "packaging" / "build_standalone.py"
SPEC_PATH = ROOT / "packaging" / "hydra-codex.spec"
ACCEPTANCE_PATH = ROOT / "packaging" / "accept_standalone.sh"
EXPECTED_ASSETS = (
    "index.html",
    "tokens.css",
    "dashboard.css",
    "bootstrap.js",
    "api.js",
    "state.js",
    "dom.js",
    "app.js",
    "views/shell.js",
    "views/overview.js",
    "views/tasks.js",
    "views/compare.js",
    "views/health.js",
    "views/evidence.js",
)


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "hydra_standalone_builder",
        BUILDER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("standalone builder cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StandaloneBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_git(self, source: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        )

    def committed_source(self, *, version: str = "0.1.0") -> Path:
        source = self.root / f"source-{version}"
        files = {
            "src/hydra_codex/__init__.py": f'__version__ = "{version}"\n',
            "packaging/hydra-codex.spec": "# fixture spec\n",
            "LICENSE": "fixture license\n",
            "install.sh": "#!/bin/sh\nexit 0\n",
            ".agents/plugins/marketplace.json": json.dumps({
                "name": "hydra",
                "plugins": [{
                    "name": "hydra-codex",
                    "source": {
                        "source": "local",
                        "path": "./plugins/hydra-codex",
                    },
                }],
            }) + "\n",
            "plugins/hydra-codex/.codex-plugin/plugin.json": json.dumps({
                "name": "hydra-codex",
                "version": version,
            }) + "\n",
            "plugins/hydra-codex/.mcp.json": "{}\n",
            "plugins/hydra-codex/README.md": "fixture\n",
            "plugins/hydra-codex/hooks/hooks.json": "{}\n",
            "plugins/hydra-codex/skills/hydra-report/SKILL.md": "fixture\n",
            "plugins/hydra-codex/skills/hydra-report/agents/openai.yaml": "fixture\n",
            "plugins/hydra-codex/skills/hydra-report/references/report-schema.md": "fixture\n",
            ".gitignore": "dist/\n",
        }
        for relative, content in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (source / "install.sh").chmod(0o755)
        self.run_git(source.parent, "init", "-q", source.name)
        self.run_git(source, "config", "user.email", "fixture@example.invalid")
        self.run_git(source, "config", "user.name", "Fixture")
        self.run_git(source, "add", ".")
        self.run_git(source, "commit", "-qm", "fixture")
        return source

    def fake_pyinstaller(self, calls: list[tuple[Path, Path, Path]]):
        def run(
            *,
            source_root: Path,
            workpath: Path,
            distpath: Path,
        ) -> None:
            calls.append((source_root, workpath, distpath))
            runtime = distpath / "hydra-codex"
            (runtime / "_internal").mkdir(parents=True)
            executable = runtime / "hydra-codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            (runtime / "_internal" / "runtime.dat").write_text(
                "fixture\n",
                encoding="utf-8",
            )
            with zipfile.ZipFile(runtime / "_internal" / "base_library.zip", "w") as bundle:
                bundle.writestr("fixture.pyc", b"fixture")
        return run

    def fixture_bundle(
        self,
        *,
        version: str = "0.1.0",
        target: str = "darwin-arm64",
    ) -> Path:
        bundle = self.root / f"hydra-codex-{version}"
        files = {
            "VERSION": version + "\n",
            "TARGET": target + "\n",
            "LICENSE": "fixture license\n",
            "install.sh": "#!/bin/sh\nexit 0\n",
            "bin/hydra-codex": "#!/bin/sh\nexit 0\n",
            "runtime/hydra-codex/hydra-codex": "fixture runtime\n",
            "runtime/hydra-codex/_internal/runtime.dat": "fixture\n",
            "marketplace/.agents/plugins/marketplace.json": "{}\n",
            "marketplace/plugins/hydra-codex/.codex-plugin/plugin.json": "{}\n",
            "marketplace/plugins/hydra-codex/.mcp.json": "{}\n",
            "marketplace/plugins/hydra-codex/README.md": "fixture\n",
            "marketplace/plugins/hydra-codex/hooks/hooks.json": "{}\n",
            "marketplace/plugins/hydra-codex/skills/hydra-report/SKILL.md": "fixture\n",
            "marketplace/plugins/hydra-codex/skills/hydra-report/agents/openai.yaml": "fixture\n",
            "marketplace/plugins/hydra-codex/skills/hydra-report/references/report-schema.md": "fixture\n",
        }
        for relative, content in files.items():
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for relative in (
            "install.sh",
            "bin/hydra-codex",
            "runtime/hydra-codex/hydra-codex",
        ):
            (bundle / relative).chmod(0o755)
        return bundle

    def test_builder_refuses_nonempty_publication_directory(self) -> None:
        builder = load_builder()
        output = self.root / "publication"
        output.mkdir()
        (output / "stale").write_text("foreign", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            builder.build_bundle(ROOT, output, "darwin-arm64")

    def test_builder_rejects_non_native_target_before_pyinstaller(self) -> None:
        builder = load_builder()
        source = self.committed_source()
        native = platform_target(platform.system(), platform.machine())
        foreign = next(
            candidate for candidate in (
                "darwin-arm64",
                "darwin-x86_64",
                "linux-x86_64",
            )
            if candidate != native
        )
        calls: list[tuple[Path, Path, Path]] = []

        with self.assertRaises(ValueError):
            builder.build_bundle(
                source,
                self.root / "publication",
                foreign,
                _pyinstaller=self.fake_pyinstaller(calls),
            )

        self.assertEqual(calls, [])

    def test_builder_refuses_dirty_tracked_source(self) -> None:
        builder = load_builder()
        source = self.committed_source()
        (source / "LICENSE").write_text("dirty\n", encoding="utf-8")
        native = platform_target(platform.system(), platform.machine())

        with self.assertRaises(RuntimeError):
            builder.build_bundle(
                source,
                self.root / "publication",
                native,
                _pyinstaller=self.fake_pyinstaller([]),
            )

    def test_builder_uses_committed_stage_and_private_pyinstaller_roots(self) -> None:
        builder = load_builder()
        source = self.committed_source()
        sentinel = source / "dist" / "ignored-sentinel"
        sentinel.parent.mkdir()
        sentinel.write_text("must not ship\n", encoding="utf-8")
        native = platform_target(platform.system(), platform.machine())
        calls: list[tuple[Path, Path, Path]] = []

        bundle = builder.build_bundle(
            source,
            self.root / "publication",
            native,
            _pyinstaller=self.fake_pyinstaller(calls),
        )

        self.assertEqual(len(calls), 1)
        staged, workpath, distpath = calls[0]
        self.assertNotEqual(staged, source)
        self.assertFalse((staged / "dist" / "ignored-sentinel").exists())
        self.assertNotEqual(distpath, source / "dist")
        self.assertEqual(workpath.parent, distpath.parent)
        self.assertFalse(workpath.exists())
        self.assertFalse(distpath.exists())
        layout = validate_bundle(
            bundle,
            expected_version="0.1.0",
            expected_target=native,
        )
        self.assertTrue(layout.executable.is_file())
        self.assertFalse(layout.executable.is_symlink())
        self.assertEqual(
            {
                path.relative_to(bundle).as_posix()
                for path in bundle.iterdir()
            },
            {
                "VERSION",
                "TARGET",
                "LICENSE",
                "install.sh",
                "bin",
                "runtime",
                "marketplace",
            },
        )
        plugin = bundle / "marketplace" / "plugins" / "hydra-codex"
        self.assertEqual(
            {
                path.relative_to(plugin)
                for path in plugin.rglob("*")
                if path.is_file()
            },
            set(CANONICAL_PLUGIN_FILES),
        )
        self.assertNotIn(
            b"ignored-sentinel",
            b"".join(
                path.read_bytes()
                for path in bundle.rglob("*")
                if path.is_file()
            ),
        )

    def test_archive_contains_one_valid_top_level_bundle(self) -> None:
        builder = load_builder()
        bundle = self.fixture_bundle()
        archive = builder.create_archive(bundle, self.root / "publication")

        self.assertEqual(
            archive.name,
            "hydra-codex-0.1.0-darwin-arm64.tar.gz",
        )
        with tarfile.open(archive, "r:gz") as stream:
            members = stream.getmembers()
        names = {member.name for member in members}
        self.assertEqual(
            {name.split("/", 1)[0] for name in names},
            {"hydra-codex-0.1.0"},
        )
        self.assertIn(
            "hydra-codex-0.1.0/runtime/hydra-codex/hydra-codex",
            names,
        )
        self.assertIn(
            "hydra-codex-0.1.0/marketplace/.agents/plugins/marketplace.json",
            names,
        )
        validated = validate_tar_members(
            archive,
            expected_top_level="hydra-codex-0.1.0",
        )
        self.assertEqual(
            (validated.version, validated.target),
            ("0.1.0", "darwin-arm64"),
        )

    def test_archive_validator_requires_bundled_installer(self) -> None:
        builder = load_builder()
        bundle = self.fixture_bundle()
        (bundle / "install.sh").unlink()
        archive = builder.create_archive(bundle, self.root / "publication")

        with self.assertRaises(UnsafeArchive):
            validate_tar_members(
                archive,
                expected_top_level="hydra-codex-0.1.0",
            )

    def test_archive_digest_is_reproducible(self) -> None:
        builder = load_builder()
        bundle = self.fixture_bundle()

        first = builder.create_archive(bundle, self.root / "one")
        second = builder.create_archive(bundle, self.root / "two")

        self.assertEqual(builder.sha256_file(first), builder.sha256_file(second))
        self.assertEqual(
            builder.sha256_file(first),
            hashlib.sha256(first.read_bytes()).hexdigest(),
        )

    def test_archive_metadata_is_normalized_and_sorted(self) -> None:
        builder = load_builder()
        bundle = self.fixture_bundle()
        archive = builder.create_archive(bundle, self.root / "publication")

        with archive.open("rb") as raw:
            self.assertEqual(raw.read(10)[4:8], b"\0\0\0\0")
        with tarfile.open(archive, "r:gz") as stream:
            members = stream.getmembers()

        self.assertEqual([member.name for member in members], sorted(
            member.name for member in members
        ))
        for member in members:
            with self.subTest(member=member.name):
                self.assertEqual(
                    (member.mtime, member.uid, member.gid, member.uname, member.gname),
                    (0, 0, 0, "", ""),
                )
                expected = 0o755 if member.isdir() else (
                    0o755 if member.name.endswith((
                        "/install.sh",
                        "/bin/hydra-codex",
                        "/runtime/hydra-codex/hydra-codex",
                    )) else 0o644
                )
                self.assertEqual(stat.S_IMODE(member.mode), expected)

    def test_launcher_execs_adjacent_one_folder_runtime(self) -> None:
        builder = load_builder()
        source = self.committed_source()
        native = platform_target(platform.system(), platform.machine())
        bundle = builder.build_bundle(
            source,
            self.root / "publication",
            native,
            _pyinstaller=self.fake_pyinstaller([]),
        )

        launcher = (bundle / "bin" / "hydra-codex").read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/bin/sh\n"))
        self.assertIn('runtime/hydra-codex/hydra-codex" "$@"', launcher)
        self.assertIn("exec ", launcher)

    def test_launcher_resolves_installed_public_symlink(self) -> None:
        builder = load_builder()
        source = self.committed_source()
        native = platform_target(platform.system(), platform.machine())
        bundle = builder.build_bundle(
            source,
            self.root / "publication",
            native,
            _pyinstaller=self.fake_pyinstaller([]),
        )
        runtime = bundle / "runtime" / "hydra-codex" / "hydra-codex"
        runtime.write_text(
            '#!/bin/sh\nprintf "runtime:%s\\n" "$1"\n',
            encoding="utf-8",
        )
        runtime.chmod(0o755)

        version_root = self.root / "home" / ".hydra" / "versions" / "0.1.0"
        shutil.copytree(bundle, version_root)
        current = self.root / "home" / ".hydra" / "current"
        current.symlink_to(version_root)
        public = self.root / "home" / ".local" / "bin" / "hydra-codex"
        public.parent.mkdir(parents=True)
        public.symlink_to(current / "bin" / "hydra-codex")

        result = subprocess.run(
            [str(public), "accepted"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.stdout, "runtime:accepted\n")

    def test_spec_explicitly_collects_deferred_modules_assets_and_metadata(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "src" / "hydra_codex").glob("*.py"))
        )
        deferred = {
            f"hydra_codex.{match}"
            for match in re.findall(r"^\s+from \.([a-z0-9_]+) import ", source, re.MULTILINE)
        }
        spec = SPEC_PATH.read_text(encoding="utf-8")
        string_literals = [
            node.value
            for node in ast.walk(ast.parse(spec))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]

        for module in sorted(deferred):
            with self.subTest(module=module):
                self.assertIn(module, string_literals)
        for asset in EXPECTED_ASSETS:
            with self.subTest(asset=asset):
                self.assertEqual(string_literals.count(asset), 1)
        self.assertIn("copy_metadata", spec)
        self.assertIn("LICENSE", spec)

    def test_release_extra_pins_pyinstaller(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(
            pyproject,
            r'release\s*=\s*\[\s*"pyinstaller==6\.21\.0",?\s*\]',
        )

    def test_pyinstaller_cache_and_import_environment_are_private(self) -> None:
        builder = load_builder()
        private = self.root / "private"
        source = private / "source"
        work = private / "work"
        dist = private / "dist"
        for directory in (source, work, dist):
            directory.mkdir(parents=True)

        with patch.object(subprocess, "run") as run:
            builder._run_pyinstaller(
                source_root=source,
                workpath=work,
                distpath=dist,
            )

        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            environment["PYINSTALLER_CONFIG_DIR"],
            str(private / "cache"),
        )
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONHOME", environment)

    def test_frozen_entrypoint_imports_package_main_absolutely(self) -> None:
        builder = load_builder()
        source = self.root / "source"
        (source / "src").mkdir(parents=True)
        (source / "packaging").mkdir()
        (source / "LICENSE").write_text("license\n", encoding="utf-8")

        builder._write_distribution_metadata(source, "0.1.0")

        entrypoint = source / "packaging" / "_frozen_main.py"
        self.assertEqual(
            entrypoint.read_text(encoding="utf-8"),
            "from hydra_codex.__main__ import main\n"
            "raise SystemExit(main())\n",
        )
        self.assertIn(
            '"_frozen_main.py"',
            SPEC_PATH.read_text(encoding="utf-8"),
        )

    def test_base_library_zip_normalization_is_order_independent(self) -> None:
        builder = load_builder()
        first = self.root / "first.zip"
        second = self.root / "second.zip"
        for archive, entries in (
            (first, (("b.pyc", b"b"), ("a.pyc", b"a"))),
            (second, (("a.pyc", b"a"), ("b.pyc", b"b"))),
        ):
            with zipfile.ZipFile(archive, "w") as bundle:
                for index, (name, payload) in enumerate(entries, start=1):
                    info = zipfile.ZipInfo(
                        name,
                        date_time=(2026, 7, 23, 12, index, 0),
                    )
                    info.external_attr = (0o100600 + index) << 16
                    bundle.writestr(info, payload)

        builder._normalize_zip_archive(first)
        builder._normalize_zip_archive(second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as bundle:
            self.assertEqual(bundle.namelist(), ["a.pyc", "b.pyc"])
            for info in bundle.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.external_attr >> 16, 0o100644)

    def test_command_builds_archive_and_removes_unpacked_publication(self) -> None:
        builder = load_builder()
        output = self.root / "publication"
        staged = output / "hydra-codex-0.1.0"
        archive = output / "hydra-codex-0.1.0-darwin-arm64.tar.gz"

        def fake_build(_source: Path, selected: Path, _target: str) -> Path:
            self.assertEqual(selected, output)
            staged.mkdir(parents=True)
            return staged

        def fake_archive(bundle: Path, selected: Path) -> Path:
            self.assertEqual((bundle, selected), (staged, output))
            archive.write_bytes(b"archive")
            return archive

        with patch.object(builder, "build_bundle", side_effect=fake_build), patch.object(
            builder,
            "create_archive",
            side_effect=fake_archive,
        ), patch.object(builder, "_native_target", return_value="darwin-arm64"):
            result = builder.main([
                "--source-root",
                str(ROOT),
                "--output",
                str(output),
            ])

        self.assertEqual(result, 0)
        self.assertTrue(archive.is_file())
        self.assertFalse(staged.exists())

    def test_acceptance_contract_is_clean_machine_and_two_release(self) -> None:
        script = ACCEPTANCE_PATH.read_text(encoding="utf-8")

        self.assertTrue(script.startswith("#!/bin/sh\nset -eu\n"))
        self.assertNotIn("0.1.0", script)
        self.assertNotIn("0.1.1", script)
        self.assertIn(
            'TEMP_PARENT=$(CDPATH=\'\' cd -- "${TMPDIR-/tmp}" && pwd -P)',
            script,
        )
        self.assertIn('"$TEMP_PARENT"/hydra-standalone-accept.*)', script)
        self.assertIn("for command in python python3 python3.12 pip uv", script)
        self.assertIn("shim=$SHIM_DIR/$command", script)
        for marker in (
            "unset PYTHONPATH PYTHONHOME",
            "HYDRA=$HOME/.local/bin/hydra-codex",
            "HYDRA_INSTALLER_RELEASE_BASE_URL",
            "validate_tar_members",
            "BASE_VERSION",
            "NEXT_VERSION",
            "patch + 1",
            'commit -qm "build: create acceptance next release"',
            '"$HYDRA" upgrade --check',
            '"$HYDRA" upgrade',
            "CODEX_FAIL_REFRESH",
            '"$HYDRA" report --last 1 --format json --cwd "$PROJECT"',
            "hydra.report-list/v2",
            "hydra.report/v4",
            "dashboard --no-open",
            "/assets/views/evidence.js",
            "uninstall -y",
            "hydra.sqlite3",
            "shim invocation log is not empty",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, script)
        self.assertEqual(script.count('"$SOURCE_ROOT/install.sh"'), 1)
        self.assertIn(
            'if ! "$HOST_PYTHON" - "$TEMP_ROOT/report.json" <<\'PY\'\n',
            script,
        )
        self.assertIn(
            'PY\nthen\n    fail "synthetic report failed"\nfi\n',
            script,
        )
        self.assertNotIn("HYDRA_VERSION_OVERRIDE", script)

    def test_acceptance_loopback_server_never_resolves_a_hostname(self) -> None:
        script = ACCEPTANCE_PATH.read_text(encoding="utf-8")

        self.assertIn("import socketserver", script)
        self.assertIn("class LoopbackServer(http.server.ThreadingHTTPServer):", script)
        self.assertIn("socketserver.TCPServer.server_bind(self)", script)
        self.assertIn(
            'server = LoopbackServer(("127.0.0.1", 0), Handler)',
            script,
        )
        self.assertNotIn("socket.getfqdn", script)

    def test_acceptance_server_start_timeout_terminates_owned_child(self) -> None:
        source = ACCEPTANCE_PATH.read_text(encoding="utf-8")
        marker = "wait_for_server_port()\n{\n"
        self.assertIn(marker, source)
        start = source.index(marker)
        end = source.index("\n}\n", start) + len("\n}\n")
        function = source[start:end]
        port_file = self.root / "missing-port"
        log_file = self.root / "server.log"
        log_file.write_text(
            "".join(f"diagnostic-{line}\n" for line in range(1, 26)),
            encoding="utf-8",
        )
        harness = self.root / "server-timeout.sh"
        harness.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            f"{function}\n"
            "sleep 30 &\n"
            "owned_pid=$!\n"
            'if wait_for_server_port "$owned_pid" "$1" "$2" 2 0.01; then\n'
            '    kill "$owned_pid" 2>/dev/null || :\n'
            "    exit 10\n"
            "fi\n"
            'if kill -0 "$owned_pid" 2>/dev/null; then exit 11; fi\n'
            'printf "%s\\n" "bounded cleanup complete"\n',
            encoding="utf-8",
        )

        completed = subprocess.run(
            ["sh", str(harness), str(port_file), str(log_file)],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "bounded cleanup complete\n")
        self.assertIn("diagnostic-20", completed.stderr)
        self.assertNotIn("diagnostic-21", completed.stderr)

    def test_acceptance_codex_shim_matches_supported_text_protocol(self) -> None:
        script = ACCEPTANCE_PATH.read_text(encoding="utf-8")

        for marker in (
            "codex-cli 0.136.0",
            "No plugin marketplaces in scope.",
            '"%-13s%s\\n" "MARKETPLACE" "ROOT"',
            "Added marketplace \\`hydra\\` from $4.",
            "Removed marketplace \\`hydra\\`.",
            "No plugins found in marketplace \\`hydra\\`.",
            "Marketplace \\`hydra\\`",
            "PLUGIN",
            "STATUS",
            "VERSION",
            "PATH",
            "installed, enabled",
            "not installed",
            "Added plugin \\`hydra-codex\\` from marketplace \\`hydra\\`.",
            "Removed plugin \\`hydra-codex\\` from marketplace \\`hydra\\`.",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, script)
        self.assertNotIn("codex-cli 1.0.0", script)
        self.assertNotIn("'--json'", script)
        self.assertNotIn("'--available'", script)
        self.assertNotIn('[{"name":"hydra"', script)
        self.assertNotIn('[{"name":"hydra-codex"', script)

    def test_acceptance_sha256_helper_prefers_gnu_then_falls_back_to_shasum(
        self,
    ) -> None:
        script = ACCEPTANCE_PATH.read_text(encoding="utf-8")
        prefix = script.split('[ "$#" -eq 1 ]', 1)[0]
        harness = self.root / "sha-harness.sh"
        harness.write_text(prefix + '\nsha256_file "$1"\n', encoding="utf-8")
        payload = self.root / "payload"
        payload.write_bytes(b"payload")

        commands = self.root / "commands"
        commands.mkdir()
        gnu = commands / "sha256sum"
        gnu.write_text(
            '#!/bin/sh\nprintf "%064d  %s\\n" 0 "$1"\n',
            encoding="utf-8",
        )
        gnu.chmod(0o755)
        bsd = commands / "shasum"
        bsd.write_text(
            '#!/bin/sh\n'
            '[ "$1" = -a ] && [ "$2" = 256 ] || exit 91\n'
            'printf "%064d  %s\\n" 1 "$3"\n',
            encoding="utf-8",
        )
        bsd.chmod(0o755)
        grep = commands / "grep"
        grep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        grep.chmod(0o755)

        preferred = subprocess.run(
            ["/bin/sh", str(harness), str(payload)],
            env={"PATH": str(commands)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual((preferred.returncode, preferred.stdout.strip()), (0, "0" * 64))

        gnu.unlink()
        fallback = subprocess.run(
            ["/bin/sh", str(harness), str(payload)],
            env={"PATH": str(commands)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual((fallback.returncode, fallback.stdout.strip()), (0, "0" * 63 + "1"))

        bsd.unlink()
        unavailable = subprocess.run(
            ["/bin/sh", str(harness), str(payload)],
            env={"PATH": str(commands)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(unavailable.returncode, 0)
        self.assertIn("SHA-256 tool is unavailable", unavailable.stderr)

    def test_installer_accepts_and_requires_bundled_install_script(self) -> None:
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertIn(
            "VERSION|TARGET|LICENSE|install.sh|bin|runtime|marketplace",
            installer,
        )
        self.assertIn('require_regular "$bundle/install.sh"', installer)


if __name__ == "__main__":
    unittest.main()
