const URL_ATTRIBUTES = new Set(["href", "src", "action", "formaction", "poster"]);
const PHASE_ORDER = Object.freeze([
  "understand", "research", "design", "implement", "test_targeted",
  "test_full", "review", "fix", "docs", "browser_qa", "release",
  "wait_external", "unclassified",
]);
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
    } else if (/^(aria-|data-)[a-z0-9_-]+$/.test(name) || /^(id|class|type|name|value|for|scope|colspan|tabindex|disabled|role|pattern|placeholder|min|max)$/.test(name)) {
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
  return `${formatNumber(value)} ${fact.unit || ""}`.trim();
}

function formatFactLowerBound(fact, options) {
  if (options.duration) return formatDuration(fact.lower_bound);
  return formatFactNumber(fact, fact.lower_bound, options);
}

export function factValueText(fact, options = {}) {
  if (!fact || fact.value === null) {
    if (fact && Number.isFinite(fact.lower_bound)) {
      return `At least ${formatFactLowerBound(fact, options)}`;
    }
    return "Unavailable";
  }
  const value = fact.value === 0
    ? formatFactNumber(fact, 0, options)
    : formatFactNumber(fact, fact.value, options);
  if (Number.isFinite(fact.lower_bound)) {
    const lowerBound = formatFactLowerBound(fact, options);
    return `${value} · lower bound ${lowerBound}`;
  }
  return value;
}

export function factDetail(fact) {
  if (!fact) return "provenance unavailable · no observation";
  const caveats = Array.isArray(fact.caveats) && fact.caveats.length ? ` · ${fact.caveats.join(", ")}` : "";
  return `${fact.provenance || "provenance unavailable"}${caveats}`;
}

export function factText(fact, options = {}) {
  return `${factValueText(fact, options)} · ${factDetail(fact)}`;
}

export function factPercent(fact) {
  return factText(fact, {percent: true});
}

export function metricCard(label, fact, options = {}) {
  const value = factValueText(fact, options);
  return el("div", {class: "metric-card"}, [
    el("span", {class: "metric-label", text: label}),
    el("strong", {class: "metric-value", text: value}),
    el("span", {class: "metric-detail", text: factDetail(fact)}),
  ]);
}

function phaseFact(allocation, phase) {
  if (phase === "unclassified") return allocation && allocation.unclassified && allocation.unclassified.working;
  return allocation && allocation.phases && allocation.phases[phase] && allocation.phases[phase].working;
}

export function phaseFigure(allocation, label = "Working tokens by phase") {
  const entries = PHASE_ORDER.map(phase => ({phase, fact: phaseFact(allocation, phase)}));
  const total = entries.reduce((sum, entry) => sum + (Number.isFinite(entry.fact && entry.fact.value) && entry.fact.value >= 0 ? entry.fact.value : 0), 0);
  const description = entries.map(entry => `${entry.phase}: ${factText(entry.fact)}`).join("; ");
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
    return el("li", {}, [
      el("span", {class: "phase-label"}, [
        el("span", {class: `phase-swatch phase-${PHASE_COLORS[entry.phase]}`, "aria-hidden": "true"}),
        el("span", {text: entry.phase}),
      ]),
      el("span", {class: "phase-amount", text: `${factText(entry.fact)} · ${formatNumber(percentage)}%`}),
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

export function asyncState(kind, title, detail, retry = null) {
  const stateKind = kind === "error" ? "error" : "loading";
  const region = el("div", {
    class: `async-state ${stateKind}`,
    role: "group",
    "aria-label": stateKind === "error" ? "Request error" : "Request progress",
    "aria-busy": stateKind === "loading",
  }, [
    el("strong", {text: title}),
    el("span", {class: "muted", text: detail}),
  ]);
  if (stateKind === "error" && typeof retry === "function") {
    const action = el("button", {class: "button ghost", type: "button", text: "Retry"});
    action.addEventListener("click", retry);
    region.append(action);
  }
  return region;
}
