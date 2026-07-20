from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydra_codex.rollout import RolloutRoot, ingest_rollouts
from hydra_codex.storage import HydraStore


def envelope(kind: object, payload: dict, timestamp: object = "2026-07-21T00:00:00Z") -> dict:
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class RolloutLineageB2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text('project_id = "project-b2"\n', encoding="utf-8")
        self.store = HydraStore(self.base / "hydra.sqlite3")
        self.key = b"b" * 32

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def meta(
        self, *, identity: object = "thread", session_id: object = None,
        timestamp: object = "2026-07-21T00:00:00Z", payload_timestamp: object = None,
        parent_thread_id: object = None,
    ) -> dict:
        payload = {"id": identity, "cwd": str(self.project)}
        if session_id is not None:
            payload["session_id"] = session_id
        if payload_timestamp is not None:
            payload["timestamp"] = payload_timestamp
        if parent_thread_id is not None:
            payload["parent_thread_id"] = parent_thread_id
        return envelope("session_meta", payload, timestamp)

    @staticmethod
    def token(value: int, timestamp: str = "2026-07-21T00:00:01Z") -> dict:
        return envelope("event_msg", {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": value, "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }}}, timestamp)

    def test_append_archive_and_repeated_identical_events_preserve_occurrences(self) -> None:
        active = self.base / "active" / "thread.jsonl"
        repeated = self.token(10)
        initial = [self.meta(), repeated, repeated]
        write(active, initial)
        ingest_rollouts(self.store, (RolloutRoot(active, "active"),), self.project, "project-b2", hash_key=self.key)

        appended = initial + [self.token(20, "2026-07-21T00:00:02Z")]
        write(active, appended)
        ingest_rollouts(self.store, (RolloutRoot(active, "active"),), self.project, "project-b2", hash_key=self.key)

        archived = self.base / "archived" / "thread.jsonl"
        write(archived, appended)
        ingest_rollouts(self.store, (RolloutRoot(archived, "archived"),), self.project, "project-b2", hash_key=self.key)

        self.assertEqual(self.store.count("rollout_logical_sources"), 1)
        self.assertEqual(self.store.count("rollout_sources"), 2)
        self.assertEqual(self.store.count("rollout_events"), 4)
        self.assertEqual(self.store.count("token_snapshots"), 3)
        self.assertEqual(self.store.count("rollout_source_locations"), 2)

    def test_truncate_and_rewrite_are_safe_lineage_relations(self) -> None:
        source = self.base / "active" / "thread.jsonl"
        original = [self.meta(), self.token(10), self.token(20, "2026-07-21T00:00:02Z")]
        write(source, original)
        ingest_rollouts(self.store, (RolloutRoot(source, "active"),), self.project, "project-b2", hash_key=self.key)

        write(source, original[:2])
        ingest_rollouts(self.store, (RolloutRoot(source, "active"),), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("token_snapshots"), 2)
        self.assertIn("truncate", {row[0] for row in self.store.connection.execute("SELECT relation FROM rollout_sources")})

        write(source, [self.meta(), self.token(15)])
        ingest_rollouts(self.store, (RolloutRoot(source, "active"),), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("token_snapshots"), 2)
        self.assertEqual(self.store.connection.execute("SELECT lineage_state FROM rollout_logical_sources").fetchone()[0], "conflicted")
        self.assertIn("source_rewrite", {row[0] for row in self.store.connection.execute("SELECT envelope_kind FROM rollout_diagnostics")})

    def test_appended_revision_seen_before_short_archive_prefix_keeps_one_source(self) -> None:
        full = [self.meta(), self.token(10), self.token(20, "2026-07-21T00:00:02Z")]
        active = self.base / "active" / "thread.jsonl"
        archived = self.base / "archived" / "thread.jsonl"
        write(active, full)
        ingest_rollouts(self.store, (RolloutRoot(active, "active"),), self.project, "project-b2", hash_key=self.key)
        write(archived, full[:2])
        ingest_rollouts(self.store, (RolloutRoot(archived, "archived"),), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("rollout_logical_sources"), 1)
        self.assertEqual(self.store.count("token_snapshots"), 2)
        self.assertEqual({row[0] for row in self.store.connection.execute("SELECT relation FROM rollout_sources")}, {"initial", "truncate"})

    def test_root_labels_fail_closed_and_plain_roots_use_explicit(self) -> None:
        with self.assertRaises(ValueError):
            RolloutRoot(self.base, "private-label")
        source = self.base / "rollouts" / "thread.jsonl"
        write(source, [self.meta()])
        ingest_rollouts(self.store, (source,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.connection.execute("SELECT location_type FROM rollout_source_locations").fetchone()[0], "explicit")

    def test_null_identity_fallback_invalid_timestamp_and_unknown_kind_are_private(self) -> None:
        secret_kind = "future/private-secret-kind"
        source = self.base / "rollouts" / "thread.jsonl"
        write(source, [
            self.meta(identity=None, session_id="fallback-thread", timestamp="not-a-time"),
            envelope(secret_kind, {"private_field": "private-secret-value"}),
            self.token(10),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-b2", hash_key=self.key)

        self.assertEqual(self.store.count("rollout_sessions"), 1)
        self.assertEqual(self.store.count("fork_baselines"), 0)
        kinds = {row[0] for row in self.store.connection.execute("SELECT envelope_kind FROM rollout_diagnostics")}
        self.assertIn("invalid_timestamp", kinds)
        self.assertIn("unknown_envelope", kinds)
        dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn(secret_kind, dump)
        self.assertNotIn("private-secret-value", dump)
        self.assertNotIn("fallback-thread", dump)
        self.assertNotIn("not-a-time", dump)

    def test_rollout_content_is_streamed_and_parse_failure_is_retryable(self) -> None:
        source = self.base / "rollouts" / "thread.jsonl"
        write(source, [self.meta(), self.token(10)])
        with patch.object(Path, "read_bytes", side_effect=AssertionError("rollouts must stream")):
            ingest_rollouts(self.store, (source,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("token_snapshots"), 1)

        retry = self.base / "rollouts" / "retry.jsonl"
        write(retry, [self.meta(identity="retry"), self.token(20)])
        with patch("hydra_codex.rollout._parse_source", side_effect=RuntimeError("parse failure")):
            with self.assertRaises(RuntimeError):
                ingest_rollouts(self.store, (retry,), self.project, "project-b2", hash_key=self.key)
        before = self.store.count("rollout_sources")
        ingest_rollouts(self.store, (retry,), self.project, "project-b2", hash_key=self.key)
        self.assertGreater(self.store.count("rollout_sources"), before)
        self.assertEqual(self.store.count("token_snapshots"), 2)

    def test_mutation_between_preflight_and_parse_rolls_back(self) -> None:
        source = self.base / "rollouts" / "mutating.jsonl"
        write(source, [self.meta(), self.token(10)])
        original = __import__("hydra_codex.rollout", fromlist=["_parse_source"])._parse_source

        def mutate_then_parse(*args, **kwargs):
            write(source, [self.meta(), self.token(99)])
            return original(*args, **kwargs)

        with patch("hydra_codex.rollout._parse_source", side_effect=mutate_then_parse):
            with self.assertRaisesRegex(RuntimeError, "changed during ingest"):
                ingest_rollouts(self.store, (source,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("rollout_sources"), 0)
        self.assertEqual(self.store.count("token_snapshots"), 0)

    def test_unknown_inner_types_are_fingerprinted_without_raw_values(self) -> None:
        source = self.base / "rollouts" / "unknown-inner.jsonl"
        write(source, [
            self.meta(),
            envelope("event_msg", {"type": "private-event-secret", "private": "event-value"}),
            envelope("response_item", {"type": "private-response-secret", "private": "response-value"}),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-b2", hash_key=self.key)
        kinds = {row[0] for row in self.store.connection.execute("SELECT envelope_kind FROM rollout_diagnostics")}
        self.assertTrue({"unknown_event_type", "unknown_response_type"}.issubset(kinds))
        dump = "\n".join(self.store.connection.iterdump())
        for secret in ("private-event-secret", "event-value", "private-response-secret", "response-value"):
            self.assertNotIn(secret, dump)

    def test_divergent_copy_at_new_location_conflicts_instead_of_double_counting(self) -> None:
        active = self.base / "active" / "thread.jsonl"
        copy = self.base / "archived" / "thread.jsonl"
        write(active, [self.meta(), self.token(10)])
        ingest_rollouts(self.store, (RolloutRoot(active, "active"),), self.project, "project-b2", hash_key=self.key)
        write(copy, [self.meta(), self.token(99)])
        ingest_rollouts(self.store, (RolloutRoot(copy, "archived"),), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("rollout_logical_sources"), 1)
        self.assertEqual(self.store.count("token_snapshots"), 1)
        self.assertEqual(self.store.connection.execute("SELECT lineage_state FROM rollout_logical_sources").fetchone()[0], "conflicted")

    def test_resume_uses_meta_observation_for_segment_but_payload_time_for_session_start(self) -> None:
        original_start = "2026-07-21T00:00:00Z"
        first = self.base / "active" / "thread-first.jsonl"
        resumed = self.base / "active" / "thread-resumed.jsonl"
        write(first, [
            self.meta(timestamp=original_start, payload_timestamp=original_start),
            self.token(10, "2026-07-21T00:00:01Z"),
        ])
        write(resumed, [
            self.meta(timestamp="2026-07-21T01:00:00Z", payload_timestamp=original_start),
            self.token(20, "2026-07-21T01:00:01Z"),
        ])
        ingest_rollouts(self.store, (first, resumed), self.project, "project-b2", hash_key=self.key)
        session = self.store.connection.execute(
            "SELECT started_at,resume_segments FROM rollout_sessions"
        ).fetchone()
        self.assertEqual(tuple(session), (original_start, 2))
        self.assertEqual(self.store.count("rollout_logical_sources"), 2)

    def test_binary_preflight_accepts_crlf_without_normalizing_line_identity(self) -> None:
        source = self.base / "rollouts" / "thread-crlf.jsonl"
        source.parent.mkdir(parents=True)
        rows = [self.meta(), self.token(10)]
        source.write_bytes(b"".join(
            json.dumps(item, sort_keys=True).encode("utf-8") + b"\r\n" for item in rows
        ))
        ingest_rollouts(self.store, (source,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("token_snapshots"), 1)

    def test_negative_delta_never_becomes_fork_baseline(self) -> None:
        source = self.base / "rollouts" / "thread-negative.jsonl"
        write(source, [
            self.meta(
                timestamp="2026-07-21T00:00:05Z",
                payload_timestamp="2026-07-21T00:00:05Z",
                parent_thread_id="parent",
            ),
            self.token(10, "2026-07-21T00:00:04Z"),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("fork_baselines"), 0)

    def test_resume_segment_does_not_reopen_fork_baseline_window(self) -> None:
        first = self.base / "rollouts" / "thread-first.jsonl"
        resumed = self.base / "rollouts" / "thread-resumed.jsonl"
        write(first, [
            self.meta(parent_thread_id="parent"),
            self.token(10, "2026-07-21T00:00:02Z"),
        ])
        write(resumed, [
            self.meta(
                timestamp="2026-07-21T01:00:00Z",
                payload_timestamp="2026-07-21T01:00:00Z",
                parent_thread_id="parent",
            ),
            self.token(999, "2026-07-21T01:00:00.500000Z"),
        ])
        ingest_rollouts(self.store, (first, resumed), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("fork_baselines"), 0)

    def test_reverse_resume_replaces_stale_baseline_from_global_earliest_start(self) -> None:
        first = self.base / "rollouts" / "thread-first.jsonl"
        resumed = self.base / "rollouts" / "thread-resumed.jsonl"
        write(first, [
            self.meta(parent_thread_id="parent"),
            self.token(10, "2026-07-21T00:00:00.500000Z"),
        ])
        write(resumed, [
            self.meta(
                timestamp="2026-07-21T01:00:00Z",
                payload_timestamp="2026-07-21T01:00:00Z",
                parent_thread_id="parent",
            ),
            self.token(999, "2026-07-21T01:00:00.500000Z"),
        ])

        ingest_rollouts(self.store, (resumed,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT input_tokens FROM fork_baselines"
            ).fetchone()[0],
            999,
        )
        ingest_rollouts(self.store, (first,), self.project, "project-b2", hash_key=self.key)

        self.assertEqual(
            self.store.connection.execute(
                "SELECT input_tokens FROM fork_baselines"
            ).fetchone()[0],
            10,
        )

    def test_reverse_resume_deletes_stale_baseline_without_earliest_candidate(self) -> None:
        first = self.base / "rollouts" / "thread-first.jsonl"
        resumed = self.base / "rollouts" / "thread-resumed.jsonl"
        write(first, [
            self.meta(parent_thread_id="parent"),
            self.token(10, "2026-07-21T00:00:02Z"),
        ])
        write(resumed, [
            self.meta(
                timestamp="2026-07-21T01:00:00Z",
                payload_timestamp="2026-07-21T01:00:00Z",
                parent_thread_id="parent",
            ),
            self.token(999, "2026-07-21T01:00:00.500000Z"),
        ])

        ingest_rollouts(self.store, (resumed,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(self.store.count("fork_baselines"), 1)
        ingest_rollouts(self.store, (first,), self.project, "project-b2", hash_key=self.key)

        self.assertEqual(self.store.count("fork_baselines"), 0)

    def test_known_sources_reconcile_a_preexisting_stale_baseline(self) -> None:
        first = self.base / "rollouts" / "thread-first.jsonl"
        resumed = self.base / "rollouts" / "thread-resumed.jsonl"
        write(first, [
            self.meta(parent_thread_id="parent"),
            self.token(10, "2026-07-21T00:00:00.500000Z"),
        ])
        write(resumed, [
            self.meta(
                timestamp="2026-07-21T01:00:00Z",
                payload_timestamp="2026-07-21T01:00:00Z",
                parent_thread_id="parent",
            ),
            self.token(999, "2026-07-21T01:00:00.500000Z"),
        ])
        ingest_rollouts(
            self.store, (first, resumed), self.project, "project-b2", hash_key=self.key,
        )
        self.store.connection.execute("UPDATE fork_baselines SET input_tokens=999")
        self.store.connection.commit()

        ingest_rollouts(
            self.store, (first, resumed), self.project, "project-b2", hash_key=self.key,
        )

        self.assertEqual(
            self.store.connection.execute(
                "SELECT input_tokens FROM fork_baselines"
            ).fetchone()[0],
            10,
        )


if __name__ == "__main__":
    unittest.main()
