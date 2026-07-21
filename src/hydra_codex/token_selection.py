"""Persist one token-total source and its cutoff-safe cumulative history."""

from __future__ import annotations

import sqlite3

from .rollout_privacy import canonical_timestamp


_RANK = {"otel": 1, "app_server": 2, "rollout": 3}


def _app_vector(item: tuple[object, ...]) -> tuple[int, int, int, int]:
    return int(item[3]), int(item[4]), int(item[5]), int(item[6])


def _app_winner(item: tuple[object, ...]) -> tuple[object, ...]:
    input_tokens, cached, output, reasoning = _app_vector(item)
    return (
        input_tokens, output, reasoning, cached, str(item[2]), str(item[7]), int(item[8]),
    )


def _app_row_key(item: tuple[object, ...]) -> tuple[str, int]:
    return str(item[7]), int(item[8])


def _selected_app_rows(
    candidates: list[tuple[object, ...]],
) -> set[tuple[str, int]]:
    """Keep cutoff-safe history without globally ordering incomparable streams."""
    timestamped = [
        (canonical_timestamp(item[1]).epoch, item)
        for item in candidates
    ]
    if any(epoch is None for epoch, _item in timestamped):
        # Source ordinals are meaningful only inside one immutable JSONL stream.
        # Pick one stream deterministically, then retain its cumulative history
        # so a trusted completion cutoff can still use a proven earlier value.
        selected_source = str(max(candidates, key=_app_winner)[7])
        source_rows = sorted(
            (item for item in candidates if str(item[7]) == selected_source),
            key=lambda item: int(item[8]),
        )
        selected: set[tuple[str, int]] = set()
        previous: tuple[int, int, int, int] | None = None
        for item in source_rows:
            vector = _app_vector(item)
            if vector != previous:
                selected.add(_app_row_key(item))
            previous = vector
        return selected

    by_instant: dict[float, list[tuple[object, ...]]] = {}
    for epoch, item in timestamped:
        if epoch is not None:
            by_instant.setdefault(epoch, []).append(item)
    selected: set[tuple[str, int]] = set()
    previous: tuple[int, int, int, int] | None = None
    for epoch in sorted(by_instant):
        winner = max(by_instant[epoch], key=_app_winner)
        vector = _app_vector(winner)
        if vector != previous:
            selected.add(_app_row_key(winner))
        previous = vector
    return selected


def refresh_token_source_selection(connection: sqlite3.Connection, project_id: str) -> None:
    """Choose rollout > App cumulative > OTel calls without deleting fallback facts."""
    rows = connection.execute(
        """SELECT session_key,source_family,observed_at,event_key,input_tokens,
                  cached_input_tokens,output_tokens,reasoning_tokens,
                  source_digest,line_number
             FROM token_snapshots WHERE project_id=? AND vector_valid=1""",
        (project_id,),
    )
    by_session: dict[str, list[tuple[object, ...]]] = {}
    for row in rows:
        by_session.setdefault(str(row[0]), []).append(tuple(row[1:]))
    for session, facts in by_session.items():
        families = {str(item[0]) for item in facts}
        selected = max(families, key=_RANK.__getitem__)
        selected_app_rows: set[tuple[str, int]] | None = None
        app_conflict = False
        if selected == "app_server":
            candidates = [item for item in facts if item[0] == "app_server"]
            winner = max(candidates, key=_app_winner)
            selected_app_rows = _selected_app_rows(candidates)
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
                  SET contributes_total=0,
                      selection_provenance='derived',
                      selection_caveat=NULL
                WHERE project_id=? AND session_key=? AND vector_valid=1""",
            (project_id, session),
        )
        if selected_app_rows is None:
            connection.execute(
                """UPDATE token_snapshots
                      SET contributes_total=1,selection_provenance=?,selection_caveat=?
                    WHERE project_id=? AND session_key=? AND source_family=?
                      AND vector_valid=1""",
                (provenance, caveat, project_id, session, selected),
            )
        else:
            connection.executemany(
                """UPDATE token_snapshots
                      SET contributes_total=1,selection_provenance=?,selection_caveat=?
                    WHERE project_id=? AND session_key=? AND source_family='app_server'
                      AND source_digest=? AND line_number=? AND vector_valid=1""",
                (
                    (provenance, caveat, project_id, session, source_digest, line_number)
                    for source_digest, line_number in selected_app_rows
                ),
            )
