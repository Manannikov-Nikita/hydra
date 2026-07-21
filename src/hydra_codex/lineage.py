"""Deterministic persistence for confirmed subagent lineage claims."""

from __future__ import annotations

from typing import Any


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


def persist_inferred_parent(
    connection: Any, *, child_key: str, parent_key: str,
) -> None:
    """Quarantine competing inferred claims without downgrading stronger facts."""
    row = connection.execute(
        "SELECT parent_key,confidence_kind FROM session_edges WHERE child_key=?",
        (child_key,),
    ).fetchone()
    if row is None:
        connection.execute(
            """INSERT INTO session_edges(
                   child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
               VALUES (?,?,NULL,'inferred',0.6)""",
            (child_key, parent_key),
        )
        return
    existing_parent, confidence_kind = row
    if confidence_kind in {"confirmed", "ambiguous"}:
        return
    if confidence_kind != "inferred":
        raise ValueError("unsupported persisted lineage confidence")
    if existing_parent == parent_key:
        return
    connection.execute(
        """UPDATE session_edges
              SET parent_key=NULL,baseline_working_tokens=NULL,
                  confidence_kind='ambiguous',confidence=0.0
            WHERE child_key=?""",
        (child_key,),
    )


def persist_confirmed_parent(
    connection: Any, *, child_key: str, parent_key: str,
) -> None:
    """Keep identical claims confirmed and quarantine conflicting confirmations."""
    row = connection.execute(
        "SELECT parent_key,confidence_kind FROM session_edges WHERE child_key=?",
        (child_key,),
    ).fetchone()
    if row is None:
        connection.execute(
            """INSERT INTO session_edges(
                   child_key,parent_key,baseline_working_tokens,confidence_kind,confidence)
               VALUES (?,?,NULL,'confirmed',1.0)""",
            (child_key, parent_key),
        )
        return
    existing_parent, confidence_kind = row
    if confidence_kind == "ambiguous":
        return
    if confidence_kind == "confirmed" and existing_parent != parent_key:
        connection.execute(
            """UPDATE session_edges
                  SET parent_key=NULL,baseline_working_tokens=NULL,
                      confidence_kind='ambiguous',confidence=0.0
                WHERE child_key=?""",
            (child_key,),
        )
        return
    connection.execute(
        """UPDATE session_edges
              SET parent_key=?,baseline_working_tokens=NULL,
                  confidence_kind='confirmed',confidence=1.0
            WHERE child_key=?""",
        (parent_key, child_key),
    )
