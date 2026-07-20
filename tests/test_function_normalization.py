from __future__ import annotations

import json
import unittest
from pathlib import Path

from hydra_codex.function_normalization import (
    NormalizedFileAccess,
    NormalizedFunctionCall,
    normalize_function_call,
)


class DirectFunctionNormalizationTests(unittest.TestCase):
    def test_exact_hydra_names_are_instrumentation_but_substrings_are_unknown(self) -> None:
        exact_names = (
            "hydra.annotate", "hydra.report", "hydra_annotate", "hydra_report",
            "mcp__hydra__annotate", "mcp__hydra__report",
        )
        for name in exact_names:
            with self.subTest(name=name):
                call = normalize_function_call(name, json.dumps({"path": "src/not-a-file.py"}))
                self.assertEqual(
                    (call.safe_name, call.category, call.instrumentation, call.file_accesses),
                    ("hydra", "instrumentation", True, ()),
                )
                self.assertEqual(call.provenance, "exact")

        for name in ("dehydrate_cache", "hydra_backup"):
            with self.subTest(name=name):
                call = normalize_function_call(name, '{"path":"src/private.py"}')
                self.assertEqual(
                    (call.safe_name, call.category, call.instrumentation, call.file_accesses),
                    ("unknown", "tool", False, ()),
                )
                self.assertEqual((call.provenance, call.terminal_state), ("lower_bound", "unknown"))
                self.assertNotIn(name, repr(call))

        generic = normalize_function_call("mcp__hydra__admin__report", "{}")
        self.assertEqual(
            (generic.safe_name, generic.category, generic.instrumentation),
            ("mcp", "tool", False),
        )

    def test_normalizes_web_mcp_and_exec_without_raw_names_or_arguments(self) -> None:
        cases = (
            ("web__run", {"query": "private-query"}, "web", "web"),
            ("web.run", {"query": "private-query"}, "web", "web"),
            ("mcp__private_server__private_tool", {"secret": "private-value"}, "mcp", "tool"),
        )
        for name, arguments, safe_name, category in cases:
            with self.subTest(name=name):
                call = normalize_function_call(name, json.dumps(arguments))
                self.assertEqual((call.safe_name, call.category), (safe_name, category))
                self.assertFalse(call.instrumentation)
                self.assertEqual(call.file_accesses, ())
                self.assertNotIn("private", repr(call))

        command = normalize_function_call(
            "exec_command", json.dumps({"cmd": "pytest tests/private_test.py"}),
        )
        self.assertEqual((command.safe_name, command.category), ("exec_command", "tool"))
        self.assertEqual(command.ephemeral_command, "pytest tests/private_test.py")
        self.assertNotIn("pytest", repr(command))

    def test_known_read_tools_emit_only_verified_filesystem_reads(self) -> None:
        root = Path("/workspace/project")
        view = normalize_function_call(
            "view_image", json.dumps({"path": "/workspace/project/assets/a.png"}),
            project_root=root,
        )
        file_read = normalize_function_call(
            "file_read", json.dumps({"path": "src/safe.py"}), project_root=root,
        )
        resource = normalize_function_call(
            "read_mcp_resource",
            json.dumps({"server": "private", "uri": "file:///workspace/project/private.txt", "path": "src/not-a-file.py"}),
            project_root=root,
        )

        self.assertEqual(
            [(access.operation, access.relative_path) for access in view.file_accesses],
            [("read", "assets/a.png")],
        )
        self.assertEqual(
            [(access.operation, access.relative_path) for access in file_read.file_accesses],
            [("read", "src/safe.py")],
        )
        self.assertEqual((resource.safe_name, resource.file_accesses), ("read_mcp_resource", ()))
        self.assertNotIn("private", repr(resource))

    def test_write_families_emit_only_verified_project_paths(self) -> None:
        root = Path("/workspace/project")
        patch = normalize_function_call(
            "apply_patch",
            json.dumps({"patch": "*** Update File: src/a.py\n+x\n*** Add File: src/b.py\n+y"}),
            project_root=root,
        )
        write = normalize_function_call(
            "write_file", json.dumps({"path": "/workspace/project/src/c.py", "content": "private"}),
            project_root=root,
        )
        outside = normalize_function_call(
            "file_write", json.dumps({"path": "/private/outside.py", "content": "private"}),
            project_root=root,
        )

        self.assertEqual(
            [(item.operation, item.relative_path) for item in patch.file_accesses],
            [("write", "src/a.py"), ("write", "src/b.py")],
        )
        self.assertEqual(
            [(item.operation, item.relative_path) for item in write.file_accesses],
            [("write", "src/c.py")],
        )
        self.assertEqual((outside.safe_name, outside.file_accesses), ("file_write", ()))
        self.assertNotIn("outside", repr(outside))

    def test_malformed_or_unknown_arguments_never_create_file_facts(self) -> None:
        cases = (
            ("view_image", "not json"),
            ("file_read", json.dumps({"path": "../outside.py"})),
            ("unknown_reader", json.dumps({"path": "src/private.py"})),
            ("apply_patch", json.dumps({"patch": 42})),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                call = normalize_function_call(name, arguments)
                self.assertEqual(call.file_accesses, ())
                self.assertEqual(call.terminal_state, "unknown")
                self.assertNotIn("private", repr(call))

    def test_public_fact_contract_rejects_inconsistent_or_unsafe_values(self) -> None:
        with self.assertRaises(ValueError):
            NormalizedFileAccess("read", "../outside.py")
        with self.assertRaises(ValueError):
            NormalizedFunctionCall("hydra", "tool", False, (), "exact")
        with self.assertRaises(ValueError):
            NormalizedFunctionCall("mcp", "instrumentation", True, (), "exact")
        with self.assertRaises(ValueError):
            NormalizedFunctionCall(
                "unknown", "tool", False,
                (NormalizedFileAccess("read", "src/not-owned.py"),),
                "lower_bound",
            )


if __name__ == "__main__":
    unittest.main()
