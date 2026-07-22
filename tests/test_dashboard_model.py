from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from types import MappingProxyType
import unittest

from hydra_codex.dashboard_model import (
    DashboardProjectSummary,
    DashboardRefreshView,
    DashboardSnapshot,
    DashboardTaskPage,
)
from hydra_codex.public_payload import reject_private_fields
from hydra_codex.reporting import NumericFact


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

        self.assertEqual(snapshot.as_dict()["schema_version"], "hydra.dashboard/v1")
        self.assertEqual(snapshot.as_dict()["project"]["display_name"], "Hydra <Core>")
        with self.assertRaises(FrozenInstanceError):
            snapshot.generated_at = "changed"  # type: ignore[misc]

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

    def test_public_payload_rejects_path_like_visible_values(self) -> None:
        for value in (
            "/Users/alice/private-project",
            r"C:\\Users\\alice\\private-project",
            r"\\\\server\\private-project",
            "~/private-project",
            "file:///Users/alice/private-project",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reject_private_fields({"display_name": value})

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


if __name__ == "__main__":
    unittest.main()
