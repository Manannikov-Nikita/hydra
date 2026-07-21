from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import tempfile
from threading import Barrier
import unittest
from unittest import mock

from hydra_codex.cli import main
from hydra_codex.contracts import ModelAnnotationInput
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.services import LocalCommandServices
from hydra_codex.storage import HydraStore
from integrations.codex.hook import handle_event


NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)


def invoke(argv: list[str], *, environ: dict[str, str], stdin: str = ""):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        argv,
        stdin=io.StringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        environ=environ,
    )
    return code, stdout.getvalue(), stderr.getvalue()


class LocalCommandServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_local_services"\n', encoding="utf-8",
        )
        self.database = self.root / "private" / "hydra.sqlite3"
        self.database.parent.mkdir()
        self.key = self.root / "private" / "rollout-hmac.key"
        self.environ = {
            "HOME": str(self.root),
            "HYDRA_DATABASE_PATH": str(self.database),
            "HYDRA_INSTALLATION_KEY_PATH": str(self.key),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _open_turn(self) -> str:
        response = handle_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "private-session",
                "turn_id": "private-turn",
                "cwd": str(self.project),
                "prompt": "never persist this prompt",
            },
            environ=self.environ,
            clock=lambda: NOW,
        )
        instruction = str(response["hookSpecificOutput"]["additionalContext"])
        match = re.search(r"hcap_v1_[A-Za-z0-9_-]{43}", instruction)
        self.assertIsNotNone(match)
        return match.group(0)

    def _annotate(self, capability: str, *, finish: bool = False):
        payload = {
            "kind": "finish" if finish else "phase",
            "phase": "test_full" if finish else "implement",
            "cause": "final_verification" if finish else "plan",
            "scope_change": "none",
            "task_family": "local-services",
            "confidence": 1.0 if finish else 0.9,
            "note": "done" if finish else "implementation",
        }
        if finish:
            payload["outcome"] = "success"
        environment = {**self.environ, "HYDRA_TURN_CAPABILITY": capability}
        return invoke(
            [
                "annotate", "--db", str(self.database),
                "--cwd", str(self.project),
            ],
            environ=environment,
            stdin=json.dumps(payload),
        )

    def _rollout(self, name: str, *, second: int, input_tokens: int) -> Path:
        source = self.root / "rollouts" / f"{name}.jsonl"
        source.parent.mkdir(exist_ok=True)
        records = (
            {
                "timestamp": f"2026-07-21T00:00:{second:02d}Z",
                "type": "session_meta",
                "payload": {"id": name, "cwd": str(self.project)},
            },
            {
                "timestamp": f"2026-07-21T00:00:{second + 1:02d}Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": f"turn-{name}"},
            },
            {
                "timestamp": f"2026-07-21T00:00:{second + 2:02d}Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": 10,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "cache_write_input_tokens": 0,
                        "total_tokens": input_tokens + 20,
                    }},
                },
            },
            {
                "timestamp": f"2026-07-21T00:00:{second + 3:02d}Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": f"turn-{name}"},
            },
        )
        source.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )
        return source

    def test_default_cli_accepts_hook_capability_for_phase_and_finish(self) -> None:
        capability = self._open_turn()

        phase = self._annotate(capability)
        finish = self._annotate(capability, finish=True)

        self.assertEqual(phase, (0, '{"command":"annotate","status":"ok"}\n', ""))
        self.assertEqual(finish, (0, '{"command":"annotate","status":"ok"}\n', ""))
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT sequence,kind,phase,cause,task_family,provenance "
                "FROM annotations ORDER BY sequence"
            ).fetchall()
            binding = store.connection.execute(
                "SELECT state,last_sequence FROM trusted_turn_bindings"
            ).fetchone()
        finally:
            store.close()
        self.assertEqual([tuple(row) for row in rows], [
            (0, "phase", "understand", "prompt", "unclassified", "derived"),
            (1, "phase", "implement", "plan", "local-services", "model_reported"),
            (2, "finish", "test_full", "final_verification", "local-services", "model_reported"),
        ])
        self.assertEqual(tuple(binding), ("finished", 2))
        database = self.database.read_bytes()
        for secret in (capability, "private-session", "private-turn", "never persist this prompt"):
            self.assertNotIn(secret.encode(), database)

    def test_default_cli_rejects_capability_bound_to_another_project(self) -> None:
        capability = self._open_turn()
        foreign = self.root / "foreign"
        (foreign / ".hydra").mkdir(parents=True)
        (foreign / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_foreign"\n', encoding="utf-8",
        )
        environment = {**self.environ, "HYDRA_TURN_CAPABILITY": capability}
        code, stdout, stderr = invoke(
            [
                "annotate", "--db", str(self.database),
                "--cwd", str(foreign),
                "--kind", "phase", "--phase", "implement", "--cause", "plan",
                "--scope-change", "none", "--task-family", "foreign",
                "--confidence", "0.8", "--note", "private foreign note",
            ],
            environ=environment,
        )

        self.assertEqual((code, stdout, stderr), (
            1, "", "hydra-codex: command failed\n",
        ))
        store = HydraStore(self.database)
        try:
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM annotations"
            ).fetchone()[0], 1)
        finally:
            store.close()

    def test_concurrent_phase_writes_allocate_distinct_sequences(self) -> None:
        capability = self._open_turn()
        ready = Barrier(2)

        def write_phase(index: int):
            service = LocalCommandServices(
                environ=self.environ,
                clock=lambda: datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc),
            )
            ready.wait(timeout=2)
            return service.annotate(
                ModelAnnotationInput.from_mapping({
                    "kind": "phase",
                    "phase": "implement" if index == 0 else "test_targeted",
                    "cause": "plan",
                    "scope_change": "none",
                    "task_family": "local-services",
                    "confidence": 0.9,
                    "note": f"phase-{index}",
                }),
                capability,
                self.database,
                self.project,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            writes = tuple(pool.map(write_phase, range(2)))

        self.assertEqual({item.sequence for item in writes}, {1, 2})
        store = HydraStore(self.database)
        try:
            rows = store.connection.execute(
                "SELECT sequence,phase FROM annotations ORDER BY sequence"
            ).fetchall()
        finally:
            store.close()
        self.assertEqual(rows[0]["phase"], "understand")
        self.assertEqual({row["sequence"] for row in rows[1:]}, {1, 2})
        self.assertEqual(
            {row["phase"] for row in rows[1:]}, {"implement", "test_targeted"},
        )

    def test_ingest_uses_same_environment_database_and_key_as_hooks(self) -> None:
        source = self.root / "rollout.jsonl"
        source.write_text(json.dumps({
            "timestamp": "2026-07-21T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "safe-session", "cwd": str(self.project)},
        }) + "\n", encoding="utf-8")
        seen_databases: list[Path | None] = []
        seen_keys: list[Path] = []
        real_store = HydraStore

        def store_factory(path):
            seen_databases.append(path)
            return real_store(self.database)

        def key_loader(path):
            seen_keys.append(path)
            return Pseudonymizer(b"i" * 32)

        with (
            mock.patch("hydra_codex.cli.HydraStore", side_effect=store_factory),
            mock.patch(
                "hydra_codex.cli.Pseudonymizer.installation_key",
                side_effect=key_loader,
            ),
        ):
            result = invoke(
                [
                    "ingest", "--cwd", str(self.project),
                    "--source", f"explicit={source}",
                ],
                environ=self.environ,
            )

        self.assertEqual(result[0], 0)
        self.assertEqual(seen_databases, [self.database])
        self.assertEqual(seen_keys, [self.key])

    def test_annotation_clock_is_read_inside_the_write_transaction(self) -> None:
        capability = self._open_turn()
        stores: list[TrackingStore] = []

        class TrackingStore(HydraStore):
            transaction_active = False

            @contextmanager
            def rollout_transaction(self):
                with super().rollout_transaction() as connection:
                    self.transaction_active = True
                    try:
                        yield connection
                    finally:
                        self.transaction_active = False

        def factory(path):
            store = TrackingStore(path)
            stores.append(store)
            return store

        def clock() -> datetime:
            self.assertTrue(stores)
            self.assertTrue(stores[-1].transaction_active)
            return datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc)

        service = LocalCommandServices(environ=self.environ, clock=clock)
        with mock.patch("hydra_codex.services.HydraStore", side_effect=factory):
            result = service.annotate(
                ModelAnnotationInput.from_mapping({
                    "kind": "phase", "phase": "review", "cause": "plan",
                    "scope_change": "none", "task_family": "local-services",
                    "confidence": 1.0, "note": "transaction ordering",
                }),
                capability,
                self.database,
                self.project,
            )

        self.assertEqual(result.sequence, 1)

    def test_ingest_reconcile_report_and_compare_work_without_injected_services(self) -> None:
        self._rollout("task-a", second=10, input_tokens=100)
        self._rollout("task-b", second=20, input_tokens=180)
        common = ["--db", str(self.database), "--cwd", str(self.project)]

        ingested = invoke(
            [
                "ingest", *common,
                "--source", f"explicit={self.root / 'rollouts'}",
            ],
            environ=self.environ,
        )
        reconciled = invoke(["reconcile", *common], environ=self.environ)
        rendered = invoke(
            ["report", *common, "--last", "2", "--format", "json"],
            environ=self.environ,
        )

        self.assertEqual(ingested[0], 0, ingested[2])
        self.assertEqual(reconciled, (
            0, '{"command":"reconcile","status":"ok"}\n', "",
        ))
        self.assertEqual(rendered[0], 0, rendered[2])
        payload = json.loads(rendered[1])
        self.assertEqual(payload["schema_version"], "hydra.report-list/v1")
        self.assertEqual(len(payload["reports"]), 2)
        self.assertEqual(
            {item["schema_version"] for item in payload["reports"]},
            {"hydra.report/v3"},
        )
        refs = [item["task_ref"] for item in payload["reports"]]
        self.assertTrue(all(re.fullmatch(r"task_[0-9a-f]+", item) for item in refs))
        self.assertEqual(
            {
                item["semantic"]["breakdown"]["marker_count"]["value"]
                for item in payload["reports"]
            },
            {0},
        )

        compared = invoke(
            ["compare", refs[1], refs[0], *common, "--format", "markdown"],
            environ=self.environ,
        )
        self.assertEqual(compared[0], 0, compared[2])
        self.assertIn("# Hydra task comparison", compared[1])
        self.assertIn(refs[0].replace("_", r"\_"), compared[1])
        self.assertNotIn("task-a", compared[1])
        self.assertNotIn("task-b", compared[1])


if __name__ == "__main__":
    unittest.main()
