"""Canonical lifecycle and execution-only metric materialization."""

from __future__ import annotations


K11_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (27, (
        "ALTER TABLE turn_attempts ADD COLUMN started_event_key TEXT",
        "ALTER TABLE turn_attempts ADD COLUMN terminal_event_key TEXT",
        "ALTER TABLE turn_attempts ADD COLUMN started_logical_source_key TEXT",
        "ALTER TABLE turn_attempts ADD COLUMN terminal_logical_source_key TEXT",
        "ALTER TABLE turn_attempts ADD COLUMN started_source_ordinal INTEGER",
        "ALTER TABLE turn_attempts ADD COLUMN terminal_source_ordinal INTEGER",
        # Test descriptions and file operands remain in immutable candidate
        # tables, but an intent is not an executed metric. File cleanup stays
        # conservative and never depends on locating an installation key.
        "DELETE FROM rollout_test_runs WHERE completeness='intent_only'",
        """DELETE FROM file_observations AS observation
              WHERE observation.line_number=0
                AND NOT EXISTS (
                    SELECT 1
                      FROM file_observation_candidates AS candidate
                     WHERE candidate.session_key=observation.session_key
                       AND candidate.operation=observation.operation
                       AND candidate.relative_path=observation.relative_path
                       AND candidate.path_hash=observation.path_hash
                       AND (
                           EXISTS (
                               SELECT 1 FROM tool_span_candidates AS terminal
                                WHERE terminal.session_key=candidate.session_key
                                  AND terminal.call_key=candidate.call_key
                                  AND terminal.candidate_kind='end'
                                  AND (
                                      candidate.requires_success=0
                                      OR terminal.terminal_state='success'
                                  )
                           ) OR EXISTS (
                               SELECT 1 FROM tool_spans AS terminal
                                WHERE terminal.session_key=candidate.session_key
                                  AND terminal.call_key=candidate.call_key
                                  AND terminal.completeness='complete'
                                  AND (
                                      candidate.requires_success=0
                                      OR terminal.terminal_state='success'
                                  )
                           )
                       )
                )""",
        """DELETE FROM file_observations
              WHERE EXISTS (
                    SELECT 1 FROM file_observation_candidates AS candidate
                     WHERE candidate.session_key=file_observations.session_key
                       AND candidate.source_digest=file_observations.source_digest
                       AND candidate.source_ordinal=file_observations.line_number
                       AND candidate.operation=file_observations.operation
                       AND candidate.path_hash=file_observations.path_hash
                       AND candidate.evidence_kind='exact'
                       AND NOT EXISTS (
                           SELECT 1 FROM file_observation_candidates AS historical
                            WHERE historical.session_key=candidate.session_key
                              AND historical.source_digest=candidate.source_digest
                              AND historical.source_ordinal=candidate.source_ordinal
                              AND historical.operation=candidate.operation
                              AND historical.path_hash=candidate.path_hash
                              AND historical.evidence_kind!='exact'
                       )
                       AND NOT EXISTS (
                           SELECT 1 FROM tool_span_candidates AS terminal
                            WHERE terminal.session_key=candidate.session_key
                              AND terminal.call_key=candidate.call_key
                              AND terminal.candidate_kind='end'
                              AND (
                                  candidate.requires_success=0
                                  OR terminal.terminal_state='success'
                              )
                       )
              )""",
    )),
)


K11_REQUIRED_SCHEMA = {
    "turn_attempts": {
        "started_event_key", "terminal_event_key",
        "started_logical_source_key", "terminal_logical_source_key",
        "started_source_ordinal", "terminal_source_ordinal",
    },
}
