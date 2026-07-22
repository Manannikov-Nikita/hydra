from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hydra_codex.codex_event_ingest import (
    CodexEventSource,
    ingest_codex_events,
    persist_prepared_codex_event_sources,
)
from hydra_codex.codex_events import APP_SERVER_V2, AdapterIssue, EventAdapterError
from hydra_codex.prepared_codex_events import prepare_codex_event_source
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.rollout_sources import SourceStat
from hydra_codex.storage import HydraStore


KEY = b"event-adapter-fixture-key-000001"
PROJECT = "trusted-project"


class PreparedCodexEventPayloadSealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = HydraStore(self.root / "hydra.sqlite3")
        source = self.root / "sealed-app-events.jsonl"
        source.write_text(
            (Path(__file__).parent / "fixtures" / "codex_events" / "app_server_v2.jsonl")
            .read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.prepared = prepare_codex_event_source(
            CodexEventSource(source, APP_SERVER_V2), hash_key=KEY,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _thread_mutation(self):
        first = self.prepared.batch.events[0]
        changed = replace(first, thread_key="reviewer-raw-private-thread")
        batch = replace(
            self.prepared.batch,
            events=(changed, *self.prepared.batch.events[1:]),
        )
        keys = tuple(sorted({
            key
            for event in batch.events
            for key in (
                event.thread_key,
                event.parent_thread_key,
                event.child_thread_key,
            )
            if key is not None
        }))
        return replace(self.prepared, batch=batch, thread_keys=keys)

    def _tool_mutation(self):
        event = next(item for item in self.prepared.batch.events if item.tool is not None)
        assert event.tool is not None
        tool = replace(event.tool)
        for name, value in (
            ("_ephemeral_command", event.tool.ephemeral_command),
            ("_ephemeral_output", event.tool.ephemeral_output),
            ("_ephemeral_workdir", event.tool.ephemeral_workdir),
            ("_ephemeral_file_writes", event.tool.ephemeral_file_writes),
        ):
            object.__setattr__(tool, name, value)
        object.__setattr__(tool, "_ephemeral_command", "private mutated command")
        changed = replace(event, tool=tool)
        events = tuple(
            changed if item is event else item
            for item in self.prepared.batch.events
        )
        return replace(self.prepared, batch=replace(self.prepared.batch, events=events))

    def _mutations(self):
        first = self.prepared.batch.events[0]
        status = replace(first, status="failed")
        status_batch = replace(
            self.prepared.batch,
            events=(status, *self.prepared.batch.events[1:]),
        )
        issue_batch = replace(
            self.prepared.batch,
            issues=(*self.prepared.batch.issues, AdapterIssue(
                1, "0" * 64, "malformed_json",
            )),
        )
        alternate = (self.root / "alternate-events.jsonl").resolve()
        location = Pseudonymizer(KEY).digest("path", str(alternate))
        details = self.prepared.source_stat
        changed_stat = SourceStat(
            details.dev, details.ino, details.size,
            details.mtime_ns + 1, details.ctime_ns,
        )
        return {
            "raw-thread": self._thread_mutation(),
            "nonopaque-status": replace(self.prepared, batch=status_batch),
            "tool-ephemeral": self._tool_mutation(),
            "batch-issues": replace(self.prepared, batch=issue_batch),
            "line-count": replace(
                self.prepared, line_count=self.prepared.line_count + 1,
            ),
            "byte-count": replace(
                self.prepared, byte_count=self.prepared.byte_count + 1,
            ),
            "canonical-path": replace(
                self.prepared, path=alternate, location_key=location,
            ),
            "source-stat": replace(self.prepared, source_stat=changed_stat),
            "raw-digest": replace(self.prepared, raw_digest="0" * 64),
        }

    def test_all_consumed_field_mutations_fail_before_stat_or_write(self) -> None:
        for name, tampered in self._mutations().items():
            with self.subTest(name=name):
                with (
                    mock.patch(
                        "hydra_codex.codex_event_ingest.source_stat",
                        side_effect=AssertionError("source stat reached"),
                    ),
                    self.assertRaisesRegex(
                        EventAdapterError, "prepared event source payload mismatch",
                    ) as raised,
                ):
                    with self.store.rollout_transaction() as connection:
                        persist_prepared_codex_event_sources(
                            connection,
                            (tampered,),
                            self.root,
                            PROJECT,
                            hash_key=KEY,
                        )
                self.assertNotIn("reviewer-raw-private-thread", str(raised.exception))
                self.assertNotIn("private mutated command", str(raised.exception))
                self.assertEqual(self.store.count("codex_event_sources"), 0)

    def test_external_mutation_after_validation_cannot_enter_local_snapshot(self) -> None:
        private_thread = "reviewer-raw-private-thread"
        calls = 0

        def mutate_original_then_stat(_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                object.__setattr__(
                    self.prepared.batch.events[0], "thread_key", private_thread,
                )
            return self.prepared.source_stat

        with mock.patch(
            "hydra_codex.codex_event_ingest.source_stat",
            side_effect=mutate_original_then_stat,
        ):
            with self.store.rollout_transaction() as connection:
                report = persist_prepared_codex_event_sources(
                    connection,
                    (self.prepared,),
                    self.root,
                    PROJECT,
                    hash_key=KEY,
                )

        self.assertEqual(report.events, 6)
        self.assertEqual(calls, 2)
        self.assertEqual(self.store.count("codex_event_sources"), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM rollout_sessions WHERE session_key=?",
            (private_thread,),
        ).fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM codex_events WHERE session_key=?",
            (private_thread,),
        ).fetchone()[0], 0)

    def test_mutation_inside_persist_batch_fails_final_seal_and_rolls_back(self) -> None:
        from hydra_codex import codex_event_ingest
        original = codex_event_ingest._persist_batch

        def persist_then_mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            batch = args[2]
            object.__setattr__(batch.events[0], "status", "failed")
            return result

        with (
            mock.patch(
                "hydra_codex.codex_event_ingest._persist_batch",
                side_effect=persist_then_mutate,
            ),
            self.assertRaisesRegex(
                EventAdapterError, "prepared event source payload mismatch",
            ),
        ):
            with self.store.rollout_transaction() as connection:
                persist_prepared_codex_event_sources(
                    connection,
                    (self.prepared,),
                    self.root,
                    PROJECT,
                    hash_key=KEY,
                )

        self.assertEqual(self.store.count("codex_event_sources"), 0)
        self.assertEqual(self.store.count("codex_events"), 0)

    def test_optional_ingest_snapshots_before_external_source_mutation(self) -> None:
        private_thread = "reviewer-raw-private-thread"
        expected_events = len(self.prepared.batch.events)
        calls = 0

        def mutate_original_then_stat(_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                private_event = replace(
                    self.prepared.batch.events[0], thread_key=private_thread,
                )
                object.__setattr__(
                    self.prepared,
                    "batch",
                    replace(
                        self.prepared.batch,
                        events=(*self.prepared.batch.events, private_event),
                    ),
                )
            return self.prepared.source_stat

        with mock.patch(
            "hydra_codex.codex_event_ingest.source_stat",
            side_effect=mutate_original_then_stat,
        ):
            report = ingest_codex_events(
                self.store,
                (),
                self.root,
                PROJECT,
                hash_key=KEY,
                prepared_sources=(self.prepared,),
            )

        self.assertEqual(report.events, expected_events)
        self.assertEqual(calls, 2)
        self.assertEqual(self.store.count("codex_events"), expected_events)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM rollout_sessions WHERE session_key=?",
            (private_thread,),
        ).fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM codex_events WHERE session_key=?",
            (private_thread,),
        ).fetchone()[0], 0)

    def test_optional_ingest_report_ignores_post_persist_snapshot_mutation(self) -> None:
        from hydra_codex import codex_event_ingest
        original = codex_event_ingest.persist_prepared_codex_event_sources
        expected_events = len(self.prepared.batch.events)

        def persist_then_mutate(connection, sources, *args, **kwargs):
            report = original(connection, sources, *args, **kwargs)
            local = tuple(sources)[0]
            object.__setattr__(
                local,
                "batch",
                replace(
                    local.batch,
                    events=(*local.batch.events, local.batch.events[0]),
                ),
            )
            return report

        with mock.patch(
            "hydra_codex.codex_event_ingest.persist_prepared_codex_event_sources",
            side_effect=persist_then_mutate,
        ):
            report = ingest_codex_events(
                self.store,
                (),
                self.root,
                PROJECT,
                hash_key=KEY,
                prepared_sources=(self.prepared,),
            )

        self.assertEqual(report.events, expected_events)
        self.assertEqual(self.store.count("codex_events"), expected_events)


if __name__ == "__main__":
    unittest.main()
