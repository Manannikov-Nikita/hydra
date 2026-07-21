"""Order-independent persistence for privacy-safe subagent lineage claims."""

from __future__ import annotations

from typing import Any


def _has_foreign_parent_claim(
    connection: Any, *, parent_key: str, project_id: str,
) -> bool:
    return connection.execute(
        """SELECT 1 FROM lineage_claim_candidates
            WHERE parent_key=? AND claim_kind != 'legacy_ambiguous'
              AND project_id != ? LIMIT 1""",
        (parent_key, project_id),
    ).fetchone() is not None


def assert_session_project(
    connection: Any, *, session_key: str, project_id: str,
) -> None:
    """Fail closed when one opaque session identity crosses project boundaries."""
    row = connection.execute(
        "SELECT project_id FROM rollout_sessions WHERE session_key=?",
        (session_key,),
    ).fetchone()
    if row is None or row[0] != project_id:
        raise ValueError("canonical session identity belongs to another project")
    if _has_foreign_parent_claim(
        connection, parent_key=session_key, project_id=project_id,
    ):
        raise ValueError("canonical session identity belongs to another project")


def _assert_parent_endpoint(
    connection: Any, *, parent_key: str, project_id: str,
) -> None:
    if _has_foreign_parent_claim(
        connection, parent_key=parent_key, project_id=project_id,
    ):
        raise ValueError("canonical session identity belongs to another project")
    row = connection.execute(
        "SELECT project_id FROM rollout_sessions WHERE session_key=?",
        (parent_key,),
    ).fetchone()
    if row is not None and row[0] != project_id:
        raise ValueError("canonical session identity belongs to another project")


def _materialize_parent(connection: Any, *, child_key: str, project_id: str) -> None:
    claims = connection.execute(
        """SELECT parent_key,claim_kind,project_id
             FROM lineage_claim_candidates
            WHERE child_key=?""",
        (child_key,),
    ).fetchall()
    if not claims:
        return
    if any(row[2] != project_id for row in claims):
        raise ValueError("lineage claim belongs to another project")
    if any(row[1] == "legacy_ambiguous" for row in claims):
        parent_key, confidence_kind, confidence = None, "ambiguous", 0.0
    else:
        confirmed = {row[0] for row in claims if row[1] == "confirmed"}
        inferred = {row[0] for row in claims if row[1] == "inferred"}
        for parent in confirmed | inferred:
            _assert_parent_endpoint(
                connection, parent_key=parent, project_id=project_id,
            )
        if len(confirmed) == 1:
            parent_key, confidence_kind, confidence = (
                next(iter(confirmed)), "confirmed", 1.0,
            )
        elif len(confirmed) > 1:
            parent_key, confidence_kind, confidence = None, "ambiguous", 0.0
        elif len(inferred) == 1:
            parent_key, confidence_kind, confidence = (
                next(iter(inferred)), "inferred", 0.6,
            )
        else:
            parent_key, confidence_kind, confidence = None, "ambiguous", 0.0
    connection.execute(
        """INSERT INTO session_edges(
               child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
           VALUES (?,?,NULL,?,?)
           ON CONFLICT(child_key) DO UPDATE SET
             parent_key=excluded.parent_key,
             baseline_working_tokens=NULL,
             confidence_kind=excluded.confidence_kind,
             confidence=excluded.confidence""",
        (child_key, parent_key, confidence_kind, confidence),
    )


def _persist_parent_claim(
    connection: Any, *, child_key: str, parent_key: str,
    project_id: str, claim_kind: str,
) -> None:
    assert_session_project(
        connection, session_key=child_key, project_id=project_id,
    )
    _assert_parent_endpoint(
        connection, parent_key=parent_key, project_id=project_id,
    )
    connection.execute(
        """INSERT INTO lineage_claim_candidates(
               child_key,parent_key,project_id,claim_kind)
           VALUES (?,?,?,?) ON CONFLICT(child_key,parent_key,claim_kind) DO NOTHING""",
        (child_key, parent_key, project_id, claim_kind),
    )
    _materialize_parent(
        connection, child_key=child_key, project_id=project_id,
    )


def persist_inferred_parent(
    connection: Any, *, child_key: str, parent_key: str, project_id: str,
) -> None:
    """Append one inferred claim and deterministically rematerialize its edge."""
    _persist_parent_claim(
        connection, child_key=child_key, parent_key=parent_key,
        project_id=project_id, claim_kind="inferred",
    )


def persist_confirmed_parent(
    connection: Any, *, child_key: str, parent_key: str, project_id: str,
) -> None:
    """Append one confirmed claim and deterministically rematerialize its edge."""
    _persist_parent_claim(
        connection, child_key=child_key, parent_key=parent_key,
        project_id=project_id, claim_kind="confirmed",
    )
