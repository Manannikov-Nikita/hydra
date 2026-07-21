"""Use timestamped OTel calls only as semantic allocation hints."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Callable, Iterable, Protocol

from .task_tree_types import TokenVector


class AuthoritativeDelta(Protocol):
    session_key: str
    observed_at: datetime | None
    vector: TokenVector


PhaseLookup = Callable[[str, datetime], tuple[str | None, str | None, bool]]


@dataclass(frozen=True)
class AllocationFact:
    session_key: str
    event_key: str
    observed_at: datetime | None
    vector: TokenVector
    provenance: str
    phase: str | None
    cause: str | None


@dataclass(frozen=True)
class AllocationResult:
    replaced_sessions: frozenset[str]
    facts: tuple[AllocationFact, ...]
    diagnostics: Counter[str]


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _sum(vectors: Iterable[TokenVector]) -> TokenVector:
    total = TokenVector.zero()
    for vector in vectors:
        total = total + vector
    return total


def allocate_otel_hints(
    connection: sqlite3.Connection, project_id: str,
    session_ids: tuple[str, ...], cutoff_at: datetime,
    authoritative: Iterable[AuthoritativeDelta], phase_lookup: PhaseLookup,
) -> AllocationResult:
    """Replace timestamp-missing cumulative deltas with bounded OTel hints plus residual."""
    by_session: dict[str, list[AuthoritativeDelta]] = defaultdict(list)
    for item in authoritative:
        by_session[item.session_key].append(item)
    if not session_ids:
        return AllocationResult(frozenset(), (), Counter())
    placeholders = ",".join("?" for _ in session_ids)
    selected = {
        str(session): str(family)
        for session, family in connection.execute(
            f"""SELECT DISTINCT session_key,source_family FROM token_snapshots
                   WHERE project_id=? AND contributes_total=1
                     AND session_key IN ({placeholders})""",
            (project_id, *session_ids),
        )
    }
    rows = connection.execute(
        f"""SELECT t.session_key,t.event_key,t.observed_at,t.input_tokens,
                   t.cached_input_tokens,t.output_tokens,t.reasoning_tokens,s.started_at
              FROM token_snapshots t JOIN rollout_sessions s ON s.session_key=t.session_key
             WHERE t.project_id=? AND t.source_family='otel' AND t.contributes_total=0
               AND t.session_key IN ({placeholders})
             ORDER BY t.session_key,julianday(t.observed_at),t.event_key""",
        (project_id, *session_ids),
    )
    hints: dict[str, list[tuple[str, datetime, TokenVector]]] = defaultdict(list)
    for session, event_key, value, input_tokens, cached, output, reasoning, start in rows:
        observed = _timestamp(value)
        started = _timestamp(start)
        if observed is not None and observed <= cutoff_at and (
            started is None or started <= observed
        ):
            hints[str(session)].append((
                str(event_key), observed,
                TokenVector(input_tokens, cached, output, reasoning),
            ))
    diagnostics: Counter[str] = Counter()
    facts: list[AllocationFact] = []
    replaced: set[str] = set()
    for session, candidates in sorted(hints.items()):
        base = by_session.get(session, [])
        if selected.get(session) != "app_server" or not base:
            continue
        authoritative_total = _sum(item.vector for item in base)
        hinted_total = _sum(item[2] for item in candidates)
        try:
            residual = authoritative_total.subtract(hinted_total)
        except ValueError:
            diagnostics["otel_allocation_exceeds_authoritative"] += 1
            continue
        replaced.add(session)
        for event_key, observed, vector in candidates:
            phase, cause, overlap = phase_lookup(session, observed)
            if overlap:
                diagnostics["overlapping_semantic_intervals"] += 1
                phase = cause = None
            facts.append(AllocationFact(
                session, event_key + ":allocation", observed, vector,
                "derived", phase, cause,
            ))
        if residual != TokenVector.zero():
            facts.append(AllocationFact(
                session, f"otel-allocation-residual:{session}", None,
                residual, "estimated", None, None,
            ))
        diagnostics["otel_allocation_hint_used"] += len(candidates)
    return AllocationResult(frozenset(replaced), tuple(facts), diagnostics)
