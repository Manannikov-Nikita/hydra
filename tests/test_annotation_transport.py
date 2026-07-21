from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from hydra_codex.cli import main
from hydra_codex.storage import HydraStore
from integrations.codex.hook import handle_event


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
PROJECT_ID = "hprj_annotation_transport"


class AnnotationTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT_ID}"\n', encoding="utf-8",
        )
        self.database = self.root / "private" / "hydra.sqlite3"
        self.database.parent.mkdir()
        self.environ = {
            "HOME": str(self.root),
            "TMPDIR": str(self.root / "tmp"),
            "HYDRA_DATABASE_PATH": str(self.database),
            "HYDRA_INSTALLATION_KEY_PATH": str(self.root / "private" / "rollout.key"),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def spool(self) -> Path:
        return self.root / "tmp" / "Hydra" / "spool"

    def prompt(
        self, session: str = "session-a", turn: str = "turn-a",
        *, now: datetime = NOW,
    ) -> str:
        response = handle_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "turn_id": turn,
                "cwd": str(self.project),
                "prompt": "private prompt",
            },
            environ=self.environ,
            clock=lambda: now,
        )
        match = re.search(
            r"hcap_v1_[A-Za-z0-9_-]{43}",
            str(response["hookSpecificOutput"]["additionalContext"]),
        )
        self.assertIsNotNone(match)
        return match.group(0)

    def stage(
        self, capability: str, *, phase: str = "implement", note: str = "working",
        finish: bool = False,
    ) -> Path:
        arguments = [
            "annotate", "--cwd", str(self.project),
            "--kind", "finish" if finish else "phase",
            "--phase", "test_full" if finish else phase,
            "--cause", "final_verification" if finish else "plan",
            "--scope-change", "none", "--task-family", "telemetry",
            "--confidence", "1", "--note", note,
        ]
        if finish:
            arguments.extend(("--outcome", "success"))
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = main(
            arguments,
            stdin=io.StringIO(),
            stdout=stdout,
            stderr=stderr,
            environ={**self.environ, "HYDRA_TURN_CAPABILITY": capability},
        )
        self.assertEqual((status, stderr.getvalue()), (0, ""))
        self.assertEqual(json.loads(stdout.getvalue())["status"], "ok")
        envelopes = sorted(self.spool.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
        self.assertTrue(envelopes)
        return envelopes[-1]

    def drain(
        self, session: str = "session-a", turn: str = "turn-a",
        *, now: datetime = NOW + timedelta(seconds=2),
    ) -> dict[str, object]:
        return handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session,
                "turn_id": turn,
                "cwd": str(self.project),
                "tool_name": "exec_command",
                "tool_input": "private command",
                "tool_response": "private response",
            },
            environ=self.environ,
            clock=lambda: now,
        )

    def test_atomic_partial_is_ignored_and_parallel_same_cwd_turns_drain_separately(self) -> None:
        capability_a = self.prompt("session-a", "turn-a")
        capability_b = self.prompt("session-b", "turn-b")
        envelope_a = self.stage(capability_a, note="turn-a-phase")
        envelope_b = self.stage(capability_b, phase="test_targeted", note="turn-b-phase")
        partial = self.spool / ".interrupted.tmp"
        partial.write_text('{"capability":', encoding="utf-8")
        partial.chmod(0o600)

        self.assertEqual(self.drain("session-a", "turn-a"), {})
        self.assertFalse(envelope_a.exists())
        self.assertTrue(envelope_b.exists())
        self.assertTrue(partial.exists())
        self.assertEqual(self.drain("session-b", "turn-b"), {})
        self.assertFalse(envelope_b.exists())
        self.assertTrue(partial.exists())

        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT sequence,phase,note_redacted FROM annotations ORDER BY note_redacted"
            ).fetchall()
        finally:
            store.close()
        self.assertEqual(
            [tuple(row) for row in rows if row["sequence"] == 1],
            [(1, "implement", "turn-a-phase"), (1, "test_targeted", "turn-b-phase")],
        )

    def test_malformed_expired_and_unknown_capabilities_are_privately_quarantined(self) -> None:
        capability = self.prompt()
        corrupt = self.spool / "corrupt.json"
        corrupt.write_text('{"raw_prompt":"PRIVATE-CORRUPT-SENTINEL"', encoding="utf-8")
        corrupt.chmod(0o600)
        unknown = self.stage(capability)
        unknown_value = json.loads(unknown.read_text(encoding="utf-8"))
        unknown_value["capability"] = "hcap_v1_" + "z" * 43
        unknown.write_text(json.dumps(unknown_value), encoding="utf-8")
        unknown.chmod(0o600)

        self.assertEqual(self.drain(), {})

        expired = self.stage(capability, phase="review", note="expired")
        self.assertEqual(self.drain(now=NOW + timedelta(hours=25)), {})
        self.assertFalse(expired.exists())
        quarantine = self.spool / "quarantine"
        categories = {path.name.split("-", 1)[0] for path in quarantine.glob("*.json")}
        self.assertEqual(categories, {"malformed", "wrong_capability", "expired"})
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in quarantine.glob("*.json")))
        self.assertEqual(quarantine.stat().st_mode & 0o777, 0o700)

        store = HydraStore(self.database)
        try:
            diagnostics = {
                row[0] for row in store.connection.execute(
                    "SELECT diagnostic_category FROM annotation_transport_events "
                    "WHERE disposition='quarantined'"
                )
            }
            dump = "\n".join(store.connection.iterdump())
        finally:
            store.close()
        self.assertEqual(diagnostics, categories)
        for forbidden in (
            "PRIVATE-CORRUPT-SENTINEL", "private prompt", "private command",
            "private response", capability,
        ):
            self.assertNotIn(forbidden, dump)

    def test_duplicate_is_quarantined_but_crash_after_persist_resumes_idempotently(self) -> None:
        capability = self.prompt()
        envelope = self.stage(capability)
        original = envelope.read_bytes()

        with mock.patch(
            "hydra_codex.annotation_spool._acknowledge",
            side_effect=OSError("simulated unlink failure"),
        ):
            self.assertEqual(self.drain(), {})
        self.assertTrue(envelope.exists())
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=3)), {})
        self.assertFalse(envelope.exists())

        duplicate = self.spool / "duplicate-copy.json"
        duplicate.write_bytes(original)
        duplicate.chmod(0o600)
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=4)), {})
        self.assertFalse(duplicate.exists())
        self.assertEqual(len(tuple((self.spool / "quarantine").glob("duplicate-*.json"))), 1)

        store = HydraStore(self.database)
        try:
            receipt = store.connection.execute(
                "SELECT retry_count FROM annotation_receipts WHERE sequence=1"
            ).fetchone()[0]
            accepted = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events WHERE disposition='accepted'"
            ).fetchone()[0]
            duplicate_diagnostic = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE diagnostic_category='duplicate'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual((receipt, accepted, duplicate_diagnostic), (1, 1, 1))

    def test_accepted_retry_recreated_on_new_inode_is_acknowledged_idempotently(self) -> None:
        capability = self.prompt()
        envelope = self.stage(capability)
        original = envelope.read_bytes()

        with mock.patch(
            "hydra_codex.annotation_spool._acknowledge",
            side_effect=OSError("simulated unlink failure"),
        ):
            self.assertEqual(self.drain(), {})

        displaced = self.root / "accepted-before-ack.json"
        envelope.replace(displaced)
        envelope.write_bytes(original)
        envelope.chmod(0o600)
        self.assertNotEqual(envelope.stat().st_ino, displaced.stat().st_ino)

        self.assertEqual(self.drain(now=NOW + timedelta(seconds=3)), {})
        self.assertFalse(envelope.exists())

        store = HydraStore(self.database)
        try:
            receipt = store.connection.execute(
                "SELECT retry_count FROM annotation_receipts WHERE sequence=1"
            ).fetchone()[0]
            accepted = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE disposition='accepted'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual((receipt, accepted), (1, 1))

    def test_late_older_envelope_is_quarantined_as_out_of_order(self) -> None:
        capability = self.prompt()
        accepted = self.stage(capability)
        accepted_time = (NOW + timedelta(seconds=1)).timestamp()
        os.utime(accepted, (accepted_time, accepted_time))
        self.assertEqual(self.drain(), {})

        late = self.stage(capability, phase="review", note="late-old")
        old_time = (NOW + timedelta(milliseconds=500)).timestamp()
        os.utime(late, (old_time, old_time))
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=3)), {})

        self.assertFalse(late.exists())
        self.assertEqual(len(tuple((self.spool / "quarantine").glob("out_of_order-*.json"))), 1)
        store = HydraStore(self.database)
        try:
            phases = [row[0] for row in store.connection.execute(
                "SELECT phase FROM annotations ORDER BY sequence"
            )]
            diagnostic = store.connection.execute(
                "SELECT diagnostic_category FROM annotation_transport_events "
                "WHERE disposition='quarantined'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(phases, ["understand", "implement"])
        self.assertEqual(diagnostic, "out_of_order")

    def test_database_unavailable_leaves_unacknowledged_envelope_for_later_drain(self) -> None:
        capability = self.prompt()
        envelope = self.stage(capability)
        unavailable = {
            **self.environ,
            "HYDRA_DATABASE_PATH": str(self.root / "unavailable" / "hydra.sqlite3"),
        }

        response = handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-a", "turn_id": "turn-a",
                "cwd": str(self.project),
            },
            environ=unavailable,
            clock=lambda: NOW + timedelta(seconds=2),
        )

        self.assertEqual(response, {})
        self.assertTrue(envelope.exists())
        self.assertFalse((self.root / "unavailable").exists())
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=3)), {})
        self.assertFalse(envelope.exists())

    def test_next_prompt_safety_net_drains_prior_turn_in_same_session(self) -> None:
        capability = self.prompt("session-a", "turn-a")
        envelope = self.stage(capability)

        self.prompt(
            "session-a", "turn-b", now=NOW + timedelta(seconds=2),
        )

        self.assertFalse(envelope.exists())
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                """SELECT trusted_turn_bindings.turn_key,annotations.sequence,
                          annotations.phase
                     FROM annotations JOIN trusted_turn_bindings
                       ON trusted_turn_bindings.turn_key=annotations.turn_id
                    ORDER BY annotations.observed_at,annotations.sequence"""
            ).fetchall()
        finally:
            store.close()
        by_turn: dict[str, list[tuple[int, str]]] = {}
        for row in rows:
            by_turn.setdefault(row["turn_key"], []).append(
                (row["sequence"], row["phase"]),
            )
        self.assertEqual(
            sorted(sorted(values) for values in by_turn.values()),
            [[(0, "understand")], [(0, "understand"), (1, "implement")]],
        )

    def test_drain_never_follows_symlinks_or_accepts_non_private_envelopes(self) -> None:
        capability = self.prompt()
        staged = self.stage(capability)
        content = staged.read_bytes()
        external = self.root / "outside-envelope.json"
        external.write_bytes(content)
        external.chmod(0o600)
        staged.unlink()
        staged.symlink_to(external)
        exposed = self.stage(capability, phase="review", note="exposed")
        exposed.chmod(0o644)

        self.assertEqual(self.drain(), {})

        self.assertEqual(external.read_bytes(), content)
        store = HydraStore(self.database)
        try:
            annotations = store.connection.execute(
                "SELECT COUNT(*) FROM annotations"
            ).fetchone()[0]
            malformed = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE diagnostic_category='malformed'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(annotations, 1)
        self.assertEqual(malformed, 2)
        quarantine = tuple((self.spool / "quarantine").glob("malformed-*.json"))
        self.assertEqual(len(quarantine), 2)
        regular = [path for path in quarantine if not path.is_symlink()]
        self.assertEqual(len(regular), 1)
        self.assertEqual(regular[0].stat().st_mode & 0o777, 0o600)

    def test_acknowledgment_does_not_delete_a_path_replacement(self) -> None:
        capability = self.prompt()
        envelope = self.stage(capability)
        displaced = self.root / "displaced-envelope.json"
        from hydra_codex import annotation_spool

        real_acknowledge = annotation_spool._acknowledge

        def replace_then_acknowledge(path: Path, *identity: object) -> None:
            path.replace(displaced)
            path.write_text('{"replacement":"PRIVATE-REPLACEMENT"}', encoding="utf-8")
            path.chmod(0o600)
            real_acknowledge(path, *identity)

        with mock.patch(
            "hydra_codex.annotation_spool._acknowledge",
            side_effect=replace_then_acknowledge,
        ):
            self.assertEqual(self.drain(), {})

        self.assertTrue(displaced.exists())
        self.assertTrue(envelope.exists())
        self.assertIn("PRIVATE-REPLACEMENT", envelope.read_text(encoding="utf-8"))
        store = HydraStore(self.database)
        try:
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM annotations"
            ).fetchone()[0], 2)
        finally:
            store.close()

    def test_private_filename_is_never_persisted_or_kept_after_quarantine(self) -> None:
        self.prompt()
        sentinel = "PRIVATE-FILENAME-SENTINEL"
        malformed = self.spool / f"{sentinel}.json"
        malformed.write_text('{"malformed":', encoding="utf-8")
        malformed.chmod(0o600)

        self.assertEqual(self.drain(), {})

        store = HydraStore(self.database)
        try:
            dump = "\n".join(store.connection.iterdump())
            rows = store.connection.execute(
                "SELECT staged_order FROM annotation_transport_events "
                "WHERE disposition='quarantined'"
            ).fetchall()
        finally:
            store.close()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(sentinel, dump)
        self.assertTrue(all(sentinel not in path.name for path in self.spool.rglob("*")))

    def test_quarantine_move_failure_does_not_commit_diagnostic(self) -> None:
        self.prompt()
        malformed = self.spool / "move-failure.json"
        malformed.write_text('{"malformed":', encoding="utf-8")
        malformed.chmod(0o600)

        with mock.patch(
            "hydra_codex.annotation_spool._quarantine",
            side_effect=OSError("simulated move failure"),
        ):
            self.assertEqual(self.drain(), {})

        self.assertTrue(malformed.exists())
        store = HydraStore(self.database)
        try:
            quarantined = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE disposition='quarantined'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(quarantined, 0)

    def test_quarantine_claim_recovers_after_database_recording_failure(self) -> None:
        self.prompt()
        malformed = self.spool / "record-failure.json"
        malformed.write_text('{"malformed":', encoding="utf-8")
        malformed.chmod(0o600)
        from hydra_codex import annotation_spool

        real_record = annotation_spool._record_transport
        failed = False

        def fail_first_quarantine(*args: object, **kwargs: object) -> None:
            nonlocal failed
            if kwargs.get("disposition") == "quarantined" and not failed:
                failed = True
                raise RuntimeError("simulated database recording failure")
            real_record(*args, **kwargs)

        with mock.patch(
            "hydra_codex.annotation_spool._record_transport",
            side_effect=fail_first_quarantine,
        ):
            self.assertEqual(self.drain(), {})

        self.assertFalse(malformed.exists())
        claimed = tuple((self.spool / "quarantine").glob("malformed-*.json"))
        self.assertEqual(len(claimed), 1)
        store = HydraStore(self.database)
        try:
            before = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE disposition='quarantined'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(before, 0)

        self.assertEqual(self.drain(now=NOW + timedelta(seconds=3)), {})
        store = HydraStore(self.database)
        try:
            after = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE disposition='quarantined'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(after, 1)

    def test_parallel_drains_create_one_quarantine_claim_and_one_diagnostic(self) -> None:
        self.prompt()
        malformed = self.spool / "parallel-malformed.json"
        malformed.write_text('{"malformed":', encoding="utf-8")
        malformed.chmod(0o600)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = tuple(executor.map(lambda _index: self.drain(), range(2)))

        self.assertEqual(responses, ({}, {}))
        self.assertFalse(malformed.exists())
        self.assertEqual(
            len(tuple((self.spool / "quarantine").glob("malformed-*.json"))), 1,
        )
        store = HydraStore(self.database)
        try:
            quarantined = store.connection.execute(
                "SELECT COUNT(*) FROM annotation_transport_events "
                "WHERE disposition='quarantined'"
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(quarantined, 1)


if __name__ == "__main__":
    unittest.main()
