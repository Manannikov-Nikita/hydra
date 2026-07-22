"""Conservative readiness summaries for audit presentation only."""

from __future__ import annotations


def comparability_readiness(
    task_views: list[dict[str, object]],
    *,
    transport_verified: bool,
    trend_ready: bool,
    receipt: object,
) -> dict[str, object]:
    """Report readiness without introducing Task 7 pairwise verdicts."""
    if not task_views:
        return {"status": "unknown", "reasons": ["no_completed_tasks"]}
    comparisons = [
        item["comparability"]
        for item in task_views
        if isinstance(item.get("comparability"), dict)
    ]
    if len(comparisons) != len(task_views):
        raise ValueError("task comparability summary is invalid")
    excluded = [item for item in comparisons if item.get("status") == "excluded"]
    unknown = [item for item in comparisons if item.get("status") == "unknown"]
    ready = [item for item in comparisons if item.get("status") == "ready"]
    if len(excluded) + len(unknown) + len(ready) != len(comparisons):
        raise ValueError("task comparability status is invalid")
    reasons = {
        str(caveat)
        for item in (*excluded, *unknown)
        for caveat in item.get("caveats", [])
    }
    if "task_family_unavailable" in reasons or "scope_change_excluded" in reasons:
        reasons.discard("automatic_comparison_excluded")
    if len(excluded) == len(comparisons):
        return {
            "status": "excluded",
            "reasons": sorted(reasons or {"automatic_comparison_excluded"}),
        }
    family_counts: dict[str, int] = {}
    for item in ready:
        family = item.get("task_family")
        if isinstance(family, str):
            family_counts[family] = family_counts.get(family, 0) + 1
    if max(family_counts.values(), default=0) < 2:
        reasons.add("insufficient_comparable_baseline")
    if not transport_verified:
        reasons.add("pilot_thresholds_unverified")
    receipt_verified = bool(
        isinstance(receipt, dict)
        and receipt.get("decision") == "verified"
        and receipt.get("current") is True
    )
    if not receipt_verified:
        reasons.add("verified_receipt_required")
    if transport_verified and receipt_verified and not trend_ready:
        reasons.add("pilot_not_trend_ready")
    if excluded and (ready or unknown):
        status = "partial"
    elif unknown or reasons:
        status = "unknown"
    else:
        status = "ready"
    return {
        "status": status,
        "reasons": sorted(reasons),
    }
