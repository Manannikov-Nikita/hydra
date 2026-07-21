"""Immutable, privacy-safe candidates for canonical file evidence."""

from __future__ import annotations

from typing import Any, Callable


I9_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (25, (
        """CREATE TABLE file_observation_candidates (
            session_key TEXT NOT NULL,
            call_key TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            source_ordinal INTEGER NOT NULL CHECK(source_ordinal >= 0),
            operation TEXT NOT NULL CHECK(operation IN ('read','write')),
            relative_path TEXT NOT NULL,
            path_hash TEXT NOT NULL,
            observed_at TEXT,
            turn_key TEXT,
            tool_name TEXT NOT NULL,
            requires_success INTEGER NOT NULL CHECK(requires_success IN (0,1)),
            evidence_kind TEXT NOT NULL CHECK(evidence_kind IN (
                'exact','legacy','quarantined'
            )),
            PRIMARY KEY(session_key,call_key,candidate_key)
        )""",
        """WITH eligible_calls AS (
               SELECT observation.rowid AS observation_rowid,candidate.call_key
                 FROM file_observations AS observation
                 JOIN tool_span_candidates AS candidate
                   ON candidate.session_key=observation.session_key
                  AND candidate.source_digest=observation.source_digest
                  AND candidate.source_ordinal=observation.line_number
               UNION
               SELECT observation.rowid AS observation_rowid,span.call_key
                 FROM file_observations AS observation
                 JOIN tool_spans AS span
                   ON span.session_key=observation.session_key
                  AND span.source_digest=observation.source_digest
                  AND span.terminal_state='success'
                  AND span.completeness='complete'
                  AND span.finished_at IS NOT NULL
                  AND observation.observed_at IS NOT NULL
                  AND span.finished_at=observation.observed_at
                  AND span.turn_key IS observation.turn_key
                  AND span.source_ordinal IS NOT NULL
                  AND span.source_ordinal<observation.line_number
                  AND (
                       (observation.operation='write' AND span.tool_name IN (
                           'apply_patch','patch','exec_command','file_write'
                       ))
                    OR (observation.operation='read' AND span.tool_name IN (
                           'exec_command','file_read','read_mcp_resource','view_image'
                       ))
                  )
           ), resolved_calls AS (
               SELECT observation_rowid,MIN(call_key) AS call_key
                 FROM eligible_calls
                GROUP BY observation_rowid
               HAVING COUNT(DISTINCT call_key)=1
           )
           INSERT INTO file_observation_candidates(
               session_key,call_key,candidate_key,source_digest,source_ordinal,operation,
               relative_path,path_hash,observed_at,turn_key,tool_name,
               requires_success,evidence_kind)
           SELECT observation.session_key,
                  COALESCE(resolved.call_key,
                           'legacy/' || observation.source_digest || '/'
                             || CAST(observation.line_number AS TEXT)),
                  'legacy/' || observation.source_digest || '/'
                    || CAST(observation.line_number AS TEXT) || '/'
                    || observation.operation || '/' || observation.path_hash,
                  observation.source_digest,observation.line_number,
                  observation.operation,observation.relative_path,
                  observation.path_hash,observation.observed_at,
                  observation.turn_key,'unknown',0,
                  CASE WHEN observation.line_number=0
                       THEN 'quarantined' ELSE 'legacy' END
             FROM file_observations AS observation
             LEFT JOIN resolved_calls AS resolved
               ON resolved.observation_rowid=observation.rowid""",
        """CREATE INDEX file_observation_candidates_call
               ON file_observation_candidates(session_key,call_key)""",
        """CREATE TRIGGER file_observation_candidates_no_update
           BEFORE UPDATE ON file_observation_candidates
           BEGIN
             SELECT RAISE(ABORT, 'file observation candidates are immutable');
           END""",
        """CREATE TRIGGER file_observation_candidates_no_delete
           BEFORE DELETE ON file_observation_candidates
           BEGIN
             SELECT RAISE(ABORT, 'file observation candidates are immutable');
           END""",
    )),
)


I9_REQUIRED_SCHEMA = {
    "file_observation_candidates": {
        "session_key", "call_key", "candidate_key", "source_digest", "source_ordinal",
        "operation", "relative_path", "path_hash", "observed_at", "turn_key",
        "tool_name", "requires_success", "evidence_kind",
    },
}


def recover_line_zero_file_candidates(
    connection: Any,
    *,
    source_identity: Callable[[str, str], str] | None,
) -> None:
    """Bind v22 synthetic line-zero rows to one privacy-safe call identity.

    The synthetic source is an installation-keyed HMAC over session/call.  We
    compare that already-pseudonymized identity directly and never need raw
    prompts, paths, or an ``ACTIVE_HASHER`` context.  Missing keys and any
    ambiguity deliberately leave the candidate quarantined.
    """
    if source_identity is None:
        return

    by_materialized_source: dict[tuple[str, str], set[str]] = {}
    for session_key, call_key in connection.execute(
        "SELECT session_key,call_key FROM tool_spans"
    ):
        materialized_source = source_identity(str(session_key), str(call_key))
        by_materialized_source.setdefault(
            (str(session_key), materialized_source), set(),
        ).add(str(call_key))

    rows = connection.execute(
        """SELECT session_key,call_key,candidate_key,source_digest,source_ordinal,
                  operation,relative_path,path_hash,observed_at,turn_key,tool_name,
                  requires_success
             FROM file_observation_candidates
            WHERE evidence_kind='quarantined' AND source_ordinal=0"""
    ).fetchall()
    for row in rows:
        matches = by_materialized_source.get((str(row[0]), str(row[3])), set())
        if len(matches) != 1:
            continue
        resolved_call = next(iter(matches))
        candidate_key = "/".join((
            "legacy-resolved", str(row[3]), str(row[5]), str(row[7]),
            resolved_call,
        ))
        connection.execute(
            """INSERT INTO file_observation_candidates(
                   session_key,call_key,candidate_key,source_digest,source_ordinal,
                   operation,relative_path,path_hash,observed_at,turn_key,tool_name,
                   requires_success,evidence_kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'legacy')
               ON CONFLICT DO NOTHING""",
            (
                row[0], resolved_call, candidate_key, row[3], row[4], row[5],
                row[6], row[7], row[8], row[9], row[10], row[11],
            ),
        )
