import {DashboardApi, ApiError} from "./api.js";
import {asyncState, el, emptyState, formatNumber, pageHeader} from "./dom.js";
import {initialState, reduce, ROUTES, runSerializedPoll} from "./state.js";
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
let changePolling = false;
let changePollPromise = null;
let activeJobPoll = null;
let activeJobKind = null;

export function syncJobKind(kind) {
  return kind === "repair" || kind === "backfill" ? "repair" : "sync";
}

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

function clearAsyncState(force = false) {
  if (activeJobPoll && !force) return;
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

function acknowledgeRevision(candidate) {
  if (Number.isInteger(candidate) && candidate >= 0 && candidate > dataRevision) {
    dataRevision = candidate;
  }
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
          await loadTaskPage(context);
        }
        if (!isCurrentRouteWork(context)) return;
        acknowledgeRevision(snapshot.data_revision);
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
    loadMore: () => runAsync(
      "Loading tasks", context => loadTaskPage(context, true),
      () => actions().loadMore(),
    ),
    compare: (left, right) => runAsync(
      "Comparing tasks", context => compareTasks(left, right, context),
      () => actions().compare(left, right),
    ),
    findEvidence: (projectRef, evidenceRef) => runAsync(
      "Finding evidence", context => findEvidence(projectRef, evidenceRef, context),
      () => actions().findEvidence(projectRef, evidenceRef),
    ),
    copyReference: async (reference, kind) => {
      const label = kind === "project" ? "Project" : "Task";
      const clipboard = navigator.clipboard;
      if (!clipboard || typeof clipboard.writeText !== "function") {
        announce(`${label} ref copy is unavailable. Use the visible short ref instead.`);
        return false;
      }
      try {
        await clipboard.writeText(reference);
        announce(`${label} ref copied`);
        return true;
      } catch (_) {
        announce(`Could not copy ${label.toLowerCase()} ref. Use the visible short ref instead.`);
        return false;
      }
    },
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
  context.projectRef = state.projectRef;
  renderRoute();
  return snapshot;
}

async function loadTaskPage(context, append = false) {
  if (!isCurrentRouteWork(context)) return false;
  if (!state.projectRef) return true;
  const projectRef = state.projectRef;
  const cursor = append ? state.cursor : null;
  if (append && !cursor) return true;
  const page = await api.tasks(projectRef, cursor, 50, abortPrevious());
  if (!isCurrentRouteWork(context)) return false;
  dispatch({
    type: append ? "append_tasks" : "tasks",
    items: page.items,
    cursor: page.page && page.page.next_cursor,
  });
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
      const loaded = await loadTaskPage(context);
      if (!loaded || !isCurrentRouteWork(context)) return;
    }
    if (!isCurrentRouteWork(context)) return;
    acknowledgeRevision(state.snapshot && state.snapshot.data_revision);
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

async function reloadAfterRefresh(acknowledge = true) {
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
    const loaded = await loadTaskPage(context);
    if (!loaded || !isCurrentRouteWork(context)) return false;
  }
  if (!isCurrentRouteWork(context)) return false;
  if (acknowledge) acknowledgeRevision(state.snapshot && state.snapshot.data_revision);
  return true;
}

function announceRefresh(current) {
  const jobKind = activeJobKind || "sync";
  const stage = REFRESH_STAGES.includes(current.stage) ? current.stage : current.state;
  const progressKey = `${jobKind}:${current.state}:${stage}`;
  if (progressKey === lastRefreshProgress) return;
  lastRefreshProgress = progressKey;
  if (!TERMINAL_REFRESH.has(current.state)) {
    if (jobKind === "repair") announce(`Repair ${stage}`);
    else announce(`Sync ${stage}`);
  }
}

async function pollRefresh(refreshRef, jobKind, retry) {
  for (;;) {
    await new Promise(resolve => window.setTimeout(resolve, 1000));
    const current = await api.syncStatus(refreshRef);
    if (jobKind === "repair") {
      showAsyncState("loading", "Repairing history", refreshProgressDetail(current));
    } else {
      showAsyncState("loading", "Syncing evidence", refreshProgressDetail(current));
    }
    announceRefresh(current);
    if (!TERMINAL_REFRESH.has(current.state)) continue;

    const reloaded = await reloadAfterRefresh();
    if (!reloaded) return current;
    if (current.state === "partial" || current.state === "failed") {
      const partial = current.state === "partial";
      const title = jobKind === "repair"
        ? partial ? "Repair needs another pass" : "Repair history failed"
        : partial ? "Sync needs another pass" : "Sync failed";
      const detail = refreshDiagnosticDetail(current);
      showAsyncState(
        partial ? "notice" : "error", title, detail, retry,
        partial ? jobKind === "repair" ? "Repair again" : "Sync again" : "Retry",
      );
      routeStatus.textContent = "";
      announceRefreshOutcome(`${title}. ${detail}`);
    } else {
      clearAsyncState(true);
      announce(jobKind === "repair" ? "Repair complete" : "Sync complete");
    }
    return current;
  }
}

function announceRefreshOutcome(message) {
  if (message === lastAnnouncement) return;
  lastAnnouncement = message;
  liveRegion.textContent = message;
}

function setMutatingBusy(jobKind, busy) {
  refreshButton.disabled = busy;
  repairButton.disabled = busy;
  repairConfirmButton.disabled = busy;
  refreshButton.removeAttribute("aria-busy");
  repairButton.removeAttribute("aria-busy");
  if (busy) {
    const initiatingControl = jobKind === "repair" ? repairButton : refreshButton;
    initiatingControl.setAttribute("aria-busy", "true");
  }
}

function updateMutatingLabels(jobKind, terminalState, failed) {
  if (jobKind === "sync") {
    refreshButton.textContent = failed
      ? "Retry sync"
      : terminalState === "partial" ? "Sync again"
        : terminalState === "failed" ? "Retry" : "Sync now";
  }
  repairButton.textContent = "Repair history";
}

function hideRepairConfirmation(nextFocus = repairButton) {
  const focusWasInside = repairConfirmation.contains(document.activeElement);
  repairConfirmation.hidden = true;
  repairButton.setAttribute("aria-expanded", "false");
  if (nextFocus && (focusWasInside || nextFocus === asyncStatus)) {
    if (nextFocus === asyncStatus) asyncStatus.focus({preventScroll: true});
    else nextFocus.focus({preventScroll: true});
  }
}

function showActiveJobLoading(jobKind) {
  const jobTitle = jobKind === "repair" ? "Repairing history" : "Syncing evidence";
  const jobDetail = jobKind === "repair"
    ? "Repair history is walking all trusted telemetry files; existing evidence remains visible."
    : "Current evidence stays visible while new queued telemetry is reconciled.";
  showAsyncState("loading", jobTitle, jobDetail);
}

function runActiveJob(requestedKind, start) {
  if (activeJobPoll) return activeJobPoll;
  let jobKind = requestedKind;
  activeJobKind = jobKind;
  lastRefreshProgress = null;
  setMutatingBusy(jobKind, true);
  showActiveJobLoading(jobKind);

  let terminalState = null;
  let failed = false;
  let retry = jobKind === "repair" ? startRepair : startRefresh;
  activeJobPoll = (async () => {
    const started = await start();
    jobKind = syncJobKind(started.kind);
    activeJobKind = jobKind;
    retry = jobKind === "repair" ? startRepair : startRefresh;
    setMutatingBusy(jobKind, true);
    showActiveJobLoading(jobKind);
    announce(started.reused
      ? `Existing ${jobKind} reused`
      : jobKind === "repair" ? "Repair started" : "Sync started");
    const terminal = await pollRefresh(started.sync_ref, jobKind, retry);
    terminalState = terminal && terminal.state;
    return terminal;
  })().catch(error => {
    failed = true;
    const code = error instanceof ApiError ? error.code : "internal_failure";
    const title = jobKind === "repair" ? "Repair history failed" : "Sync failed";
    showAsyncState("error", title, errorMessage(code), retry);
    routeStatus.textContent = "";
    announceRefreshOutcome(title);
    return null;
  }).finally(() => {
    setMutatingBusy(jobKind, false);
    updateMutatingLabels(jobKind, terminalState, failed);
    activeJobPoll = null;
    activeJobKind = null;
    if (jobKind === "repair" && document.activeElement === asyncStatus) {
      repairButton.focus({preventScroll: true});
    }
  });
  return activeJobPoll;
}

async function startRefresh() {
  if (!api) return null;
  return runActiveJob("sync", () => api.startSync());
}

async function startRepair() {
  if (!api) return null;
  return runActiveJob("repair", () => api.startRepair());
}

refreshButton.addEventListener("click", startRefresh);
repairButton.addEventListener("click", () => {
  if (repairConfirmation.hidden) {
    repairConfirmation.hidden = false;
    repairButton.setAttribute("aria-expanded", "true");
    repairConfirmButton.focus({preventScroll: true});
  } else {
    hideRepairConfirmation(repairButton);
  }
});
repairConfirmButton.addEventListener("click", () => {
  showAsyncState("loading", "Repairing history", "Repair history is walking all trusted telemetry files; existing evidence remains visible.");
  hideRepairConfirmation(asyncStatus);
  runActiveJob("repair", () => api.startRepair());
});

async function pollChanges() {
  if (!api) return;
  try {
    const changes = await api.changes(dataRevision);
    const pendingRevision = Number.isInteger(changes.data_revision)
      && changes.data_revision > dataRevision ? changes.data_revision : null;
    if (!changes.changed || pendingRevision === null || activeJobPoll) return;
    const reloaded = await reloadAfterRefresh(false);
    if (!reloaded) return;
    const loadedRevision = state.snapshot && state.snapshot.data_revision;
    if (!Number.isInteger(loadedRevision) || loadedRevision < pendingRevision) return;
    acknowledgeRevision(loadedRevision);
  } catch (_) { /* A later one-second poll retries without exposing transport details. */ }
}

function startChangePolling() {
  if (changePollPromise) return changePollPromise;
  changePolling = true;
  changePollPromise = runSerializedPoll(
    delay => new Promise(resolve => window.setTimeout(resolve, delay)),
    pollChanges,
    () => changePolling,
  ).finally(() => {
    changePollPromise = null;
  });
  return changePollPromise;
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
    if (current && (current.state === "queued" || current.state === "running")) {
      const jobKind = syncJobKind(current.kind);
      runActiveJob(jobKind, () => Promise.resolve(current));
    }
  }).catch(() => undefined);
}, {once: true});
