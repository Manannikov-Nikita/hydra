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
    eligible = [
        item
        for item in task_views
        if isinstance(item.get("comparability"), dict)
        and item["comparability"].get("status") != "excluded"  # type: ignore[union-attr]
    ]
    if not eligible:
        reasons = {
            caveat
            for item in task_views
            for caveat in item["comparability"]["caveats"]  # type: ignore[index]
            if caveat in {"task_family_unavailable", "scope_change_excluded"}
        }
        return {
            "status": "excluded",
            "reasons": sorted(reasons or {"automatic_comparison_excluded"}),
        }
    family_counts: dict[str, int] = {}
    for item in eligible:
        family = item["comparability"].get("task_family")  # type: ignore[union-attr]
        if isinstance(family, str):
            family_counts[family] = family_counts.get(family, 0) + 1
    reasons: list[str] = []
    if max(family_counts.values(), default=0) < 2:
        reasons.append("insufficient_comparable_baseline")
    if not transport_verified:
        reasons.append("pilot_thresholds_unverified")
    receipt_verified = bool(
        isinstance(receipt, dict)
        and receipt.get("decision") == "verified"
        and receipt.get("current") is True
    )
    if not receipt_verified:
        reasons.append("verified_receipt_required")
    if not reasons and not trend_ready:
        reasons.append("pilot_not_trend_ready")
    return {
        "status": "ready" if not reasons and trend_ready else "unknown",
        "reasons": reasons,
    }
