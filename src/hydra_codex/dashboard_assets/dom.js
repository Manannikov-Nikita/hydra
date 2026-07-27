const URL_ATTRIBUTES = new Set(["href", "src", "action", "formaction", "poster"]);
const PHASE_ORDER = Object.freeze([
  "understand", "research", "design", "implement", "test_targeted",
  "test_full", "review", "fix", "docs", "browser_qa", "release",
  "wait_external", "unclassified",
]);
const PHASE_LABELS = Object.freeze({
  "understand": "Understand",
  "research": "Research",
  "design": "Design",
  "implement": "Implement",
  "test_targeted": "Targeted tests",
  "test_full": "Full suite",
  "review": "Review",
  "fix": "Fix",
  "docs": "Docs",
  "browser_qa": "Browser QA",
  "release": "Release",
  "wait_external": "External wait",
  "unclassified": "Unclassified",
});
const COMPACT_SUFFIXES = Object.freeze(["", "k", "M", "B", "T"]);
export const PHASE_COLORS = Object.freeze({
  "understand": "blue", "research": "blue", "design": "blue",
  "implement": "orange", "docs": "orange",
  "test_targeted": "green", "test_full": "green", "browser_qa": "green",
  "review": "purple", "release": "purple", "fix": "red",
  "wait_external": "neutral", "unclassified": "neutral",
});

export function el(tag, attributes = {}, children = []) {
  if (!/^[a-z][a-z0-9-]*$/.test(tag)) throw new Error("invalid element");
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "text") node.textContent = String(value);
    else if (name.startsWith("on") || name === "html" || name === "style" || name === "srcdoc") {
      throw new Error("unsafe attribute");
    } else if (URL_ATTRIBUTES.has(name)) {
      if (name !== "href" || typeof value !== "string" || !value.startsWith("#")) throw new Error("unsafe URL attribute");
      node.setAttribute(name, value);
    } else if (/^(aria-|data-)[a-z0-9_-]+$/.test(name) || /^(id|class|type|name|value|for|scope|colspan|tabindex|disabled|role|title|pattern|placeholder|min|max)$/.test(name)) {
      if ((value !== false || name.startsWith("aria-")) && value !== null && value !== undefined) {
        const rendered = name.startsWith("aria-") ? String(value) : value === true ? "" : String(value);
        node.setAttribute(name, rendered);
      }
    } else {
      throw new Error("unexpected attribute");
    }
  }
  for (const child of children) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function formatNumber(value) {
  if (!Number.isFinite(value)) return "Unavailable";
  return new Intl.NumberFormat(undefined, {maximumFractionDigits: 2}).format(value);
}

export function formatCompactNumber(value) {
  if (!Number.isFinite(value)) return "Unavailable";
  const sign = value < 0 ? "-" : "";
  const absolute = Math.abs(value);
  let suffixIndex = absolute < 1000
    ? 0
    : Math.min(Math.floor(Math.log10(absolute) / 3), COMPACT_SUFFIXES.length - 1);
  let scaled = absolute / 1000 ** suffixIndex;
  let fractionDigits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
  let rounded = Number(scaled.toFixed(fractionDigits));
  if (rounded >= 1000 && suffixIndex < COMPACT_SUFFIXES.length - 1) {
    suffixIndex += 1;
    scaled = absolute / 1000 ** suffixIndex;
    fractionDigits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
    rounded = Number(scaled.toFixed(fractionDigits));
  }
  const rendered = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: fractionDigits,
  }).format(rounded);
  return `${sign}${rendered}${COMPACT_SUFFIXES[suffixIndex]}`;
}

export function formatDuration(value) {
  if (!Number.isFinite(value)) return "Unavailable";
  if (value < 1000) return `${formatNumber(value)} ms`;
  const seconds = Math.round(value / 1000);
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}:${String(seconds % 60).padStart(2, "0")}` : `${seconds} s`;
}

function formatFactNumber(fact, value, options) {
  if (options.duration) return formatDuration(value);
  if (options.percent) return `${formatNumber(value * 100)}%`;
  return `${formatCompactNumber(value)} ${fact.unit || ""}`.trim();
}

function formatExactFactNumber(fact, value, options) {
  if (options.duration) return formatDuration(value);
  if (options.percent) return `${formatNumber(value * 100)}%`;
  return `${formatNumber(value)} ${fact.unit || ""}`.trim();
}

function formatFactMinimum(fact, options, formatter = formatFactNumber) {
  if (options.duration) return formatDuration(fact.lower_bound);
  return formatter(fact, fact.lower_bound, options);
}

function hasPositiveMinimum(fact) {
  return Number.isFinite(fact && fact.lower_bound)
    && fact.lower_bound > 0;
}

function renderFactValue(fact, options, formatter) {
  if (!fact || fact.value === null) {
    if (fact && Number.isFinite(fact.lower_bound) && fact.lower_bound > 0) {
      return `≥ ${formatFactMinimum(fact, options, formatter)}`;
    }
    return "Unavailable";
  }
  const value = formatter(fact, fact.value, options);
  if (hasPositiveMinimum(fact) && fact.lower_bound === fact.value) {
    return `≥ ${value}`;
  }
  if (hasPositiveMinimum(fact)) {
    return `${value} · ≥ ${formatFactMinimum(fact, options, formatter)}`;
  }
  return value;
}

export function factValueText(fact, options = {}) {
  return renderFactValue(fact, options, formatFactNumber);
}

export function factAccessibleText(fact, options = {}) {
  return renderFactValue(fact, options, formatExactFactNumber);
}

export function phaseDisplayName(phase) {
  return PHASE_LABELS[phase] || "Unclassified";
}

export function provenanceText(fact) {
  const labels = {
    exact: "Exact",
    derived: "Derived",
    model_reported: "Model reported",
    estimated: "Estimated",
  };
  return labels[fact && fact.provenance] || "Provenance unavailable";
}

export function factDetail(fact) {
  if (!fact) return "provenance unavailable · no observation";
  const caveats = Array.isArray(fact.caveats) && fact.caveats.length ? ` · ${fact.caveats.join(", ")}` : "";
  return `${fact.provenance || "provenance unavailable"}${caveats}`;
}

export function factText(fact, options = {}) {
  return `${factAccessibleText(fact, options)} · ${factDetail(fact)}`;
}

export function factSummaryText(fact, options = {}) {
  return `${factValueText(fact, options)} · ${provenanceText(fact)}`;
}

export function taskDisplay(task) {
  if (task && typeof task.display_name === "string" && task.display_name.trim()) return task.display_name;
  const family = task && typeof task.task_family === "string" && task.task_family.trim() ? task.task_family : null;
  const date = task && typeof task.last_activity_at === "string" && /^\d{4}-\d{2}-\d{2}/.test(task.last_activity_at) ? task.last_activity_at.slice(0, 10) : null;
  const ref = task && typeof task.task_ref === "string" ? task.task_ref.slice(-8) : null;
  return [family, date, ref].filter(Boolean).join(" · ") || "Unlabelled task";
}

export function factPercent(fact) {
  return factText(fact, {percent: true});
}

export function metricCard(label, fact, options = {}) {
  const value = factValueText(fact, options);
  const accessible = factAccessibleText(fact, options);
  return el("div", {class: "metric-card", title: `${label}: ${accessible}`}, [
    el("span", {class: "metric-label", text: label}),
    el("strong", {class: "metric-value", text: value, "aria-label": `${label}: ${accessible}`}),
    el("span", {class: "metric-detail", text: provenanceText(fact)}),
  ]);
}

function phaseFact(allocation, phase) {
  if (phase === "unclassified") return allocation && allocation.unclassified && allocation.unclassified.working;
  return allocation && allocation.phases && allocation.phases[phase] && allocation.phases[phase].working;
}

export function phaseFigure(allocation, label = "Working tokens by phase") {
  const entries = PHASE_ORDER.map(phase => ({phase, fact: phaseFact(allocation, phase)}));
  const total = entries.reduce((sum, entry) => sum + (Number.isFinite(entry.fact && entry.fact.value) && entry.fact.value >= 0 ? entry.fact.value : 0), 0);
  const description = entries.map(entry => `${phaseDisplayName(entry.phase)}: ${factAccessibleText(entry.fact)}; ${provenanceText(entry.fact)}`).join("; ");
  const track = el("div", {class: "phase-track", role: "img", "aria-label": label, "aria-describedby": "phase-description"});
  for (const entry of entries) {
    const raw = Number.isFinite(entry.fact && entry.fact.value) && entry.fact.value >= 0 ? entry.fact.value : 0;
    const percentage = total > 0 ? Math.min(100, Math.max(0, raw / total * 100)) : 0;
    const segment = el("span", {class: `phase-segment phase-${PHASE_COLORS[entry.phase]}`, "aria-hidden": "true"});
    segment.style.flexBasis = `${percentage}%`;
    track.append(segment);
  }
  const legend = el("ul", {class: "phase-legend"}, entries.map(entry => {
    const raw = Number.isFinite(entry.fact && entry.fact.value) && entry.fact.value >= 0 ? entry.fact.value : 0;
    const percentage = total > 0 ? raw / total * 100 : 0;
    const visibleValue = factValueText(entry.fact);
    const accessibleValue = factAccessibleText(entry.fact);
    return el("li", {}, [
      el("span", {class: "phase-label"}, [
        el("span", {class: `phase-swatch phase-${PHASE_COLORS[entry.phase]}`, "aria-hidden": "true"}),
        el("span", {text: phaseDisplayName(entry.phase)}),
      ]),
      el("span", {
        class: "phase-amount",
        text: `${visibleValue} · ${formatNumber(percentage)}%`,
        title: `${accessibleValue} · ${formatNumber(percentage)}%`,
        "aria-label": `${phaseDisplayName(entry.phase)}: ${accessibleValue}; ${formatNumber(percentage)} percent; ${provenanceText(entry.fact)}`,
      }),
      el("span", {class: "phase-provenance", text: provenanceText(entry.fact)}),
    ]);
  }));
  return el("figure", {}, [track, legend, el("figcaption", {id: "phase-description", class: "sr-only", text: description})]);
}

export function dataTable(captionText, headers, rows) {
  const head = el("tr", {}, headers.map(header => el("th", {scope: "col", text: header})));
  const body = el("tbody", {}, rows.map(row => el("tr", {}, row.map(cell => el("td", {}, [cell])))));
  return el("div", {class: "table-wrap"}, [el("table", {}, [
    el("caption", {text: captionText}), el("thead", {}, [head]), body,
  ])]);
}

export function pageHeader(title, description) {
  return el("header", {class: "page-header"}, [el("div", {}, [
    el("h1", {text: title}), el("p", {text: description}),
  ])]);
}

export function emptyState(title, detail) {
  return el("div", {class: "empty-state"}, [el("h2", {text: title}), el("p", {text: detail})]);
}

export function asyncState(kind, title, detail, retry = null, actionLabel = "Retry") {
  const stateKind = kind === "error" ? "error" : kind === "notice" ? "notice" : "loading";
  const region = el("div", {
    class: `async-state ${stateKind}`,
    role: "group",
    "aria-label": stateKind === "error" ? "Request error"
      : stateKind === "notice" ? "Refresh notice" : "Request progress",
    "aria-busy": stateKind === "loading",
  }, [
    el("strong", {text: title}),
    el("span", {class: "muted", text: detail}),
  ]);
  if (stateKind !== "loading" && typeof retry === "function") {
    const action = el("button", {class: "button ghost", type: "button", text: actionLabel});
    action.addEventListener("click", retry);
    region.append(action);
  }
  return region;
}
