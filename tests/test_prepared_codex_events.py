from __future__ import annotations

import json
import hmac
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hydra_codex.codex_event_ingest import (
    CodexEventSource,
    ingest_codex_events,
    persist_prepared_codex_event_sources,
)
from hydra_codex.codex_events import APP_SERVER_V2, EventAdapterError
from hydra_codex.prepared_codex_events import (
    EventAttributionDiagnostic,
    PreparedEventAttribution,
    attribute_prepared_codex_event_source,
    prepare_codex_event_source,
    revalidate_prepared_event_attribution,
)
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.rollout_sources import SourceChanged
from hydra_codex.storage import HydraStore


KEY = b"event-adapter-fixture-key-000001"
PROJECT = "trusted-project"


class PreparedCodexEventSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def source(self, *threads: str, name: str = "private-events.jsonl") -> CodexEventSource:
        path = self.root / name
        path.write_text("".join(
            json.dumps({
                "method": "thread/started",
                "params": {"thread": {"id": thread, "createdAt": 1720000000}},
            }) + "\n"
            for thread in threads
        ), encoding="utf-8")
        return CodexEventSource(path, APP_SERVER_V2)

    def test_prepare_reads_one_exact_stream_and_repr_hides_sensitive_fields(self) -> None:
        source = self.source("private-thread")
        expected_thread = Pseudonymizer(KEY).digest("identity", "private-thread")

        with mock.patch(
            "hydra_codex.rollout_sources.os.open", wraps=os.open,
        ) as open_file:
            prepared = prepare_codex_event_source(source, hash_key=KEY)

        self.assertEqual(open_file.call_count, 1)
        self.assertEqual((prepared.line_count, prepared.byte_count), (1, source.path.stat().st_size))
        self.assertEqual(prepared.thread_keys, (expected_thread,))
        self.assertEqual(len(prepared.raw_digest), 64)
        rendered = repr(prepared)
        for private in (
            str(source.path), source.path.name, expected_thread,
            prepared.raw_digest, prepared.location_key, prepared.key_binding,
            KEY.hex(),
        ):
            self.assertNotIn(private, rendered)

    def test_prepare_rejects_a_symlink_without_reading_its_target(self) -> None:
        target = self.source("private-thread", name="target.jsonl")
        link = self.root / "link.jsonl"
        link.symlink_to(target.path)

        with self.assertRaisesRegex(SourceChanged, "changed during ingest"):
            prepare_codex_event_source(
                CodexEventSource(link, APP_SERVER_V2), hash_key=KEY,
            )

    def test_prepare_rejects_invalid_key_before_source_stat_or_read(self) -> None:
        source = self.source("private-thread")

        with (
            mock.patch(
                "hydra_codex.prepared_codex_events.source_stat",
                side_effect=AssertionError("source stat reached"),
            ),
            mock.patch(
                "hydra_codex.rollout_sources.os.open",
                side_effect=AssertionError("source read reached"),
            ),
            self.assertRaisesRegex(EventAdapterError, "exactly 32 bytes"),
        ):
            prepare_codex_event_source(source, hash_key=b"short")


class PreparedCodexEventAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = HydraStore(self.root / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def prepared(self, *threads: str):
        path = self.root / f"events-{len(threads)}.jsonl"
        path.write_text("".join(
            json.dumps({
                "method": "thread/started",
                "params": {"thread": {"id": thread, "createdAt": 1720000000}},
            }) + "\n"
            for thread in threads
        ), encoding="utf-8")
        return prepare_codex_event_source(
            CodexEventSource(path, APP_SERVER_V2), hash_key=KEY,
        )

    def bind(self, raw_thread: str, project_id: str, worktree: str) -> str:
        thread = Pseudonymizer(KEY).digest("identity", raw_thread)
        self.store.connection.execute(
            """INSERT INTO sessions(
                   session_id,project_id,worktree_path,started_at,provenance)
               VALUES (?,?,?,'2026-07-22T00:00:00Z','exact')""",
            (thread, project_id, worktree),
        )
        self.store.connection.execute(
            """INSERT INTO trusted_turn_bindings(
                   turn_key,project_id,session_key,created_at)
               VALUES (?,?,?,'2026-07-22T00:00:01Z')""",
            (f"turn-{raw_thread}", project_id, thread),
        )
        return thread

    def test_exact_all_thread_binding_selects_one_current_rollout_root(self) -> None:
        prepared = self.prepared("first", "second")
        first = self.bind("first", PROJECT, "feature/one")
        second = self.bind("second", PROJECT, "feature/one")
        current_root = self.root / "private-worktree"
        current_root.mkdir()
        current_root = current_root.resolve()

        result = attribute_prepared_codex_event_source(
            self.store.connection,
            prepared,
            {(PROJECT, "feature/one"): (current_root,)},
        )

        self.assertIsInstance(result, PreparedEventAttribution)
        assert isinstance(result, PreparedEventAttribution)
        self.assertEqual((result.project_id, result.worktree_path), (PROJECT, "feature/one"))
        self.assertEqual(result.project_root, current_root)
        rendered = repr(result)
        for private in (PROJECT, "feature/one", str(current_root), first, second):
            self.assertNotIn(private, rendered)

    def test_no_thread_keys_or_partially_bound_set_is_unavailable(self) -> None:
        empty = self.prepared()
        partial = self.prepared("first", "missing")
        self.bind("first", PROJECT, "feature/one")
        roots = {(PROJECT, "feature/one"): (self.root / "worktree",)}

        for prepared in (empty, partial):
            with self.subTest(thread_count=len(prepared.thread_keys)):
                result = attribute_prepared_codex_event_source(
                    self.store.connection, prepared, roots,
                )
                self.assertEqual(
                    result,
                    EventAttributionDiagnostic("event_attribution_unavailable"),
                )

    def test_mixed_binding_or_multiple_current_roots_is_ambiguous(self) -> None:
        mixed = self.prepared("first", "second")
        self.bind("first", PROJECT, "feature/one")
        self.bind("second", "other-project", "feature/two")
        same = self.prepared("third")
        self.bind("third", PROJECT, "feature/one")
        one = self.root / "one"
        two = self.root / "two"
        also_one = self.root / "also-one"
        for root in (one, two, also_one):
            root.mkdir()
        one, two, also_one = one.resolve(), two.resolve(), also_one.resolve()

        mixed_result = attribute_prepared_codex_event_source(
            self.store.connection,
            mixed,
            {
                (PROJECT, "feature/one"): (one,),
                ("other-project", "feature/two"): (two,),
            },
        )
        duplicate_root_result = attribute_prepared_codex_event_source(
            self.store.connection,
            same,
            {(PROJECT, "feature/one"): (one, also_one)},
        )

        self.assertEqual(
            mixed_result,
            EventAttributionDiagnostic("event_attribution_ambiguous"),
        )
        self.assertEqual(
            duplicate_root_result,
            EventAttributionDiagnostic("event_attribution_ambiguous"),
        )

    def test_exact_binding_without_a_current_rollout_root_is_unavailable(self) -> None:
        prepared = self.prepared("first")
        self.bind("first", PROJECT, "feature/one")

        result = attribute_prepared_codex_event_source(
            self.store.connection, prepared, {},
        )

        self.assertEqual(
            result,
            EventAttributionDiagnostic("event_attribution_unavailable"),
        )

    def test_attributed_root_identity_revalidates_and_rejects_replacement(self) -> None:
        prepared = self.prepared("first")
        self.bind("first", PROJECT, "feature/one")
        current_root = self.root / "current-root"
        current_root.mkdir()
        current_root = current_root.resolve()
        result = attribute_prepared_codex_event_source(
            self.store.connection,
            prepared,
            {(PROJECT, "feature/one"): (current_root,)},
        )
        assert isinstance(result, PreparedEventAttribution)

        revalidate_prepared_event_attribution(result)
        moved = current_root.with_name("moved-root")
        current_root.rename(moved)
        current_root.mkdir()

        with self.assertRaisesRegex(SourceChanged, "changed during ingest"):
            revalidate_prepared_event_attribution(result)

    def test_relative_missing_or_symlink_rollout_roots_are_unavailable(self) -> None:
        prepared = self.prepared("first")
        self.bind("first", PROJECT, "feature/one")
        missing = self.root / "missing"
        real = self.root / "real"
        real.mkdir()
        real = real.resolve()
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)

        for candidate in (Path("relative"), missing, linked, str(real)):
            with self.subTest(candidate=candidate):
                result = attribute_prepared_codex_event_source(
                    self.store.connection,
                    prepared,
                    {(PROJECT, "feature/one"): (candidate,)},  # type: ignore[arg-type]
                )
                self.assertEqual(
                    result,
                    EventAttributionDiagnostic("event_attribution_unavailable"),
                )


class PreparedCodexEventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = HydraStore(self.root / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def prepared(self, thread: str, name: str):
        path = self.root / name
        path.write_text(json.dumps({
            "method": "thread/started",
            "params": {"thread": {"id": thread, "createdAt": 1720000000}},
        }) + "\n", encoding="utf-8")
        return prepare_codex_event_source(
            CodexEventSource(path, APP_SERVER_V2), hash_key=KEY,
        )

    def persist(self, *prepared):
        with self.store.rollout_transaction() as connection:
            return persist_prepared_codex_event_sources(
                connection,
                prepared,
                self.root,
                PROJECT,
                hash_key=KEY,
            )

    def test_prepared_persistence_is_idempotent_without_reopening_the_stream(self) -> None:
        prepared = self.prepared("first", "events.jsonl")

        with mock.patch(
            "hydra_codex.rollout_sources.os.open",
            side_effect=AssertionError("prepared persistence reopened source"),
        ):
            first = self.persist(prepared)
            repeated = self.persist(prepared)

        self.assertEqual((first.files_seen, first.unique_sources, first.events), (1, 1, 1))
        self.assertEqual(repeated, first)
        self.assertEqual(self.store.count("codex_event_sources"), 1)
        self.assertEqual(self.store.count("codex_events"), 1)

    def test_prepared_persistence_requires_an_owning_transaction(self) -> None:
        prepared = self.prepared("first", "events.jsonl")

        with self.assertRaisesRegex(ValueError, "owning transaction"):
            persist_prepared_codex_event_sources(
                self.store.connection,
                (prepared,),
                self.root,
                PROJECT,
                hash_key=KEY,
            )

        self.assertEqual(self.store.count("codex_event_sources"), 0)

    def test_mismatched_valid_key_is_rejected_before_stat_or_write_in_both_seams(self) -> None:
        prepared = self.prepared("first", "events.jsonl")
        other_key = b"z" * 32

        for seam in ("optional", "direct"):
            with self.subTest(seam=seam):
                with (
                    mock.patch(
                        "hydra_codex.prepared_codex_events.hmac.compare_digest",
                        wraps=hmac.compare_digest,
                    ) as compare,
                    mock.patch(
                        "hydra_codex.codex_event_ingest.source_stat",
                        side_effect=AssertionError("source stat reached"),
                    ),
                    self.assertRaisesRegex(
                        EventAdapterError, "prepared event source key mismatch",
                    ) as raised,
                ):
                    if seam == "optional":
                        ingest_codex_events(
                            self.store,
                            (),
                            self.root,
                            PROJECT,
                            hash_key=other_key,
                            prepared_sources=(prepared,),
                        )
                    else:
                        with self.store.rollout_transaction() as connection:
                            persist_prepared_codex_event_sources(
                                connection,
                                (prepared,),
                                self.root,
                                PROJECT,
                                hash_key=other_key,
                            )
                compare.assert_called()
                message = str(raised.exception)
                self.assertNotIn(other_key.hex(), message)
                self.assertNotIn(prepared.key_binding, message)
                self.assertEqual(self.store.count("codex_event_sources"), 0)

    def test_invalid_key_length_is_rejected_before_compare_stat_or_write(self) -> None:
        prepared = self.prepared("first", "events.jsonl")
        short_key = b"short"

        for seam in ("optional", "direct"):
            with self.subTest(seam=seam):
                with (
                    mock.patch(
                        "hydra_codex.prepared_codex_events.hmac.compare_digest",
                        side_effect=AssertionError("compare reached"),
                    ),
                    mock.patch(
                        "hydra_codex.codex_event_ingest.source_stat",
                        side_effect=AssertionError("source stat reached"),
                    ),
                    self.assertRaisesRegex(EventAdapterError, "exactly 32 bytes") as raised,
                ):
                    if seam == "optional":
                        ingest_codex_events(
                            self.store,
                            (),
                            self.root,
                            PROJECT,
                            hash_key=short_key,
                            prepared_sources=(prepared,),
                        )
                    else:
                        with self.store.rollout_transaction() as connection:
                            persist_prepared_codex_event_sources(
                                connection,
                                (prepared,),
                                self.root,
                                PROJECT,
                                hash_key=short_key,
                            )
                self.assertNotIn(short_key.hex(), str(raised.exception))
                self.assertEqual(self.store.count("codex_event_sources"), 0)

    def test_legacy_ingest_accepts_optional_prepared_sources_without_reopening(self) -> None:
        prepared = self.prepared("first", "events.jsonl")

        with mock.patch(
            "hydra_codex.rollout_sources.os.open",
            side_effect=AssertionError("optional prepared source was reopened"),
        ):
            report = ingest_codex_events(
                self.store,
                (),
                self.root,
                PROJECT,
                hash_key=KEY,
                prepared_sources=(prepared,),
            )

        self.assertEqual((report.files_seen, report.unique_sources, report.events), (1, 1, 1))
        self.assertEqual(self.store.count("codex_event_sources"), 1)
        self.assertEqual(self.store.count("codex_events"), 1)

    def test_changed_source_rolls_back_the_entire_caller_transaction(self) -> None:
        first = self.prepared("first", "first.jsonl")
        second = self.prepared("second", "second.jsonl")
        second_details = second.path.stat()
        os.utime(
            second.path,
            ns=(second_details.st_atime_ns, second_details.st_mtime_ns + 1_000_000),
        )

        with self.assertRaisesRegex(SourceChanged, "changed during ingest"):
            self.persist(first, second)

        self.assertEqual(self.store.count("codex_event_sources"), 0)
        self.assertEqual(self.store.count("codex_events"), 0)

    def test_after_persistence_stat_swap_rolls_back_written_facts(self) -> None:
        prepared = self.prepared("first", "events.jsonl")
        changed = type(prepared.source_stat)(
            prepared.source_stat.dev,
            prepared.source_stat.ino,
            prepared.source_stat.size,
            prepared.source_stat.mtime_ns + 1,
            prepared.source_stat.ctime_ns,
        )

        with (
            mock.patch(
                "hydra_codex.codex_event_ingest.source_stat",
                side_effect=(prepared.source_stat, changed),
            ),
            self.assertRaisesRegex(SourceChanged, "changed during ingest"),
        ):
            self.persist(prepared)

        self.assertEqual(self.store.count("codex_event_sources"), 0)
        self.assertEqual(self.store.count("codex_events"), 0)

    def test_persistence_failure_rolls_back_all_prepared_sources(self) -> None:
        first = self.prepared("first", "first.jsonl")
        second = self.prepared("second", "second.jsonl")
        from hydra_codex import codex_event_ingest
        original = codex_event_ingest._persist_batch
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("persistence failed")
            return original(*args, **kwargs)

        with (
            mock.patch(
                "hydra_codex.codex_event_ingest._persist_batch",
                side_effect=fail_second,
            ),
            self.assertRaisesRegex(RuntimeError, "persistence failed"),
        ):
            self.persist(first, second)

        self.assertEqual(self.store.count("codex_event_sources"), 0)
        self.assertEqual(self.store.count("codex_events"), 0)

    def test_optional_prepared_ingest_matches_legacy_post_persist_reconciliation(self) -> None:
        source = self.root / "parity.jsonl"
        source.write_text(
            (Path(__file__).parent / "fixtures" / "codex_events" / "app_server_v2.jsonl")
            .read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        legacy = HydraStore(self.root / "legacy.sqlite3")
        prepared_store = HydraStore(self.root / "prepared.sqlite3")
        self.addCleanup(legacy.close)
        self.addCleanup(prepared_store.close)

        ingest_codex_events(
            legacy,
            (CodexEventSource(source, APP_SERVER_V2),),
            self.root,
            PROJECT,
            hash_key=KEY,
        )
        prepared = prepare_codex_event_source(
            CodexEventSource(source, APP_SERVER_V2), hash_key=KEY,
        )
        ingest_codex_events(
            prepared_store,
            (),
            self.root,
            PROJECT,
            hash_key=KEY,
            prepared_sources=(prepared,),
        )
        self.assertGreater(prepared_store.count("turn_attempts"), 0)

        tables = tuple(row[0] for row in legacy.connection.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   AND name != 'schema_migrations'
                 ORDER BY name"""
        ))
        for table in tables:
            with self.subTest(table=table):
                legacy_rows = sorted(
                    (tuple(row) for row in legacy.connection.execute(f'SELECT * FROM "{table}"')),
                    key=repr,
                )
                prepared_rows = sorted(
                    (tuple(row) for row in prepared_store.connection.execute(f'SELECT * FROM "{table}"')),
                    key=repr,
                )
                self.assertEqual(prepared_rows, legacy_rows)


if __name__ == "__main__":
    unittest.main()
