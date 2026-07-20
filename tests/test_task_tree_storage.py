from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from hydra_codex.storage import HydraStore
from hydra_codex.task_tree import TokenVector
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


def stamp(second: int) -> str:
    return datetime(2026, 7, 21, 0, 0, second, tzinfo=timezone.utc).isoformat()


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


if __name__ == "__main__":
    unittest.main()
