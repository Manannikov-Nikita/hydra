from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import unittest

from hydra_codex.audit_builder import StorageHealthSnapshot, build_audit
from hydra_codex.audit_model import AUDIT_SCHEMA
from hydra_codex.pilot import PilotStatus, default_thresholds
from hydra_codex.report_semantics import SemanticBreakdown
from hydra_codex.reporting import NumericFact, project_public_references, report_from_task_tree
from hydra_codex.task_tree import (
    ActivityObservation,
    LifecycleObservation,
    NormalizedSession,
    TokenObservation,
    TokenVector,
    aggregate_task_tree,
)


def at(second: int) -> datetime:
    return datetime(2026, 7, 21, 0, 0, second, tzinfo=timezone.utc)


def public_report(root: str, *, input_tokens: int = 100, second: int = 10):
    metrics = aggregate_task_tree(
        root_id=root,
        sessions=(NormalizedSession(root, None, at(0)),),
        tokens=(
            TokenObservation(
                root,
                at(5),
                1,
                TokenVector(
                    input_tokens,
                    min(20, input_tokens),
                    0 if input_tokens == 0 else 10,
                    0 if input_tokens == 0 else 5,
                ),
            ),
        ),
        lifecycle=(LifecycleObservation(root, "task_complete", at(second)),),
        activities=(ActivityObservation(root, at(second - 1)),),
    )
    task_ref = project_public_references((root,), b"a" * 32)[root]
    empty = SemanticBreakdown.empty()
    no_marker_breakdown = replace(
        empty,
        marker_count=NumericFact(0, "count", "derived"),
        self_report_missing=NumericFact(0, "count", "derived"),
    )
    return report_from_task_tree(
        metrics,
        public_ref=task_ref,
        task_family="telemetry-analysis",
        semantic_breakdown=no_marker_breakdown,
    )


def pilot_snapshot(reports) -> PilotStatus:
    thresholds = default_thresholds(5)
    tasks = [
        {
            "task_ref": report.task_ref,
            "completed_at": report.last_activity_at,
            "task_family": report.task_family,
            "scope_change": "none",
            "instrumented": False,
            "initial_missing": True,
            "finish_missing": True,
            "delivery_failures": 0,
            "semantic_conflicts": 0,
            "schema_diagnostics": 0,
            "coverage": 0.0,
            "accepted_transport_events": 0,
            "staging_latency_p95_ms": None,
            "trend_eligible": True,
        }
        for report in reports
    ]
    facts = {
        "eligible_tasks": len(tasks),
        "instrumented_tasks": 0,
        "enrollment": 0.0,
        "initial_missing": len(tasks),
        "finish_missing": len(tasks),
        "delivery_failures": 0,
        "semantic_conflicts": 0,
        "schema_diagnostics": 0,
        "aggregate_coverage": 0.0,
        "minimum_task_coverage": 0.0,
        "staging_latency_p95_ms": None,
        "token_overhead": None,
    }
    return PilotStatus({
        "schema_version": "hydra.pilot/v1",
        "pilot": {
            "pilot_id": "hpilot_v1_0123456789abcdef0123456789abcdef",
            "started_at": "2026-07-20T00:00:00Z",
            "closed_at": None,
            "target": 5,
            "task_family": "telemetry-analysis",
            "state": "open",
        },
        "reconciliation_version": 1,
        "storage_schema_version": 35,
        "thresholds": thresholds,
        "facts": facts,
        "tasks": tasks,
        "threshold_results": {name: False for name in thresholds},
        "transport_verified": False,
        "trend_ready": False,
        "receipt": None,
        "snapshot_digest": "a" * 64,
    })


def storage() -> StorageHealthSnapshot:
    return StorageHealthSnapshot(
        database_bytes=4096,
        wal_bytes=0,
        rollout_sources=2,
        rollout_events=12,
        codex_event_sources=0,
        codex_events=0,
        schema_version=35,
    )


class AuditBuilderTests(unittest.TestCase):
    def test_builds_canonical_collection_with_exact_once_evidence_appendix(self) -> None:
        reports = (
            public_report("root-a", input_tokens=100, second=10),
            public_report("root-b", input_tokens=0, second=20),
        )

        audit = build_audit(pilot_snapshot(reports), reports, storage())
        payload = audit.as_dict()

        self.assertEqual(payload["schema_version"], AUDIT_SCHEMA)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "pilot_snapshot",
                "cohort",
                "collection",
                "storage_health",
                "evidence_appendix",
            },
        )
        self.assertEqual(payload["pilot_snapshot"], pilot_snapshot(reports).as_dict())
        self.assertEqual(len(payload["collection"]["overview"]), 2)
        self.assertEqual(len(payload["collection"]["tasks"]), 2)
        self.assertEqual(
            payload["collection"]["tasks"][0]["agent_topology"]["status"],
            "aggregate_only",
        )
        evidence = payload["evidence_appendix"]
        ids = [item["evidence_id"] for item in evidence]
        facts = [item["fact"] for item in evidence]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(facts), len(set(facts)))
        self.assertEqual(facts, sorted(facts))
        encoded = json.dumps(payload, sort_keys=True)
        for evidence_id in ids:
            self.assertGreaterEqual(encoded.count(evidence_id), 2)
        complete_records = [item for item in evidence if set(item) == {
            "caveats", "evidence_id", "fact", "lower_bound", "provenance", "unit", "value",
        }]
        self.assertEqual(len(complete_records), len(evidence))
        self.assertEqual(
            sum(item["fact"] == "transport.pending_annotation_drain" for item in evidence),
            1,
        )
        drain = next(
            item for item in evidence
            if item["fact"] == "transport.pending_annotation_drain"
        )
        self.assertIsNone(drain["value"])
        self.assertIn("host_context_unavailable", drain["caveats"])
        with self.assertRaises(FrozenInstanceError):
            audit.schema_version = "changed"  # type: ignore[misc]

    def test_historical_reports_keep_zero_semantic_coverage_without_inferred_markers(self) -> None:
        report = public_report("historical-root")
        snapshot = pilot_snapshot((report,))

        payload = build_audit(snapshot, (report,), storage()).as_dict()
        evidence = {item["fact"]: item for item in payload["evidence_appendix"]}

        coverage = evidence[f"tasks.{report.task_ref}.semantic_coverage"]
        marker_count = evidence[
            f"tasks.{report.task_ref}.semantic.marker_count"
        ]
        self.assertEqual(coverage["value"], 0.0)
        self.assertEqual(marker_count["value"], 0)
        self.assertEqual(payload["collection"]["tasks"][0]["semantic_markers"], [])

    def test_builder_rejects_private_fields_in_nominally_public_input(self) -> None:
        report = public_report("safe-root")
        payload = pilot_snapshot((report,)).as_dict()
        payload["project_id"] = "raw-project-secret"

        with self.assertRaisesRegex(ValueError, "private field") as error:
            build_audit(PilotStatus(payload), (report,), storage())

        self.assertNotIn("raw-project-secret", str(error.exception))

    def test_builder_rejects_report_not_bound_to_pilot_collection(self) -> None:
        bound = public_report("bound-root")
        extra = public_report("extra-root")

        with self.assertRaisesRegex(ValueError, "task collection"):
            build_audit(pilot_snapshot((bound,)), (bound, extra), storage())

    def test_family_conflict_is_excluded_from_comparability_readiness(self) -> None:
        report = public_report("family-conflict")
        report = replace(
            report,
            task_family=None,
            trend_input=replace(report.trend_input, task_family=None),
        )
        snapshot = pilot_snapshot((report,)).as_dict()
        snapshot["tasks"][0]["task_family"] = None
        snapshot["tasks"][0]["trend_eligible"] = False

        collection = build_audit(
            PilotStatus(snapshot), (report,), storage(),
        ).as_dict()["collection"]

        self.assertEqual(collection["comparability_readiness"]["status"], "excluded")
        self.assertIn(
            "task_family_unavailable",
            collection["comparability_readiness"]["reasons"],
        )
        self.assertEqual(
            collection["tasks"][0]["comparability"]["status"], "excluded",
        )

    def test_expanded_scope_is_excluded_without_pairwise_comparison_claims(self) -> None:
        report = public_report("expanded-scope")
        snapshot = pilot_snapshot((report,)).as_dict()
        snapshot["tasks"][0]["scope_change"] = "expanded"
        snapshot["tasks"][0]["trend_eligible"] = False

        collection = build_audit(
            PilotStatus(snapshot), (report,), storage(),
        ).as_dict()["collection"]

        readiness = collection["comparability_readiness"]
        self.assertEqual(readiness["status"], "excluded")
        self.assertIn("scope_change_excluded", readiness["reasons"])
        self.assertNotIn("verdict", readiness)

    def test_single_eligible_task_has_unknown_readiness_and_insufficient_baseline(self) -> None:
        report = public_report("one-task")

        readiness = build_audit(
            pilot_snapshot((report,)), (report,), storage(),
        ).as_dict()["collection"]["comparability_readiness"]

        self.assertEqual(readiness["status"], "unknown")
        self.assertEqual(
            readiness["reasons"],
            [
                "insufficient_comparable_baseline",
                "pilot_thresholds_unverified",
                "verified_receipt_required",
            ],
        )


if __name__ == "__main__":
    unittest.main()
