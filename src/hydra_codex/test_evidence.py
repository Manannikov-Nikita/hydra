"""Privacy-safe test evidence and deterministic retry reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Callable

from .classifier import classify_test_command


MODEL_CAUSES = frozenset({
    "prompt", "plan", "test_failure", "review_finding", "user_change",
    "infra_failure", "final_verification", "other",
})
_INFRA_MARKERS = (
    "sandbox", "network", "econn", "timed out", "timeout", "permission denied",
    "connection refused", "could not resolve", "no space left", "resource temporarily unavailable",
)


@dataclass(frozen=True)
class StructuredTestResult:
    exit_status: int | None
    outcome: str
    failure_cause: str
    completeness: str


@dataclass(frozen=True)
class TestIntent:
    evidence_key: str
    source_digest: str
    line_number: int
    session_key: str
    observed_at: str | None
    turn_key: str | None
    tool_call_key: str
    command_hash: str
    runner: str
    scope: str


def parse_structured_result(output: object) -> StructuredTestResult:
    """Accept only a structured object with an exact non-boolean integer exit."""
    parsed: Any = output
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, list):
        candidates: list[dict[str, Any]] = []
        for item in parsed:
            candidate: Any = item
            if isinstance(item, dict) and item.get("type") == "input_text":
                candidate = item.get("text")
            if isinstance(candidate, str):
                try:
                    candidate = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
            if isinstance(candidate, dict) and "exit_code" in candidate:
                candidates.append(candidate)
        if len(candidates) != 1:
            completeness = "conflicted" if len(candidates) > 1 else "result_without_exit"
            return StructuredTestResult(None, "unknown", "unknown", completeness)
        parsed = candidates[0]
    if not isinstance(parsed, dict):
        return StructuredTestResult(None, "unknown", "unknown", "result_without_exit")
    exit_status = parsed.get("exit_code")
    if not isinstance(exit_status, int) or isinstance(exit_status, bool):
        return StructuredTestResult(None, "unknown", "unknown", "result_without_exit")
    if exit_status == 0:
        return StructuredTestResult(0, "success", "none", "complete")
    diagnostic = " ".join(
        value[:4096] for field in ("stdout", "stderr", "message", "output")
        if isinstance((value := parsed.get(field)), str)
    ).lower()
    cause = "infra_failure" if any(marker in diagnostic for marker in _INFRA_MARKERS) else "product_failure"
    return StructuredTestResult(exit_status, "failed", cause, "complete")


def unknown_result(completeness: str = "intent_only") -> StructuredTestResult:
    return StructuredTestResult(None, "unknown", "unknown", completeness)


def persist_test_evidence(
    connection: sqlite3.Connection,
    intent: TestIntent,
    result: StructuredTestResult,
    *,
    observed_at: str | None = None,
) -> None:
    """Upsert one stable evidence item without retaining command or output text."""
    connection.execute(
        """INSERT INTO rollout_test_runs(
               evidence_key, source_digest, line_number, session_key, observed_at, turn_key,
               tool_call_key, command_hash, runner, scope, exit_status, outcome, failure_cause,
               retry_kind, attempt_ordinal, provenance, completeness)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'none', 1, 'derived', ?)
           ON CONFLICT(evidence_key) DO UPDATE SET
             observed_at = CASE WHEN excluded.completeness = 'complete' THEN excluded.observed_at
                                ELSE rollout_test_runs.observed_at END,
             exit_status = CASE WHEN excluded.completeness = 'complete' THEN excluded.exit_status
                                ELSE rollout_test_runs.exit_status END,
             outcome = CASE WHEN excluded.completeness = 'complete' THEN excluded.outcome
                            ELSE rollout_test_runs.outcome END,
             failure_cause = CASE WHEN excluded.completeness = 'complete' THEN excluded.failure_cause
                                  ELSE rollout_test_runs.failure_cause END,
             completeness = CASE WHEN excluded.completeness = 'complete' THEN excluded.completeness
                                 ELSE rollout_test_runs.completeness END""",
        (
            intent.evidence_key, intent.source_digest, intent.line_number, intent.session_key,
            observed_at or intent.observed_at, intent.turn_key, intent.tool_call_key,
            intent.command_hash, intent.runner, intent.scope, result.exit_status,
            result.outcome, result.failure_cause, result.completeness,
        ),
    )


def persist_semantic_conflict(
    connection: sqlite3.Connection,
    intent: TestIntent,
    result: StructuredTestResult,
    model_cause: object,
    conflict_key: str,
) -> None:
    """Record only validated cause enums; deterministic evidence is never changed."""
    safe_model = model_cause if isinstance(model_cause, str) and model_cause in MODEL_CAUSES else "invalid"
    expected = {"product_failure": "test_failure", "infra_failure": "infra_failure"}.get(result.failure_cause)
    if safe_model != "invalid" and (expected is None or safe_model == expected):
        return
    connection.execute(
        """INSERT INTO semantic_conflicts(
               conflict_key, source_digest, line_number, deterministic_cause, model_cause)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        (conflict_key, intent.source_digest, intent.line_number, result.failure_cause, safe_model),
    )


def _timestamp_key(value: str | None, source: str, line: int) -> tuple[int, float, str, int]:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (0, parsed.timestamp(), source, line)
        except ValueError:
            pass
    return (1, 0.0, source, line)


def reconcile_test_retries(connection: sqlite3.Connection) -> None:
    """Recompute attempts globally per session/command from stable evidence."""
    tests = list(connection.execute(
        """SELECT evidence_key, source_digest, line_number, session_key, observed_at,
                  command_hash, outcome, failure_cause
             FROM rollout_test_runs"""
    ))
    writes: dict[str, list[tuple[int, float, str, int]]] = {}
    for row in connection.execute(
        """SELECT session_key, observed_at, source_digest, line_number
             FROM file_observations WHERE operation = 'write'"""
    ):
        writes.setdefault(row[0], []).append(_timestamp_key(row[1], row[2], row[3]))
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in tests:
        groups.setdefault((row[3], row[5]), []).append(row)
    for (session_key, _), rows in groups.items():
        rows.sort(key=lambda row: _timestamp_key(row[4], row[1], row[2]))
        previous: sqlite3.Row | None = None
        previous_key: tuple[int, float, str, int] | None = None
        for ordinal, row in enumerate(rows, start=1):
            retry_kind = "none"
            current_key = _timestamp_key(row[4], row[1], row[2])
            if previous is not None and row[6] == "success":
                if previous[7] == "infra_failure":
                    retry_kind = "infra_recovery"
                elif previous[7] == "product_failure":
                    intervening_write = any(previous_key < item < current_key for item in writes.get(session_key, ()))
                    retry_kind = "product_fix_verification" if intervening_write else "flaky_retry"
                elif previous[6] != "success":
                    retry_kind = "unknown_recovery"
            connection.execute(
                "UPDATE rollout_test_runs SET attempt_ordinal = ?, retry_kind = ? WHERE evidence_key = ?",
                (ordinal, retry_kind, row[0]),
            )
            previous, previous_key = row, current_key


class TestEvidenceBuffer:
    """Join test intent/result records within one source, then reconcile globally."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        source_digest: str,
        model_causes: dict[str, str],
        pseudonymize: Callable[[str, str], str],
    ) -> None:
        self.connection = connection
        self.source_digest = source_digest
        self.model_causes = model_causes
        self.pseudonymize = pseudonymize
        self.intents: dict[str, tuple[TestIntent, str]] = {}
        self.results: dict[str, tuple[StructuredTestResult, str | None]] = {}

    def intent(
        self,
        *,
        logical_call_id: str,
        model_call_id: str,
        command: str,
        session_key: str,
        line_number: int,
        observed_at: str | None,
        turn_key: str | None,
        tool_call_key: str,
    ) -> bool:
        runner, scope = classify_test_command(command)
        if runner == "unknown":
            return False
        command_hash = self.pseudonymize("command", command)
        evidence_key = self.pseudonymize(
            "event", f"test/{session_key}/{logical_call_id}/{command_hash}",
        )
        self.intents[logical_call_id] = (
            TestIntent(
                evidence_key, self.source_digest, line_number, session_key, observed_at,
                turn_key, tool_call_key, command_hash, runner, scope,
            ),
            model_call_id,
        )
        return True

    def result(self, logical_call_id: str, output: object, observed_at: str | None) -> None:
        self.results[logical_call_id] = (parse_structured_result(output), observed_at)

    def flush(self) -> None:
        for logical_call_id, (intent, model_call_id) in self.intents.items():
            result, observed_at = self.results.get(logical_call_id, (unknown_result(), intent.observed_at))
            persist_test_evidence(self.connection, intent, result, observed_at=observed_at)
            model_cause = self.model_causes.get(model_call_id)
            if model_cause is not None:
                persist_semantic_conflict(
                    self.connection, intent, result, model_cause,
                    self.pseudonymize("diagnostic", f"test-conflict/{intent.evidence_key}"),
                )
        reconcile_test_retries(self.connection)
