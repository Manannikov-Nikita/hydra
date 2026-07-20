from __future__ import annotations

import unittest

from hydra_codex.tool_normalization import scan_custom_exec


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

