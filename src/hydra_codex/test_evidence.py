"""Privacy-safe test evidence and deterministic retry reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Callable

from .classifier import classify_test_command
from .source_authority import (
    SOURCE_AUTHORITY,
    rollout_revision_identity,
    source_family,
)


MODEL_CAUSES = frozenset({
    "prompt", "plan", "test_failure", "review_finding", "user_change",
    "infra_failure", "final_verification", "other",
})
_INFRA_MARKERS = (
    "sandbox", "network", "econn", "timed out", "timeout", "permission denied",
    "connection refused", "could not resolve", "no space left", "resource temporarily unavailable",
)
_TEST_CAPABLE_TOOL_NAMES = frozenset({
    "exec_command", "nested_exec", "function", "unknown",
})


@dataclass(frozen=True)
class StructuredTestResult:
    exit_status: int | None
    outcome: str
    failure_cause: str
    completeness: str


@dataclass(frozen=True)
class TestIntent:
    candidate_key: str
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


def _evidence_rank(
    connection: sqlite3.Connection, row: sqlite3.Row,
) -> tuple[int, int, str, int, str]:
    """Rank a complete result first, then deterministic source authority."""
    return (
        int(row[16] == "complete"),
        SOURCE_AUTHORITY[source_family(connection, str(row[3]))],
        str(row[3]),
        int(row[4]),
        str(row[0]),
    )


def _description_rank(
    connection: sqlite3.Connection, row: sqlite3.Row,
) -> tuple[int, str, int, str]:
    """Rank privacy-safe command descriptions by deterministic authority."""
    return (
        SOURCE_AUTHORITY[source_family(connection, str(row[3]))],
        str(row[3]),
        int(row[4]),
        str(row[0]),
    )


def persist_test_evidence(
    connection: sqlite3.Connection,
    intent: TestIntent,
    result: StructuredTestResult,
    *,
    observed_at: str | None = None,
) -> None:
    """Persist an immutable source candidate; materialization happens separately."""
    candidate_kind = "evidence" if intent.runner != "unknown" else "description"
    connection.execute(
        """INSERT INTO test_evidence_candidates(
               candidate_key,candidate_kind,evidence_key,source_digest,line_number,
               session_key,observed_at,turn_key,tool_call_key,command_hash,runner,
               scope,exit_status,outcome,failure_cause,provenance,completeness)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'derived',?)
           ON CONFLICT(candidate_key) DO NOTHING""",
        (
            intent.candidate_key, candidate_kind, intent.evidence_key,
            intent.source_digest,
            intent.line_number, intent.session_key,
            observed_at or intent.observed_at, intent.turn_key, intent.tool_call_key,
            intent.command_hash, intent.runner, intent.scope, result.exit_status,
            result.outcome, result.failure_cause, result.completeness,
        ),
    )


def persist_non_execution(
    connection: sqlite3.Connection, *, candidate_key: str, evidence_key: str,
    source_digest: str, session_key: str, tool_call_key: str,
) -> None:
    """Persist terminal proof that an App command did not execute."""
    connection.execute(
        """INSERT INTO test_evidence_candidates(
               candidate_key,candidate_kind,evidence_key,source_digest,line_number,
               session_key,tool_call_key,command_hash,runner,scope,outcome,
               failure_cause,provenance,completeness)
           VALUES (?,'non_execution',?,?,0,?,?,'','','','unknown','unknown',
                   'exact','non_execution')
           ON CONFLICT(candidate_key) DO NOTHING""",
        (candidate_key, evidence_key, source_digest, session_key, tool_call_key),
    )


def _bound_structural_terminals(
    connection: sqlite3.Connection, matching: list[sqlite3.Row],
    evidence_candidates: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    """Find terminal tool facts proven to describe a matching command fact.

    App Server evidence is retained against the read-only source while tool-span
    candidates use its normalized adapter source.  The adapter's chain digest,
    exact source ordinal, session, and call key jointly bind those two facts.
    A reused call key alone is deliberately insufficient.
    """
    terminals: dict[tuple[str, int], sqlite3.Row] = {}
    for evidence in matching:
        identity_hashes = {
            str(candidate[9])
            for candidate in evidence_candidates
            if (
                str(candidate[3]), int(candidate[4])
            ) == (
                str(evidence[3]), int(evidence[4])
            )
        }
        if identity_hashes != {str(evidence[9])}:
            continue
        for terminal in connection.execute(
            """SELECT terminal.source_digest,terminal.source_ordinal,
                      terminal.finished_at,terminal.turn_key
                 FROM tool_span_candidates AS terminal
                 LEFT JOIN rollout_sources AS adapter
                   ON adapter.source_digest=terminal.source_digest
                WHERE terminal.session_key=? AND terminal.call_key=?
                  AND terminal.candidate_kind='end'
                  AND terminal.tool_name IN (
                      'exec_command','nested_exec','function','unknown'
                  )
                  AND terminal.source_ordinal=?
                  AND (
                      terminal.source_digest=?
                      OR (
                          adapter.source_type='explicit'
                          AND adapter.relation='event_adapter'
                          AND adapter.chain_digest=?
                      )
                  )""",
            (
                str(evidence[5]), str(evidence[8]), int(evidence[4]),
                str(evidence[3]), str(evidence[3]),
            ),
        ):
            terminals[(str(terminal[0]), int(terminal[1]))] = terminal
    return list(terminals.values())


def materialize_test_evidence(
    connection: sqlite3.Connection,
    project_id: str | None = None,
) -> None:
    """Rebuild one canonical test run per session/tool call from immutable facts."""
    project_filter = (
        ""
        if project_id is None
        else """ WHERE session_key IN (
                    SELECT session_key FROM rollout_sessions
                     WHERE project_id=?
                )"""
    )
    parameters: tuple[object, ...] = (
        () if project_id is None else (project_id,)
    )
    rows = list(connection.execute(
        f"""SELECT candidate_key,candidate_kind,evidence_key,source_digest,line_number,
                  session_key,observed_at,turn_key,tool_call_key,command_hash,runner,
                  scope,exit_status,outcome,failure_cause,provenance,completeness
             FROM test_evidence_candidates{project_filter}""",
        parameters,
    ))
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((str(row[5]), str(row[8])), []).append(row)
    desired: dict[str, tuple[object, ...]] = {}
    for (session_key, tool_call_key), candidates in sorted(groups.items()):
        canonical_tool = connection.execute(
            "SELECT tool_name FROM tool_spans WHERE session_key=? AND call_key=?",
            (session_key, tool_call_key),
        ).fetchone()
        if (
            canonical_tool is not None
            and str(canonical_tool[0] or "unknown") not in _TEST_CAPABLE_TOOL_NAMES
        ):
            continue
        descriptions = [
            row for row in candidates if row[1] in {"description", "evidence"}
        ]
        evidence = [row for row in descriptions if row[1] == "evidence"]
        if not descriptions or not evidence:
            continue

        # Command classification is a description fact and therefore follows
        # source authority even when a lower-authority source supplies the only
        # complete result.  Same-authority disagreement means a reused call
        # identity is ambiguous and must not pick a command by digest order.
        authority = max(
            SOURCE_AUTHORITY[source_family(connection, str(row[3]))]
            for row in descriptions
        )
        authoritative = [
            row for row in descriptions
            if SOURCE_AUTHORITY[source_family(connection, str(row[3]))] == authority
        ]
        if len({str(row[9]) for row in authoritative}) != 1:
            continue
        description = max(
            authoritative, key=lambda row: _description_rank(connection, row),
        )
        if description[1] != "evidence":
            continue
        matching = [row for row in evidence if row[9] == description[9]]
        completed = [row for row in matching if row[16] == "complete"]
        terminal = [row for row in matching if row[16] != "intent_only"]
        structural_terminals = _bound_structural_terminals(
            connection, matching, evidence,
        )
        structural_terminal = max(
            structural_terminals,
            key=lambda row: (
                SOURCE_AUTHORITY[source_family(connection, str(row[0]))],
                int(row[1]), str(row[0]),
            ),
        ) if structural_terminals else None
        # Execution is existential but command-bound.  A matching terminal can
        # outlive an earlier cancellation, while a reused call key or another
        # command hash can never manufacture an execution metric.
        if not terminal and structural_terminal is None:
            continue
        selected = max(
            completed or terminal or matching,
            key=lambda row: _evidence_rank(connection, row),
        )
        result = (
            unknown_result("result_without_exit")
            if not terminal and structural_terminal is not None
            else StructuredTestResult(
                selected[12], selected[13], selected[14], selected[16],
            )
        )
        desired[str(description[2])] = (
            description[2],
            structural_terminal[0]
            if not terminal and structural_terminal is not None
            else selected[3],
            structural_terminal[1]
            if not terminal and structural_terminal is not None
            else selected[4],
            session_key,
            (
                structural_terminal[2]
                if not terminal and structural_terminal is not None else selected[6]
            ) or description[6],
            (
                structural_terminal[3]
                if not terminal and structural_terminal is not None else selected[7]
            ) or description[7],
            tool_call_key, description[9], description[10], description[11],
            result.exit_status, result.outcome, result.failure_cause,
            "none", 1, selected[15], result.completeness,
        )

    existing = {
        str(row[0])
        for row in connection.execute(
            f"""SELECT evidence_key
                  FROM rollout_test_runs{project_filter}""",
            parameters,
        )
    }
    connection.executemany(
        "DELETE FROM rollout_test_runs WHERE evidence_key=?",
        ((key,) for key in sorted(existing - desired.keys())),
    )
    connection.executemany(
        """INSERT INTO rollout_test_runs(
               evidence_key,source_digest,line_number,session_key,observed_at,
               turn_key,tool_call_key,command_hash,runner,scope,exit_status,
               outcome,failure_cause,retry_kind,attempt_ordinal,provenance,
               completeness)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(evidence_key) DO UPDATE SET
             source_digest=excluded.source_digest,
             line_number=excluded.line_number,
             session_key=excluded.session_key,
             observed_at=excluded.observed_at,
             turn_key=excluded.turn_key,
             tool_call_key=excluded.tool_call_key,
             command_hash=excluded.command_hash,
             runner=excluded.runner,
             scope=excluded.scope,
             exit_status=excluded.exit_status,
             outcome=excluded.outcome,
             failure_cause=excluded.failure_cause,
             provenance=excluded.provenance,
             completeness=excluded.completeness
           WHERE rollout_test_runs.source_digest IS NOT excluded.source_digest
              OR rollout_test_runs.line_number IS NOT excluded.line_number
              OR rollout_test_runs.session_key IS NOT excluded.session_key
              OR rollout_test_runs.observed_at IS NOT excluded.observed_at
              OR rollout_test_runs.turn_key IS NOT excluded.turn_key
              OR rollout_test_runs.tool_call_key IS NOT excluded.tool_call_key
              OR rollout_test_runs.command_hash IS NOT excluded.command_hash
              OR rollout_test_runs.runner IS NOT excluded.runner
              OR rollout_test_runs.scope IS NOT excluded.scope
              OR rollout_test_runs.exit_status IS NOT excluded.exit_status
              OR rollout_test_runs.outcome IS NOT excluded.outcome
              OR rollout_test_runs.failure_cause IS NOT excluded.failure_cause
              OR rollout_test_runs.provenance IS NOT excluded.provenance
              OR rollout_test_runs.completeness IS NOT excluded.completeness""",
        tuple(desired[key] for key in sorted(desired)),
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


def reconcile_test_retries(
    connection: sqlite3.Connection,
    project_id: str | None = None,
) -> None:
    """Recompute attempts globally per session/command from stable evidence."""
    project_filter = (
        ""
        if project_id is None
        else """ WHERE session_key IN (
                    SELECT session_key FROM rollout_sessions
                     WHERE project_id=?
                )"""
    )
    parameters: tuple[object, ...] = (
        () if project_id is None else (project_id,)
    )
    tests = list(connection.execute(
        f"""SELECT evidence_key, source_digest, line_number, session_key, observed_at,
                  command_hash, outcome, failure_cause
             FROM rollout_test_runs{project_filter}""",
        parameters,
    ))
    writes: dict[str, list[tuple[int, float, str, int]]] = {}
    write_project_filter = (
        ""
        if project_id is None
        else """ AND session_key IN (
                    SELECT session_key FROM rollout_sessions
                     WHERE project_id=?
                )"""
    )
    for row in connection.execute(
        f"""SELECT session_key, observed_at, source_digest, line_number
             FROM file_observations
            WHERE operation = 'write'{write_project_filter}""",
        parameters,
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
                """UPDATE rollout_test_runs
                      SET attempt_ordinal=?,retry_kind=?
                    WHERE evidence_key=?
                      AND (
                          attempt_ordinal IS NOT ?
                          OR retry_kind IS NOT ?
                      )""",
                (ordinal, retry_kind, row[0], ordinal, retry_kind),
            )
            previous, previous_key = row, current_key


def reconcile_test_evidence(connection: sqlite3.Connection) -> None:
    """Rebuild canonical test runs and retry attribution once per ingest batch."""
    materialize_test_evidence(connection)
    reconcile_test_retries(connection)


class TestEvidenceBuffer:
    """Join and persist test intent/result records from one source."""

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
        self.intents: dict[tuple[str, str], tuple[TestIntent, str]] = {}
        self.results: dict[
            tuple[str, str], tuple[StructuredTestResult, str | None]
        ] = {}
        self.rejected: set[tuple[str, str]] = set()

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
        buffer_key = (session_key, logical_call_id)
        # A later command-bearing event is fresh execution evidence for a
        # reused call key and therefore supersedes an earlier cancellation.
        self.rejected.discard(buffer_key)
        runner, scope = classify_test_command(command)
        is_test = runner != "unknown"
        command_hash = self.pseudonymize("command", command)
        evidence_key = self.pseudonymize(
            "event", f"test/{session_key}/{tool_call_key}",
        )
        candidate_key = self.pseudonymize(
            "event",
            f"test-candidate/evidence/{session_key}/{tool_call_key}/"
            f"{self.source_digest}/{line_number}/{command_hash}",
        )
        current = self.intents.get(buffer_key)
        if current is not None:
            self._persist_current(buffer_key)
        self.results.pop(buffer_key, None)
        self.intents[buffer_key] = (
            TestIntent(
                candidate_key, evidence_key, self.source_digest, line_number,
                session_key, observed_at, turn_key, tool_call_key, command_hash,
                runner, scope,
            ),
            model_call_id,
        )
        return is_test

    def _restore_intent(
        self, *, logical_call_id: str, session_key: str, line_number: int,
        observed_at: str | None, turn_key: str | None, tool_call_key: str,
    ) -> None:
        """Restore only hashed intent fields when an append contains the terminal line."""
        rows = list(self.connection.execute(
            """SELECT candidate_key,candidate_kind,evidence_key,source_digest,line_number,
                      session_key,observed_at,turn_key,tool_call_key,command_hash,runner,
                      scope,exit_status,outcome,failure_cause,provenance,completeness
                 FROM test_evidence_candidates
                WHERE session_key=? AND tool_call_key=?
                  AND candidate_kind IN ('description','evidence')""",
            (session_key, tool_call_key),
        ))
        current_revision = rollout_revision_identity(
            self.connection, self.source_digest,
        )
        rows = [
            row for row in rows
            if str(row[3]) == self.source_digest
            or (
                current_revision is not None
                and (
                    candidate_revision := rollout_revision_identity(
                        self.connection, str(row[3]),
                    )
                ) is not None
                and candidate_revision[0] == current_revision[0]
            )
        ]
        if not rows:
            return
        description = max(rows, key=lambda row: _description_rank(self.connection, row))
        command_hash = str(description[9])
        candidate_key = self.pseudonymize(
            "event",
            f"test-candidate/evidence/{session_key}/{tool_call_key}/"
            f"{self.source_digest}/{line_number}/{command_hash}",
        )
        self.intents[(session_key, logical_call_id)] = (
            TestIntent(
                candidate_key, str(description[2]), self.source_digest, line_number,
                session_key, observed_at or description[6], turn_key or description[7],
                tool_call_key, command_hash, str(description[10]), str(description[11]),
            ),
            logical_call_id,
        )

    def result(
        self, logical_call_id: str, output: object, observed_at: str | None, *,
        session_key: str | None = None, line_number: int | None = None,
        turn_key: str | None = None, tool_call_key: str | None = None,
    ) -> None:
        if session_key is None:
            return
        buffer_key = (session_key, logical_call_id)
        if buffer_key in self.rejected:
            return
        if (
            buffer_key not in self.intents
            and line_number is not None
            and tool_call_key is not None
        ):
            self._restore_intent(
                logical_call_id=logical_call_id, session_key=session_key,
                line_number=line_number, observed_at=observed_at,
                turn_key=turn_key, tool_call_key=tool_call_key,
            )
        if buffer_key not in self.intents:
            return
        self.results[buffer_key] = (parse_structured_result(output), observed_at)

    def _persist_current(self, buffer_key: tuple[str, str]) -> None:
        current = self.intents.get(buffer_key)
        if current is None:
            return
        intent, model_call_id = current
        result, observed_at = self.results.get(
            buffer_key, (unknown_result(), intent.observed_at),
        )
        persist_test_evidence(
            self.connection, intent, result, observed_at=observed_at,
        )
        model_cause = self.model_causes.get(model_call_id)
        if model_cause is not None and intent.runner != "unknown":
            persist_semantic_conflict(
                self.connection, intent, result, model_cause,
                self.pseudonymize(
                    "diagnostic", f"test-conflict/{intent.evidence_key}",
                ),
            )

    def _persist_non_execution(self, session_key: str, tool_call_key: str) -> None:
        evidence_key = self.pseudonymize(
            "event", f"test/{session_key}/{tool_call_key}",
        )
        candidate_key = self.pseudonymize(
            "event",
            f"test-candidate/non-execution/{session_key}/{tool_call_key}/"
            f"{self.source_digest}",
        )
        persist_non_execution(
            self.connection, candidate_key=candidate_key,
            evidence_key=evidence_key, source_digest=self.source_digest,
            session_key=session_key, tool_call_key=tool_call_key,
        )

    def _has_app_terminal_rejection(self, intent: TestIntent) -> bool:
        if source_family(self.connection, self.source_digest) != "app_server":
            return False
        return self.connection.execute(
            """SELECT 1 FROM codex_events
                 WHERE session_key=? AND tool_call_key=? AND tool_phase='completed'
                   AND tool_status IN ('declined','cancelled','interrupted')
                   AND tool_exit_status IS NULL LIMIT 1""",
            (intent.session_key, intent.tool_call_key),
        ).fetchone() is not None

    def reject(
        self, logical_call_id: str, *, session_key: str, tool_call_key: str,
    ) -> None:
        """Discard an App command whose terminal state proves no completed run."""
        buffer_key = (session_key, logical_call_id)
        self._persist_current(buffer_key)
        self.rejected.add(buffer_key)
        self.intents.pop(buffer_key, None)
        self.results.pop(buffer_key, None)
        self._persist_non_execution(session_key, tool_call_key)

    def flush(self) -> None:
        for buffer_key, (intent, _model_call_id) in self.intents.items():
            if buffer_key in self.rejected:
                continue
            if self._has_app_terminal_rejection(intent):
                self._persist_non_execution(intent.session_key, intent.tool_call_key)
            self._persist_current(buffer_key)
