"""Human-readable per-task sections for canonical Hydra audits."""

from __future__ import annotations

from html import escape
import re
from typing import Mapping

from .audit_model import AuditEvidence


def _number(value: int | float | None) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return format(value, ".12g")
    return f"{value:,}".replace(",", " ")


def _value(record: AuditEvidence) -> str:
    if record.value is None:
        result = "unavailable"
        if record.lower_bound is not None:
            result += f" (lower bound {_number(record.lower_bound)})"
        return result
    return f"{_number(record.value)} {record.unit}"


def _md(value: object) -> str:
    text = escape(str(value), quote=False).replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", text)


def _md_ref(evidence_id: object, evidence: Mapping[str, AuditEvidence]) -> str:
    record = evidence[str(evidence_id)]
    return f"{_md(_value(record))} (`{record.evidence_id}`)"


def _html_ref(evidence_id: object, evidence: Mapping[str, AuditEvidence]) -> str:
    record = evidence[str(evidence_id)]
    return (
        f'<span class="metric-value">{escape(_value(record))}</span> '
        f'<a class="evidence-link" href="#{record.evidence_id}">{record.evidence_id}</a>'
    )


def markdown_task_detail(
    item: dict[str, object],
    evidence: Mapping[str, AuditEvidence],
) -> list[str]:
    topology = item["agent_topology"]
    comparison = item["comparability"]
    issues = item["issues"]
    tools = item["tool_file_test"]
    lines = [
        "",
        f"### Task `{_md(item['task_ref'])}`",
        "",
        f"- Status: {_md(item['status'])}",
        f"- Family: {_md(item['task_family'] or 'unavailable')}",
        f"- Completed: {_md(item['last_activity_at'])}",
        f"- Agent topology: {_md(topology['status'])}; "
        f"sessions {_md_ref(topology['sessions'], evidence)}; "
        f"subagents {_md_ref(topology['subagents'], evidence)}",
        "",
        "#### Comparability",
        "",
        f"- Status: `{_md(comparison['status'])}`",
        f"- Scope change: {_md(comparison['scope_change'])}",
        f"- Trend eligible: {'yes' if comparison['trend_eligible'] else 'no'}",
        f"- Baseline working tokens: "
        f"{_md_ref(comparison['baseline_working_tokens'], evidence)}",
        f"- Warning: {'yes' if comparison['warning'] else 'no'}",
        f"- Corroborating signal: "
        f"{_md(comparison['corroborating_signal'] or 'none')}",
        f"- Caveats: {_md(', '.join(comparison['caveats']) or 'none')}",
        "",
        "#### Issue and marker counts",
        "",
        "| Issue | Value | Evidence |",
        "| --- | ---: | --- |",
    ]
    for name, evidence_id in issues.items():
        record = evidence[str(evidence_id)]
        lines.append(
            f"| {_md(name.replace('_', ' '))} | {_md(_value(record))} | "
            f"`{record.evidence_id}` |"
        )
    lines.extend([
        "",
        "#### Per-task phase allocation",
        "",
        "| Phase | Working | Full context | Reasoning |",
        "| --- | ---: | ---: | ---: |",
    ])
    for phase in item["phase_allocation"]:
        lines.append(
            f"| `{_md(phase['phase'])}` | "
            f"{_md_ref(phase['working_tokens'], evidence)} | "
            f"{_md_ref(phase['full_context_tokens'], evidence)} | "
            f"{_md_ref(phase['reasoning_tokens'], evidence)} |"
        )
    lines.extend([
        "",
        "#### Tool, file, and test evidence",
        "",
        "| Metric | Value | Evidence |",
        "| --- | ---: | --- |",
    ])
    for name, evidence_id in tools.items():
        if name == "test_evidence":
            continue
        record = evidence[str(evidence_id)]
        lines.append(
            f"| {_md(name.replace('_', ' '))} | {_md(_value(record))} | "
            f"`{record.evidence_id}` |"
        )
    test_rows = tools["test_evidence"]
    if test_rows:
        lines.extend([
            "",
            "##### Deterministic test evidence",
            "",
            "| Scope | Failure cause | Retry kind | Phase | Cause | Count |",
            "| --- | --- | --- | --- | --- | ---: |",
        ])
        for row in test_rows:
            lines.append(
                "| " + " | ".join((
                    _md(row["scope"]),
                    _md(row["failure_cause"]),
                    _md(row["retry_kind"]),
                    _md(row["phase"]),
                    _md(row["cause"]),
                    _md_ref(row["count"], evidence),
                )) + " |"
            )
    lines.extend(["", "#### Semantic marker timeline", ""])
    markers = item["semantic_markers"]
    if not markers:
        lines.append("No model-reported markers.")
    else:
        lines.extend([
            "| Kind | Phase | Cause | Scope change | Outcome | Confidence | Note | Provenance |",
            "| --- | --- | --- | --- | --- | ---: | --- | --- |",
        ])
        for marker in markers:
            lines.append(
                "| " + " | ".join((
                    _md(marker["kind"]),
                    _md(marker["phase"]),
                    _md(marker["cause"]),
                    _md(marker["scope_change"]),
                    _md(marker["outcome"] or "none"),
                    _md_ref(marker["confidence"], evidence),
                    _md(marker["note"]),
                    _md(marker["provenance"]),
                )) + " |"
            )
    return lines


def _html_rows(
    values: Mapping[str, object],
    evidence: Mapping[str, AuditEvidence],
) -> str:
    return "".join(
        f"<tr><th>{escape(name.replace('_', ' ').title())}</th>"
        f"<td>{_html_ref(evidence_id, evidence)}</td></tr>"
        for name, evidence_id in values.items()
    )


def html_task_detail(
    item: dict[str, object],
    evidence: Mapping[str, AuditEvidence],
    phase_html: object,
) -> str:
    topology = item["agent_topology"]
    comparison = item["comparability"]
    issues = item["issues"]
    tools = item["tool_file_test"]
    tool_refs = {name: value for name, value in tools.items() if name != "test_evidence"}
    tests = ""
    if tools["test_evidence"]:
        test_rows = "".join(
            "<tr>" + "".join(
                f"<td>{escape(str(value))}</td>"
                for value in (
                    row["scope"], row["failure_cause"], row["retry_kind"],
                    row["phase"], row["cause"],
                )
            ) + f"<td>{_html_ref(row['count'], evidence)}</td></tr>"
            for row in tools["test_evidence"]
        )
        tests = (
            "<h5>Deterministic test evidence</h5><div class=\"table-wrap\"><table>"
            "<thead><tr><th>Scope</th><th>Failure cause</th><th>Retry kind</th>"
            "<th>Phase</th><th>Cause</th><th>Count</th></tr></thead>"
            f"<tbody>{test_rows}</tbody></table></div>"
        )
    markers = item["semantic_markers"]
    if markers:
        marker_rows = "".join(
            "<tr>" + "".join(
                f"<td>{escape(str(value))}</td>"
                for value in (
                    marker["kind"], marker["phase"], marker["cause"],
                    marker["scope_change"], marker["outcome"] or "none",
                )
            ) + f"<td>{_html_ref(marker['confidence'], evidence)}</td>"
            f"<td>{escape(str(marker['note']))}</td>"
            f"<td>{escape(str(marker['provenance']))}</td></tr>"
            for marker in markers
        )
        marker_table = (
            '<div class="table-wrap"><table><thead><tr><th>Kind</th><th>Phase</th>'
            '<th>Cause</th><th>Scope change</th><th>Outcome</th><th>Confidence</th>'
            f"<th>Note</th><th>Provenance</th></tr></thead><tbody>{marker_rows}</tbody></table></div>"
        )
    else:
        marker_table = '<p class="empty">No model-reported markers.</p>'
    reasons = ", ".join(comparison["caveats"]) or "none"
    task_ref = escape(str(item["task_ref"]))
    return (
        f'<section class="task" aria-labelledby="task-{task_ref}">'
        f'<h3 id="task-{task_ref}">Task <code>{task_ref}</code></h3>'
        f'<p>{escape(str(item["status"]))} · '
        f'{escape(str(item["task_family"] or "unavailable"))} · '
        f'{escape(str(item["last_activity_at"]))}</p>'
        '<div class="task-columns"><div><h4>Agent topology</h4>'
        f'<p>{escape(str(topology["status"]))}. Sessions '
        f'{_html_ref(topology["sessions"], evidence)}; subagents '
        f'{_html_ref(topology["subagents"], evidence)}.</p>'
        '<h4>Comparability</h4>'
        f'<p><strong>{escape(str(comparison["status"]))}</strong> · {escape(reasons)}. '
        f'Baseline working tokens: {_html_ref(comparison["baseline_working_tokens"], evidence)}.</p>'
        '<h4>Issue and marker counts</h4>'
        f'<div class="table-wrap"><table><tbody>{_html_rows(issues, evidence)}</tbody></table></div></div>'
        '<div><h4>Tool, file, and test evidence</h4>'
        f'<div class="table-wrap"><table><tbody>{_html_rows(tool_refs, evidence)}</tbody></table></div>'
        f'{tests}</div></div><h4>Per-task phase allocation</h4>'
        f'{phase_html(item["phase_allocation"], evidence)}'
        f'<h4>Semantic marker timeline</h4>{marker_table}</section>'
    )
