from __future__ import annotations

import unittest
from pathlib import Path

from hydra_codex.tool_normalization import (
    CUSTOM_EXEC_DIAGNOSTICS,
    NormalizedToolCall,
    normalize_custom_exec,
)
from hydra_codex.rollout_privacy import safe_diagnostic_kind


class CustomToolFactTests(unittest.TestCase):
    def test_normalizes_supported_families_without_retaining_arguments(self) -> None:
        source = r'''
        tools.apply_patch("*** Update File: src/safe.py\n+safe");
        tools.mcp__private_server__read({"credential": "do-not-retain"});
        tools.web__run({"search_query": [{"q": "do-not-retain"}]});
        tools.view_image({"path": "assets/screenshot.png", "detail": "original"});
        tools.hydra__annotate({"note": "do-not-retain"});
        tools.mcp__hydra__report({"format": "json"});
        tools.exec_command({"cmd": "hydra-codex annotate --kind phase"});
        '''

        result = normalize_custom_exec(source)

        self.assertEqual(
            [
                (call.safe_name, call.category, call.instrumentation, call.relative_paths)
                for call in result.calls
            ],
            [
                ("apply_patch", "tool", False, ("src/safe.py",)),
                ("mcp", "tool", False, ()),
                ("web", "web", False, ()),
                ("view_image", "tool", False, ("assets/screenshot.png",)),
                ("hydra", "instrumentation", True, ()),
                ("hydra", "instrumentation", True, ()),
                ("exec_command", "instrumentation", True, ()),
            ],
        )
        rendered = repr(result)
        self.assertNotIn("private_server", rendered)
        self.assertNotIn("credential", rendered)
        self.assertNotIn("do-not-retain", rendered)
        self.assertNotIn("--kind phase", rendered)

    def test_command_is_ephemeral_and_regular_exec_is_not_instrumentation(self) -> None:
        result = normalize_custom_exec(
            'tools.exec_command({"cmd": "pytest tests/test_safe.py"});'
        )

        call = result.calls[0]
        self.assertEqual(
            (call.safe_name, call.category, call.instrumentation),
            ("exec_command", "tool", False),
        )
        self.assertEqual(call.ephemeral_command, "pytest tests/test_safe.py")
        self.assertNotIn("pytest", repr(call))

    def test_normalizes_only_project_relative_paths(self) -> None:
        result = normalize_custom_exec(
            r'''
            tools.view_image({"path": "/workspace/project/assets/inside.png"});
            tools.view_image({"path": "/private/outside.png"});
            tools.apply_patch("*** Update File: ../outside.py\n+unsafe");
            ''',
            project_root=Path("/workspace/project"),
        )

        self.assertEqual(
            [(call.safe_name, call.relative_paths) for call in result.calls],
            [("view_image", ("assets/inside.png",))],
        )
        self.assertEqual(result.diagnostics, ("unsupported", "unsupported"))
        self.assertNotIn("/private/outside.png", repr(result))

    def test_reports_explicit_safe_diagnostics_and_no_fact_for_uncertain_calls(self) -> None:
        cases = {
            "conditional": 'if (enabled) { tools.web__run({"q": "safe"}); }',
            "dead_code": 'return; tools.web__run({"q": "safe"});',
            "dynamic": 'tools[toolName]({"q": "safe"});',
            "unsupported": 'tools.unknown_private_tool({"q": "safe"});',
            "unbalanced": 'tools.web__run({"q": "safe"}',
        }

        for diagnostic, source in cases.items():
            with self.subTest(diagnostic=diagnostic):
                result = normalize_custom_exec(source)
                self.assertEqual(result.calls, ())
                self.assertEqual(result.diagnostics, (diagnostic,))
                self.assertEqual(
                    safe_diagnostic_kind("custom_exec_" + diagnostic),
                    "custom_exec_" + diagnostic,
                )

        spaced = normalize_custom_exec('tools [ "web__run" ]({"q": "safe"});')
        self.assertEqual(spaced.calls, ())
        self.assertEqual(spaced.diagnostics, ("dynamic",))

    def test_dynamic_literal_fields_are_diagnostic_not_facts(self) -> None:
        for source in (
            "tools.exec_command({cmd: command});",
            "tools.view_image({path: imagePath});",
            "tools.apply_patch(patchFromNetwork);",
        ):
            with self.subTest(source=source):
                result = normalize_custom_exec(source)
                self.assertEqual(result.calls, ())
                self.assertEqual(result.diagnostics, ("dynamic",))

    def test_does_not_resolve_fields_or_bindings_from_private_strings(self) -> None:
        result = normalize_custom_exec(r'''
        const privateText = "const patch = '*** Update File: src/leaked.py\\n+leak'";
        tools.apply_patch(patch);
        tools.view_image({"note": "path: '/private/leaked.png'", "path": imagePath});
        ''')

        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics, ("dynamic", "dynamic"))
        self.assertNotIn("leaked", repr(result))

    def test_public_fact_contract_rejects_invalid_safe_combinations(self) -> None:
        with self.assertRaises(ValueError):
            NormalizedToolCall("private_tool", "tool", False, ())
        with self.assertRaises(ValueError):
            NormalizedToolCall("mcp", "instrumentation", False, ())
        with self.assertRaises(ValueError):
            NormalizedToolCall("mcp", "tool", False, ("src/not-owned.py",))

        self.assertEqual(
            CUSTOM_EXEC_DIAGNOSTICS,
            frozenset({"conditional", "dynamic", "dead_code", "unsupported", "unbalanced"}),
        )

    def test_only_exact_hydra_mcp_tools_are_instrumentation(self) -> None:
        result = normalize_custom_exec(r'''
        tools.mcp__hydra__admin__report({"format": "json"});
        tools.mcp__hydra__annotate({"kind": "phase"});
        ''')

        self.assertEqual(
            [(call.safe_name, call.instrumentation) for call in result.calls],
            [("mcp", False), ("hydra", True)],
        )


if __name__ == "__main__":
    unittest.main()
