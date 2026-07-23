from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hydra_codex.cli import main as cli_main
from hydra_codex.install_layout import validate_bundle


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
TARGETS = ("darwin-arm64", "darwin-x86_64", "linux-x86_64")
PLUGIN_FILES = (
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "README.md",
    "hooks/hooks.json",
    "skills/hydra-report/SKILL.md",
    "skills/hydra-report/agents/openai.yaml",
    "skills/hydra-report/references/report-schema.md",
)


def add_file(
    bundle: tarfile.TarFile,
    name: str,
    content: bytes,
    mode: int = 0o644,
) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = mode
    bundle.addfile(member, io.BytesIO(content))


def make_archive(
    destination: Path,
    version: str,
    target: str,
    *,
    version_output: str | None = None,
    extra: tuple[tarfile.TarInfo, bytes | None] | None = None,
    mode_overrides: dict[str, int] | None = None,
) -> bytes:
    top = f"hydra-codex-{version}"
    modes = mode_overrides or {}
    executable = (
        "#!/bin/sh\n"
        "case \"${1-}\" in\n"
        f"  --version) printf '%s\\n' 'hydra-codex {version_output or version}' ;;\n"
        "  __installer-activate)\n"
        "    printf '%s\\n' activate > \"$HOME/activation-called\"\n"
        "    exec \"$HYDRA_TEST_ACTIVATOR\" \"$2\"\n"
        "    ;;\n"
        "  uninstall) printf '%s\\n' uninstall > \"$HOME/uninstall-called\" ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
    ).encode()
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        add_file(
            bundle,
            f"{top}/VERSION",
            f"{version}\n".encode(),
            modes.get("VERSION", 0o644),
        )
        add_file(
            bundle,
            f"{top}/TARGET",
            f"{target}\n".encode(),
            modes.get("TARGET", 0o644),
        )
        add_file(
            bundle,
            f"{top}/LICENSE",
            b"MIT\n",
            modes.get("LICENSE", 0o644),
        )
        add_file(
            bundle,
            f"{top}/install.sh",
            b"#!/bin/sh\n",
            modes.get("install.sh", 0o755),
        )
        add_file(
            bundle,
            f"{top}/bin/hydra-codex",
            executable,
            modes.get("bin/hydra-codex", 0o755),
        )
        add_file(
            bundle,
            f"{top}/runtime/runtime.bin",
            b"runtime\n",
            modes.get("runtime/runtime.bin", 0o644),
        )
        add_file(
            bundle,
            f"{top}/marketplace/.agents/plugins/marketplace.json",
            (
                b'{"name":"hydra","plugins":[{"name":"hydra-codex",'
                b'"source":{"source":"local","path":"./plugins/hydra-codex"}}]}\n'
            ),
            modes.get(
                "marketplace/.agents/plugins/marketplace.json",
                0o644,
            ),
        )
        for relative in PLUGIN_FILES:
            content = (
                f'{{"name":"hydra-codex","version":"{version}"}}\n'.encode()
                if relative == ".codex-plugin/plugin.json"
                else b"asset\n"
            )
            add_file(
                bundle,
                f"{top}/marketplace/plugins/hydra-codex/{relative}",
                content,
                modes.get(
                    f"marketplace/plugins/hydra-codex/{relative}",
                    0o644,
                ),
            )
        if extra is not None:
            member, payload = extra
            bundle.addfile(
                member,
                None if payload is None else io.BytesIO(payload),
            )
    return destination.read_bytes()


def manifest_for(version: str, archives: dict[str, bytes]) -> bytes:
    rows = []
    for target in TARGETS:
        filename = f"hydra-codex-{version}-{target}.tar.gz"
        rows.append(f"{hashlib.sha256(archives[target]).hexdigest()}  {filename}\n")
    return "".join(rows).encode()


class ReleaseState:
    def __init__(self) -> None:
        self.latest = "1.0.0"
        self.assets: dict[str, bytes] = {}
        self.manifest: bytes = b""
        self.requests: list[str] = []
        self.latest_location: str | None = None
        self.delay_archive = 0.0


class ReleaseHandler(BaseHTTPRequestHandler):
    server: "ReleaseServer"

    def do_GET(self) -> None:
        state = self.server.state
        state.requests.append(self.path)
        if self.path == "/releases/latest":
            location = state.latest_location or f"/releases/tag/v{state.latest}"
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        if self.path == f"/releases/tag/v{state.latest}":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"release\n")
            return
        if self.path.endswith("/SHA256SUMS"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(state.manifest)))
            self.end_headers()
            self.wfile.write(state.manifest)
            return
        prefix = "/releases/download/v"
        if self.path.startswith(prefix):
            name = self.path.rsplit("/", 1)[-1]
            content = state.assets.get(name)
            if content is not None:
                if state.delay_archive:
                    time.sleep(state.delay_archive)
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        pass


class ReleaseServer(ThreadingHTTPServer):
    state: ReleaseState


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = ReleaseState()
        cls.server = ReleaseServer(("127.0.0.1", 0), ReleaseHandler)
        cls.server.state = cls.state
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.release_base = (
            f"http://127.0.0.1:{cls.server.server_port}/releases"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "private-home-do-not-leak"
        self.home.mkdir(mode=0o700)
        self.shims = self.root / "shims"
        self.shims.mkdir()
        uname = self.shims / "uname"
        uname.write_text(
            "#!/bin/sh\n"
            "case \"${1-}\" in\n"
            "  -s) printf '%s\\n' \"${TEST_UNAME_S-Darwin}\" ;;\n"
            "  -m) printf '%s\\n' \"${TEST_UNAME_M-arm64}\" ;;\n"
            "  *) exit 64 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        uname.chmod(0o755)
        self.activator = self.root / "activate-staged"
        self.activator.write_text(
            f"#!{ROOT.parent.parent / '.venv' / 'bin' / 'python'}\n"
            "from pathlib import Path\n"
            "import os\n"
            "import sys\n"
            "from hydra_codex.install_layout import validate_bundle\n"
            "from hydra_codex.release_management import (\n"
            "    activate_version,\n"
            "    default_install_roots,\n"
            ")\n"
            "candidate = Path(sys.argv[1])\n"
            "layout = validate_bundle(candidate)\n"
            "roots = default_install_roots(Path(os.environ['HOME']))\n"
            "activate_version(layout, roots=roots, environ=os.environ)\n",
            encoding="utf-8",
        )
        self.activator.chmod(0o755)
        self.state.latest = "1.0.0"
        self.state.latest_location = None
        self.state.delay_archive = 0
        self.state.requests.clear()
        self.publish("1.0.0")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publish(
        self,
        version: str,
        *,
        selected_archive: bytes | None = None,
        version_output: str | None = None,
    ) -> None:
        archives: dict[str, bytes] = {}
        for target in TARGETS:
            path = self.root / f"{version}-{target}.tar.gz"
            archives[target] = make_archive(
                path,
                version,
                target,
                version_output=version_output if target == "darwin-arm64" else None,
            )
        if selected_archive is not None:
            archives["darwin-arm64"] = selected_archive
        self.state.latest = version
        self.state.assets = {
            f"hydra-codex-{version}-{target}.tar.gz": content
            for target, content in archives.items()
        }
        self.state.manifest = manifest_for(version, archives)

    def run_installer(
        self,
        *arguments: str,
        environment: dict[str, str | None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        values = {
            "HOME": str(self.home),
            "PATH": f"{self.shims}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HYDRA_INSTALLER_RELEASE_BASE_URL": self.release_base,
            "HYDRA_TEST_ACTIVATOR": str(self.activator),
            "PYTHONPATH": str(ROOT / "src"),
            "LC_ALL": "C",
        }
        for key, value in (environment or {}).items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        return subprocess.run(
            ("sh", str(INSTALLER), *arguments),
            cwd=ROOT,
            env=values,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    @property
    def current(self) -> Path:
        return self.home / ".hydra" / "current"

    @property
    def launcher(self) -> Path:
        return self.home / ".local" / "bin" / "hydra-codex"

    def test_script_is_posix_private_and_has_no_python_or_jq_bootstrap(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertTrue(source.startswith("#!/bin/sh\nset -eu\n"))
        self.assertIn("umask 077", source)
        self.assertNotIn("python", source.lower())
        self.assertNotIn("jq", source.lower())

    def test_private_acquisition_requires_capability_and_preserves_parent_lock(
        self,
    ) -> None:
        before = tuple(self.state.requests)
        rejected = self.run_installer("--acquire")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(tuple(self.state.requests), before)

        self.run_installer("--version", "1.0.0")
        self.publish("1.0.1")
        lock = self.home / ".hydra-installer-lock"
        lock.mkdir(mode=0o700)
        capability = "a" * 64
        acquired = self.run_installer(
            "--acquire",
            environment={
                "HYDRA_INTERNAL_RELEASE_ACQUISITION": capability,
            },
        )

        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        lines = acquired.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        candidate = Path(lines[0])
        self.assertEqual(candidate.parent.parent, self.home / ".hydra")
        self.assertTrue(candidate.parent.name.startswith(".acquire."))
        self.assertEqual(validate_bundle(candidate).version, "1.0.1")
        self.assertTrue(lock.is_dir())

    def test_private_resolution_is_capability_gated_machine_readable_and_read_only(
        self,
    ) -> None:
        before_requests = tuple(self.state.requests)
        rejected = self.run_installer("--resolve")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(tuple(self.state.requests), before_requests)

        installed = self.run_installer("--version", "1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.publish("1.0.1")
        before = tuple(
            sorted(
                (
                    path.relative_to(self.home).as_posix(),
                    path.lstat().st_mode,
                    os.readlink(path) if path.is_symlink() else (
                        path.read_bytes() if path.is_file() else None
                    ),
                )
                for path in self.home.rglob("*")
            ),
        )
        requests_before_resolve = len(self.state.requests)

        resolved = self.run_installer(
            "--resolve",
            environment={
                "HYDRA_INTERNAL_RELEASE_RESOLUTION": "b" * 64,
            },
        )

        self.assertEqual((resolved.returncode, resolved.stderr), (0, ""))
        self.assertEqual(
            resolved.stdout,
            '{"current_version":"1.0.0","latest_version":"1.0.1"}\n',
        )
        self.assertEqual(
            self.state.requests[requests_before_resolve:],
            ["/releases/latest", "/releases/tag/v1.0.1"],
        )
        after = tuple(
            sorted(
                (
                    path.relative_to(self.home).as_posix(),
                    path.lstat().st_mode,
                    os.readlink(path) if path.is_symlink() else (
                        path.read_bytes() if path.is_file() else None
                    ),
                )
                for path in self.home.rglob("*")
            ),
        )
        self.assertEqual(after, before)

    def test_explicit_version_installs_exact_asset_and_is_idempotent(self) -> None:
        first = self.run_installer("--version", "1.0.0")
        second = self.run_installer("--version", "1.0.0")

        self.assertEqual((first.returncode, second.returncode), (0, 0))
        version = self.home / ".hydra" / "versions" / "1.0.0"
        self.assertEqual(os.readlink(self.current), str(version))
        self.assertEqual(
            os.readlink(self.launcher),
            str(self.current / "bin" / "hydra-codex"),
        )
        self.assertEqual(
            self.state.requests.count(
                "/releases/download/v1.0.0/"
                "hydra-codex-1.0.0-darwin-arm64.tar.gz",
            ),
            2,
        )
        self.assertNotIn("/releases/latest", self.state.requests)

    def test_activation_is_delegated_to_task5_runtime_helper(self) -> None:
        result = self.run_installer("--version", "1.0.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.home / "activation-called").read_text(encoding="utf-8"),
            "activate\n",
        )
        self.assertFalse(
            (self.home / ".hydra" / "release-transaction.json").exists(),
        )
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("ln -s", source)
        self.assertNotIn("atomic_links", source)

    def test_latest_accepts_only_canonical_final_url_and_resolves_once(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.requests.count("/releases/latest"), 1)
        before = sorted(
            (str(path.relative_to(self.home)), path.is_symlink())
            for path in self.home.rglob("*")
        )

        self.state.requests.clear()
        self.state.latest_location = "/releases/not-a-tag/v1.0.0"
        rejected = self.run_installer("--check")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(
            sorted(
                (str(path.relative_to(self.home)), path.is_symlink())
                for path in self.home.rglob("*")
            ),
            before,
        )
        self.state.requests.clear()
        self.state.latest_location = (
            "http://127.0.0.1:1/releases/tag/v1.0.0"
        )
        rejected = self.run_installer("--check")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(
            sorted(
                (str(path.relative_to(self.home)), path.is_symlink())
                for path in self.home.rglob("*")
            ),
            before,
        )

    def test_production_assets_accept_only_narrow_github_release_redirects(self) -> None:
        archive = self.root / "1.0.0-darwin-arm64.tar.gz"
        manifest = self.root / "production-SHA256SUMS"
        manifest.write_bytes(self.state.manifest)
        curl = self.shims / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            "output=\n"
            "url=\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -o) output=$2; shift 2 ;;\n"
            "    https://*) url=$1; shift ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "case \"$url\" in\n"
            "  */releases/latest)\n"
            "    printf '%s' "
            "'https://github.com/Manannikov-Nikita/hydra/releases/tag/v1.0.0'\n"
            "    ;;\n"
            "  */SHA256SUMS)\n"
            "    cp \"$HYDRA_REDIRECT_MANIFEST\" \"$output\"\n"
            "    printf 'https://%s/%s' "
            "\"${HYDRA_REDIRECT_HOST-release-assets.githubusercontent.com}\" "
            "'github-production-release-asset/12345/checksums?sp=r&sig=x'\n"
            "    ;;\n"
            "  *)\n"
            "    cp \"$HYDRA_REDIRECT_ARCHIVE\" \"$output\"\n"
            "    printf 'https://%s/%s' "
            "\"${HYDRA_REDIRECT_HOST-release-assets.githubusercontent.com}\" "
            "'github-production-release-asset/12345/archive?sp=r&sig=x'\n"
            "    ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)

        result = self.run_installer(
            environment={
                "HYDRA_INSTALLER_RELEASE_BASE_URL": None,
                "HYDRA_REDIRECT_ARCHIVE": str(archive),
                "HYDRA_REDIRECT_MANIFEST": str(manifest),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.launcher.is_symlink())
        current_before = os.readlink(self.current)
        rejected = self.run_installer(
            "--version",
            "1.0.0",
            environment={
                "HYDRA_INSTALLER_RELEASE_BASE_URL": None,
                "HYDRA_REDIRECT_ARCHIVE": str(archive),
                "HYDRA_REDIRECT_MANIFEST": str(manifest),
                "HYDRA_REDIRECT_HOST": (
                    "release-assets.githubusercontent.com.evil.invalid"
                ),
            },
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(os.readlink(self.current), current_before)

    def test_internal_activation_route_is_hidden_and_path_private(self) -> None:
        private = str(self.home / "candidate-do-not-echo")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "hydra_codex.installation_cli.activate_staged_runtime",
        ) as activate:
            code = cli_main(
                ["__installer-activate", private],
                stdout=stdout,
                stderr=stderr,
                environ={
                    "HOME": str(self.home),
                    "HYDRA_INTERNAL_INSTALLER_ACTIVATION": "1",
                },
            )
        self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (0, "", ""))
        activate.assert_called_once()

        stderr = io.StringIO()
        rejected = cli_main(
            ["__installer-activate", private],
            stdout=io.StringIO(),
            stderr=stderr,
            environ={"HOME": str(self.home)},
        )
        self.assertEqual(rejected, 2)
        self.assertNotIn(private, stderr.getvalue())

    def test_staged_runtime_activation_requires_its_own_frozen_bundle(self) -> None:
        from hydra_codex.installation_cli import activate_staged_runtime

        archive = self.root / "activation-candidate.tar.gz"
        make_archive(archive, "1.0.0", "darwin-arm64")
        extract = self.root / "activation-extract"
        extract.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(extract, filter="data")
        candidate = extract / "hydra-codex-1.0.0"
        with self.assertRaises(ValueError):
            activate_staged_runtime(
                candidate,
                environ={"HOME": str(self.home)},
                executable=self.root / "foreign" / "bin" / "hydra-codex",
            )
        self.assertFalse((self.home / ".hydra").exists())

        activated = activate_staged_runtime(
            candidate,
            environ={"HOME": str(self.home)},
            executable=candidate / "bin" / "hydra-codex",
        )
        self.assertEqual(activated.name, "1.0.0")
        self.assertTrue(self.current.is_symlink())

    def test_invalid_release_override_is_rejected_before_curl(self) -> None:
        marker = self.root / "curl-called"
        curl = self.shims / "curl"
        curl.write_text(
            f"#!/bin/sh\n: > '{marker}'\nexit 99\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        invalid = (
            "http://localhost:1234/releases",
            "http://127.0.0.1:1234/other",
            "http://127.0.0.1:1234/releases?x=1",
            "http://user@127.0.0.1:1234/releases",
            "file:///tmp/releases",
            "https://127.0.0.1:1234/releases",
            "http://127.0.0.1.evil:1234/releases",
        )
        for value in invalid:
            with self.subTest(value=value):
                marker.unlink(missing_ok=True)
                result = self.run_installer(
                    "--check",
                    environment={"HYDRA_INSTALLER_RELEASE_BASE_URL": value},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())

    def test_foreign_uid_home_is_rejected_before_curl(self) -> None:
        marker = self.root / "curl-called-for-foreign-home"
        curl = self.shims / "curl"
        curl.write_text(
            f"#!/bin/sh\n: > '{marker}'\nexit 99\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        ls = self.shims / "ls"
        ls.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' "
            "'drwx------ 2 999999 999999 64 Jul 23 00:00 selected-home'\n",
            encoding="utf-8",
        )
        ls.chmod(0o755)

        result = self.run_installer("--check")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_manifest_must_be_exact_lf_sorted_three_row_contract(self) -> None:
        valid = self.state.manifest
        rows = valid.splitlines(keepends=True)
        invalid = (
            valid.replace(b"\n", b"\r\n"),
            valid.upper(),
            rows[0] + rows[0] + rows[2],
            rows[0] + rows[2],
            valid + b"0" * 64 + b"  extra.tar.gz\n",
            valid.replace(
                b"hydra-codex-1.0.0-linux-x86_64.tar.gz",
                b"hydra-codex-1.0.0-linux-x86_64.tar.gz.evil",
            ),
            rows[1] + rows[0] + rows[2],
            valid.rstrip(b"\n"),
        )
        for index, manifest in enumerate(invalid):
            with self.subTest(index=index):
                self.state.manifest = manifest
                result = self.run_installer("--version", "1.0.0")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / ".hydra").exists())
                self.state.manifest = valid

    def test_oversized_manifest_and_archive_fail_before_validation_privately(self) -> None:
        self.state.manifest = b"x" * 4097
        manifest_result = self.run_installer("--version", "1.0.0")
        self.assertNotEqual(manifest_result.returncode, 0)
        self.assertFalse((self.home / ".hydra").exists())
        self.assertNotIn(str(self.home), manifest_result.stderr)

        oversized = self.root / "oversized-archive"
        with oversized.open("wb") as stream:
            stream.seek(512 * 1024 * 1024)
            stream.write(b"x")
        curl = self.shims / "curl"
        curl_arguments = self.root / "oversized-curl-arguments"
        curl.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > '{curl_arguments}'\n"
            "output=\n"
            "url=\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -o) output=$2; shift 2 ;;\n"
            "    http://*) url=$1; shift ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "cp \"$HYDRA_OVERSIZED_ARCHIVE\" \"$output\"\n"
            "printf '%s' \"$url\"\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        archive_result = self.run_installer(
            "--version",
            "1.0.0",
            environment={"HYDRA_OVERSIZED_ARCHIVE": str(oversized)},
        )
        self.assertNotEqual(archive_result.returncode, 0)
        self.assertFalse((self.home / ".hydra").exists())
        self.assertNotIn(str(self.home), archive_result.stderr)
        arguments = curl_arguments.read_text(encoding="utf-8")
        self.assertIn("--max-filesize", arguments)
        self.assertIn("536870912", arguments)

    def test_checksum_mismatch_never_invokes_tar_or_creates_install_root(self) -> None:
        tar_marker = self.root / "tar-called"
        tar = self.shims / "tar"
        tar.write_text(
            f"#!/bin/sh\n: > '{tar_marker}'\nexit 99\n",
            encoding="utf-8",
        )
        tar.chmod(0o755)
        rows = self.state.manifest.splitlines(keepends=True)
        rows[0] = b"0" * 64 + rows[0][64:]
        self.state.manifest = b"".join(rows)

        result = self.run_installer("--version", "1.0.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(tar_marker.exists())
        self.assertFalse((self.home / ".hydra").exists())

    def test_shell_preflight_rejects_hostile_archive_without_leaking_names(self) -> None:
        hostile = "do-not-leak-hostile\nmember"
        member = tarfile.TarInfo(f"hydra-codex-1.0.0/runtime/{hostile}")
        member.size = 1
        path = self.root / "hostile.tar.gz"
        content = make_archive(
            path,
            "1.0.0",
            "darwin-arm64",
            extra=(member, b"x"),
        )
        self.publish("1.0.0", selected_archive=content)

        result = self.run_installer("--version", "1.0.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".hydra").exists())
        self.assertNotIn("do-not-leak-hostile", result.stderr)
        self.assertNotIn(str(self.home), result.stderr)

    def test_shell_preflight_rejects_owner_unreadable_directories(self) -> None:
        for mode in (0o100, 0o300):
            with self.subTest(mode=oct(mode)):
                member = tarfile.TarInfo(
                    "hydra-codex-1.0.0/runtime/unreadable-directory/"
                )
                member.type = tarfile.DIRTYPE
                member.mode = mode
                path = self.root / f"unreadable-directory-{mode:o}.tar.gz"
                content = make_archive(
                    path,
                    "1.0.0",
                    "darwin-arm64",
                    extra=(member, None),
                )
                self.publish("1.0.0", selected_archive=content)

                result = self.run_installer("--version", "1.0.0")

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(str(self.home), result.stderr)
                self.assertNotIn("unreadable-directory", result.stderr)
                self.assertFalse((self.home / ".hydra").exists())

    def test_unreadable_required_members_fail_without_private_path_output(self) -> None:
        relatives = (
            "VERSION",
            "TARGET",
            "LICENSE",
            "install.sh",
            "marketplace/.agents/plugins/marketplace.json",
            "marketplace/plugins/hydra-codex/.codex-plugin/plugin.json",
        )
        for index, relative in enumerate(relatives):
            with self.subTest(relative=relative):
                path = self.root / f"unreadable-{index}.tar.gz"
                content = make_archive(
                    path,
                    "1.0.0",
                    "darwin-arm64",
                    mode_overrides={relative: 0},
                )
                self.publish("1.0.0", selected_archive=content)
                result = self.run_installer("--version", "1.0.0")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(str(self.home), result.stderr)
                self.assertNotIn(relative, result.stderr)
                self.assertFalse((self.home / ".hydra").exists())

    def test_staged_version_mismatch_preserves_previous_release_and_links(self) -> None:
        first = self.run_installer("--version", "1.0.0")
        self.assertEqual(first.returncode, 0, first.stderr)
        before_current = os.readlink(self.current)
        before_launcher = os.readlink(self.launcher)
        self.publish("2.0.0", version_output="9.9.9")

        failed = self.run_installer("--version", "2.0.0")

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(os.readlink(self.current), before_current)
        self.assertEqual(os.readlink(self.launcher), before_launcher)
        self.assertFalse(
            (self.home / ".hydra" / "versions" / "2.0.0").exists(),
        )

    def test_foreign_launcher_current_and_install_root_are_preserved(self) -> None:
        cases = ("launcher", "current", "root")
        for case in cases:
            with self.subTest(case=case):
                foreign_home = self.root / f"foreign-{case}"
                foreign_home.mkdir(mode=0o700)
                if case == "root":
                    foreign = foreign_home / ".hydra"
                    foreign.write_bytes(b"foreign-root")
                else:
                    hydra = foreign_home / ".hydra"
                    hydra.mkdir(mode=0o700)
                    foreign = (
                        foreign_home / ".local" / "bin" / "hydra-codex"
                        if case == "launcher"
                        else hydra / "current"
                    )
                    foreign.parent.mkdir(parents=True, exist_ok=True)
                    foreign.write_bytes(f"foreign-{case}".encode())
                before = foreign.read_bytes()
                result = self.run_installer(
                    "--version",
                    "1.0.0",
                    environment={"HOME": str(foreign_home)},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(foreign.read_bytes(), before)

    def test_check_rejects_symlinked_launcher_ancestor_without_mutation(self) -> None:
        foreign = self.root / "foreign-local-directory"
        foreign.mkdir()
        local = self.home / ".local"
        local.symlink_to(foreign, target_is_directory=True)
        before = os.readlink(local)

        result = self.run_installer("--check")

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(local.is_symlink())
        self.assertEqual(os.readlink(local), before)
        self.assertEqual(tuple(foreign.iterdir()), ())

    def test_check_is_fully_read_only_for_missing_current_and_update_states(self) -> None:
        before = tuple(self.home.rglob("*"))
        missing = self.run_installer("--check")
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(tuple(self.home.rglob("*")), before)
        self.assertFalse((self.home / "uninstall-called").exists())

        installed = self.run_installer("--version", "1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        inventory = sorted(
            (str(path.relative_to(self.home)), path.is_symlink())
            for path in self.home.rglob("*")
        )
        up_to_date = self.run_installer("--check")
        self.publish("2.0.0")
        update = self.run_installer("--check")
        self.assertEqual((up_to_date.returncode, update.returncode), (0, 0))
        self.assertEqual(
            sorted(
                (str(path.relative_to(self.home)), path.is_symlink())
                for path in self.home.rglob("*")
            ),
            inventory,
        )
        self.assertIn("up to date", up_to_date.stdout.lower())
        self.assertIn("update available", update.stdout.lower())

    def test_check_rejects_active_bundle_for_a_different_platform_read_only(self) -> None:
        installed = self.run_installer("--version", "1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        inventory = sorted(
            (str(path.relative_to(self.home)), path.is_symlink())
            for path in self.home.rglob("*")
        )

        result = self.run_installer(
            "--check",
            environment={"TEST_UNAME_S": "Linux", "TEST_UNAME_M": "x86_64"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            sorted(
                (str(path.relative_to(self.home)), path.is_symlink())
                for path in self.home.rglob("*")
            ),
            inventory,
        )

    def test_uninstall_only_delegates_to_valid_active_bundle(self) -> None:
        installed = self.run_installer("--version", "1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        current_before = os.readlink(self.current)
        launcher_before = os.readlink(self.launcher)

        result = self.run_installer("--uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "uninstall-called").is_file())
        self.assertEqual(os.readlink(self.current), current_before)
        self.assertEqual(os.readlink(self.launcher), launcher_before)
        keep_cli = self.run_installer("--uninstall", "--keep-cli")
        self.assertNotEqual(keep_cli.returncode, 0)

    def test_unsupported_platform_and_invalid_arguments_fail_without_mutation(self) -> None:
        cases = (
            ((), {"TEST_UNAME_S": "Linux", "TEST_UNAME_M": "aarch64"}),
            (("--version", "../1.0.0"), {}),
            (("--version", "01.0.0"), {}),
            (("--unknown",), {}),
            (("--check", "--version", "1.0.0"), {}),
        )
        for arguments, environment in cases:
            with self.subTest(arguments=arguments, environment=environment):
                result = self.run_installer(*arguments, environment=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / ".hydra").exists())

    def test_concurrent_installs_share_one_private_lock_domain(self) -> None:
        self.state.delay_archive = 0.4
        values = {
            "HOME": str(self.home),
            "PATH": f"{self.shims}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HYDRA_INSTALLER_RELEASE_BASE_URL": self.release_base,
            "HYDRA_TEST_ACTIVATOR": str(self.activator),
            "PYTHONPATH": str(ROOT / "src"),
            "LC_ALL": "C",
        }
        command = ("sh", str(INSTALLER), "--version", "1.0.0")
        first = subprocess.Popen(
            command,
            cwd=ROOT,
            env=values,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        second = subprocess.run(
            command,
            cwd=ROOT,
            env=values,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=15)

        returncodes = (first.returncode, second.returncode)
        self.assertEqual(sum(code == 0 for code in returncodes), 1)
        self.assertEqual(sum(code != 0 for code in returncodes), 1)
        self.assertTrue(self.launcher.is_symlink())
        self.assertNotIn(str(self.home), second.stderr + first_stderr)
        self.assertNotIn(str(self.home), first_stdout)

    def test_preexisting_lock_is_never_removed_by_a_nonowner(self) -> None:
        lock = self.home / ".hydra-installer-lock"
        lock.mkdir(mode=0o700)

        result = self.run_installer("--version", "1.0.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.is_dir())


if __name__ == "__main__":
    unittest.main()
