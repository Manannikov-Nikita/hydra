from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydra_codex.rollout_identity import (
    RolloutRoot,
    TrustedRolloutCandidate,
    discover_trusted_rollouts,
    revalidate_trusted_rollout,
)
from hydra_codex.rollout_sources import (
    SourceChanged,
    SourceScan,
    SourceStat,
    scan_source,
)


def write_rollout(path: Path, *, identity: str = "private-thread") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "timestamp": "2026-07-22T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": identity,
                "session_id": "private-conversation",
                "cwd": "/private/worktree",
            },
        }) + "\n",
        encoding="utf-8",
    )


class SourceScanTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.key = b"s" * 32

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def scan(self, path: Path) -> SourceScan:
        return scan_source(
            path,
            self.key,
            lambda domain, value: f"safe-{domain}-{len(value)}",
        )

    def test_scan_carries_frozen_exact_stat_and_private_fields_stay_out_of_repr(self) -> None:
        source = self.root / "private-name.jsonl"
        write_rollout(source)

        scan = self.scan(source)

        details = source.stat()
        self.assertEqual(scan.path, source.resolve())
        self.assertEqual(
            scan.source_stat,
            SourceStat(
                dev=details.st_dev,
                ino=details.st_ino,
                size=details.st_size,
                mtime_ns=details.st_mtime_ns,
                ctime_ns=details.st_ctime_ns,
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            scan.source_stat.size = 0  # type: ignore[misc]
        rendered = repr(scan)
        for private_value in (
            "private-thread",
            "private-conversation",
            "/private/worktree",
            "private-name.jsonl",
        ):
            self.assertNotIn(private_value, rendered)

    def test_non_regular_source_raises_source_changed(self) -> None:
        with self.assertRaisesRegex(SourceChanged, "changed during ingest"):
            self.scan(self.root)

    def test_metadata_change_during_stream_raises_source_changed(self) -> None:
        source = self.root / "changing.jsonl"
        write_rollout(source)
        from hydra_codex import rollout_sources

        original_fingerprint = rollout_sources.line_fingerprint

        def fingerprint_then_touch(value: bytes, key: bytes) -> str:
            result = original_fingerprint(value, key)
            details = os.stat(source)
            os.utime(
                source,
                ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000),
            )
            return result

        with (
            patch(
                "hydra_codex.rollout_sources.line_fingerprint",
                side_effect=fingerprint_then_touch,
            ),
            self.assertRaisesRegex(SourceChanged, "changed during ingest"),
        ):
            self.scan(source)

    def test_symlink_swap_to_same_inode_between_stat_and_open_fails_closed(self) -> None:
        target = self.root / "target.jsonl"
        source = self.root / "source.jsonl"
        write_rollout(target)
        os.link(target, source)
        from hydra_codex import rollout_sources

        original_source_stat = rollout_sources.source_stat
        calls = 0
        original: SourceStat | None = None

        def stat_then_swap(path: Path) -> SourceStat:
            nonlocal calls, original
            calls += 1
            if calls == 1:
                original = original_source_stat(path)
                source.unlink()
                source.symlink_to(target)
            assert original is not None
            return original

        with (
            patch(
                "hydra_codex.rollout_sources.source_stat",
                side_effect=stat_then_swap,
            ),
            self.assertRaisesRegex(SourceChanged, "changed during ingest"),
        ):
            self.scan(source)


class TrustedRolloutDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovery_is_sorted_deduplicated_labeled_and_repr_private(self) -> None:
        first = self.root / "active" / "z" / "first.jsonl"
        second = self.root / "active" / "a" / "second.jsonl"
        write_rollout(first, identity="first")
        write_rollout(second, identity="second")

        candidates = discover_trusted_rollouts((
            RolloutRoot(first, "archived"),
            RolloutRoot(self.root / "active", "active"),
        ))

        self.assertEqual(
            [(candidate.path, candidate.label) for candidate in candidates],
            [(second.resolve(), "active"), (first.resolve(), "active")],
        )
        self.assertTrue(all(isinstance(item, TrustedRolloutCandidate) for item in candidates))
        for candidate in candidates:
            rendered = repr(candidate)
            self.assertIn(candidate.label, rendered)
            self.assertNotIn(str(self.root), rendered)
            self.assertNotIn(candidate.path.name, rendered)

    def test_discovery_accepts_only_labeled_active_or_archived_roots(self) -> None:
        source = self.root / "source.jsonl"
        write_rollout(source)

        for roots in ((source,), (RolloutRoot(source, "explicit"),)):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                discover_trusted_rollouts(roots)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TrustedRolloutCandidate(source, "explicit", source, True)

    def test_symlink_files_directories_root_and_escapes_are_rejected(self) -> None:
        active = self.root / "active"
        external = self.root / "external"
        accepted = active / "nested" / "accepted.jsonl"
        write_rollout(accepted, identity="accepted")
        write_rollout(external / "escaped.jsonl", identity="escaped")
        (active / "file-link.jsonl").symlink_to(external / "escaped.jsonl")
        (active / "directory-link").symlink_to(external, target_is_directory=True)
        root_link = self.root / "root-link"
        root_link.symlink_to(active, target_is_directory=True)

        candidates = discover_trusted_rollouts((
            RolloutRoot(active, "active"),
            RolloutRoot(root_link, "archived"),
        ))

        self.assertEqual(
            [(candidate.path, candidate.label) for candidate in candidates],
            [(accepted.resolve(), "active")],
        )

    def test_revalidation_rejects_file_and_directory_component_swaps(self) -> None:
        active = self.root / "active"
        source = active / "nested" / "thread.jsonl"
        external = self.root / "external"
        write_rollout(source)
        write_rollout(external / "thread.jsonl", identity="external")
        candidate = discover_trusted_rollouts((RolloutRoot(active, "active"),))[0]
        self.assertIsInstance(revalidate_trusted_rollout(candidate), SourceStat)

        source.unlink()
        source.symlink_to(external / "thread.jsonl")
        with self.assertRaisesRegex(SourceChanged, "changed during ingest"):
            revalidate_trusted_rollout(candidate)

        source.unlink()
        write_rollout(source)
        candidate = discover_trusted_rollouts((RolloutRoot(active, "active"),))[0]
        nested = active / "nested"
        moved = active / "moved"
        nested.rename(moved)
        nested.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(SourceChanged, "changed during ingest"):
            revalidate_trusted_rollout(candidate)

if __name__ == "__main__":
    unittest.main()
