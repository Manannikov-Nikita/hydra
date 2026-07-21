"""Parse safe command categories without executing or retaining command text."""

from __future__ import annotations

import re
import shlex
from typing import Iterable


UNKNOWN = ("unknown", "unknown")
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=.*", re.ASCII)


def _unsafe_shell_syntax(command: str) -> bool:
    if "\n" in command or "\r" in command:
        return True
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if character in "*`$": return True
        if quote:
            if character == "\\": index += 2; continue
            if character == quote: quote = None
            index += 1; continue
        if character in "'\"": quote = character; index += 1; continue
        if character == "\\": index += 2; continue
        if character in ";|&<>": return True
        index += 1
    return quote is not None


def _targeted_js(arguments: list[str]) -> bool:
    return any(
        argument in {"--grep", "-t"} or argument.startswith(("--grep=", "--testPathPattern="))
        or "/" in argument or argument.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        for argument in arguments
    )


def _targeted_flags(arguments: list[str], flags: tuple[str, ...]) -> bool:
    prefixes = tuple(prefix for flag in flags for prefix in (flag + "=", flag + ":"))
    return any(argument in flags or argument.startswith(prefixes) for argument in arguments)


def _has_positional(arguments: list[str], value_flags: tuple[str, ...]) -> bool:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in value_flags:
            index += 2
        elif argument.startswith("-"):
            index += 1
        else:
            return True
    return False


def _classify_tokens(tokens: list[str], depth: int) -> tuple[str, str]:
    if not tokens or depth > 2:
        return UNKNOWN
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable == "env":
        index = 1
        while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]):
            index += 1
        return _classify_tokens(tokens[index:], depth + 1)
    if executable == "uv" and len(tokens) >= 3 and tokens[1] == "run":
        index = 3 if tokens[2] == "--" and len(tokens) > 3 else 2
        return _classify_tokens(tokens[index:], depth + 1)
    if executable == "npx":
        index = 1
        while index < len(tokens) and tokens[index] in {"-y", "--yes", "--no-install"}:
            index += 1
        return _classify_tokens(tokens[index:], depth + 1)
    if executable in {"bash", "sh"}:
        if len(tokens) != 3 or tokens[1] != "-c":
            return UNKNOWN
        return _classify_command(tokens[2], depth + 1)

    if (
        re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable)
        and len(tokens) >= 3
        and tokens[1:3] == ["-m", "unittest"]
    ):
        arguments = tokens[3:]
        targeted = _targeted_flags(arguments, ("-k",)) or bool(
            arguments
            and arguments[0] != "discover"
            and _has_positional(arguments, ("-k",))
        )
        return "unittest", "targeted" if targeted else "full"
    if executable == "pytest":
        arguments = tokens[1:]
        targeted = _targeted_flags(arguments, ("-k", "-m", "--keyword", "--mark")) or _has_positional(
            arguments, ("--maxfail", "--junitxml", "--tb", "--capture")
        )
        return "pytest", "targeted" if targeted else "full"
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable) and len(tokens) >= 3 and tokens[1:3] == ["-m", "pytest"]:
        arguments = tokens[3:]
        targeted = _targeted_flags(arguments, ("-k", "-m", "--keyword", "--mark")) or _has_positional(
            arguments, ("--maxfail", "--junitxml", "--tb", "--capture")
        )
        return "pytest", "targeted" if targeted else "full"
    if executable in {"vitest", "jest"}:
        arguments = tokens[2:] if executable == "vitest" and len(tokens) > 1 and tokens[1] == "run" else tokens[1:]
        return executable, "targeted" if _targeted_js(arguments) else "full"
    if executable == "playwright" and len(tokens) >= 2 and tokens[1] == "test":
        return "playwright", "targeted" if _targeted_js(tokens[2:]) else "full"
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        if len(tokens) >= 2 and tokens[1] == "test":
            return executable, "targeted" if _targeted_js(tokens[2:]) else "full"
        if len(tokens) >= 3 and tokens[1] == "run" and (tokens[2] == "test" or tokens[2].startswith("test:")):
            return executable, "targeted" if tokens[2] != "test" or _targeted_js(tokens[3:]) else "full"
        return UNKNOWN
    if executable == "go" and len(tokens) >= 2 and tokens[1] == "test":
        arguments = tokens[2:]
        targeted = _targeted_flags(arguments, ("-run", "-bench", "-list")) or any(
            item.startswith("./") and item != "./..." for item in arguments
        )
        return "go", "targeted" if targeted else "full"
    if executable == "cargo" and len(tokens) >= 2 and tokens[1] == "test":
        arguments = tokens[2:]
        targeted = _targeted_flags(arguments, ("-p", "--package", "--test", "--bench")) or _has_positional(
            arguments, ("--jobs", "--color", "--manifest-path", "--target")
        )
        return "cargo", "targeted" if targeted else "full"
    if executable in {"mvn", "maven"} and "test" in tokens[1:]:
        return "maven", "targeted" if _targeted_flags(tokens[1:], ("-Dtest", "-Dtests", "-pl")) else "full"
    if executable in {"gradle", "gradlew"}:
        tasks = [item for item in tokens[1:] if not item.startswith("-")]
        test_task = next((item for item in tasks if item == "test" or item.endswith(":test")), None)
        if test_task is None:
            return UNKNOWN
        return "gradle", "targeted" if test_task != "test" or _targeted_flags(tokens[1:], ("--tests",)) else "full"
    if executable == "xcodebuild":
        actions = {"test", "test-without-building"}
        if not actions.intersection(tokens[1:]):
            return UNKNOWN
        return "xcode", "targeted" if _targeted_flags(tokens[1:], ("-only-testing", "-skip-testing")) else "full"
    if executable == "swift" and len(tokens) >= 2 and tokens[1] == "test":
        return "swift", "targeted" if _targeted_flags(tokens[2:], ("--filter",)) else "full"
    if executable == "dotnet" and len(tokens) >= 2 and tokens[1] == "test":
        return "dotnet", "targeted" if _targeted_flags(tokens[2:], ("--filter", "--tests")) else "full"
    return UNKNOWN


def _classify_command(command: str, depth: int = 0) -> tuple[str, str]:
    if _unsafe_shell_syntax(command):
        return UNKNOWN
    try:
        return _classify_tokens(shlex.split(command, posix=True), depth)
    except ValueError:
        return UNKNOWN


def classify_test_command(command: str) -> tuple[str, str]:
    """Classify one bounded literal command without shell expansion or execution."""
    return _classify_command(command)


def classify_test_outcome(exit_code: int | None, output: str, previous_hashes: Iterable[str]) -> tuple[str, str]:
    lowered = output.lower()
    if exit_code == 0:
        return ("flaky_retry", "success") if tuple(previous_hashes) else ("unknown", "success")
    if any(marker in lowered for marker in ("sandbox", "network", "econn", "timed out", "permission denied")):
        return "infra_retry", "blocked"
    return ("unknown", "unknown") if exit_code is None else ("product_failure", "failed")
