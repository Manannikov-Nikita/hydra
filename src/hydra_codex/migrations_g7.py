"""Order-independent lineage claim candidate schema."""

from __future__ import annotations


G7_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (23, (
        """CREATE TABLE lineage_claim_candidates (
            child_key TEXT NOT NULL REFERENCES rollout_sessions(session_key),
            parent_key TEXT NOT NULL,
            project_id TEXT NOT NULL,
            claim_kind TEXT NOT NULL CHECK(claim_kind IN (
                'confirmed','inferred','legacy_ambiguous'
            )),
            CHECK(
                (claim_kind = 'legacy_ambiguous' AND parent_key = '') OR
                (claim_kind != 'legacy_ambiguous' AND parent_key != '')
            ),
            PRIMARY KEY(child_key,parent_key,claim_kind)
        )""",
        """INSERT INTO lineage_claim_candidates(
               child_key,parent_key,project_id,claim_kind)
           SELECT edge.child_key,edge.parent_key,child.project_id,edge.confidence_kind
             FROM session_edges AS edge
             JOIN rollout_sessions AS child ON child.session_key=edge.child_key
             LEFT JOIN rollout_sessions AS parent ON parent.session_key=edge.parent_key
            WHERE edge.parent_key IS NOT NULL
              AND edge.confidence_kind IN ('confirmed','inferred')
              AND (
                  parent.project_id=child.project_id OR (
                      parent.session_key IS NULL AND 1=(
                          SELECT COUNT(DISTINCT sibling_child.project_id)
                            FROM session_edges AS sibling
                            JOIN rollout_sessions AS sibling_child
                              ON sibling_child.session_key=sibling.child_key
                           WHERE sibling.parent_key=edge.parent_key
                      )
                  )
              )""",
        """INSERT INTO lineage_claim_candidates(
               child_key,parent_key,project_id,claim_kind)
           SELECT edge.child_key,'',child.project_id,'legacy_ambiguous'
             FROM session_edges AS edge
             JOIN rollout_sessions AS child ON child.session_key=edge.child_key
             LEFT JOIN rollout_sessions AS parent ON parent.session_key=edge.parent_key
            WHERE edge.confidence_kind NOT IN ('confirmed','inferred')
               OR edge.parent_key IS NULL
               OR parent.project_id != child.project_id
               OR (
                   parent.session_key IS NULL AND 1 != (
                       SELECT COUNT(DISTINCT sibling_child.project_id)
                         FROM session_edges AS sibling
                         JOIN rollout_sessions AS sibling_child
                           ON sibling_child.session_key=sibling.child_key
                        WHERE sibling.parent_key=edge.parent_key
                   )
               )
           ON CONFLICT(child_key,parent_key,claim_kind) DO NOTHING""",
        """UPDATE session_edges
              SET parent_key=NULL,baseline_working_tokens=NULL,
                  confidence_kind='ambiguous',confidence=0.0
            WHERE child_key IN (
                SELECT child_key FROM lineage_claim_candidates
                 WHERE claim_kind='legacy_ambiguous'
            )""",
        """CREATE INDEX lineage_claim_candidates_child
               ON lineage_claim_candidates(child_key,claim_kind,parent_key)""",
        """CREATE TRIGGER lineage_claim_candidates_no_update
           BEFORE UPDATE ON lineage_claim_candidates
           BEGIN
             SELECT RAISE(ABORT, 'lineage claims are immutable');
           END""",
        """CREATE TRIGGER lineage_claim_candidates_no_delete
           BEFORE DELETE ON lineage_claim_candidates
           BEGIN
             SELECT RAISE(ABORT, 'lineage claims are immutable');
           END""",
    )),
)


G7_REQUIRED_SCHEMA = {
    "lineage_claim_candidates": {
        "child_key", "parent_key", "project_id", "claim_kind",
    },
}
