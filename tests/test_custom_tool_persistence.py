from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hydra_codex.rollout import ingest_rollouts
from hydra_codex.storage import HydraStore


def envelope(kind: str, payload: dict, second: int) -> dict:
    return {
        "timestamp": f"2026-07-21T00:00:{second:02d}Z",
        "type": kind,
        "payload": payload,
    }


class CustomToolPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        (self.project / ".hydra").mkdir(parents=True)
        (self.project / ".hydra" / "project.toml").write_text(
            'project_id = "custom-tool-project"\n', encoding="utf-8",
        )
        self.store = HydraStore(self.base / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def ingest(self, rows: list[dict]) -> None:
        source = self.base / "rollout.jsonl"
        source.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        ingest_rollouts(
            self.store, (source,), self.project, "custom-tool-project", hash_key=b"c" * 32,
        )

    def meta(self) -> dict:
        return envelope("session_meta", {"id": "thread", "cwd": str(self.project)}, 0)

    def test_persists_all_safe_nested_families_without_raw_custom_exec_content(self) -> None:
        secret_name = "private_server_name"
        secret_argument = "private_argument_value"
        secret_output = "private_output_value"
        source = f'''
        tools.apply_patch("*** Update File: src/safe.py\\n+safe");
        tools.mcp__{secret_name}__read({{"credential": "{secret_argument}"}});
        tools.web__run({{"search_query": [{{"q": "{secret_argument}"}}]}});
        tools.view_image({{"path": "assets/screenshot.png", "detail": "original"}});
        tools.hydra__annotate({{"note": "{secret_argument}"}});
        tools.exec_command({{"cmd": "pytest tests/test_safe.py"}});
        tools.unknown_{secret_name}({{"value": "{secret_argument}"}});
        '''
        self.ingest([
            self.meta(),
            envelope("response_item", {
                "type": "custom_tool_call", "call_id": "broker", "name": "exec", "input": source,
            }, 1),
            envelope("response_item", {
                "type": "custom_tool_call_output", "call_id": "broker", "output": [
                    {"type": "input_text", "text": f"Script completed {secret_output}"},
                ],
            }, 2),
            envelope("event_msg", {
                "type": "mcp_tool_call_end", "call_id": "canonical-mcp", "result": {"Ok": {}},
            }, 3),
            envelope("event_msg", {
                "type": "mcp_tool_call_end", "call_id": "canonical-mcp", "result": {"Ok": {}},
            }, 4),
        ])

        spans = [tuple(row) for row in self.store.connection.execute(
            "SELECT tool_name,category,provenance FROM tool_spans ORDER BY tool_name,category,provenance"
        )]
        self.assertEqual(spans, sorted([
            ("apply_patch", "tool", "lower_bound"),
            ("custom_exec", "opaque_exec", "exact"),
            ("exec_command", "tool", "lower_bound"),
            ("hydra", "instrumentation", "lower_bound"),
            ("mcp", "tool", "exact"),
            ("mcp", "tool", "lower_bound"),
            ("view_image", "tool", "lower_bound"),
            ("web", "web", "lower_bound"),
        ]))
        self.assertEqual(
            {tuple(row) for row in self.store.connection.execute(
                "SELECT operation,relative_path FROM file_observations"
            )},
            set(),
        )
        self.assertEqual(self.store.count("rollout_test_runs"), 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM test_evidence_candidates "
            "WHERE completeness='intent_only'"
        ).fetchone()[0], 1)
        self.assertIn("custom_exec_unsupported", {
            row[0] for row in self.store.connection.execute(
                "SELECT envelope_kind FROM rollout_diagnostics"
            )
        })
        database_dump = "\n".join(self.store.connection.iterdump())
        for private_value in (secret_name, secret_argument, secret_output, "pytest tests/test_safe.py"):
            self.assertNotIn(private_value, database_dump)

    def test_custom_exec_does_not_persist_unproven_nested_file_facts(self) -> None:
        self.ingest([
            self.meta(),
            envelope("response_item", {
                "type": "custom_tool_call", "call_id": "broker", "name": "exec",
                "input": (
                    'tools.exec_command({"cmd":"cat src/maybe.py"});'
                    'tools.apply_patch("*** Update File: src/maybe.py\\n+maybe");'
                    'tools.view_image({"path":"assets/maybe.png"});'
                ),
            }, 1),
            envelope("response_item", {
                "type": "custom_tool_call_output", "call_id": "broker",
                "output": [{"type": "input_text", "text": "Script completed"}],
            }, 2),
        ])

        self.assertEqual(self.store.count("file_observations"), 0)

    def test_identical_nested_test_commands_remain_two_distinct_attempts(self) -> None:
        command = 'tools.exec_command({"cmd":"pytest tests/test_repeat.py"});'
        self.ingest([
            self.meta(),
            envelope("response_item", {
                "type": "custom_tool_call", "call_id": "broker", "name": "exec",
                "input": command + command,
            }, 1),
        ])

        rows = [tuple(row) for row in self.store.connection.execute(
            """SELECT tool_call_key,command_hash,outcome
                 FROM test_evidence_candidates
                WHERE candidate_kind='evidence'
                ORDER BY evidence_key"""
        )]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual(rows[0][1], rows[1][1])
        self.assertEqual([row[2] for row in rows], ["unknown", "unknown"])

    def test_persists_exact_allowlisted_custom_exec_diagnostic_suffixes(self) -> None:
        programs = (
            'if (enabled) { tools.web__run({"q": "safe"}); }',
            'tools[toolName]({"q": "safe"});',
            'return; tools.web__run({"q": "safe"});',
            'tools.private_tool({"q": "safe"});',
            'tools.web__run({"q": "safe"}',
        )
        rows = [self.meta()]
        rows.extend(
            envelope("response_item", {
                "type": "custom_tool_call", "call_id": f"broker-{index}",
                "name": "exec", "input": program,
            }, index)
            for index, program in enumerate(programs, start=1)
        )
        self.ingest(rows)

        self.assertEqual({
            row[0] for row in self.store.connection.execute(
                "SELECT envelope_kind FROM rollout_diagnostics"
            )
        }, {
            "custom_exec_conditional", "custom_exec_dead_code", "custom_exec_dynamic",
            "custom_exec_unbalanced", "custom_exec_unsupported",
        })

    def test_multiple_diagnostic_suffixes_on_one_source_line_remain_distinct(self) -> None:
        self.ingest([
            self.meta(),
            envelope("response_item", {
                "type": "custom_tool_call", "call_id": "broker", "name": "exec",
                "input": 'tools.private_tool({"q": "safe"}); tools[toolName]({"q": "safe"});',
            }, 1),
        ])

        self.assertEqual({
            row[0] for row in self.store.connection.execute(
                "SELECT envelope_kind FROM rollout_diagnostics"
            )
        }, {"custom_exec_dynamic", "custom_exec_unsupported"})


if __name__ == "__main__":
    unittest.main()
