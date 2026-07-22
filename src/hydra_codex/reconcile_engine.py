"""Idempotent deterministic task reconciliation and privacy-safe queries."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3
from typing import Iterable

from .exact_time import ExactInstant, require_exact_timestamp
from .public_refs import project_public_references
from .reconcile_annotations import AnnotationFacts
from .reconcile_facts import (
    DeltaFact,
    SemanticAssembly,
    build_token_deltas,
    discover_task_plans,
    semantic_assembly,
)
from .reconcile_types import (
    ReconciledTask,
    ReconciliationSummary,
    SemanticTaskFacts,
    TaskPlan,
)
from .storage import HydraStore
from .rollout_reconcile import reconcile_turn_attempts
from .task_tree_storage import aggregate_stored_task_tree
from .task_tree_types import ScalarFact, TaskTreeMetrics
from .test_evidence import materialize_test_evidence, reconcile_test_retries


RECONCILIATION_VERSION = 1


class ReconciliationStale(RuntimeError):
    """Raised when source facts changed after the persisted reconciliation."""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _plan_instant(plan: TaskPlan) -> ExactInstant:
    if plan.cutoff_instant is None:
        raise ValueError("task plan is missing its exact cutoff")
    return plan.cutoff_instant


def _digest(key: bytes, domain: bytes, payload: bytes) -> str:
    return hmac.new(key, domain + payload, hashlib.sha256).hexdigest()


def _fact_fingerprint(value: ScalarFact) -> list[object]:
    return [value.value, value.known_lower_bound, value.provenance, list(value.caveats)]


def _task_fingerprint(
    plan: TaskPlan, metrics: TaskTreeMetrics, semantic: SemanticAssembly,
    deltas: Iterable[DeltaFact],
) -> dict[str, object]:
    unique = getattr(metrics, "unique")
    recorded = getattr(metrics, "recorded")
    scalar_names = (
        "sessions", "subagents", "root_wall_clock_ms", "agent_time_ms", "tool_calls",
        "instrumentation_calls", "file_reads", "file_writes", "test_runs",
        "targeted_test_runs", "full_test_runs", "test_retries",
    )
    scalar_facts = {
        name: [
            getattr(metrics, name).value, getattr(metrics, name).known_lower_bound,
            getattr(metrics, name).provenance, list(getattr(metrics, name).caveats),
        ]
        for name in scalar_names
    }
    return {
        "root": plan.root_key,
        "status": plan.status,
        "cutoff": _plan_instant(plan).canonical,
        "cutoff_timing_provenance": plan.cutoff_timing_provenance,
        "sessions": list(plan.session_ids),
        "recorded": list(recorded.vector.values),
        "unique": list(unique.vector.values),
        "recorded_provenance": [recorded.provenance, list(recorded.caveats)],
        "unique_provenance": [unique.provenance, list(unique.caveats)],
        "facts": scalar_facts,
        "family": semantic.task_family,
        "annotations": {
            "instrumented": semantic.annotations.instrumented,
            "finish_count": semantic.annotations.finish_count,
            "kind_counts": sorted(semantic.annotations.kind_counts.items()),
            "cause_counts": sorted(semantic.annotations.cause_counts.items()),
            "scope_counts": sorted(semantic.annotations.scope_change_counts.items()),
            "outcome_counts": sorted(semantic.annotations.finish_outcome_counts.items()),
            "deterministic_causes": sorted(semantic.annotations.deterministic_test_causes.items()),
            "test_evidence": [
                item.fingerprint() for item in semantic.annotations.test_evidence
            ],
            "timeline": [item.fingerprint() for item in semantic.annotations.timeline],
            "truncated": semantic.annotations.truncated_count,
            "source": semantic.annotations.source_fingerprint,
        },
        "coverage": semantic.coverage.value,
        "classified": semantic.classified_working,
        "unclassified": _fact_fingerprint(semantic.unclassified_working),
        "unclassified_full": _fact_fingerprint(semantic.unclassified_full_context),
        "unclassified_reasoning": _fact_fingerprint(semantic.unclassified_reasoning),
        "phases": [
            [phase, _fact_fingerprint(value)]
            for phase, value in sorted(semantic.phase_working.items())
        ],
        "phase_full": [
            [phase, _fact_fingerprint(value)]
            for phase, value in sorted(semantic.phase_full_context.items())
        ],
        "phase_reasoning": [
            [phase, _fact_fingerprint(value)]
            for phase, value in sorted(semantic.phase_reasoning.items())
        ],
        "deltas": [
            [
                item.event_key, item.provenance, item.phase, item.cause,
                list(item.vector.values),
            ]
            for item in deltas
        ],
        "markers": semantic.marker_count,
        "missing": semantic.self_report_missing,
        "conflicts": semantic.semantic_conflicts,
        "diagnostics": list(semantic.diagnostics),
        "diagnostic_counts": sorted(semantic.diagnostic_counts.items()),
    }


def _assemble_project(
    store: HydraStore, project_id: str,
) -> tuple[
    tuple[TaskPlan, ...],
    tuple[tuple[TaskPlan, TaskTreeMetrics, tuple[DeltaFact, ...], SemanticAssembly], ...],
    str,
]:
    plans = discover_task_plans(store.connection, project_id)
    assembled: list[tuple[TaskPlan, TaskTreeMetrics, tuple[DeltaFact, ...], SemanticAssembly]] = []
    fingerprints: list[dict[str, object]] = []
    for plan in plans:
        metrics = aggregate_stored_task_tree(
            store.connection, project_id=project_id, root_id=plan.root_key,
            cutoff_at=plan.cutoff_at,
            cutoff_instant=plan.cutoff_instant,
            cutoff_timing_provenance=plan.cutoff_timing_provenance,
            include_ambiguous_lineage=False,
        )
        deltas, diagnostics = build_token_deltas(store.connection, project_id, plan)
        semantic = semantic_assembly(
            store.connection, project_id, plan, metrics, deltas, diagnostics,
        )
        metrics = replace(metrics, semantic_coverage=semantic.coverage)
        assembled.append((plan, metrics, deltas, semantic))
        fingerprints.append(_task_fingerprint(plan, metrics, semantic, deltas))
    project_event_issues = [
        [str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4])]
        for row in store.connection.execute(
            """SELECT i.source_digest,i.source_ordinal,i.event_key,i.issue_code,i.provenance
                 FROM codex_event_issues i
                 JOIN codex_event_sources s ON s.source_digest=i.source_digest
                WHERE s.project_id=?
                ORDER BY i.source_digest,i.source_ordinal,i.event_key,i.issue_code""",
            (project_id,),
        )
    ]
    lifecycle_source_facts = [
        [str(row[0]), str(row[1]), row[2], row[3], str(row[4]), int(row[5])]
        for row in store.connection.execute(
            """SELECT e.event_key,e.event_kind,e.observed_at,e.timestamp_epoch,
                      e.logical_source_key,e.source_ordinal
                 FROM turn_lifecycle_events e
                 JOIN rollout_sessions s ON s.session_key=e.session_key
                WHERE s.project_id=?
                ORDER BY e.event_key""",
            (project_id,),
        )
    ]
    payload = json.dumps(
        {
            "tasks": fingerprints,
            "project_event_issues": project_event_issues,
            "lifecycle_source_facts": lifecycle_source_facts,
        },
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return plans, tuple(assembled), hashlib.sha256(payload).hexdigest()


def _persist_task(
    connection: sqlite3.Connection, project_id: str, public_ref: str,
    plan: TaskPlan, input_digest: str, deltas: Iterable[DeltaFact], semantic: SemanticAssembly,
) -> None:
    connection.execute(
        """INSERT INTO reconciled_tasks(
               project_id,root_key,public_ref,status,cutoff_at,last_activity_at,task_family,
               reconciliation_version,input_digest) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            project_id, plan.root_key, public_ref, plan.status,
            _plan_instant(plan).canonical, _plan_instant(plan).canonical,
            semantic.task_family, RECONCILIATION_VERSION, input_digest,
        ),
    )
    connection.executemany(
        """INSERT INTO reconciled_token_deltas(
               project_id,root_key,session_key,event_key,observed_at,ordinal,input_tokens,
               cached_input_tokens,output_tokens,reasoning_tokens,working_tokens,full_context,
               provenance,phase,cause) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            (
                project_id, plan.root_key, item.session_key, item.event_key,
                None if item.observed_at is None else _iso(item.observed_at), item.ordinal,
                item.vector.input_tokens, item.vector.cached_input_tokens,
                item.vector.output_tokens, item.vector.reasoning_output_tokens,
                item.vector.working_tokens, item.vector.full_context,
                item.provenance, item.phase, item.cause,
            )
            for item in deltas
        ),
    )
    phases = sorted(set(semantic.phase_working) | set(semantic.phase_full_context) | set(semantic.phase_reasoning))
    connection.executemany(
        """INSERT INTO reconciled_phase_metrics(
               project_id,root_key,phase,working_tokens,working_lower_bound,
               working_provenance,full_context,full_context_lower_bound,
               full_context_provenance,reasoning_tokens,reasoning_lower_bound,
               reasoning_provenance)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            (
                project_id, plan.root_key, phase,
                semantic.phase_working[phase].value,
                semantic.phase_working[phase].known_lower_bound,
                semantic.phase_working[phase].provenance,
                semantic.phase_full_context[phase].value,
                semantic.phase_full_context[phase].known_lower_bound,
                semantic.phase_full_context[phase].provenance,
                semantic.phase_reasoning[phase].value,
                semantic.phase_reasoning[phase].known_lower_bound,
                semantic.phase_reasoning[phase].provenance,
            )
            for phase in phases
        ),
    )
    connection.execute(
        """INSERT INTO reconciled_semantic_summaries(
               project_id,root_key,classified_working,unclassified_working,
               unclassified_working_lower_bound,unclassified_working_provenance,
               unclassified_full_context,unclassified_full_context_lower_bound,
               unclassified_full_context_provenance,unclassified_reasoning,
               unclassified_reasoning_lower_bound,unclassified_reasoning_provenance,
               coverage_value,coverage_provenance,marker_count,self_report_missing,
               semantic_conflicts,schema_diagnostics)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            project_id, plan.root_key, semantic.classified_working,
            semantic.unclassified_working.value,
            semantic.unclassified_working.known_lower_bound,
            semantic.unclassified_working.provenance,
            semantic.unclassified_full_context.value,
            semantic.unclassified_full_context.known_lower_bound,
            semantic.unclassified_full_context.provenance,
            semantic.unclassified_reasoning.value,
            semantic.unclassified_reasoning.known_lower_bound,
            semantic.unclassified_reasoning.provenance,
            semantic.coverage.value, semantic.coverage.provenance,
            semantic.marker_count, semantic.self_report_missing,
            semantic.semantic_conflicts, semantic.schema_diagnostics,
        ),
    )
    connection.executemany(
        """INSERT INTO reconciled_task_diagnostics(
               project_id,root_key,diagnostic_code,occurrence_count) VALUES (?,?,?,?)""",
        (
            (project_id, plan.root_key, code, count)
            for code, count in sorted(semantic.diagnostic_counts.items())
        ),
    )


def reconcile_project(
    store: HydraStore, project_id: str, installation_key: bytes,
) -> ReconciliationSummary:
    """Rebuild all derived task facts for one project in a single transaction."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be non-empty text")
    if not isinstance(installation_key, bytes) or len(installation_key) < 16:
        raise ValueError("installation_key must contain at least 16 bytes")
    with store.rollout_transaction() as connection:
        materialize_test_evidence(connection)
        reconcile_test_retries(connection)
        reconcile_turn_attempts(connection)
    plans, assembled, input_digest = _assemble_project(store, project_id)
    references = project_public_references((item.root_key for item in plans), installation_key)
    run_id = "hrec_v1_" + _digest(
        installation_key, b"hydra/reconcile-run/v1/",
        f"{project_id}/{RECONCILIATION_VERSION}/{input_digest}".encode("utf-8"),
    )[:32]
    completed_instant = max(
        (_plan_instant(item) for item in plans),
        default=require_exact_timestamp("1970-01-01T00:00:00Z"),
    )
    with store.rollout_transaction() as connection:
        connection.execute("DELETE FROM reconciled_tasks WHERE project_id=?", (project_id,))
        for plan, _metrics, deltas, semantic in assembled:
            _persist_task(
                connection, project_id, references[plan.root_key], plan,
                input_digest, deltas, semantic,
            )
        connection.execute(
            """INSERT INTO reconciliation_runs(
                   run_id,project_id,started_at,outcome,provenance,reconciliation_version,
                   input_digest,completed_at,task_count)
               VALUES (?,?,?,'success','derived',?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                   outcome='success',completed_at=excluded.completed_at,task_count=excluded.task_count""",
            (
                run_id, project_id, completed_instant.canonical,
                RECONCILIATION_VERSION, input_digest,
                completed_instant.canonical, len(plans),
            ),
        )
    complete_count = sum(item.status == "complete" for item in plans)
    return ReconciliationSummary(
        run_id, project_id, RECONCILIATION_VERSION, len(plans),
        complete_count, len(plans) - complete_count,
    )


def _semantic_from_store(
    connection: sqlite3.Connection, project_id: str, root_key: str,
    task_family: str | None, annotations: AnnotationFacts,
) -> SemanticTaskFacts:
    summary = connection.execute(
        """SELECT * FROM reconciled_semantic_summaries
            WHERE project_id=? AND root_key=?""",
        (project_id, root_key),
    ).fetchone()
    if summary is None:
        raise RuntimeError("reconciled semantic summary is missing")
    phase_rows = list(connection.execute(
        """SELECT phase,working_tokens,working_lower_bound,working_provenance,
                  full_context,full_context_lower_bound,full_context_provenance,
                  reasoning_tokens,reasoning_lower_bound,reasoning_provenance
             FROM reconciled_phase_metrics WHERE project_id=? AND root_key=? ORDER BY phase""",
        (project_id, root_key),
    ))
    diagnostics = tuple(row[0] for row in connection.execute(
        """SELECT diagnostic_code FROM reconciled_task_diagnostics
            WHERE project_id=? AND root_key=? ORDER BY diagnostic_code""",
        (project_id, root_key),
    ))
    if summary["coverage_value"] is None:
        caveats = (
            ("semantic_allocation_conflict",)
            if "semantic_allocation_exceeds_unique_tokens" in diagnostics
            else ("unknown_working_tokens",)
        )
    elif summary["coverage_provenance"] == "estimated":
        caveats = ("uncertain_unique_tokens",)
    else:
        caveats = ()
    coverage = ScalarFact(summary["coverage_value"], summary["coverage_provenance"], caveats)

    def phase_fact(row: sqlite3.Row, offset: int) -> ScalarFact:
        value = row[offset]
        return ScalarFact(
            value, str(row[offset + 2]),
            ("incomplete_phase_token_component",)
            if value is None else ("semantic_interval_allocation",),
            int(row[offset + 1]),
        )

    def unclassified_fact(component: str) -> ScalarFact:
        value = summary[f"unclassified_{component}"]
        provenance = str(summary[f"unclassified_{component}_provenance"])
        caveats = (
            ("semantic_component_unavailable",)
            if value is None else ("semantic_unclassified_remainder",) + (
                ("uncertain_unique_tokens",) if provenance == "estimated" else ()
            )
        )
        return ScalarFact(
            value, provenance, caveats,
            int(summary[f"unclassified_{component}_lower_bound"]),
        )

    return SemanticTaskFacts(
        task_family, annotations, coverage, int(summary["classified_working"]),
        unclassified_fact("working"),
        unclassified_fact("full_context"),
        unclassified_fact("reasoning"),
        {str(row[0]): phase_fact(row, 1) for row in phase_rows},
        {str(row[0]): phase_fact(row, 4) for row in phase_rows},
        {str(row[0]): phase_fact(row, 7) for row in phase_rows},
        int(summary["marker_count"]),
        int(summary["self_report_missing"]), int(summary["semantic_conflicts"]),
        int(summary["schema_diagnostics"]), diagnostics,
    )


def list_reconciled_tasks(
    store: HydraStore, project_id: str, last: int | None = None,
) -> tuple[ReconciledTask, ...]:
    """Return complete and incomplete tasks in deterministic recent-first order."""
    if last is not None and (isinstance(last, bool) or not isinstance(last, int) or last < 1):
        raise ValueError("last must be a positive integer")
    with store.rollout_transaction():
        rows = list(store.connection.execute(
            "SELECT * FROM reconciled_tasks WHERE project_id=?", (project_id,),
        ))
        plans, assembled, current_digest = _assemble_project(store, project_id)
        expected_roots = {item.root_key for item in plans}
        stored_roots = {str(row["root_key"]) for row in rows}
        reconciled = store.connection.execute(
            """SELECT 1 FROM reconciliation_runs
                 WHERE project_id=? AND reconciliation_version=? AND input_digest=?
                   AND outcome='success' LIMIT 1""",
            (project_id, RECONCILIATION_VERSION, current_digest),
        ).fetchone()
        if reconciled is None or expected_roots != stored_roots or any(
            row["input_digest"] != current_digest for row in rows
        ):
            raise ReconciliationStale("source facts changed; run reconcile before reporting")
        metrics_by_root = {item[0].root_key: item[1] for item in assembled}
        annotations_by_root = {item[0].root_key: item[3].annotations for item in assembled}
        rows.sort(key=lambda row: (
            -require_exact_timestamp(
                row["last_activity_at"], "stored task activity timestamp",
            ).epoch_nanoseconds,
            str(row["public_ref"]),
        ))
        if last is not None:
            rows = rows[:last]
        tasks: list[ReconciledTask] = []
        for row in rows:
            cutoff_instant = require_exact_timestamp(
                row["cutoff_at"], "stored task cutoff timestamp",
            )
            cutoff = cutoff_instant.presentation
            metrics = metrics_by_root[str(row["root_key"])]
            semantic = _semantic_from_store(
                store.connection, project_id, row["root_key"], row["task_family"],
                annotations_by_root[str(row["root_key"])],
            )
            tasks.append(ReconciledTask(
                str(row["public_ref"]), str(row["status"]), cutoff,
                replace(metrics, semantic_coverage=semantic.coverage), semantic,
                cutoff_instant,
            ))
        return tuple(tasks)


def get_reconciled_task(
    store: HydraStore, project_id: str, public_ref: str,
) -> ReconciledTask:
    """Resolve only an opaque public reference; private task keys are never accepted."""
    match = next(
        (item for item in list_reconciled_tasks(store, project_id=project_id) if item.public_ref == public_ref),
        None,
    )
    if match is None:
        raise KeyError("unknown public task reference")
    return match


def list_reconciled_reports(
    store: HydraStore, project_id: str, limit: int | None = None,
):
    from .reconcile_reports import list_reconciled_reports as build_reports

    return build_reports(store, project_id, limit)


def get_reconciled_report(
    store: HydraStore, project_id: str, public_ref: str,
):
    from .reconcile_reports import get_reconciled_report as find_report

    return find_report(store, project_id, public_ref)
