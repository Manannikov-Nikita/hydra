"""One-shot canonical audit orchestration over public reconciled models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from .audit_builder import StorageHealthSnapshot, build_audit
from .audit_model import AuditReport
from .audit_renderers import (
    render_audit_html,
    render_audit_json,
    render_audit_markdown,
)
from .dashboard_contract import validate_task_report
from .exact_time import parse_exact_timestamp, public_timestamp, require_exact_timestamp
from .pilot import (
    PILOT_SCHEMA,
    PilotStatus,
    pilot_status,
    read_only_pilot_statuses,
    read_pilot_status,
)
from .project import resolve_project
from .project_schema import project_event_schema_counts
from .public_payload import reject_private_fields
from .reconcile_engine import RECONCILIATION_VERSION, list_reconciled_reports
from .reconcile_reports import (
    list_reconciled_reports as _list_reconciled_reports_after_refresh,
)
from .report_semantics import (
    SemanticAnnotationSummary,
    SemanticBreakdown,
    SemanticMarkerSummary,
    SemanticTokenFacts,
    TestEvidenceRow,
    TestEvidenceSummary,
    TrendAssessment,
)
from .reporting import (
    NumericFact,
    PilotHealth,
    TaskReport,
    TokenFacts,
    TrendInput,
)
from .rollout_identity import RolloutRoot
from .storage import HydraStore
from .storage_health import current_storage_health


def build_pilot_audit(
    store: HydraStore,
    *,
    project_id: str,
    pilot_id: str,
    refresh_enrollment: bool = True,
) -> AuditReport:
    """Build from PilotStatus and TaskReport only, never raw semantic content."""
    return _build_pilot_audit_with_health(
        store, project_id=project_id, pilot_id=pilot_id,
        refresh_enrollment=refresh_enrollment,
    )[0]


def _build_pilot_audit_with_health(
    store: HydraStore,
    *,
    project_id: str,
    pilot_id: str,
    refresh_enrollment: bool = True,
    materialized_only: bool = False,
) -> tuple[AuditReport, StorageHealthSnapshot]:
    def build() -> tuple[AuditReport, StorageHealthSnapshot]:
        transaction = (
            store.read_transaction
            if materialized_only
            else store.rollout_transaction
        )
        with transaction():
            if materialized_only:
                status, reports = _materialized_pilot_state(
                    store, project_id, pilot_id,
                )
            else:
                status_builder = pilot_status if refresh_enrollment else read_pilot_status
                status = status_builder(store, project_id, pilot_id)
                reports = (
                    _list_reconciled_reports_after_refresh(
                        store,
                        project_id,
                    )
                    if refresh_enrollment
                    else list_reconciled_reports(store, project_id)
                )
            task_refs = tuple(
                str(item["task_ref"]) for item in status.as_dict()["tasks"]
            )
            reports_by_ref = {report.task_ref: report for report in reports}
            try:
                reports = tuple(reports_by_ref[task_ref] for task_ref in task_refs)
            except KeyError as error:
                raise ValueError(
                    "pilot task collection lacks a reconciled public report"
                ) from error
            health = current_storage_health(store, project_id)
            return build_audit(status, reports, health), health

    if refresh_enrollment:
        return build()
    with read_only_pilot_statuses():
        return build()


def read_materialized_pilot_audit(
    store: HydraStore,
    *,
    project_id: str,
    pilot_id: str,
) -> AuditReport:
    """Read one audit from persisted public projections in a coherent snapshot."""
    return _build_pilot_audit_with_health(
        store,
        project_id=project_id,
        pilot_id=pilot_id,
        refresh_enrollment=False,
        materialized_only=True,
    )[0]


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _fact(value: object) -> NumericFact:
    row = _object(value, "numeric fact")
    return NumericFact(
        row["value"],
        str(row["unit"]),
        str(row["provenance"]),
        tuple(str(item) for item in row["caveats"]),  # type: ignore[union-attr]
        row["lower_bound"],
    )


def _token_facts(value: object) -> TokenFacts:
    row = _object(value, "token facts")
    return TokenFacts(*(
        _fact(row[name])
        for name in (
            "input", "cached_input", "output", "reasoning", "working",
            "full_context",
        )
    ))


def _semantic_annotations(value: object) -> SemanticAnnotationSummary:
    row = _object(value, "semantic annotations")

    def fact_group(name: str) -> dict[str, NumericFact]:
        return {
            str(key): _fact(fact)
            for key, fact in _object(row[name], name).items()
        }

    evidence = _object(row["test_evidence"], "test evidence")
    evidence_rows = tuple(
        TestEvidenceRow(
            str(item["scope"]),
            str(item["failure_cause"]),
            str(item["retry_kind"]),
            str(item["phase"]),
            str(item["cause"]),
            _fact(item["count"]),
        )
        for value in evidence["rows"]  # type: ignore[union-attr]
        for item in (_object(value, "test evidence row"),)
    )
    timeline = tuple(
        SemanticMarkerSummary(
            str(item["kind"]),
            str(item["phase"]),
            str(item["cause"]),
            str(item["scope_change"]),
            None if item["outcome"] is None else str(item["outcome"]),
            float(item["confidence"]),
            str(item["note"]),
            str(item["provenance"]),
        )
        for value in row["timeline"]  # type: ignore[union-attr]
        for item in (_object(value, "semantic marker"),)
    )
    return SemanticAnnotationSummary(
        _fact(row["total_count"]),
        fact_group("kind_counts"),
        fact_group("cause_counts"),
        fact_group("scope_change_counts"),
        fact_group("finish_outcome_counts"),
        fact_group("deterministic_test_causes"),
        TestEvidenceSummary(
            _fact(evidence["total_count"]),
            evidence_rows,
            tuple(str(item) for item in evidence["caveats"]),  # type: ignore[union-attr]
        ),
        timeline,
        _fact(row["truncated_count"]),
        tuple(str(item) for item in row["caveats"]),  # type: ignore[union-attr]
    )


def _semantic_breakdown(value: object, annotations: object) -> SemanticBreakdown:
    row = _object(value, "semantic breakdown")

    def tokens(candidate: object) -> SemanticTokenFacts:
        token_row = _object(candidate, "semantic token facts")
        return SemanticTokenFacts(*(
            _fact(token_row[name])
            for name in ("working", "full_context", "reasoning")
        ))

    return SemanticBreakdown(
        {
            str(phase): tokens(facts)
            for phase, facts in _object(row["phases"], "semantic phases").items()
        },
        tokens(row["unclassified"]),
        _fact(row["marker_count"]),
        _fact(row["self_report_missing"]),
        _semantic_annotations(annotations),
    )


def _task_report(value: object) -> TaskReport:
    validate_task_report(value)
    reject_private_fields(value)
    row = _object(value, "task report")
    timing = _object(row["timing"], "task timing")
    counts = _object(row["counts"], "task counts")
    semantic = _object(row["semantic"], "task semantic facts")
    health = _object(row["pilot_health"], "pilot health")
    trend = _object(row["trend"], "task trend")
    trend_input = _object(trend["input"], "trend input")
    trend_result = _object(trend["result"], "trend result")
    return TaskReport(
        str(row["schema_version"]),
        str(row["task_ref"]),
        str(row["status"]),
        str(row["last_activity_at"]),
        None if row["task_family"] is None else str(row["task_family"]),
        _token_facts(row["recorded_tokens"]),
        _token_facts(row["deduplicated_tokens"]),
        _fact(timing["wall_clock"]),
        _fact(timing["agent_time"]),
        *(_fact(counts[name]) for name in (
            "sessions", "subagents", "tool_calls", "instrumentation_calls",
            "file_reads", "file_writes", "test_runs", "targeted_test_runs",
            "full_test_runs", "test_retries",
        )),
        _fact(semantic["coverage"]),
        _semantic_breakdown(semantic["breakdown"], semantic["annotations"]),
        _fact(semantic["conflicts"]),
        _fact(semantic["schema_diagnostics"]),
        _fact(row["instrumentation_overhead"]),
        PilotHealth(
            *(_fact(health[name]) for name in (
                "task_count", "missing_marker_rate", "semantic_coverage",
                "self_report_missing", "semantic_conflicts",
                "instrumentation_calls", "instrumentation_overhead",
                "schema_diagnostics",
            )),
            str(health["status"]),
            bool(health["receipt_verified"]),
            tuple(str(item) for item in health["caveats"]),  # type: ignore[union-attr]
        ),
        TrendInput(
            str(trend_input["task_ref"]),
            (
                None
                if trend_input["task_family"] is None
                else str(trend_input["task_family"])
            ),
            bool(trend_input["completed"]),
            *(_fact(trend_input[name]) for name in (
                "working_tokens", "test_retries", "read_amplification",
                "review_fix_cycles", "compactions",
            )),
        ),
        TrendAssessment(
            bool(trend_result["warning"]),
            (
                None
                if trend_result["corroborating_signal"] is None
                else str(trend_result["corroborating_signal"])
            ),
            _fact(trend_result["baseline_working_tokens"]),
            _fact(trend_result["token_growth"]),
            _fact(trend_result["signal_growth"]),
            tuple(str(item) for item in trend_result["caveats"]),  # type: ignore[union-attr]
        ),
        None if row["display_name"] is None else str(row["display_name"]),
    )


def _materialized_reports(
    store: HydraStore,
    project_id: str,
    task_refs: tuple[str, ...],
    *,
    missing_error: type[ValueError] | type[KeyError] = ValueError,
) -> tuple[TaskReport, ...]:
    if not task_refs:
        return ()
    placeholders = ",".join("?" for _ in task_refs)
    rows = store.connection.execute(
        f"""SELECT task_ref,report_json FROM materialized_report_snapshots
              WHERE project_id=? AND task_ref IN ({placeholders})""",
        (project_id, *task_refs),
    )
    reports: dict[str, TaskReport] = {}
    for task_ref, serialized in rows:
        try:
            payload = json.loads(str(serialized))
        except (TypeError, ValueError) as error:
            raise ValueError("materialized task report is invalid") from error
        report = _task_report(payload)
        if report.task_ref != task_ref or report.task_ref in reports:
            raise ValueError("materialized task report identity is invalid")
        reports[report.task_ref] = report
    try:
        return tuple(reports[task_ref] for task_ref in task_refs)
    except KeyError as error:
        message = (
            "unknown materialized task reference"
            if missing_error is KeyError
            else "pilot task collection lacks a materialized public report"
        )
        raise missing_error(message) from error


def read_materialized_task_reports(
    store: HydraStore,
    project_id: str,
    task_refs: tuple[str, ...],
) -> tuple[TaskReport, ...]:
    """Read exactly the requested public reports without scanning task history."""
    if len(set(task_refs)) != len(task_refs):
        raise ValueError("materialized task references must be unique")
    return _materialized_reports(
        store, project_id, task_refs, missing_error=KeyError,
    )


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def _threshold_results(
    thresholds: Mapping[str, int | float],
    facts: Mapping[str, object],
) -> dict[str, bool]:
    coverage = facts["aggregate_coverage"]
    per_task = facts["minimum_task_coverage"]
    latency = facts["staging_latency_p95_ms"]
    return {
        "minimum_completed_tasks": (
            int(facts["eligible_tasks"]) >= thresholds["minimum_completed_tasks"]
        ),
        "minimum_enrollment": (
            float(facts["enrollment"]) >= thresholds["minimum_enrollment"]
        ),
        "maximum_delivery_failures": (
            int(facts["delivery_failures"]) <= thresholds["maximum_delivery_failures"]
        ),
        "maximum_finish_missing": (
            int(facts["finish_missing"]) <= thresholds["maximum_finish_missing"]
        ),
        "maximum_semantic_conflicts": (
            int(facts["semantic_conflicts"]) <= thresholds["maximum_semantic_conflicts"]
        ),
        "maximum_schema_diagnostics": (
            int(facts["schema_diagnostics"]) <= thresholds["maximum_schema_diagnostics"]
        ),
        "minimum_aggregate_coverage": (
            isinstance(coverage, (int, float))
            and coverage >= thresholds["minimum_aggregate_coverage"]
        ),
        "minimum_per_task_coverage": (
            isinstance(per_task, (int, float))
            and per_task >= thresholds["minimum_per_task_coverage"]
        ),
        "maximum_staging_latency_p95_ms": (
            isinstance(latency, int)
            and latency <= thresholds["maximum_staging_latency_p95_ms"]
        ),
    }


def _transport_latencies(
    store: HydraStore,
    project_id: str,
    task_ref: str,
    cutoff_at: str,
) -> list[int]:
    cutoff = require_exact_timestamp(cutoff_at, "pilot task cutoff")
    rows = store.connection.execute(
        """WITH RECURSIVE task_sessions(session_key) AS (
               SELECT root_key FROM reconciled_tasks
                WHERE project_id=? AND public_ref=?
               UNION
               SELECT edges.child_key FROM session_edges edges
                 JOIN task_sessions ON edges.parent_key=task_sessions.session_key
                 WHERE edges.confidence_kind!='ambiguous'
           )
           SELECT disposition,latency_ms,staged_at
             FROM annotation_transport_events
            WHERE project_id=? AND session_key IN (SELECT session_key FROM task_sessions)
            ORDER BY staged_order,transport_key""",
        (project_id, task_ref, project_id),
    )
    latencies: list[int] = []
    for disposition, latency, staged_at in rows:
        observed = parse_exact_timestamp(staged_at)
        if (
            observed is not None
            and observed <= cutoff
            and disposition == "accepted"
        ):
            latencies.append(int(latency))
    return latencies


def _materialized_pilot_state(
    store: HydraStore,
    project_id: str,
    pilot_id: str,
) -> tuple[PilotStatus, tuple[TaskReport, ...]]:
    from .reconcile_engine import require_source_fact_fence_current

    require_source_fact_fence_current(store.connection, project_id)
    run = store.connection.execute(
        """SELECT pilot_id,started_at,closed_at,target,task_family,thresholds_json,state
             FROM pilot_runs WHERE pilot_id=? AND project_id=?""",
        (pilot_id, project_id),
    ).fetchone()
    if run is None:
        raise ValueError("pilot is unavailable for this project")
    started = require_exact_timestamp(run["started_at"], "pilot start timestamp")
    closed = (
        None
        if run["closed_at"] is None
        else require_exact_timestamp(run["closed_at"], "pilot close timestamp")
    )
    thresholds_value = json.loads(str(run["thresholds_json"]))
    if not isinstance(thresholds_value, dict):
        raise ValueError("stored pilot thresholds are invalid")
    thresholds = {
        str(name): value
        for name, value in thresholds_value.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    task_rows = sorted(
        store.connection.execute(
            """SELECT task_ref,completed_at,task_family,scope_change,instrumented,
                      initial_missing,finish_missing,delivery_failures,
                      semantic_conflicts,schema_diagnostics,coverage_value,
                      accepted_transport_events,staging_latency_p95_ms,trend_eligible
                 FROM pilot_tasks WHERE pilot_id=?""",
            (pilot_id,),
        ),
        key=lambda row: (
            require_exact_timestamp(
                row["completed_at"], "pilot task completion timestamp",
            ).epoch_nanoseconds,
            str(row["task_ref"]),
        ),
    )
    task_refs = tuple(str(row["task_ref"]) for row in task_rows)
    reports = _materialized_reports(store, project_id, task_refs)
    reports_by_ref = {report.task_ref: report for report in reports}
    public_tasks: list[dict[str, object]] = []
    task_cutoffs: list[list[str]] = []
    all_latencies: list[int] = []
    denominators: list[int] = []
    classified = 0
    for row in task_rows:
        task_ref = str(row["task_ref"])
        report = reports_by_ref[task_ref]
        cutoff_exact = require_exact_timestamp(
            row["completed_at"], "pilot task completion timestamp",
        )
        working = report.deduplicated_tokens.working.value
        phase_values = tuple(
            report.semantic_breakdown.phases[phase].working.value
            for phase in report.semantic_breakdown.phases
        )
        if isinstance(working, int):
            denominators.append(working)
            if all(isinstance(value, int) for value in phase_values):
                classified += sum(int(value) for value in phase_values)
        latencies = _transport_latencies(
            store, project_id, task_ref, cutoff_exact.canonical,
        )
        all_latencies.extend(latencies)
        public_tasks.append({
            "task_ref": task_ref,
            "completed_at": report.last_activity_at,
            "task_family": row["task_family"],
            "scope_change": str(row["scope_change"]),
            "instrumented": bool(row["instrumented"]),
            "initial_missing": bool(row["initial_missing"]),
            "finish_missing": bool(row["finish_missing"]),
            "delivery_failures": int(row["delivery_failures"]),
            "semantic_conflicts": int(row["semantic_conflicts"]),
            "schema_diagnostics": int(row["schema_diagnostics"]),
            "coverage": row["coverage_value"],
            "accepted_transport_events": int(row["accepted_transport_events"]),
            "staging_latency_p95_ms": (
                None
                if row["staging_latency_p95_ms"] is None
                else int(row["staging_latency_p95_ms"])
            ),
            "trend_eligible": bool(row["trend_eligible"]),
        })
        task_cutoffs.append([task_ref, cutoff_exact.canonical])

    eligible_count = len(public_tasks)
    instrumented_count = sum(bool(item["instrumented"]) for item in public_tasks)
    denominator = sum(denominators) if len(denominators) == eligible_count else None
    coverage = (
        None
        if denominator is None
        else 0.0 if denominator == 0
        else classified / denominator
    )
    task_coverages = [item["coverage"] for item in public_tasks]
    minimum_coverage = (
        None
        if not task_coverages or any(value is None for value in task_coverages)
        else min(float(value) for value in task_coverages)
    )
    project_event_issues, attributed_event_issues = project_event_schema_counts(
        store.connection, project_id,
    )
    non_event_schema = sum(
        max(
            0,
            int(item["schema_diagnostics"])
            - attributed_event_issues.get(str(item["task_ref"]), 0),
        )
        for item in public_tasks
    )
    facts: dict[str, object] = {
        "eligible_tasks": eligible_count,
        "instrumented_tasks": instrumented_count,
        "enrollment": (
            0.0 if eligible_count == 0 else instrumented_count / eligible_count
        ),
        "initial_missing": sum(bool(item["initial_missing"]) for item in public_tasks),
        "finish_missing": sum(bool(item["finish_missing"]) for item in public_tasks),
        "delivery_failures": sum(int(item["delivery_failures"]) for item in public_tasks),
        "semantic_conflicts": sum(int(item["semantic_conflicts"]) for item in public_tasks),
        "schema_diagnostics": non_event_schema + project_event_issues,
        "aggregate_coverage": coverage,
        "minimum_task_coverage": minimum_coverage,
        "staging_latency_p95_ms": _p95(all_latencies),
        "token_overhead": None,
    }
    threshold_results = _threshold_results(thresholds, facts)
    transport_verified = all(threshold_results.values())
    binding = {
        "pilot_id": str(run["pilot_id"]),
        "started_at": started.canonical,
        "target": int(run["target"]),
        "task_family": str(run["task_family"]),
        "task_refs": [item["task_ref"] for item in public_tasks],
        "task_cutoffs": task_cutoffs,
        "reconciliation_version": RECONCILIATION_VERSION,
        "storage_schema_version": store.schema_version(),
        "thresholds": thresholds,
        "facts": facts,
        "tasks": public_tasks,
        "threshold_results": threshold_results,
        "transport_verified": transport_verified,
    }
    snapshot_digest = hashlib.sha256(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    receipt_row = store.connection.execute(
        "SELECT * FROM pilot_receipts WHERE pilot_id=?",
        (pilot_id,),
    ).fetchone()
    receipt: dict[str, object] | None = None
    receipt_verified = False
    if receipt_row is not None:
        receipt_created = parse_exact_timestamp(receipt_row["created_at"])
        receipt_current = (
            str(receipt_row["snapshot_digest"]) == snapshot_digest
            and str(run["state"]) == "closed"
            and closed is not None
            and receipt_created == closed
        )
        receipt_verified = receipt_current and receipt_row["decision"] == "verified"
        receipt = {
            "receipt_id": str(receipt_row["receipt_id"]),
            "decision": str(receipt_row["decision"]),
            "created_at": str(receipt_row["created_at"]),
            "audit_sha256": str(receipt_row["audit_sha256"]),
            "current": receipt_current,
        }
    return PilotStatus({
        "schema_version": PILOT_SCHEMA,
        "pilot": {
            "pilot_id": str(run["pilot_id"]),
            "started_at": public_timestamp(started.presentation),
            "closed_at": (
                None if closed is None else public_timestamp(closed.presentation)
            ),
            "target": int(run["target"]),
            "task_family": str(run["task_family"]),
            "state": str(run["state"]),
        },
        "reconciliation_version": RECONCILIATION_VERSION,
        "storage_schema_version": store.schema_version(),
        "thresholds": thresholds,
        "facts": facts,
        "tasks": public_tasks,
        "threshold_results": threshold_results,
        "transport_verified": transport_verified,
        "trend_ready": transport_verified and receipt_verified,
        "receipt": receipt,
        "snapshot_digest": snapshot_digest,
    }), reports


def render_pilot_audit(audit: AuditReport, output_format: str) -> str:
    renderers = {
        "json": render_audit_json,
        "markdown": render_audit_markdown,
        "html": render_audit_html,
    }
    try:
        renderer = renderers[output_format]
    except KeyError as error:
        raise ValueError("unsupported audit format") from error
    return renderer(audit)


def _default_rollout_roots(environ: Mapping[str, str]) -> tuple[RolloutRoot, ...]:
    """Legacy discovery helper retained only for explicit backfill/test setup."""
    home_value = environ.get("HOME")
    home = (
        Path(home_value).expanduser()
        if isinstance(home_value, str) and home_value
        else Path.home()
    )
    candidates = (
        (home / ".codex" / "sessions", "active"),
        (home / ".codex" / "archived_sessions", "archived"),
    )
    return tuple(
        RolloutRoot(path, label)
        for path, label in candidates
        if path.is_dir()
    )


def generate_audit(
    *,
    environ: Mapping[str, str],
    database_path: Path | None,
    installation_key_path: Path,
    cwd: Path,
    pilot_id: str,
    output_format: str,
    observed_at: datetime,
) -> str:
    """Render one audit from already materialized public state without writes."""
    _ = environ, installation_key_path, observed_at
    project = resolve_project(cwd)
    store = HydraStore.open_current(database_path)
    try:
        audit, _health = _build_pilot_audit_with_health(
            store,
            project_id=project.project_id,
            pilot_id=pilot_id,
            refresh_enrollment=False,
            materialized_only=True,
        )
        return render_pilot_audit(audit, output_format)
    finally:
        store.close()
