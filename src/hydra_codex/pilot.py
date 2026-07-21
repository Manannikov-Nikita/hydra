"""Persisted, privacy-safe pilot cohorts and verification receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .redaction import project_task_family, validate_task_family
from .reconcile_engine import RECONCILIATION_VERSION, list_reconciled_tasks
from .reconcile_facts import discover_task_plans
from .storage import HydraStore


PILOT_SCHEMA = "hydra.pilot/v1"
AUDIT_SCHEMA = "hydra.audit/v1"
_NANOSECONDS_PER_SECOND = 1_000_000_000
_RFC3339_NANOSECOND = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt]"
    r"(?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<offset>[Zz]|[+-]\d{2}:\d{2})$"
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("pilot clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored pilot timestamp is not timezone-aware")
    return parsed


def _datetime_nanoseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("pilot timestamp must be timezone-aware")
    observed = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed - epoch
    whole_seconds = delta.days * 86_400 + delta.seconds
    return (
        whole_seconds * _NANOSECONDS_PER_SECOND
        + observed.microsecond * 1_000
    )


def _stored_timestamp_nanoseconds(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _RFC3339_NANOSECOND.fullmatch(value)
    if match is None:
        return None
    offset = match.group("offset")
    if offset in {"Z", "z"}:
        offset = "+00:00"
    else:
        offset_hour = int(offset[1:3])
        offset_minute = int(offset[4:6])
        if (
            offset_hour > 23
            or offset_minute > 59
            or offset == "-00:00"
        ):
            return None
    try:
        observed = datetime.fromisoformat(
            f'{match.group("date")}T{match.group("clock")}{offset}'
        )
    except ValueError:
        return None
    fraction = match.group("fraction") or ""
    return (
        _datetime_nanoseconds(observed)
        + int(fraction.ljust(9, "0") or "0")
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def default_thresholds(target: int) -> dict[str, int | float]:
    return {
        "minimum_completed_tasks": max(5, target),
        "minimum_enrollment": 1.0,
        "maximum_delivery_failures": 0,
        "maximum_finish_missing": 0,
        "maximum_semantic_conflicts": 0,
        "maximum_schema_diagnostics": 0,
        "minimum_aggregate_coverage": 0.85,
        "minimum_per_task_coverage": 0.60,
        "maximum_staging_latency_p95_ms": 2000,
    }


@dataclass(frozen=True)
class PilotRun:
    pilot_id: str
    project_id: str
    started_at: datetime
    closed_at: datetime | None
    target: int
    task_family: str
    thresholds: dict[str, int | float]
    state: str


@dataclass(frozen=True)
class PilotStatus:
    payload: dict[str, object]

    @property
    def snapshot_digest(self) -> str:
        return str(self.payload["snapshot_digest"])

    def as_dict(self) -> dict[str, object]:
        return json.loads(_canonical(self.payload))


@dataclass(frozen=True)
class PilotReceipt:
    receipt_id: str
    pilot_id: str
    created_at: str
    decision: str
    task_refs: tuple[str, ...]
    reconciliation_version: int
    storage_schema_version: int
    thresholds: dict[str, int | float]
    observed_facts: dict[str, object]
    snapshot_digest: str
    audit_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "pilot_id": self.pilot_id,
            "created_at": self.created_at,
            "decision": self.decision,
            "task_refs": list(self.task_refs),
            "reconciliation_version": self.reconciliation_version,
            "storage_schema_version": self.storage_schema_version,
            "thresholds": dict(self.thresholds),
            "observed_facts": json.loads(_canonical(self.observed_facts)),
            "snapshot_digest": self.snapshot_digest,
            "audit_sha256": self.audit_sha256,
        }


def _run_from_row(row) -> PilotRun:
    return PilotRun(
        str(row["pilot_id"]),
        str(row["project_id"]),
        _timestamp(str(row["started_at"])),
        None if row["closed_at"] is None else _timestamp(str(row["closed_at"])),
        int(row["target"]),
        str(row["task_family"]),
        dict(json.loads(str(row["thresholds_json"]))),
        str(row["state"]),
    )


def start_pilot(
    store: HydraStore,
    *,
    project_id: str,
    target: int,
    task_family: str,
    now: datetime,
) -> PilotRun:
    """Start a project cohort, or resume its matching open run."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be non-empty")
    if isinstance(target, bool) or not isinstance(target, int) or target < 1:
        raise ValueError("target must be a positive integer")
    family = validate_task_family(task_family)
    started_at = _iso(now)
    thresholds = default_thresholds(target)
    thresholds_json = _canonical(thresholds)
    with store.rollout_transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM pilot_runs WHERE project_id=? AND state='open'",
            (project_id,),
        ).fetchone()
        if existing is not None:
            run = _run_from_row(existing)
            if run.target != target or run.task_family != family:
                raise ValueError("project already has a different open pilot")
            return run
        pilot_id = "hpilot_v1_" + hashlib.sha256(
            _canonical({
                "project_id": project_id,
                "started_at": started_at,
                "target": target,
                "task_family": family,
            }).encode("utf-8")
        ).hexdigest()[:32]
        connection.execute(
            """INSERT INTO pilot_runs(
                   pilot_id,project_id,started_at,closed_at,target,task_family,
                   thresholds_json,state)
               VALUES (?,?,?,NULL,?,?,?,'open')""",
            (pilot_id, project_id, started_at, target, family, thresholds_json),
        )
        return PilotRun(
            pilot_id, project_id, _timestamp(started_at), None, target, family,
            thresholds, "open",
        )


def _pilot_run(store: HydraStore, project_id: str, pilot_id: str) -> PilotRun:
    row = store.connection.execute(
        "SELECT * FROM pilot_runs WHERE pilot_id=? AND project_id=?",
        (pilot_id, project_id),
    ).fetchone()
    if row is None:
        raise ValueError("pilot is unavailable for this project")
    return _run_from_row(row)


def _scope_change(counts: dict[str, int]) -> str:
    if counts.get("redefined", 0):
        return "redefined"
    if counts.get("expanded", 0):
        return "expanded"
    return "none"


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def _task_sessions(
    store: HydraStore, project_id: str,
) -> dict[str, tuple[str, ...]]:
    roots_to_refs = {
        str(row[0]): str(row[1])
        for row in store.connection.execute(
            "SELECT root_key,public_ref FROM reconciled_tasks WHERE project_id=?",
            (project_id,),
        )
    }
    return {
        roots_to_refs[plan.root_key]: plan.session_ids
        for plan in discover_task_plans(store.connection, project_id)
        if plan.root_key in roots_to_refs
    }


def _initial_present(
    store: HydraStore,
    project_id: str,
    sessions: tuple[str, ...],
    cutoff_at: datetime,
) -> bool:
    placeholders = ",".join("?" for _ in sessions)
    cutoff_nanoseconds = _datetime_nanoseconds(cutoff_at)
    rows = store.connection.execute(
        f"""SELECT observed_at FROM annotations
              WHERE project_id=? AND session_id IN ({placeholders})
                AND kind='phase' AND phase='understand' AND cause='prompt'
                AND provenance='derived'""",
        (project_id, *sessions),
    )
    return any(
        observed is not None and observed <= cutoff_nanoseconds
        for (value,) in rows
        if (observed := _stored_timestamp_nanoseconds(value)) is not None
    )


def _transport_facts(
    store: HydraStore,
    project_id: str,
    sessions: tuple[str, ...],
    cutoff_at: datetime,
) -> tuple[int, int, list[int]]:
    placeholders = ",".join("?" for _ in sessions)
    cutoff_nanoseconds = _datetime_nanoseconds(cutoff_at)
    rows = store.connection.execute(
        f"""SELECT disposition,latency_ms,staged_at
               FROM annotation_transport_events
              WHERE project_id=? AND session_key IN ({placeholders})
              ORDER BY staged_order,transport_key""",
        (project_id, *sessions),
    )
    accepted = 0
    failures = 0
    latencies: list[int] = []
    for disposition, latency, staged_at in rows:
        observed = _stored_timestamp_nanoseconds(staged_at)
        if observed is None:
            failures += 1
            continue
        if observed > cutoff_nanoseconds:
            continue
        if disposition == "accepted":
            accepted += 1
            latencies.append(int(latency))
        else:
            failures += 1
    return accepted, failures, latencies


def _threshold_results(
    thresholds: dict[str, int | float], facts: dict[str, Any],
) -> dict[str, bool]:
    coverage = facts["aggregate_coverage"]
    per_task = facts["minimum_task_coverage"]
    latency = facts["staging_latency_p95_ms"]
    return {
        "minimum_completed_tasks": (
            facts["eligible_tasks"] >= thresholds["minimum_completed_tasks"]
        ),
        "minimum_enrollment": facts["enrollment"] >= thresholds["minimum_enrollment"],
        "maximum_delivery_failures": (
            facts["delivery_failures"] <= thresholds["maximum_delivery_failures"]
        ),
        "maximum_finish_missing": (
            facts["finish_missing"] <= thresholds["maximum_finish_missing"]
        ),
        "maximum_semantic_conflicts": (
            facts["semantic_conflicts"] <= thresholds["maximum_semantic_conflicts"]
        ),
        "maximum_schema_diagnostics": (
            facts["schema_diagnostics"] <= thresholds["maximum_schema_diagnostics"]
        ),
        "minimum_aggregate_coverage": (
            coverage is not None
            and coverage >= thresholds["minimum_aggregate_coverage"]
        ),
        "minimum_per_task_coverage": (
            per_task is not None
            and per_task >= thresholds["minimum_per_task_coverage"]
        ),
        "maximum_staging_latency_p95_ms": (
            latency is not None
            and latency <= thresholds["maximum_staging_latency_p95_ms"]
        ),
    }


def pilot_status(
    store: HydraStore,
    project_id: str,
    pilot_id: str,
    *,
    _window_end: datetime | None = None,
) -> PilotStatus:
    """Build and persist the current deterministic cohort snapshot."""
    run = _pilot_run(store, project_id, pilot_id)
    if _window_end is not None:
        _iso(_window_end)
        if run.closed_at is not None and _window_end != run.closed_at:
            raise ValueError("closed pilot window cannot be overridden")
    tasks = list_reconciled_tasks(store, project_id)
    sessions_by_ref = _task_sessions(store, project_id)
    window_end = run.closed_at if run.closed_at is not None else _window_end
    eligible = tuple(
        task for task in tasks
        if task.status == "complete"
        and task.last_activity_at > run.started_at
        and (window_end is None or task.last_activity_at <= window_end)
    )
    public_tasks: list[dict[str, object]] = []
    all_latencies: list[int] = []
    denominators: list[int] = []
    classified = 0
    with store.rollout_transaction() as connection:
        for task in sorted(
            eligible, key=lambda item: (item.last_activity_at, item.public_ref),
        ):
            sessions = sessions_by_ref.get(task.public_ref)
            if not sessions:
                raise RuntimeError("reconciled pilot task sessions are unavailable")
            cutoff = _iso(task.last_activity_at)
            instrumented = task.semantic.annotations.instrumented
            initial_missing = not _initial_present(
                store, project_id, sessions, task.last_activity_at,
            )
            finish_missing = task.semantic.annotations.finish_count == 0
            accepted, failures, latencies = _transport_facts(
                store, project_id, sessions, task.last_activity_at,
            )
            all_latencies.extend(latencies)
            family = (
                None
                if task.semantic.annotations.family_conflict
                else project_task_family(task.semantic.task_family)
            )
            scope = _scope_change(task.semantic.annotations.scope_change_counts)
            coverage = task.semantic.coverage.value
            working = task.metrics.unique.working_tokens
            if working is not None:
                denominators.append(working)
                classified += task.semantic.classified_working
            task_fact = {
                "task_ref": task.public_ref,
                "completed_at": cutoff,
                "task_family": family,
                "scope_change": scope,
                "instrumented": instrumented,
                "initial_missing": initial_missing,
                "finish_missing": finish_missing,
                "delivery_failures": failures,
                "semantic_conflicts": task.semantic.semantic_conflicts,
                "schema_diagnostics": task.semantic.schema_diagnostics,
                "coverage": coverage,
                "accepted_transport_events": accepted,
                "staging_latency_p95_ms": _p95(latencies),
                "trend_eligible": family is not None and scope == "none",
            }
            task_digest = hashlib.sha256(
                _canonical({
                    **task_fact,
                    "reconciliation_version": RECONCILIATION_VERSION,
                    "storage_schema_version": store.schema_version(),
                }).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """INSERT INTO pilot_tasks(
                       pilot_id,task_ref,completed_at,task_family,scope_change,
                       instrumented,initial_missing,finish_missing,delivery_failures,
                       semantic_conflicts,schema_diagnostics,coverage_value,
                       accepted_transport_events,staging_latency_p95_ms,
                       trend_eligible,task_input_digest)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pilot_id,task_ref) DO UPDATE SET
                       completed_at=excluded.completed_at,
                       task_family=excluded.task_family,
                       scope_change=excluded.scope_change,
                       instrumented=excluded.instrumented,
                       initial_missing=excluded.initial_missing,
                       finish_missing=excluded.finish_missing,
                       delivery_failures=excluded.delivery_failures,
                       semantic_conflicts=excluded.semantic_conflicts,
                       schema_diagnostics=excluded.schema_diagnostics,
                       coverage_value=excluded.coverage_value,
                       accepted_transport_events=excluded.accepted_transport_events,
                       staging_latency_p95_ms=excluded.staging_latency_p95_ms,
                       trend_eligible=excluded.trend_eligible,
                       task_input_digest=excluded.task_input_digest""",
                (
                    run.pilot_id, task.public_ref, cutoff, family, scope,
                    int(instrumented), int(initial_missing), int(finish_missing),
                    failures, task.semantic.semantic_conflicts,
                    task.semantic.schema_diagnostics, coverage, accepted,
                    _p95(latencies), int(family is not None and scope == "none"),
                    task_digest,
                ),
            )
            public_tasks.append(task_fact)

    eligible_count = len(public_tasks)
    instrumented_count = sum(bool(item["instrumented"]) for item in public_tasks)
    denominator = sum(denominators) if len(denominators) == eligible_count else None
    coverage = (
        None if denominator is None
        else 0.0 if denominator == 0
        else classified / denominator
    )
    task_coverages = [item["coverage"] for item in public_tasks]
    minimum_coverage = (
        None
        if not task_coverages or any(value is None for value in task_coverages)
        else min(float(value) for value in task_coverages)
    )
    facts: dict[str, Any] = {
        "eligible_tasks": eligible_count,
        "instrumented_tasks": instrumented_count,
        "enrollment": (
            0.0 if eligible_count == 0 else instrumented_count / eligible_count
        ),
        "initial_missing": sum(bool(item["initial_missing"]) for item in public_tasks),
        "finish_missing": sum(bool(item["finish_missing"]) for item in public_tasks),
        "delivery_failures": sum(int(item["delivery_failures"]) for item in public_tasks),
        "semantic_conflicts": sum(int(item["semantic_conflicts"]) for item in public_tasks),
        "schema_diagnostics": sum(int(item["schema_diagnostics"]) for item in public_tasks),
        "aggregate_coverage": coverage,
        "minimum_task_coverage": minimum_coverage,
        "staging_latency_p95_ms": _p95(all_latencies),
        "token_overhead": None,
    }
    threshold_results = _threshold_results(run.thresholds, facts)
    transport_verified = all(threshold_results.values())
    binding = {
        "pilot_id": run.pilot_id,
        "task_refs": [item["task_ref"] for item in public_tasks],
        "reconciliation_version": RECONCILIATION_VERSION,
        "storage_schema_version": store.schema_version(),
        "thresholds": run.thresholds,
        "facts": facts,
        "tasks": public_tasks,
        "threshold_results": threshold_results,
        "transport_verified": transport_verified,
    }
    snapshot_digest = hashlib.sha256(_canonical(binding).encode("utf-8")).hexdigest()
    receipt_row = store.connection.execute(
        "SELECT * FROM pilot_receipts WHERE pilot_id=?", (run.pilot_id,),
    ).fetchone()
    receipt: dict[str, object] | None = None
    receipt_current = False
    receipt_verified = False
    if receipt_row is not None:
        receipt_current = str(receipt_row["snapshot_digest"]) == snapshot_digest
        receipt_verified = receipt_current and receipt_row["decision"] == "verified"
        receipt = {
            "receipt_id": str(receipt_row["receipt_id"]),
            "decision": str(receipt_row["decision"]),
            "created_at": str(receipt_row["created_at"]),
            "audit_sha256": str(receipt_row["audit_sha256"]),
            "current": receipt_current,
        }
    payload: dict[str, object] = {
        "schema_version": PILOT_SCHEMA,
        "pilot": {
            "pilot_id": run.pilot_id,
            "started_at": _iso(run.started_at),
            "closed_at": None if run.closed_at is None else _iso(run.closed_at),
            "target": run.target,
            "task_family": run.task_family,
            "state": run.state,
        },
        "reconciliation_version": RECONCILIATION_VERSION,
        "storage_schema_version": store.schema_version(),
        "thresholds": run.thresholds,
        "facts": facts,
        "tasks": public_tasks,
        "threshold_results": threshold_results,
        "transport_verified": transport_verified,
        "trend_ready": transport_verified and receipt_verified,
        "receipt": receipt,
        "snapshot_digest": snapshot_digest,
    }
    return PilotStatus(payload)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("audit JSON contains a non-finite value")


class _DuplicateAuditKey(ValueError):
    pass


def _unique_audit_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateAuditKey(key)
        result[key] = value
    return result


def close_pilot(
    store: HydraStore,
    *,
    project_id: str,
    pilot_id: str,
    audit_json: Path,
    decision: str,
    now: datetime,
) -> PilotReceipt:
    """Close one cohort only after validating its exact live audit snapshot."""
    if decision not in {"verified", "rejected"}:
        raise ValueError("invalid pilot decision")
    created_at = _iso(now)
    path = Path(audit_json).expanduser()
    if not path.is_file():
        raise ValueError("audit JSON is unavailable")
    raw = path.read_bytes()
    try:
        audit = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_audit_object,
        )
    except (
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateAuditKey,
    ) as error:
        raise ValueError("audit JSON is invalid") from error
    if not isinstance(audit, dict) or audit.get("schema_version") != AUDIT_SCHEMA:
        raise ValueError("audit JSON has an unsupported schema")
    embedded = audit.get("pilot_snapshot")
    if not isinstance(embedded, dict):
        raise ValueError("audit JSON is missing its pilot snapshot")
    audit_sha256 = hashlib.sha256(raw).hexdigest()

    with store.rollout_transaction() as connection:
        run = _pilot_run(store, project_id, pilot_id)
        if run.state != "open":
            raise ValueError("pilot is already closed")
        if now < run.started_at:
            raise ValueError("pilot close time predates its start")
        if connection.execute(
            "SELECT 1 FROM pilot_receipts WHERE pilot_id=?", (pilot_id,),
        ).fetchone() is not None:
            raise ValueError("pilot already has a receipt")
        live = pilot_status(
            store, project_id, pilot_id, _window_end=now,
        ).as_dict()
        if _canonical(embedded) != _canonical(live):
            raise ValueError("audit pilot snapshot is stale or inconsistent")
        if embedded.get("snapshot_digest") != live["snapshot_digest"]:
            raise ValueError("audit pilot digest is inconsistent")
        if decision == "verified" and not bool(live["transport_verified"]):
            raise ValueError("pilot thresholds are not verified")

        tasks = live["tasks"]
        if not isinstance(tasks, list):
            raise RuntimeError("pilot task snapshot is invalid")
        task_refs = tuple(str(item["task_ref"]) for item in tasks)
        thresholds = dict(live["thresholds"])
        observed_facts = {
            "facts": live["facts"],
            "tasks": tasks,
            "threshold_results": live["threshold_results"],
            "transport_verified": live["transport_verified"],
        }
        receipt_id = "hreceipt_v1_" + hashlib.sha256(
            _canonical({
                "pilot_id": pilot_id,
                "created_at": created_at,
                "decision": decision,
                "snapshot_digest": live["snapshot_digest"],
                "audit_sha256": audit_sha256,
            }).encode("utf-8")
        ).hexdigest()[:32]
        connection.execute(
            """INSERT INTO pilot_receipts(
                   receipt_id,pilot_id,created_at,decision,task_refs_json,
                   reconciliation_version,schema_version,thresholds_json,
                   observed_facts_json,snapshot_digest,audit_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                receipt_id, pilot_id, created_at, decision,
                _canonical(task_refs), int(live["reconciliation_version"]),
                int(live["storage_schema_version"]), _canonical(thresholds),
                _canonical(observed_facts), str(live["snapshot_digest"]),
                audit_sha256,
            ),
        )
        connection.execute(
            """UPDATE pilot_runs SET state='closed',closed_at=?
                 WHERE pilot_id=? AND state='open'""",
            (created_at, pilot_id),
        )
        return PilotReceipt(
            receipt_id, pilot_id, created_at, decision, task_refs,
            int(live["reconciliation_version"]),
            int(live["storage_schema_version"]), thresholds, observed_facts,
            str(live["snapshot_digest"]), audit_sha256,
        )
