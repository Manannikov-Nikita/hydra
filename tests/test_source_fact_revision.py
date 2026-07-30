from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hydra_codex.migrations_ad30 import (
    AD30_REQUIRED_TRIGGER_SQL,
    AD30_SOURCE_FACT_PROJECT_SELECTORS,
)
from hydra_codex.storage import HydraStore
from hydra_codex.sync_state import SyncStateRepository


EXPECTED_SOURCE_FACT_TABLES = frozenset({
    "annotation_transport_events",
    "annotations",
    "codex_event_issues",
    "codex_event_sources",
    "codex_events",
    "file_observation_candidates",
    "file_observations",
    "fork_baselines",
    "hook_safe_facts",
    "lineage_claim_candidates",
    "pilot_receipts",
    "pilot_runs",
    "pilot_tasks",
    "rollout_diagnostics",
    "rollout_events",
    "rollout_logical_sources",
    "rollout_revision_events",
    "rollout_sessions",
    "rollout_sources",
    "rollout_test_runs",
    "semantic_conflicts",
    "semantic_fact_staging",
    "semantic_intervals",
    "session_edges",
    "test_evidence_candidates",
    "token_snapshots",
    "tool_span_candidates",
    "tool_span_roles",
    "tool_spans",
    "trusted_turn_bindings",
    "turn_attempts",
    "turn_lifecycle_events",
})


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).lower()


class SourceFactRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = HydraStore(Path(self.temporary.name) / "hydra.sqlite3")
        self.connection = self.store.connection

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def revisions(self, project_id: str) -> tuple[int, int]:
        project = self.connection.execute(
            """SELECT revision FROM sync_project_source_fact_revisions
                WHERE project_id=?""",
            (project_id,),
        ).fetchone()
        unattributed = self.connection.execute(
            """SELECT revision FROM sync_unattributed_source_fact_revision
                WHERE singleton=1""",
        ).fetchone()
        return (
            0 if project is None else int(project[0]),
            int(unattributed[0]),
        )

    def test_schema_has_exact_project_scoped_trigger_inventory(self) -> None:
        self.assertEqual(
            frozenset(AD30_SOURCE_FACT_PROJECT_SELECTORS),
            EXPECTED_SOURCE_FACT_TABLES,
        )
        self.assertEqual(
            len(AD30_REQUIRED_TRIGGER_SQL),
            len(EXPECTED_SOURCE_FACT_TABLES) * 3,
        )
        actual = {
            str(row[0]): str(row[1])
            for row in self.connection.execute(
                """SELECT name,sql FROM sqlite_master
                    WHERE type='trigger'
                      AND name LIKE 'source_fact_revision_%'"""
            )
        }
        self.assertEqual(set(actual), set(AD30_REQUIRED_TRIGGER_SQL))
        self.assertEqual(
            {name: _normalized(sql) for name, sql in actual.items()},
            {
                name: _normalized(sql)
                for name, sql in AD30_REQUIRED_TRIGGER_SQL.items()
            },
        )
        self.assertEqual(
            {
                str(row[0])
                for row in self.connection.execute(
                    """SELECT name FROM sqlite_master
                        WHERE type='table' AND name LIKE 'sync_%fact_revision%'"""
                )
            },
            {
                "sync_project_source_fact_revisions",
                "sync_unattributed_source_fact_revision",
            },
        )
        self.assertEqual(
            {
                str(row[1])
                for row in self.connection.execute(
                    "PRAGMA table_info(sync_project_reconcile_fences)",
                )
            },
            {
                "project_id",
                "project_revision",
                "unattributed_revision",
                "storage_schema_version",
                "storage_schema_cookie",
                "reconciliation_version",
                "input_digest",
            },
        )
        for table in EXPECTED_SOURCE_FACT_TABLES:
            for operation, timing in (
                ("insert", "after insert"),
                ("update", "before update"),
                ("delete", "before delete"),
            ):
                sql = _normalized(
                    actual[f"source_fact_revision_{table}_{operation}"],
                )
                self.assertIn(timing, sql)
        self.assertEqual(self.revisions("project-unseen"), (0, 0))

    def test_control_commits_and_pending_hook_outbox_do_not_change_fact_revision(
        self,
    ) -> None:
        repository = SyncStateRepository(self.store)
        before = self.revisions("project-control")

        repository.create_job("sync", "2026-07-29T00:00:00Z")
        self.assertTrue(repository.acquire_lease(
            "control-worker",
            "2026-07-29T00:00:01Z",
            "2026-07-29T00:01:00Z",
        ))
        repository.record_hook_event_and_enqueue(
            event_key="pending-control-event",
            project_id="project-control",
            session_key="session-control",
            turn_key="turn-control",
            event_kind="prompt",
            observed_at="2026-07-29T00:00:02Z",
        )

        self.assertEqual(self.revisions("project-control"), before)

    def test_direct_source_insert_update_delete_bumps_only_its_project(self) -> None:
        data_revision = int(self.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1",
        ).fetchone()[0])
        statement = """INSERT INTO hook_safe_facts(
                           event_key,project_id,session_key,turn_key,event_kind,
                           tool_category,tool_status,duration_ms,observed_at)
                       VALUES (
                           'event-a','project-a','session-a','turn-a','prompt',
                           NULL,NULL,NULL,'2026-07-29T00:00:00Z'
                       )"""
        self.connection.execute(statement)
        self.connection.commit()
        self.assertEqual(self.revisions("project-a"), (1, 0))
        self.assertEqual(self.revisions("project-b"), (0, 0))
        self.assertEqual(
            self.connection.execute(
                "SELECT revision FROM sync_data_revision WHERE singleton=1",
            ).fetchone()[0],
            data_revision + 1,
        )

        self.connection.execute(
            """UPDATE hook_safe_facts SET observed_at='2026-07-29T00:00:01Z'
                WHERE event_key='event-a'"""
        )
        self.connection.commit()
        self.assertEqual(self.revisions("project-a"), (2, 0))
        self.assertEqual(
            self.connection.execute(
                "SELECT revision FROM sync_data_revision WHERE singleton=1",
            ).fetchone()[0],
            data_revision + 2,
        )

        self.connection.execute(
            "DELETE FROM hook_safe_facts WHERE event_key='event-a'"
        )
        self.connection.commit()
        self.assertEqual(self.revisions("project-a"), (3, 0))
        self.assertEqual(
            self.connection.execute(
                "SELECT revision FROM sync_data_revision WHERE singleton=1",
            ).fetchone()[0],
            data_revision + 3,
        )

    def test_indirect_source_fact_maps_through_session(self) -> None:
        self.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES ('session-indirect','project-indirect','safe',1,'')"""
        )
        self.connection.commit()
        before = self.revisions("project-indirect")

        self.connection.execute(
            """INSERT INTO tool_spans(
                   session_key,call_key,category,terminal_state,
                   completeness,provenance)
               VALUES (
                   'session-indirect','call-indirect','shell','success',
                   'complete','exact'
               )"""
        )
        self.connection.commit()

        self.assertEqual(
            self.revisions("project-indirect"),
            (before[0] + 1, before[1]),
        )

    def test_indirect_source_fact_maps_through_rollout_source_when_session_is_missing(
        self,
    ) -> None:
        self.connection.execute(
            """INSERT INTO rollout_logical_sources(
                   logical_source_key,project_id,session_key,
                   canonical_revision_digest,lineage_state)
               VALUES ('logical-source','project-source',NULL,NULL,'clean')"""
        )
        self.connection.execute(
            """INSERT INTO rollout_sources(
                   source_digest,source_type,logical_source_key)
               VALUES ('source-mapped','rollout','logical-source')"""
        )
        self.connection.commit()
        before = self.revisions("project-source")

        self.connection.execute(
            """INSERT INTO file_observations(
                   source_digest,line_number,session_key,operation,
                   relative_path,path_hash)
               VALUES (
                   'source-mapped',1,'missing-session','read',
                   'safe.txt','safe-hash'
               )"""
        )
        self.connection.commit()

        self.assertEqual(
            self.revisions("project-source"),
            (before[0] + 1, before[1]),
        )

    def test_session_edge_bumps_both_child_and_parent_projects(self) -> None:
        self.connection.executemany(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES (?,?,'safe',1,'')""",
            (
                ("edge-child", "project-child"),
                ("edge-parent", "project-parent"),
            ),
        )
        self.connection.commit()
        child_before = self.revisions("project-child")
        parent_before = self.revisions("project-parent")

        self.connection.execute(
            """INSERT INTO session_edges(
                   child_key,parent_key,baseline_working_tokens,
                   confidence_kind,confidence)
               VALUES (
                   'edge-child','edge-parent',NULL,'confirmed',1.0
               )"""
        )
        self.connection.commit()

        self.assertEqual(
            self.revisions("project-child"),
            (child_before[0] + 1, child_before[1]),
        )
        self.assertEqual(
            self.revisions("project-parent"),
            (parent_before[0] + 1, parent_before[1]),
        )

    def test_unresolved_indirect_fact_bumps_unattributed_revision(self) -> None:
        self.connection.execute(
            """INSERT INTO file_observations(
                   source_digest,line_number,session_key,operation,
                   relative_path,path_hash)
               VALUES (
                   'missing-source',1,'missing-session','read',
                   'safe.txt','safe-hash'
               )"""
        )
        self.connection.commit()

        self.assertEqual(self.revisions("project-any"), (0, 1))

    def test_update_from_mapped_to_unresolved_bumps_project_and_fallback(
        self,
    ) -> None:
        self.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES ('session-moving','project-moving','safe',1,'')"""
        )
        self.connection.execute(
            """INSERT INTO file_observations(
                   source_digest,line_number,session_key,operation,
                   relative_path,path_hash)
               VALUES (
                   'moving-source',1,'session-moving','read',
                   'safe.txt','safe-hash'
               )"""
        )
        self.connection.commit()
        before = self.revisions("project-moving")

        self.connection.execute(
            """UPDATE file_observations SET session_key='missing-session'
                WHERE source_digest='moving-source' AND line_number=1"""
        )
        self.connection.commit()

        self.assertEqual(
            self.revisions("project-moving"),
            (before[0] + 1, before[1] + 1),
        )

    def test_reassignment_bumps_old_and_new_project_without_fallback(self) -> None:
        self.connection.executemany(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES (?,?,'safe',1,'')""",
            (
                ("session-old", "project-old"),
                ("session-new", "project-new"),
            ),
        )
        self.connection.execute(
            """INSERT INTO file_observations(
                   source_digest,line_number,session_key,operation,
                   relative_path,path_hash)
               VALUES (
                   'reassigned-source',1,'session-old','read',
                   'safe.txt','safe-hash'
               )"""
        )
        self.connection.commit()
        old_before = self.revisions("project-old")
        new_before = self.revisions("project-new")

        self.connection.execute(
            """UPDATE file_observations SET session_key='session-new'
                WHERE source_digest='reassigned-source' AND line_number=1"""
        )
        self.connection.commit()

        self.assertEqual(
            self.revisions("project-old"),
            (old_before[0] + 1, old_before[1]),
        )
        self.assertEqual(
            self.revisions("project-new"),
            (new_before[0] + 1, new_before[1]),
        )

    def test_missing_unattributed_singleton_aborts_and_rolls_back_source_write(
        self,
    ) -> None:
        self.connection.execute(
            "DELETE FROM sync_unattributed_source_fact_revision WHERE singleton=1",
        )
        self.connection.commit()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "unattributed source fact revision missing",
        ):
            self.connection.execute(
                """INSERT INTO file_observations(
                       source_digest,line_number,session_key,operation,
                       relative_path,path_hash)
                   VALUES (
                       'missing-fallback',1,'missing-session','read',
                       'safe.txt','safe-hash'
                   )"""
            )
        self.connection.rollback()

        self.assertIsNone(self.connection.execute(
            """SELECT 1 FROM file_observations
                WHERE source_digest='missing-fallback'""",
        ).fetchone())

    def test_revision_overflow_aborts_and_rolls_back_source_write(self) -> None:
        maximum = 9_223_372_036_854_775_807
        self.connection.execute(
            """INSERT INTO sync_project_source_fact_revisions(
                   project_id,revision)
               VALUES ('project-overflow',?)""",
            (maximum,),
        )
        self.connection.commit()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "source fact revision exhausted",
        ):
            self.connection.execute(
                """INSERT INTO hook_safe_facts(
                       event_key,project_id,session_key,turn_key,event_kind,
                       tool_category,tool_status,duration_ms,observed_at)
                   VALUES (
                       'event-overflow','project-overflow','session-overflow',
                       'turn-overflow','prompt',NULL,NULL,NULL,
                       '2026-07-29T00:00:00Z'
                   )"""
            )
        self.connection.rollback()

        self.assertEqual(self.revisions("project-overflow"), (maximum, 0))
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM hook_safe_facts WHERE event_key='event-overflow'",
        ).fetchone())

    def test_rolled_back_source_write_does_not_change_revision(self) -> None:
        before = self.revisions("project-rollback")
        self.connection.execute(
            """INSERT INTO hook_safe_facts(
                   event_key,project_id,session_key,turn_key,event_kind,
                   tool_category,tool_status,duration_ms,observed_at)
               VALUES (
                   'event-rollback','project-rollback','session-rollback',
                   'turn-rollback','prompt',NULL,NULL,NULL,
                   '2026-07-29T00:00:00Z'
               )"""
        )
        self.connection.rollback()

        self.assertEqual(self.revisions("project-rollback"), before)


if __name__ == "__main__":
    unittest.main()
