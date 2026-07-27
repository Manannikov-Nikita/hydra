import {DashboardApi, ApiError} from "./api.js";
import {asyncState, el, emptyState, formatNumber, pageHeader} from "./dom.js";
import {initialState, reduce, ROUTES} from "./state.js";
import {initializeShell, updateShell} from "./views/shell.js";
import {renderOverview} from "./views/overview.js";
import {renderTasks} from "./views/tasks.js";
import {renderCompare} from "./views/compare.js";
import {renderHealth} from "./views/health.js";
import {renderEvidence} from "./views/evidence.js";

const REFRESH_STAGES = Object.freeze(["queued", "running", "succeeded", "partial", "failed"]);
const TERMINAL_REFRESH = new Set(["succeeded", "partial", "failed"]);
const FOCUSABLE = "button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";
const routeView = document.getElementById("route-view");
const routeStatus = document.getElementById("route-status");
const asyncStatus = document.getElementById("async-status");
const liveRegion = document.getElementById("global-live-region");
const refreshButton = document.getElementById("sync-button");
const repairButton = document.getElementById("repair-button");
const repairConfirmation = document.getElementById("repair-confirmation");
const repairConfirmButton = document.getElementById("repair-confirm-button");
const freshnessLabel = document.getElementById("freshness-label");
let state = initialState();
let api = null;
let activeRequest = null;
let routeWorkGeneration = 0;
let lastAnnouncement = null;
let lastRefreshProgress = null;
let dataRevision = 0;
let changePoll = null;

function dispatch(action) {
  state = reduce(state, action);
}

function announce(message) {
  routeStatus.textContent = message;
  if (message === lastAnnouncement) return;
  lastAnnouncement = message;
  liveRegion.textContent = message;
}

function showAsyncState(kind, title, detail, retry = null, actionLabel = "Retry") {
  asyncStatus.replaceChildren(asyncState(kind, title, detail, retry, actionLabel));
}

function clearAsyncState() {
  asyncStatus.replaceChildren();
}

function routeFromLocation() {
  const candidate = window.location.hash.slice(1);
  return ROUTES.includes(candidate) ? candidate : "overview";
}

function navigate(route) {
  const next = ROUTES.includes(route) ? route : "overview";
  history.pushState(null, "", `#${next}`);
  dispatch({type: "route", route: next});
  loadRoute();
}

function abortPrevious() {
  if (activeRequest) activeRequest.abort();
  activeRequest = new AbortController();
  return activeRequest.signal;
}

function beginRouteWork() {
  routeWorkGeneration += 1;
  if (activeRequest) activeRequest.abort();
  activeRequest = null;
  dispatch({type: "busy", value: false});
  clearAsyncState();
  return {
    generation: routeWorkGeneration,
    route: state.route,
    projectRef: state.projectRef,
  };
}

function isCurrentRouteWork(context) {
  return Boolean(context)
    && context.generation === routeWorkGeneration
    && context.route === state.route
    && context.projectRef === state.projectRef;
}

function routeNeedsRefresh(route = state.route) {
  const project = state.snapshot && state.snapshot.project;
  const freshness = project && project.freshness_state;
  const taskRoute = route === "tasks" || route === "compare";
  return taskRoute && (freshness === "stale" || freshness === "unavailable");
}

function refreshRequiredRoute(route) {
  const comparing = route === "compare";
  const title = comparing ? "Compare" : "Tasks";
  const detail = comparing
    ? "Refresh Hydra before comparing task evidence."
    : "Refresh Hydra before browsing task evidence.";
  return el("div", {class: "route-stack"}, [
    pageHeader(title, "Validated task evidence is unavailable until Sync now completes."),
    emptyState("Sync required", detail.replaceAll("Refresh", "Sync")),
  ]);
}

function refreshProgressCount(fact) {
  if (Number.isFinite(fact) && fact >= 0) return formatNumber(fact);
  if (!fact || fact.value === null) {
    return fact && Number.isFinite(fact.lower_bound) && fact.lower_bound > 0
      ? `≥ ${formatNumber(fact.lower_bound)}`
      : "unavailable";
  }
  const value = fact.value === 0 ? "0" : formatNumber(fact.value);
  return Number.isFinite(fact.lower_bound)
    && fact.lower_bound > 0
    && fact.lower_bound !== fact.value
    ? `${value} (≥ ${formatNumber(fact.lower_bound)})`
    : value;
}

function refreshProgressDetail(current) {
  const progress = current && current.progress ? current.progress : {};
  return `Queued ${refreshProgressCount(progress.sources_queued)}; `
    + `processed ${refreshProgressCount(progress.sources_processed)}; `
    + `new bytes ${refreshProgressCount(progress.new_bytes)}`;
}

function refreshDiagnosticDetail(current) {
  const codes = new Set(current && Array.isArray(current.diagnostic_codes)
    ? current.diagnostic_codes : []);
  if (codes.has("source_changed")) {
    return "A live task changed during refresh. Stable evidence remains visible. "
      + "Wait for the active task to finish writing, then sync again.";
  }
  if (codes.has("database_busy")) {
    return "Hydra is finishing another write. Stable evidence remains visible. Sync again shortly.";
  }
  if (codes.has("reconciliation_stale")) {
    return "New observations arrived before reconciliation finished. Stable evidence remains visible. Sync again.";
  }
  if (current && current.state === "partial") {
    return "Some sources could not be synced. Stable evidence remains visible. Sync again when the source is ready.";
  }
  return "The sync could not finish. Stable evidence remains visible. Try again.";
}

function errorMessage(code) {
  const messages = {
    reopen_dashboard: "Reopen the dashboard from Hydra to continue.",
    storage_unavailable: "Hydra storage is temporarily unavailable.",
    not_found: "The selected evidence disappeared. Choose another record.",
  };
  return messages[code] || "The request could not be completed.";
}

function renderError(code, retry = loadRoute) {
  showAsyncState("error", "Evidence unavailable", errorMessage(code), retry);
  announce("Evidence unavailable");
}

async function runAsync(title, operation, retry) {
  const context = beginRouteWork();
  showAsyncState("loading", title, "Existing evidence remains visible while Hydra works.");
  announce(title);
  try {
    const result = await operation(context);
    if (!isCurrentRouteWork(context)) return null;
    clearAsyncState();
    return result;
  } catch (error) {
    if (error && error.name === "AbortError") return null;
    if (!isCurrentRouteWork(context)) return null;
    dispatch({type: "busy", value: false});
    renderError(error instanceof ApiError ? error.code : "internal_failure", retry);
    return null;
  }
}

function actions() {
  return {
    navigate,
    selectProject: projectRef => {
      dispatch({type: "project", projectRef});
      return runAsync("Changing project", async context => {
        const snapshot = await loadSnapshot(projectRef, context);
        if (!snapshot || !isCurrentRouteWork(context)) return;
        if (!routeNeedsRefresh()
          && (state.route === "tasks" || state.route === "compare")) {
          await loadAllTasks(context);
        }
        if (!isCurrentRouteWork(context)) return;
        announce("Project changed");
      }, () => actions().selectProject(projectRef));
    },
    selectTask: taskRef => {
      dispatch({type: "task", taskRef});
      renderRoute();
      const heading = document.getElementById("task-detail-heading");
      if (heading) heading.focus({preventScroll: true});
      announce("Task detail opened");
    },
    setFilters: (family, status) => {
      dispatch({type: "filters", family, status});
      renderRoute();
    },
    loadMore: () => runAsync("Loading tasks", context => loadAllTasks(context), () => actions().loadMore()),
    compare: (left, right) => runAsync(
      "Comparing tasks", context => compareTasks(left, right, context),
      () => actions().compare(left, right),
    ),
    findEvidence: (projectRef, evidenceRef) => runAsync(
      "Finding evidence", context => findEvidence(projectRef, evidenceRef, context),
      () => actions().findEvidence(projectRef, evidenceRef),
    ),
  };
}

function routeFocusIndex() {
  if (!routeView.contains(document.activeElement)) return -1;
  return Array.from(routeView.querySelectorAll(FOCUSABLE)).indexOf(document.activeElement);
}

function renderRoute() {
  const focusIndex = routeFocusIndex();
  updateShell(state.snapshot, state);
  const routeActions = actions();
  const views = {
    overview: () => renderOverview(state.snapshot, routeActions),
    tasks: () => renderTasks(state, routeActions),
    compare: () => renderCompare(state, routeActions),
    health: () => renderHealth(state.snapshot),
    evidence: () => renderEvidence(state, routeActions),
  };
  const view = routeNeedsRefresh()
    ? refreshRequiredRoute(state.route)
    : views[state.route]
      ? views[state.route]()
      : emptyState("Unknown view", "Choose a dashboard destination.");
  routeView.replaceChildren(view);
  if (focusIndex >= 0) {
    const nextFocus = routeView.querySelectorAll(FOCUSABLE)[focusIndex];
    if (nextFocus) nextFocus.focus({preventScroll: true});
  }
  const project = state.snapshot && state.snapshot.project;
  freshnessLabel.textContent = project
    ? `${project.display_name} · ${project.freshness_state}`
    : "No project selected";
}

async function loadSnapshot(projectRef = state.projectRef, context) {
  dispatch({type: "busy", value: true});
  const snapshot = await api.snapshot(projectRef, null, abortPrevious());
  if (!isCurrentRouteWork(context)) return null;
  dispatch({type: "snapshot", snapshot});
  dataRevision = Number.isInteger(snapshot.data_revision) && snapshot.data_revision >= 0 ? snapshot.data_revision : dataRevision;
  context.projectRef = state.projectRef;
  renderRoute();
  return snapshot;
}

async function loadAllTasks(context) {
  if (!isCurrentRouteWork(context)) return false;
  if (!state.projectRef) return true;
  const projectRef = state.projectRef;
  let cursor = state.cursor;
  const collected = state.tasks.length === 0 ? [] : state.tasks.slice();
  do {
    const page = await api.tasks(projectRef, cursor, 50, abortPrevious());
    if (!isCurrentRouteWork(context)) return false;
    collected.push(...page.items);
    cursor = page.page && page.page.next_cursor;
  } while (cursor);
  if (!isCurrentRouteWork(context)) return false;
  dispatch({type: "tasks", items: collected, cursor: null});
  renderRoute();
  return true;
}

async function compareTasks(left, right, context) {
  if (!isCurrentRouteWork(context) || !state.projectRef || !left || !right) return null;
  const projectRef = state.projectRef;
  dispatch({type: "busy", value: true});
  const comparison = await api.compare(projectRef, left, right, abortPrevious());
  if (!isCurrentRouteWork(context)) return null;
  dispatch({type: "comparison", comparison});
  renderRoute();
  announce(comparison.verdict === "comparable"
    ? "Comparable evidence loaded" : "Not-comparable evidence loaded");
  return comparison;
}

async function findEvidence(projectRef, evidenceRef, context) {
  if (!isCurrentRouteWork(context) || !projectRef) return null;
  dispatch({type: "busy", value: true});
  const evidence = await api.evidence(projectRef, evidenceRef, abortPrevious());
  if (!isCurrentRouteWork(context)) return null;
  dispatch({type: "evidence", evidence});
  renderRoute();
  announce("One evidence record loaded");
  return evidence;
}

async function loadRoute() {
  const context = beginRouteWork();
  const needsTasks = state.route === "tasks" || state.route === "compare";
  renderRoute();
  if (!state.snapshot || needsTasks && state.tasks.length === 0) {
    showAsyncState("loading", "Loading evidence", "The selected view will appear when its public facts are ready.");
  }
  try {
    if (!state.snapshot) {
      const snapshot = await loadSnapshot(state.projectRef, context);
      if (!snapshot || !isCurrentRouteWork(context)) return;
    }
    if (routeNeedsRefresh()) {
      clearAsyncState();
      announce(`${state.route} requires Sync`);
      return;
    }
    if (needsTasks && state.tasks.length === 0) {
      const loaded = await loadAllTasks(context);
      if (!loaded || !isCurrentRouteWork(context)) return;
    }
    if (!isCurrentRouteWork(context)) return;
    renderRoute();
    clearAsyncState();
    announce(`${state.route} ready`);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (!isCurrentRouteWork(context)) return;
    dispatch({type: "busy", value: false});
    renderError(error instanceof ApiError ? error.code : "internal_failure");
  }
}

async function reloadAfterRefresh() {
  const preferredProject = state.projectRef;
  dispatch({type: "reset_after_refresh"});
  const context = beginRouteWork();
  try {
    const snapshot = await loadSnapshot(preferredProject, context);
    if (!snapshot || !isCurrentRouteWork(context)) return false;
  } catch (error) {
    if (error && error.name === "AbortError") return false;
    if (!isCurrentRouteWork(context)) return false;
    if (!(error instanceof ApiError) || error.code !== "not_found") throw error;
    const snapshot = await loadSnapshot(null, context);
    if (!snapshot || !isCurrentRouteWork(context)) return false;
  }
  if (!routeNeedsRefresh()
    && (state.route === "tasks" || state.route === "compare")
    && state.projectRef) {
    const loaded = await loadAllTasks(context);
    if (!loaded || !isCurrentRouteWork(context)) return false;
  }
  return isCurrentRouteWork(context);
}

function announceRefresh(current) {
  const stage = REFRESH_STAGES.includes(current.stage) ? current.stage : current.state;
  const progressKey = `${current.state}:${stage}`;
  if (progressKey === lastRefreshProgress) return;
  lastRefreshProgress = progressKey;
  if (!TERMINAL_REFRESH.has(current.state)) announce(`Sync ${stage}`);
}

async function pollRefresh(refreshRef) {
  for (;;) {
    await new Promise(resolve => window.setTimeout(resolve, 1000));
    const current = await api.syncStatus(refreshRef);
    showAsyncState("loading", "Syncing evidence", refreshProgressDetail(current));
    announceRefresh(current);
    if (!TERMINAL_REFRESH.has(current.state)) continue;

    const reloaded = await reloadAfterRefresh();
    refreshButton.disabled = false;
    refreshButton.removeAttribute("aria-busy");
    refreshButton.textContent = current.state === "partial"
      ? "Sync again" : current.state === "failed" ? "Retry" : "Sync now";
    if (!reloaded) return;
    if (current.state === "partial" || current.state === "failed") {
      const partial = current.state === "partial";
      const title = partial ? "Sync needs another pass" : "Sync failed";
      const detail = refreshDiagnosticDetail(current);
      showAsyncState(
        partial ? "notice" : "error", title, detail, startRefresh,
        partial ? "Sync again" : "Retry",
      );
      routeStatus.textContent = "";
      announceRefreshOutcome(`${title}. ${detail}`);
    } else {
      clearAsyncState();
      announce("Sync complete");
    }
    return;
  }
}

function announceRefreshOutcome(message) {
  if (message === lastAnnouncement) return;
  lastAnnouncement = message;
  liveRegion.textContent = message;
}

async function startRefresh() {
  if (!api || refreshButton.disabled) return;
  refreshButton.disabled = true;
  refreshButton.setAttribute("aria-busy", "true");
  lastRefreshProgress = null;
  showAsyncState("loading", "Syncing evidence", "Current evidence stays visible while new queued telemetry is reconciled.");
  try {
    const started = await api.startSync();
    announce(started.reused ? "Existing sync reused" : "Sync started");
    await pollRefresh(started.sync_ref);
  } catch (error) {
    refreshButton.disabled = false;
    refreshButton.removeAttribute("aria-busy");
    refreshButton.textContent = "Retry sync";
    const code = error instanceof ApiError ? error.code : "internal_failure";
    showAsyncState("error", "Sync failed", errorMessage(code), startRefresh);
    routeStatus.textContent = "";
    announceRefreshOutcome("Sync failed");
  }
}

refreshButton.addEventListener("click", startRefresh);
repairButton.addEventListener("click", () => {
  const visible = repairConfirmation.hidden;
  repairConfirmation.hidden = !visible;
  repairButton.setAttribute("aria-expanded", String(visible));
  if (visible) repairConfirmButton.focus({preventScroll: true});
});
repairConfirmButton.addEventListener("click", async () => {
  repairConfirmButton.disabled = true;
  showAsyncState("loading", "Repairing history", "Repair history is walking all trusted telemetry files; existing evidence remains visible.");
  try {
    const started = await api.startRepair();
    repairConfirmation.hidden = true;
    repairButton.setAttribute("aria-expanded", "false");
    await pollRefresh(started.sync_ref);
  } catch (error) {
    showAsyncState("error", "Repair history failed", errorMessage(error instanceof ApiError ? error.code : "internal_failure"));
  } finally { repairConfirmButton.disabled = false; }
});

async function pollChanges() {
  if (!api) return;
  try {
    const changes = await api.changes(dataRevision);
    if (Number.isInteger(changes.data_revision) && changes.data_revision > dataRevision) dataRevision = changes.data_revision;
    if (changes.changed && !refreshButton.disabled) await reloadAfterRefresh();
  } catch (_) { /* A later one-second poll retries without exposing transport details. */ }
}

function startChangePolling() {
  if (changePoll !== null) window.clearInterval(changePoll);
  changePoll = window.setInterval(pollChanges, 1000);
}
window.addEventListener("popstate", () => {
  dispatch({type: "route", route: routeFromLocation()});
  loadRoute();
});
window.addEventListener("hydra-dashboard-ready", event => {
  const credential = event.detail.takeCredential();
  if (!credential) {
    renderError("reopen_dashboard");
    return;
  }
  api = new DashboardApi(credential, event.detail.clearCredential);
  initializeShell(actions());
  dispatch({type: "route", route: routeFromLocation()});
  loadRoute();
  startChangePolling();
  api.sync().then(current => {
    if (current && (current.state === "queued" || current.state === "running")) pollRefresh(current.sync_ref);
  }).catch(() => undefined);
}, {once: true});
