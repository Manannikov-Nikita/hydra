from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydra_codex.rollout import Pseudonymizer, ingest_rollouts
from hydra_codex.storage import HydraStore


def record(kind: str, payload: dict, timestamp: str) -> dict:
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class RolloutReviewFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project_a = self.base / "project-a"
        self.project_b = self.base / "project-b"
        for root, project_id in ((self.project_a, "project-a"), (self.project_b, "project-b")):
            (root / ".hydra").mkdir(parents=True)
            (root / ".hydra" / "project.toml").write_text(
                f'project_id = "{project_id}"\n', encoding="utf-8",
            )
        self.store = HydraStore(self.base / "hydra.sqlite3")
        self.key = b"r" * 32

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def meta(self, *, parent: str | None = None) -> dict:
        payload = {"id": "thread", "cwd": str(self.project_a)}
        if parent is not None:
            payload["parent_thread_id"] = parent
        return record("session_meta", payload, "2026-07-21T00:00:00Z")

    @staticmethod
    def token(value: int, timestamp: str, *, cache_write: int | None = None) -> dict:
        usage = {
            "input_tokens": value,
            "cached_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
        }
        if cache_write is not None:
            usage["cache_write_input_tokens"] = cache_write
        return record("event_msg", {"type": "token_count", "info": {"total_token_usage": usage}}, timestamp)

    def test_cross_project_failed_materialization_cannot_poison_same_revision(self) -> None:
        source = self.base / "rollouts" / "thread.jsonl"
        write(source, [self.meta(), self.token(10, "2026-07-21T00:00:01Z")])

        ingest_rollouts(self.store, (source,), self.project_b, "project-b", hash_key=self.key)
        ingest_rollouts(self.store, (source,), self.project_a, "project-a", hash_key=self.key)

        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM token_snapshots WHERE project_id='project-a'"
        ).fetchone()[0], 1)

    def test_renamed_archive_append_reuses_prefix_lineage(self) -> None:
        active = self.base / "active" / "thread.jsonl"
        renamed = self.base / "archive" / "renamed-thread.jsonl"
        prefix = [self.meta(), self.token(10, "2026-07-21T00:00:01Z")]
        write(active, prefix)
        ingest_rollouts(self.store, (active,), self.project_a, "project-a", hash_key=self.key)

        write(renamed, prefix + [self.token(20, "2026-07-21T00:00:02Z")])
        ingest_rollouts(self.store, (renamed,), self.project_a, "project-a", hash_key=self.key)

        self.assertEqual(self.store.count("rollout_logical_sources"), 1)
        self.assertEqual(self.store.count("token_snapshots"), 2)

    def test_renamed_divergent_relocation_quarantines_same_logical_segment(self) -> None:
        active = self.base / "active" / "thread.jsonl"
        renamed = self.base / "archive" / "renamed-thread.jsonl"
        write(active, [self.meta(), self.token(10, "2026-07-21T00:00:01Z")])
        ingest_rollouts(self.store, (active,), self.project_a, "project-a", hash_key=self.key)

        write(renamed, [self.meta(), self.token(99, "2026-07-21T00:00:02Z")])
        ingest_rollouts(self.store, (renamed,), self.project_a, "project-a", hash_key=self.key)

        self.assertEqual(self.store.count("rollout_logical_sources"), 1)
        self.assertEqual(self.store.count("token_snapshots"), 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT lineage_state FROM rollout_logical_sources"
            ).fetchone()[0],
            "conflicted",
        )

    def test_rollout_filename_selects_exact_child_suffix_before_replayed_parent(self) -> None:
        source = (
            self.base / "rollouts" /
            "rollout-2026-07-21T00-00-00.000Z-parent-child.jsonl"
        )
        write(source, [
            record("session_meta", {
                "id": "parent-child", "cwd": str(self.project_a),
                "parent_thread_id": "parent",
            }, "2026-07-21T00:00:00Z"),
            record("session_meta", {
                "id": "parent", "cwd": str(self.project_a),
            }, "2026-07-21T00:00:01Z"),
            self.token(10, "2026-07-21T00:00:02Z"),
        ])

        ingest_rollouts(self.store, (source,), self.project_a, "project-a", hash_key=self.key)

        hasher = Pseudonymizer(self.key)
        child_key = hasher.digest("identity", "parent-child")
        parent_key = hasher.digest("identity", "parent")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT session_key FROM token_snapshots"
            ).fetchone()[0],
            child_key,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT child_key,parent_key,confidence_kind FROM session_edges"
            ).fetchone()),
            (child_key, parent_key, "confirmed"),
        )

    def test_same_timestamp_remains_separate_for_distinct_session_identities(self) -> None:
        first = self.base / "rollouts" / "first.jsonl"
        second = self.base / "rollouts" / "second.jsonl"
        common_time = "2026-07-21T00:00:00Z"
        write(first, [
            record("session_meta", {"id": "thread-a", "cwd": str(self.project_a)}, common_time),
            self.token(10, "2026-07-21T00:00:01Z"),
        ])
        write(second, [
            record("session_meta", {"id": "thread-b", "cwd": str(self.project_a)}, common_time),
            self.token(20, "2026-07-21T00:00:01Z"),
        ])

        ingest_rollouts(self.store, (first, second), self.project_a, "project-a", hash_key=self.key)

        self.assertEqual(self.store.count("rollout_sessions"), 2)
        self.assertEqual(self.store.count("rollout_logical_sources"), 2)
        self.assertEqual(self.store.count("token_snapshots"), 2)

    def test_missing_cache_write_baseline_stays_null(self) -> None:
        source = self.base / "rollouts" / "child.jsonl"
        write(source, [self.meta(parent="parent"), self.token(10, "2026-07-21T00:00:00.500000Z")])
        ingest_rollouts(self.store, (source,), self.project_a, "project-a", hash_key=self.key)

        baseline = self.store.connection.execute(
            "SELECT cache_write_tokens FROM fork_baselines"
        ).fetchone()
        self.assertIsNotNone(baseline)
        self.assertIsNone(baseline[0])

    def test_counter_reset_emits_safe_diagnostic(self) -> None:
        source = self.base / "rollouts" / "reset.jsonl"
        write(source, [
            self.meta(),
            self.token(20, "2026-07-21T00:00:01Z", cache_write=2),
            self.token(2, "2026-07-21T00:00:02Z", cache_write=1),
        ])
        ingest_rollouts(self.store, (source,), self.project_a, "project-a", hash_key=self.key)

        kinds = {row[0] for row in self.store.connection.execute(
            "SELECT envelope_kind FROM rollout_diagnostics"
        )}
        self.assertIn("counter_reset", kinds)

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_preexisting_installation_key_is_enforced_to_user_only(self) -> None:
        directory = self.base / "keys-existing"
        directory.mkdir()
        key_path = directory / "rollout-hmac.key"
        key_path.write_bytes(b"e" * 32)
        key_path.chmod(0o644)

        hasher = Pseudonymizer.installation(directory)

        self.assertEqual(hasher.key, b"e" * 32)
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_race_winning_installation_key_is_enforced_to_user_only(self) -> None:
        directory = self.base / "keys-race"
        directory.mkdir()
        key_path = directory / "rollout-hmac.key"

        def win_race(_temporary: str, destination: Path) -> None:
            Path(destination).write_bytes(b"w" * 32)
            Path(destination).chmod(0o644)
            raise FileExistsError

        with patch("hydra_codex.rollout_identity.os.link", side_effect=win_race):
            hasher = Pseudonymizer.installation(directory)

        self.assertEqual(hasher.key, b"w" * 32)
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
