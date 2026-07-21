"""Order-independent normalized tool-span candidate schema."""

from __future__ import annotations


F6_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (22, (
        """CREATE TABLE tool_span_candidates (
            session_key TEXT NOT NULL,
            call_key TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            source_ordinal INTEGER NOT NULL CHECK(source_ordinal >= 0),
            candidate_kind TEXT NOT NULL CHECK(candidate_kind IN (
                'start','end','legacy_description','legacy_values'
            )),
            category TEXT NOT NULL,
            terminal_state TEXT NOT NULL CHECK(terminal_state IN ('unknown','success','failed')),
            latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms >= 0),
            tool_name TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            turn_key TEXT,
            provenance TEXT NOT NULL CHECK(provenance IN ('exact','lower_bound','estimated')),
            PRIMARY KEY(
                session_key,call_key,source_digest,source_ordinal,candidate_kind
            )
        )""",
        """INSERT INTO tool_span_candidates(
               session_key,call_key,source_digest,source_ordinal,candidate_kind,
               category,terminal_state,latency_ms,tool_name,started_at,finished_at,
               turn_key,provenance)
           SELECT session_key,call_key,COALESCE(source_digest,''),
                  COALESCE(source_ordinal,0),'legacy_description',category,'unknown',
                  NULL,COALESCE(tool_name,'unknown'),NULL,NULL,NULL,
                  CASE WHEN provenance IN ('exact','lower_bound','estimated')
                       THEN provenance ELSE 'estimated' END
             FROM tool_spans""",
        """INSERT INTO tool_span_candidates(
               session_key,call_key,source_digest,source_ordinal,candidate_kind,
               category,terminal_state,latency_ms,tool_name,started_at,finished_at,
               turn_key,provenance)
           SELECT session_key,call_key,'',0,'legacy_values',category,
                  CASE
                    WHEN terminal_state IN ('success','completed') THEN 'success'
                    WHEN terminal_state IN ('failed','interrupted','cancelled','declined')
                      THEN 'failed'
                    ELSE 'unknown'
                  END,
                  latency_ms,COALESCE(tool_name,'unknown'),started_at,finished_at,
                  turn_key,
                  CASE WHEN provenance IN ('exact','lower_bound','estimated')
                       THEN provenance ELSE 'estimated' END
             FROM tool_spans""",
        """UPDATE tool_spans
              SET terminal_state=CASE
                    WHEN terminal_state IN ('success','completed') THEN 'success'
                    WHEN terminal_state IN ('failed','interrupted','cancelled','declined')
                      THEN 'failed'
                    ELSE 'unknown'
                  END,
                  provenance=CASE
                    WHEN provenance IN ('exact','lower_bound','estimated')
                      THEN provenance ELSE 'estimated' END""",
        """CREATE INDEX tool_span_candidates_span
               ON tool_span_candidates(session_key,call_key)""",
    )),
)


F6_REQUIRED_SCHEMA = {
    "tool_span_candidates": {
        "session_key", "call_key", "source_digest", "source_ordinal",
        "candidate_kind", "category", "terminal_state", "latency_ms",
        "tool_name", "started_at", "finished_at", "turn_key", "provenance",
    },
}
