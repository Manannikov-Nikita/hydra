import {dataTable, el, factPercent, factText, metricCard, pageHeader, phaseFigure} from "../dom.js";

function filterButton(label, pressed, select) {
  const button = el("button", {class: "button ghost", type: "button", "aria-pressed": pressed, text: label});
  button.addEventListener("click", select);
  return button;
}

function selectedTask(state) {
  return state.tasks.find(item => item.task_ref === state.taskRef) || null;
}

function trendStatus(trend) {
  const facts = [trend.baseline_working_tokens, trend.token_growth, trend.signal_growth];
  const hasObservedFact = facts.some(fact => fact && fact.value !== null);
  if (!hasObservedFact && trend.warning !== true) return "Trend unavailable";
  if (trend.warning === true) return "Warning with corroborating evidence";
  return "No warning detected";
}

function caveatText(values) {
  return Array.isArray(values) && values.length ? values.join(", ") : "None reported";
}

function renderDetail(task) {
  if (!task) return el("div", {class: "empty-state"}, [
    el("h2", {text: "Select a task"}), el("p", {text: "Choose one task to inspect its evidence."}),
  ]);
  const heading = el("h2", {id: "task-detail-heading", tabindex: "-1", text: task.task_ref});
  const timeline = task.semantic && task.semantic.annotations && Array.isArray(task.semantic.annotations.timeline)
    ? task.semantic.annotations.timeline : [];
  const testRows = task.semantic && task.semantic.annotations && task.semantic.annotations.test_evidence
    && Array.isArray(task.semantic.annotations.test_evidence.rows)
    ? task.semantic.annotations.test_evidence.rows : [];
  const phase = task.semantic && task.semantic.breakdown;
  const pilot = task.pilot_health || {};
  const trend = task.trend && task.trend.result ? task.trend.result : {};
  return el("article", {"aria-labelledby": "task-detail-heading"}, [
    heading,
    el("p", {class: "muted", text: `${task.task_family} · ${task.status} · ${task.last_activity_at}`}),
    el("div", {class: "metric-grid"}, [
      metricCard("Working tokens", task.deduplicated_tokens && task.deduplicated_tokens.working),
      metricCard("Full context", task.deduplicated_tokens && task.deduplicated_tokens.full_context),
      metricCard("Wall-clock", task.timing && task.timing.wall_clock, {duration: true}),
    ]),
    el("section", {class: "section"}, [el("h3", {text: "Phase allocation"}), phaseFigure(phase, "Selected task working tokens by phase")]),
    el("section", {class: "section"}, [
      el("h3", {text: "Deterministic facts"}),
      el("dl", {class: "divider-list"}, [
        el("div", {class: "divider-row"}, [el("dt", {text: "Sessions"}), el("dd", {text: factText(task.counts && task.counts.sessions)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Tool calls"}), el("dd", {text: factText(task.counts && task.counts.tool_calls)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Test runs"}), el("dd", {text: factText(task.counts && task.counts.test_runs)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Retry evidence"}), el("dd", {text: factText(task.counts && task.counts.test_retries)})]),
      ]),
    ]),
    el("section", {class: "section"}, [el("h3", {text: "Semantic timeline"}),
      dataTable("Model-reported timeline", ["Kind", "Phase", "Cause", "Note", "Confidence / provenance"], timeline.map(item => [item.kind, item.phase || "—", item.cause, item.note || "—", `${String(item.confidence)} · ${item.provenance || "provenance unavailable"}`]))]),
    el("section", {class: "section"}, [el("h3", {text: "Test and retry evidence"}),
      dataTable("Detected test evidence", ["Scope", "Failure cause", "Retry kind", "Phase", "Cause", "Count"], testRows.map(item => [
        item.scope, item.failure_cause, item.retry_kind, item.phase, item.cause, factText(item.count),
      ]))]),
    el("section", {class: "section"}, [el("h3", {text: "Pilot and trend context"}),
      el("dl", {class: "divider-list"}, [
        el("div", {class: "divider-row"}, [el("dt", {text: "Pilot status"}), el("dd", {text: pilot.status || "Unavailable"})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Semantic coverage"}), el("dd", {text: factPercent(pilot.semantic_coverage)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Pilot caveats"}), el("dd", {text: caveatText(pilot.caveats)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Trend status"}), el("dd", {text: trendStatus(trend)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Baseline working tokens"}), el("dd", {text: factText(trend.baseline_working_tokens)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Token growth"}), el("dd", {text: factText(trend.token_growth)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Signal growth"}), el("dd", {text: factText(trend.signal_growth)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Trend caveats"}), el("dd", {text: caveatText(trend.caveats)})]),
      ])]),
  ]);
}

export function renderTasks(state, actions) {
  const families = ["all", ...new Set(state.tasks.map(item => item.task_family || "unclassified"))];
  const statuses = ["all", ...new Set(state.tasks.map(item => item.status || "unknown"))];
  const visible = state.tasks.filter(item =>
    (state.taskFamily === "all" || item.task_family === state.taskFamily)
    && (state.taskStatus === "all" || item.status === state.taskStatus));
  const listRows = visible.map(task => {
    const button = el("button", {class: "row-button mono", type: "button", "aria-pressed": task.task_ref === state.taskRef, text: task.task_ref});
    button.addEventListener("click", () => actions.selectTask(task.task_ref));
    return [button, task.task_family || "Unclassified", task.status, factText(task.deduplicated_tokens && task.deduplicated_tokens.working)];
  });
  return el("div", {}, [
    pageHeader("Tasks", "Browse reconciled tasks, then inspect one evidence record in context."),
    el("div", {class: "filter-row", "aria-label": "Task family filters"}, families.map(family => filterButton(family, family === state.taskFamily, () => actions.setFilters(family, state.taskStatus)))),
    el("div", {class: "filter-row", "aria-label": "Task status filters"}, statuses.map(status => filterButton(status, status === state.taskStatus, () => actions.setFilters(state.taskFamily, status)))),
    el("div", {class: "tasks-layout"}, [
      el("section", {"aria-labelledby": "task-list-heading"}, [
        el("h2", {id: "task-list-heading", text: "Task list"}),
        dataTable("Tasks after loaded-page filters", ["Task", "Family", "Status", "Working tokens"], listRows),
        state.cursor ? (() => { const more = el("button", {class: "button ghost", type: "button", text: "Load more"}); more.addEventListener("click", actions.loadMore); return more; })() : el("span", {class: "muted", text: "All loaded"}),
      ]),
      renderDetail(selectedTask(state)),
    ]),
  ]);
}
