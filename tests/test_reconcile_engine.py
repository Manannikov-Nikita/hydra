from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hydra_codex.contracts import AnnotationContext, ModelAnnotationInput, materialize_annotation
from hydra_codex.exact_time import require_exact_timestamp
from hydra_codex.reconcile_engine import (
    ReconciliationStale,
    get_reconciled_task,
    get_reconciled_report,
    list_reconciled_tasks,
    list_reconciled_reports,
    reconcile_project,
    render_materialized_report_collection,
)
from hydra_codex.report_renderers import render_json
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.public_refs import project_public_references
from hydra_codex.storage import HydraStore


PROJECT = "hprj_reconcile"
HISTORICAL = Path(__file__).parent / "fixtures" / "historical"


def stamp(second: int) -> str:
    return datetime(2026, 7, 21, 0, 0, second, tzinfo=timezone.utc).isoformat()


def moment(second: int) -> datetime:
    return datetime(2026, 7, 21, 0, 0, second, tzinfo=timezone.utc)


class ReconcileEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = HydraStore(Path(self.temporary.name) / "hydra.sqlite3")
        self.connection = self.store.connection

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def session(
        self, key: str, started: int, *, parent: str | None = None,
        confidence: str = "confirmed", score: float = 1.0,
    ) -> None:
        logical = f"logical-{key}"
        source = f"source-{key}"
        self.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,conversation_key,started_at,last_activity_at)
               VALUES (?,?,?,1,'',?,?)""",
            (key, PROJECT, "shared-worktree", stamp(started), stamp(started)),
        )
        self.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
               VALUES (?,?,?,?,'clean')""",
            (logical, PROJECT, key, source),
        )
        self.connection.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key,relation,line_count,
                   byte_count,chain_digest,materialized)
               VALUES (?,'explicit',?,'initial',1,1,'chain',1)""",
            (source, logical),
        )
        if parent is not None:
            self.connection.execute(
                """INSERT INTO session_edges(
                       child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
                   VALUES (?,?,?,?,?)""",
                (key, parent, None, confidence, score),
            )

    def token(
        self, session: str, line: int, second: int,
        input_tokens: int, cached: int | None, output: int, reasoning: int | None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                   completeness,observed_at)
               VALUES (?,?,?,?,0,?,?,?,?,0,'complete',?)""",
            (f"source-{session}", line, session, PROJECT, input_tokens, cached, output, reasoning, stamp(second)),
        )

    def complete(self, session: str, second: int, ordinal: int = 100) -> None:
        started_at = self.connection.execute(
            "SELECT started_at FROM rollout_sessions WHERE session_key=?", (session,),
        ).fetchone()[0]
        start_event = f"start-{session}-{ordinal}"
        self.connection.execute(
            """INSERT INTO rollout_events(
                   event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
                   timestamp_quality,fingerprint)
               VALUES (?, ?, 0, 'event_msg', ?, 'valid', 'shape-start')""",
            (start_event, f"logical-{session}", started_at),
        )
        self.connection.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                   emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
               VALUES (?,?,'turn','started',?,?,NULL,?,?,0)""",
            (
                start_event, session, started_at,
                datetime.fromisoformat(started_at).timestamp(),
                f"source-{session}", f"logical-{session}",
            ),
        )
        event = f"complete-{session}-{ordinal}"
        self.connection.execute(
            """INSERT INTO rollout_events(
                   event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
                   timestamp_quality,fingerprint)
               VALUES (?, ?, ?, 'event_msg', ?, 'valid', 'shape')""",
            (event, f"logical-{session}", ordinal, stamp(second)),
        )
        self.connection.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                   emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
               VALUES (?,?,'turn','completed',?,?,NULL,?,?,?)""",
            (event, session, stamp(second), float(second), f"source-{session}", f"logical-{session}", ordinal),
        )

    def semantic_interval(
        self, session: str, *, start: int, end: int | None,
        phase: str, cause: str, family: str, sequence: int,
    ) -> None:
        legacy_session = session
        legacy_turn = f"turn-{session}"
        self.connection.execute(
            """INSERT INTO sessions(session_id,project_id,worktree_path,started_at,provenance)
               VALUES (?,?,?,?, 'exact') ON CONFLICT DO NOTHING""",
            (legacy_session, PROJECT, "safe", stamp(0)),
        )
        self.connection.execute(
            """INSERT INTO turns(turn_id,session_id,ordinal,observed_at,provenance)
               VALUES (?,?,0,?,'exact') ON CONFLICT DO NOTHING""",
            (legacy_turn, legacy_session, stamp(0)),
        )
        binding = f"binding-{session}"
        self.connection.execute(
            """INSERT INTO trusted_turn_bindings(
                   turn_key,project_id,session_key,created_at,state,last_sequence)
               VALUES (?,?,?,?, 'open', ?) ON CONFLICT DO NOTHING""",
            (binding, PROJECT, session, stamp(0), sequence),
        )
        annotation = f"annotation-{session}-{sequence}"
        self.connection.execute(
            """INSERT INTO annotations(
                   annotation_id,project_id,session_id,turn_id,sequence,observed_at,kind,
                   phase,cause,scope_change,task_family,confidence,outcome,provenance,
                   note_redacted,note_hash,note_length)
               VALUES (?,?,?,?,?,?,'phase',?,?,'none',?,1.0,NULL,'model_reported','safe','hash',4)""",
            (annotation, PROJECT, legacy_session, legacy_turn, sequence, stamp(start), phase, cause, family),
        )
        self.connection.execute(
            """INSERT INTO semantic_intervals(
                   interval_key,project_id,session_key,turn_key,start_annotation_id,
                   start_sequence,started_at,ended_at,phase,cause,provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,'model_reported')""",
            (f"interval-{session}-{sequence}", PROJECT, session, binding, annotation,
             sequence, stamp(start), None if end is None else stamp(end), phase, cause),
        )

    def finish_annotation(self, session: str, second: int, family: str, sequence: int) -> None:
        self.connection.execute(
            """INSERT INTO annotations(
                   annotation_id,project_id,session_id,turn_id,sequence,observed_at,kind,
                   phase,cause,scope_change,task_family,confidence,outcome,provenance,
                   note_redacted,note_hash,note_length)
               VALUES (?,?,?,?,?,?,'finish','test_full','final_verification','none',?,1.0,
                       'success','model_reported','safe','finish-hash',4)""",
            (f"finish-{session}", PROJECT, session, f"turn-{session}", sequence, stamp(second), family),
        )

    def add_test_result(
        self, session: str, line: int, second: int, failure: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO test_evidence_candidates(
                   candidate_key,candidate_kind,evidence_key,source_digest,line_number,
                   session_key,observed_at,tool_call_key,command_hash,runner,scope,
                   exit_status,outcome,failure_cause,provenance,completeness)
               VALUES (?,'evidence',?,?,?,?,?,?,?,?,?,1,'failed',?,'derived','complete')""",
            (
                f"candidate-{session}-{line}", f"test-{session}-{line}",
                f"source-{session}", line, session, stamp(second), f"call-{line}",
                "command", "pytest", "targeted", failure,
            ),
        )

    def test_reconcile_is_idempotent_orders_complete_and_incomplete_roots_and_respects_cutoffs(self) -> None:
        self.session("complete-root", 0)
        self.session("inferred-child", 1, parent="complete-root", confidence="inferred", score=0.6)
        self.session("ambiguous-child", 2, parent="complete-root", confidence="ambiguous", score=0.4)
        self.session("incomplete-root", 20)
        self.token("complete-root", 1, 8, 100, 20, 10, 5)
        self.token("complete-root", 2, 12, 900, 0, 90, 50)
        self.token("inferred-child", 1, 9, 40, 10, 5, 2)
        self.token("ambiguous-child", 1, 7, 30, 5, 3, 1)
        self.token("incomplete-root", 1, 25, 70, 20, 7, 3)
        self.complete("complete-root", 10)
        self.connection.commit()

        first = reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        second = reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        tasks = list_reconciled_tasks(self.store, project_id=PROJECT)

        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM reconciliation_runs WHERE project_id=?", (PROJECT,),
        ).fetchone()[0], 1)
        self.assertEqual(first.task_count, 3)
        self.assertEqual(tuple(item.status for item in tasks), ("incomplete", "complete", "incomplete"))
        self.assertEqual(tasks[0].last_activity_at, moment(25))
        complete = next(item for item in tasks if item.status == "complete")
        self.assertEqual(complete.last_activity_at, moment(10))
        self.assertEqual(complete.metrics.session_ids, ("complete-root", "inferred-child"))
        self.assertEqual(complete.metrics.recorded.working_tokens, 125)
        self.assertEqual(
            get_reconciled_task(self.store, project_id=PROJECT, public_ref=complete.public_ref),
            complete,
        )
        self.connection.execute(
            """INSERT INTO tool_spans(session_key,call_key,category,terminal_state,started_at)
               VALUES ('complete-root','later-tool','tool','success',?)""",
            (stamp(9),),
        )
        self.connection.commit()
        changed = reconcile_project(self.store, project_id=PROJECT, installation_key=b"r" * 32)
        self.assertNotEqual(changed.run_id, first.run_id)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM reconciliation_runs WHERE project_id=?", (PROJECT,),
        ).fetchone()[0], 2)

    def test_reconcile_uses_the_latest_trusted_task_label_at_the_cutoff(self) -> None:
        self.session("label-root", 0)
        self.token("label-root", 1, 2, 10, 1, 1, 0)
        self.semantic_interval(
            "label-root", start=1, end=None, phase="implement", cause="plan",
            family="telemetry", sequence=1,
        )
        self.complete("label-root", 5)
        label = materialize_annotation(
            ModelAnnotationInput.from_mapping({
                "kind": "phase", "phase": "implement", "cause": "plan", "scope_change": "none",
                "task_family": "telemetry", "confidence": 1.0, "note": "", "task_label": "Initial label",
            }),
            AnnotationContext(
                annotation_id="materialized-label", project_id=PROJECT, session_id="label-root",
                turn_id="turn-label-root", sequence=2, observed_at=stamp(2),
            ),
        )
        self.connection.commit()
        self.assertTrue(self.store.write_annotation(label).inserted)
        self.connection.execute(
            """INSERT INTO annotations(annotation_id,project_id,session_id,turn_id,sequence,observed_at,
                  kind,phase,cause,scope_change,task_family,confidence,outcome,provenance,note_redacted,
                  note_hash,note_length,task_label)
                 VALUES ('later-label',?,'label-root','turn-label-root',9,?,'phase','review','plan',
                         'none','telemetry',1,NULL,'model_reported','safe','later',4,'Too late')""",
            (PROJECT, stamp(6)),
        )
        self.connection.commit()
        reconcile_project(self.store, PROJECT, b"l" * 32)
        report = list_reconciled_reports(self.store, PROJECT)[0]
        self.assertEqual(report.display_name, "Initial label")
        self.assertNotIn("Too late", render_json(report))

    def test_reconcile_orders_equivalent_fractional_label_timestamps_by_sequence(self) -> None:
        self.session("fractional-label-root", 0)
        self.token("fractional-label-root", 1, 2, 10, 1, 1, 0)
        self.semantic_interval(
            "fractional-label-root",
            start=1,
            end=None,
            phase="implement",
            cause="plan",
            family="telemetry",
            sequence=1,
        )
        self.complete("fractional-label-root", 5)
        for annotation_id, sequence, observed_at, task_label in (
            ("fractional-short", 2, "2026-07-21T00:00:04.1Z", "Older label"),
            ("fractional-long", 3, "2026-07-21T00:00:04.100000Z", "Latest label"),
        ):
            self.connection.execute(
                """INSERT INTO annotations(
                       annotation_id,project_id,session_id,turn_id,sequence,observed_at,
                       kind,phase,cause,scope_change,task_family,confidence,outcome,
                       provenance,note_redacted,note_hash,note_length,task_label)
                   VALUES (?,?,'fractional-label-root','turn-fractional-label-root',?,?,
                           'phase','implement','plan','none','telemetry',1,NULL,
                           'model_reported','safe','label',4,?)""",
                (annotation_id, PROJECT, sequence, observed_at, task_label),
            )
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"f" * 32)

        self.assertEqual(
            list_reconciled_reports(self.store, PROJECT)[0].display_name,
            "Latest label",
        )

    def test_materialized_snapshot_change_bumps_revision_atomically_and_idempotently(self) -> None:
        self.session("revision-root", 0)
        self.token("revision-root", 1, 2, 10, 1, 1, 0)
        self.complete("revision-root", 5)
        self.connection.commit()
        initial_revision = self.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1"
        ).fetchone()[0]

        reconcile_project(self.store, PROJECT, b"v" * 32)

        first_revision = self.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1"
        ).fetchone()[0]
        snapshot_revision = self.connection.execute(
            """SELECT data_revision FROM materialized_report_snapshots
                 WHERE project_id=?""",
            (PROJECT,),
        ).fetchone()[0]
        self.assertGreater(first_revision, initial_revision)
        self.assertEqual(snapshot_revision, first_revision)

        reconcile_project(self.store, PROJECT, b"v" * 32)

        self.assertEqual(
            self.connection.execute(
                "SELECT revision FROM sync_data_revision WHERE singleton=1"
            ).fetchone()[0],
            first_revision,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT data_revision FROM materialized_report_snapshots
                     WHERE project_id=?""",
                (PROJECT,),
            ).fetchone()[0],
            first_revision,
        )

    def test_materialized_snapshot_publication_persists_exact_project_stats(self) -> None:
        self.session("stats-older", 0)
        self.token("stats-older", 1, 2, 10, 1, 1, 0)
        self.complete("stats-older", 5)
        self.session("stats-newer", 0)
        self.token("stats-newer", 1, 2, 10, 1, 1, 0)
        self.complete("stats-newer", 10)
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"m" * 32)

        row = self.connection.execute(
            """SELECT report_count,first_reconciled_at,last_reconciled_at,
                      first_activity_at,first_activity_epoch_ns,
                      last_activity_at,last_activity_epoch_ns,data_revision
                 FROM materialized_project_stats WHERE project_id=?""",
            (PROJECT,),
        ).fetchone()
        revision = self.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1",
        ).fetchone()[0]
        self.assertEqual(tuple(row), (
            2,
            require_exact_timestamp(stamp(10)).canonical,
            require_exact_timestamp(stamp(10)).canonical,
            require_exact_timestamp(stamp(5)).canonical,
            require_exact_timestamp(stamp(5)).epoch_nanoseconds,
            require_exact_timestamp(stamp(10)).canonical,
            require_exact_timestamp(stamp(10)).epoch_nanoseconds,
            revision,
        ))

    def test_materialized_report_reads_snapshot_without_reassembling_or_writing(self) -> None:
        self.session("snapshot-root", 0)
        self.token("snapshot-root", 1, 2, 10, 1, 1, 0)
        self.complete("snapshot-root", 5)
        self.connection.commit()
        reconcile_project(self.store, PROJECT, b"s" * 32)
        serialized = json.loads(str(self.connection.execute(
            """SELECT report_json FROM materialized_report_snapshots
                 WHERE project_id=?""",
            (PROJECT,),
        ).fetchone()[0]))
        serialized.pop("sync_freshness")
        self.connection.execute(
            """UPDATE materialized_report_snapshots SET report_json=?
                 WHERE project_id=?""",
            (
                json.dumps(
                    serialized, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                PROJECT,
            ),
        )
        self.connection.commit()
        statements: list[str] = []
        self.connection.set_trace_callback(statements.append)
        try:
            with mock.patch(
                "hydra_codex.reconcile_engine._assemble_project",
                side_effect=AssertionError("report must not reassemble"),
            ):
                rendered = render_materialized_report_collection(
                    self.store, PROJECT, 1, "json",
                    {
                        "schema_version": "hydra.sync-freshness/v1",
                        "state": "current",
                        "data_revision": 7,
                    },
                )
        finally:
            self.connection.set_trace_callback(None)
        payload = json.loads(rendered)
        self.assertEqual(payload["reports"][0]["schema_version"], "hydra.report/v4")
        self.assertEqual(
            payload["reports"][0]["sync_freshness"],
            payload["sync_freshness"],
        )
        self.assertFalse(any(
            statement.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE"))
            for statement in statements
        ), statements)

    def test_materialized_report_limit_orders_by_report_activity_not_shared_reconcile_time(self) -> None:
        key = b"r" * 32
        roots = ("activity-a", "activity-b")
        references = project_public_references(
            roots, key,
        )
        older_root, newer_root = (
            root
            for root in sorted(roots, key=lambda item: references[item])
        )
        self.session(older_root, 0)
        self.token(older_root, 1, 1, 10, 1, 1, 0)
        self.complete(older_root, 5)
        self.session(newer_root, 0)
        self.token(newer_root, 1, 1, 10, 1, 1, 0)
        self.complete(newer_root, 10)
        self.connection.commit()
        reconcile_project(self.store, PROJECT, key)

        rendered = render_materialized_report_collection(
            self.store, PROJECT, 1, "json",
            {
                "schema_version": "hydra.sync-freshness/v1",
                "state": "current",
                "data_revision": 0,
            },
        )

        self.assertEqual(
            json.loads(rendered)["reports"][0]["task_ref"],
            references[newer_root],
        )

    def test_reconcile_deduplicates_fork_replay_then_attributes_exact_deltas_to_intervals(self) -> None:
        self.session("root", 0)
        self.session("child", 2, parent="root")
        self.token("root", 1, 2, 100, 20, 10, 5)
        self.token("root", 2, 4, 130, 30, 20, 8)
        self.token("child", 1, 2, 50, 20, 5, 2)
        self.token("child", 2, 6, 80, 25, 10, 4)
        self.connection.execute(
            "UPDATE token_snapshots SET epoch=0"
        )
        self.connection.execute(
            """INSERT INTO fork_baselines(
                   child_key,source_digest,line_number,input_tokens,cached_input_tokens,
                   output_tokens,reasoning_tokens,cache_write_tokens,provenance,observed_at)
               VALUES ('child','source-child',1,50,20,5,2,0,'exact',?)""",
            (stamp(2),),
        )
        self.complete("root", 10)
        self.semantic_interval("root", start=1, end=3, phase="understand", cause="prompt", family="unclassified", sequence=0)
        self.semantic_interval("root", start=3, end=9, phase="implement", cause="plan", family="unclassified", sequence=1)
        self.semantic_interval("child", start=3, end=9, phase="test_targeted", cause="infra_failure", family="unclassified", sequence=0)
        self.finish_annotation("root", 9, "quiz", 2)
        self.add_test_result("root", 50, 4, "product_failure")
        self.add_test_result("child", 51, 6, "infra_failure")
        self.add_test_result("root", 52, 11, "product_failure")
        binding = self.connection.execute(
            "SELECT * FROM trusted_turn_bindings WHERE session_key='root' LIMIT 1"
        ).fetchone()
        self.connection.execute(
            """INSERT INTO semantic_fact_staging(
                   fact_key,project_id,session_key,turn_key,sequence,fact_kind,observed_at,provenance)
               VALUES ('missing',?,'root',?,NULL,'self_report_missing',?,'derived'),
                      ('conflict',?,'root',?,1,'semantic_conflict',?,'derived')""",
            (PROJECT, binding["turn_key"], stamp(9), PROJECT, binding["turn_key"], stamp(8)),
        )
        self.connection.execute(
            "INSERT INTO rollout_diagnostics VALUES ('source-root',99,'unknown_event_type','shape')"
        )
        self.connection.execute(
            "INSERT INTO rollout_diagnostics VALUES ('source-root',98,'unknown_event_type','shape-2')"
        )
        self.connection.commit()

        reconcile_project(self.store, project_id=PROJECT, installation_key=b"s" * 32)
        task = list_reconciled_tasks(self.store, project_id=PROJECT)[0]

        self.assertEqual(task.metrics.unique.working_tokens, 150)
        self.assertEqual({
            phase: fact.value for phase, fact in task.semantic.phase_working.items()
        }, {
            "implement": 30,
            "test_targeted": 30,
            "understand": 90,
        })
        self.assertEqual(task.semantic.unclassified_working.value, 0)
        self.assertEqual(task.semantic.coverage.value, 1.0)
        self.assertEqual(task.semantic.task_family, "quiz")
        self.assertEqual(task.semantic.self_report_missing, 1)
        self.assertEqual(task.semantic.semantic_conflicts, 2)
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT failure_cause FROM rollout_test_runs ORDER BY line_number"
            )],
            ["product_failure", "infra_failure", "product_failure"],
        )
        self.assertGreaterEqual(task.semantic.schema_diagnostics, 1)
        self.assertEqual(self.connection.execute(
            """SELECT occurrence_count FROM reconciled_task_diagnostics
                WHERE project_id=? AND diagnostic_code='schema:unknown_event_type'""",
            (PROJECT,),
        ).fetchone()[0], 2)
        self.assertEqual(self.connection.execute(
            "SELECT SUM(working_tokens) FROM reconciled_token_deltas WHERE project_id=?",
            (PROJECT,),
        ).fetchone()[0], 150)
        report = get_reconciled_report(
            self.store, project_id=PROJECT, public_ref=task.public_ref,
        )
        self.assertEqual(report.semantic_coverage.value, 1.0)
        self.assertEqual(report.schema_version, "hydra.report/v4")
        self.assertEqual(report.semantic_breakdown.phases["understand"].working.value, 90)
        self.assertEqual(report.semantic_breakdown.phases["understand"].full_context.value, 110)
        self.assertEqual(report.semantic_breakdown.phases["understand"].reasoning.value, 5)
        self.assertEqual(report.semantic_breakdown.phases["implement"].working.value, 30)
        self.assertEqual(report.semantic_breakdown.phases["test_targeted"].working.value, 30)
        self.assertEqual(report.semantic_breakdown.unclassified.working.value, 0)
        self.assertEqual(report.semantic_breakdown.marker_count.value, 4)
        self.assertEqual(report.semantic_breakdown.self_report_missing.value, 1)
        self.assertEqual(report.semantic_conflicts.value, 2)
        self.assertEqual(report.schema_diagnostics.value, task.semantic.schema_diagnostics)
        self.assertEqual(report.pilot_health.self_report_missing.value, 1)
        payload = json.loads(render_json(report))
        self.assertEqual(
            payload["semantic"]["breakdown"]["phases"]["understand"]["working"]["value"],
            90,
        )
        self.assertEqual(
            report.public_facts()["semantic.unclassified.reasoning"].provenance,
            "derived",
        )
        self.assertNotIn("root", render_json(report))

    def test_unmarked_intervals_remain_unclassified_and_parallel_roots_in_one_cwd_do_not_merge(self) -> None:
        for index in range(2):
            key = f"parallel-{index}"
            self.session(key, index)
            self.token(key, 1, 5 + index, 10 + index, 2, 3, 1)
        self.semantic_interval(
            "parallel-0", start=1, end=2, phase="understand", cause="prompt",
            family="parallel", sequence=0,
        )
        self.connection.commit()

        summary = reconcile_project(self.store, project_id=PROJECT, installation_key=b"u" * 32)
        tasks = list_reconciled_tasks(self.store, project_id=PROJECT, last=1)

        self.assertEqual(summary.task_count, 2)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].semantic.coverage.value, 0.0)
        self.assertEqual(
            tasks[0].semantic.unclassified_working.value,
            tasks[0].metrics.unique.working_tokens,
        )
        self.assertEqual(tasks[0].semantic.phase_working, {})
        reports = list_reconciled_reports(self.store, project_id=PROJECT, limit=1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].pilot_health.task_count.value, 0)
        self.assertEqual(reports[0].pilot_health.missing_marker_rate.value, 0.0)

    def test_reconciled_task_listing_reads_project_observations_once(self) -> None:
        for index in range(3):
            key = f"root-{index}"
            self.session(key, index)
            self.token(key, 1, 5 + index, 10 + index, 2, 3, 1)
        self.connection.commit()
        reconcile_project(self.store, PROJECT, b"o" * 32)

        statements: list[str] = []
        self.connection.set_trace_callback(statements.append)
        try:
            tasks = list_reconciled_tasks(self.store, PROJECT)
        finally:
            self.connection.set_trace_callback(None)

        project_session_reads = [
            statement for statement in statements
            if "SELECT s.session_key,e.parent_key,s.started_at" in statement
        ]
        self.assertEqual(len(tasks), 3)
        self.assertEqual(len(project_session_reads), 1)

    def test_query_fails_closed_when_sources_change_after_reconciliation(self) -> None:
        self.session("root", 0)
        self.token("root", 1, 5, 10, 2, 3, 1)
        self.connection.commit()
        reconcile_project(self.store, PROJECT, b"q" * 32)
        original = list_reconciled_tasks(self.store, PROJECT)[0]
        self.assertEqual(original.metrics.unique.working_tokens, 11)

        self.connection.execute(
            """UPDATE token_snapshots SET input_tokens=100,cached_input_tokens=20,
                      output_tokens=10 WHERE session_key='root'"""
        )
        self.connection.commit()

        with self.assertRaisesRegex(ReconciliationStale, "reconcile"):
            list_reconciled_tasks(self.store, PROJECT)
        reconcile_project(self.store, PROJECT, b"q" * 32)
        self.assertEqual(list_reconciled_tasks(self.store, PROJECT)[0].metrics.unique.working_tokens, 90)

    def test_query_requires_reconciliation_even_when_project_has_no_tasks(self) -> None:
        with self.assertRaisesRegex(ReconciliationStale, "reconcile"):
            list_reconciled_tasks(self.store, PROJECT)

        summary = reconcile_project(self.store, PROJECT, b"e" * 32)

        self.assertEqual(summary.task_count, 0)
        self.assertEqual(list_reconciled_tasks(self.store, PROJECT), ())

    def test_incomplete_cutoff_includes_trusted_annotation_and_finish_activity(self) -> None:
        self.session("root", 0)
        self.token("root", 1, 5, 10, 2, 3, 1)
        self.semantic_interval(
            "root", start=10, end=None, phase="implement", cause="plan",
            family="unclassified", sequence=0,
        )
        self.finish_annotation("root", 12, "quiz", 1)
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"a" * 32)
        task = list_reconciled_tasks(self.store, PROJECT)[0]

        self.assertEqual(task.last_activity_at, moment(12))
        self.assertEqual(task.metrics.agent_time_ms.value, 12_000)
        self.assertEqual(task.semantic.task_family, "quiz")
        self.assertEqual(task.semantic.marker_count, 2)
        self.assertEqual(task.semantic.coverage.value, 0.0)

    def test_timestampless_token_after_complete_source_ordinal_is_not_attributed(self) -> None:
        self.session("root", 0)
        self.token("root", 1, 5, 10, 2, 3, 1)
        self.complete("root", 10, ordinal=100)
        self.connection.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                   completeness,observed_at)
               VALUES ('source-root',101,'root',?,0,100,20,10,5,0,'complete',NULL)""",
            (PROJECT,),
        )
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"t" * 32)
        task = list_reconciled_tasks(self.store, PROJECT)[0]

        self.assertEqual(task.metrics.unique.working_tokens, 11)
        self.assertEqual(task.semantic.unclassified_working.value, 11)
        self.assertEqual(self.connection.execute(
            "SELECT SUM(working_tokens) FROM reconciled_token_deltas WHERE project_id=?",
            (PROJECT,),
        ).fetchone()[0], 11)
        self.assertIn("ambiguous_token_placement", task.semantic.diagnostics)

    def test_authoritative_abort_excludes_cross_source_timestampless_app_total(self) -> None:
        self.session("root", 0)
        self.token("root", 1, 5, 50, 10, 5, 2)
        self.connection.execute(
            """INSERT INTO codex_event_sources(
                   source_digest,project_id,schema_version,source_format,
                   line_count,byte_count)
               VALUES ('app-total-source',?,'codex.app-server/v2','app_server',1,1)""",
            (PROJECT,),
        )
        self.connection.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                   completeness,observed_at,source_family,counter_scope,event_key,
                   contributes_total,selection_provenance,selection_caveat)
               VALUES ('app-total-source',1,'root',?,0,100,20,10,5,0,
                       'complete',NULL,'app_server','thread_total','app-total',1,
                       'exact','app_total_timestamp_missing')""",
            (PROJECT,),
        )
        self.connection.execute(
            """INSERT INTO rollout_events(
                   event_key,logical_source_key,source_ordinal,envelope_kind,
                   observed_at,timestamp_quality,fingerprint)
               VALUES ('abort-start','logical-root',0,'event_msg',?,'valid','start')""",
            (stamp(0),),
        )
        self.connection.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                   emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
               VALUES ('abort-start','root','abort-turn','started',?,0,NULL,
                       'source-root','logical-root',0)""",
            (stamp(0),),
        )
        self.connection.execute(
            """INSERT INTO rollout_events(
                   event_key,logical_source_key,source_ordinal,envelope_kind,
                   observed_at,timestamp_quality,fingerprint)
               VALUES ('abort-terminal','logical-root',100,'event_msg',?,'valid','abort')""",
            (stamp(10),),
        )
        self.connection.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                   emitted_duration_ms,source_digest,logical_source_key,source_ordinal)
               VALUES ('abort-terminal','root','abort-turn','aborted',?,10,NULL,
                       'source-root','logical-root',100)""",
            (stamp(10),),
        )
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"a" * 32)
        task = list_reconciled_tasks(self.store, PROJECT)[0]

        self.assertEqual(task.status, "incomplete")
        self.assertIsNone(task.metrics.unique.working.value)
        self.assertEqual(task.metrics.unique.working.known_lower_bound, 45)
        self.assertEqual(task.semantic.unclassified_working.known_lower_bound, 45)
        self.assertEqual(
            self.connection.execute(
                """SELECT SUM(working_tokens) FROM reconciled_token_deltas
                    WHERE project_id=?""",
                (PROJECT,),
            ).fetchone()[0],
            45,
        )
        self.assertIn("ambiguous_token_placement", task.semantic.diagnostics)

    def test_token_before_session_start_is_not_persisted_as_a_reconciled_delta(self) -> None:
        self.session("root", 10)
        self.token("root", 1, 5, 100, 20, 10, 5)
        self.complete("root", 10)
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"y" * 32)
        task = list_reconciled_tasks(self.store, PROJECT)[0]

        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM reconciled_token_deltas WHERE project_id=?",
            (PROJECT,),
        ).fetchone()[0], 0)
        self.assertIn("token_before_session_start", task.semantic.diagnostics)
        self.assertEqual(task.semantic.unclassified_working.known_lower_bound, 0)

    def test_partial_vectors_preserve_known_unclassified_components_and_lower_bounds(self) -> None:
        self.session("root", 0)
        self.token("root", 1, 5, 100, None, 7, 5)
        self.complete("root", 10)
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"v" * 32)
        report = list_reconciled_reports(self.store, PROJECT)[0]

        self.assertIsNone(report.deduplicated_tokens.working.value)
        self.assertEqual(report.deduplicated_tokens.full_context.value, 107)
        self.assertEqual(report.deduplicated_tokens.reasoning.value, 5)
        self.assertIsNone(report.semantic_breakdown.unclassified.working.value)
        self.assertEqual(report.semantic_breakdown.unclassified.working.lower_bound, 7)
        self.assertEqual(report.semantic_breakdown.unclassified.full_context.value, 107)
        self.assertEqual(report.semantic_breakdown.unclassified.reasoning.value, 5)

    def test_partial_phase_vector_does_not_turn_unknown_reasoning_into_zero(self) -> None:
        self.session("root", 0)
        self.token("root", 1, 5, 100, 20, 7, None)
        self.complete("root", 10)
        self.semantic_interval(
            "root", start=1, end=9, phase="understand", cause="prompt",
            family="quiz", sequence=0,
        )
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"w" * 32)
        phase = list_reconciled_reports(self.store, PROJECT)[0].semantic_breakdown.phases[
            "understand"
        ]

        self.assertEqual(phase.working.value, 87)
        self.assertEqual(phase.full_context.value, 107)
        self.assertIsNone(phase.reasoning.value)
        self.assertEqual(phase.reasoning.lower_bound, 0)
        self.assertEqual(phase.reasoning.provenance, "estimated")

    def test_post_completion_legacy_facts_require_trusted_source_order(self) -> None:
        self.session("root", 0)
        self.token("root", 1, 5, 10, 2, 3, 1)
        self.complete("root", 10, ordinal=100)
        self.connection.execute(
            "INSERT INTO semantic_conflicts VALUES ('before','source-root',99,'test_failure','plan')"
        )
        self.connection.execute(
            "INSERT INTO semantic_conflicts VALUES ('after','source-root',101,'test_failure','plan')"
        )
        self.connection.execute(
            "INSERT INTO rollout_diagnostics VALUES ('source-root',99,'before_schema','shape')"
        )
        self.connection.execute(
            "INSERT INTO rollout_diagnostics VALUES ('source-root',101,'after_schema','shape')"
        )
        self.connection.commit()

        reconcile_project(self.store, PROJECT, b"x" * 32)
        task = list_reconciled_tasks(self.store, PROJECT)[0]

        self.assertEqual(task.semantic.semantic_conflicts, 1)
        self.assertEqual(task.semantic.schema_diagnostics, 1)
        self.assertIn("schema:before_schema", task.semantic.diagnostics)
        self.assertNotIn("schema:after_schema", task.semantic.diagnostics)
        self.assertIn("ambiguous_legacy_conflict_placement", task.semantic.diagnostics)
        self.assertIn("ambiguous_schema_diagnostic_placement", task.semantic.diagnostics)

    def test_historical_rollout_totals_reconcile_without_model_reported_answers(self) -> None:
        project = Path(self.temporary.name) / "historical-project"
        (project / ".hydra").mkdir(parents=True)
        project_id = "historical-reconcile"
        (project / ".hydra" / "project.toml").write_text(
            f'project_id = "{project_id}"\n', encoding="utf-8",
        )
        rollouts = Path(self.temporary.name) / "historical-rollouts"
        rollouts.mkdir()
        for source in sorted((HISTORICAL / "newer").glob("*.jsonl")):
            (rollouts / source.name).write_text(
                source.read_text(encoding="utf-8").replace("__PROJECT_ROOT__", str(project)),
                encoding="utf-8",
            )
        manifest = json.loads(
            (HISTORICAL / "newer-manifest.json").read_text(encoding="utf-8")
        )

        ingest_rollouts(
            self.store, (rollouts,), project, project_id, hash_key=b"h" * 32,
        )
        reconcile_project(self.store, project_id=project_id, installation_key=b"p" * 32)
        reports = list_reconciled_reports(self.store, project_id=project_id)
        report = next(item for item in reports if item.sessions.value == manifest["expected"]["sessions"])

        self.assertEqual(report.deduplicated_tokens.working.value, manifest["expected"]["working_tokens"])
        self.assertEqual(report.deduplicated_tokens.full_context.value, manifest["expected"]["full_context"])
        self.assertEqual(report.semantic_coverage.value, 0.0)
        self.assertEqual(
            report.semantic_breakdown.unclassified.working.value,
            manifest["expected"]["working_tokens"],
        )
        self.assertEqual(
            report.semantic_breakdown.unclassified.full_context.value,
            manifest["expected"]["full_context"],
        )
        self.assertEqual(report.semantic_breakdown.marker_count.value, 0)
        self.assertEqual(report.pilot_health.task_count.value, 0)
        self.assertEqual(report.pilot_health.missing_marker_rate.value, 0.0)
        self.assertNotIn(manifest["root"], render_json(report))


if __name__ == "__main__":
    unittest.main()
