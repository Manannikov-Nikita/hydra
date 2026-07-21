from __future__ import annotations

import json
from itertools import permutations
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2
from hydra_codex.lineage import persist_confirmed_parent
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import MIGRATIONS, V2_TRIGGER_STATEMENTS, HydraStore


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

    def test_confirmed_parent_wins_in_every_claim_order(self) -> None:
        first = self._inferred_source("parent-a", "ambiguous-a")
        second = self._inferred_source("parent-b", "ambiguous-b")
        confirmed = self.base / "confirmed-child.jsonl"
        write_jsonl(confirmed, (rollout_row("session_meta", {
            "id": "shared-child", "cwd": str(self.project),
            "parent_thread_id": "parent-a",
        }, 3),))
        child = Pseudonymizer(KEY).digest("identity", "shared-child")
        expected_parent = Pseudonymizer(KEY).digest("identity", "parent-a")
        observed: list[tuple[object, ...]] = []
        for index, sources in enumerate(permutations((first, second, confirmed))):
            store = HydraStore(self.base / f"confirmed-order-{index}.sqlite3")
            self.addCleanup(store.close)
            for source in sources:
                ingest_rollouts(
                    store, (source,), self.project, "project-a", hash_key=KEY,
                )
            row = store.connection.execute(
                "SELECT parent_key,confidence_kind,confidence FROM session_edges WHERE child_key=?",
                (child,),
            ).fetchone()
            observed.append(tuple(row))
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM lineage_claim_candidates WHERE child_key=?",
                (child,),
            ).fetchone()[0], 3)

        self.assertEqual(observed, [
            (expected_parent, "confirmed", 1.0),
        ] * 6)

    def test_parent_endpoint_cannot_cross_projects_when_foreign_parent_exists_first(self) -> None:
        project_b = self._project("project-b", "project-b")
        parent_source = self.base / "foreign-parent-first.jsonl"
        child_source = self.base / "child-after-foreign-parent.jsonl"
        write_jsonl(parent_source, (rollout_row(
            "session_meta", {"id": "shared-parent", "cwd": str(project_b)}, 1,
        ),))
        write_jsonl(child_source, (rollout_row("session_meta", {
            "id": "project-a-child", "cwd": str(self.project),
            "parent_thread_id": "shared-parent",
        }, 2),))
        store = HydraStore(self.base / "foreign-parent-first.sqlite3")
        self.addCleanup(store.close)
        ingest_rollouts(store, (parent_source,), project_b, "project-b", hash_key=KEY)

        with self.assertRaisesRegex(ValueError, "another project"):
            ingest_rollouts(
                store, (child_source,), self.project, "project-a", hash_key=KEY,
            )

        parent = Pseudonymizer(KEY).digest("identity", "shared-parent")
        child = Pseudonymizer(KEY).digest("identity", "project-a-child")
        self.assertEqual(store.connection.execute(
            "SELECT project_id FROM rollout_sessions WHERE session_key=?", (parent,),
        ).fetchone()[0], "project-b")
        self.assertIsNone(store.connection.execute(
            "SELECT 1 FROM session_edges WHERE child_key=?", (child,),
        ).fetchone())

    def test_parent_endpoint_cannot_cross_projects_when_foreign_parent_arrives_later(self) -> None:
        project_b = self._project("project-b", "project-b")
        child_source = self.base / "child-before-foreign-parent.jsonl"
        parent_source = self.base / "foreign-parent-later.jsonl"
        write_jsonl(child_source, (rollout_row("session_meta", {
            "id": "project-a-child", "cwd": str(self.project),
            "parent_thread_id": "shared-parent",
        }, 1),))
        write_jsonl(parent_source, (rollout_row(
            "session_meta", {"id": "shared-parent", "cwd": str(project_b)}, 2,
        ),))
        store = HydraStore(self.base / "foreign-parent-later.sqlite3")
        self.addCleanup(store.close)
        ingest_rollouts(
            store, (child_source,), self.project, "project-a", hash_key=KEY,
        )

        with self.assertRaisesRegex(ValueError, "another project"):
            ingest_rollouts(
                store, (parent_source,), project_b, "project-b", hash_key=KEY,
            )

        parent = Pseudonymizer(KEY).digest("identity", "shared-parent")
        child = Pseudonymizer(KEY).digest("identity", "project-a-child")
        self.assertIsNone(store.connection.execute(
            "SELECT 1 FROM rollout_sessions WHERE session_key=?", (parent,),
        ).fetchone())
        expected = (parent, "confirmed", 1.0)
        self.assertEqual(tuple(store.connection.execute(
            "SELECT parent_key,confidence_kind,confidence FROM session_edges WHERE child_key=?",
            (child,),
        ).fetchone()), expected)

    def test_app_session_cannot_claim_foreign_project_after_parent_reference(self) -> None:
        project_b = self._project("project-b", "project-b")
        child_source = self.base / "child-before-foreign-app-parent.jsonl"
        app_source = self.base / "foreign-app-parent-later.jsonl"
        write_jsonl(child_source, (rollout_row("session_meta", {
            "id": "project-a-child", "cwd": str(self.project),
            "parent_thread_id": "shared-parent",
        }, 1),))
        write_jsonl(app_source, ({
            "received_at": "2026-07-20T00:00:02Z",
            "message": {"method": "turn/started", "params": {
                "threadId": "shared-parent",
                "turn": {"id": "turn-b", "items": [], "status": "inProgress"},
            }},
        },))
        store = HydraStore(self.base / "foreign-app-parent-later.sqlite3")
        self.addCleanup(store.close)
        ingest_rollouts(
            store, (child_source,), self.project, "project-a", hash_key=KEY,
        )

        with self.assertRaisesRegex(ValueError, "another project"):
            ingest_codex_events(
                store, (CodexEventSource(app_source, APP_SERVER_V2),),
                project_b, "project-b", hash_key=KEY,
            )

        parent = Pseudonymizer(KEY).digest("identity", "shared-parent")
        self.assertIsNone(store.connection.execute(
            "SELECT 1 FROM rollout_sessions WHERE session_key=?", (parent,),
        ).fetchone())

    def test_unknown_parent_identity_can_be_claimed_by_only_one_project(self) -> None:
        project_b = self._project("project-b", "project-b")
        source_a = self.base / "project-a-parent-claim.jsonl"
        source_b = self.base / "project-b-parent-claim.jsonl"
        write_jsonl(source_a, (rollout_row("session_meta", {
            "id": "child-a", "cwd": str(self.project),
            "parent_thread_id": "shared-parent",
        }, 1),))
        write_jsonl(source_b, (rollout_row("session_meta", {
            "id": "child-b", "cwd": str(project_b),
            "parent_thread_id": "shared-parent",
        }, 2),))
        scenarios = (
            (source_a, self.project, "project-a", source_b, project_b, "project-b"),
            (source_b, project_b, "project-b", source_a, self.project, "project-a"),
        )
        for index, scenario in enumerate(scenarios):
            first, first_root, first_project, second, second_root, second_project = scenario
            store = HydraStore(self.base / f"cross-project-parent-claim-{index}.sqlite3")
            self.addCleanup(store.close)
            ingest_rollouts(
                store, (first,), first_root, first_project, hash_key=KEY,
            )
            with self.assertRaisesRegex(ValueError, "another project"):
                ingest_rollouts(
                    store, (second,), second_root, second_project, hash_key=KEY,
                )
            parent = Pseudonymizer(KEY).digest("identity", "shared-parent")
            self.assertEqual(store.connection.execute(
                """SELECT COUNT(*) FROM lineage_claim_candidates
                    WHERE parent_key=?""",
                (parent,),
            ).fetchone()[0], 1)

    def test_same_project_parent_claim_accepts_later_session_metadata(self) -> None:
        child_source = self.base / "child-before-local-parent.jsonl"
        parent_source = self.base / "local-parent-later.jsonl"
        write_jsonl(child_source, (rollout_row("session_meta", {
            "id": "project-a-child", "cwd": str(self.project),
            "parent_thread_id": "local-parent",
        }, 1),))
        write_jsonl(parent_source, (rollout_row(
            "session_meta", {"id": "local-parent", "cwd": str(self.project)}, 2,
        ),))
        store = HydraStore(self.base / "local-parent-later.sqlite3")
        self.addCleanup(store.close)
        ingest_rollouts(
            store, (child_source,), self.project, "project-a", hash_key=KEY,
        )
        parent = Pseudonymizer(KEY).digest("identity", "local-parent")
        self.assertIsNone(store.connection.execute(
            "SELECT 1 FROM rollout_sessions WHERE session_key=?", (parent,),
        ).fetchone())
        ingest_rollouts(
            store, (parent_source,), self.project, "project-a", hash_key=KEY,
        )

        row = store.connection.execute(
            "SELECT path_key,conversation_key FROM rollout_sessions WHERE session_key=?",
            (parent,),
        ).fetchone()
        self.assertNotEqual(row[0], "unresolved")
        self.assertNotEqual(row[1], "")

    def test_version_23_preserves_legacy_claims_and_legacy_ambiguity(self) -> None:
        database = self.base / "legacy-lineage.sqlite3"
        connection = sqlite3.connect(database)
        for version, statements in MIGRATIONS:
            if version > 22:
                break
            for statement in statements:
                connection.execute(statement)
            if version == 2:
                for statement in V2_TRIGGER_STATEMENTS:
                    connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) "
                "VALUES (?,'2026-07-21T00:00:00Z')",
                (version,),
            )
            connection.execute(f"PRAGMA user_version={version}")
        for session in (
            "confirmed-child", "inferred-child", "ambiguous-child",
            "unsupported-child", "confirmed-parent", "inferred-parent",
        ):
            connection.execute(
                """INSERT INTO rollout_sessions(
                       session_key,project_id,path_key,resume_segments,conversation_key)
                   VALUES (?,'project-a','safe',1,?)""",
                (session, session),
            )
        connection.executemany(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,conversation_key)
               VALUES (?,?,'safe',1,?)""",
            (
                ("cross-child-a", "project-a", "cross-child-a"),
                ("cross-child-b", "project-b", "cross-child-b"),
            ),
        )
        connection.executemany(
            """INSERT INTO session_edges(
                   child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
               VALUES (?,?,NULL,?,?)""",
            (
                ("confirmed-child", "confirmed-parent", "confirmed", 1.0),
                ("inferred-child", "inferred-parent", "inferred", 0.6),
                ("ambiguous-child", None, "ambiguous", 0.0),
                ("unsupported-child", "confirmed-parent", "unsupported", 0.4),
                ("cross-child-a", "shared-missing-parent", "confirmed", 1.0),
                ("cross-child-b", "shared-missing-parent", "confirmed", 1.0),
            ),
        )
        connection.commit()
        connection.close()

        store = HydraStore(database)
        self.addCleanup(store.close)
        self.assertEqual(
            [tuple(row) for row in store.connection.execute(
                """SELECT child_key,parent_key,claim_kind
                     FROM lineage_claim_candidates ORDER BY child_key"""
            )],
            [
                ("ambiguous-child", "", "legacy_ambiguous"),
                ("confirmed-child", "confirmed-parent", "confirmed"),
                ("cross-child-a", "", "legacy_ambiguous"),
                ("cross-child-b", "", "legacy_ambiguous"),
                ("inferred-child", "inferred-parent", "inferred"),
                ("unsupported-child", "", "legacy_ambiguous"),
            ],
        )
        self.assertIsNone(store.connection.execute(
            "SELECT 1 FROM rollout_sessions WHERE session_key='shared-missing-parent'",
        ).fetchone())
        persist_confirmed_parent(
            store.connection, child_key="ambiguous-child",
            parent_key="confirmed-parent", project_id="project-a",
        )
        persist_confirmed_parent(
            store.connection, child_key="ambiguous-child",
            parent_key="confirmed-parent", project_id="project-a",
        )
        self.assertEqual(store.connection.execute(
            """SELECT COUNT(*) FROM lineage_claim_candidates
                 WHERE child_key='ambiguous-child'"""
        ).fetchone()[0], 2)
        self.assertEqual(tuple(store.connection.execute(
            """SELECT parent_key,confidence_kind,confidence
                 FROM session_edges WHERE child_key='ambiguous-child'"""
        ).fetchone()), (None, "ambiguous", 0.0))

        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                """UPDATE lineage_claim_candidates SET claim_kind='inferred'
                     WHERE child_key='confirmed-child'"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                """DELETE FROM lineage_claim_candidates
                     WHERE child_key='confirmed-child'"""
            )

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
