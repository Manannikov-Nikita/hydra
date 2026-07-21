from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydra_codex.rollout import RolloutRoot, ingest_rollouts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore, StorageUnavailable


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

    def test_unchanged_materialized_source_skips_scanner_and_preserves_counts(self) -> None:
        source = self.base / "active" / "unchanged.jsonl"
        write(source, [self.meta(), self.token(10)])
        first = ingest_rollouts(
            self.store, (RolloutRoot(source, "active"),), self.project,
            "project-b2", hash_key=self.key,
        )
        stable_counts = {
            table: self.store.count(table)
            for table in (
                "rollout_sources", "rollout_logical_sources", "rollout_events",
                "token_snapshots", "rollout_diagnostics",
            )
        }

        with patch(
            "hydra_codex.rollout.scan_source",
            side_effect=AssertionError("unchanged source must not be scanned"),
        ):
            second = ingest_rollouts(
                self.store, (RolloutRoot(source, "archived"),), self.project,
                "project-b2", hash_key=self.key,
            )

        self.assertEqual((first.files_seen, first.unique_sources), (1, 1))
        self.assertEqual((second.files_seen, second.unique_sources), (1, 1))
        self.assertEqual(
            {
                table: self.store.count(table)
                for table in stable_counts
            },
            stable_counts,
        )
        self.assertEqual(self.store.count("rollout_source_location_states"), 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT location_type FROM rollout_source_locations"
            ).fetchone()[0],
            "archived",
        )
        self.assertNotIn(str(source), "\n".join(self.store.connection.iterdump()))

    def test_fast_hit_restats_before_skipping_scanner(self) -> None:
        source = self.base / "active" / "racing-hit.jsonl"
        initial = [self.meta(identity="racing-hit"), self.token(10)]
        appended = initial + [self.token(20, "2026-07-21T00:00:02Z")]
        write(source, initial)
        ingest_rollouts(
            self.store, (source,), self.project, "project-b2", hash_key=self.key,
        )
        from hydra_codex import rollout as rollout_module

        original = rollout_module._unchanged_location

        def mutate_after_candidate(*args, **kwargs):
            candidate = original(*args, **kwargs)
            write(source, appended)
            return candidate

        with (
            patch(
                "hydra_codex.rollout._unchanged_location",
                side_effect=mutate_after_candidate,
            ),
            patch(
                "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
            ) as scanner,
        ):
            ingest_rollouts(
                self.store, (source,), self.project, "project-b2", hash_key=self.key,
            )

        self.assertEqual(scanner.call_count, 1)
        self.assertIn(
            "append",
            {row[0] for row in self.store.connection.execute(
                "SELECT relation FROM rollout_sources"
            )},
        )
        self.assertEqual(self.store.count("token_snapshots"), 2)

    def test_changed_metadata_scans_and_materializes_append(self) -> None:
        source = self.base / "active" / "append.jsonl"
        initial = [self.meta(), self.token(10)]
        write(source, initial)
        ingest_rollouts(
            self.store, (source,), self.project, "project-b2", hash_key=self.key,
        )
        write(source, initial + [self.token(20, "2026-07-21T00:00:02Z")])
        from hydra_codex import rollout as rollout_module

        with patch(
            "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
        ) as scanner:
            ingest_rollouts(
                self.store, (source,), self.project, "project-b2", hash_key=self.key,
            )

        self.assertEqual(scanner.call_count, 1)
        self.assertIn(
            "append",
            {row[0] for row in self.store.connection.execute(
                "SELECT relation FROM rollout_sources"
            )},
        )
        self.assertEqual(self.store.count("token_snapshots"), 2)

    def test_new_archived_location_scans_once_then_fast_paths(self) -> None:
        active = self.base / "active" / "relocated.jsonl"
        archived = self.base / "archived" / "relocated.jsonl"
        rows = [self.meta(), self.token(10)]
        write(active, rows)
        ingest_rollouts(
            self.store, (RolloutRoot(active, "active"),), self.project,
            "project-b2", hash_key=self.key,
        )
        write(archived, rows)
        from hydra_codex import rollout as rollout_module

        with patch(
            "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
        ) as scanner:
            ingest_rollouts(
                self.store, (RolloutRoot(archived, "archived"),), self.project,
                "project-b2", hash_key=self.key,
            )
        self.assertEqual(scanner.call_count, 1)
        with patch(
            "hydra_codex.rollout.scan_source",
            side_effect=AssertionError("known archive location must fast-path"),
        ):
            ingest_rollouts(
                self.store, (RolloutRoot(archived, "archived"),), self.project,
                "project-b2", hash_key=self.key,
            )

        self.assertEqual(self.store.count("rollout_logical_sources"), 1)
        self.assertEqual(self.store.count("rollout_source_locations"), 2)
        self.assertEqual(self.store.count("rollout_source_location_states"), 2)

    def test_known_revision_with_foreign_logical_source_fails_before_location_mutation(self) -> None:
        source = self.base / "active" / "foreign-known-revision.jsonl"
        write(source, [self.meta(identity="foreign-known-revision"), self.token(10)])
        ingest_rollouts(
            self.store, (source,), self.project, "project-b2", hash_key=self.key,
        )
        revision = self.store.connection.execute(
            "SELECT source_digest FROM rollout_sources"
        ).fetchone()[0]
        self.store.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES ('foreign-logical','foreign-project',NULL,NULL,'clean')"""
        )
        self.store.connection.execute(
            "UPDATE rollout_sources SET logical_source_key='foreign-logical' "
            "WHERE source_digest=?",
            (revision,),
        )
        self.store.connection.commit()
        details = source.stat()
        os.utime(
            source,
            ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000_000),
        )
        before = "\n".join(self.store.connection.iterdump())

        with self.assertRaisesRegex(StorageUnavailable, "project"):
            ingest_rollouts(
                self.store, (source,), self.project, "project-b2", hash_key=self.key,
            )

        self.assertEqual("\n".join(self.store.connection.iterdump()), before)

    def test_scanner_version_mismatch_forces_scan_and_refreshes_state(self) -> None:
        source = self.base / "active" / "scanner-version.jsonl"
        write(source, [self.meta(), self.token(10)])
        ingest_rollouts(
            self.store, (source,), self.project, "project-b2", hash_key=self.key,
        )
        self.store.connection.execute(
            "UPDATE rollout_source_location_states SET scanner_version=-1"
        )
        self.store.connection.commit()
        from hydra_codex import rollout as rollout_module

        with patch(
            "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
        ) as scanner:
            ingest_rollouts(
                self.store, (source,), self.project, "project-b2", hash_key=self.key,
            )

        self.assertEqual(scanner.call_count, 1)
        self.assertGreater(
            self.store.connection.execute(
                "SELECT scanner_version FROM rollout_source_location_states"
            ).fetchone()[0],
            0,
        )

    def test_poisoned_location_state_cannot_fast_path(self) -> None:
        cases = ("project", "logical", "revision", "materialized")
        targets = {
            case: self.base / "active" / f"poison-{case}.jsonl"
            for case in cases
        }
        decoy = self.base / "active" / "poison-decoy.jsonl"
        for index, (case, source) in enumerate(targets.items(), start=1):
            write(source, [self.meta(identity=f"poison-{case}"), self.token(index)])
        write(decoy, [self.meta(identity="poison-decoy"), self.token(99)])
        ingest_rollouts(
            self.store, (*targets.values(), decoy), self.project,
            "project-b2", hash_key=self.key,
        )
        hasher = Pseudonymizer(self.key)

        def state_for(path: Path):
            location = hasher.digest("source", str(path.resolve()))
            row = self.store.connection.execute(
                """SELECT logical_source_key,revision_digest
                     FROM rollout_source_location_states
                    WHERE project_id='project-b2' AND location_key=?""",
                (location,),
            ).fetchone()
            return location, str(row[0]), str(row[1])

        _, decoy_logical, decoy_revision = state_for(decoy)
        from hydra_codex import rollout as rollout_module

        for case, source in targets.items():
            with self.subTest(case=case):
                location, logical, revision = state_for(source)
                if case == "project":
                    self.store.connection.execute(
                        """UPDATE rollout_source_location_states
                              SET project_id='poison-project'
                            WHERE project_id='project-b2' AND location_key=?""",
                        (location,),
                    )
                elif case == "logical":
                    self.store.connection.execute(
                        """INSERT INTO rollout_source_locations(
                               logical_source_key,location_key,location_type,revision_digest)
                           VALUES (?,?,'explicit',?)""",
                        (decoy_logical, location, revision),
                    )
                    self.store.connection.execute(
                        """UPDATE rollout_source_location_states
                              SET logical_source_key=?
                            WHERE project_id='project-b2' AND location_key=?""",
                        (decoy_logical, location),
                    )
                elif case == "revision":
                    self.store.connection.execute(
                        """UPDATE rollout_source_location_states
                              SET revision_digest=?
                            WHERE project_id='project-b2' AND location_key=?""",
                        (decoy_revision, location),
                    )
                else:
                    self.store.connection.execute(
                        "UPDATE rollout_sources SET materialized=0 WHERE source_digest=?",
                        (revision,),
                    )
                self.store.connection.commit()

                with patch(
                    "hydra_codex.rollout.scan_source",
                    wraps=rollout_module.scan_source,
                ) as scanner:
                    ingest_rollouts(
                        self.store, (source,), self.project, "project-b2",
                        hash_key=self.key,
                    )

                self.assertEqual(scanner.call_count, 1)
                repaired = self.store.connection.execute(
                    """SELECT state.logical_source_key,state.revision_digest,
                              source.materialized
                         FROM rollout_source_location_states AS state
                         JOIN rollout_sources AS source
                           ON source.source_digest=state.revision_digest
                        WHERE state.project_id='project-b2'
                          AND state.location_key=?""",
                    (location,),
                ).fetchone()
                self.assertEqual(tuple(repaired), (logical, revision, 1))

    def test_truncate_and_rewrite_are_safe_lineage_relations(self) -> None:
        source = self.base / "active" / "thread.jsonl"
        original = [self.meta(), self.token(10), self.token(20, "2026-07-21T00:00:02Z")]
        write(source, original)
        ingest_rollouts(self.store, (RolloutRoot(source, "active"),), self.project, "project-b2", hash_key=self.key)

        write(source, original[:2])
        from hydra_codex import rollout as rollout_module

        with patch(
            "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
        ) as truncate_scanner:
            ingest_rollouts(self.store, (RolloutRoot(source, "active"),), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(truncate_scanner.call_count, 1)
        self.assertEqual(self.store.count("token_snapshots"), 2)
        self.assertIn("truncate", {row[0] for row in self.store.connection.execute("SELECT relation FROM rollout_sources")})

        write(source, [self.meta(), self.token(15)])
        with patch(
            "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
        ) as rewrite_scanner:
            ingest_rollouts(self.store, (RolloutRoot(source, "active"),), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(rewrite_scanner.call_count, 1)
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
        self.assertEqual(self.store.count("rollout_source_location_states"), 1)
        before = self.store.count("rollout_sources")
        from hydra_codex import rollout as rollout_module

        with patch(
            "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
        ) as retry_scanner:
            ingest_rollouts(self.store, (retry,), self.project, "project-b2", hash_key=self.key)
        self.assertEqual(retry_scanner.call_count, 1)
        self.assertGreater(self.store.count("rollout_sources"), before)
        self.assertEqual(self.store.count("token_snapshots"), 2)
        self.assertEqual(self.store.count("rollout_source_location_states"), 2)

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
        self.assertEqual(self.store.count("rollout_source_location_states"), 0)

    def test_metadata_race_after_parse_rolls_back_and_retry_scans(self) -> None:
        source = self.base / "rollouts" / "metadata-race.jsonl"
        write(source, [self.meta(), self.token(10)])
        original = __import__("hydra_codex.rollout", fromlist=["_parse_source"])._parse_source

        def touch_after_parse(*args, **kwargs):
            result = original(*args, **kwargs)
            details = source.stat()
            os.utime(
                source,
                ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000_000),
            )
            return result

        with patch("hydra_codex.rollout._parse_source", side_effect=touch_after_parse):
            with self.assertRaisesRegex(RuntimeError, "changed during ingest"):
                ingest_rollouts(
                    self.store, (source,), self.project, "project-b2", hash_key=self.key,
                )
        self.assertEqual(self.store.count("rollout_sources"), 0)
        self.assertEqual(self.store.count("rollout_source_location_states"), 0)

        from hydra_codex import rollout as rollout_module

        with patch(
            "hydra_codex.rollout.scan_source", wraps=rollout_module.scan_source,
        ) as retry_scanner:
            ingest_rollouts(
                self.store, (source,), self.project, "project-b2", hash_key=self.key,
            )
        self.assertEqual(retry_scanner.call_count, 1)
        self.assertEqual(self.store.count("rollout_source_location_states"), 1)

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

    def test_recognized_privacy_ignored_shapes_do_not_persist_content_or_diagnostics(self) -> None:
        sentinels = {
            "world_state": "private-world-state-sentinel",
            "agent_reasoning": "private-agent-reasoning-sentinel",
            "agent_message": "private-agent-message-sentinel",
            "user_message": "private-user-message-sentinel",
            "reasoning": "private-reasoning-sentinel",
            "message": "private-message-sentinel",
        }
        source = self.base / "rollouts" / "ignored-shapes.jsonl"
        write(source, [
            self.meta(),
            envelope("world_state", {"state": {"content": sentinels["world_state"]}}),
            envelope("event_msg", {"type": "agent_reasoning", "message": sentinels["agent_reasoning"]}),
            envelope("event_msg", {"type": "agent_message", "message": sentinels["agent_message"]}),
            envelope("event_msg", {"type": "user_message", "message": sentinels["user_message"]}),
            envelope("response_item", {"type": "reasoning", "summary": [{"text": sentinels["reasoning"]}]}),
            envelope("response_item", {"type": "message", "content": [{"text": sentinels["message"]}]}),
        ])

        report = ingest_rollouts(
            self.store, (source,), self.project, "project-b2", hash_key=self.key,
        )

        self.assertEqual(report.diagnostics, 0)
        self.assertEqual(self.store.count("rollout_diagnostics"), 0)
        dump = "\n".join(self.store.connection.iterdump())
        for sentinel in sentinels.values():
            self.assertNotIn(sentinel, dump)

    def test_unknown_schema_values_remain_diagnostic_without_raw_content(self) -> None:
        unknown_envelope = "private-unknown-envelope"
        unknown_event = "private-unknown-event"
        unknown_response = "private-unknown-response"
        sentinels = (
            "private-unknown-envelope-content",
            "private-unknown-event-content",
            "private-unknown-response-content",
        )
        source = self.base / "rollouts" / "unknown-schema.jsonl"
        write(source, [
            self.meta(),
            envelope(unknown_envelope, {"content": sentinels[0]}),
            envelope("event_msg", {"type": unknown_event, "content": sentinels[1]}),
            envelope("response_item", {"type": unknown_response, "content": sentinels[2]}),
        ])

        report = ingest_rollouts(
            self.store, (source,), self.project, "project-b2", hash_key=self.key,
        )

        self.assertEqual(report.diagnostics, 3)
        kinds = {row[0] for row in self.store.connection.execute(
            "SELECT envelope_kind FROM rollout_diagnostics"
        )}
        self.assertEqual(
            kinds,
            {"unknown_envelope", "unknown_event_type", "unknown_response_type"},
        )
        dump = "\n".join(self.store.connection.iterdump())
        for private_value in (unknown_envelope, unknown_event, unknown_response, *sentinels):
            self.assertNotIn(private_value, dump)

    def test_object_and_array_schema_discriminators_are_private_diagnostics(self) -> None:
        sentinels = tuple(
            f"private-non-string-discriminator-{index}" for index in range(6)
        )
        source = self.base / "rollouts" / "non-string-schema.jsonl"
        write(source, [
            self.meta(),
            envelope({"future_type": sentinels[0]}, {"content": sentinels[0]}),
            envelope([sentinels[1]], {"content": sentinels[1]}),
            envelope("event_msg", {
                "type": {"future_type": sentinels[2]}, "content": sentinels[2],
            }),
            envelope("event_msg", {
                "type": [sentinels[3]], "content": sentinels[3],
            }),
            envelope("response_item", {
                "type": {"future_type": sentinels[4]}, "content": sentinels[4],
            }),
            envelope("response_item", {
                "type": [sentinels[5]], "content": sentinels[5],
            }),
        ])

        try:
            report = ingest_rollouts(
                self.store, (source,), self.project, "project-b2", hash_key=self.key,
            )
        except TypeError as error:
            self.fail(f"non-string discriminator escaped privacy handling: {error}")

        self.assertEqual(report.diagnostics, 6)
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                """SELECT envelope_kind,COUNT(*)
                     FROM rollout_diagnostics
                    GROUP BY envelope_kind ORDER BY envelope_kind"""
            )],
            [
                ("unknown_envelope", 2),
                ("unknown_event_type", 2),
                ("unknown_response_type", 2),
            ],
        )
        dump = "\n".join(self.store.connection.iterdump())
        for sentinel in sentinels:
            self.assertNotIn(sentinel, dump)

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
        self.store.connection.execute(
            "UPDATE token_snapshots SET epoch=99,contributes_total=0"
        )
        self.store.connection.commit()

        with patch(
            "hydra_codex.rollout.scan_source",
            side_effect=AssertionError("reconciliation must not require rescanning"),
        ):
            ingest_rollouts(
                self.store, (first, resumed), self.project, "project-b2", hash_key=self.key,
            )

        self.assertEqual(
            self.store.connection.execute(
                "SELECT input_tokens FROM fork_baselines"
            ).fetchone()[0],
            10,
        )
        self.assertEqual(
            {
                tuple(row) for row in self.store.connection.execute(
                    "SELECT epoch,contributes_total FROM token_snapshots"
                )
            },
            {(0, 1)},
        )


if __name__ == "__main__":
    unittest.main()
