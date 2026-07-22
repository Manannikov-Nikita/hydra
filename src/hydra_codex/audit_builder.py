"""Build hydra.audit/v1 only from privacy-safe reconciled public models."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping

from .audit_coherence import validate_task_coherence
from .audit_comparability import comparability_readiness
from .audit_model import AuditEvidenceRegistry, AuditFact, AuditReport
from .pilot import PILOT_SCHEMA, PilotStatus
from .report_semantics import SEMANTIC_PHASES
from .reporting import NumericFact, TaskReport


_PILOT_ID = re.compile(r"hpilot_v1_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_FIELDS = frozenset({
    "absolute_path",
    "arguments",
    "capability",
    "command",
    "content",
    "database_path",
    "message",
    "path",
    "project_id",
    "prompt",
    "raw",
    "root",
    "root_id",
    "root_key",
    "session_id",
    "session_ids",
    "session_key",
    "turn_id",
    "turn_key",
})
_PILOT_FACT_UNITS = {
    "aggregate_coverage": "ratio",
    "delivery_failures": "count",
    "eligible_tasks": "count",
    "enrollment": "ratio",
    "finish_missing": "count",
    "initial_missing": "count",
    "instrumented_tasks": "count",
    "minimum_task_coverage": "ratio",
    "schema_diagnostics": "count",
    "semantic_conflicts": "count",
    "staging_latency_p95_ms": "milliseconds",
    "token_overhead": "tokens",
}
_PILOT_TASK_FACT_UNITS = {
    "accepted_transport_events": "count",
    "coverage": "ratio",
    "delivery_failures": "count",
    "finish_missing": "count",
    "initial_missing": "count",
    "instrumented": "count",
    "schema_diagnostics": "count",
    "semantic_conflicts": "count",
    "staging_latency_p95_ms": "milliseconds",
    "trend_eligible": "count",
}


@dataclass(frozen=True)
class StorageHealthSnapshot:
    """Read-only current storage facts; no growth or maintenance behavior."""

    database_bytes: int
    wal_bytes: int
    rollout_sources: int
    rollout_events: int
    codex_event_sources: int
    codex_events: int
    schema_version: int

    def __post_init__(self) -> None:
        for name in (
            "database_bytes",
            "wal_bytes",
            "rollout_sources",
            "rollout_events",
            "codex_event_sources",
            "codex_events",
            "schema_version",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("storage health facts must be non-negative integers")


def _reject_private_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _PRIVATE_FIELDS:
                raise ValueError("public audit input contains a private field")
            _reject_private_fields(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_private_fields(nested)


def _numeric(
    value: object,
    unit: str,
    *,
    unavailable_caveat: str,
    provenance: str = "derived",
) -> AuditFact:
    if value is None:
        return AuditFact(None, unit, "estimated", (unavailable_caveat,))
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, (int, float)):
        raise ValueError("pilot numeric fact has an invalid value")
    return AuditFact(value, unit, provenance)


def _sum_facts(values: Iterable[NumericFact], unit: str) -> AuditFact:
    supplied = tuple(values)
    lower_bound = sum(
        int(item.value if item.value is not None else item.lower_bound or 0)
        for item in supplied
    )
    caveats = tuple(dict.fromkeys(
        item for fact in supplied for item in fact.caveats
    ))
    if any(item.value is None for item in supplied):
        return AuditFact(
            None,
            unit,
            "estimated",
            caveats + ("aggregate_component_unavailable",),
            lower_bound=lower_bound,
        )
    provenance = (
        "derived"
        if all(item.provenance in {"exact", "derived"} for item in supplied)
        else "estimated"
    )
    return AuditFact(
        sum(item.value for item in supplied if item.value is not None),
        unit,
        provenance,
        caveats,
        lower_bound=lower_bound,
    )


def _register_pilot_facts(
    registry: AuditEvidenceRegistry,
    pilot_payload: dict[str, object],
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    raw_facts = pilot_payload.get("facts")
    raw_tasks = pilot_payload.get("tasks")
    if not isinstance(raw_facts, dict) or set(raw_facts) != set(_PILOT_FACT_UNITS):
        raise ValueError("pilot facts do not match the public pilot contract")
    if not isinstance(raw_tasks, list):
        raise ValueError("pilot task collection is invalid")
    facts = {
        name: registry.register(
            f"pilot.facts.{name}",
            _numeric(
                raw_facts[name],
                unit,
                unavailable_caveat=f"{name}_unavailable",
            ),
        )
        for name, unit in sorted(_PILOT_FACT_UNITS.items())
    }
    task_facts: dict[str, dict[str, str]] = {}
    for item in raw_tasks:
        if not isinstance(item, dict) or not isinstance(item.get("task_ref"), str):
            raise ValueError("pilot task collection is invalid")
        task_ref = str(item["task_ref"])
        task_facts[task_ref] = {
            name: registry.register(
                f"pilot.tasks.{task_ref}.{name}",
                _numeric(
                    item.get(name),
                    unit,
                    unavailable_caveat=f"{name}_unavailable",
                    provenance="derived",
                ),
            )
            for name, unit in sorted(_PILOT_TASK_FACT_UNITS.items())
        }
    return facts, task_facts


def _task_view(
    registry: AuditEvidenceRegistry,
    report: TaskReport,
    pilot_task: dict[str, object],
    pilot_refs: dict[str, str],
) -> dict[str, object]:
    prefix = f"tasks.{report.task_ref}."
    evidence_refs = {
        name: registry.register(prefix + name, fact)
        for name, fact in report.public_facts().items()
    }
    phases = [
        {
            "phase": phase,
            "working_tokens": evidence_refs[f"semantic.phase.{phase}.working"],
            "full_context_tokens": evidence_refs[f"semantic.phase.{phase}.full_context"],
            "reasoning_tokens": evidence_refs[f"semantic.phase.{phase}.reasoning"],
        }
        for phase in SEMANTIC_PHASES
    ]
    phases.append({
        "phase": "unclassified",
        "working_tokens": evidence_refs["semantic.unclassified.working"],
        "full_context_tokens": evidence_refs["semantic.unclassified.full_context"],
        "reasoning_tokens": evidence_refs["semantic.unclassified.reasoning"],
    })
    test_rows = []
    for index, row in enumerate(
        report.semantic_breakdown.annotations.test_evidence.rows,
        start=1,
    ):
        count_ref = registry.register(
            prefix + f"semantic.test_evidence.rows.{index}.count",
            row.count,
        )
        test_rows.append({
            "scope": row.scope,
            "failure_cause": row.failure_cause,
            "retry_kind": row.retry_kind,
            "phase": row.phase,
            "cause": row.cause,
            "count": count_ref,
        })
    markers = []
    for index, marker in enumerate(
        report.semantic_breakdown.annotations.timeline,
        start=1,
    ):
        confidence_ref = registry.register(
            prefix + f"semantic.markers.{index}.confidence",
            AuditFact(marker.confidence, "ratio", marker.provenance),
        )
        markers.append({
            "kind": marker.kind,
            "phase": marker.phase,
            "cause": marker.cause,
            "scope_change": marker.scope_change,
            "outcome": marker.outcome,
            "confidence": confidence_ref,
            "note": marker.note,
            "provenance": marker.provenance,
        })
    scope_change = str(pilot_task.get("scope_change", "none"))
    trend_eligible = bool(pilot_task.get("trend_eligible", False))
    comparability_caveats = []
    if report.task_family is None:
        comparability_caveats.append("task_family_unavailable")
    if scope_change in {"expanded", "redefined"}:
        comparability_caveats.append("scope_change_excluded")
    if not trend_eligible:
        comparability_caveats.append("automatic_comparison_excluded")
    if comparability_caveats:
        comparability_status = "excluded"
    elif report.trend_result.baseline_working_tokens.value is None:
        comparability_status = "unknown"
        comparability_caveats.append("insufficient_comparable_baseline")
    else:
        comparability_status = "ready"
    return {
        "task_ref": report.task_ref,
        "status": report.status,
        "last_activity_at": report.last_activity_at,
        "task_family": report.task_family,
        "headline": {
            "working_tokens": evidence_refs["deduplicated_working_tokens"],
            "wall_clock_ms": evidence_refs["wall_clock_ms"],
            "test_runs": evidence_refs["test_runs"],
            "semantic_coverage": evidence_refs["semantic_coverage"],
        },
        "phase_allocation": phases,
        "agent_topology": {
            "status": "aggregate_only",
            "sessions": evidence_refs["sessions"],
            "subagents": evidence_refs["subagents"],
            "caveats": ["identity_topology_unavailable"],
        },
        "tool_file_test": {
            "tool_calls": evidence_refs["tool_calls"],
            "instrumentation_calls": evidence_refs["instrumentation_calls"],
            "file_reads": evidence_refs["file_reads"],
            "file_writes": evidence_refs["file_writes"],
            "test_runs": evidence_refs["test_runs"],
            "targeted_test_runs": evidence_refs["targeted_test_runs"],
            "full_test_runs": evidence_refs["full_test_runs"],
            "test_retries": evidence_refs["test_retries"],
            "test_evidence": test_rows,
        },
        "issues": {
            "semantic_conflicts": evidence_refs["semantic_conflicts"],
            "schema_diagnostics": evidence_refs["schema_diagnostics"],
            "marker_count": evidence_refs["semantic.marker_count"],
            "self_report_missing": evidence_refs["semantic.self_report_missing"],
        },
        "comparability": {
            "status": comparability_status,
            "task_family": report.task_family,
            "scope_change": scope_change,
            "trend_eligible": trend_eligible,
            "baseline_working_tokens": evidence_refs[
                "trend.result.baseline_working_tokens"
            ],
            "warning": report.trend_result.warning,
            "corroborating_signal": report.trend_result.corroborating_signal,
            "caveats": comparability_caveats,
        },
        "semantic_markers": markers,
        "pilot_evidence_refs": pilot_refs,
        "evidence_refs": evidence_refs,
    }


def build_audit(
    pilot_status: PilotStatus,
    reports: tuple[TaskReport, ...],
    storage_health: StorageHealthSnapshot,
) -> AuditReport:
    """Build one deterministic collection audit without raw semantic queries."""
    if not isinstance(pilot_status, PilotStatus):
        raise ValueError("pilot status must be the public pilot model")
    if (
        not isinstance(reports, tuple)
        or any(not isinstance(item, TaskReport) for item in reports)
    ):
        raise ValueError("reports must be an immutable public task collection")
    if not isinstance(storage_health, StorageHealthSnapshot):
        raise ValueError("storage health must be a read-only snapshot")
    pilot_payload = pilot_status.as_dict()
    _reject_private_fields(pilot_payload)
    if pilot_payload.get("schema_version") != PILOT_SCHEMA:
        raise ValueError("unsupported pilot snapshot schema")
    pilot = pilot_payload.get("pilot")
    digest = pilot_payload.get("snapshot_digest")
    pilot_tasks = pilot_payload.get("tasks")
    if (
        not isinstance(pilot, dict)
        or _PILOT_ID.fullmatch(str(pilot.get("pilot_id"))) is None
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or not isinstance(pilot_tasks, list)
    ):
        raise ValueError("pilot snapshot metadata is invalid")
    task_refs = tuple(
        str(item.get("task_ref")) if isinstance(item, dict) else ""
        for item in pilot_tasks
    )
    report_by_ref = {item.task_ref: item for item in reports}
    if (
        len(report_by_ref) != len(reports)
        or len(set(task_refs)) != len(task_refs)
        or set(report_by_ref) != set(task_refs)
    ):
        raise ValueError("report task collection does not match the pilot snapshot")

    registry = AuditEvidenceRegistry()
    pilot_refs, pilot_task_refs = _register_pilot_facts(registry, pilot_payload)
    ordered_reports = tuple(report_by_ref[task_ref] for task_ref in task_refs)
    for index, report in enumerate(ordered_reports):
        validate_task_coherence(pilot_tasks[index], report)
    task_views = [
        _task_view(
            registry,
            report,
            pilot_tasks[index],
            pilot_task_refs[report.task_ref],
        )
        for index, report in enumerate(ordered_reports)
    ]
    headline = {
        "working_tokens": registry.register(
            "headline.working_tokens",
            _sum_facts(
                (item.deduplicated_tokens.working for item in ordered_reports),
                "tokens",
            ),
        ),
        "wall_clock_ms": registry.register(
            "headline.wall_clock_ms",
            _sum_facts((item.wall_clock for item in ordered_reports), "milliseconds"),
        ),
        "test_runs": registry.register(
            "headline.test_runs",
            _sum_facts((item.test_runs for item in ordered_reports), "count"),
        ),
        "semantic_coverage": pilot_refs["aggregate_coverage"],
    }
    aggregate_phases = []
    for phase in (*SEMANTIC_PHASES, "unclassified"):
        semantic_values = (
            (
                item.semantic_breakdown.unclassified
                if phase == "unclassified"
                else item.semantic_breakdown.phases[phase]
            )
            for item in ordered_reports
        )
        values = tuple(semantic_values)
        aggregate_phases.append({
            "phase": phase,
            **{
                f"{component}_tokens": registry.register(
                    f"cohort.phase.{phase}.{component}",
                    _sum_facts(
                        (getattr(value, component) for value in values),
                        "tokens",
                    ),
                )
                for component in ("working", "full_context", "reasoning")
            },
        })
    drain_ref = registry.register(
        "transport.pending_annotation_drain",
        AuditFact(
            None,
            "count",
            "estimated",
            ("host_context_unavailable", "pending_annotation_drain_not_attempted"),
        ),
    )
    storage_refs = {
        name: registry.register(
            f"storage.{name}",
            AuditFact(value, "bytes" if name.endswith("_bytes") else "count", "exact"),
        )
        for name, value in (
            ("database_bytes", storage_health.database_bytes),
            ("wal_bytes", storage_health.wal_bytes),
            ("rollout_sources", storage_health.rollout_sources),
            ("rollout_events", storage_health.rollout_events),
            ("codex_event_sources", storage_health.codex_event_sources),
            ("codex_events", storage_health.codex_events),
            ("schema_version", storage_health.schema_version),
        )
    }
    cohort = {
        "pilot_id": pilot["pilot_id"],
        "started_at": pilot.get("started_at"),
        "closed_at": pilot.get("closed_at"),
        "target": pilot.get("target"),
        "task_family": pilot.get("task_family"),
        "state": pilot.get("state"),
        "snapshot_digest": digest,
        "transport_verified": bool(pilot_payload.get("transport_verified")),
        "trend_ready": bool(pilot_payload.get("trend_ready")),
        "headline": headline,
        "phase_allocation": aggregate_phases,
        "transport": {
            "pending_annotation_drain": {
                "status": "unavailable",
                "evidence_id": drain_ref,
            },
            "evidence_refs": pilot_refs,
        },
    }
    collection = {
        "count": len(task_views),
        "comparability_readiness": comparability_readiness(
            task_views,
            transport_verified=bool(pilot_payload.get("transport_verified")),
            trend_ready=bool(pilot_payload.get("trend_ready")),
            receipt=pilot_payload.get("receipt"),
        ),
        "overview": [
            {
                "task_ref": item["task_ref"],
                "status": item["status"],
                "last_activity_at": item["last_activity_at"],
                "task_family": item["task_family"],
                **item["headline"],
            }
            for item in task_views
        ],
        "tasks": task_views,
    }
    storage_payload = {
        "snapshot": "current",
        "evidence_refs": storage_refs,
        "caveats": ["growth_baseline_unavailable"],
    }
    return AuditReport.create(
        pilot_snapshot=pilot_payload,
        cohort=cohort,
        collection=collection,
        storage_health=storage_payload,
        evidence_appendix=registry.evidence,
    )
