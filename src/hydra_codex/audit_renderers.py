"""Deterministic JSON, Markdown, and static HTML renderers for Hydra audits."""

from __future__ import annotations

from html import escape
import json
import re
from typing import Mapping

from .audit_detail_renderers import html_task_detail, markdown_task_detail
from .audit_model import AuditEvidence, AuditReport


def render_audit_json(audit: AuditReport) -> str:
    """Return strict canonical JSON accepted directly by ``pilot close``."""
    if not isinstance(audit, AuditReport):
        raise ValueError("audit renderer requires an AuditReport")
    return json.dumps(
        audit.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def _md(value: object) -> str:
    text = escape(str(value), quote=False).replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", text)


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


def _index(audit: AuditReport) -> dict[str, AuditEvidence]:
    return {item.evidence_id: item for item in audit.evidence_appendix}


def _md_ref(evidence_id: str, evidence: Mapping[str, AuditEvidence]) -> str:
    record = evidence[evidence_id]
    return f"{_md(_value(record))} (`{record.evidence_id}`)"


def _section_refs_markdown(
    title: str,
    refs: Mapping[str, object],
    evidence: Mapping[str, AuditEvidence],
) -> list[str]:
    lines = [f"## {title}", "", "| Metric | Value | Evidence |", "| --- | ---: | --- |"]
    for name, evidence_id in refs.items():
        if not isinstance(evidence_id, str) or evidence_id not in evidence:
            continue
        record = evidence[evidence_id]
        lines.append(
            f"| {_md(name.replace('_', ' '))} | {_md(_value(record))} | `{record.evidence_id}` |"
        )
    return lines


def render_audit_markdown(audit: AuditReport) -> str:
    if not isinstance(audit, AuditReport):
        raise ValueError("audit renderer requires an AuditReport")
    payload = audit.as_dict()
    cohort = payload["cohort"]
    collection = payload["collection"]
    storage = payload["storage_health"]
    evidence = _index(audit)
    lines = [
        "# Hydra pilot audit",
        "",
        f"- Pilot: `{_md(cohort['pilot_id'])}`",
        f"- State: `{_md(cohort['state'])}`",
        f"- Task family: {_md(cohort['task_family'])}",
        f"- Snapshot digest: `{_md(cohort['snapshot_digest'])}`",
        f"- Transport verified: {'yes' if cohort['transport_verified'] else 'no'}",
        f"- Trend ready: {'yes' if cohort['trend_ready'] else 'no'}",
        "",
    ]
    lines.extend(_section_refs_markdown("Headline", cohort["headline"], evidence))
    lines.extend(["", "## Phase allocation", "", "| Phase | Working | Full context | Reasoning |", "| --- | ---: | ---: | ---: |"])
    for phase in cohort["phase_allocation"]:
        lines.append(
            f"| `{_md(phase['phase'])}` | "
            f"{_md_ref(phase['working_tokens'], evidence)} | "
            f"{_md_ref(phase['full_context_tokens'], evidence)} | "
            f"{_md_ref(phase['reasoning_tokens'], evidence)} |"
        )
    drain = cohort["transport"]["pending_annotation_drain"]
    lines.extend([
        "",
        "## Transport health",
        "",
        f"- Pending annotation drain: {_md(drain['status'])} "
        f"({_md_ref(drain['evidence_id'], evidence)})",
    ])
    readiness = collection["comparability_readiness"]
    reasons = ", ".join(readiness["reasons"]) or "none"
    lines.extend([
        "",
        "## Comparability readiness",
        "",
        f"- Status: `{_md(readiness['status'])}`",
        f"- Reasons: {_md(reasons)}",
        "",
        "## Task collection",
        "",
        "| Task | Status | Family | Working | Wall clock | Tests | Coverage |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ])
    for item in collection["overview"]:
        lines.append(
            f"| `{_md(item['task_ref'])}` | {_md(item['status'])} | "
            f"{_md(item['task_family'] or 'unavailable')} | "
            f"{_md_ref(item['working_tokens'], evidence)} | "
            f"{_md_ref(item['wall_clock_ms'], evidence)} | "
            f"{_md_ref(item['test_runs'], evidence)} | "
            f"{_md_ref(item['semantic_coverage'], evidence)} |"
        )
    for item in collection["tasks"]:
        lines.extend(markdown_task_detail(item, evidence))
    lines.extend([
        "",
        *_section_refs_markdown("Storage health", storage["evidence_refs"], evidence),
        "",
        "## Evidence appendix",
        "",
        "Every complete evidence record appears once in this appendix.",
        "",
        "| Evidence | Fact | Value | Unit | Provenance | Lower bound | Caveats |",
        "| --- | --- | ---: | --- | --- | ---: | --- |",
    ])
    for record in audit.evidence_appendix:
        caveats = ", ".join(record.caveats) or "none"
        lines.extend([
            f"<!-- evidence-record:{record.evidence_id} -->",
            f"| `{record.evidence_id}` | `{_md(record.fact)}` | {_md(_number(record.value))} | "
            f"{_md(record.unit)} | {_md(record.provenance)} | "
            f"{_md(_number(record.lower_bound))} | {_md(caveats)} |",
        ])
    return "\n".join(lines) + "\n"


def _html_ref(evidence_id: str, evidence: Mapping[str, AuditEvidence]) -> str:
    record = evidence[evidence_id]
    return (
        f'<span class="metric-value">{escape(_value(record))}</span> '
        f'<a class="evidence-link" href="#{record.evidence_id}">{record.evidence_id}</a>'
    )


def _headline_html(refs: Mapping[str, object], evidence: Mapping[str, AuditEvidence]) -> str:
    labels = {
        "working_tokens": "Working tokens",
        "wall_clock_ms": "Wall clock",
        "test_runs": "Test runs",
        "semantic_coverage": "Semantic coverage",
    }
    return "".join(
        f"<div><dt>{escape(labels.get(name, name.replace('_', ' ').title()))}</dt>"
        f"<dd>{_html_ref(str(evidence_id), evidence)}</dd></div>"
        for name, evidence_id in refs.items()
    )


def _phase_html(phases: list[dict[str, object]], evidence: Mapping[str, AuditEvidence]) -> str:
    known = [
        evidence[str(item["working_tokens"])].value
        for item in phases
    ]
    total = sum(float(value) for value in known if value is not None)
    rows = []
    for item, value in zip(phases, known, strict=True):
        share = 0.0 if value is None or total <= 0 else float(value) / total * 100
        phase = str(item["phase"])
        rows.append(
            '<div class="phase-row">'
            f'<div class="phase-label"><code>{escape(phase)}</code>'
            f'<span>{_html_ref(str(item["working_tokens"]), evidence)}</span></div>'
            '<div class="phase-track" aria-hidden="true">'
            f'<span class="phase-fill {"muted" if phase == "unclassified" else ""}" '
            f'style="inline-size:{share:.4f}%"></span></div></div>'
        )
    return "".join(rows)


def _overview_html(items: list[dict[str, object]], evidence: Mapping[str, AuditEvidence]) -> str:
    rows = "".join(
        "<tr>"
        f"<td><code>{escape(str(item['task_ref']))}</code></td>"
        f"<td>{escape(str(item['status']))}</td>"
        f"<td>{escape(str(item['task_family'] or 'unavailable'))}</td>"
        f"<td>{_html_ref(str(item['working_tokens']), evidence)}</td>"
        f"<td>{_html_ref(str(item['wall_clock_ms']), evidence)}</td>"
        f"<td>{_html_ref(str(item['test_runs']), evidence)}</td>"
        f"<td>{_html_ref(str(item['semantic_coverage']), evidence)}</td>"
        "</tr>"
        for item in items
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Task</th><th>Status</th>'
        '<th>Family</th><th>Working</th><th>Wall clock</th><th>Tests</th>'
        f"<th>Coverage</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _appendix_html(records: tuple[AuditEvidence, ...]) -> str:
    rendered = []
    for item in records:
        values = (
            ("Evidence", f"<code>{item.evidence_id}</code>"),
            ("Fact", f"<code>{escape(item.fact)}</code>"),
            ("Value", escape(_number(item.value))),
            ("Unit", escape(item.unit)),
            ("Provenance", escape(item.provenance)),
            ("Lower bound", escape(_number(item.lower_bound))),
            ("Caveats", escape(", ".join(item.caveats) or "none")),
        )
        fields = "".join(
            f"<div><dt>{label}</dt><dd>{value}</dd></div>"
            for label, value in values
        )
        rendered.append(
            f'<dl class="appendix-record" id="{item.evidence_id}" '
            f'data-evidence-record="{item.evidence_id}">{fields}</dl>'
        )
    return f'<div class="appendix-list">{"".join(rendered)}</div>'


_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f7f8fa;
  --surface: #ffffff;
  --text: #18212f;
  --muted: #526071;
  --border: #cbd3dd;
  --accent: #1d5fd1;
  --accent-soft: #dce9ff;
  --track: #e5e9ef;
}
* { box-sizing: border-box; }
html { background: var(--bg); }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  line-height: 1.5;
}
main { max-width: 1180px; margin: 0 auto; padding: 40px 24px 72px; }
h1, h2, h3, h4 { margin: 0; line-height: 1.2; text-wrap: balance; }
h1 { font-size: 2rem; letter-spacing: -0.025em; }
h2 { font-size: 1.35rem; }
h3 { font-size: 1.12rem; }
h4 { margin-block-end: 8px; font-size: .95rem; }
p { max-width: 72ch; color: var(--muted); text-wrap: pretty; }
code { overflow-wrap: anywhere; font: .9em ui-monospace, SFMono-Regular, Menlo, monospace; }
a { color: var(--accent); text-underline-offset: 2px; }
.report-header { padding-block-end: 28px; border-block-end: 2px solid var(--text); }
.meta { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-block: 14px 0; color: var(--muted); }
.status { display: inline-block; padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px; color: var(--text); }
.headline { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0; border-block: 1px solid var(--border); }
.headline > div { min-width: 0; padding: 18px 16px; border-inline-end: 1px solid var(--border); }
.headline > div:last-child { border-inline-end: 0; }
.headline dt { color: var(--muted); font-size: .82rem; font-weight: 650; }
.headline dd { margin: 6px 0 0; font-size: 1.05rem; font-weight: 680; }
.report-section { padding-block: 30px; border-block-end: 1px solid var(--border); }
.section-intro { display: flex; justify-content: space-between; gap: 16px; align-items: baseline; margin-block-end: 16px; }
.phase-list { display: grid; gap: 12px; }
.phase-row { display: grid; gap: 5px; }
.phase-label { display: flex; justify-content: space-between; gap: 12px; }
.phase-track { block-size: 9px; overflow: hidden; background: var(--track); border-radius: 999px; }
.phase-fill { display: block; block-size: 100%; background: var(--accent); }
.phase-fill.muted { background: var(--muted); }
.table-wrap { max-width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .88rem; }
th, td { padding: 10px 12px; border-block-end: 1px solid var(--border); text-align: start; vertical-align: top; }
thead th { color: var(--muted); font-size: .78rem; font-weight: 700; }
tbody th { color: var(--muted); font-weight: 600; }
.metric-value { font-variant-numeric: tabular-nums; }
.evidence-link { margin-inline-start: 4px; font: .72rem ui-monospace, monospace; white-space: nowrap; }
.readiness { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline; }
.task { padding-block: 26px; border-block-start: 1px solid var(--border); }
.task:first-of-type { margin-block-start: 26px; }
.task-columns { display: grid; grid-template-columns: minmax(0, .8fr) minmax(0, 1.2fr); gap: 28px; margin-block: 20px; }
.task-columns > * { min-width: 0; }
.appendix-list { display: grid; gap: 16px; }
.appendix-record { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 18px; margin: 0; padding: 14px 0; border-block-start: 1px solid var(--border); }
.appendix-record > div { min-width: 0; }
.appendix-record dt { color: var(--muted); font-size: .78rem; font-weight: 700; }
.appendix-record dd { margin: 2px 0 0; overflow-wrap: anywhere; }
.empty { color: var(--muted); font-style: italic; }
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101318;
    --surface: #171c23;
    --text: #edf2f7;
    --muted: #b6c0cc;
    --border: #3b4654;
    --accent: #7db2ff;
    --accent-soft: #1c3558;
    --track: #29313c;
  }
}
@media (max-width: 640px) {
  main { padding: 24px 12px 48px; }
  h1 { font-size: 1.65rem; }
  .headline { grid-template-columns: 1fr; }
  .headline > div { border-inline-end: 0; border-block-end: 1px solid var(--border); }
  .headline > div:last-child { border-block-end: 0; }
  .section-intro, .phase-label { align-items: flex-start; flex-direction: column; }
  .task-columns { grid-template-columns: 1fr; gap: 20px; }
  .appendix-record { grid-template-columns: 1fr; }
  th, td { padding: 8px; }
}
@media print {
  @page { size: A4; margin: 12mm; }
  :root { --bg: #fff; --text: #000; --muted: #333; --border: #999; --accent: #000; --track: #ddd; }
  body { background: #fff; color: #000; font-size: 10pt; }
  main { max-width: none; padding: 0; }
  a { color: #000; text-decoration: none; }
  h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
  .report-section { break-inside: auto; }
  .task { break-before: page; break-inside: auto; }
  .task:first-of-type { break-before: auto; }
  .report-section[aria-labelledby="appendix"] { break-before: page; }
  .table-wrap { overflow: visible; }
  table { break-inside: auto; }
  thead { display: table-header-group; }
  thead { break-inside: avoid; page-break-inside: avoid; }
  tbody { break-inside: auto; }
  tfoot { display: table-footer-group; }
  tr { break-inside: avoid; page-break-inside: avoid; }
  tr { break-after: auto; page-break-after: auto; }
  th, td { padding: 5px 6px; overflow-wrap: anywhere; }
  .appendix-record { break-inside: avoid-page; page-break-inside: avoid; gap: 4px 12px; padding: 8px 0; }
  .evidence-link { white-space: normal; }
}
""".strip()


def render_audit_html(audit: AuditReport) -> str:
    if not isinstance(audit, AuditReport):
        raise ValueError("audit renderer requires an AuditReport")
    payload = audit.as_dict()
    cohort = payload["cohort"]
    collection = payload["collection"]
    storage = payload["storage_health"]
    evidence = _index(audit)
    readiness = collection["comparability_readiness"]
    reasons = ", ".join(readiness["reasons"]) or "none"
    task_sections = "".join(
        html_task_detail(item, evidence, _phase_html)
        for item in collection["tasks"]
    )
    if not task_sections:
        task_sections = '<p class="empty">No completed cohort tasks.</p>'
    storage_rows = "".join(
        f"<tr><th>{escape(name.replace('_', ' ').title())}</th>"
        f"<td>{_html_ref(str(evidence_id), evidence)}</td></tr>"
        for name, evidence_id in storage["evidence_refs"].items()
    )
    drain = cohort["transport"]["pending_annotation_drain"]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Hydra pilot audit</title>'
        f'<style>{_STYLE}</style></head><body><main id="hydra-audit">'
        '<header class="report-header"><h1>Hydra pilot audit</h1>'
        '<div class="meta">'
        f'<code>{escape(str(cohort["pilot_id"]))}</code>'
        f'<span class="status">{escape(str(cohort["state"]))}</span>'
        f'<span>{escape(str(cohort["task_family"]))}</span>'
        f'<span>{escape(str(cohort["started_at"]))}</span></div>'
        f'<p>Snapshot <code>{escape(str(cohort["snapshot_digest"]))}</code>. '
        f'Transport verified: {"yes" if cohort["transport_verified"] else "no"}; '
        f'trend ready: {"yes" if cohort["trend_ready"] else "no"}.</p></header>'
        '<section class="report-section" aria-labelledby="headline"><div class="section-intro">'
        '<h2 id="headline">Headline</h2></div>'
        f'<dl class="headline">{_headline_html(cohort["headline"], evidence)}</dl></section>'
        '<section class="report-section" aria-labelledby="phase"><div class="section-intro">'
        '<h2 id="phase">Phase allocation</h2><span>Working-token share</span></div>'
        f'<div class="phase-list">{_phase_html(cohort["phase_allocation"], evidence)}</div></section>'
        '<section class="report-section" aria-labelledby="transport"><div class="section-intro">'
        '<h2 id="transport">Transport health</h2></div>'
        f'<p>Pending annotation drain: {escape(str(drain["status"]))}. '
        f'{_html_ref(str(drain["evidence_id"]), evidence)}</p></section>'
        '<section class="report-section" aria-labelledby="readiness"><div class="section-intro">'
        '<h2 id="readiness">Comparability readiness</h2></div>'
        f'<p class="readiness"><strong>{escape(str(readiness["status"]))}</strong>'
        f'<span>{escape(reasons)}</span></p></section>'
        '<section class="report-section" aria-labelledby="collection"><div class="section-intro">'
        f'<h2 id="collection">Task collection</h2><span>{collection["count"]} tasks</span></div>'
        f'{_overview_html(collection["overview"], evidence)}{task_sections}</section>'
        '<section class="report-section" aria-labelledby="storage"><div class="section-intro">'
        '<h2 id="storage">Storage health</h2><span>Current read-only snapshot</span></div>'
        f'<div class="table-wrap"><table><tbody>{storage_rows}</tbody></table></div></section>'
        '<section class="report-section" aria-labelledby="appendix"><div class="section-intro">'
        '<h2 id="appendix">Evidence appendix</h2></div>'
        '<p>Every complete evidence record appears once in this appendix.</p>'
        f'{_appendix_html(audit.evidence_appendix)}</section></main></body></html>\n'
    )
