from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from types import MappingProxyType
import unittest
from copy import deepcopy

from hydra_codex.dashboard_model import (
    DashboardProjectSummary,
    DashboardRefreshView,
    DashboardSnapshot,
    DashboardTaskPage,
)
from hydra_codex.public_payload import reject_private_fields
from hydra_codex.reporting import NumericFact
from tests.test_audit_builder import public_report


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class DashboardModelTests(unittest.TestCase):
    def refresh(self) -> DashboardRefreshView:
        return DashboardRefreshView(
            None,
            "idle",
            None,
            None,
            None,
            MappingProxyType({}),
            (),
        )

    def snapshot(self, *, report: bool = True) -> DashboardSnapshot:
        unavailable = NumericFact(
            None,
            "tokens",
            "estimated",
            ("no_reconciled_tasks",),
        )
        project = {
            "project_ref": "project_0123456789ab",
            "display_name": "Hydra <Core>",
            "last_activity_at": "2026-07-22T10:00:00Z",
            "freshness_state": "current",
            "overview": {
                "basis": {
                    "kind": "latest_task",
                    "task_ref": "task_0123456789ab" if report else None,
                },
                "headline": {
                    "working_tokens": (
                        NumericFact(1, "tokens", "derived").as_dict()
                        if report else unavailable.as_dict()
                    ),
                    "full_context_tokens": (
                        NumericFact(2, "tokens", "derived").as_dict()
                        if report else unavailable.as_dict()
                    ),
                    "wall_clock_ms": (
                        NumericFact(1, "milliseconds", "derived").as_dict()
                        if report else NumericFact(
                            None, "milliseconds", "estimated", ("no_reconciled_tasks",),
                        ).as_dict()
                    ),
                },
                "phase_allocation": None,
            },
            "recent_tasks": [],
            "pilot": None,
            "storage": {
                "baseline_state": "unavailable",
                "current": {
                    "database_bytes": {
                        "value": 0, "unit": "bytes", "provenance": "exact",
                        "caveats": [], "lower_bound": None,
                    },
                    "wal_bytes": {
                        "value": 0, "unit": "bytes", "provenance": "exact",
                        "caveats": [], "lower_bound": None,
                    },
                    **{
                        name: NumericFact(0, "count", "exact").as_dict()
                        for name in (
                            "rollout_sources", "rollout_events",
                            "codex_event_sources", "codex_events", "schema_version",
                        )
                    },
                },
                "baseline": None,
                "growth": None,
                "diagnostics": [
                    {"code": "growth_baseline_unavailable", "severity": "info"},
                ],
            },
            "system_health": {
                "scope": "global_launch_context",
                "doctor": self.doctor(),
            },
        }
        summary = DashboardProjectSummary(
            "project_0123456789ab",
            "Hydra <Core>",
            "2026-07-22T10:00:00Z",
            "current",
            NumericFact(1 if report else 0, "count", "derived"),
        )
        return DashboardSnapshot(
            "2026-07-22T12:00:00Z",
            MappingProxyType({
                "state": "current",
                "doctor": {
                    "scope": "global_launch_context",
                    "report": self.doctor(),
                },
            }),
            (summary,),
            summary.project_ref,
            canonical(project),
            None,
            self.refresh(),
        )

    @staticmethod
    def doctor() -> dict[str, object]:
        codes = (
            "project_resolution", "storage_available", "schema_current",
            "foreign_keys_ok", "integrity_ok", "storage_permissions_restricted",
        )
        return {
            "schema_version": "hydra.doctor/v1",
            "status": "healthy",
            "checks": [{"code": code, "status": "ok"} for code in codes],
        }

    def test_snapshot_is_immutable_and_canonical(self) -> None:
        snapshot = self.snapshot()

        self.assertEqual(snapshot.as_dict()["schema_version"], "hydra.dashboard/v2")
        self.assertEqual(snapshot.as_dict()["project"]["display_name"], "Hydra <Core>")
        with self.assertRaises(FrozenInstanceError):
            snapshot.generated_at = "changed"  # type: ignore[misc]

    def test_snapshot_rejects_malformed_sync_summary(self) -> None:
        baseline = self.snapshot()
        valid = {
            "schema_version": "hydra.dashboard-sync/v1",
            "sync_ref": "sync_0123456789abcdef0123456789abcdef",
            "kind": "sync",
            "state": "queued",
            "started_at": "2026-07-22T12:00:00Z",
            "finished_at": None,
            "progress": {
                "sources_queued": 0,
                "sources_processed": 0,
                "new_bytes": 0,
            },
        }
        cases = (
            {"schema_version": "wrong"},
            {**valid, "unexpected": "field"},
            {**valid, "sync_ref": "sync_private/path"},
            {**valid, "state": "unknown"},
            {**valid, "progress": {"sources_queued": -1, "sources_processed": 0, "new_bytes": 0}},
            {**valid, "progress": {"sources_queued": True, "sources_processed": 0, "new_bytes": 0}},
            {**valid, "started_at": "2026-07-22T12:00:00"},
            {**valid, "finished_at": "2026-07-22T12:01:00Z", "state": "succeeded", "progress": {"sources_queued": 1, "sources_processed": 2, "new_bytes": 0}},
        )
        for sync in cases:
            with self.subTest(sync=sync), self.assertRaises(ValueError):
                DashboardSnapshot(baseline.generated_at, baseline.freshness, baseline.projects,
                                  baseline.selected_project_ref, baseline.project_json,
                                  baseline.selected_task_json, baseline.refresh, 0, sync)

    def test_snapshot_keeps_strict_validation_for_external_project_tuples(self) -> None:
        snapshot = self.snapshot()
        duplicate = (snapshot.projects[0], snapshot.projects[0])

        with self.assertRaisesRegex(ValueError, "project references must be unique"):
            DashboardSnapshot(
                snapshot.generated_at,
                snapshot.freshness,
                duplicate,
                snapshot.selected_project_ref,
                snapshot.project_json,
                snapshot.selected_task_json,
                snapshot.refresh,
            )
        with self.assertRaisesRegex(
            ValueError, "projects must contain dashboard project summaries",
        ):
            DashboardSnapshot(
                snapshot.generated_at,
                snapshot.freshness,
                [snapshot.projects[0]],  # type: ignore[arg-type]
                snapshot.selected_project_ref,
                snapshot.project_json,
                snapshot.selected_task_json,
                snapshot.refresh,
            )

    def test_unavailable_fact_does_not_become_zero(self) -> None:
        payload = self.snapshot(report=False).as_dict()
        fact = payload["project"]["overview"]["headline"]["working_tokens"]

        self.assertIsNone(fact["value"])
        self.assertEqual(fact["provenance"], "estimated")
        self.assertIn("no_reconciled_tasks", fact["caveats"])

    def test_public_payload_rejects_private_vocabulary_recursively(self) -> None:
        for key in (
            "project_id",
            "session_id",
            "turn_id",
            "path",
            "prompt",
            "command",
            "source_root",
            "tool_output",
            "worktree_path",
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                reject_private_fields({"safe": [{key: "secret"}]})

    def test_structural_privacy_guard_allows_safe_semantic_note_vocabulary(self) -> None:
        for note in ("/api/status returned 500", "file: upload complete"):
            with self.subTest(note=note):
                reject_private_fields({"semantic": {"timeline": [{"note": note}]}})

    def test_selected_task_and_task_page_allow_safe_semantic_notes(self) -> None:
        for note in ("/api/status returned 500", "file: upload complete"):
            report = public_report("safe-note", input_tokens=10, second=10).as_dict()
            report["semantic"]["annotations"]["timeline"] = [{
                "kind": "phase",
                "phase": "review",
                "cause": "other",
                "scope_change": "none",
                "outcome": None,
                "confidence": 0.9,
                "note": note,
                "provenance": "model_reported",
            }]
            encoded = canonical(report)
            with self.subTest(note=note):
                page = DashboardTaskPage(
                    "2026-07-22T12:00:00Z",
                    "project_0123456789ab",
                    (encoded,),
                    50,
                    None,
                )
                self.assertEqual(
                    page.as_dict()["items"][0]["semantic"]["annotations"]["timeline"][0]["note"],
                    note,
                )
                baseline = self.snapshot()
                selected = DashboardSnapshot(
                    baseline.generated_at,
                    baseline.freshness,
                    baseline.projects,
                    baseline.selected_project_ref,
                    baseline.project_json,
                    encoded,
                    baseline.refresh,
                )
                self.assertEqual(
                    selected.as_dict()["selected_task"]["semantic"]["annotations"]["timeline"][0]["note"],
                    note,
                )

    def test_task_page_rejects_shallow_report_shaped_payload(self) -> None:
        with self.assertRaises(ValueError):
            DashboardTaskPage(
                "2026-07-22T12:00:00Z",
                "project_0123456789ab",
                (canonical({
                    "schema_version": "hydra.report/v3",
                    "task_ref": "task_0123456789ab",
                }),),
                50,
                None,
            )

    def test_snapshot_rejects_shallow_project_payload(self) -> None:
        snapshot = self.snapshot()
        shallow = canonical({
            "project_ref": "project_0123456789ab",
            "display_name": "Hydra Core",
        })

        with self.assertRaises(ValueError):
            DashboardSnapshot(
                snapshot.generated_at,
                snapshot.freshness,
                snapshot.projects,
                snapshot.selected_project_ref,
                shallow,
                None,
                snapshot.refresh,
            )

    def test_snapshot_rejects_malformed_freshness_shape(self) -> None:
        snapshot = self.snapshot()

        with self.assertRaises(ValueError):
            DashboardSnapshot(
                snapshot.generated_at,
                {"state": "current", "unexpected": True},
                snapshot.projects,
                snapshot.selected_project_ref,
                snapshot.project_json,
                None,
                snapshot.refresh,
            )

    def test_snapshot_rejects_malformed_numeric_fact_shape(self) -> None:
        snapshot = self.snapshot()
        project = json.loads(snapshot.project_json or "{}")
        project["overview"]["headline"]["working_tokens"]["unexpected"] = 1

        with self.assertRaises(ValueError):
            DashboardSnapshot(
                snapshot.generated_at,
                snapshot.freshness,
                snapshot.projects,
                snapshot.selected_project_ref,
                canonical(project),
                None,
                snapshot.refresh,
            )

    def test_task_page_requires_canonical_report_schema(self) -> None:
        with self.assertRaises(ValueError):
            DashboardTaskPage(
                "2026-07-22T12:00:00Z",
                "project_0123456789ab",
                (canonical({"schema_version": "wrong"}),),
                50,
                None,
            )

    def test_task_report_rejects_invalid_canonical_numeric_semantics(self) -> None:
        baseline = public_report("numeric-semantics", input_tokens=10, second=10).as_dict()
        mutations = (
            ("negative token", lambda payload: payload["recorded_tokens"]["input"].update(value=-1)),
            ("fractional token", lambda payload: payload["recorded_tokens"]["input"].update(value=1.5)),
            ("negative count", lambda payload: payload["counts"]["sessions"].update(value=-1)),
            ("fractional count", lambda payload: payload["counts"]["sessions"].update(value=1.5)),
            ("negative time", lambda payload: payload["timing"]["wall_clock"].update(value=-1)),
            ("fractional time", lambda payload: payload["timing"]["wall_clock"].update(value=1.5)),
            ("ratio above one", lambda payload: payload["semantic"]["coverage"].update(value=1.1)),
            (
                "negative lower bound",
                lambda payload: payload["counts"]["sessions"].update(lower_bound=-1),
            ),
            (
                "fractional lower bound",
                lambda payload: payload["counts"]["sessions"].update(lower_bound=0.5),
            ),
            (
                "negative semantic phase",
                lambda payload: payload["semantic"]["breakdown"]["phases"]["review"]["working"].update(value=-1),
            ),
        )
        for label, mutate in mutations:
            payload = deepcopy(baseline)
            mutate(payload)
            with self.subTest(label=label), self.assertRaises(ValueError):
                DashboardTaskPage(
                    "2026-07-22T12:00:00Z",
                    "project_0123456789ab",
                    (canonical(payload),),
                    50,
                    None,
                )

    def test_task_report_rejects_health_trend_and_evidence_incoherence(self) -> None:
        baseline = public_report("coherence", input_tokens=10, second=10).as_dict()

        verified_without_receipt = deepcopy(baseline)
        verified_without_receipt["pilot_health"].update(
            status="verified", receipt_verified=False,
        )
        bad_signal = deepcopy(baseline)
        bad_signal["trend"]["result"]["corroborating_signal"] = "private_signal"
        bad_evidence = deepcopy(baseline)
        bad_evidence["semantic"]["annotations"]["test_evidence"] = {
            "total_count": NumericFact(2, "count", "derived").as_dict(),
            "rows": [{
                "scope": "full", "failure_cause": "none", "retry_kind": "none",
                "phase": "test_full", "cause": "other",
                "count": NumericFact(1, "count", "derived").as_dict(),
            }],
            "caveats": [],
        }
        unsorted_evidence = deepcopy(baseline)
        unsorted_evidence["semantic"]["annotations"]["test_evidence"] = {
            "total_count": NumericFact(2, "count", "derived").as_dict(),
            "rows": [
                {
                    "scope": scope, "failure_cause": "none", "retry_kind": "none",
                    "phase": "test_full", "cause": "other",
                    "count": NumericFact(1, "count", "derived").as_dict(),
                }
                for scope in ("targeted", "full")
            ],
            "caveats": [],
        }
        long_timeline = deepcopy(baseline)
        long_timeline["semantic"]["annotations"]["timeline"] = [
            {
                "kind": "phase", "phase": "review", "cause": "other",
                "scope_change": "none", "outcome": None, "confidence": 0.9,
                "note": "safe review", "provenance": "model_reported",
            }
            for _ in range(21)
        ]
        long_note = deepcopy(baseline)
        long_note["semantic"]["annotations"]["timeline"] = [{
            "kind": "phase", "phase": "review", "cause": "other",
            "scope_change": "none", "outcome": None, "confidence": 0.9,
            "note": "a" * 241, "provenance": "model_reported",
        }]
        for label, payload in (
            ("verified without receipt", verified_without_receipt),
            ("unknown trend signal", bad_signal),
            ("inconsistent test evidence total", bad_evidence),
            ("unsorted test evidence", unsorted_evidence),
            ("unbounded timeline", long_timeline),
            ("unbounded marker note", long_note),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                DashboardTaskPage(
                    "2026-07-22T12:00:00Z",
                    "project_0123456789ab",
                    (canonical(payload),),
                    50,
                    None,
                )

    def test_task_report_allows_signed_canonical_trend_growth(self) -> None:
        payload = public_report("signed-trend", input_tokens=10, second=10).as_dict()
        payload["trend"]["result"]["token_growth"].update(value=-10)
        payload["trend"]["result"]["signal_growth"].update(value=-2)

        page = DashboardTaskPage(
            "2026-07-22T12:00:00Z",
            "project_0123456789ab",
            (canonical(payload),),
            50,
            None,
        )

        self.assertEqual(page.as_dict()["items"][0]["trend"]["result"]["token_growth"]["value"], -10)

    def test_dashboard_storage_enforces_exact_current_and_signed_growth_facts(self) -> None:
        snapshot = self.snapshot()
        signed = json.loads(snapshot.project_json or "{}")
        signed["storage"]["baseline_state"] = "available"
        signed["storage"]["baseline"] = deepcopy(signed["storage"]["current"])
        signed["storage"]["growth"] = {
            name: {
                "value": -1,
                "unit": "bytes" if name.endswith("_bytes") else "count",
                "provenance": "derived",
                "caveats": [],
                "lower_bound": None,
            }
            for name in (
                "database_bytes", "wal_bytes", "rollout_sources", "rollout_events",
                "codex_event_sources", "codex_events",
            )
        }
        DashboardSnapshot(
            snapshot.generated_at, snapshot.freshness, snapshot.projects,
            snapshot.selected_project_ref, canonical(signed), None,
            snapshot.refresh,
        )

        mutations = (
            ("derived current", {"provenance": "derived"}),
            ("negative current", {"value": -1}),
            ("fractional current", {"value": 1.5}),
        )
        for label, mutation in mutations:
            project = deepcopy(signed)
            project["storage"]["current"]["database_bytes"].update(mutation)
            with self.subTest(label=label), self.assertRaises(ValueError):
                DashboardSnapshot(
                    snapshot.generated_at, snapshot.freshness, snapshot.projects,
                    snapshot.selected_project_ref, canonical(project), None,
                    snapshot.refresh,
                )


if __name__ == "__main__":
    unittest.main()
