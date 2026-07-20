"""Parse safe command categories without executing or retaining command text."""

from __future__ import annotations

import re
from typing import Iterable


RUNNERS = (
    (r"\bpytest\b", "pytest"), (r"\bvitest\b", "vitest"), (r"\bjest\b", "jest"),
    (r"\bplaywright\b", "playwright"), (r"\bnpm\b", "npm"), (r"\bpnpm\b", "pnpm"),
    (r"\byarn\b", "yarn"), (r"\bbun\b", "bun"), (r"\bgo\s+test\b", "go"),
    (r"\bcargo\s+test\b", "cargo"), (r"\bmvn\b|\bmaven\b", "maven"),
    (r"\bgradle\b|\bgradlew\b", "gradle"), (r"\bxcodebuild\b", "xcode"),
    (r"\bswift\s+test\b", "swift"), (r"\bdotnet\s+test\b", "dotnet"),
)


def classify_test_command(command: str) -> tuple[str, str]:
    lowered = command.lower()
    runner = next((name for pattern, name in RUNNERS if re.search(pattern, lowered)), "unknown")
    targeted = bool(re.search(r"(?:\btests?/|\.test\.|\.spec\.|::|\s-k\s|\s--filter\s)", lowered))
    return runner, "targeted" if targeted else "full"


def classify_test_outcome(exit_code: int | None, output: str, previous_hashes: Iterable[str]) -> tuple[str, str]:
    lowered = output.lower()
    if exit_code == 0:
        return ("flaky_retry", "success") if tuple(previous_hashes) else ("unknown", "success")
    if any(marker in lowered for marker in ("sandbox", "network", "econn", "timed out", "permission denied")):
        return "infra_retry", "blocked"
    return ("unknown", "unknown") if exit_code is None else ("product_failure", "failed")
