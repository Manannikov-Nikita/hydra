from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex import report_operations
from hydra_codex.cli import main as cli_main
from hydra_codex.reconcile_engine import (
    ReconciliationStale,
    list_reconciled_reports,
    reconcile_project,
)
from hydra_codex.report_renderers import (
    render_html,
    render_json,
    render_markdown,
    render_report_collection,
)
from hydra_codex.reporting import NumericFact, project_public_references, report_from_task_tree
from hydra_codex.storage import HydraStore
from hydra_codex.task_tree import (
    ActivityObservation,
    LifecycleObservation,
    NormalizedSession,
    TokenObservation,
    TokenVector,
    aggregate_task_tree,
)


PROJECT = "hprj_semantic_report"
BASE = datetime(2026, 7, 21, tzinfo=timezone.utc)


def stamp(second: int) -> str:
    return (BASE + timedelta(seconds=second)).isoformat()


def report_fixture(name: str, tokens: int, retries: int, offset: int):
    metrics = aggregate_task_tree(
        root_id=name,
        sessions=(NormalizedSession(name, None, BASE),),
        tokens=(TokenObservation(name, BASE + timedelta(seconds=5), 1, TokenVector(tokens, 0, 0, 0)),),
        lifecycle=(LifecycleObservation(name, "task_complete", BASE + timedelta(seconds=10)),),
        activities=(ActivityObservation(name, BASE + timedelta(seconds=8)),),
    )
    reference = project_public_references((name,), b"t" * 32)[name]
    report = report_from_task_tree(metrics, public_ref=reference, task_family="quiz")
    return replace(
        report,
        last_activity_at=stamp(offset),
        trend_input=replace(
            report.trend_input,
            test_retries=NumericFact(retries, "count", "exact"),
        ),
    )


class StoredReportScenario(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "hydra.sqlite3"
        self.project = self.root / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT}"\n', encoding="utf-8",
        )
        self.store = HydraStore(self.database)
        self.db = self.store.connection

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def add_task(
        self,
        name: str,
        offset: int,
        tokens: int,
        *,
        instrumented: bool = True,
        finish: bool = True,
        family: str = "quiz",
        note: str = "safe summary",
        extra_markers: int = 0,
        deterministic_failure: str | None = None,
        retries: int = 0,
        semantic_phase: str = "implement",
        semantic_cause: str = "plan",
    ) -> None:
        source = f"source-{name}"
        logical = f"logical-{name}"
        completion_second = offset + 9 + extra_markers
        self.db.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,conversation_key,
                   started_at,last_activity_at)
               VALUES (?,?,?,1,'',?,?)""",
            (name, PROJECT, "shared", stamp(offset), stamp(completion_second)),
        )
        self.db.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,canonical_revision_digest,lineage_state)
               VALUES (?,?,?,?, 'clean')""",
            (logical, PROJECT, name, source),
        )
        self.db.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key,relation,line_count,
                   byte_count,chain_digest,materialized)
               VALUES (?,'explicit',?,'initial',1,1,'chain',1)""",
            (source, logical),
        )
        self.db.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,cache_write_tokens,
                   completeness,observed_at)
               VALUES (?,1,?,?,0,?,0,0,0,0,'complete',?)""",
            (source, name, PROJECT, tokens, stamp(offset + 5)),
        )
        start_event = f"start-{name}"
        self.db.execute(
            """INSERT INTO rollout_events(
                   event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
                   timestamp_quality,fingerprint)
               VALUES (?,?,99,'event_msg',?,'valid','shape')""",
            (start_event, logical, stamp(offset)),
        )
        self.db.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                   source_digest,logical_source_key,source_ordinal)
               VALUES (?,?,?,'started',?,0,?,?,99)""",
            (start_event, name, f"turn-{name}", stamp(offset), source, logical),
        )
        event = f"complete-{name}"
        self.db.execute(
            """INSERT INTO rollout_events(
                   event_key,logical_source_key,source_ordinal,envelope_kind,observed_at,
                   timestamp_quality,fingerprint)
               VALUES (?,?,100,'event_msg',?,'valid','shape')""",
            (event, logical, stamp(completion_second)),
        )
        self.db.execute(
            """INSERT INTO turn_lifecycle_events(
                   event_key,session_key,turn_key,event_kind,observed_at,timestamp_epoch,
                   source_digest,logical_source_key,source_ordinal)
               VALUES (?,?,?,'completed',?,0,?,?,100)""",
            (event, name, f"turn-{name}", stamp(completion_second), source, logical),
        )
        if instrumented:
            self._annotations(
                name, offset, family, finish=finish, note=note,
                extra_markers=extra_markers, semantic_phase=semantic_phase,
                semantic_cause=semantic_cause,
            )
        if deterministic_failure is not None:
            self._test_run(
                name, source, offset + 4, 0, failure=deterministic_failure,
                retry_kind="none", scope="targeted",
            )
        for index in range(retries):
            if deterministic_failure is None or index > 0:
                self._test_run(
                    name, source, offset + 6 + index, 100 + index * 2,
                    failure="product_failure", retry_kind="none", scope="full",
                )
            self._test_run(
                name, source, offset + 6 + index, 101 + index * 2,
                failure="none", retry_kind="flaky_retry", scope="full",
            )

    def _annotations(
        self, name: str, offset: int, family: str, *, finish: bool,
        note: str, extra_markers: int, semantic_phase: str,
        semantic_cause: str,
    ) -> None:
        turn = f"turn-{name}"
        self.db.execute(
            """INSERT INTO sessions(session_id,project_id,worktree_path,started_at,provenance)
               VALUES (?,?,'.',?,'exact')""",
            (name, PROJECT, stamp(offset)),
        )
        self.db.execute(
            """INSERT INTO turns(turn_id,session_id,ordinal,observed_at,provenance)
               VALUES (?,?,0,?,'exact')""",
            (turn, name, stamp(offset)),
        )
        last_sequence = 2 + extra_markers if finish else 1 + extra_markers
        self.db.execute(
            """INSERT INTO trusted_turn_bindings(
                   turn_key,project_id,session_key,created_at,state,last_sequence)
               VALUES (?,?,?,?,?,?)""",
            (turn, PROJECT, name, stamp(offset), "finished" if finish else "open", last_sequence),
        )
        self._annotation(
            name, turn, 0, offset, "phase", "understand", "prompt", "none",
            "unclassified", 1.0, None, "", "derived",
        )
        self._annotation(
            name, turn, 1, offset + 1, "phase", semantic_phase, semantic_cause, "expanded",
            family, 0.8, None, note, "model_reported",
        )
        for index in range(extra_markers):
            self._annotation(
                name, turn, index + 2, offset + 2 + index, "blocker", "implement",
                "review_finding", "none", family, 0.7, None,
                f"marker {index}", "model_reported",
            )
        finish_sequence = extra_markers + 2
        finish_second = offset + 7 + extra_markers
        if finish:
            self._annotation(
                name, turn, finish_sequence, finish_second, "finish", "test_full",
                "final_verification", "none", family, 1.0, "partial",
                "finished safely", "model_reported",
            )
        self.db.execute(
            """INSERT INTO semantic_intervals(
                   interval_key,project_id,session_key,turn_key,start_annotation_id,
                   start_sequence,started_at,ended_at,phase,cause,provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,'model_reported')""",
            (
                f"interval-{name}", PROJECT, name, turn, f"annotation-{name}-1", 1,
                stamp(offset + 1), stamp(finish_second), semantic_phase, semantic_cause,
            ),
        )

    def _annotation(
        self, session: str, turn: str, sequence: int, second: int, kind: str,
        phase: str, cause: str, scope: str, family: str, confidence: float,
        outcome: str | None, note: str, provenance: str,
    ) -> None:
        self.db.execute(
            """INSERT INTO annotations(
                   annotation_id,project_id,session_id,turn_id,sequence,observed_at,kind,
                   phase,cause,scope_change,task_family,confidence,outcome,provenance,
                   note_redacted,note_hash,note_length)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"annotation-{session}-{sequence}", PROJECT, session, turn, sequence,
                stamp(second), kind, phase, cause, scope, family, confidence, outcome,
                provenance, note, "private-hash", len(note),
            ),
        )

    def _test_run(
        self, session: str, source: str, second: int, index: int, *,
        failure: str, retry_kind: str, scope: str,
    ) -> None:
        exit_status = 0 if failure == "none" else 1
        outcome = "success" if exit_status == 0 else "failed"
        self.db.execute(
            """INSERT INTO test_evidence_candidates(
                   candidate_key,candidate_kind,evidence_key,source_digest,
                   line_number,session_key,observed_at,tool_call_key,command_hash,
                   runner,scope,exit_status,outcome,failure_cause,provenance,
                   completeness)
               VALUES (?,'evidence',?,?,?,?,?,?,'command','pytest',?,?,?,?,
                       'derived','complete')""",
            (
                f"candidate-{session}-{index}", f"test-{session}-{index}",
                source, 20 + index, session, stamp(second),
                f"call-{session}-{index}", scope, exit_status, outcome, failure,
            ),
        )

    def reconcile(self):
        self.db.commit()
        reconcile_project(self.store, PROJECT, b"s" * 32)
        return list_reconciled_reports(self.store, PROJECT)

    def cli_report(self) -> dict[str, object]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = cli_main(
            [
                "report", "--db", str(self.database), "--cwd", str(self.project),
                "--last", "20", "--format", "json",
            ],
            stdin=io.StringIO(), stdout=stdout, stderr=stderr,
            environ={"HOME": str(self.root)},
        )
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        return json.loads(stdout.getvalue())


class SemanticReportIntegrationTests(StoredReportScenario):
    def test_report_exposes_bounded_privacy_safe_model_semantics_and_deterministic_causes(self) -> None:
        private_note = "/Users/alice/private password=hunter2"
        self.add_task(
            "root", 0, 200, note=private_note,
            deterministic_failure="product_failure",
        )

        report = self.reconcile()[0]
        payload = json.loads(render_json(report))
        annotations = payload["semantic"]["annotations"]

        self.assertEqual(payload["schema_version"], "hydra.report/v4")
        self.assertEqual(annotations["kind_counts"]["phase"]["value"], 1)
        self.assertEqual(annotations["kind_counts"]["finish"]["value"], 1)
        self.assertEqual(annotations["cause_counts"]["plan"]["value"], 1)
        self.assertEqual(annotations["scope_change_counts"]["expanded"]["value"], 1)
        self.assertEqual(annotations["finish_outcome_counts"]["partial"]["value"], 1)
        self.assertEqual(annotations["deterministic_test_causes"]["test_failure"]["value"], 1)
        evidence = annotations["test_evidence"]
        self.assertEqual(evidence["total_count"]["value"], 1)
        self.assertEqual(evidence["rows"][0]["scope"], "targeted")
        self.assertEqual(evidence["rows"][0]["failure_cause"], "product_failure")
        self.assertEqual(evidence["rows"][0]["phase"], "implement")
        self.assertEqual(evidence["rows"][0]["cause"], "plan")
        self.assertEqual(evidence["rows"][0]["count"]["provenance"], "derived")
        self.assertEqual(annotations["timeline"][0]["note"], "[redacted]")
        self.assertEqual(annotations["timeline"][0]["provenance"], "model_reported")
        self.assertEqual(report.semantic_conflicts.value, 1)
        rendered = (
            render_report_collection((report,), "json"),
            render_markdown(report),
            render_html(report),
        )
        self.assertIn("Semantic marker timeline", rendered[1])
        self.assertIn("Semantic marker timeline", rendered[2])
        self.assertIn("Deterministic test evidence", rendered[1])
        self.assertIn("Deterministic test evidence", rendered[2])
        self.assertIn(r"\[redacted\]", rendered[1])
        self.assertIn("[redacted]", rendered[2])
        for private in (private_note, "alice", "hunter2", "private-hash"):
            for serialized in rendered:
                self.assertNotIn(private, serialized)
        for forbidden in (
            "annotation_id", "session_id", "turn_id", "observed_at", "note_hash",
            "command_hash", "tool_call_key", "evidence_key",
        ):
            self.assertNotIn(forbidden, rendered[0])

    def test_report_cross_tabs_test_scope_failure_retry_and_semantic_purpose(self) -> None:
        self.add_task(
            "root", 0, 200, deterministic_failure="product_failure", retries=1,
            semantic_phase="test_full", semantic_cause="final_verification",
        )
        self._test_run(
            "root", "source-root", 5, 99, failure="infra_failure",
            retry_kind="infra_recovery", scope="full",
        )

        report = self.reconcile()[0]
        evidence = json.loads(render_json(report))["semantic"]["annotations"]["test_evidence"]
        rows = {
            (
                row["scope"], row["failure_cause"], row["retry_kind"],
                row["phase"], row["cause"], row["count"]["value"],
            )
            for row in evidence["rows"]
        }

        self.assertEqual(evidence["total_count"]["value"], 3)
        self.assertEqual(rows, {
            ("targeted", "product_failure", "none", "test_full", "final_verification", 1),
            ("full", "infra_failure", "none", "test_full", "final_verification", 1),
            ("full", "none", "infra_recovery", "test_full", "final_verification", 1),
        })
        for artifact in (render_json(report), render_markdown(report), render_html(report)):
            self.assertNotIn("source-root", artifact)

    def test_malformed_interval_end_fails_closed_with_diagnostic(self) -> None:
        self.add_task("root", 0, 200)
        self.db.execute(
            "UPDATE semantic_intervals SET ended_at='not-a-time' WHERE session_key='root'"
        )
        self._test_run(
            "root", "source-root", 8, 99, failure="none",
            retry_kind="none", scope="full",
        )

        report = self.reconcile()[0]
        evidence = json.loads(render_json(report))["semantic"]["annotations"]["test_evidence"]

        self.assertEqual(evidence["rows"][0]["phase"], "unclassified")
        self.assertEqual(evidence["rows"][0]["cause"], "unclassified")
        self.assertEqual(report.schema_diagnostics.value, 1)

    def test_null_interval_end_remains_a_valid_open_interval(self) -> None:
        self.add_task("root", 0, 200)
        self.db.execute(
            "UPDATE semantic_intervals SET ended_at=NULL WHERE session_key='root'"
        )
        self._test_run(
            "root", "source-root", 8, 99, failure="none",
            retry_kind="none", scope="full",
        )

        report = self.reconcile()[0]
        evidence = json.loads(render_json(report))["semantic"]["annotations"]["test_evidence"]

        self.assertEqual(evidence["rows"][0]["phase"], "implement")
        self.assertEqual(evidence["rows"][0]["cause"], "plan")
        self.assertEqual(report.schema_diagnostics.value, 0)

    def test_timeline_is_bounded_to_twenty_recent_model_markers(self) -> None:
        self.add_task("root", 0, 100, extra_markers=24)

        annotations = json.loads(render_json(self.reconcile()[0]))["semantic"]["annotations"]

        self.assertEqual(annotations["total_count"]["value"], 26)
        self.assertEqual(len(annotations["timeline"]), 20)
        self.assertEqual(annotations["truncated_count"]["value"], 6)
        self.assertIn("timeline_truncated", annotations["caveats"])
        self.assertEqual(annotations["timeline"][-1]["kind"], "finish")

    def test_annotation_change_after_reconcile_invalidates_stored_report(self) -> None:
        self.add_task("root", 0, 100)
        self.reconcile()
        self.db.execute(
            "UPDATE annotations SET note_redacted='changed' WHERE provenance='model_reported'"
        )
        self.db.commit()

        with self.assertRaises(ReconciliationStale):
            list_reconciled_reports(self.store, PROJECT)

    def test_test_evidence_change_after_reconcile_invalidates_stored_report(self) -> None:
        self.add_task("root", 0, 100, retries=1)
        self.reconcile()
        self.db.execute(
            "UPDATE rollout_test_runs SET retry_kind='infra_recovery'"
        )
        self.db.commit()

        with self.assertRaises(ReconciliationStale):
            list_reconciled_reports(self.store, PROJECT)

    def test_pilot_health_counts_only_trusted_instrumented_tasks_and_missing_finish(self) -> None:
        self.add_task("historical", 0, 100, instrumented=False)
        self.add_task("missing", 20, 100, finish=False)
        self.add_task("finished", 40, 100, finish=True)
        self.add_task("unfinished", 60, 100, finish=False)
        self.db.execute(
            "DELETE FROM turn_lifecycle_events WHERE session_key='unfinished'"
        )

        pilot = self.reconcile()[0].pilot_health.as_dict()

        self.assertEqual(pilot["task_count"]["value"], 2)
        self.assertIn(
            "completed_instrumented_tasks_denominator",
            pilot["task_count"]["caveats"],
        )
        self.assertEqual(pilot["missing_marker_rate"]["value"], 0.5)
        self.assertIn(
            "completed_instrumented_tasks_denominator",
            pilot["missing_marker_rate"]["caveats"],
        )
        self.assertEqual(pilot["status"], "measuring")
        self.assertFalse(pilot["receipt_verified"])
        self.assertIn("pilot_receipt_required", pilot["caveats"])
        self.assertIn("incomplete_instrumented_tasks_excluded", pilot["caveats"])

    def test_only_incomplete_instrumented_tasks_start_measurement_without_counting_them(self) -> None:
        self.add_task("unfinished", 0, 100, finish=False)
        self.db.execute(
            "DELETE FROM turn_lifecycle_events WHERE session_key='unfinished'"
        )

        pilot = self.reconcile()[0].pilot_health.as_dict()

        self.assertEqual(pilot["task_count"]["value"], 0)
        self.assertEqual(pilot["missing_marker_rate"]["value"], 0.0)
        self.assertEqual(pilot["status"], "measuring")
        self.assertIn("incomplete_instrumented_tasks_excluded", pilot["caveats"])

    def test_project_event_schema_issue_invalidates_reconciliation_and_reaches_pilot(self) -> None:
        self.add_task("finished", 0, 100)
        self.reconcile()
        self.db.execute(
            """INSERT INTO codex_event_sources(
                   source_digest,project_id,schema_version,source_format,line_count,byte_count)
               VALUES ('event-source',?,'app_server/v2','app_server',1,1)""",
            (PROJECT,),
        )
        self.db.execute(
            """INSERT INTO codex_event_issues(
                   source_digest,source_ordinal,event_key,issue_code)
               VALUES ('event-source',1,'issue-event','schema_drift')"""
        )
        self.db.commit()

        with self.assertRaises(ReconciliationStale):
            list_reconciled_reports(self.store, PROJECT)

        pilot = self.reconcile()[0].pilot_health.as_dict()
        self.assertEqual(pilot["schema_diagnostics"]["value"], 1)
        self.assertIn(
            "project_event_schema_diagnostics",
            pilot["schema_diagnostics"]["caveats"],
        )

    def test_five_instrumented_tasks_await_receipt_and_cli_warns_from_complete_retries(self) -> None:
        for index in range(5):
            self.add_task(
                f"task-{index}", index * 20, 200 if index == 4 else 100,
                retries=3 if index == 4 else 1,
            )
            self.db.execute(
                "UPDATE annotations SET scope_change='none' WHERE session_id=?",
                (f"task-{index}",),
            )

        reports = self.reconcile()
        latest = reports[0]
        payload = json.loads(render_report_collection(reports, "json"))
        cli_payload = self.cli_report()

        self.assertEqual(latest.pilot_health.status, "awaiting_receipt")
        self.assertFalse(latest.pilot_health.receipt_verified)
        with self.assertRaisesRegex(ValueError, "requires a receipt"):
            replace(latest.pilot_health, status="verified")
        self.assertIn(
            r"Pilot status: awaiting\_receipt; receipt verified: no",
            render_markdown(latest),
        )
        self.assertIn(
            "Pilot status: awaiting_receipt; receipt verified: no",
            render_html(latest),
        )
        self.assertIn("trend", payload["reports"][0])
        self.assertEqual(latest.trend_input.test_retries.provenance, "derived")
        self.assertEqual(latest.trend_input.test_retries.lower_bound, 3)
        self.assertEqual(latest.trend_input.test_retries.caveats, ("reconciled_test_retries",))
        self.assertTrue(payload["reports"][0]["trend"]["result"]["warning"])
        self.assertTrue(cli_payload["reports"][0]["trend"]["result"]["warning"])
        self.assertEqual(
            cli_payload["reports"][0]["trend"]["result"]["corroborating_signal"],
            "test_retries",
        )
        self.assertIn(
            "deterministic_derived_test_reruns",
            cli_payload["reports"][0]["trend"]["result"]["caveats"],
        )
        self.assertIsNone(payload["reports"][0]["trend"]["input"]["read_amplification"]["value"])


class ReportTrendWiringTests(unittest.TestCase):
    def test_report_trend_evaluator_attaches_positive_result_only_to_fifth_comparable_task(self) -> None:
        evaluate_reports = getattr(report_operations, "evaluate_report_trends", None)
        self.assertTrue(callable(evaluate_reports), "report trend evaluator is missing")
        reports = tuple(
            report_fixture(f"task-{index}", 200 if index == 4 else 100, 3 if index == 4 else 1, index)
            for index in range(5)
        )

        evaluated = evaluate_reports(reports)
        latest = max(evaluated, key=lambda item: item.last_activity_at)

        self.assertTrue(latest.trend_result.warning)
        self.assertEqual(latest.trend_result.corroborating_signal, "test_retries")
        self.assertEqual(latest.trend_result.baseline_working_tokens.value, 100)
        self.assertEqual(latest.trend_result.token_growth.value, 100)
        self.assertEqual(sum(item.trend_result.warning for item in evaluated), 1)
        self.assertIn("Trend warning: yes", render_markdown(latest))
        self.assertIn("Trend warning: yes", render_html(latest))
        self.assertIn(r"test\_retries", render_markdown(latest))
        self.assertIn("test_retries", render_html(latest))

    def test_trend_window_excludes_future_and_equal_timestamp_tasks(self) -> None:
        current = report_fixture("current", 200, 3, 10)
        earlier = tuple(
            report_fixture(f"earlier-{index}", 100, 1, index)
            for index in range(4)
        )
        equal = replace(
            report_fixture("equal", 100, 1, 10),
            last_activity_at="2026-07-21T02:00:10+02:00",
        )
        future = report_fixture("future", 100, 1, 11)

        window = report_operations.build_trend_window(
            current, (*earlier, equal, future),
        )

        self.assertEqual(
            {item.task_ref for item in window.prior},
            {item.task_ref for item in earlier},
        )

    def test_equal_timestamp_tasks_never_form_a_trend_baseline(self) -> None:
        reports = tuple(
            report_fixture(
                f"task-{index}", 200 if index == 4 else 100,
                3 if index == 4 else 1, 10,
            )
            for index in range(5)
        )

        evaluated = report_operations.evaluate_report_trends(reports)

        self.assertFalse(any(item.trend_result.warning for item in evaluated))
        self.assertTrue(all(
            "insufficient_baseline" in item.trend_result.caveats
            for item in evaluated
        ))

    def test_unproven_or_uncertain_retry_counts_cannot_trigger_warning(self) -> None:
        variants = (
            lambda value: NumericFact(
                value, "count", "derived", ("reconciled_test_retries",),
            ),
            lambda value: NumericFact(
                value, "count", "derived", ("other_derived_count",), lower_bound=value,
            ),
            lambda value: NumericFact(
                value, "count", "estimated", ("timestamp_missing_test_retry:1",),
                lower_bound=0,
            ),
            lambda value: NumericFact(value, "count", "model_reported"),
        )
        for case, fact in enumerate(variants):
            with self.subTest(case=case):
                reports = tuple(
                    replace(
                        report_fixture(
                            f"case-{case}-{index}", 200 if index == 4 else 100,
                            3 if index == 4 else 1, index,
                        ),
                        trend_input=replace(
                            report_fixture(
                                f"case-{case}-{index}", 200 if index == 4 else 100,
                                3 if index == 4 else 1, index,
                            ).trend_input,
                            test_retries=fact(3 if index == 4 else 1),
                        ),
                    )
                    for index in range(5)
                )

                evaluated = report_operations.evaluate_report_trends(reports)

                self.assertFalse(any(item.trend_result.warning for item in evaluated))


if __name__ == "__main__":
    unittest.main()
