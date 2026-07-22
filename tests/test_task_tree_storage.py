from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.exact_time import require_exact_timestamp
from hydra_codex.storage import HydraStore
from hydra_codex.rollout import Pseudonymizer, ingest_rollouts
from hydra_codex.rollout_reconcile import reconcile_turn_attempts
from hydra_codex.task_tree import TokenVector
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


def stamp(second: int) -> str:
    return datetime(2026, 7, 21, 0, 0, second, tzinfo=timezone.utc).isoformat()


def write_rollout(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class StoredTaskTreeTests(unittest.TestCase):
    def test_adapter_aggregates_normalized_rows_and_respects_edge_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            self.addCleanup(store.close)
            connection = store.connection
            connection.executemany(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,conversation_key,started_at,last_activity_at)
                   VALUES (?, 'project-a', 'worktree', 1, '', ?, ?)""",
                (
                    ("root", stamp(0), stamp(10)),
                    ("confirmed", stamp(1), stamp(8)),
                    ("inferred", stamp(2), stamp(9)),
                ),
            )
            connection.executemany(
                """INSERT INTO session_edges(
                       child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
                   VALUES (?, 'root', NULL, ?, ?)""",
                (("confirmed", "confirmed", 1.0), ("inferred", "inferred", 0.6)),
            )
            connection.executemany(
                """INSERT INTO token_snapshots(
                       source_digest,line_number,session_key,project_id,epoch,input_tokens,
                       cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                       completeness,observed_at)
                   VALUES (?, ?, ?, 'project-a', 0, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    ("src-root", 1, "root", 100, 20, 10, 5, "complete", stamp(8)),
                    ("src-confirmed", 1, "confirmed", 30, 10, 2, 1, "complete", stamp(1)),
                    ("src-confirmed", 2, "confirmed", 50, 15, 4, 2, "complete", stamp(7)),
                    ("src-inferred", 1, "inferred", 40, 10, 3, 1, "complete", stamp(2)),
                    ("src-inferred", 2, "inferred", 60, 20, 5, 2, "complete", stamp(8)),
                ),
            )
            connection.execute(
                """INSERT INTO fork_baselines(
                       child_key,source_digest,line_number,input_tokens,cached_input_tokens,
                       output_tokens,reasoning_tokens,cache_write_tokens,provenance,observed_at)
                   VALUES ('confirmed','src-confirmed',1,30,10,2,1,0,'exact',?)""",
                (stamp(1),),
            )
            connection.execute(
                """INSERT INTO rollout_sources(
                       source_digest,source_type,logical_source_key,relation,line_count,
                       byte_count,chain_digest,materialized)
                   VALUES ('src-root','explicit','logical-root','canonical',10,10,'chain',1)"""
            )
            connection.execute(
                """INSERT INTO rollout_logical_sources(
                       logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
                   VALUES ('logical-root','project-a','root','src-root','clean')"""
            )
            connection.execute(
                """INSERT INTO rollout_events(
                       event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
                       timestamp_quality,fingerprint)
                   VALUES ('event-root','logical-root',10,'event_msg',?,'valid','fingerprint')""",
                (stamp(10),),
            )
            connection.execute(
                """INSERT INTO turn_lifecycle_events(
                       event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                       emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
                   VALUES ('event-root','root','turn-root','completed',?,10,NULL,
                           'src-root','logical-root',10)""",
                (stamp(10),),
            )
            connection.execute(
                """INSERT INTO tool_spans(
                       session_key,call_key,category,terminal_state,latency_ms,tool_name,
                       started_at,finished_at,turn_key,source_digest,source_ordinal,completeness,provenance)
                   VALUES ('root','call-1','instrumentation','success',1,'hydra.annotate',
                           ?,?,'turn-root','src-root',2,'complete','exact')""",
                (stamp(2), stamp(2)),
            )
            connection.execute(
                """INSERT INTO file_observations(
                       source_digest,line_number,session_key,operation,relative_path,path_hash,
                       observed_at,turn_key)
                   VALUES ('src-root',3,'root','read','src/file.py','hash',?,'turn-root')""",
                (stamp(3),),
            )
            connection.execute(
                """INSERT INTO rollout_test_runs(
                       evidence_key,source_digest,line_number,session_key,observed_at,turn_key,
                       tool_call_key,command_hash,runner,scope,exit_status,outcome,failure_cause,
                       retry_kind,attempt_ordinal,provenance,completeness)
                   VALUES ('test-1','src-root',4,'root',?,'turn-root','call-test','command',
                           'pytest','full',0,'success','none','infra_recovery',2,'derived','complete')""",
                (stamp(4),),
            )
            connection.commit()

            reconcile_turn_attempts(connection)
            metrics = aggregate_stored_task_tree(connection, project_id="project-a", root_id="root")

            self.assertEqual(metrics.session_ids, ("confirmed", "inferred", "root"))
            self.assertEqual(metrics.recorded.vector, TokenVector(210, 55, 19, 9))
            self.assertEqual(metrics.replay_baseline.vector, TokenVector(30, 10, 2, 1))
            self.assertEqual(metrics.unique.vector, TokenVector(180, 45, 17, 8))
            self.assertEqual(metrics.unconfirmed_replay_edges, 1)
            self.assertEqual(metrics.tool_calls.value, 1)
            self.assertEqual(metrics.instrumentation_calls.value, 1)
            self.assertEqual(metrics.file_reads.known_lower_bound, 1)
            self.assertEqual(metrics.full_test_runs.value, 1)
            self.assertEqual(metrics.test_retries.value, 1)

    def test_post_cutoff_submicrosecond_replay_baseline_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            self.addCleanup(store.close)
            connection = store.connection
            connection.executemany(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,
                       conversation_key,started_at)
                   VALUES (?, 'project-exact-baseline', 'worktree', 1, '', ?)""",
                (
                    ("root", stamp(0)),
                    ("child", "2026-07-21T00:00:08.500000Z"),
                ),
            )
            connection.execute(
                """INSERT INTO session_edges(
                       child_key,parent_key,baseline_working_tokens,
                       confidence_kind,confidence)
                   VALUES ('child','root',NULL,'confirmed',1.0)"""
            )
            connection.executemany(
                """INSERT INTO token_snapshots(
                       source_digest,line_number,session_key,project_id,epoch,
                       input_tokens,cached_input_tokens,output_tokens,
                       reasoning_tokens,cache_write_tokens,completeness,observed_at)
                   VALUES (?, ?, ?, 'project-exact-baseline', 0,
                           ?,0,0,0,0,'complete',?)""",
                (
                    ("source-root", 1, "root", 100, stamp(5)),
                    (
                        "source-child", 1, "child", 20,
                        "2026-07-21T00:00:08.750000Z",
                    ),
                    (
                        "source-child", 2, "child", 100,
                        "2026-07-21T00:00:09.0000001Z",
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO fork_baselines(
                       child_key,source_digest,line_number,input_tokens,
                       cached_input_tokens,output_tokens,reasoning_tokens,
                       cache_write_tokens,provenance,observed_at)
                   VALUES ('child','source-child',2,100,0,0,0,0,'exact',
                           '2026-07-21T00:00:09.0000001Z')"""
            )
            connection.commit()
            cutoff = require_exact_timestamp("2026-07-21T00:00:09Z")

            metrics = aggregate_stored_task_tree(
                connection,
                project_id="project-exact-baseline",
                root_id="root",
                cutoff_at=cutoff.presentation,
                cutoff_instant=cutoff,
            )

            self.assertEqual(metrics.recorded.working_tokens, 120)
            self.assertEqual(metrics.replay_baseline.working_tokens, 0)
            self.assertEqual(metrics.unique.working_tokens, 120)

    def test_ephemeral_child_without_started_at_is_retained_as_uncertain_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "project-ephemeral"\n', encoding="utf-8",
            )
            source = base / "root.jsonl"
            write_rollout(source, [
                {"timestamp": stamp(0), "type": "session_meta", "payload": {"id": "root", "cwd": str(project)}},
                {"timestamp": stamp(1), "type": "event_msg", "payload": {"type": "sub_agent_activity", "agent_thread_id": "ephemeral"}},
                {"timestamp": stamp(2), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3,
                    "reasoning_output_tokens": 1,
                }}}},
                {"timestamp": stamp(10), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
            ])
            store = HydraStore(base / "hydra.sqlite3")
            self.addCleanup(store.close)
            ingest_rollouts(
                store, (source,), project, "project-ephemeral", hash_key=b"e" * 32,
            )
            keys = Pseudonymizer(b"e" * 32)
            child = keys.digest("identity", "ephemeral")
            self.assertIsNone(store.connection.execute(
                "SELECT started_at FROM rollout_sessions WHERE session_key=?", (child,),
            ).fetchone()[0])

            metrics = aggregate_stored_task_tree(
                store.connection, project_id="project-ephemeral",
                root_id=keys.digest("identity", "root"),
            )

            self.assertEqual(metrics.sessions.value, 2)
            self.assertIn(child, metrics.session_ids)
            self.assertIsNone(metrics.recorded.input.value)
            self.assertEqual(metrics.recorded.input.known_lower_bound, 10)
            self.assertIsNone(metrics.agent_time_ms.value)
            self.assertEqual(metrics.agent_time_ms.known_lower_bound, 10_000)
            self.assertIn("missing_session_start:1", metrics.agent_time_ms.caveats)
            self.assertEqual(metrics.unique.provenance, "estimated")

    def test_snapshot_without_timestamp_contributes_as_estimated_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "project-timestampless"\n', encoding="utf-8",
            )
            source = base / "root.jsonl"
            write_rollout(source, [
                {"timestamp": stamp(0), "type": "session_meta", "payload": {"id": "root", "cwd": str(project)}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10,
                    "reasoning_output_tokens": 5,
                }}}},
                {"timestamp": stamp(10), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
            ])
            store = HydraStore(base / "hydra.sqlite3")
            self.addCleanup(store.close)
            ingest_rollouts(
                store, (source,), project, "project-timestampless", hash_key=b"m" * 32,
            )
            self.assertEqual(tuple(store.connection.execute(
                "SELECT observed_at,completeness FROM token_snapshots"
            ).fetchone()), (None, "complete"))

            metrics = aggregate_stored_task_tree(
                store.connection, project_id="project-timestampless",
                root_id=Pseudonymizer(b"m" * 32).digest("identity", "root"),
            )

            self.assertIsNone(metrics.recorded.input.value)
            self.assertEqual(metrics.recorded.input.known_lower_bound, 100)
            self.assertEqual(metrics.recorded.working.known_lower_bound, 90)
            self.assertEqual(metrics.recorded.provenance, "estimated")
            self.assertIn("timestamp_missing_token:1", metrics.recorded.caveats)
            self.assertNotIn("missing_final_token:1", metrics.recorded.caveats)

    def test_timestampless_snapshot_after_completion_is_excluded_by_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "project-source-cutoff"\n', encoding="utf-8",
            )
            source = base / "root.jsonl"
            write_rollout(source, [
                {"timestamp": stamp(0), "type": "session_meta", "payload": {"id": "root", "cwd": str(project)}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10,
                    "reasoning_output_tokens": 5,
                }}}},
                {"timestamp": stamp(10), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 900, "cached_input_tokens": 100, "output_tokens": 90,
                    "reasoning_output_tokens": 50,
                }}}},
            ])
            store = HydraStore(base / "hydra.sqlite3")
            self.addCleanup(store.close)
            ingest_rollouts(
                store, (source,), project, "project-source-cutoff", hash_key=b"s" * 32,
            )

            metrics = aggregate_stored_task_tree(
                store.connection, project_id="project-source-cutoff",
                root_id=Pseudonymizer(b"s" * 32).digest("identity", "root"),
            )

            self.assertIsNone(metrics.recorded.input.value)
            self.assertEqual(metrics.recorded.input.known_lower_bound, 100)
            self.assertEqual(metrics.recorded.working.known_lower_bound, 90)
            self.assertIn("timestamp_missing_token:1", metrics.recorded.caveats)

    def test_timestampless_snapshot_from_another_source_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            self.addCleanup(store.close)
            connection = store.connection
            connection.execute(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,conversation_key,started_at)
                   VALUES ('root','project-cross-source','worktree',1,'',?)""",
                (stamp(0),),
            )
            connection.executemany(
                """INSERT INTO rollout_logical_sources(
                       logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
                   VALUES (?, 'project-cross-source', 'root', NULL, 'clean')""",
                (("logical-a",), ("logical-b",)),
            )
            connection.executemany(
                """INSERT INTO rollout_sources(
                       source_digest,source_type,logical_source_key,relation,line_count,
                       byte_count,chain_digest,materialized)
                   VALUES (?, 'explicit', ?, 'canonical', 3, 3, 'chain', 1)""",
                (("source-a", "logical-a"), ("source-b", "logical-b")),
            )
            connection.executemany(
                """INSERT INTO token_snapshots(
                       source_digest,line_number,session_key,project_id,epoch,input_tokens,
                       cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                       completeness,observed_at)
                   VALUES (?,?, 'root','project-cross-source',0,?,?,?,?,0,'complete',?)""",
                (
                    ("source-a", 1, 50, 10, 5, 2, stamp(5)),
                    ("source-b", 2, 100, 20, 10, 5, None),
                ),
            )
            connection.execute(
                """INSERT INTO rollout_events(
                       event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
                       timestamp_quality,fingerprint)
                   VALUES ('complete','logical-a',3,'event_msg',?,'valid','complete')""",
                (stamp(10),),
            )
            connection.execute(
                """INSERT INTO turn_lifecycle_events(
                       event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                       emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
                   VALUES ('complete','root','turn','completed',?,10,NULL,
                           'source-a','logical-a',3)""",
                (stamp(10),),
            )
            connection.commit()

            reconcile_turn_attempts(connection)
            metrics = aggregate_stored_task_tree(
                connection, project_id="project-cross-source", root_id="root",
            )

            self.assertIsNone(metrics.recorded.input.value)
            self.assertEqual(metrics.recorded.input.known_lower_bound, 50)
            self.assertEqual(metrics.recorded.working.known_lower_bound, 45)
            self.assertIn("ambiguous_timestamp_token:1", metrics.recorded.caveats)

    def test_activity_history_uses_last_event_before_cutoff_not_global_last_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            self.addCleanup(store.close)
            connection = store.connection
            connection.executemany(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,conversation_key,
                       started_at,last_activity_at)
                   VALUES (?, 'project-activity', 'worktree', 1, '', ?, ?)""",
                (("root", stamp(0), stamp(10)), ("child", stamp(2), stamp(20))),
            )
            connection.execute(
                """INSERT INTO session_edges(
                       child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
                   VALUES ('child','root',NULL,'inferred',0.5)"""
            )
            connection.executemany(
                """INSERT INTO rollout_logical_sources(
                       logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
                   VALUES (?, 'project-activity', ?, NULL, 'clean')""",
                (("logical-root", "root"), ("logical-child", "child")),
            )
            connection.executemany(
                """INSERT INTO rollout_sources(
                       source_digest,source_type,logical_source_key,relation,line_count,
                       byte_count,chain_digest,materialized)
                   VALUES (?, 'explicit', ?, 'canonical', 3, 3, 'chain', 1)""",
                (("source-root", "logical-root"), ("source-child", "logical-child")),
            )
            connection.executemany(
                """INSERT INTO rollout_events(
                       event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
                       timestamp_quality,fingerprint)
                   VALUES (?, ?, ?, 'event_msg', ?, 'valid', ?)""",
                (
                    ("child-before", "logical-child", 1, stamp(5), "before"),
                    ("root-complete", "logical-root", 2, stamp(10), "complete"),
                    ("child-after", "logical-child", 2, stamp(20), "after"),
                ),
            )
            connection.execute(
                """INSERT INTO turn_lifecycle_events(
                       event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                       emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
                   VALUES ('root-complete','root','turn','completed',?,10,NULL,
                           'source-root','logical-root',2)""",
                (stamp(10),),
            )
            connection.commit()

            reconcile_turn_attempts(connection)
            metrics = aggregate_stored_task_tree(
                connection, project_id="project-activity", root_id="root",
            )

            self.assertEqual(metrics.agent_time_ms.value, 13_000)


if __name__ == "__main__":
    unittest.main()
