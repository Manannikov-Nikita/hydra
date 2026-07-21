"""Small human renderers for the Task 5 pilot status contract."""

from __future__ import annotations

import html
import json

from .pilot import PilotStatus


def render_pilot_status(status: PilotStatus, output_format: str) -> str:
    payload = status.as_dict()
    if output_format == "json":
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
    if output_format == "markdown":
        pilot = payload["pilot"]
        facts = payload["facts"]
        tasks = payload["tasks"]
        lines = [
            "# Hydra pilot status", "",
            f"- Pilot: `{pilot['pilot_id']}`",
            f"- State: `{pilot['state']}`",
            f"- Task family: `{pilot['task_family']}`",
            f"- Transport verified: {'yes' if payload['transport_verified'] else 'no'}",
            f"- Trend ready: {'yes' if payload['trend_ready'] else 'no'}",
            "", "## Observed facts", "",
        ]
        lines.extend(
            f"- {name.replace('_', ' ')}: `{value}`"
            for name, value in facts.items()
        )
        lines.extend((
            "", "## Cohort tasks", "",
            "| Task | Family | Scope | Instrumented | Coverage |",
            "|---|---|---|---:|---:|",
        ))
        for item in tasks:
            family = item["task_family"] or "unavailable"
            coverage = item["coverage"] if item["coverage"] is not None else "unavailable"
            lines.append(
                f"| `{item['task_ref']}` | `{family}` | `{item['scope_change']}` | "
                f"{'yes' if item['instrumented'] else 'no'} | {coverage} |"
            )
        return "\n".join(lines) + "\n"
    if output_format == "html":
        pilot = payload["pilot"]
        facts = payload["facts"]
        task_rows = "".join(
            "<tr>"
            f"<td><code>{html.escape(str(item['task_ref']))}</code></td>"
            f"<td>{html.escape(str(item['task_family'] or 'unavailable'))}</td>"
            f"<td>{html.escape(str(item['scope_change']))}</td>"
            f"<td>{'yes' if item['instrumented'] else 'no'}</td>"
            f"<td>{html.escape(str(item['coverage'] if item['coverage'] is not None else 'unavailable'))}</td>"
            "</tr>"
            for item in payload["tasks"]
        )
        facts_html = "".join(
            f"<dt>{html.escape(name.replace('_', ' '))}</dt>"
            f"<dd>{html.escape(str(value))}</dd>"
            for name, value in facts.items()
        )
        return (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Hydra pilot status</title></head><body>"
            "<main><h1>Hydra pilot status</h1>"
            f"<p>Pilot <code>{html.escape(str(pilot['pilot_id']))}</code>; "
            f"state <code>{html.escape(str(pilot['state']))}</code>.</p>"
            f"<p>Transport verified: {'yes' if payload['transport_verified'] else 'no'}; "
            f"trend ready: {'yes' if payload['trend_ready'] else 'no'}.</p>"
            f"<h2>Observed facts</h2><dl>{facts_html}</dl>"
            "<h2>Cohort tasks</h2><table><thead><tr><th>Task</th><th>Family</th>"
            f"<th>Scope</th><th>Instrumented</th><th>Coverage</th></tr></thead>"
            f"<tbody>{task_rows}</tbody></table></main></body></html>"
        )
    raise ValueError("unsupported pilot format")
