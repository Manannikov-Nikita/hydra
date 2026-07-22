from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hydra_codex.codex_event_ingest import (
    CodexEventSource,
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


if __name__ == "__main__":
    unittest.main()
