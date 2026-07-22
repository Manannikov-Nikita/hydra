export const ROUTES = Object.freeze(["overview", "tasks", "compare", "health", "evidence"]);

const freeze = value => Object.freeze(value);

export function initialState() {
  return freeze({
    route: "overview",
    projectRef: null,
    taskRef: null,
    snapshot: null,
    tasks: freeze([]),
    cursor: null,
    taskFamily: "all",
    taskStatus: "all",
    comparison: null,
    evidence: null,
    busy: false,
    notice: "",
  });
}

export function reduce(state, action) {
  switch (action.type) {
    case "route":
      return freeze({...state, route: ROUTES.includes(action.route) ? action.route : "overview", notice: ""});
    case "project":
      return freeze({...state, projectRef: action.projectRef, taskRef: null, tasks: freeze([]), cursor: null, taskFamily: "all", taskStatus: "all", comparison: null, evidence: null});
    case "snapshot": {
      const availableProjectRefs = new Set(
        Array.isArray(action.snapshot.projects)
          ? action.snapshot.projects.map(project => project.project_ref) : [],
      );
      const projectRef = availableProjectRefs.has(action.snapshot.selected_project_ref)
        ? action.snapshot.selected_project_ref : null;
      const projectChanged = projectRef !== state.projectRef;
      return freeze({
        ...state,
        snapshot: action.snapshot,
        projectRef,
        taskRef: projectChanged ? null : state.taskRef,
        tasks: projectChanged ? freeze([]) : state.tasks,
        cursor: projectChanged ? null : state.cursor,
        comparison: projectChanged ? null : state.comparison,
        evidence: projectChanged ? null : state.evidence,
        busy: false,
      });
    }
    case "reset_after_refresh":
      return freeze({
        ...state,
        taskRef: null,
        tasks: freeze([]),
        cursor: null,
        taskFamily: "all",
        taskStatus: "all",
        comparison: null,
        evidence: null,
      });
    case "tasks":
      return freeze({...state, tasks: freeze(action.items.slice()), cursor: action.cursor || null, busy: false});
    case "append_tasks":
      return freeze({...state, tasks: freeze([...state.tasks, ...action.items]), cursor: action.cursor || null, busy: false});
    case "task":
      return freeze({...state, taskRef: action.taskRef, comparison: null});
    case "filters":
      return freeze({...state, taskFamily: action.family, taskStatus: action.status});
    case "comparison":
      return freeze({...state, comparison: action.comparison, busy: false});
    case "evidence":
      return freeze({...state, evidence: action.evidence, busy: false});
    case "busy":
      return freeze({...state, busy: Boolean(action.value)});
    case "notice":
      return freeze({...state, notice: String(action.value || "")});
    default:
      return state;
  }
}
