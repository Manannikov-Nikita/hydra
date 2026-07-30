from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import tempfile
import sqlite3
from threading import Barrier
import unittest
from unittest import mock
from pathlib import Path

from hydra_codex.annotation_core import (
    AnnotationConflict,
    AnnotationDisposition,
    CapabilityExpired,
    CapabilityRejected,
    TrustedAnnotationContext,
    TrustedTurnContext,
    annotate_with_capability,
    finish_turn,
    issue_capability,
    record_initial_understand,
)
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import MIGRATIONS, V2_TRIGGER_STATEMENTS, HydraStore


PROJECT_ID = "hprj_annotation_test"
RAW_SESSION = "raw-session-private-019f"
RAW_TURN = "raw-turn-private-7a42"
CREATED_AT = "2026-07-21T09:00:00Z"
EXPIRES_AT = "2026-07-21T10:00:00Z"


def turn_context() -> TrustedTurnContext:
    return TrustedTurnContext(
        project_id=PROJECT_ID,
        session_id=RAW_SESSION,
        turn_id=RAW_TURN,
        observed_at=CREATED_AT,
    )


def request(sequence: int, *, key: str | None = None, observed_at: str | None = None) -> TrustedAnnotationContext:
    return TrustedAnnotationContext(
        request_key=key or f"trusted-request-{sequence}",
        sequence=sequence,
        observed_at=observed_at or f"2026-07-21T09:0{sequence}:00Z",
    )


def phase_payload(phase: str = "implement", *, note: str = "working") -> dict[str, object]:
    return {
        "kind": "phase",
        "phase": phase,
        "cause": "plan",
        "scope_change": "none",
        "task_family": "annotation-core",
        "confidence": 0.9,
        "note": note,
    }


def finish_payload(*, note: str = "complete") -> dict[str, object]:
    return {
        "kind": "finish",
        "phase": "test_full",
        "cause": "final_verification",
        "scope_change": "none",
        "task_family": "annotation-core",
        "confidence": 0.95,
        "note": note,
        "outcome": "success",
    }


class AnnotationCapabilitySchemaTests(unittest.TestCase):
    def test_fresh_database_has_private_capability_annotation_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HydraStore(Path(temporary) / "hydra.sqlite3")
            self.addCleanup(store.close)

            expected = {
                "trusted_turn_bindings",
                "turn_capabilities",
                "annotation_receipts",
                "semantic_intervals",
                "semantic_fact_staging",
            }
            actual = {
                row[0]
                for row in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

            self.assertTrue(expected.issubset(actual))
            binding_columns = {
                row[1]
                for row in store.connection.execute("PRAGMA table_info(trusted_turn_bindings)")
            }
            self.assertIn("first_stop_at", binding_columns)

    def test_previous_database_upgrades_without_rewriting_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "upgrade.sqlite3"
            connection = sqlite3.connect(database)
            connection.create_function(
                "hydra_rfc3339_nanos",
                1,
                lambda _value: 0,
                deterministic=True,
            )
            for version, statements in MIGRATIONS[:-1]:
                for statement in statements:
                    connection.execute(statement)
                if version == 2:
                    for statement in V2_TRIGGER_STATEMENTS:
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES (?,'2026-07-21T00:00:00Z')",
                    (version,),
                )
                connection.execute(f"PRAGMA user_version={version}")
            connection.execute(
                "INSERT INTO sessions VALUES ('old-session','hprj_upgrade','safe','2026-07-21T00:00:00Z','exact')"
            )
            connection.execute(
                "INSERT INTO turns VALUES ('old-turn','old-session',0,'2026-07-21T00:00:00Z','exact')"
            )
            connection.execute(
                """INSERT INTO annotations(
                       annotation_id,project_id,session_id,turn_id,sequence,observed_at,kind,
                       phase,cause,scope_change,confidence,outcome,provenance,note_redacted,
                       note_hash,note_length,task_family)
                   VALUES ('old-annotation','hprj_upgrade','old-session','old-turn',0,
                       '2026-07-21T00:00:00Z','phase','understand','prompt','none',1.0,
                       NULL,'model_reported','safe','old-hash',4,'upgrade')"""
            )
            connection.execute(
                """INSERT INTO trusted_turn_bindings(
                       turn_key,project_id,session_key,created_at,state,last_sequence)
                   VALUES ('old-turn','hprj_upgrade','old-session',
                       '2026-07-21T00:00:00Z','open',-1)"""
            )
            connection.execute(
                """INSERT INTO turn_capabilities(
                       capability_digest,turn_key,created_at,expires_at,stop_retry)
                   VALUES ('legacy-capability','old-turn','2026-07-21T00:00:00Z',
                       '2026-07-21T01:00:00Z',1)"""
            )
            connection.commit()
            connection.close()

            store = HydraStore(database)
            self.addCleanup(store.close)

            self.assertEqual(store.connection.execute(
                "SELECT task_family FROM annotations WHERE annotation_id='old-annotation'"
            ).fetchone()[0], "upgrade")
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM trusted_turn_bindings"
            ).fetchone()[0], 1)
            self.assertIsNone(store.connection.execute(
                "SELECT first_stop_at FROM trusted_turn_bindings WHERE turn_key='old-turn'"
            ).fetchone()[0])


class AnnotationCapabilityCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)
        self.keys = Pseudonymizer(b"a" * 32)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def issue(self):
        return issue_capability(
            self.store,
            self.keys,
            turn_context(),
            expires_at=EXPIRES_AT,
        )

    def sync_dirty_project(self, *, observed_at: str, expires_at: str) -> None:
        from hydra_codex.incremental_sync import (
            IncrementalSyncWorker,
            TrustedSourceRoots,
        )
        from hydra_codex.reconcile_engine import reconcile_project

        sessions = Path(self.temporary.name) / "sessions"
        archived = Path(self.temporary.name) / "archived-sessions"
        sessions.mkdir(exist_ok=True)
        archived.mkdir(exist_ok=True)
        worker = IncrementalSyncWorker(
            self.store,
            TrustedSourceRoots(
                sessions=sessions,
                archived_sessions=archived,
            ),
            reconcile=lambda project_id, roots: reconcile_project(
                self.store,
                project_id,
                b"a" * 32,
                expected_dirty_roots=roots,
            ),
        )
        result = worker.sync_once(
            "annotation-test-sync",
            observed_at,
            expires_at,
            maximum_sources=1,
        )
        self.assertEqual(result.claimed, 0)

    def test_issue_returns_256_bit_secret_once_and_persists_only_bound_digest(self) -> None:
        issued = self.issue()

        capability = self.store.connection.execute("SELECT * FROM turn_capabilities").fetchone()
        binding = self.store.connection.execute("SELECT * FROM trusted_turn_bindings").fetchone()
        database_text = " ".join(
            value.decode("utf-8", "ignore")
            for value in (self.database.read_bytes(),)
        )

        self.assertRegex(issued.token, r"^hcap_v1_[A-Za-z0-9_-]{43}$")
        self.assertNotEqual(capability["capability_digest"], issued.token)
        self.assertEqual(binding["project_id"], PROJECT_ID)
        self.assertEqual(binding["session_key"], self.keys.digest("identity", RAW_SESSION))
        self.assertEqual(binding["turn_key"], self.keys.digest("turn", RAW_TURN))
        self.assertEqual((capability["used_at"], capability["revoked_at"], capability["stop_retry"]), (None, None, 0))
        self.assertNotIn(issued.token, database_text)
        self.assertNotIn(RAW_SESSION, database_text)
        self.assertNotIn(RAW_TURN, database_text)

    def test_capability_binding_commit_marks_dirty_but_reissue_does_not_churn(
        self,
    ) -> None:
        from hydra_codex.reconcile_engine import source_fact_fence_current
        from hydra_codex.sync_state import SyncStateRepository

        self.issue()
        repository = SyncStateRepository(self.store)
        self.assertEqual(
            [
                (
                    root.project_id,
                    root.root_key,
                    root.root_kind,
                    root.observed_at,
                )
                for root in repository.list_dirty_roots()
            ],
            [(PROJECT_ID, PROJECT_ID, "project", CREATED_AT)],
        )
        self.sync_dirty_project(
            observed_at="2026-07-21T09:00:30Z",
            expires_at="2026-07-21T09:00:40Z",
        )
        revision = self.store.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1"
        ).fetchone()[0]

        self.issue()

        self.assertEqual(repository.list_dirty_roots(), ())
        self.assertEqual(
            self.store.connection.execute(
                "SELECT revision FROM sync_data_revision WHERE singleton=1"
            ).fetchone()[0],
            revision,
        )
        self.assertTrue(source_fact_fence_current(self.store.connection, PROJECT_ID))

    def test_capability_binding_rolls_back_when_dirty_enqueue_fails(self) -> None:
        with mock.patch(
            "hydra_codex.sync_state.SyncStateRepository.mark_dirty_in_transaction",
            side_effect=RuntimeError("simulated dirty enqueue failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "dirty enqueue"):
                self.issue()

        self.assertEqual(
            tuple(
                self.store.connection.execute(
                    """SELECT
                           (SELECT COUNT(*) FROM sessions),
                           (SELECT COUNT(*) FROM turns),
                           (SELECT COUNT(*) FROM trusted_turn_bindings),
                           (SELECT COUNT(*) FROM turn_capabilities),
                           (SELECT COUNT(*) FROM sync_dirty_roots)"""
                ).fetchone()
            ),
            (0, 0, 0, 0, 0),
        )

    def test_initial_understand_and_multiple_annotations_share_one_capability(self) -> None:
        issued = self.issue()

        initial = record_initial_understand(
            self.store,
            self.keys,
            issued.token,
            request(0),
            task_family="annotation-core",
        )
        phase = annotate_with_capability(
            self.store,
            self.keys,
            issued.token,
            request(1),
            phase_payload(),
        )
        blocker = annotate_with_capability(
            self.store,
            self.keys,
            issued.token,
            request(2),
            {
                **phase_payload(),
                "kind": "blocker",
                "cause": "infra_failure",
                "note": "runner queue",
            },
        )

        rows = self.store.connection.execute(
            "SELECT kind,phase,sequence FROM annotations ORDER BY sequence"
        ).fetchall()
        intervals = self.store.connection.execute(
            "SELECT phase,start_sequence,end_sequence FROM semantic_intervals ORDER BY start_sequence"
        ).fetchall()
        capability = self.store.connection.execute("SELECT used_at,revoked_at FROM turn_capabilities").fetchone()

        self.assertEqual((initial.disposition, phase.disposition, blocker.disposition), (
            AnnotationDisposition.INSERTED,
            AnnotationDisposition.INSERTED,
            AnnotationDisposition.INSERTED,
        ))
        self.assertEqual([tuple(row) for row in rows], [
            ("phase", "understand", 0),
            ("phase", "implement", 1),
            ("blocker", "implement", 2),
        ])
        self.assertEqual([tuple(row) for row in intervals], [
            ("understand", 0, 1),
            ("implement", 1, 2),
            ("implement", 2, None),
        ])
        self.assertEqual(capability["used_at"], request(0).observed_at)
        self.assertIsNone(capability["revoked_at"])

    def test_annotation_commit_syncs_without_a_later_hook_outbox_event(self) -> None:
        from hydra_codex.reconcile_engine import source_fact_fence_current
        from hydra_codex.sync_state import SyncStateRepository

        issued = self.issue()
        record_initial_understand(
            self.store,
            self.keys,
            issued.token,
            request(0),
            task_family="annotation-core",
        )

        repository = SyncStateRepository(self.store)
        dirty = repository.list_dirty_roots()
        self.assertEqual(
            [
                (
                    root.project_id,
                    root.root_key,
                    root.root_kind,
                    root.observed_at,
                )
                for root in dirty
            ],
            [(PROJECT_ID, PROJECT_ID, "project", CREATED_AT)],
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM hook_event_outbox"
            ).fetchone()[0],
            0,
        )
        self.assertFalse(source_fact_fence_current(self.store.connection, PROJECT_ID))

        self.sync_dirty_project(
            observed_at="2026-07-21T09:02:00Z",
            expires_at="2026-07-21T09:03:00Z",
        )

        self.assertEqual(repository.list_dirty_roots(), ())
        self.assertTrue(source_fact_fence_current(self.store.connection, PROJECT_ID))

    def test_exact_annotation_retry_does_not_requeue_unchanged_source_facts(self) -> None:
        from hydra_codex.reconcile_engine import source_fact_fence_current
        from hydra_codex.sync_state import SyncStateRepository

        issued = self.issue()
        record_initial_understand(
            self.store,
            self.keys,
            issued.token,
            request(0),
            task_family="annotation-core",
        )
        self.sync_dirty_project(
            observed_at="2026-07-21T09:02:00Z",
            expires_at="2026-07-21T09:03:00Z",
        )
        revision = self.store.connection.execute(
            "SELECT revision FROM sync_data_revision WHERE singleton=1"
        ).fetchone()[0]

        retried = record_initial_understand(
            self.store,
            self.keys,
            issued.token,
            request(0, observed_at="2026-07-21T09:04:00Z"),
            task_family="annotation-core",
        )

        self.assertEqual(retried.disposition, AnnotationDisposition.RETRIED)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT revision FROM sync_data_revision WHERE singleton=1"
            ).fetchone()[0],
            revision,
        )
        self.assertEqual(SyncStateRepository(self.store).list_dirty_roots(), ())
        self.assertTrue(source_fact_fence_current(self.store.connection, PROJECT_ID))

    def test_annotation_source_write_rolls_back_when_dirty_enqueue_fails(self) -> None:
        issued = self.issue()
        with self.store.rollout_transaction() as connection:
            connection.execute("DELETE FROM sync_dirty_roots")

        with mock.patch(
            "hydra_codex.sync_state.SyncStateRepository.mark_dirty_in_transaction",
            side_effect=RuntimeError("simulated dirty enqueue failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "dirty enqueue"):
                record_initial_understand(
                    self.store,
                    self.keys,
                    issued.token,
                    request(0),
                    task_family="annotation-core",
                )

        self.assertEqual(
            tuple(
                self.store.connection.execute(
                    """SELECT
                           (SELECT COUNT(*) FROM annotations),
                           (SELECT COUNT(*) FROM annotation_receipts),
                           (SELECT COUNT(*) FROM semantic_intervals),
                           (SELECT COUNT(*) FROM sync_dirty_roots)"""
                ).fetchone()
            ),
            (0, 0, 0, 0),
        )

    def test_new_annotation_cannot_predate_binding_or_issuing_capability(self) -> None:
        issued = self.issue()

        with self.assertRaisesRegex(CapabilityRejected, "predates"):
            record_initial_understand(
                self.store,
                self.keys,
                issued.token,
                request(0, observed_at="2026-07-21T08:59:59Z"),
                task_family="annotation-core",
            )

        later_capability = issue_capability(
            self.store,
            self.keys,
            TrustedTurnContext(
                project_id=PROJECT_ID,
                session_id=RAW_SESSION,
                turn_id=RAW_TURN,
                observed_at="2026-07-21T09:05:00Z",
            ),
            expires_at=EXPIRES_AT,
        )
        with self.assertRaisesRegex(CapabilityRejected, "predates"):
            record_initial_understand(
                self.store,
                self.keys,
                later_capability.token,
                request(0, observed_at="2026-07-21T09:04:59Z"),
                task_family="annotation-core",
            )

        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM semantic_fact_staging"
            ).fetchone()[0],
            0,
        )

    def test_finish_revokes_new_writes_but_identical_finish_retry_is_idempotent(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )
        first = finish_turn(
            self.store,
            self.keys,
            issued.token,
            request(1, key="finish-request", observed_at="2026-07-21T09:01:00Z"),
            finish_payload(),
        )
        retried = finish_turn(
            self.store,
            self.keys,
            issued.token,
            request(1, key="finish-request", observed_at="2026-07-21T10:01:00Z"),
            finish_payload(),
        )
        with self.assertRaisesRegex(CapabilityRejected, "predates"):
            finish_turn(
                self.store,
                self.keys,
                issued.token,
                request(1, key="finish-request", observed_at="2026-07-21T08:59:59Z"),
                finish_payload(),
            )

        receipt = self.store.connection.execute("SELECT retry_count,last_received_at FROM annotation_receipts WHERE sequence=1").fetchone()
        capability = self.store.connection.execute("SELECT revoked_at FROM turn_capabilities").fetchone()
        binding = self.store.connection.execute("SELECT state,finished_at FROM trusted_turn_bindings").fetchone()

        self.assertEqual(first.disposition, AnnotationDisposition.INSERTED)
        self.assertEqual(retried.disposition, AnnotationDisposition.RETRIED)
        self.assertEqual(tuple(receipt), (1, "2026-07-21T10:01:00Z"))
        self.assertEqual(capability["revoked_at"], "2026-07-21T09:01:00Z")
        self.assertEqual(tuple(binding), ("finished", "2026-07-21T09:01:00Z"))
        with self.assertRaises(CapabilityRejected):
            annotate_with_capability(
                self.store, self.keys, issued.token, request(2), phase_payload("docs")
            )

    def test_conflicting_duplicate_sequence_is_diagnosed_without_overwrite(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )
        annotate_with_capability(
            self.store, self.keys, issued.token, request(1, key="first"), phase_payload("implement")
        )

        with self.assertRaises(AnnotationConflict):
            annotate_with_capability(
                self.store, self.keys, issued.token, request(1, key="second"), phase_payload("review")
            )

        row = self.store.connection.execute("SELECT phase,note_redacted FROM annotations WHERE sequence=1").fetchone()
        fact = self.store.connection.execute("SELECT fact_kind FROM semantic_fact_staging").fetchone()
        self.assertEqual(tuple(row), ("implement", "working"))
        self.assertEqual(fact[0], "annotation_sequence_conflict")

    def test_nonidentical_duplicate_must_pass_state_and_expiry_before_conflict_staging(self) -> None:
        expired = issue_capability(
            self.store,
            self.keys,
            turn_context(),
            expires_at="2026-07-21T09:00:30Z",
        )
        record_initial_understand(
            self.store,
            self.keys,
            expired.token,
            request(0, key="expired-request"),
            task_family="annotation-core",
        )
        with self.assertRaises(CapabilityExpired):
            annotate_with_capability(
                self.store,
                self.keys,
                expired.token,
                request(
                    0,
                    key="expired-request",
                    observed_at="2026-07-21T09:00:30Z",
                ),
                phase_payload("research"),
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM semantic_fact_staging"
            ).fetchone()[0],
            0,
        )

        finish_turn(
            self.store,
            self.keys,
            expired.token,
            request(1, key="finish", observed_at="2026-07-21T09:00:20Z"),
            finish_payload(),
        )
        with self.assertRaises(CapabilityRejected):
            finish_turn(
                self.store,
                self.keys,
                expired.token,
                request(1, key="different-finish", observed_at="2026-07-21T09:00:25Z"),
                finish_payload(note="different"),
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM semantic_fact_staging"
            ).fetchone()[0],
            0,
        )

    def test_trusted_timestamp_regression_is_diagnosed_without_corrupting_interval(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )
        annotate_with_capability(
            self.store,
            self.keys,
            issued.token,
            request(1, observed_at="2026-07-21T09:02:00Z"),
            phase_payload(),
        )

        with self.assertRaises(AnnotationConflict):
            annotate_with_capability(
                self.store,
                self.keys,
                issued.token,
                request(2, observed_at="2026-07-21T09:01:59Z"),
                phase_payload("docs"),
            )

        interval = self.store.connection.execute(
            """SELECT start_sequence,ended_at FROM semantic_intervals
                WHERE ended_at IS NULL"""
        ).fetchone()
        fact = self.store.connection.execute(
            "SELECT fact_kind FROM semantic_fact_staging"
        ).fetchone()
        self.assertEqual(tuple(interval), (1, None))
        self.assertEqual(fact[0], "annotation_out_of_order")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0], 2)

    def test_blocker_phase_disagreement_keeps_active_phase_and_stages_conflict(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )
        annotate_with_capability(
            self.store, self.keys, issued.token, request(1), phase_payload("implement")
        )

        annotate_with_capability(
            self.store,
            self.keys,
            issued.token,
            request(2),
            {
                **phase_payload("review"),
                "kind": "blocker",
                "cause": "infra_failure",
            },
        )

        open_phase = self.store.connection.execute(
            "SELECT phase FROM semantic_intervals WHERE ended_at IS NULL"
        ).fetchone()[0]
        fact = self.store.connection.execute(
            "SELECT fact_kind FROM semantic_fact_staging"
        ).fetchone()[0]
        self.assertEqual(open_phase, "implement")
        self.assertEqual(fact, "semantic_conflict")

    def test_model_payload_cannot_supply_trusted_identity_or_timestamp(self) -> None:
        issued = self.issue()
        payload = {**phase_payload(), "turn_id": "model-chosen-turn"}

        with self.assertRaisesRegex(ValueError, "forbidden fields"):
            annotate_with_capability(
                self.store, self.keys, issued.token, request(0), payload
            )

        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0], 0)

    def test_expired_or_unknown_capability_fails_without_echoing_private_input(self) -> None:
        issued = issue_capability(
            self.store,
            self.keys,
            turn_context(),
            expires_at="2026-07-21T09:00:30Z",
        )
        private_note = "token SUPER-PRIVATE-NOTE-123456789"
        private_payload = phase_payload(note=private_note)
        output = io.StringIO()

        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(CapabilityExpired) as expired:
                annotate_with_capability(
                    self.store,
                    self.keys,
                    issued.token,
                    request(0, observed_at="2026-07-21T09:00:30Z"),
                    private_payload,
                )
            with self.assertRaises(CapabilityRejected) as unknown:
                annotate_with_capability(
                    self.store,
                    self.keys,
                    "hcap_v1_" + "z" * 43,
                    request(0),
                    private_payload,
                )

        combined = output.getvalue() + str(expired.exception) + str(unknown.exception)
        for private in (issued.token, private_note, str(self.database)):
            self.assertNotIn(private, combined)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0], 0)

    def test_identical_concurrent_retry_is_serialized_by_immediate_transaction(self) -> None:
        issued = self.issue()
        record_initial_understand(
            self.store, self.keys, issued.token, request(0), task_family="annotation-core"
        )
        ready = Barrier(2)

        def write_once() -> AnnotationDisposition:
            worker = HydraStore(self.database)
            try:
                ready.wait(timeout=2)
                return annotate_with_capability(
                    worker,
                    self.keys,
                    issued.token,
                    request(1, key="concurrent-request"),
                    phase_payload(),
                ).disposition
            finally:
                worker.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(lambda _: write_once(), range(2)), key=lambda item: item.value)

        receipt = self.store.connection.execute("SELECT retry_count FROM annotation_receipts WHERE sequence=1").fetchone()
        self.assertEqual(set(outcomes), {AnnotationDisposition.INSERTED, AnnotationDisposition.RETRIED})
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM annotations WHERE sequence=1").fetchone()[0], 1)
        self.assertEqual(receipt[0], 1)

    def test_database_dump_contains_no_raw_capability_identity_request_or_secret_note(self) -> None:
        issued = self.issue()
        private_request = "private-request-id-should-not-persist"
        private_note = "Authorization: Bearer private-note-secret-value"
        annotate_with_capability(
            self.store,
            self.keys,
            issued.token,
            request(0, key=private_request),
            phase_payload(note=private_note),
        )
        self.store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dump = self.database.read_bytes()

        for private in (issued.token, RAW_SESSION, RAW_TURN, private_request, private_note):
            self.assertNotIn(private.encode(), dump)
        stored = self.store.connection.execute(
            "SELECT note_redacted,note_hash FROM annotations"
        ).fetchone()
        self.assertEqual(stored["note_redacted"], "[redacted]")
        self.assertNotEqual(stored["note_hash"], private_note)


if __name__ == "__main__":
    unittest.main()
