# Dashboard Sync Polling Hotfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent transient dashboard status-read errors from falsely reporting a persisted sync job as failed.

**Architecture:** Keep the API and persisted job model unchanged. Add one bounded asynchronous polling helper in `app.js`; `pollRefresh` supplies rendering and terminal-state behavior, while the active-job catch renders status-unavailable copy for transport exhaustion.

**Tech Stack:** Browser JavaScript modules, Python `unittest`, Node.js module execution, pytest.

## Global Constraints

- Retry status reads once per second.
- Stop after five consecutive failures.
- Reset the failure counter after every successful status read.
- Do not retry `reopen_dashboard`.
- Only terminal job payloads with `state: failed` may render `Sync failed`.
- Preserve existing evidence and the single-flight mutating-control contract.
- Do not change public API schemas or persist private error details.

---

### Task 1: Bounded persisted-job status polling

**Files:**
- Modify: `tests/test_dashboard_assets.py`
- Modify: `src/hydra_codex/dashboard_assets/app.js`

**Interfaces:**
- Consumes: `api.syncStatus(syncRef)`, `ApiError`, `TERMINAL_REFRESH`.
- Produces: `pollJobStatus(readStatus, observeStatus, wait, maxFailures)` and `statusUnavailableTitle(jobKind)`.

- [ ] **Step 1: Write the failing retry test**

Add a Node-executed asset test that feeds the real helper:

```python
observed = self.evaluate_app(
    """(async () => {
      const replies = [new Error("busy"), {state: "running"}, {state: "succeeded"}];
      const waits = [];
      const states = [];
      const result = await subject.pollJobStatus(
        async () => {
          const reply = replies.shift();
          if (reply instanceof Error) throw reply;
          return reply;
        },
        async current => {
          states.push(current.state);
          return current.state === "succeeded";
        },
        async delay => { waits.push(delay); },
        5,
      );
      return {state: result.state, states, waits};
    })()"""
)
self.assertEqual(observed, {
    "state": "succeeded",
    "states": ["running", "succeeded"],
    "waits": [1000, 1000, 1000],
})
```

- [ ] **Step 2: Write the failing bounded/reset/copy tests**

Exercise five consecutive failures, a success that resets the counter, the
immediate `reopen_dashboard` branch, and literal status-unavailable titles for
both job kinds. Assertions must target returned behavior, not source text.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
python3.12 -m pytest tests/test_dashboard_assets.py -q
```

Expected: the new tests fail because `pollJobStatus` and
`statusUnavailableTitle` are not exported.

- [ ] **Step 4: Implement the minimal polling helper**

In `app.js`, implement:

```javascript
export async function pollJobStatus(
  readStatus,
  observeStatus,
  wait = delay => new Promise(resolve => window.setTimeout(resolve, delay)),
  maxFailures = 5,
) {
  let consecutiveFailures = 0;
  for (;;) {
    await wait(1000);
    let current;
    try {
      current = await readStatus();
    } catch (error) {
      if (error instanceof ApiError && error.code === "reopen_dashboard") throw error;
      consecutiveFailures += 1;
      if (consecutiveFailures >= maxFailures) throw error;
      continue;
    }
    consecutiveFailures = 0;
    if (await observeStatus(current)) return current;
  }
}
```

Use it from `pollRefresh`, preserving the existing progress and terminal
rendering callback. Use `statusUnavailableTitle(jobKind)` in the outer catch.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run:

```bash
python3.12 -m pytest tests/test_dashboard_assets.py -q
```

Expected: all dashboard asset tests pass.

- [ ] **Step 6: Run dashboard integration coverage**

Run:

```bash
python3.12 -m pytest \
  tests/test_dashboard_sync_api.py \
  tests/test_dashboard_server.py \
  tests/test_dashboard_distribution.py -q
```

Expected: all selected integration tests pass.

### Task 2: Full verification and browser acceptance

**Files:**
- Verify only: repository test suite and packaged dashboard assets.

**Interfaces:**
- Consumes: completed Task 1 behavior.
- Produces: verified local browser behavior and unchanged dashboard API contract.

- [ ] **Step 1: Run the complete test suite**

Run:

```bash
python3.12 -m pytest -q
```

Expected: zero failures.

- [ ] **Step 2: Launch the source dashboard**

Run the source checkout against the isolated/local Hydra database on an
ephemeral loopback port, open the full fragment-token handoff URL, and keep the
process owned by this task.

- [ ] **Step 3: Verify browser behavior**

Confirm materialized evidence renders, `Sync now` remains single-flight, a
transient polling failure does not produce `Sync failed`, and a later terminal
payload controls the final copy. Leave `Repair history` untouched.

- [ ] **Step 4: Review the final diff**

Run:

```bash
git diff --check
git status --short
```

Expected: only the spec, plan, dashboard asset, and dashboard asset test are
modified; `outputs/` remains untouched.
