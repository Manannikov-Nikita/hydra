from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _document() -> dict[str, object]:
    text = WORKFLOW.read_text(encoding="utf-8")
    payload = json.loads(
        "\n".join(
            line for line in text.splitlines()
            if not re.fullmatch(r"\s*#.*", line)
        ),
    )
    if not isinstance(payload, dict):
        raise AssertionError("workflow root must be a mapping")
    return payload


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


class ReleaseWorkflowRecoveryTests(unittest.TestCase):
    def test_draft_release_lookup_recovers_from_tag_404_via_paginated_list(
        self,
    ) -> None:
        workflow = _document()
        publish = workflow["jobs"]["publish"]
        step_names = (
            "Require absent or draft release",
            "Reconcile draft release assets",
            "Verify and publish release",
        )
        for name in step_names:
            script = _step(publish, name)["run"]
            with self.subTest(step=name):
                self.assertIn("gh api --paginate --slurp", script)
                self.assertIn("releases?per_page=100", script)
                self.assertIn("release list response is invalid", script)
                self.assertIn("release tag is ambiguous", script)

        preflight = _step(
            publish,
            "Require absent or draft release",
        )["run"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_GH_LOG\"\n"
                "if [ \"$1\" != api ]; then exit 64; fi\n"
                "if [ \"$2\" = --include ]; then\n"
                "  printf 'HTTP/1.1 404 fake\\n\\n'\n"
                "  exit 1\n"
                "fi\n"
                "if [ \"$2\" = --paginate ] && [ \"$3\" = --slurp ]; then\n"
                "  printf '%s' \"$FAKE_RELEASE_PAGES\"\n"
                "  exit 0\n"
                "fi\n"
                "exit 64\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            (fake_bin / "python").symlink_to(Path(os.sys.executable).resolve())
            log = root / "gh.log"
            release = {
                "id": 359004778,
                "tag_name": "v0.1.0",
                "draft": True,
                "prerelease": False,
                "assets": [],
            }
            result = subprocess.run(
                ["bash", "-c", preflight],
                cwd=root,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "RUNNER_TEMP": str(root),
                    "GITHUB_REPOSITORY": "Manannikov-Nikita/hydra",
                    "GITHUB_REF_NAME": "v0.1.0",
                    "FAKE_GH_LOG": str(log),
                    "FAKE_RELEASE_PAGES": json.dumps([[], [release]]),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "api --paginate --slurp "
                "repos/Manannikov-Nikita/hydra/releases?per_page=100",
                log.read_text(encoding="utf-8"),
            )

    def test_draft_release_list_lookup_fails_closed_on_invalid_candidates(
        self,
    ) -> None:
        workflow = _document()
        publish = workflow["jobs"]["publish"]
        preflight = _step(
            publish,
            "Require absent or draft release",
        )["run"]
        valid = {
            "id": 359004778,
            "tag_name": "v0.1.0",
            "draft": True,
            "prerelease": False,
            "assets": [],
        }
        cases = {
            "duplicate exact tag": [[valid], [{**valid, "id": 359004779}]],
            "malformed page": [{"not": "a page"}],
            "malformed candidate": [[{"tag_name": "v0.1.0"}]],
            "published candidate": [[{**valid, "draft": False}]],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" != api ]; then exit 64; fi\n"
                "if [ \"$2\" = --include ]; then\n"
                "  printf 'HTTP/1.1 404 fake\\n\\n'\n"
                "  exit 1\n"
                "fi\n"
                "if [ \"$2\" = --paginate ] && [ \"$3\" = --slurp ]; then\n"
                "  printf '%s' \"$FAKE_RELEASE_PAGES\"\n"
                "  exit 0\n"
                "fi\n"
                "exit 64\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            (fake_bin / "python").symlink_to(Path(os.sys.executable).resolve())
            for name, pages in cases.items():
                with self.subTest(case=name):
                    result = subprocess.run(
                        ["bash", "-c", preflight],
                        cwd=root,
                        env={
                            **os.environ,
                            "PATH": (
                                f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
                            ),
                            "RUNNER_TEMP": str(root),
                            "GITHUB_REPOSITORY": "Manannikov-Nikita/hydra",
                            "GITHUB_REF_NAME": "v0.1.0",
                            "FAKE_RELEASE_PAGES": json.dumps(pages),
                        },
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        f"{name} unexpectedly passed",
                    )

    def _release_fixture(
        self,
        *,
        draft: bool,
        immutable: bool,
        assets: list[dict[str, object]],
        upload_url: str | None = None,
    ) -> dict[str, object]:
        release_id = 359004778
        return {
            "id": release_id,
            "tag_name": "v0.1.0",
            "draft": draft,
            "prerelease": False,
            "immutable": immutable,
            "assets": assets,
            "upload_url": upload_url or (
                "https://uploads.github.com/repos/Manannikov-Nikita/hydra/"
                f"releases/{release_id}/assets{{?name,label}}"
            ),
        }

    def _release_asset_names(self) -> list[str]:
        return [
            "SHA256SUMS",
            "hydra-codex-0.1.0-darwin-arm64.tar.gz",
            "hydra-codex-0.1.0-darwin-x86_64.tar.gz",
            "hydra-codex-0.1.0-linux-x86_64.tar.gz",
        ]

    def _write_release_fakes(self, root: Path) -> tuple[Path, Path, Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        gh_log = root / "gh.log"
        curl_log = root / "curl.log"
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(
            f"#!{Path(os.sys.executable).resolve()}\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "\n"
            "args = sys.argv[1:]\n"
            "with Path(os.environ['FAKE_GH_LOG']).open('a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "state = json.loads(Path(os.environ['FAKE_RELEASE_STATE']).read_text())\n"
            "if args[:2] == ['release', 'create']:\n"
            "    raise SystemExit(65)\n"
            "if not args or args[0] != 'api':\n"
            "    raise SystemExit(64)\n"
            "endpoint = args[-1]\n"
            "if '--include' in args:\n"
            "    if endpoint.endswith('/releases/latest'):\n"
            "        status = state['latest_status']\n"
            "    elif '/releases/tags/' in endpoint:\n"
            "        status = state['tag_status']\n"
            "    else:\n"
            "        raise SystemExit(63)\n"
            "    print(f'HTTP/1.1 {status} fake\\n')\n"
            "    raise SystemExit(0 if status == 200 else 1)\n"
            "if '--paginate' in args and '--slurp' in args:\n"
            "    print(json.dumps([[], [state['release']]]))\n"
            "elif endpoint.endswith('/releases/latest'):\n"
            "    print(json.dumps(state['latest']))\n"
            "elif '/releases/tags/' in endpoint:\n"
            "    print(json.dumps(state['release']))\n"
            "elif '/git/ref/tags/' in endpoint:\n"
            "    print(json.dumps({'object': {'type': 'commit', 'sha': state['sha']}}))\n"
            "elif endpoint.endswith('/immutable-releases'):\n"
            "    print(json.dumps({'enabled': state['immutable_enabled'], 'enforced_by_owner': False}))\n"
            "elif '/releases/assets/' in endpoint:\n"
            "    asset_id = endpoint.rsplit('/', 1)[1]\n"
            "    sys.stdout.buffer.write((Path(os.environ['FAKE_ASSETS']) / asset_id).read_bytes())\n"
            "elif '--method' in args and args[args.index('--method') + 1] == 'PATCH':\n"
            "    print(json.dumps(state['published']))\n"
            "else:\n"
            "    raise SystemExit(62)\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        (fake_bin / "python").symlink_to(Path(os.sys.executable).resolve())
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            f"#!{Path(os.sys.executable).resolve()}\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "from urllib.parse import parse_qs, urlsplit\n"
            "import sys\n"
            "\n"
            "args = sys.argv[1:]\n"
            "with Path(os.environ['FAKE_CURL_LOG']).open('a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "url = urlsplit(args[-1])\n"
            "name = parse_qs(url.query).get('name', [None])[0]\n"
            "data = args[args.index('--data-binary') + 1]\n"
            "if url.netloc != 'uploads.github.com' or not name or not data.startswith('@'):\n"
            "    raise SystemExit(61)\n"
            "Path(data[1:]).read_bytes()\n"
            "print(json.dumps({'id': 900000000 + len(name), 'name': name}))\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        return fake_bin, gh_log, curl_log

    def _write_release_state(
        self,
        root: Path,
        *,
        release: dict[str, object],
        latest_status: int,
        tag_status: int,
        immutable_enabled: bool = True,
        published: dict[str, object] | None = None,
        sha: str = "0" * 40,
    ) -> Path:
        path = root / "release-state.json"
        path.write_text(
            json.dumps(
                {
                    "release": release,
                    "latest": release,
                    "latest_status": latest_status,
                    "tag_status": tag_status,
                    "immutable_enabled": immutable_enabled,
                    "published": published or release,
                    "sha": sha,
                },
            ),
            encoding="utf-8",
        )
        return path

    def _release_env(
        self,
        root: Path,
        fake_bin: Path,
        gh_log: Path,
        curl_log: Path,
        state: Path,
        assets: Path,
        sha: str = "0" * 40,
    ) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(root),
            "GITHUB_REPOSITORY": "Manannikov-Nikita/hydra",
            "GITHUB_REF_NAME": "v0.1.0",
            "GITHUB_SHA": sha,
            "GH_TOKEN": "fake-token",
            "FAKE_GH_LOG": str(gh_log),
            "FAKE_CURL_LOG": str(curl_log),
            "FAKE_RELEASE_STATE": str(state),
            "FAKE_ASSETS": str(assets),
        }

    def test_reconcile_hidden_draft_resumes_partial_assets_by_validated_ids(
        self,
    ) -> None:
        workflow = _document()
        script = _step(
            workflow["jobs"]["publish"],
            "Reconcile draft release assets",
        )["run"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, gh_log, curl_log = self._write_release_fakes(root)
            dist = root / "dist"
            dist.mkdir()
            for name in self._release_asset_names():
                (dist / name).write_bytes(f"local:{name}".encode())
            existing_name = self._release_asset_names()[1]
            existing_id = 801
            assets = root / "assets"
            assets.mkdir()
            (assets / str(existing_id)).write_bytes(
                (dist / existing_name).read_bytes(),
            )
            release = self._release_fixture(
                draft=True,
                immutable=False,
                assets=[{"id": existing_id, "name": existing_name}],
            )
            state = self._write_release_state(
                root,
                release=release,
                latest_status=404,
                tag_status=404,
            )

            result = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env=self._release_env(
                    root, fake_bin, gh_log, curl_log, state, assets,
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(
                result.returncode,
                0,
                result.stderr + (
                    "\n" + gh_log.read_text(encoding="utf-8")
                    if gh_log.exists()
                    else ""
                ) + (
                    "\nprobe="
                    + (root / "hydra-release-response").read_text(
                        encoding="utf-8",
                    )
                    if (root / "hydra-release-response").exists()
                    else ""
                ),
            )
            gh_calls = [
                json.loads(line)
                for line in gh_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(any(call[:2] == ["release", "create"] for call in gh_calls))
            self.assertTrue(any(
                call[-1].endswith(f"/releases/assets/{existing_id}")
                for call in gh_calls
            ))
            curl_calls = [
                json.loads(line)
                for line in curl_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(curl_calls), 3)
            self.assertFalse(any(
                call[-1].endswith(f"name={existing_name}")
                for call in curl_calls
            ))
            self.assertFalse(any("--clobber" in call for call in gh_calls + curl_calls))

    def test_reconcile_rejects_untrusted_upload_url_before_curl(self) -> None:
        workflow = _document()
        script = _step(
            workflow["jobs"]["publish"],
            "Reconcile draft release assets",
        )["run"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, gh_log, curl_log = self._write_release_fakes(root)
            dist = root / "dist"
            dist.mkdir()
            for name in self._release_asset_names():
                (dist / name).write_bytes(b"local")
            assets = root / "assets"
            assets.mkdir()
            release = self._release_fixture(
                draft=True,
                immutable=False,
                assets=[],
                upload_url="https://attacker.invalid/assets{?name,label}",
            )
            state = self._write_release_state(
                root,
                release=release,
                latest_status=404,
                tag_status=404,
            )

            result = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env=self._release_env(
                    root, fake_bin, gh_log, curl_log, state, assets,
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release upload URL is invalid", result.stderr)
            self.assertFalse(curl_log.exists())

    def test_final_publish_and_lost_response_retry_are_idempotent(
        self,
    ) -> None:
        workflow = _document()
        script = _step(
            workflow["jobs"]["publish"],
            "Verify and publish release",
        )["run"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, gh_log, curl_log = self._write_release_fakes(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
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
            (root / "tracked.txt").write_text("release\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "release"], cwd=root, check=True)
            subprocess.run(["git", "tag", "v0.1.0"], cwd=root, check=True)
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
            ).strip()
            dist = root / "dist"
            dist.mkdir()
            assets_dir = root / "assets"
            assets_dir.mkdir()
            remote_assets = []
            for index, name in enumerate(self._release_asset_names(), start=810):
                content = f"local:{name}".encode()
                (dist / name).write_bytes(content)
                (assets_dir / str(index)).write_bytes(content)
                remote_assets.append({"id": index, "name": name})
            draft = self._release_fixture(
                draft=True,
                immutable=False,
                assets=remote_assets,
            )
            published = self._release_fixture(
                draft=False,
                immutable=True,
                assets=remote_assets,
            )
            state = self._write_release_state(
                root,
                release=draft,
                latest_status=404,
                tag_status=404,
                immutable_enabled=False,
                published=published,
                sha=sha,
            )
            env = self._release_env(
                root, fake_bin, gh_log, curl_log, state, assets_dir, sha,
            )

            disabled = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(disabled.returncode, 0)
            disabled_calls = gh_log.read_text(encoding="utf-8")
            self.assertIn("immutable-releases", disabled_calls)
            self.assertNotIn('"PATCH"', disabled_calls)

            mutable_published = self._release_fixture(
                draft=False,
                immutable=False,
                assets=remote_assets,
            )
            state = self._write_release_state(
                root,
                release=draft,
                latest_status=404,
                tag_status=404,
                published=mutable_published,
                sha=sha,
            )
            gh_log.write_text("", encoding="utf-8")
            env["FAKE_RELEASE_STATE"] = str(state)
            mutable_response = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(mutable_response.returncode, 0)
            self.assertIn(
                "published release state is invalid",
                mutable_response.stderr,
            )

            state = self._write_release_state(
                root,
                release=draft,
                latest_status=404,
                tag_status=404,
                published=published,
                sha=sha,
            )
            gh_log.write_text("", encoding="utf-8")
            env["FAKE_RELEASE_STATE"] = str(state)
            initial = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)
            initial_calls = [
                json.loads(line)
                for line in gh_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                len([call for call in initial_calls if "/releases/assets/" in call[-1]]),
                4,
            )
            self.assertTrue(any("--method" in call and "PATCH" in call for call in initial_calls))

            state = self._write_release_state(
                root,
                release=published,
                latest_status=200,
                tag_status=200,
                published=published,
                sha=sha,
            )
            gh_log.write_text("", encoding="utf-8")
            env["FAKE_RELEASE_STATE"] = str(state)
            retry = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            retry_calls = [
                json.loads(line)
                for line in gh_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                len([call for call in retry_calls if "/releases/assets/" in call[-1]]),
                4,
            )
            self.assertFalse(any(
                "--method" in call and "PATCH" in call
                for call in retry_calls
            ))

    def test_exact_immutable_published_recovery_passes_early_guards(
        self,
    ) -> None:
        workflow = _document()
        publish = workflow["jobs"]["publish"]
        latest_guard = _step(
            publish,
            "Require newer stable release",
        )["run"]
        release_guard = _step(
            publish,
            "Require absent or draft release",
        )["run"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, gh_log, curl_log = self._write_release_fakes(root)
            assets = root / "assets"
            assets.mkdir()
            release = self._release_fixture(
                draft=False,
                immutable=True,
                assets=[],
            )
            state = self._write_release_state(
                root,
                release=release,
                latest_status=200,
                tag_status=200,
            )
            env = self._release_env(
                root, fake_bin, gh_log, curl_log, state, assets,
            )

            latest = subprocess.run(
                ["bash", "-c", latest_guard],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            preflight = subprocess.run(
                ["bash", "-c", release_guard],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(latest.returncode, 0, latest.stderr)
            self.assertEqual(preflight.returncode, 0, preflight.stderr)


if __name__ == "__main__":
    unittest.main()
