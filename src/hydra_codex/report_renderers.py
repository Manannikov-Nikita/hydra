"""Deterministic JSON, Markdown, and standalone HTML report renderers."""

from __future__ import annotations

from html import escape
import json
import re

from .reporting import ComparisonReport, NumericFact, TaskReport


Renderable = TaskReport | ComparisonReport
REPORT_LIST_SCHEMA = "hydra.report-list/v1"


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


def _report_markdown(report: TaskReport) -> str:
    family = report.task_family if report.task_family is not None else "unavailable"
    lines = [
        "# Hydra task report",
        "",
        f"- Task: `{_md(report.task_ref)}`",
        f"- Status: {_md(report.status)}",
        f"- Last activity: {_md(report.last_activity_at)}",
        f"- Task family: {_md(family)}",
        "",
        "| Metric | Value; provenance; lower bound; caveats |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| {_md(name)} | {_md_fact(fact)} |"
        for name, fact in report.public_facts().items()
    )
    return "\n".join(lines) + "\n"


def _comparison_markdown(report: ComparisonReport) -> str:
    lines = [
        "# Hydra task comparison",
        "",
        f"- Baseline: `{_md(report.baseline_ref)}`",
        f"- Current: `{_md(report.current_ref)}`",
        f"- Caveats: {_md(', '.join(report.caveats))}",
        "",
        "| Metric | Baseline | Current | Delta | Change |",
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


def _report_html(report: TaskReport) -> str:
    family = report.task_family if report.task_family is not None else "unavailable"
    summary = (
        f"<p>Task <code>{escape(report.task_ref)}</code>; status {escape(report.status)}; "
        f"last activity {escape(report.last_activity_at)}; family {escape(family)}.</p>"
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
        f"<code>{escape(report.current_ref)}</code>; caveats {escape(', '.join(report.caveats))}.</p>"
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
        ("Metric", "Baseline", "Current", "Delta", "Change"), rows,
    )


def render_html(value: Renderable) -> str:
    return _report_html(value) if isinstance(value, TaskReport) else _comparison_html(value)


def render_report_collection(reports: tuple[TaskReport, ...], output_format: str) -> str:
    """Render recent reports in caller-supplied deterministic order."""
    if not isinstance(reports, tuple) or any(not isinstance(item, TaskReport) for item in reports):
        raise ValueError("reports must be a tuple of TaskReport values")
    if output_format == "json":
        return json.dumps(
            {
                "schema_version": REPORT_LIST_SCHEMA,
                "reports": [item.as_dict() for item in reports],
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
                f"<code>{escape(report.task_ref)}</code>", escape(report.status),
                escape(name), _html_fact(fact),
            )
            for report in reports
            for name, fact in report.public_facts().items()
        ]
        summary = f"<p>{len(reports)} reconciled tasks.</p>"
        return _document(
            "Hydra task reports", "Hydra task reports", summary,
            ("Task", "Status", "Metric", "Value; provenance; lower bound; caveats"), rows,
        )
    raise ValueError("unsupported report format")
