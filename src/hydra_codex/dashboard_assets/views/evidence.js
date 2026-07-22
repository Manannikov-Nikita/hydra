import {el, factDetail, factText, pageHeader} from "../dom.js";

const EVIDENCE_PATTERN = /^ev_[0-9a-f]{16}$/;

export function renderEvidence(state, actions) {
  const input = el("input", {
    id: "evidence-ref", name: "evidence", pattern: "ev_[0-9a-f]{16}",
    placeholder: "ev_0123456789abcdef", "aria-describedby": "evidence-help",
  });
  const action = el("button", {class: "button primary", type: "button", text: "Find evidence"});
  action.addEventListener("click", () => {
    if (EVIDENCE_PATTERN.test(input.value)) actions.findEvidence(state.projectRef, input.value);
    else {
      input.setCustomValidity("Use an evidence reference such as ev_0123456789abcdef");
      input.reportValidity();
    }
  });
  input.addEventListener("input", () => input.setCustomValidity(""));
  const record = state.evidence;
  return el("div", {}, [
    pageHeader("Evidence", "Resolve one project-scoped evidence record at a time."),
    el("div", {class: "control-row"}, [
      el("div", {class: "field"}, [el("label", {for: "evidence-ref", text: "Evidence reference"}), input,
        el("span", {id: "evidence-help", class: "metric-detail", text: "Format: ev_ followed by 16 lowercase hexadecimal characters."})]),
      action,
    ]),
    record ? el("section", {class: "section", "aria-labelledby": "evidence-record-heading"}, [
      el("h2", {id: "evidence-record-heading", class: "mono", text: record.evidence_id}),
      el("dl", {class: "divider-list"}, [
        el("div", {class: "divider-row"}, [el("dt", {text: "Fact"}), el("dd", {text: record.fact})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Value"}), el("dd", {text: factText(record)})]),
        el("div", {class: "divider-row"}, [el("dt", {text: "Provenance and caveats"}), el("dd", {text: factDetail(record)})]),
      ]),
    ]) : el("p", {class: "empty-state", text: "No evidence record requested."}),
  ]);
}
