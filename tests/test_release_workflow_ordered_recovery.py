from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import test_release_workflow_recovery as recovery


class OrderedPublishedReleaseRecoveryTests(unittest.TestCase):
    def test_lost_patch_recovery_runs_all_steps_without_mutation(self) -> None:
        workflow = recovery._document()
        publish = workflow["jobs"]["publish"]
        steps = [
            recovery._step(publish, name)["run"]
            for name in (
                "Require newer stable release",
                "Require absent or draft release",
                "Reconcile draft release assets",
                "Verify and publish release",
            )
        ]
        helper = recovery.ReleaseWorkflowRecoveryTests()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin, gh_log, curl_log = helper._write_release_fakes(root)
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
            asset_directory = root / "assets"
            asset_directory.mkdir()
            assets = []
            for asset_id, name in enumerate(
                helper._release_asset_names(),
                start=910,
            ):
                content = f"exact:{name}".encode()
                (dist / name).write_bytes(content)
                (asset_directory / str(asset_id)).write_bytes(content)
                assets.append({"id": asset_id, "name": name})
            published = helper._release_fixture(
                draft=False,
                immutable=True,
                assets=assets,
            )
            state = helper._write_release_state(
                root,
                release=published,
                latest_status=200,
                tag_status=200,
                published=published,
                sha=sha,
            )
            env = helper._release_env(
                root,
                fake_bin,
                gh_log,
                curl_log,
                state,
                asset_directory,
                sha,
            )

            results = [
                subprocess.run(
                    ["bash", "-c", script],
                    cwd=root,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for script in steps
            ]

            self.assertEqual(
                [result.returncode for result in results],
                [0, 0, 0, 0],
                "\n".join(result.stderr for result in results),
            )
            calls = [
                json.loads(line)
                for line in gh_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(any(call[:2] == ["release", "create"] for call in calls))
            self.assertFalse(any("--method" in call and "PATCH" in call for call in calls))
            self.assertFalse(curl_log.exists())
            for asset in assets:
                endpoint = (
                    "repos/Manannikov-Nikita/hydra/releases/assets/"
                    f"{asset['id']}"
                )
                self.assertEqual(sum(call[-1] == endpoint for call in calls), 2)


if __name__ == "__main__":
    unittest.main()
