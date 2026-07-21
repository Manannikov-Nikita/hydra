from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

from hydra_codex.annotation_core import (
    TrustedAnnotationContext,
    finish_turn,
)
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.storage import HydraStore
from integrations.codex.hook import handle_event, run


NOW = datetime(2026, 7, 21, 4, 30, tzinfo=timezone.utc)
PROJECT_ID = "hprj_hook_test"


def prompt_payload(
    *, session_id: str = "session-private-a", turn_id: str = "turn-private-a",
    cwd: str, prompt: str = "secret user prompt",
) -> dict[str, object]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "prompt": prompt,
        "model": "private-model-name",
        "permission_mode": "default",
    }


def stop_payload(
    *, session_id: str = "session-private-a", turn_id: str = "turn-private-a",
    cwd: str, active: bool = False,
) -> dict[str, object]:
    return {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "stop_hook_active": active,
        "last_assistant_message": "private assistant response",
    }


class CodexHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".hydra").mkdir()
        (self.root / ".hydra" / "project.toml").write_text(
            f'project_id = "{PROJECT_ID}"\n', encoding="utf-8",
        )
        self.database = self.root / "private" / "hydra.sqlite3"
        self.database.parent.mkdir()
        self.key_path = self.root / "private" / "rollout-hmac.key"
        self.environ = {
            "HOME": str(self.root),
            "HYDRA_DATABASE_PATH": str(self.database),
            "HYDRA_INSTALLATION_KEY_PATH": str(self.key_path),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def handle(self, payload: dict[str, object]) -> dict[str, object]:
        return handle_event(payload, environ=self.environ, clock=lambda: NOW)

    def capability(self, response: dict[str, object]) -> str:
        self.assertEqual(set(response), {"systemMessage"})
        message = response["systemMessage"]
        self.assertIsInstance(message, str)
        match = re.search(r"hcap_v1_[A-Za-z0-9_-]{43}", message)
        self.assertIsNotNone(match)
        return match.group(0)

    def test_user_prompt_opens_private_turn_and_returns_only_short_model_instruction(self) -> None:
        raw_prompt = "Do not persist this secret prompt: sk-private-example"
        response = self.handle(prompt_payload(cwd=str(self.root), prompt=raw_prompt))

        capability = self.capability(response)
        message = str(response["systemMessage"])
        self.assertLessEqual(len(message), 640)
        self.assertIn("hydra-codex annotate", message)
        self.assertIn("finish", message)
        self.assertNotIn(raw_prompt, message)
        self.assertNotIn("session-private-a", message)
        self.assertNotIn("turn-private-a", message)

        store = HydraStore(self.database)
        try:
            annotation = store.connection.execute(
                "SELECT kind,phase,cause,task_family,provenance,note_redacted "
                "FROM annotations"
            ).fetchone()
            binding = store.connection.execute(
                "SELECT project_id,session_key,turn_key,last_sequence FROM trusted_turn_bindings"
            ).fetchone()
            capability_row = store.connection.execute(
                "SELECT capability_digest FROM turn_capabilities"
            ).fetchone()
        finally:
            store.close()
        self.assertEqual(tuple(annotation), (
            "phase", "understand", "prompt", "unclassified", "derived", "[redacted]",
        ))
        self.assertEqual(binding["project_id"], PROJECT_ID)
        self.assertEqual(binding["last_sequence"], 0)
        self.assertNotEqual(binding["session_key"], "session-private-a")
        self.assertNotEqual(binding["turn_key"], "turn-private-a")
        self.assertNotEqual(capability_row["capability_digest"], capability)
        database_bytes = self.database.read_bytes()
        for forbidden in (
            raw_prompt, "session-private-a", "turn-private-a", str(self.root),
            "private-model-name", capability,
        ):
            self.assertNotIn(forbidden.encode(), database_bytes)

    def test_repeated_prompt_hook_is_idempotent_and_returns_a_fresh_usable_capability(self) -> None:
        first = self.handle(prompt_payload(cwd=str(self.root)))
        second = self.handle(prompt_payload(cwd=str(self.root)))

        self.assertNotEqual(self.capability(first), self.capability(second))
        store = HydraStore(self.database)
        try:
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM annotations"
            ).fetchone()[0], 1)
            self.assertEqual(store.connection.execute(
                "SELECT retry_count FROM annotation_receipts"
            ).fetchone()[0], 1)
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM turn_capabilities"
            ).fetchone()[0], 2)
        finally:
            store.close()

    def test_parallel_chats_in_same_cwd_get_separate_turns(self) -> None:
        payloads = (
            prompt_payload(cwd=str(self.root), session_id="session-a", turn_id="turn-a"),
            prompt_payload(cwd=str(self.root), session_id="session-b", turn_id="turn-b"),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = tuple(executor.map(self.handle, payloads))

        self.assertNotEqual(self.capability(responses[0]), self.capability(responses[1]))
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT session_key,turn_key FROM trusted_turn_bindings ORDER BY session_key"
            ).fetchall()
        finally:
            store.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["session_key"] for row in rows}), 2)
        self.assertEqual(len({row["turn_key"] for row in rows}), 2)

    def test_first_missing_finish_blocks_once_then_records_missing_without_blocking(self) -> None:
        self.handle(prompt_payload(cwd=str(self.root)))

        first = self.handle(stop_payload(cwd=str(self.root)))
        second = self.handle(stop_payload(cwd=str(self.root)))
        third = self.handle(stop_payload(cwd=str(self.root)))

        self.assertEqual(first.get("continue"), False)
        self.assertEqual(set(first), {"continue", "stopReason"})
        self.assertIn("finish", str(first["stopReason"]))
        self.assertEqual(second, {})
        self.assertEqual(third, {})
        store = HydraStore(self.database)
        try:
            facts = store.connection.execute(
                "SELECT fact_kind FROM semantic_fact_staging"
            ).fetchall()
            binding = store.connection.execute(
                "SELECT state FROM trusted_turn_bindings"
            ).fetchone()
        finally:
            store.close()
        self.assertEqual([row["fact_kind"] for row in facts], ["self_report_missing"])
        self.assertEqual(binding["state"], "finished")
        self.assertNotIn(b"private assistant response", self.database.read_bytes())

    def test_existing_finish_allows_stop_without_retry(self) -> None:
        capability = self.capability(self.handle(prompt_payload(cwd=str(self.root))))
        keys = Pseudonymizer.installation_key(self.key_path)
        store = HydraStore(self.database)
        try:
            finish_turn(
                store,
                keys,
                capability,
                TrustedAnnotationContext(
                    request_key="trusted-finish-request",
                    sequence=1,
                    observed_at="2026-07-21T04:31:00Z",
                ),
                {
                    "kind": "finish",
                    "phase": "test_full",
                    "cause": "final_verification",
                    "outcome": "success",
                    "scope_change": "none",
                    "task_family": "codex-hooks",
                    "note": "done",
                    "confidence": 1.0,
                },
            )
        finally:
            store.close()

        self.assertEqual(self.handle(stop_payload(cwd=str(self.root))), {})

    def test_active_stop_hook_returns_immediately_without_touching_storage(self) -> None:
        unavailable = dict(self.environ)
        unavailable["HYDRA_DATABASE_PATH"] = str(self.root / "missing" / "db.sqlite3")

        response = handle_event(
            {"hook_event_name": "Stop", "stop_hook_active": True},
            environ=unavailable,
            clock=lambda: NOW,
        )

        self.assertEqual(response, {})
        self.assertFalse((self.root / "missing").exists())

    def test_malformed_input_missing_project_and_unavailable_database_fail_open(self) -> None:
        unavailable = dict(self.environ)
        unavailable["HYDRA_DATABASE_PATH"] = str(self.root / "missing" / "db.sqlite3")
        cases = (
            None,
            [],
            {},
            {"hook_event_name": "Unexpected"},
            prompt_payload(cwd=str(self.root), session_id="", turn_id="turn"),
            prompt_payload(cwd=str(self.root.parent / f"{self.root.name}-outside")),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    handle_event(payload, environ=self.environ, clock=lambda: NOW), {},
                )
        self.assertEqual(
            handle_event(
                prompt_payload(cwd=str(self.root)), environ=unavailable, clock=lambda: NOW,
            ),
            {},
        )

    def test_invalid_identity_fails_open_before_creating_private_state(self) -> None:
        response = self.handle(
            prompt_payload(cwd=str(self.root), session_id="", turn_id="turn-private"),
        )

        self.assertEqual(response, {})
        self.assertFalse(self.database.exists())
        self.assertFalse(self.key_path.exists())

    def test_runner_always_emits_one_json_value_and_never_leaks_errors_to_stderr(self) -> None:
        cases = (
            "not json",
            json.dumps(prompt_payload(cwd=str(self.root / "absent"))),
            json.dumps(prompt_payload(cwd=str(self.root))),
        )
        for body in cases:
            with self.subTest(body=body):
                stdout = io.StringIO()
                stderr = io.StringIO()
                status = run(
                    stdin=io.StringIO(body), stdout=stdout, stderr=stderr,
                    environ=self.environ, clock=lambda: NOW,
                )
                self.assertEqual(status, 0)
                self.assertIsInstance(json.loads(stdout.getvalue()), dict)
                self.assertEqual(stdout.getvalue().count("\n"), 1)
                self.assertEqual(stderr.getvalue(), "")


class CodexHookManifestTests(unittest.TestCase):
    def test_project_manifest_uses_turn_scoped_no_matcher_hooks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))

        self.assertEqual(set(manifest["hooks"]), {"UserPromptSubmit", "Stop"})
        for event in ("UserPromptSubmit", "Stop"):
            self.assertEqual(len(manifest["hooks"][event]), 1)
            group = manifest["hooks"][event][0]
            self.assertNotIn("matcher", group)
            self.assertEqual(len(group["hooks"]), 1)
            hook = group["hooks"][0]
            self.assertEqual(hook["type"], "command")
            self.assertIn("integrations/codex/hook.py", hook["command"])
            self.assertIn("git rev-parse --show-toplevel", hook["command"])
            self.assertLessEqual(hook["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
