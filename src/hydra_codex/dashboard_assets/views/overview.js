import {dataTable, el, factPercent, factSummaryText, factText, metricCard, pageHeader, phaseFigure, taskDisplay} from "../dom.js";

function basisText(project, overview) {
  if (overview.basis && overview.basis.task_ref) {
    return `Basis: latest task ${taskDisplay(overview.basis)}`;
  }
  if (project.freshness_state === "stale" && overview.basis && overview.basis.kind === "latest_task") {
    return "Latest validated task unavailable — Sync required";
  }
  return "No validated task evidence is available.";
}

function readinessText(value) {
  return value === true ? "Ready" : value === false ? "Not ready" : "Unavailable";
}

function absentPilotText(project) {
  return project.freshness_state === "stale"
    ? "Unavailable until Sync"
    : "Not started";
}

export function renderOverview(snapshot, actions) {
  const project = snapshot && snapshot.project;
  if (!project) return el("div", {class: "empty-state"}, [
    el("h1", {text: "No projects yet"}),
    el("p", {text: "Run Sync now after Codex telemetry is available."}),
  ]);
  const overview = project.overview || {};
  const headline = overview.headline || {};
  const phase = overview.phase_allocation;
  const recent = Array.isArray(project.recent_tasks) ? project.recent_tasks : [];
  const cards = el("div", {class: "metric-grid"}, [
    metricCard("Working tokens", headline.working_tokens),
    metricCard("Full context", headline.full_context_tokens),
    metricCard("Wall-clock", headline.wall_clock_ms, {duration: true}),
  ]);
  const classified = phase && phase.phases
    ? Object.values(phase.phases).reduce((sum, entry) => sum + (Number(entry && entry.working && entry.working.value) || 0), 0)
    : 0;
  const unclassified = Number(phase && phase.unclassified && phase.unclassified.working && phase.unclassified.working.value) || 0;
  const classifiedShare = classified + unclassified > 0 ? classified / (classified + unclassified) * 100 : null;
  const rows = recent.map(task => [
    taskDisplay(task),
    task.task_family || "Unclassified",
    task.status,
    factSummaryText(task.headline && task.headline.working_tokens),
    task.last_activity_at,
  ]);
  const pilot = project.pilot;
  const absentPilot = absentPilotText(project);
  const storage = project.storage || {};
  const instrumentation = storage.current && storage.current.codex_events;
  const systemButton = el("button", {class: "row-button", type: "button", text: "Open System health"});
  systemButton.addEventListener("click", () => actions.navigate("health"));
  return el("div", {}, [
    pageHeader(project.display_name, `${project.freshness_state} · ${project.last_activity_at}`),
    el("p", {class: "muted", text: basisText(project, overview)}),
    cards,
    el("section", {class: "section", "aria-labelledby": "overview-phases"}, [
      el("div", {class: "page-header"}, [el("h2", {id: "overview-phases", text: "Working tokens by phase"}),
        el("span", {class: "muted", text: classifiedShare === null ? "Classified share unavailable · derived" : `Classified share ${classifiedShare.toFixed(1)}% · derived from phase facts`})]),
      phaseFigure(phase, "Working tokens by semantic phase"),
    ]),
    el("section", {class: "section", "aria-labelledby": "recent-tasks"}, [
      el("h2", {id: "recent-tasks", text: "Recent tasks"}),
      rows.length ? dataTable("Recent reconciled tasks", ["Task", "Family", "Status", "Working tokens", "Last activity"], rows)
        : el("p", {class: "muted", text: project.freshness_state === "stale"
          ? "Recent task evidence requires Sync." : "No reconciled tasks are available."}),
    ]),
    el("section", {class: "section", "aria-labelledby": "pilot-heading"}, [
      el("h2", {id: "pilot-heading", text: "Pilot"}),
      el("dl", {class: "divider-list"}, [
        el("div", {class: "divider-row"}, [el("dt", {text: "State"}), el("dd", {text: pilot ? pilot.state : absentPilot})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Target"}), el("dd", {text: factText(pilot && pilot.target)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Enrollment"}), el("dd", {text: factPercent(pilot && pilot.enrollment)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Aggregate coverage"}), el("dd", {text: factPercent(pilot && pilot.aggregate_coverage)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Transport readiness"}), el("dd", {text: pilot ? readinessText(pilot.transport_verified) : absentPilot})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Trend readiness"}), el("dd", {text: pilot ? readinessText(pilot.trend_ready) : absentPilot})]),
      ]),
    ]),
    el("p", {class: "inline-status"}, ["System health · ", factText(instrumentation), " observed events · ", systemButton]),
  ]);
}
