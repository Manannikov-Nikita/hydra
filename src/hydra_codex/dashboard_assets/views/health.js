import {el, factText, pageHeader} from "../dom.js";

function rows(items) {
  return el("dl", {class: "divider-list"}, items.map(([label, value]) =>
    el("div", {class: "divider-row"}, [el("dt", {text: label}), el("dd", {text: value})])));
}

export function renderHealth(snapshot) {
  const project = snapshot && snapshot.project;
  const doctor = project && project.system_health && project.system_health.doctor;
  const checks = doctor && Array.isArray(doctor.checks) ? doctor.checks : [];
  const storage = project && project.storage;
  const current = storage && storage.current ? storage.current : {};
  const diagnostics = storage && Array.isArray(storage.diagnostics) ? storage.diagnostics : [];
  return el("div", {}, [
    pageHeader("System health", "Transport, storage, schema and instrumentation evidence without a synthetic rating."),
    el("section", {class: "section"}, [el("h2", {text: "Doctor · global launch context"}), rows(checks.map(item => [item.code === "project_resolution" ? "Project resolution · global launch context" : item.code, item.status]))]),
    el("section", {class: "section"}, [el("h2", {text: "Storage"}), rows(Object.entries(current).map(([name, fact]) => [name, factText(fact)]))]),
    el("section", {class: "section"}, [el("h2", {text: "Diagnostics"}), rows(diagnostics.map(item => [item.code, item.severity]))]),
  ]);
}
