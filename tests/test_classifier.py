from __future__ import annotations

import unittest

from hydra_codex.classifier import classify_test_command


class TestCommandGrammarTests(unittest.TestCase):
    def test_recognizes_supported_runners_and_runner_aware_scope(self) -> None:
        cases = {
            "pytest": ("pytest", "full"),
            "pytest --maxfail 1": ("pytest", "full"),
            "python -m pytest tests/unit/test_safe.py": ("pytest", "targeted"),
            "pytest -k focused": ("pytest", "targeted"),
            "vitest run": ("vitest", "full"),
            "jest src/safe.test.ts": ("jest", "targeted"),
            "playwright test --grep safe": ("playwright", "targeted"),
            "npm test": ("npm", "full"),
            "pnpm run test:unit": ("pnpm", "targeted"),
            "yarn test tests/safe.spec.ts": ("yarn", "targeted"),
            "bun run test": ("bun", "full"),
            "go test ./...": ("go", "full"),
            "go test ./internal/safe -run TestSafe": ("go", "targeted"),
            "cargo test": ("cargo", "full"),
            "cargo test --jobs 2": ("cargo", "full"),
            "cargo test safe_case": ("cargo", "targeted"),
            "mvn test": ("maven", "full"),
            "mvn -Dtest=SafeTest test": ("maven", "targeted"),
            "gradle test": ("gradle", "full"),
            "./gradlew :module:test": ("gradle", "targeted"),
            "xcodebuild test": ("xcode", "full"),
            "xcodebuild test-without-building -only-testing:AppTests/Safe": ("xcode", "targeted"),
            "swift test": ("swift", "full"),
            "swift test --filter SafeTests": ("swift", "targeted"),
            "dotnet test": ("dotnet", "full"),
            "dotnet test --filter FullyQualifiedName~Safe": ("dotnet", "targeted"),
        }

        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(classify_test_command(command), expected)

    def test_accepts_bounded_safe_wrappers(self) -> None:
        cases = {
            "env CI=1 pytest tests/safe.py": ("pytest", "targeted"),
            "uv run pytest": ("pytest", "full"),
            "npx vitest run --grep safe": ("vitest", "targeted"),
            "bash -c 'python -m pytest tests/safe.py'": ("pytest", "targeted"),
            "sh -c 'go test ./...'": ("go", "full"),
        }

        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(classify_test_command(command), expected)

    def test_rejects_non_test_commands_mentions_expansion_and_shell_composition(self) -> None:
        commands = (
            "npm install", "npm run build", "gradle build", "xcodebuild build", "cargo build", "go vet",
            "rg pytest src", "echo pytest", "pytest && npm test", "pytest | tee result.txt", "pytest; echo done",
            "bash -c 'pytest && npm test'", "pytest tests/*.py", "pytest 'tests/*.py'", "pytest $(cat target)",
            'pytest "$(cat target)"', "echo `pytest`",
        )

        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_test_command(command), ("unknown", "unknown"))
