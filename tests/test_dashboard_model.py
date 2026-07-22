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
                },
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
            MappingProxyType({"state": "current"}),
            (summary,),
            summary.project_ref,
            canonical(project),
            None,
            self.refresh(),
        )

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
