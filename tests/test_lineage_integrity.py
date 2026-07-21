from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore


KEY = b"lineage-integrity-fixture-key-01"


def write_jsonl(path: Path, rows: tuple[dict[str, object], ...]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def rollout_row(kind: str, payload: dict[str, object], second: int) -> dict[str, object]:
    return {
        "timestamp": f"2026-07-20T00:00:{second:02d}Z",
        "type": kind,
        "payload": payload,
    }


class LineageIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self._project("project-a", "project-a")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _project(self, name: str, project_id: str) -> Path:
        project = self.base / name
        (project / ".hydra").mkdir(parents=True)
        (project / ".hydra" / "project.toml").write_text(
            f'project_id = "{project_id}"\n', encoding="utf-8",
        )
        return project

    def _inferred_source(self, parent: str, suffix: str) -> Path:
        source = self.base / f"{suffix}.jsonl"
        write_jsonl(source, (
            rollout_row("session_meta", {"id": parent, "cwd": str(self.project)}, 1),
            rollout_row(
                "event_msg",
                {"type": "sub_agent_activity", "agent_thread_id": "shared-child"},
                2,
            ),
        ))
        return source

    def test_conflicting_inferred_parents_are_ambiguous_in_both_orders(self) -> None:
        parent_a = self._inferred_source("parent-a", "parent-a")
        parent_b = self._inferred_source("parent-b", "parent-b")

        observed: list[tuple[object, ...]] = []
        for index, sources in enumerate(((parent_a, parent_b), (parent_b, parent_a))):
            store = HydraStore(self.base / f"inferred-order-{index}.sqlite3")
            self.addCleanup(store.close)
            for source in sources:
                ingest_rollouts(
                    store, (source,), self.project, "project-a", hash_key=KEY,
                )
            child = Pseudonymizer(KEY).digest("identity", "shared-child")
            row = store.connection.execute(
                "SELECT parent_key,confidence_kind,confidence FROM session_edges "
                "WHERE child_key=?",
                (child,),
            ).fetchone()
            observed.append(tuple(row))

        self.assertEqual(observed, [
            (None, "ambiguous", 0.0),
            (None, "ambiguous", 0.0),
        ])

    def test_ambiguous_inferred_claim_is_not_resolved_by_arrival_order(self) -> None:
        first = self._inferred_source("parent-a", "ambiguous-a")
        second = self._inferred_source("parent-b", "ambiguous-b")
        confirmed = self.base / "confirmed-child.jsonl"
        write_jsonl(confirmed, (rollout_row("session_meta", {
            "id": "shared-child", "cwd": str(self.project),
            "parent_thread_id": "parent-a",
        }, 3),))
        store = HydraStore(self.base / "ambiguous-sticky.sqlite3")
        self.addCleanup(store.close)

        for source in (first, second, confirmed):
            ingest_rollouts(
                store, (source,), self.project, "project-a", hash_key=KEY,
            )

        child = Pseudonymizer(KEY).digest("identity", "shared-child")
        row = store.connection.execute(
            "SELECT parent_key,confidence_kind,confidence FROM session_edges WHERE child_key=?",
            (child,),
        ).fetchone()
        self.assertEqual(tuple(row), (None, "ambiguous", 0.0))

    def test_rollout_session_identity_cannot_cross_projects(self) -> None:
        project_b = self._project("project-b", "project-b")
        source_a = self.base / "session-a.jsonl"
        source_b = self.base / "session-b.jsonl"
        write_jsonl(source_a, (rollout_row(
            "session_meta", {"id": "shared-session", "cwd": str(self.project)}, 1,
        ),))
        write_jsonl(source_b, (rollout_row(
            "session_meta", {"id": "shared-session", "cwd": str(project_b)}, 2,
        ),))
        store = HydraStore(self.base / "cross-project-rollout.sqlite3")
        self.addCleanup(store.close)
        ingest_rollouts(store, (source_a,), self.project, "project-a", hash_key=KEY)

        with self.assertRaisesRegex(ValueError, "another project"):
            ingest_rollouts(store, (source_b,), project_b, "project-b", hash_key=KEY)

        session = Pseudonymizer(KEY).digest("identity", "shared-session")
        project_id = store.connection.execute(
            "SELECT project_id FROM rollout_sessions WHERE session_key=?", (session,),
        ).fetchone()[0]
        self.assertEqual(project_id, "project-a")

    def test_spawned_child_identity_cannot_cross_projects(self) -> None:
        project_b = self._project("project-b", "project-b")
        child_source = self.base / "child-project-a.jsonl"
        write_jsonl(child_source, (rollout_row(
            "session_meta", {"id": "shared-child", "cwd": str(self.project)}, 1,
        ),))
        app_source = self.base / "spawn-project-b.jsonl"
        write_jsonl(app_source, ({
            "received_at": "2026-07-20T00:00:02Z",
            "message": {
                "method": "item/completed",
                "params": {
                    "threadId": "parent-b", "turnId": "spawn-turn",
                    "item": {
                        "id": "spawn-call", "type": "collabToolCall",
                        "senderThreadId": "parent-b", "newThreadId": "shared-child",
                        "status": "completed",
                    },
                },
            },
        },))
        store = HydraStore(self.base / "cross-project-child.sqlite3")
        self.addCleanup(store.close)
        ingest_rollouts(store, (child_source,), self.project, "project-a", hash_key=KEY)

        with self.assertRaisesRegex(ValueError, "another project"):
            ingest_codex_events(
                store, (CodexEventSource(app_source, APP_SERVER_V2),),
                project_b, "project-b", hash_key=KEY,
            )

        child = Pseudonymizer(KEY).digest("identity", "shared-child")
        self.assertEqual(
            store.connection.execute(
                "SELECT project_id FROM rollout_sessions WHERE session_key=?", (child,),
            ).fetchone()[0],
            "project-a",
        )


if __name__ == "__main__":
    unittest.main()
