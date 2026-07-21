"""Persist the single token-total authority selected for each Codex session."""

from __future__ import annotations

import sqlite3


_RANK = {"otel": 1, "app_server": 2, "rollout": 3}


def refresh_token_source_selection(connection: sqlite3.Connection, project_id: str) -> None:
    """Choose rollout > App cumulative > OTel calls without deleting fallback facts."""
    rows = connection.execute(
        """SELECT session_key,source_family,observed_at,event_key,input_tokens,
                  cached_input_tokens,output_tokens,reasoning_tokens,
                  source_digest,line_number
             FROM token_snapshots WHERE project_id=?""",
        (project_id,),
    )
    by_session: dict[str, list[tuple[object, ...]]] = {}
    for row in rows:
        by_session.setdefault(str(row[0]), []).append(tuple(row[1:]))
    for session, facts in by_session.items():
        families = {str(item[0]) for item in facts}
        selected = max(families, key=_RANK.__getitem__)
        selected_event = None
        app_conflict = False
        if selected == "app_server":
            candidates = [item for item in facts if item[0] == "app_server"]
            winner = max(candidates, key=lambda item: (
                int(item[3]), int(item[5]), int(item[6]), int(item[4]), str(item[2]),
            ))
            selected_event = str(winner[2])
            app_conflict = any(
                any(int(winner[index]) < int(item[index]) for index in (3, 4, 5, 6))
                for item in candidates
            )
            by_source: dict[str, list[tuple[object, ...]]] = {}
            for item in candidates:
                by_source.setdefault(str(item[7]), []).append(item)
            app_conflict = app_conflict or any(
                any(
                    int(current[index]) < int(previous[index])
                    for index in (3, 4, 5, 6)
                )
                for values in by_source.values()
                for previous, current in zip(
                    sorted(values, key=lambda item: int(item[8])),
                    sorted(values, key=lambda item: int(item[8]))[1:],
                )
            )
        if selected == "rollout" and len(families) > 1:
            caveat = "rollout_cumulative_preferred"
            provenance = "derived"
        elif selected == "app_server" and app_conflict:
            caveat = "app_cumulative_conflict"
            provenance = "estimated"
        elif selected == "app_server" and "otel" in families:
            caveat = "app_cumulative_preferred_over_otel"
            provenance = "derived"
        elif selected == "app_server" and any(
            item[0] == "app_server" and item[1] is None for item in facts
        ):
            caveat = "app_total_timestamp_missing"
            provenance = "exact"
        elif selected == "otel":
            caveat = "otel_per_call_fallback"
            provenance = "estimated"
        else:
            caveat = None
            provenance = "exact"
        connection.execute(
            """UPDATE token_snapshots
                  SET contributes_total=CASE
                        WHEN source_family=? AND (? IS NULL OR event_key=?) THEN 1 ELSE 0 END,
                      selection_provenance=CASE
                        WHEN source_family=? AND (? IS NULL OR event_key=?) THEN ? ELSE 'derived' END,
                      selection_caveat=CASE
                        WHEN source_family=? AND (? IS NULL OR event_key=?) THEN ? ELSE NULL END
                WHERE project_id=? AND session_key=?""",
            (
                selected, selected_event, selected_event,
                selected, selected_event, selected_event, provenance,
                selected, selected_event, selected_event, caveat,
                project_id, session,
            ),
        )
