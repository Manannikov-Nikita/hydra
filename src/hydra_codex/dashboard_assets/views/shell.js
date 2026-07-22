import {el} from "../dom.js";

const ROUTE_ITEMS = Object.freeze([
  ["Overview", "overview"],
  ["Tasks", "tasks"],
  ["Compare", "compare"],
  ["System health", "health"],
  ["Evidence", "evidence"],
]);

const routeButtons = new Map();
const projectButtons = new Map();
const projectOptions = new Map();
let projectList = null;
let routeList = null;
let projectSelect = null;
let selectProject = null;
let initialized = false;

function routeButton(label, route, navigate) {
  const button = el("button", {
    class: "nav-button", type: "button", "data-route": route, text: label,
  });
  button.addEventListener("click", () => navigate(route));
  return button;
}

export function initializeShell(actions) {
  if (initialized) return;
  initialized = true;
  selectProject = actions.selectProject;
  projectList = el("div", {class: "rail-list"});
  routeList = el("div", {class: "rail-list"});
  document.getElementById("project-navigation").append(projectList);
  document.getElementById("route-navigation").append(routeList);
  projectSelect = document.getElementById("project-select");
  projectSelect.addEventListener("change", () => selectProject(projectSelect.value));
  for (const [label, route] of ROUTE_ITEMS) {
    const button = routeButton(label, route, actions.navigate);
    routeButtons.set(route, button);
    routeList.append(button);
  }
}

function removeMissingProjects(available) {
  for (const [projectRef, button] of projectButtons) {
    if (available.has(projectRef)) continue;
    button.remove();
    projectButtons.delete(projectRef);
    const option = projectOptions.get(projectRef);
    if (option) option.remove();
    projectOptions.delete(projectRef);
  }
}

export function updateShell(snapshot, state) {
  if (!initialized) throw new Error("shell is not initialized");
  const projects = snapshot && Array.isArray(snapshot.projects) ? snapshot.projects : [];
  const available = new Set(projects.map(project => project.project_ref));
  removeMissingProjects(available);

  for (const project of projects) {
    let button = projectButtons.get(project.project_ref);
    if (!button) {
      button = el("button", {class: "nav-button", type: "button"});
      button.addEventListener("click", () => selectProject(project.project_ref));
      projectButtons.set(project.project_ref, button);
    }
    button.textContent = project.display_name;
    button.setAttribute("aria-pressed", String(project.project_ref === state.projectRef));
    projectList.append(button);

    let option = projectOptions.get(project.project_ref);
    if (!option) {
      option = el("option", {value: project.project_ref});
      projectOptions.set(project.project_ref, option);
    }
    option.textContent = project.display_name;
    projectSelect.append(option);
  }
  projectSelect.value = state.projectRef || "";

  for (const [route, button] of routeButtons) {
    if (route === state.route) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}
