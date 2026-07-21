"""Task-scoped annotation facts and deterministic test-cause evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import sqlite3

from .contracts import AnnotationCause, AnnotationPhase
from .exact_time import ExactInstant, instant_from_datetime, parse_exact_timestamp
from .redaction import redact_note


TIMELINE_LIMIT = 20
_TEST_SCOPES = frozenset({"targeted", "full", "unknown"})
_TEST_FAILURES = frozenset({"none", "product_failure", "infra_failure", "unknown"})
_TEST_RETRIES = frozenset({
    "none", "flaky_retry", "product_fix_verification", "infra_recovery",
    "unknown_recovery",
})
_PHASES = frozenset(item.value for item in AnnotationPhase)
_CAUSES = frozenset(item.value for item in AnnotationCause)
SemanticInterval = tuple[ExactInstant, ExactInstant | None, str, str]


@dataclass(frozen=True)
class ReconciledMarker:
    kind: str
    phase: str
    cause: str
    scope_change: str
    confidence: float
    outcome: str | None
    note: str
    provenance: str

    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.kind, self.phase, self.cause, self.scope_change,
            self.confidence, self.outcome, self.note, self.provenance,
        )


@dataclass(frozen=True)
class ReconciledTestEvidence:
    scope: str
    failure_cause: str
    retry_kind: str
    phase: str
    cause: str
    count: int

    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.scope, self.failure_cause, self.retry_kind,
            self.phase, self.cause, self.count,
        )


@dataclass(frozen=True)
class AnnotationFacts:
    task_family: str | None
    family_conflict: bool
    invalid_annotation_timestamps: int
    invalid_interval_timestamps: int
    marker_count: int
    instrumented: bool
    finish_count: int
    kind_counts: dict[str, int]
    cause_counts: dict[str, int]
    scope_change_counts: dict[str, int]
    finish_outcome_counts: dict[str, int]
    deterministic_test_causes: dict[str, int]
    test_evidence: tuple[ReconciledTestEvidence, ...]
    timeline: tuple[ReconciledMarker, ...]
    truncated_count: int
    source_fingerprint: str
    detected_conflicts: frozenset[str]
    invalid_test_times: frozenset[str]


def load_intervals(
    connection: sqlite3.Connection,
    project_id: str,
    sessions: tuple[str, ...],
    cutoff: datetime,
    cutoff_instant: ExactInstant | None = None,
) -> tuple[dict[str, list[SemanticInterval]], int]:
    exact_cutoff = cutoff_instant or instant_from_datetime(cutoff)
    placeholders = ",".join("?" for _ in sessions)
    rows = connection.execute(
        f"""SELECT session_key,started_at,ended_at,phase,cause
                FROM semantic_intervals
               WHERE project_id=? AND session_key IN ({placeholders})""",
        (project_id, *sessions),
    )
    result: dict[str, list[SemanticInterval]] = defaultdict(list)
    invalid = 0
    for row in rows:
        start_instant = parse_exact_timestamp(row[1])
        raw_end = row[2]
        end_instant = parse_exact_timestamp(raw_end)
        if (
            start_instant is None
            or raw_end is not None and end_instant is None
            or end_instant is not None and end_instant <= start_instant
        ):
            invalid += 1
            continue
        if start_instant <= exact_cutoff:
            result[str(row[0])].append((
                start_instant,
                end_instant,
                str(row[3]), str(row[4]),
            ))
    for values in result.values():
        values.sort(key=lambda item: (
            item[0], item[1] or exact_cutoff, item[2], item[3],
        ))
    return result, invalid


def phase_at(
    intervals: list[SemanticInterval],
    observed: ExactInstant | datetime,
) -> tuple[str | None, str | None, bool]:
    exact_observed = (
        observed
        if isinstance(observed, ExactInstant)
        else instant_from_datetime(observed)
    )
    matches = [
        item for item in intervals
        if item[0] <= exact_observed
        and (item[1] is None or exact_observed < item[1])
    ]
    if not matches:
        return None, None, False
    selected = max(matches, key=lambda item: (item[0], item[2], item[3]))
    return selected[2], selected[3], len(matches) > 1


def _annotation_rows(
    connection: sqlite3.Connection,
    project_id: str,
    sessions: tuple[str, ...],
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in sessions)
    return list(connection.execute(
        f"""SELECT annotation_id,observed_at,sequence,kind,phase,cause,scope_change,
                   task_family,confidence,outcome,provenance,note_redacted
              FROM annotations
             WHERE project_id=? AND session_id IN ({placeholders})""",
        (project_id, *sessions),
    ))


def _instrumented(
    connection: sqlite3.Connection,
    project_id: str,
    sessions: tuple[str, ...],
    cutoff: datetime,
    cutoff_instant: ExactInstant | None = None,
) -> bool:
    exact_cutoff = cutoff_instant or instant_from_datetime(cutoff)
    placeholders = ",".join("?" for _ in sessions)
    return any(
        observed is not None and observed <= exact_cutoff
        for (value,) in connection.execute(
            f"""SELECT created_at FROM trusted_turn_bindings
                  WHERE project_id=? AND session_key IN ({placeholders})""",
            (project_id, *sessions),
        )
        if (observed := parse_exact_timestamp(value)) is not None
    )


def _test_facts(
    connection: sqlite3.Connection,
    sessions: tuple[str, ...],
    cutoff: datetime,
    intervals: dict[str, list[SemanticInterval]],
    cutoff_instant: ExactInstant | None = None,
) -> tuple[
    Counter[str], tuple[ReconciledTestEvidence, ...], frozenset[str], frozenset[str],
]:
    exact_cutoff = cutoff_instant or instant_from_datetime(cutoff)
    placeholders = ",".join("?" for _ in sessions)
    rows = connection.execute(
        f"""SELECT evidence_key,session_key,observed_at,scope,failure_cause,retry_kind,
                   source_digest,line_number
              FROM rollout_test_runs
             WHERE session_key IN ({placeholders}) AND completeness='complete'""",
        sessions,
    )
    public_cause = {"product_failure": "test_failure", "infra_failure": "infra_failure"}
    counts: Counter[str] = Counter()
    evidence_counts: Counter[tuple[str, str, str, str, str]] = Counter()
    conflicts: set[str] = set()
    invalid: set[str] = set()
    for evidence, session, value, scope, failure, retry, source, line in rows:
        observed_instant = parse_exact_timestamp(value)
        if observed_instant is None:
            invalid.add(str(evidence))
            continue
        if observed_instant > exact_cutoff:
            continue
        phase, model_cause, overlap = phase_at(
            intervals.get(str(session), []), observed_instant,
        )
        safe_scope = str(scope) if str(scope) in _TEST_SCOPES else "unknown"
        safe_failure = str(failure) if str(failure) in _TEST_FAILURES else "unknown"
        safe_retry = str(retry) if str(retry) in _TEST_RETRIES else "unknown_recovery"
        safe_phase = phase if not overlap and phase in _PHASES else "unclassified"
        safe_cause = model_cause if not overlap and model_cause in _CAUSES else "unclassified"
        evidence_counts[(safe_scope, safe_failure, safe_retry, safe_phase, safe_cause)] += 1
        deterministic = public_cause.get(safe_failure)
        if deterministic is None:
            continue
        counts[deterministic] += 1
        if not overlap and model_cause is not None and model_cause != deterministic:
            conflicts.add(f"{source}:{line}")
    evidence_rows = tuple(
        ReconciledTestEvidence(*key, count)
        for key, count in sorted(evidence_counts.items())
    )
    return counts, evidence_rows, frozenset(conflicts), frozenset(invalid)


def build_annotation_facts(
    connection: sqlite3.Connection,
    project_id: str,
    sessions: tuple[str, ...],
    cutoff: datetime,
    cutoff_instant: ExactInstant | None = None,
) -> AnnotationFacts:
    exact_cutoff = cutoff_instant or instant_from_datetime(cutoff)
    rows = _annotation_rows(connection, project_id, sessions)
    valid: list[tuple[ExactInstant, sqlite3.Row]] = []
    invalid = 0
    for row in rows:
        observed = parse_exact_timestamp(row[1])
        if observed is None:
            invalid += 1
        elif observed <= exact_cutoff:
            valid.append((observed, row))
    valid.sort(key=lambda item: (item[0], int(item[1][2]), str(item[1][0])))
    real_families = [
        str(row[7]) for _observed, row in valid if str(row[7]) != "unclassified"
    ]
    model = [item for item in valid if str(item[1][10]) == "model_reported"]
    kind_counts = Counter(str(row[3]) for _observed, row in model)
    cause_counts = Counter(str(row[5]) for _observed, row in model)
    scope_counts = Counter(str(row[6]) for _observed, row in model)
    outcome_counts = Counter(
        str(row[9]) for _observed, row in model if row[9] is not None
    )
    markers = tuple(
        ReconciledMarker(
            str(row[3]), str(row[4]), str(row[5]), str(row[6]), float(row[8]),
            None if row[9] is None else str(row[9]), redact_note(str(row[11])),
            "model_reported",
        )
        for _observed, row in model
    )
    timeline = markers[-TIMELINE_LIMIT:]
    intervals, invalid_intervals = load_intervals(
        connection, project_id, sessions, cutoff, exact_cutoff,
    )
    deterministic, test_evidence, conflicts, invalid_tests = _test_facts(
        connection, sessions, cutoff, intervals, exact_cutoff,
    )
    fingerprint = hashlib.sha256(json.dumps(
        {
            "markers": [marker.fingerprint() for marker in markers],
            "test_evidence": [item.fingerprint() for item in test_evidence],
            "invalid_interval_timestamps": invalid_intervals,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    families = set(real_families)
    return AnnotationFacts(
        real_families[-1] if len(families) == 1 else None,
        len(families) > 1,
        invalid,
        invalid_intervals,
        len(valid),
        _instrumented(connection, project_id, sessions, cutoff, exact_cutoff),
        kind_counts["finish"],
        dict(sorted(kind_counts.items())),
        dict(sorted(cause_counts.items())),
        dict(sorted(scope_counts.items())),
        dict(sorted(outcome_counts.items())),
        dict(sorted(deterministic.items())),
        test_evidence,
        timeline,
        len(markers) - len(timeline),
        fingerprint,
        conflicts,
        invalid_tests,
    )
