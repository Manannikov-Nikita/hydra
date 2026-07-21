"""Canonical reportable roles for persisted normalized tool spans."""

from __future__ import annotations


L12_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (28, (
        """CREATE TABLE tool_span_roles (
               session_key TEXT NOT NULL,
               call_key TEXT NOT NULL,
               role TEXT NOT NULL CHECK(role='nested_inferred'),
               PRIMARY KEY(session_key,call_key,role),
               FOREIGN KEY(session_key,call_key)
                   REFERENCES tool_spans(session_key,call_key) ON DELETE CASCADE
           )""",
        """INSERT INTO tool_span_roles(session_key,call_key,role)
           SELECT child.session_key,child.call_key,'nested_inferred'
             FROM tool_spans AS child
            WHERE child.provenance='lower_bound'
              AND child.source_digest IS NOT NULL
              AND child.source_ordinal IS NOT NULL
              AND EXISTS (
                    SELECT 1
                      FROM tool_spans AS outer_span
                     WHERE outer_span.session_key=child.session_key
                       AND outer_span.tool_name='custom_exec'
                       AND outer_span.provenance='exact'
                       AND outer_span.source_digest=child.source_digest
                       AND outer_span.source_ordinal=child.source_ordinal
              )
           ON CONFLICT(session_key,call_key,role) DO NOTHING""",
    )),
)


L12_REQUIRED_SCHEMA = {
    "tool_span_roles": {"session_key", "call_key", "role"},
}
