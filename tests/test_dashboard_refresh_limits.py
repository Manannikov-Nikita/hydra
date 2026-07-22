from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from hydra_codex.dashboard_refresh import (
    CachedRollout,
    GlobalRefreshRunner,
    GlobalRolloutPlan,
    ProjectPartition,
)
from hydra_codex.rollout_identity import TrustedRolloutCandidate
from hydra_codex.rollout_sources import SourceStat
from hydra_codex.storage import HydraStore


KEY = b"r" * 32


class CachedRefreshDescriptorLimitTests(unittest.TestCase):
    def test_many_cached_sources_do_not_hold_one_descriptor_each(self) -> None:
        try:
            import resource
        except ImportError:
            self.skipTest("resource limits are unavailable")
        old_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        if old_limit[1] != resource.RLIM_INFINITY and old_limit[1] < 64:
            self.skipTest("hard descriptor limit is below the test limit")
        if old_limit[0] != resource.RLIM_INFINITY and old_limit[0] < 64:
            self.skipTest("soft descriptor limit is already below the test limit")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cached = []
            for index in range(80):
                source = (root / f"cached-{index}.jsonl").resolve()
                source.write_text("{}\n", encoding="utf-8")
                details = source.stat()
                source_stat = SourceStat(
                    details.st_dev, details.st_ino, details.st_size,
                    details.st_mtime_ns, details.st_ctime_ns,
                )
                candidate = TrustedRolloutCandidate(source, "active", source, True)
                cached.append(CachedRollout(
                    "project-cached", f"logical-{index}", "revision",
                    f"location-{index}", "active", candidate, source_stat,
                ))
            plan = GlobalRolloutPlan((
                ProjectPartition("project-cached", (), tuple(cached)),
            ), 80, 80, 0, ())
            reconciles: list[str] = []
            runner = GlobalRefreshRunner(
                lambda: HydraStore(root / "hydra.sqlite3"), KEY,
                SimpleNamespace(_refresh_snapshots_from_store=lambda *_a, **_k: {}),
                planner=lambda *_a, **_k: plan,
                reconciler=lambda _store, project_id, _key: reconciles.append(project_id),
                cached_refresher=lambda *_args: None,
            )

            resource.setrlimit(resource.RLIMIT_NOFILE, (64, old_limit[1]))
            try:
                result = runner.run(lambda *_args: None)
            finally:
                resource.setrlimit(resource.RLIMIT_NOFILE, old_limit)

        self.assertEqual(result.diagnostic_codes, ())
        self.assertEqual(reconciles, ["project-cached"])
        self.assertTrue(result.replace_all)


if __name__ == "__main__":
    unittest.main()
