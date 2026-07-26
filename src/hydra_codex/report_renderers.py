"""Deterministic JSON, Markdown, and standalone HTML report renderers."""

from __future__ import annotations

from html import escape
import json
import re

from .reporting import ComparisonReport, NumericFact, TaskReport


Renderable = TaskReport | ComparisonReport
REPORT_LIST_SCHEMA = "hydra.report-list/v2"


def _number(value: int | float | None) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)


def render_json(value: Renderable) -> str:
    """Render stable strict JSON without NaN or implementation-only fields."""
    return json.dumps(
        value.as_dict(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n"


def _md(value: object) -> str:
    escaped = escape(str(value), quote=False).replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", escaped)


def _md_fact(fact: NumericFact) -> str:
    caveats = ", ".join(fact.caveats) if fact.caveats else "none"
    lower = _number(fact.lower_bound)
    return _md(
        f"{_number(fact.value)} {fact.unit}; {fact.provenance}; "
        f"lower={lower}; caveats={caveats}"
    )


def _trend_text(report: TaskReport) -> str:
    signal = report.trend_result.corroborating_signal or "none"
    return f"Trend warning: {'yes' if report.trend_result.warning else 'no'}; signal: {signal}"


def _pilot_text(report: TaskReport) -> str:
    receipt = "yes" if report.pilot_health.receipt_verified else "no"
    return f"Pilot status: {report.pilot_health.status}; receipt verified: {receipt}"


def _marker_values(marker: object) -> tuple[object, ...]:
    return (
        marker.kind, marker.phase, marker.cause, marker.scope_change,
        marker.outcome or "none", marker.confidence, marker.note, marker.provenance,
    )


def _semantic_markdown(report: TaskReport) -> list[str]:
    summary = report.semantic_breakdown.annotations
    lines: list[str] = []
    if summary.timeline:
        lines.extend([
            "", "## Semantic marker timeline", "",
            (
                f"Showing {len(summary.timeline)} of {_number(summary.total_count.value)} model markers; "
                f"truncated={_number(summary.truncated_count.value)}."
            ),
            "",
            "| Kind | Phase | Cause | Scope change | Outcome | Confidence | Note | Provenance |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        lines.extend(
            "| " + " | ".join(_md(value) for value in _marker_values(marker)) + " |"
            for marker in summary.timeline
        )
    if summary.test_evidence.rows:
        lines.extend([
            "", "## Deterministic test evidence", "",
            "| Scope | Failure cause | Retry kind | Semantic phase | Semantic cause | Count |",
            "| --- | --- | --- | --- | --- | --- |",
        ])
        lines.extend(
            "| " + " | ".join((
                *(_md(value) for value in (
                    row.scope, row.failure_cause, row.retry_kind, row.phase, row.cause,
                )),
                _md_fact(row.count),
            )) + " |"
            for row in summary.test_evidence.rows
        )
    return lines


def _report_markdown(report: TaskReport) -> str:
    family = report.task_family if report.task_family is not None else "unavailable"
    display_name = report.display_name if report.display_name is not None else "unavailable"
    lines = [
        "# Hydra task report",
        "",
        f"- Task: `{_md(report.task_ref)}`",
        f"- Display name: {_md(display_name)}",
        f"- Status: {_md(report.status)}",
        f"- Last activity: {_md(report.last_activity_at)}",
        f"- Task family: {_md(family)}",
        f"- {_md(_trend_text(report))}",
        f"- {_md(_pilot_text(report))}",
        "",
        "| Metric | Value; provenance; lower bound; caveats |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| {_md(name)} | {_md_fact(fact)} |"
        for name, fact in report.public_facts().items()
    )
    lines.extend(_semantic_markdown(report))
    return "\n".join(lines) + "\n"


def _comparison_markdown(report: ComparisonReport) -> str:
    lines = [
        "# Hydra task comparison",
        "",
        f"- Baseline: `{_md(report.baseline_ref)}`",
        f"- Current: `{_md(report.current_ref)}`",
        f"- Verdict: `{_md(report.verdict)}`",
        f"- Reasons: {_md(', '.join(report.reasons) if report.reasons else 'none')}",
        f"- Caveats: {_md(', '.join(report.caveats))}",
        "",
        "| Metric | Baseline | Current | Delta | Raw percent change |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {_md(name)} | {_md_fact(item.baseline)} | {_md_fact(item.current)} | "
        f"{_md_fact(item.delta)} | {_md_fact(item.percent_change)} |"
        for name, item in report.metrics.items()
    )
    return "\n".join(lines) + "\n"


def render_markdown(value: Renderable) -> str:
    return _report_markdown(value) if isinstance(value, TaskReport) else _comparison_markdown(value)


def _html_fact(fact: NumericFact) -> str:
    caveats = ", ".join(fact.caveats) if fact.caveats else "none"
    return escape(
        f"{_number(fact.value)} {fact.unit}; {fact.provenance}; "
        f"lower={_number(fact.lower_bound)}; caveats={caveats}"
    )


def _document(title: str, heading: str, summary: str, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    header = "".join(f"<th>{escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{item}</td>" for item in row) + "</tr>"
        for row in rows
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;"
        "color:#17202a}table{border-collapse:collapse;width:100%;font-size:.9rem}"
        "th,td{border:1px solid #ccd1d1;padding:.45rem;text-align:left;vertical-align:top}"
        "th{background:#f4f6f7}code{background:#f4f6f7;padding:.1rem .25rem}</style></head>"
        f"<body><h1>{escape(heading)}</h1>{summary}<table><thead><tr>{header}</tr></thead>"
        f"<tbody>{body}</tbody></table></body></html>\n"
    )


def _semantic_html(report: TaskReport) -> str:
    summary = report.semantic_breakdown.annotations
    sections: list[str] = []
    if summary.timeline:
        header = "".join(
            f"<th>{item}</th>" for item in
            ("Kind", "Phase", "Cause", "Scope change", "Outcome", "Confidence", "Note", "Provenance")
        )
        rows = "".join(
            "<tr>" + "".join(
                f"<td>{escape(str(value))}</td>" for value in _marker_values(marker)
            ) + "</tr>"
            for marker in summary.timeline
        )
        sections.append(
            "<h2>Semantic marker timeline</h2>"
            f"<p>Showing {len(summary.timeline)} of {escape(_number(summary.total_count.value))} "
            f"model markers; truncated={escape(_number(summary.truncated_count.value))}.</p>"
            f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"
        )
    if summary.test_evidence.rows:
        header = "".join(
            f"<th>{item}</th>" for item in
            ("Scope", "Failure cause", "Retry kind", "Semantic phase", "Semantic cause", "Count")
        )
        rows = "".join(
            "<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in (
                row.scope, row.failure_cause, row.retry_kind, row.phase, row.cause,
            )) + f"<td>{_html_fact(row.count)}</td></tr>"
            for row in summary.test_evidence.rows
        )
        sections.append(
            "<h2>Deterministic test evidence</h2>"
            f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"
        )
    return "".join(sections)


def _report_html(report: TaskReport) -> str:
    family = report.task_family if report.task_family is not None else "unavailable"
    display_name = report.display_name if report.display_name is not None else "unavailable"
    summary = (
        f"<p>Task {escape(display_name)} <code>{escape(report.task_ref)}</code>; status {escape(report.status)}; "
        f"last activity {escape(report.last_activity_at)}; family {escape(family)}.</p>"
        f"<p>{escape(_trend_text(report))}</p>"
        f"<p>{escape(_pilot_text(report))}</p>{_semantic_html(report)}"
    )
    rows = [
        (escape(name), _html_fact(fact))
        for name, fact in report.public_facts().items()
    ]
    return _document(
        "Hydra task report", "Hydra task report", summary,
        ("Metric", "Value; provenance; lower bound; caveats"), rows,
    )


def _comparison_html(report: ComparisonReport) -> str:
    summary = (
        f"<p>Baseline <code>{escape(report.baseline_ref)}</code>; current "
        f"<code>{escape(report.current_ref)}</code>; verdict "
        f"<strong>{escape(report.verdict)}</strong>; reasons "
        f"{escape(', '.join(report.reasons) if report.reasons else 'none')}; "
        f"caveats {escape(', '.join(report.caveats))}.</p>"
    )
    rows = [
        (
            escape(name), _html_fact(item.baseline), _html_fact(item.current),
            _html_fact(item.delta), _html_fact(item.percent_change),
        )
        for name, item in report.metrics.items()
    ]
    return _document(
        "Hydra task comparison", "Hydra task comparison", summary,
        ("Metric", "Baseline", "Current", "Delta", "Raw percent change"), rows,
    )


def render_html(value: Renderable) -> str:
    return _report_html(value) if isinstance(value, TaskReport) else _comparison_html(value)


def render_report_collection(
    reports: tuple[TaskReport, ...], output_format: str, *,
    sync_freshness: dict[str, object] | None = None,
) -> str:
    """Render recent reports in caller-supplied deterministic order."""
    if not isinstance(reports, tuple) or any(not isinstance(item, TaskReport) for item in reports):
        raise ValueError("reports must be a tuple of TaskReport values")
    if output_format == "json":
        return json.dumps(
            {
                "schema_version": REPORT_LIST_SCHEMA,
                "reports": [item.as_dict() for item in reports],
                "sync_freshness": sync_freshness or {
                    "schema_version": "hydra.sync-freshness/v1", "state": "unknown",
                },
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ) + "\n"
    if output_format == "markdown":
        if not reports:
            return "# Hydra task reports\n\nNo reconciled tasks.\n"
        return "# Hydra task reports\n\n" + "\n---\n\n".join(
            _report_markdown(item).removeprefix("# Hydra task report\n\n")
            for item in reports
        )
    if output_format == "html":
        rows = [
            (
                f"{escape(report.display_name or 'unavailable')} <code>{escape(report.task_ref)}</code>", escape(report.status),
                escape(name), _html_fact(fact),
            )
            for report in reports
            for name, fact in report.public_facts().items()
        ]
        rows.extend(
            (
                f"<code>{escape(report.task_ref)}</code>", escape(report.status),
                "trend.warning", escape(_trend_text(report)),
            )
            for report in reports
        )
        rows.extend(
            (
                f"<code>{escape(report.task_ref)}</code>", escape(report.status),
                "pilot.status", escape(_pilot_text(report)),
            )
            for report in reports
        )
        rows.extend(
            (
                f"<code>{escape(report.task_ref)}</code>", escape(report.status),
                "semantic.marker", escape("; ".join(str(value) for value in _marker_values(marker))),
            )
            for report in reports
            for marker in report.semantic_breakdown.annotations.timeline
        )
        rows.extend(
            (
                f"<code>{escape(report.task_ref)}</code>", escape(report.status),
                "semantic.test_evidence", escape("; ".join((
                    item.scope, item.failure_cause, item.retry_kind, item.phase, item.cause,
                    str(item.count.value), item.count.provenance,
                ))),
            )
            for report in reports
            for item in report.semantic_breakdown.annotations.test_evidence.rows
        )
        summary = f"<p>{len(reports)} reconciled tasks.</p>"
        return _document(
            "Hydra task reports", "Hydra task reports", summary,
            ("Task", "Status", "Metric", "Value; provenance; lower bound; caveats"), rows,
        )
    raise ValueError("unsupported report format")
