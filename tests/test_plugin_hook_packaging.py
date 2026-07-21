from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagedHookEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".hydra").mkdir()
        (self.root / ".hydra" / "project.toml").write_text(
            'project_id = "hprj_plugin_hook_test"\n', encoding="utf-8",
        )
        self.environment = dict(os.environ)
        self.environment.update({
            "HOME": str(self.root),
            "HYDRA_DATABASE_PATH": str(self.root / "private" / "hydra.sqlite3"),
            "HYDRA_INSTALLATION_KEY_PATH": str(self.root / "private" / "rollout.key"),
            "PYTHONPATH": str(ROOT / "src"),
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, command: list[str], payload: dict[str, object]) -> dict[str, object]:
        completed = subprocess.run(
            command,
            cwd=self.root,
            env=self.environment,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        response = json.loads(completed.stdout)
        self.assertIsInstance(response, dict)
        return response

    def prompt(self, *, session: str, turn: str) -> dict[str, object]:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "turn_id": turn,
            "cwd": str(self.root),
            "prompt": "must not be returned",
        }

    def stop(self, *, session: str, turn: str) -> dict[str, object]:
        return {
            "hook_event_name": "Stop",
            "session_id": session,
            "turn_id": turn,
            "cwd": str(self.root),
            "stop_hook_active": False,
            "last_assistant_message": "must not be returned",
        }

    def assert_prompt_shape(self, response: dict[str, object]) -> str:
        self.assertEqual(set(response), {"hookSpecificOutput"})
        output = response["hookSpecificOutput"]
        self.assertIsInstance(output, dict)
        self.assertEqual(
            set(output), {"hookEventName", "additionalContext"},
        )
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIsInstance(output["additionalContext"], str)
        return output["additionalContext"]

    def assert_stop_shape(self, response: dict[str, object]) -> None:
        self.assertEqual(set(response), {"decision", "reason"})
        self.assertEqual(response["decision"], "block")
        self.assertIn("finish", str(response["reason"]))

    def test_checkout_wrapper_uses_source_tree_annotation_command(self) -> None:
        command = [sys.executable, str(ROOT / "integrations" / "codex" / "hook.py")]

        context = self.assert_prompt_shape(self.invoke(
            command, self.prompt(session="local-session", turn="local-turn"),
        ))
        self.assertIn('PYTHONPATH="$(git rev-parse --show-toplevel)/src"', context)
        self.assertIn("python3.12 -m hydra_codex annotate", context)
        self.assert_stop_shape(self.invoke(
            command, self.stop(session="local-session", turn="local-turn"),
        ))

    def test_packaged_module_uses_installed_annotation_command(self) -> None:
        command = [sys.executable, "-m", "hydra_codex.hook_runtime"]

        context = self.assert_prompt_shape(self.invoke(
            command, self.prompt(session="plugin-session", turn="plugin-turn"),
        ))
        self.assertIn("hydra-codex annotate --kind phase", context)
        self.assertIn("hydra-codex annotate --kind finish", context)
        self.assertIn("`HYDRA_TURN_CAPABILITY=hcap_v1_", context)
        self.assertNotIn("PYTHONPATH", context)
        self.assert_stop_shape(self.invoke(
            command, self.stop(session="plugin-session", turn="plugin-turn"),
        ))

    def test_hook_capability_drives_real_phase_finish_and_nonblocking_stop(self) -> None:
        command = [sys.executable, "-m", "hydra_codex.hook_runtime"]
        context = self.assert_prompt_shape(self.invoke(
            command, self.prompt(session="lifecycle-session", turn="lifecycle-turn"),
        ))
        match = re.search(
            r"HYDRA_TURN_CAPABILITY=([A-Za-z0-9_-]+) hydra-codex annotate",
            context,
        )
        self.assertIsNotNone(match)
        environment = {**self.environment, "HYDRA_TURN_CAPABILITY": match.group(1)}

        common = [
            "--scope-change", "none", "--task-family", "telemetry",
            "--confidence", "1",
        ]
        for semantic in (
            ["--kind", "phase", "--phase", "implement", "--cause", "plan",
             "--note", "implementation", *common],
            ["--kind", "finish", "--phase", "test_full", "--cause", "final_verification",
             "--outcome", "success", "--note", "verified", *common],
        ):
            completed = subprocess.run(
                [sys.executable, "-m", "hydra_codex", "annotate", *semantic],
                cwd=self.root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {
                "command": "annotate", "status": "ok",
            })

        self.assertEqual(self.invoke(
            command, self.stop(session="lifecycle-session", turn="lifecycle-turn"),
        ), {})

    def test_project_hook_owns_events_when_plugin_is_enabled_in_same_checkout(self) -> None:
        (self.root / ".codex").mkdir()
        (self.root / ".codex" / "hooks.json").write_text(
            (ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        local_command = [
            sys.executable, str(ROOT / "integrations" / "codex" / "hook.py"),
        ]
        plugin_command = [sys.executable, "-m", "hydra_codex.hook_runtime"]
        prompt = self.prompt(session="dual-session", turn="dual-turn")
        stop = self.stop(session="dual-session", turn="dual-turn")

        local_prompt = self.invoke(local_command, prompt)
        self.environment["HYDRA_CODEX_HOOK_SOURCE"] = "plugin"
        plugin_prompt = self.invoke(plugin_command, prompt)
        del self.environment["HYDRA_CODEX_HOOK_SOURCE"]
        local_stop = self.invoke(local_command, stop)
        self.environment["HYDRA_CODEX_HOOK_SOURCE"] = "plugin"
        plugin_stop = self.invoke(plugin_command, stop)

        self.assert_prompt_shape(local_prompt)
        self.assertEqual(plugin_prompt, {})
        self.assert_stop_shape(local_stop)
        self.assertEqual(plugin_stop, {})


class PluginHookContractTests(unittest.TestCase):
    def test_console_script_and_plugin_hook_manifest_are_consistent(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            'hydra-codex-hook = "hydra_codex.hook_runtime:main"', pyproject,
        )

        plugin_root = ROOT / "plugins" / "hydra-codex"
        manifest = json.loads(
            (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"),
        )
        self.assertNotIn("hooks", manifest)
        hooks_path = plugin_root / "hooks" / "hooks.json"
        self.assertTrue(hooks_path.is_file())
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(set(hooks["hooks"]), {"UserPromptSubmit", "Stop"})
        for event in ("UserPromptSubmit", "Stop"):
            self.assertEqual(len(hooks["hooks"][event]), 1)
            group = hooks["hooks"][event][0]
            self.assertNotIn("matcher", group)
            self.assertEqual(len(group["hooks"]), 1)
            command = group["hooks"][0]
            self.assertEqual(command["type"], "command")
            self.assertEqual(
                command["command"],
                "env HYDRA_CODEX_HOOK_SOURCE=plugin hydra-codex-hook",
            )
            self.assertLessEqual(command["timeout"], 10)

    def test_plugin_documents_post_pilot_installation_precondition(self) -> None:
        plugin_readme = (
            ROOT / "plugins" / "hydra-codex" / "README.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(plugin_readme.lower().split())
        self.assertIn("post-pilot", normalized)
        self.assertIn("hydra-codex must be installed", normalized)
        self.assertIn("hydra-codex-hook", normalized)
        self.assertIn("hydra.report", normalized)
        self.assertIn("authenticated turn transport", normalized)
        self.assertNotIn("trusted turn transport", normalized)
        self.assertIn("does not advertise `hydra.annotate`", normalized)

        schema = (
            ROOT / "plugins" / "hydra-codex" / "skills" / "hydra-report"
            / "references" / "report-schema.md"
        ).read_text(encoding="utf-8")
        self.assertIn("`hydra.report/v3`", schema)
        self.assertIn("`semantic.annotations`", schema)


if __name__ == "__main__":
    unittest.main()
