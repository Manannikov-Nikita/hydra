export class ApiError extends Error {
  constructor(code, status) {
    super(code);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export class DashboardApi {
  constructor(credential, clearCredential) {
    this.credential = credential;
    this.clearCredential = clearCredential;
  }

  async request(target, {method = "GET", signal} = {}) {
    const credential = this.credential;
    if (!credential) throw new ApiError("reopen_dashboard", 401);
    const response = await fetch(target, {
      method,
      signal,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      headers: {
        "Accept": "application/json",
        "Authorization": `Bearer ${credential}`,
      },
    });
    if (response.status === 401) {
      this.credential = null;
      this.clearCredential();
      throw new ApiError("reopen_dashboard", 401);
    }
    let payload = null;
    try { payload = await response.json(); } catch (_) { throw new ApiError("invalid_response", 500); }
    if (!response.ok) {
      const allowed = payload && payload.error && typeof payload.error.code === "string"
        ? payload.error.code : "request_failed";
      throw new ApiError(allowed, response.status);
    }
    return payload;
  }

  snapshot(projectRef = null, taskRef = null, signal) {
    const query = new URLSearchParams();
    if (projectRef) query.set("project", projectRef);
    if (taskRef) query.set("task", taskRef);
    const suffix = query.size ? `?${query.toString()}` : "";
    return this.request(`/api/v1/snapshot${suffix}`, {signal});
  }

  tasks(projectRef, cursor = null, limit = 50, signal) {
    const query = new URLSearchParams({project: projectRef, limit: String(limit)});
    if (cursor) query.set("cursor", cursor);
    return this.request(`/api/v1/tasks?${query.toString()}`, {signal});
  }

  compare(projectRef, left, right, signal) {
    const query = new URLSearchParams({project: projectRef, left, right});
    return this.request(`/api/v1/compare?${query.toString()}`, {signal});
  }

  evidence(projectRef, evidenceRef, signal) {
    const project = encodeURIComponent(projectRef);
    return this.request(`/api/v1/evidence/${encodeURIComponent(evidenceRef)}?project=${project}`, {signal});
  }

  sync(signal) { return this.request("/api/v1/sync", {signal}); }
  startSync(signal) { return this.request("/api/v1/sync", {method: "POST", signal}); }
  startRepair(signal) { return this.request("/api/v1/repair", {method: "POST", signal}); }
  syncStatus(syncRef, signal) { return this.request(`/api/v1/sync/${encodeURIComponent(syncRef)}`, {signal}); }
  changes(after, signal) { return this.request(`/api/v1/changes?after=${encodeURIComponent(String(after))}`, {signal}); }
}
