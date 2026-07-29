from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
TARGETS = (
    {"runner": "macos-15", "target": "darwin-arm64"},
    {"runner": "macos-15-intel", "target": "darwin-x86_64"},
    {"runner": "ubuntu-24.04", "target": "linux-x86_64"},
)
ACTION_REFS = {
    "actions/checkout": (
        "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "v7.0.1",
    ),
    "actions/setup-python": (
        "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "v7.0.0",
    ),
    "actions/upload-artifact": (
        "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "v7.0.1",
    ),
    "actions/download-artifact": (
        "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        "v8.0.1",
    ),
    "actions/attest": (
        "f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6",
        "v4.2.0",
    ),
}


def _document(name: str) -> tuple[str, dict[str, object]]:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    # Workflows use the JSON subset of YAML. Standalone reviewed-version
    # comments are removed before structural parsing.
    payload = json.loads(
        "\n".join(
            line for line in text.splitlines()
            if not re.fullmatch(r"\s*#.*", line)
        ),
    )
    if not isinstance(payload, dict):
        raise AssertionError("workflow root must be a mapping")
    return text, payload


def _uses(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses":
                if not isinstance(child, str):
                    raise AssertionError("uses must be text")
                found.append(child)
            found.extend(_uses(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_uses(child))
    return found


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise AssertionError("job steps must be a list")
    matching = [
        step for step in steps
        if isinstance(step, dict) and step.get("name") == name
    ]
    if len(matching) != 1:
        raise AssertionError(f"expected exactly one {name!r} step")
    return matching[0]


class WorkflowContractTests(unittest.TestCase):
    def assert_action_refs_are_immutable_and_reviewed(
        self,
        text: str,
        workflow: dict[str, object],
    ) -> None:
        for reference in _uses(workflow):
            action, separator, revision = reference.partition("@")
            self.assertEqual(separator, "@")
            self.assertIn(action, ACTION_REFS)
            expected_revision, version = ACTION_REFS[action]
            self.assertEqual(revision, expected_revision)
            reviewed = (
                rf"# reviewed: {re.escape(action)} {re.escape(version)}\n"
                rf"\s*\{{[^\n]*\"uses\": \"{re.escape(reference)}\""
            )
            self.assertRegex(text, reviewed)

    def test_quality_matrix_runs_all_source_and_native_distribution_gates(self) -> None:
        text, workflow = _document("quality.yml")
        self.assertEqual(set(workflow["on"]), {"push", "pull_request"})
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", text)
        self.assertNotIn("self-hosted", text)
        jobs = workflow["jobs"]
        self.assertIsInstance(jobs, dict)
        self.assertEqual(set(jobs), {"quality"})
        job = jobs["quality"]
        self.assertIsInstance(job, dict)
        self.assertNotIn("permissions", job)
        self.assertEqual(job["runs-on"], "${{ matrix.runner }}")
        self.assertEqual(
            job["strategy"]["matrix"]["include"],
            list(TARGETS),
        )

        architecture = _step(job, "Verify native runner architecture")["run"]
        self.assertIn("uname -s", architecture)
        self.assertIn("uname -m", architecture)
        for target in ("darwin-arm64", "darwin-x86_64", "linux-x86_64"):
            self.assertIn(target, architecture)

        install = _step(job, "Install test and release tooling")["run"]
        self.assertEqual(
            install,
            "python -m pip install --only-binary=:all: --require-hashes "
            "-r requirements/release-tools.txt",
        )
        source = _step(job, "Run full source suite")["run"]
        self.assertIn(
            "python -m unittest discover -s tests -t .",
            source,
        )
        inventory = _step(job, "Build and verify wheel and source archive")["run"]
        self.assertIn("python -m build --no-isolation", inventory)
        self.assertIn("wheel_count", inventory)
        self.assertIn("sdist_count", inventory)
        standalone = _step(job, "Build and accept native standalone archive")["run"]
        self.assertIn("packaging/build_standalone.py", standalone)
        self.assertIn("packaging/accept_standalone.sh", standalone)
        self.assertIn("${{ matrix.target }}", standalone)

        shellcheck = _step(job, "Run pinned ShellCheck")
        self.assertEqual(
            shellcheck["if"],
            "${{ matrix.target == 'linux-x86_64' }}",
        )
        self.assertIn("shellcheck-v0.11.0.linux.x86_64.tar.gz", shellcheck["run"])
        self.assertNotIn("shellcheck-v0.11.0.linux.x86_64.tar.xz", shellcheck["run"])
        self.assertIn(
            "b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6",
            shellcheck["run"],
        )
        self.assertIn("install.sh", shellcheck["run"])
        self.assertIn("packaging/accept_standalone.sh", shellcheck["run"])

        upload = _step(job, "Upload accepted standalone archive")
        self.assertEqual(
            upload["with"]["name"],
            "hydra-standalone-${{ matrix.target }}",
        )
        self.assertEqual(
            upload["with"]["path"],
            "build/standalone/hydra-codex-*.tar.gz",
        )
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        checkout = _step(job, "Checkout source")
        self.assertEqual(checkout["with"], {"persist-credentials": False})
        self.assert_action_refs_are_immutable_and_reviewed(text, workflow)

    def test_release_toolchain_is_exact_and_hash_locked(self) -> None:
        lock = ROOT / "requirements" / "release-tools.txt"
        self.assertTrue(lock.is_file())
        text = lock.read_text(encoding="ascii")
        expected = {
            "altgraph",
            "build",
            "macholib",
            "packaging",
            "pyinstaller",
            "pyinstaller-hooks-contrib",
            "pyproject-hooks",
            "setuptools",
        }
        logical_lines = [
            line for line in text.splitlines()
            if line and not line.startswith("#")
        ]
        names = set()
        for line in logical_lines:
            match = re.fullmatch(
                r"([a-z0-9-]+)==([0-9][a-zA-Z0-9.+-]*)"
                r"(?: --hash=sha256:[0-9a-f]{64})+",
                line,
            )
            self.assertIsNotNone(match, line)
            assert match is not None
            names.add(match.group(1))
        self.assertEqual(names, expected)

        for workflow_name in ("quality.yml", "release.yml"):
            _, workflow = _document(workflow_name)
            jobs = workflow["jobs"]
            selected = jobs["quality"] if workflow_name == "quality.yml" else jobs["build"]
            install = _step(selected, "Install test and release tooling")["run"]
            self.assertIn("--only-binary=:all:", install)
            self.assertIn("--require-hashes", install)
            self.assertIn("requirements/release-tools.txt", install)
            self.assertNotIn(".[test,release]", install)

    def test_release_is_tag_only_and_write_permissions_are_publish_only(self) -> None:
        text, workflow = _document("release.yml")
        self.assertEqual(workflow["on"], {"push": {"tags": ["v*"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["concurrency"],
            {
                "cancel-in-progress": False,
                "group": "hydra-release-publish",
            },
        )
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs), {"verify", "build", "publish"})
        for name in ("verify", "build"):
            self.assertNotIn("permissions", jobs[name])
        self.assertEqual(
            jobs["publish"]["permissions"],
            {
                "artifact-metadata": "write",
                "attestations": "write",
                "contents": "write",
                "id-token": "write",
            },
        )
        for job in jobs.values():
            checkout = _step(job, "Checkout tagged source")
            self.assertFalse(checkout["with"]["persist-credentials"])
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assert_action_refs_are_immutable_and_reviewed(text, workflow)

    def test_release_verifies_exact_semver_clean_tag_and_native_targets(self) -> None:
        _, workflow = _document("release.yml")
        jobs = workflow["jobs"]
        verify = jobs["verify"]
        verification = _step(verify, "Verify exact release tag")["run"]
        self.assertIn("GITHUB_REF_NAME", verification)
        self.assertIn("GITHUB_SHA", verification)
        self.assertIn("hydra_codex.__version__", verification)
        self.assertIn("^{commit}", verification)
        self.assertIn("git status --porcelain", verification)
        self.assertIn(
            "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$",
            verification,
        )

        build = jobs["build"]
        self.assertEqual(build["needs"], ["verify"])
        self.assertEqual(
            build["strategy"]["matrix"]["include"],
            list(TARGETS),
        )
        source = _step(build, "Run full source suite")["run"]
        self.assertIn("python -m unittest discover -s tests -t .", source)
        standalone = _step(build, "Build and accept native standalone archive")["run"]
        self.assertIn("packaging/build_standalone.py", standalone)
        self.assertIn("packaging/accept_standalone.sh", standalone)
        upload = _step(build, "Upload accepted standalone archive")
        self.assertEqual(
            upload["with"]["name"],
            "hydra-standalone-${{ matrix.target }}",
        )

    def test_release_aggregates_exact_assets_attests_then_publishes_without_overwrite(
        self,
    ) -> None:
        _, workflow = _document("release.yml")
        publish = workflow["jobs"]["publish"]
        self.assertEqual(publish["needs"], ["build"])
        steps = publish["steps"]
        names = [step["name"] for step in steps]
        self.assertEqual(
            [
                name for name in names
                if name.startswith("Download ")
            ],
            [
                "Download darwin-arm64",
                "Download darwin-x86_64",
                "Download linux-x86_64",
            ],
        )
        aggregate = _step(publish, "Aggregate exact release assets")["run"]
        for target in ("darwin-arm64", "darwin-x86_64", "linux-x86_64"):
            self.assertIn(target, aggregate)
        self.assertIn("expected_names", aggregate)
        self.assertIn("unexpected release artifact inventory", aggregate)
        self.assertIn("is_symlink", aggregate)
        self.assertIn("iterdir", aggregate)
        self.assertIn("SHA256SUMS", aggregate)
        self.assertIn("for name in sorted(expected_names)", aggregate)
        privileged_scripts = "\n".join(
            str(step.get("run", "")) for step in steps
            if isinstance(step, dict)
        )
        self.assertNotIn("PYTHONPATH=src", privileged_scripts)
        self.assertNotIn("from hydra_codex", privileged_scripts)
        self.assertNotIn("import hydra_codex", privileged_scripts)
        self.assertNotRegex(
            privileged_scripts,
            r"(?<![-\w])python (?!-I )",
        )
        self.assertIn("python -I -", privileged_scripts)

        remote_tag = _step(publish, "Verify remote tag at event commit")
        self.assertEqual(remote_tag["env"], {"GH_TOKEN": "${{ github.token }}"})
        self.assertIn(
            "repos/$GITHUB_REPOSITORY/git/ref/tags/$GITHUB_REF_NAME",
            remote_tag["run"],
        )
        self.assertIn("git/tags/", remote_tag["run"])
        self.assertIn("GITHUB_SHA", remote_tag["run"])

        archive_attestation = _step(publish, "Attest standalone archives")
        self.assertEqual(
            archive_attestation["with"],
            {"subject-checksums": "dist/SHA256SUMS"},
        )
        checksum_attestation = _step(publish, "Attest checksum manifest")
        self.assertEqual(
            checksum_attestation["with"],
            {"subject-path": "dist/SHA256SUMS"},
        )
        preflight_index = names.index("Require absent or draft release")
        reconcile_index = names.index("Reconcile draft release assets")
        verify_index = names.index("Verify and publish release")
        self.assertNotIn("Publish release", names)
        self.assertLess(preflight_index, names.index("Attest standalone archives"))
        preflight = _step(publish, "Require absent or draft release")
        self.assertEqual(preflight["env"], {"GH_TOKEN": "${{ github.token }}"})
        self.assertIn("published releases are immutable", preflight["run"])
        self.assertIn("prerelease", preflight["run"])
        self.assertIn("gh api --include", preflight["run"])
        self.assertIn("404/*)", preflight["run"])
        self.assertIn("release lookup failed", preflight["run"])
        self.assertLess(names.index("Attest standalone archives"), reconcile_index)
        self.assertLess(names.index("Attest checksum manifest"), reconcile_index)
        self.assertLess(reconcile_index, verify_index)

        reconcile = _step(publish, "Reconcile draft release assets")
        self.assertEqual(reconcile["env"], {"GH_TOKEN": "${{ github.token }}"})
        self.assertIn("gh release create", reconcile["run"])
        self.assertIn("--draft", reconcile["run"])
        self.assertIn("--verify-tag", reconcile["run"])
        self.assertIn(
            "for attempt in 1 2 3 4 5 6 7 8",
            reconcile["run"],
        )
        self.assertIn('sleep 1', reconcile["run"])
        self.assertNotIn("gh release upload", reconcile["run"])
        self.assertNotIn("gh release download", reconcile["run"])
        self.assertIn("releases/assets/$asset_id", reconcile["run"])
        self.assertIn("https://uploads.github.com/", reconcile["run"])
        self.assertIn("curl --fail-with-body", reconcile["run"])
        self.assertIn("releases/{release_id}/assets", reconcile["run"])
        self.assertIn(
            "release must be a draft or an immutable published release",
            reconcile["run"],
        )
        self.assertIn("published release asset inventory is incomplete", reconcile["run"])
        self.assertIn('"$release_state")" = "published"', reconcile["run"])
        self.assertIn("remote asset checksum mismatch", reconcile["run"])
        self.assertIn("prerelease", reconcile["run"])
        self.assertIn("gh api --include", reconcile["run"])
        self.assertIn("404/*)", reconcile["run"])
        self.assertIn("release lookup failed", reconcile["run"])
        self.assertNotIn("--clobber", reconcile["run"])
        self.assertNotIn("dist/*", reconcile["run"])
        verify = _step(publish, "Verify and publish release")
        self.assertEqual(verify["env"], {"GH_TOKEN": "${{ github.token }}"})
        self.assertIn("GITHUB_SHA", verify["run"])
        self.assertIn("^{commit}", verify["run"])
        self.assertNotIn("--untracked-files=all", verify["run"])
        self.assertIn("git diff --quiet", verify["run"])
        self.assertIn("git diff --cached --quiet", verify["run"])
        self.assertIn(
            "repos/$GITHUB_REPOSITORY/git/ref/tags/$GITHUB_REF_NAME",
            verify["run"],
        )
        self.assertIn("git/tags/", verify["run"])
        self.assertIn("releases/latest", verify["run"])
        self.assertIn("current_version <= latest_version", verify["run"])
        self.assertNotIn("immutable-releases", verify["run"])
        self.assertNotIn("gh release download", verify["run"])
        self.assertNotIn("gh release upload", verify["run"])
        self.assertNotIn("gh release edit", verify["run"])
        self.assertIn("releases/assets/$asset_id", verify["run"])
        self.assertIn("--method PATCH", verify["run"])
        self.assertIn("releases/$release_id", verify["run"])
        self.assertIn("published release state is invalid", verify["run"])
        self.assertIn("draft release is required", verify["run"])
        self.assertIn("prerelease", verify["run"])
        self.assertIn("remote asset checksum mismatch", verify["run"])
        self.assertGreater(
            verify["run"].index("--method PATCH"),
            verify["run"].index("remote asset checksum mismatch"),
        )
        self.assertGreater(
            verify["run"].index("--method PATCH"),
            verify["run"].rindex("current_version <= latest_version"),
        )
        self.assertGreater(
            verify["run"].rindex("remote asset checksum mismatch"),
            verify["run"].rindex("remote tag does not match event commit"),
        )
        self.assertGreater(
            verify["run"].rindex("remote asset checksum mismatch"),
            verify["run"].rindex("current_version <= latest_version"),
        )
        self.assertFalse(
            any("release" in reference.lower() for reference in _uses(workflow)),
        )

    def test_latest_release_guard_rejects_downgrades_and_transient_lookup_errors(
        self,
    ) -> None:
        _, workflow = _document("release.yml")
        publish = workflow["jobs"]["publish"]
        guard = _step(publish, "Require newer stable release")
        script = guard["run"]
        self.assertIn("releases/latest", script)
        self.assertIn("404/*)", script)
        self.assertIn("current_version <= latest_version", script)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" != api ]; then exit 64; fi\n"
                "if [ \"$2\" = --include ]; then\n"
                "  printf 'HTTP/1.1 %s fake\\n\\n' \"$FAKE_LATEST_STATUS\"\n"
                "  [ \"$FAKE_LATEST_STATUS\" = 200 ]\n"
                "  exit\n"
                "fi\n"
                "printf '%s' \"$FAKE_LATEST_PAYLOAD\"\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            (fake_bin / "python").symlink_to(Path(os.sys.executable).resolve())

            def run_guard(
                tag: str,
                status: int,
                latest_tag: str = "v1.0.0",
            ) -> subprocess.CompletedProcess[str]:
                payload = json.dumps(
                    {
                        "tag_name": latest_tag,
                        "draft": False,
                        "prerelease": False,
                    },
                )
                return subprocess.run(
                    ["bash", "-c", script],
                    cwd=root,
                    env={
                        **os.environ,
                        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                        "RUNNER_TEMP": str(root),
                        "GITHUB_REPOSITORY": "Manannikov-Nikita/hydra",
                        "GITHUB_REF_NAME": tag,
                        "FAKE_LATEST_STATUS": str(status),
                        "FAKE_LATEST_PAYLOAD": payload,
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            self.assertNotEqual(run_guard("v0.9.0", 200).returncode, 0)
            self.assertEqual(run_guard("v1.0.1", 200).returncode, 0)
            self.assertEqual(run_guard("v0.1.0", 404).returncode, 0)
            self.assertNotEqual(run_guard("v1.0.1", 503).returncode, 0)

    def test_final_source_guard_allows_artifacts_but_rejects_tracked_changes(
        self,
    ) -> None:
        _, workflow = _document("release.yml")
        publish = workflow["jobs"]["publish"]
        script = _step(publish, "Verify and publish release")["run"]
        self.assertIn("git diff --quiet", script)
        self.assertIn("git diff --cached --quiet", script)
        self.assertNotIn("--untracked-files=all", script)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "-q"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "hydra@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Hydra Test"],
                cwd=root,
                check=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_text("stable\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test fixture"],
                cwd=root,
                check=True,
            )
            downloads = root / "downloads" / "darwin-arm64"
            downloads.mkdir(parents=True)
            (downloads / "artifact.tar.gz").write_bytes(b"artifact")

            self.assertEqual(
                subprocess.run(["git", "diff", "--quiet"], cwd=root).returncode,
                0,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=root,
                ).returncode,
                0,
            )
            tracked.write_text("mutated\n", encoding="utf-8")
            self.assertNotEqual(
                subprocess.run(["git", "diff", "--quiet"], cwd=root).returncode,
                0,
            )

    def test_public_documentation_covers_operator_privacy_and_recovery_contracts(
        self,
    ) -> None:
        documents = {
            path.name: path.read_text(encoding="utf-8")
            for path in (
                ROOT / "docs" / "installation.md",
                ROOT / "docs" / "upgrade-and-uninstall.md",
                ROOT / "docs" / "privacy.md",
                ROOT / "docs" / "troubleshooting.md",
                ROOT / "docs" / "release-process.md",
            )
        }
        installation = documents["installation.md"]
        for command in (
            "curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh",
            "hydra-codex install -y",
            "hydra-codex init .",
            "hydra-codex status . --json",
            "hydra-codex dashboard",
        ):
            self.assertIn(command, installation)
        path_export = 'export PATH="$HOME/.local/bin:$PATH"'
        self.assertIn(path_export, installation)
        self.assertLess(
            installation.index("curl -fsSL"),
            installation.index(path_export),
        )
        self.assertLess(
            installation.index(path_export),
            installation.index("hydra-codex install -y"),
        )
        self.assertIn("Developer installation", installation)
        self.assertIn("mutable", installation.lower())
        self.assertIn("raw.githubusercontent.com", installation)

        lifecycle = documents["upgrade-and-uninstall.md"]
        for command in (
            "hydra-codex upgrade --check",
            "hydra-codex upgrade",
            "hydra-codex uninstall --keep-cli",
            "hydra-codex uninstall",
            "hydra-codex uninstall -y --keep-cli",
            "hydra-codex uninstall -y",
        ):
            self.assertIn(command, lifecycle)
        self.assertIn("preserves telemetry", lifecycle.lower())

        privacy = documents["privacy.md"]
        privacy_normalized = " ".join(privacy.lower().split())
        self.assertIn(
            "~/Library/Application Support/Hydra/hydra.sqlite3",
            privacy,
        )
        self.assertIn("~/.local/share/hydra/hydra.sqlite3", privacy)
        self.assertIn("raw prompts", privacy_normalized)
        self.assertIn("tool output", privacy_normalized)
        self.assertIn("uninstall", privacy_normalized)
        self.assertIn("preserves", privacy_normalized)

        troubleshooting = documents["troubleshooting.md"].lower()
        for phrase in (
            "path",
            "unsupported platform",
            "codex is missing",
            "integration ownership conflict",
            "checksum",
            "read-only",
            "dashboard port",
            ".hydra-installer-lock",
            "hydra-installer-lock/v2",
            "malformed",
            "legacy directory",
            "prove that no installer is running",
        ):
            self.assertIn(phrase, troubleshooting)

        release = documents["release-process.md"]
        self.assertIn("vMAJOR.MINOR.PATCH", release)
        self.assertIn("SHA256SUMS", release)
        self.assertIn("gh attestation verify", release)
        self.assertIn("sha256sum -c SHA256SUMS", release)
        self.assertIn("shasum -a 256 -c SHA256SUMS", release)
        self.assertIn(
            "gh attestation verify \\\n  SHA256SUMS",
            release,
        )
        self.assertIn(
            "PUT repos/Manannikov-Nikita/hydra/immutable-releases",
            release,
        )
        self.assertIn(
            "GET repos/Manannikov-Nikita/hydra/immutable-releases",
            release,
        )
        self.assertIn("immutable", release.lower())
        self.assertIn("strictly newer", release.lower())
        self.assertIn("older tags", release.lower())


if __name__ == "__main__":
    unittest.main()
