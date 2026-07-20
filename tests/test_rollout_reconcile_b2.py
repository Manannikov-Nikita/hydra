from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hydra_codex.metrics import aggregate_project_facts
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.storage import HydraStore


def row(kind: str, payload: dict, timestamp: object) -> dict:
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in rows), encoding="utf-8")


class RolloutReconcileB2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text('project_id = "project-reconcile"\n', encoding="utf-8")
        self.store = HydraStore(self.base / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def meta(self, timestamp: str = "2026-07-21T00:00:00Z") -> dict:
        return row("session_meta", {"id": "thread", "cwd": str(self.project)}, timestamp)

    @staticmethod
    def lifecycle(kind: str, timestamp: object, duration: int | None = None) -> dict:
        payload = {"type": kind, "turn_id": "private-turn-id"}
        if duration is not None:
            payload["duration_ms"] = duration
        return row("event_msg", payload, timestamp)

    @staticmethod
    def token(payload: dict, timestamp: str) -> dict:
        return row("event_msg", {"type": "token_count", "info": {"total_token_usage": payload}}, timestamp)

    def test_repeated_turn_attempts_get_ordinals_and_completed_never_regresses(self) -> None:
        source = self.base / "turns.jsonl"
        write(source, [
            self.meta(),
            self.lifecycle("task_started", "2026-07-21T00:00:01Z"),
            self.lifecycle("task_complete", "2026-07-21T00:00:03Z", 1500),
            self.lifecycle("task_started", "2026-07-21T00:00:04Z"),
            self.lifecycle("turn_aborted", "2026-07-21T00:00:05Z", 500),
            self.lifecycle("task_started", "2026-07-21T00:00:02Z"),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-reconcile", hash_key=b"t" * 32)

        attempts = [tuple(item) for item in self.store.connection.execute(
            "SELECT attempt_ordinal,state,wall_duration_ms,emitted_duration_ms FROM turn_attempts ORDER BY attempt_ordinal"
        )]
        self.assertEqual(attempts, [(1, "completed", 2000, 1500), (2, "aborted", 1000, 500)])
        self.assertNotIn("private-turn-id", "\n".join(self.store.connection.iterdump()))

    def test_invalid_or_inverted_turn_times_never_create_negative_duration(self) -> None:
        source = self.base / "invalid-turn.jsonl"
        write(source, [
            self.meta(),
            self.lifecycle("task_started", "2026-07-21T00:00:05Z"),
            self.lifecycle("task_complete", "not-a-time", 100),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-reconcile", hash_key=b"u" * 32)
        attempt = self.store.connection.execute("SELECT wall_duration_ms FROM turn_attempts").fetchone()
        self.assertIsNone(attempt[0])
        self.assertNotIn("not-a-time", "\n".join(self.store.connection.iterdump()))

    def test_partial_components_do_not_create_false_epochs(self) -> None:
        source = self.base / "tokens.jsonl"
        write(source, [
            self.meta(),
            self.token({"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 3, "cache_write_input_tokens": 5}, "2026-07-21T00:00:01Z"),
            self.token({"input_tokens": 120, "output_tokens": 12}, "2026-07-21T00:00:02Z"),
            self.token({"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 1, "reasoning_output_tokens": 1}, "2026-07-21T00:00:03Z"),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-reconcile", hash_key=b"v" * 32)

        epochs = [row[0] for row in self.store.connection.execute("SELECT epoch FROM token_snapshots ORDER BY observed_at")]
        self.assertEqual(epochs, [0, 0, 1])
        facts = aggregate_project_facts(self.store.connection, "project-reconcile")
        self.assertEqual(facts["recorded_working"].value, 121)

    def test_reconciliation_is_independent_of_source_ingest_order(self) -> None:
        def snapshot(order: tuple[str, str]) -> tuple[list[tuple], list[tuple]]:
            database = self.base / ("-".join(order) + ".sqlite3")
            store = HydraStore(database)
            earlier = self.base / ("earlier-" + order[0]) / "thread-earlier.jsonl"
            later = self.base / ("later-" + order[0]) / "thread-later.jsonl"
            meta_payload = {"id": "thread", "cwd": str(self.project), "timestamp": "2026-07-21T00:00:00Z"}
            write(earlier, [
                row("session_meta", meta_payload, "2026-07-21T00:00:00Z"),
                self.lifecycle("task_started", "2026-07-21T00:00:01Z"),
                self.token({"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 5}, "2026-07-21T00:00:02Z"),
            ])
            write(later, [
                row("session_meta", meta_payload, "2026-07-21T01:00:00Z"),
                self.lifecycle("task_complete", "2026-07-21T01:00:01Z", 60_000),
                self.token({"input_tokens": 120, "cached_input_tokens": 15, "output_tokens": 8}, "2026-07-21T01:00:02Z"),
            ])
            paths = {"earlier": earlier, "later": later}
            try:
                for name in order:
                    ingest_rollouts(store, (paths[name],), self.project, "project-reconcile", hash_key=b"o" * 32)
                tokens = [tuple(item) for item in store.connection.execute(
                    "SELECT epoch,input_tokens,cached_input_tokens,output_tokens FROM token_snapshots ORDER BY observed_at"
                )]
                attempts = [tuple(item) for item in store.connection.execute(
                    "SELECT attempt_ordinal,state,wall_duration_ms,timing_provenance FROM turn_attempts ORDER BY attempt_ordinal"
                )]
                return tokens, attempts
            finally:
                store.close()

        forward = snapshot(("earlier", "later"))
        reverse = snapshot(("later", "earlier"))
        self.assertEqual(forward, reverse)
        self.assertEqual(forward[0], [(0, 100, 10, 5), (0, 120, 15, 8)])
        self.assertEqual(forward[1], [(1, "completed", 3_600_000, "derived")])

    def test_lifecycle_diagnostics_and_timing_provenance_are_explicit(self) -> None:
        source = self.base / "diagnostics.jsonl"
        write(source, [
            self.meta(),
            self.lifecycle("task_started", "2026-07-21T00:00:05Z"),
            self.lifecycle("task_started", "2026-07-21T00:00:06Z"),
            self.lifecycle("task_complete", "2026-07-21T00:00:04Z"),
        ])
        ingest_rollouts(self.store, (source,), self.project, "project-reconcile", hash_key=b"d" * 32)
        kinds = {row[0] for row in self.store.connection.execute(
            "SELECT envelope_kind FROM rollout_diagnostics"
        )}
        self.assertTrue({"duplicate_turn_start", "invalid_turn_interval"}.issubset(kinds))
        provenance = {row[0] for row in self.store.connection.execute(
            "SELECT timing_provenance FROM turn_attempts"
        )}
        self.assertTrue(provenance.issubset({"derived", "estimated"}))


if __name__ == "__main__":
    unittest.main()
