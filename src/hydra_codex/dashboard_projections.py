"""Dashboard-only projections for canonical pilot and storage contracts."""

from __future__ import annotations

from collections.abc import Mapping
import math

from .reporting import NumericFact
from .storage_health import StorageStatus


def _fact(
    value: object,
    unit: str,
    *,
    provenance: str = "derived",
    unavailable: str,
) -> dict[str, object]:
    if unit == "bytes":
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return {
                "value": None, "unit": unit, "provenance": "estimated",
                "caveats": [unavailable], "lower_bound": None,
            }
        return {
            "value": value, "unit": unit, "provenance": provenance,
            "caveats": [], "lower_bound": None,
        }
    if value is None:
        return NumericFact(None, unit, "estimated", (unavailable,)).as_dict()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return NumericFact(None, unit, "estimated", (unavailable,)).as_dict()
    return NumericFact(value, unit, provenance).as_dict()


def project_pilot_status(payload: Mapping[str, object]) -> dict[str, object]:
    """Keep only dashboard pilot readiness fields and wrap every number."""
    pilot = payload.get("pilot")
    facts = payload.get("facts")
    if not isinstance(pilot, Mapping) or not isinstance(facts, Mapping):
        raise ValueError("canonical pilot status is malformed")
    threshold_results = payload.get("threshold_results", {})
    if not isinstance(threshold_results, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, bool)
        for key, value in threshold_results.items()
    ):
        raise ValueError("canonical pilot threshold results are malformed")
    transport_verified = payload.get("transport_verified")
    trend_ready = payload.get("trend_ready")
    if not isinstance(transport_verified, bool) or not isinstance(trend_ready, bool):
        raise ValueError("canonical pilot readiness flags are malformed")
    state = pilot.get("state")
    family = pilot.get("task_family")
    started_at = pilot.get("started_at")
    closed_at = pilot.get("closed_at")
    if (
        not isinstance(state, str)
        or family is not None and not isinstance(family, str)
        or not isinstance(started_at, str)
        or closed_at is not None and not isinstance(closed_at, str)
    ):
        raise ValueError("canonical pilot metadata is malformed")
    return {
        "state": state,
        "task_family": family,
        "started_at": started_at,
        "closed_at": closed_at,
        "target": _fact(
            pilot.get("target"), "count", provenance="exact",
            unavailable="pilot_target_unavailable",
        ),
        "eligible_tasks": _fact(
            facts.get("eligible_tasks"), "count",
            unavailable="pilot_eligible_tasks_unavailable",
        ),
        "instrumented_tasks": _fact(
            facts.get("instrumented_tasks"), "count",
            unavailable="pilot_instrumented_tasks_unavailable",
        ),
        "enrollment": _fact(
            facts.get("enrollment"), "ratio",
            unavailable="pilot_enrollment_unavailable",
        ),
        "aggregate_coverage": _fact(
            facts.get("aggregate_coverage"), "ratio",
            unavailable="pilot_coverage_unavailable",
        ),
        "minimum_task_coverage": _fact(
            facts.get("minimum_task_coverage"), "ratio",
            unavailable="pilot_minimum_coverage_unavailable",
        ),
        "staging_latency_p95_ms": _fact(
            facts.get("staging_latency_p95_ms"), "milliseconds",
            unavailable="pilot_staging_latency_unavailable",
        ),
        "token_overhead": _fact(
            facts.get("token_overhead"), "tokens",
            unavailable="pilot_token_overhead_unavailable",
        ),
        "transport_verified": transport_verified,
        "trend_ready": trend_ready,
        "threshold_results": {
            str(key): bool(threshold_results[key]) for key in sorted(threshold_results)
        },
    }


_STORAGE_UNITS = {
    "database_bytes": "bytes",
    "wal_bytes": "bytes",
    "rollout_sources": "count",
    "rollout_events": "count",
    "codex_event_sources": "count",
    "codex_events": "count",
    "schema_version": "count",
}


def _storage_group(
    values: Mapping[str, object] | None,
    *,
    provenance: str,
    caveat: str,
) -> dict[str, object] | None:
    if values is None:
        return None
    return {
        name: _fact(
            values.get(name), unit, provenance=provenance,
            unavailable=caveat,
        )
        for name, unit in _STORAGE_UNITS.items()
    }


def project_storage_status(status: StorageStatus) -> dict[str, object]:
    """Project canonical raw storage counters into dashboard NumericFacts."""
    canonical = status.as_dict()
    current = canonical.get("current")
    baseline = canonical.get("baseline")
    growth = canonical.get("growth")
    diagnostics = canonical.get("diagnostics")
    if not isinstance(current, Mapping):
        raise ValueError("canonical storage current facts are malformed")
    if baseline is not None and not isinstance(baseline, Mapping):
        raise ValueError("canonical storage baseline facts are malformed")
    if growth is not None and not isinstance(growth, Mapping):
        raise ValueError("canonical storage growth facts are malformed")
    if not isinstance(diagnostics, list):
        raise ValueError("canonical storage diagnostics are malformed")
    growth_units = {
        name: unit for name, unit in _STORAGE_UNITS.items()
        if name != "schema_version"
    }
    return {
        "baseline_state": canonical["baseline_state"],
        "current": _storage_group(
            current, provenance="exact", caveat="storage_current_unavailable",
        ),
        "baseline": _storage_group(
            baseline, provenance="exact", caveat="storage_baseline_unavailable",
        ),
        "growth": (
            None if growth is None else {
                name: _fact(
                    growth.get(name), unit,
                    unavailable="storage_growth_unavailable",
                )
                for name, unit in growth_units.items()
            }
        ),
        "diagnostics": [
            {"code": item.get("code"), "severity": item.get("severity")}
            for item in diagnostics if isinstance(item, Mapping)
        ],
    }
