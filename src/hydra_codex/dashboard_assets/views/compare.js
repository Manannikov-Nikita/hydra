import {dataTable, el, factText, pageHeader} from "../dom.js";

export function renderCompare(state, actions) {
  const comparison = state.comparison;
  const baselineRef = comparison && comparison.baseline_ref
    ? comparison.baseline_ref : state.tasks[0] && state.tasks[0].task_ref;
  const currentRef = comparison && comparison.current_ref
    ? comparison.current_ref : state.tasks[1] && state.tasks[1].task_ref;
  const taskRefs = state.tasks.map(task => task.task_ref);
  for (const taskRef of [baselineRef, currentRef]) {
    if (taskRef && !taskRefs.includes(taskRef)) taskRefs.push(taskRef);
  }
  const options = taskRefs.map(taskRef => el("option", {value: taskRef, text: taskRef}));
  const left = el("select", {id: "compare-left", name: "left"}, options.map(option => option.cloneNode(true)));
  const right = el("select", {id: "compare-right", name: "right"}, options.map(option => option.cloneNode(true)));
  if (baselineRef) left.value = baselineRef;
  if (currentRef) right.value = currentRef;
  const action = el("button", {class: "button primary", type: "button", text: "Compare"});
  action.addEventListener("click", () => actions.compare(left.value, right.value));
  const comparable = comparison && comparison.verdict === "comparable";
  const rows = comparison && comparison.metrics ? Object.entries(comparison.metrics).map(([name, metric]) => {
    let interpretation = "Raw evidence only";
    if (comparable && metric.delta && Number.isFinite(metric.delta.value)) {
      interpretation = metric.delta.value > 0 ? "Higher" : metric.delta.value < 0 ? "Lower" : "No change";
    }
    return [name, factText(metric.baseline), factText(metric.current), factText(metric.delta), factText(metric.percent_change), interpretation];
  }) : [];
  const caveats = comparison && Array.isArray(comparison.caveats) ? comparison.caveats : [];
  return el("div", {}, [
    pageHeader("Compare", "Compare two tasks from the selected project without inferring quality."),
    el("div", {class: "control-row"}, [
      el("div", {class: "field"}, [el("label", {for: "compare-left", text: "Baseline"}), left]),
      el("div", {class: "field"}, [el("label", {for: "compare-right", text: "Current"}), right]),
      action,
    ]),
    comparison ? el("section", {class: "section", "aria-labelledby": "comparison-heading"}, [
      el("h2", {id: "comparison-heading", text: comparable ? "Comparable evidence" : "Not comparable"}),
      el("p", {class: "muted mono", text: `Baseline ${comparison.baseline_ref} · Current ${comparison.current_ref}`}),
      el("p", {class: "muted", text: comparable ? "Directional wording describes magnitude only." : (comparison.reasons || []).join(", ") || "Comparison evidence is incomplete."}),
      dataTable("Complete comparison metrics", ["Metric", "Baseline", "Current", "Delta", "Percent", "Interpretation"], rows),
      el("h3", {class: "comparison-caveats-heading", text: "Comparison caveats"}),
      caveats.length
        ? el("ul", {class: "caveat-list"}, caveats.map(caveat => el("li", {text: caveat})))
        : el("p", {class: "muted", text: "No comparison-level caveats reported."}),
    ]) : el("p", {class: "empty-state", text: "Choose two tasks to compare."}),
  ]);
}
