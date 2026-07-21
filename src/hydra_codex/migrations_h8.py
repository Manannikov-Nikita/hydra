"""Immutable test-evidence candidates for order-independent materialization."""

from __future__ import annotations


H8_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (24, (
        """CREATE TABLE test_evidence_candidates (
            candidate_key TEXT PRIMARY KEY,
            candidate_kind TEXT NOT NULL CHECK(candidate_kind IN (
                'description','evidence','non_execution'
            )),
            evidence_key TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            line_number INTEGER NOT NULL CHECK(line_number >= 0),
            session_key TEXT NOT NULL,
            observed_at TEXT,
            turn_key TEXT,
            tool_call_key TEXT NOT NULL,
            command_hash TEXT NOT NULL,
            runner TEXT NOT NULL,
            scope TEXT NOT NULL,
            exit_status INTEGER,
            outcome TEXT NOT NULL,
            failure_cause TEXT NOT NULL,
            provenance TEXT NOT NULL,
            completeness TEXT NOT NULL
        )""",
        """INSERT INTO test_evidence_candidates(
               candidate_key,candidate_kind,evidence_key,source_digest,line_number,
               session_key,observed_at,turn_key,tool_call_key,command_hash,runner,
               scope,exit_status,outcome,failure_cause,provenance,completeness)
           SELECT 'legacy:' || evidence_key,'evidence',evidence_key,source_digest,
                  line_number,session_key,observed_at,turn_key,tool_call_key,
                  command_hash,runner,scope,exit_status,outcome,failure_cause,
                  provenance,completeness
             FROM rollout_test_runs""",
        """INSERT INTO test_evidence_candidates(
               candidate_key,candidate_kind,evidence_key,source_digest,line_number,
               session_key,observed_at,turn_key,tool_call_key,command_hash,runner,
               scope,outcome,failure_cause,provenance,completeness)
           SELECT 'legacy-non-execution:' || source_digest || ':'
                    || CAST(source_ordinal AS TEXT) || ':' || event_key,
                  'non_execution',
                  'legacy-non-execution:' || event_key,source_digest,source_ordinal,
                  session_key,observed_at,turn_key,tool_call_key,'','','','unknown',
                  'unknown','exact','non_execution'
             FROM codex_events
            WHERE tool_name='exec_command' AND tool_phase='completed'
              AND tool_status NOT IN ('completed','success')
              AND session_key IS NOT NULL AND tool_call_key IS NOT NULL""",
        """CREATE INDEX test_evidence_candidates_call
               ON test_evidence_candidates(session_key,tool_call_key)""",
        """CREATE TRIGGER test_evidence_candidates_no_update
           BEFORE UPDATE ON test_evidence_candidates
           BEGIN
             SELECT RAISE(ABORT, 'test evidence candidates are immutable');
           END""",
        """CREATE TRIGGER test_evidence_candidates_no_delete
           BEFORE DELETE ON test_evidence_candidates
           BEGIN
             SELECT RAISE(ABORT, 'test evidence candidates are immutable');
           END""",
    )),
)


H8_REQUIRED_SCHEMA = {
    "test_evidence_candidates": {
        "candidate_key", "candidate_kind", "evidence_key", "source_digest",
        "line_number", "session_key", "observed_at", "turn_key",
        "tool_call_key", "command_hash", "runner", "scope", "exit_status",
        "outcome", "failure_cause", "provenance", "completeness",
    },
}
