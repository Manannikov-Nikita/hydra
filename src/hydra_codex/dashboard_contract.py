"""Strict nested validators for browser-facing dashboard JSON."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import math
import re

from .contracts import AnnotationCause, AnnotationKind, Outcome, ScopeChange
from .diagnostics import DOCTOR_SCHEMA
from .redaction import project_task_family, redact_note
from .report_semantics import (
    FINISH_OUTCOMES,
    SCOPE_CHANGES,
    SEMANTIC_CAUSES,
    SEMANTIC_KINDS,
    SEMANTIC_PHASES,
    TEST_FAILURE_CAUSES,
    TEST_RETRY_KINDS,
    TEST_SCOPES,
    TEST_SEMANTIC_CAUSES,
    TEST_SEMANTIC_PHASES,
)
from .reporting import NumericFact, REPORT_SCHEMA
from .task_tree_types import validate_provenance


FACT_KEYS = frozenset({"value", "unit", "provenance", "caveats", "lower_bound"})
TOKEN_KEYS = frozenset({
    "input", "cached_input", "output", "reasoning", "working", "full_context",
})
COUNT_KEYS = frozenset({
    "sessions", "subagents", "tool_calls", "instrumentation_calls",
    "file_reads", "file_writes", "test_runs", "targeted_test_runs",
    "full_test_runs", "test_retries",
})
_TASK_REF = re.compile(r"task_[0-9a-f]{1,64}\Z")
_PROJECT_REF = re.compile(r"project_[0-9a-f]{12,64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_DOCTOR_CODES = (
    "project_resolution", "storage_available", "schema_current",
    "foreign_keys_ok", "integrity_ok", "storage_permissions_restricted",
)


def _object(value: object, keys: set[str] | frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{label} has an invalid schema")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} requires text keys")
    return value


def _array(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    return value


def _text(value: object, label: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")


def _timestamp(value: object, label: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    _text(value, label)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _task_ref(value: object, label: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or _TASK_REF.fullmatch(value) is None:
        raise ValueError(f"{label} must be an opaque task reference")


def _project_ref(value: object, label: str) -> None:
    if not isinstance(value, str) or _PROJECT_REF.fullmatch(value) is None:
        raise ValueError(f"{label} must be an opaque project reference")


def _task_family(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or project_task_family(value) != value:
        raise ValueError(f"{label} must be privacy-safe")


def validate_numeric_fact(value: object, unit: str | None = None) -> None:
    fact = _object(value, FACT_KEYS, "numeric fact")
    caveats = fact["caveats"]
    if not isinstance(caveats, list):
        raise ValueError("numeric fact caveats must be an array")
    supplied_unit = fact["unit"]
    if supplied_unit == "bytes":
        for candidate in (fact["value"], fact["lower_bound"]):
            if candidate is not None and (
                isinstance(candidate, bool)
                or not isinstance(candidate, (int, float))
                or not math.isfinite(candidate)
            ):
                raise ValueError("byte fact values must be finite numbers or null")
        validate_provenance(str(fact["provenance"]))
        if fact["value"] is None and fact["provenance"] != "estimated":
            raise ValueError("unavailable byte facts must use estimated provenance")
        if any(
            not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None
            for item in caveats
        ):
            raise ValueError("byte fact caveats are invalid")
        lower = fact["lower_bound"]
        current = fact["value"]
        if lower is not None and lower < 0:
            raise ValueError("byte fact lower bound must be non-negative")
        if current is not None and current >= 0 and lower is not None and lower > current:
            raise ValueError("byte fact lower bound cannot exceed value")
        parsed_unit = "bytes"
    else:
        parsed = NumericFact(
            fact["value"],
            str(supplied_unit),
            str(fact["provenance"]),
            tuple(caveats),
            fact["lower_bound"],
        )
        parsed_unit = parsed.unit
    if unit is not None and parsed_unit != unit:
        raise ValueError("numeric fact has an invalid unit")


def _validate_fact_group(
    value: object, names: set[str] | frozenset[str], unit: str,
) -> None:
    group = _object(value, names, "numeric fact group")
    for fact in group.values():
        validate_numeric_fact(fact, unit)


def _validate_doctor(value: object) -> None:
    report = _object(value, {"schema_version", "status", "checks"}, "doctor report")
    if report["schema_version"] != DOCTOR_SCHEMA or report["status"] not in {"healthy", "degraded"}:
        raise ValueError("doctor report metadata is invalid")
    checks = _array(report["checks"], "doctor checks")
    if len(checks) != len(_DOCTOR_CODES):
        raise ValueError("doctor checks must be complete")
    observed_codes: list[str] = []
    for item in checks:
        check = _object(item, {"code", "status"}, "doctor check")
        _text(check["code"], "doctor code")
        observed_codes.append(str(check["code"]))
        if check["status"] not in {"ok", "failed", "unavailable"}:
            raise ValueError("doctor status is invalid")
    if tuple(observed_codes) != _DOCTOR_CODES:
        raise ValueError("doctor checks must be complete and ordered")


def _validate_phase_allocation(value: object) -> None:
    allocation = _object(value, {"working", "full_context", "reasoning"}, "phase allocation")
    for fact in allocation.values():
        validate_numeric_fact(fact, "tokens")


def validate_semantic_breakdown(value: object) -> None:
    breakdown = _object(
        value, {"phases", "unclassified", "self_report_missing", "marker_count"},
        "semantic breakdown",
    )
    phases = _object(breakdown["phases"], set(SEMANTIC_PHASES), "semantic phases")
    for allocation in phases.values():
        _validate_phase_allocation(allocation)
    _validate_phase_allocation(breakdown["unclassified"])
    validate_numeric_fact(breakdown["self_report_missing"], "count")
    validate_numeric_fact(breakdown["marker_count"], "count")


def _validate_annotations(value: object) -> None:
    annotations = _object(value, {
        "total_count", "kind_counts", "cause_counts", "scope_change_counts",
        "finish_outcome_counts", "deterministic_test_causes", "test_evidence",
        "timeline", "truncated_count", "caveats",
    }, "semantic annotations")
    validate_numeric_fact(annotations["total_count"], "count")
    validate_numeric_fact(annotations["truncated_count"], "count")
    for field, names in (
        ("kind_counts", {item.value for item in AnnotationKind}),
        ("cause_counts", {item.value for item in AnnotationCause}),
        ("scope_change_counts", {item.value for item in ScopeChange}),
        ("finish_outcome_counts", {item.value for item in Outcome}),
        ("deterministic_test_causes", {"test_failure", "infra_failure"}),
    ):
        _validate_fact_group(annotations[field], names, "count")
    evidence = _object(
        annotations["test_evidence"], {"total_count", "rows", "caveats"},
        "test evidence",
    )
    validate_numeric_fact(evidence["total_count"], "count")
    _array(evidence["caveats"], "test evidence caveats")
    for item in _array(evidence["rows"], "test evidence rows"):
        row = _object(item, {
            "scope", "failure_cause", "retry_kind", "phase", "cause", "count",
        }, "test evidence row")
        for field in ("scope", "failure_cause", "retry_kind", "phase", "cause"):
            _text(row[field], f"test evidence {field}")
        if (
            row["scope"] not in TEST_SCOPES
            or row["failure_cause"] not in TEST_FAILURE_CAUSES
            or row["retry_kind"] not in TEST_RETRY_KINDS
            or row["phase"] not in TEST_SEMANTIC_PHASES
            or row["cause"] not in TEST_SEMANTIC_CAUSES
        ):
            raise ValueError("test evidence vocabulary is invalid")
        validate_numeric_fact(row["count"], "count")
    for item in _array(annotations["timeline"], "semantic timeline"):
        marker = _object(item, {
            "kind", "phase", "cause", "scope_change", "outcome", "confidence",
            "note", "provenance",
        }, "semantic marker")
        for field in ("kind", "phase", "cause", "scope_change", "note", "provenance"):
            _text(marker[field], f"semantic marker {field}")
        _text(marker["outcome"], "semantic marker outcome", allow_none=True)
        if (
            marker["kind"] not in SEMANTIC_KINDS
            or marker["phase"] not in SEMANTIC_PHASES
            or marker["cause"] not in SEMANTIC_CAUSES
            or marker["scope_change"] not in SCOPE_CHANGES
            or marker["outcome"] is not None and marker["outcome"] not in FINISH_OUTCOMES
            or marker["provenance"] != "model_reported"
            or redact_note(str(marker["note"])) != marker["note"]
        ):
            raise ValueError("semantic marker vocabulary is invalid")
        confidence = marker["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("semantic marker confidence is invalid")
    _array(annotations["caveats"], "semantic annotation caveats")


def validate_task_report(value: object) -> None:
    report = _object(value, {
        "schema_version", "task_ref", "status", "last_activity_at", "task_family",
        "recorded_tokens", "deduplicated_tokens", "timing", "counts", "semantic",
        "instrumentation_overhead", "pilot_health", "trend",
    }, "task report")
    if report["schema_version"] != REPORT_SCHEMA:
        raise ValueError("task report schema is invalid")
    _task_ref(report["task_ref"], "task report task_ref")
    if report["status"] not in {"complete", "incomplete"}:
        raise ValueError("task report status is invalid")
    _timestamp(report["last_activity_at"], "task report last_activity_at")
    _task_family(report["task_family"], "task family")
    _validate_fact_group(report["recorded_tokens"], TOKEN_KEYS, "tokens")
    _validate_fact_group(report["deduplicated_tokens"], TOKEN_KEYS, "tokens")
    timing = _object(report["timing"], {"wall_clock", "agent_time"}, "task timing")
    for fact in timing.values():
        validate_numeric_fact(fact, "milliseconds")
    _validate_fact_group(report["counts"], COUNT_KEYS, "count")
    semantic = _object(
        report["semantic"],
        {"coverage", "breakdown", "annotations", "conflicts", "schema_diagnostics"},
        "task semantic data",
    )
    validate_numeric_fact(semantic["coverage"], "ratio")
    validate_semantic_breakdown(semantic["breakdown"])
    _validate_annotations(semantic["annotations"])
    validate_numeric_fact(semantic["conflicts"], "count")
    validate_numeric_fact(semantic["schema_diagnostics"], "count")
    validate_numeric_fact(report["instrumentation_overhead"], "tokens")
    health = _object(report["pilot_health"], {
        "task_count", "missing_marker_rate", "semantic_coverage", "self_report_missing",
        "semantic_conflicts", "instrumentation_calls", "instrumentation_overhead",
        "schema_diagnostics", "status", "receipt_verified", "caveats",
    }, "pilot health")
    for field in ("task_count", "self_report_missing", "semantic_conflicts", "instrumentation_calls", "schema_diagnostics"):
        validate_numeric_fact(health[field], "count")
    for field in ("missing_marker_rate", "semantic_coverage"):
        validate_numeric_fact(health[field], "ratio")
    validate_numeric_fact(health["instrumentation_overhead"], "tokens")
    if not isinstance(health["receipt_verified"], bool):
        raise ValueError("pilot receipt flag is invalid")
    _text(health["status"], "pilot health status")
    _array(health["caveats"], "pilot health caveats")
    _validate_trend(report["trend"])
    trend_input = report["trend"]["input"]  # type: ignore[index]
    assert isinstance(trend_input, Mapping)
    if (
        trend_input["task_ref"] != report["task_ref"]
        or trend_input["completed"] != (report["status"] == "complete")
        or trend_input["task_family"] not in {None, report["task_family"]}
    ):
        raise ValueError("trend input must describe the selected task")


def _validate_trend(value: object) -> None:
    trend = _object(value, {"input", "result"}, "task trend")
    inputs = _object(trend["input"], {
        "task_ref", "task_family", "completed", "working_tokens", "test_retries",
        "read_amplification", "review_fix_cycles", "compactions",
    }, "trend input")
    _task_ref(inputs["task_ref"], "trend task_ref")
    _task_family(inputs["task_family"], "trend task family")
    if not isinstance(inputs["completed"], bool):
        raise ValueError("trend completion flag is invalid")
    validate_numeric_fact(inputs["working_tokens"], "tokens")
    for field in ("test_retries", "read_amplification", "review_fix_cycles", "compactions"):
        validate_numeric_fact(inputs[field], "count")
    result = _object(trend["result"], {
        "warning", "token_growth", "corroborating_signal", "signal_growth",
        "baseline_working_tokens", "caveats",
    }, "trend result")
    if not isinstance(result["warning"], bool):
        raise ValueError("trend warning flag is invalid")
    _text(result["corroborating_signal"], "trend signal", allow_none=True)
    validate_numeric_fact(result["token_growth"], "tokens")
    validate_numeric_fact(result["signal_growth"], "count")
    validate_numeric_fact(result["baseline_working_tokens"], "tokens")
    _array(result["caveats"], "trend caveats")


_PILOT_KEYS = frozenset({
    "state", "task_family", "started_at", "closed_at", "target", "eligible_tasks",
    "instrumented_tasks", "enrollment", "aggregate_coverage", "minimum_task_coverage",
    "staging_latency_p95_ms", "token_overhead", "transport_verified", "trend_ready",
    "threshold_results",
})
_STORAGE_FIELDS = frozenset({
    "database_bytes", "wal_bytes", "rollout_sources", "rollout_events",
    "codex_event_sources", "codex_events", "schema_version",
})


def _validate_pilot(value: object) -> None:
    pilot = _object(value, _PILOT_KEYS, "dashboard pilot")
    for field in ("state", "started_at"):
        _text(pilot[field], f"dashboard pilot {field}")
    if pilot["state"] not in {"open", "closed"}:
        raise ValueError("dashboard pilot state is invalid")
    _timestamp(pilot["started_at"], "dashboard pilot started_at")
    _timestamp(pilot["closed_at"], "dashboard pilot closed_at", allow_none=True)
    _task_family(pilot["task_family"], "dashboard pilot task family")
    for field in ("target", "eligible_tasks", "instrumented_tasks"):
        validate_numeric_fact(pilot[field], "count")
    for field in ("enrollment", "aggregate_coverage", "minimum_task_coverage"):
        validate_numeric_fact(pilot[field], "ratio")
    validate_numeric_fact(pilot["staging_latency_p95_ms"], "milliseconds")
    validate_numeric_fact(pilot["token_overhead"], "tokens")
    if not isinstance(pilot["transport_verified"], bool) or not isinstance(pilot["trend_ready"], bool):
        raise ValueError("dashboard pilot flags are invalid")
    thresholds = pilot["threshold_results"]
    if not isinstance(thresholds, Mapping) or any(
        not isinstance(key, str) or not isinstance(result, bool)
        for key, result in thresholds.items()
    ):
        raise ValueError("dashboard pilot threshold results are invalid")


def _validate_storage_group(value: object, *, allow_none: bool, growth: bool = False) -> None:
    if value is None and allow_none:
        return
    fields = _STORAGE_FIELDS - ({"schema_version"} if growth else set())
    group = _object(value, fields, "dashboard storage group")
    for field, fact in group.items():
        validate_numeric_fact(fact, "bytes" if field.endswith("_bytes") else "count")


def _validate_storage(value: object) -> None:
    storage = _object(
        value, {"baseline_state", "current", "baseline", "growth", "diagnostics"},
        "dashboard storage",
    )
    if storage["baseline_state"] not in {"available", "unavailable"}:
        raise ValueError("dashboard storage baseline state is invalid")
    _validate_storage_group(storage["current"], allow_none=False)
    _validate_storage_group(storage["baseline"], allow_none=True)
    _validate_storage_group(storage["growth"], allow_none=True, growth=True)
    if storage["baseline_state"] == "unavailable" and (
        storage["baseline"] is not None or storage["growth"] is not None
    ):
        raise ValueError("unavailable storage baseline cannot carry facts")
    if storage["baseline_state"] == "available" and (
        storage["baseline"] is None or storage["growth"] is None
    ):
        raise ValueError("available storage baseline requires facts")
    for item in _array(storage["diagnostics"], "storage diagnostics"):
        diagnostic = _object(item, {"code", "severity"}, "storage diagnostic")
        _text(diagnostic["code"], "storage diagnostic code")
        _text(diagnostic["severity"], "storage diagnostic severity")


def validate_project_payload(value: object) -> None:
    project = _object(value, {
        "project_ref", "display_name", "last_activity_at", "freshness_state",
        "overview", "recent_tasks", "pilot", "storage", "system_health",
    }, "dashboard project")
    _project_ref(project["project_ref"], "dashboard project project_ref")
    _text(project["display_name"], "dashboard project display_name")
    if not str(project["display_name"]).strip() or len(str(project["display_name"])) > 120:
        raise ValueError("dashboard project display_name is invalid")
    if project["freshness_state"] not in {"current", "stale", "refreshing", "unavailable"}:
        raise ValueError("dashboard project freshness state is invalid")
    _timestamp(project["last_activity_at"], "dashboard project activity", allow_none=True)
    overview = _object(project["overview"], {"basis", "headline", "phase_allocation"}, "dashboard overview")
    basis = _object(overview["basis"], {"kind", "task_ref"}, "dashboard overview basis")
    if basis["kind"] != "latest_task":
        raise ValueError("dashboard overview basis is invalid")
    _task_ref(basis["task_ref"], "dashboard overview task_ref", allow_none=True)
    headline = _object(
        overview["headline"],
        {"working_tokens", "full_context_tokens", "wall_clock_ms"},
        "dashboard headline",
    )
    validate_numeric_fact(headline["working_tokens"], "tokens")
    validate_numeric_fact(headline["full_context_tokens"], "tokens")
    validate_numeric_fact(headline["wall_clock_ms"], "milliseconds")
    if overview["phase_allocation"] is not None:
        validate_semantic_breakdown(overview["phase_allocation"])
    recent_tasks = _array(project["recent_tasks"], "recent tasks")
    if len(recent_tasks) > 10:
        raise ValueError("dashboard recent tasks are not bounded")
    for item in recent_tasks:
        recent = _object(item, {"task_ref", "status", "last_activity_at", "task_family", "headline"}, "recent task")
        _task_ref(recent["task_ref"], "recent task task_ref")
        if recent["status"] not in {"complete", "incomplete"}:
            raise ValueError("recent task status is invalid")
        _timestamp(recent["last_activity_at"], "recent task last_activity_at")
        _task_family(recent["task_family"], "recent task family")
        recent_headline = _object(
            recent["headline"],
            {"working_tokens", "full_context_tokens", "wall_clock_ms"},
            "recent task headline",
        )
        validate_numeric_fact(recent_headline["working_tokens"], "tokens")
        validate_numeric_fact(recent_headline["full_context_tokens"], "tokens")
        validate_numeric_fact(recent_headline["wall_clock_ms"], "milliseconds")
    if project["pilot"] is not None:
        _validate_pilot(project["pilot"])
    _validate_storage(project["storage"])
    health = _object(project["system_health"], {"scope", "doctor"}, "system health")
    if health["scope"] != "global_launch_context":
        raise ValueError("system health scope is invalid")
    _validate_doctor(health["doctor"])


def validate_freshness(value: object) -> None:
    freshness = _object(value, {"state", "doctor"}, "dashboard freshness")
    if freshness["state"] not in {"current", "stale", "refreshing", "unavailable"}:
        raise ValueError("dashboard freshness state is invalid")
    doctor = _object(freshness["doctor"], {"scope", "report"}, "dashboard freshness doctor")
    if doctor["scope"] != "global_launch_context":
        raise ValueError("dashboard freshness doctor scope is invalid")
    _validate_doctor(doctor["report"])
