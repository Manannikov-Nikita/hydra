from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from hydra_codex.tool_normalization import ToolSpanJoin, scan_custom_exec, scan_custom_exec_details
from hydra_codex.rollout import ingest_rollouts
from hydra_codex.storage import HydraStore


class CustomExecScannerTests(unittest.TestCase):
    def test_extracts_literal_nested_tool_calls_but_ignores_strings_and_comments(self) -> None:
        source = '''
        // tools.exec_command({cmd: "ignored"})
        const text = "tools.apply_patch({patch: 'ignored'})";
        tools.exec_command({cmd: "pytest tests/test_safe.py", workdir: "src"});
        tools.apply_patch({patch: "*** Update File: src/safe.py\\n+safe"});
        '''

        calls = scan_custom_exec(source)

        self.assertEqual([(call.name, call.command, call.paths) for call in calls], [
            ("exec_command", "pytest tests/test_safe.py", ()),
            ("apply_patch", None, ("src/safe.py",)),
        ])

    def test_const_patch_binding_is_resolved(self) -> None:
        calls = scan_custom_exec('const patch = "*** Update File: src/a.py\\n+x"; text(await tools.apply_patch(patch));')
        self.assertEqual(calls[0].paths, ("src/a.py",))

    def test_scans_quoted_json_keys_escaped_literals_and_freeform_patch_values(self) -> None:
        source = r'''const patch = "*** Update File: src/a.py\n+safe";
        tools.exec_command({"cmd": "pytest \"tests/safe.py\""});
        tools.apply_patch(patch);''' + '\ntools.apply_patch("*** Update File: src/b.py\\n+safe");'

        calls = scan_custom_exec(source)

        self.assertEqual([(call.name, call.command, call.paths) for call in calls], [
            ("exec_command", 'pytest "tests/safe.py"', ()),
            ("apply_patch", None, ("src/a.py",)),
            ("apply_patch", None, ("src/b.py",)),
        ])

    def test_reports_conditional_dead_and_unresolved_calls_without_claiming_them(self) -> None:
        result = scan_custom_exec_details('''
        if (enabled) { tools.exec_command({"cmd": "pytest tests/safe.py"}); }
        return; tools.apply_patch("*** Update File: src/dead.py\\n+safe");
        tools.exec_command({"cmd": command});
        ''')

        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics, ("conditional_or_dead", "conditional_or_dead", "unresolved_arguments"))

    def test_end_only_span_stays_unknown_until_a_structured_result_exists(self) -> None:
        span = ToolSpanJoin().end("call", "2026-07-21T00:00:02Z")
        enriched = ToolSpanJoin().start_after_end(span, "exec_command", "2026-07-21T00:00:01Z")
        self.assertEqual((enriched.terminal_state, enriched.completeness), ("unknown", "incomplete"))
