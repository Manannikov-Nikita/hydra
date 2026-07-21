from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.rollout import Pseudonymizer, ingest_rollouts
from hydra_codex.storage import HydraStore
from hydra_codex.task_tree_storage import aggregate_stored_task_tree
from hydra_codex.tool_normalization import normalize_custom_exec


def exec_program(command: str, *, workdir: str | None = None) -> str:
    arguments: dict[str, str] = {"cmd": command}
    if workdir is not None:
        arguments["workdir"] = workdir
    return f"tools.exec_command({json.dumps(arguments)});"


def event(kind: str, payload: dict[str, object], second: int) -> dict[str, object]:
    return {
        "timestamp": f"2026-07-21T00:00:{second:02d}Z",
        "type": kind,
        "payload": payload,
    }


class HydraCommandClassificationTests(unittest.TestCase):
    def test_exact_project_hook_and_all_installed_cli_commands_are_instrumentation(self) -> None:
        root = Path("/workspace/Hydra Project")
        hook = (
            'env PYTHONPATH="$(git rev-parse --show-toplevel)/src" '
            "HYDRA_TURN_CAPABILITY=opaque-capability "
            "python3.12 -m hydra_codex annotate --kind phase --phase test_targeted"
        )
        commands = (
            hook,
            "hydra-codex annotate --kind finish --outcome success",
            "hydra-codex ingest --cwd .",
            "hydra-codex reconcile --cwd .",
            "hydra-codex report --last 5 --format json",
            "hydra-codex compare htask_aaaa htask_bbbb",
        )

        result = normalize_custom_exec(
            "\n".join(exec_program(command) for command in commands),
            project_root=root,
        )

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), len(commands))
        self.assertTrue(all(call.instrumentation for call in result.calls))
        self.assertEqual({call.category for call in result.calls}, {"instrumentation"})
        self.assertNotIn("opaque-capability", repr(result))
        self.assertNotIn(str(root), repr(result))

    def test_mentions_wrappers_invalid_commands_and_compounds_are_not_instrumentation(self) -> None:
        commands = (
            "echo hydra-codex report",
            "not-hydra-codex report",
            "hydra-codex unknown",
            "python3.12 -m other_module report",
            "python3.12 -m hydra_codex unknown",
            "env PYTHONPATH=/workspace/src python3.12 hydra_codex report",
            "hydra-codex report | tee report.txt",
            "hydra-codex report; hydra-codex ingest",
            "hydra-codex report\ncat private.txt",
        )

        result = normalize_custom_exec("\n".join(exec_program(command) for command in commands))

        self.assertEqual(len(result.calls), len(commands))
        self.assertFalse(any(call.instrumentation for call in result.calls))

    def test_command_field_must_be_one_complete_static_literal(self) -> None:
        programs = (
            'tools.exec_command({"cmd":"hydra-codex report","cmd":"cat private.txt"});',
            'tools.exec_command({"cmd":"hydra-codex report" + suffix});',
            'tools.exec_command({"cmd":"hydra-codex report", ...overrides});',
            'tools.exec_command({[dynamicKey]: "cat private.txt", "cmd":"hydra-codex report"});',
        )

        for program in programs:
            with self.subTest(program=program):
                result = normalize_custom_exec(program)
                self.assertEqual(result.calls, ())
                self.assertEqual(result.diagnostics, ("dynamic",))


class ShellFileObservationTests(unittest.TestCase):
    def test_direct_readers_and_redirection_emit_relative_lower_bound_facts(self) -> None:
        root = Path("/workspace/Hydra Project")
        programs = (
            exec_program('rg -n "needle" "src/alpha file.py"', workdir=str(root)),
            exec_program("sed -n '1,20p' parser.py", workdir=str(root / "src")),
            exec_program("cat -- README.md", workdir=str(root)),
            exec_program("head -n 20 docs/guide.md", workdir=str(root)),
            exec_program("tail -f logs/app.log", workdir=str(root)),
            exec_program("cat src/input.txt > build/output.txt", workdir=str(root)),
            exec_program("printf '%s' generated > generated/result.txt", workdir=str(root)),
        )

        result = normalize_custom_exec("\n".join(programs), project_root=root)

        self.assertEqual(
            [call.file_observations for call in result.calls],
            [
                (("read", "src/alpha file.py"),),
                (("read", "src/parser.py"),),
                (("read", "README.md"),),
                (("read", "docs/guide.md"),),
                (("read", "logs/app.log"),),
                (("read", "src/input.txt"), ("write", "build/output.txt")),
                (("write", "generated/result.txt"),),
            ],
        )
        self.assertTrue(all(call.category == "tool" for call in result.calls))

    def test_flags_patterns_external_paths_and_ambiguous_shell_are_not_paths(self) -> None:
        root = Path("/workspace/project")
        commands = (
            "rg --glob '*.py' needle src",
            "rg needle",
            "sed -n '1p'",
            "head -n 20",
            "cat -",
            "cat *.py",
            'cat "$PRIVATE_FILE"',
            "cat safe.txt | tee copied.txt",
            "cat safe.txt && cat second.txt",
            "cat safe.txt; cat second.txt",
            "cat /private/outside.txt",
            "cat ~/private-home.txt",
            "printf data > ~/private-output.txt",
            "python3.12 -c 'open(\"src/not-observed.py\").read()'",
        )

        result = normalize_custom_exec(
            "\n".join(exec_program(command, workdir=str(root)) for command in commands),
            project_root=root,
        )

        self.assertEqual(result.calls[0].file_observations, (("read", "src"),))
        self.assertTrue(all(not call.file_observations for call in result.calls[1:]))
        self.assertNotIn("PRIVATE_FILE", repr(result))
        self.assertNotIn("/private/outside.txt", repr(result))

    def test_input_and_ambiguous_output_redirections_fail_closed(self) -> None:
        root = Path("/workspace/project")
        commands = (
            "cat < private.txt",
            "cat <> private.txt",
            "cat safe.txt >| private.txt",
            "cat safe.txt >& private.txt",
            "cat safe.txt &> private.txt",
        )

        for command in commands:
            with self.subTest(command=command):
                result = normalize_custom_exec(
                    exec_program(command, workdir=str(root)), project_root=root,
                )
                self.assertEqual(result.calls[0].file_observations, ())

    def test_hash_shell_syntax_fails_closed_instead_of_changing_a_filename(self) -> None:
        root = Path("/workspace/project")
        for command in (
            'cat "safe#name.txt"',
            "cat safe#name.txt",
            "cat safe.txt # cat private-comment.txt",
        ):
            with self.subTest(command=command):
                result = normalize_custom_exec(
                    exec_program(command, workdir=str(root)), project_root=root,
                )
                self.assertEqual(result.calls[0].file_observations, ())

    def test_tail_legacy_offset_is_not_a_file_operand(self) -> None:
        root = Path("/workspace/project")
        offsets = ("+2", "+2b", "+2c", "+2l", "+2f", "+2bf", "+2cf", "+2lf")
        for offset in offsets:
            with self.subTest(offset=offset):
                result = normalize_custom_exec(
                    exec_program(f"tail {offset} src/events.jsonl", workdir=str(root)),
                    project_root=root,
                )
                self.assertEqual(
                    result.calls[0].file_observations,
                    (("read", "src/events.jsonl"),),
                )

        for offset in offsets:
            with self.subTest(explicit_filename=offset):
                explicit = normalize_custom_exec(
                    exec_program(f"tail -- {offset} src/events.jsonl", workdir=str(root)),
                    project_root=root,
                )
                self.assertEqual(
                    explicit.calls[0].file_observations,
                    (("read", offset), ("read", "src/events.jsonl")),
                )

    def test_dynamic_or_duplicate_workdir_suppresses_path_facts(self) -> None:
        root = Path("/workspace/project")
        programs = (
            'tools.exec_command({"cmd":"cat file.py","workdir":base + "/src"});',
            'tools.exec_command({"cmd":"cat file.py","workdir":"/workspace/project/src",'
            '"workdir":"/workspace/project/private"});',
        )

        for program in programs:
            with self.subTest(program=program):
                result = normalize_custom_exec(program, project_root=root)
                self.assertEqual(len(result.calls), 1)
                self.assertEqual(result.calls[0].file_observations, ())

        for workdir in ("~/outside", "$HOME/outside", "src/*", "src\nbad", "C:/outside"):
            with self.subTest(workdir=workdir):
                result = normalize_custom_exec(
                    exec_program("cat target.py", workdir=workdir), project_root=root,
                )
                self.assertEqual(len(result.calls), 1)
                self.assertEqual(result.calls[0].file_observations, ())

        for unsafe_base in ("~/outside", "/private/outside"):
            with self.subTest(absolute_operand_base=unsafe_base):
                absolute = normalize_custom_exec(
                    exec_program(
                        "cat /workspace/project/absolute.py", workdir=unsafe_base,
                    ),
                    project_root=root,
                )
                self.assertEqual(absolute.calls[0].file_observations, ())

    def test_expression_control_flow_never_creates_file_lower_bounds(self) -> None:
        call = exec_program("cat target.py")
        expression_call = call.removesuffix(";")
        programs = (
            f"false && {expression_call};",
            f"true || {expression_call};",
            f"enabled ? {expression_call} : null;",
            f"const deferred = () => {expression_call};",
        )
        for program in programs:
            with self.subTest(program=program):
                result = normalize_custom_exec(program, project_root=Path("/workspace/project"))
                self.assertEqual(result.calls, ())
                self.assertEqual(result.diagnostics, ("conditional",))

        direct = normalize_custom_exec(
            f"const result = await {call}", project_root=Path("/workspace/project"),
        )
        self.assertEqual(
            direct.calls[0].file_observations,
            (("read", "target.py"),),
        )
        self.assertEqual(direct.diagnostics, ())

    def test_throw_marks_dead_code_without_matching_names_members_or_comments(self) -> None:
        call = exec_program("cat target.py")
        dead = normalize_custom_exec(
            f'throw new Error("stop"); {call}',
            project_root=Path("/workspace/project"),
        )
        self.assertEqual(dead.calls, ())
        self.assertEqual(dead.diagnostics, ("dead_code",))

        direct_prefixes = (
            'const word = "throw";',
            "// throw\n",
            "const thrower = 1;",
            "const $throw = 1;",
            "logger.throw();",
            "obj.return();",
        )
        for prefix in direct_prefixes:
            with self.subTest(prefix=prefix):
                result = normalize_custom_exec(
                    f"{prefix} {call}", project_root=Path("/workspace/project"),
                )
                self.assertEqual(len(result.calls), 1)
                self.assertEqual(
                    result.calls[0].file_observations,
                    (("read", "target.py"),),
                )
                self.assertEqual(result.diagnostics, ())

    def test_member_return_is_not_a_return_statement(self) -> None:
        result = normalize_custom_exec(
            f"obj.return(); {exec_program('cat target.py')}",
            project_root=Path("/workspace/project"),
        )

        self.assertEqual(len(result.calls), 1)
        self.assertEqual(
            result.calls[0].file_observations,
            (("read", "target.py"),),
        )
        self.assertEqual(result.diagnostics, ())

    def test_unsafe_static_workdir_never_aborts_rollout_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "unsafe-workdir"\n', encoding="utf-8",
            )
            source = base / "rollout.jsonl"
            rows = (
                event("session_meta", {"id": "session", "cwd": str(project)}, 0),
                event("response_item", {
                    "type": "custom_tool_call", "call_id": "call", "name": "exec",
                    "input": exec_program("cat target.py", workdir="src\nbad"),
                }, 1),
            )
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
            )
            store = HydraStore(base / "hydra.sqlite3")
            self.addCleanup(store.close)

            ingest_rollouts(
                store, (source,), project, "unsafe-workdir", hash_key=b"w" * 32,
            )

            self.assertEqual(
                store.connection.execute("SELECT COUNT(*) FROM file_observations").fetchone()[0],
                0,
            )
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM tool_spans WHERE tool_name='exec_command'"
                ).fetchone()[0],
                1,
            )

    def test_ingestion_is_idempotent_private_and_reports_only_derived_lower_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project with spaces"
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra" / "project.toml").write_text(
                'project_id = "project-shell-files"\n', encoding="utf-8",
            )
            source = base / "rollout.jsonl"
            private_fragment = "do-not-store-private-fragment"
            program = exec_program(
                f'cat "src/input file.py" > "build/{private_fragment}.txt"',
                workdir=str(project),
            )
            rows = (
                event("session_meta", {"id": "session", "cwd": str(project)}, 0),
                event("turn_context", {"turn_id": "turn"}, 1),
                event("response_item", {
                    "type": "custom_tool_call", "call_id": "call", "name": "exec",
                    "input": program,
                }, 2),
                event("event_msg", {"type": "task_complete", "turn_id": "turn"}, 3),
            )
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
            )
            store = HydraStore(base / "hydra.sqlite3")
            self.addCleanup(store.close)

            for _ in range(2):
                ingest_rollouts(
                    store, (source,), project, "project-shell-files", hash_key=b"f" * 32,
                )

            observations = store.connection.execute(
                "SELECT operation,relative_path FROM file_observations ORDER BY operation"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in observations],
                [
                    ("read", "src/input file.py"),
                    ("write", f"build/{private_fragment}.txt"),
                ],
            )
            dump = "\n".join(store.connection.iterdump())
            self.assertNotIn("cat ", dump)
            self.assertNotIn(str(project), dump)
            self.assertNotIn("project with spaces", dump)
            tool = store.connection.execute(
                "SELECT category,provenance FROM tool_spans WHERE tool_name='exec_command'"
            ).fetchone()
            self.assertEqual(tuple(tool), ("tool", "lower_bound"))
            metrics = aggregate_stored_task_tree(
                store.connection,
                project_id="project-shell-files",
                root_id=Pseudonymizer(b"f" * 32).digest("identity", "session"),
            )
            self.assertEqual(
                (metrics.file_reads.value, metrics.file_reads.known_lower_bound),
                (None, 1),
            )
            self.assertEqual(
                (metrics.file_writes.value, metrics.file_writes.known_lower_bound),
                (None, 1),
            )
            self.assertEqual(metrics.file_reads.provenance, "estimated")
            self.assertIn("observed_file_lower_bound", metrics.file_reads.caveats)


if __name__ == "__main__":
    unittest.main()
