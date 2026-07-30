# Project-Scoped Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent unrelated or unattributed telemetry sources from making the selected Hydra project's report appear to require a global repair.

**Architecture:** Keep `hydra.sync-freshness/v1` unchanged and scope every source, queue, outbox, and dirty-root freshness query by the selected opaque `project_id`. Resolve Dashboard public project references before computing the overlay. Keep global repair on operator surfaces, strengthen its scope warning, and remove the one-click repeat action after a partial repair.

**Tech Stack:** Python 3.12, SQLite, Python `unittest`, browser JavaScript modules, Node.js asset execution, Markdown Codex plugin instructions.

## Global Constraints

- Preserve the exact `hydra.sync-freshness/v1` fields: `schema_version`, `state`, and `data_revision`.
- Add no storage migration and no `repair --project` command.
- Do not attribute `project_id IS NULL` sources to any project.
- Do not change `hydra-codex repair --all` execution semantics.
- Preserve existing user data, telemetry, and the user-owned `outputs/` directory.
- Use test-first red-green-refactor for every production behavior change.

---

### Task 1: Project-scope CLI and MCP report freshness

**Files:**
- Modify: `tests/test_local_services.py:573-608`
- Modify: `src/hydra_codex/services.py:323-389`

**Interfaces:**
- Consumes: `LocalCommandServices._sync_freshness(store: HydraStore, project_id: str) -> dict[str, object]`
- Produces: the unchanged `hydra.sync-freshness/v1` mapping with a state derived only from the supplied project.

- [ ] **Step 1: Replace the global-repair expectation with a failing cross-project isolation test**

Add this test beside the existing freshness tests:

```python
def test_sync_freshness_ignores_foreign_and_unattributed_repair_sources(self) -> None:
    store = HydraStore(self.database)
    try:
        repository = SyncStateRepository(store)
        repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="a/queued.jsonl",
            project_id="hprj_local_services",
            observed_at="2026-07-21T00:00:00Z",
        )
        for locator, project_id in (
            ("z/foreign.jsonl", "hprj_foreign"),
            ("z/unattributed.jsonl", None),
        ):
            repository.register_source(
                root_kind="sessions",
                source_locator=locator,
                project_id=project_id,
            )
            repository.mark_repair_required(
                "sessions", locator, "2026-07-21T00:00:01Z",
            )
        freshness = LocalCommandServices(
            environ=self.environ,
        )._sync_freshness(store, "hprj_local_services")
    finally:
        store.close()

    self.assertEqual(freshness["state"], "queued")
```

- [ ] **Step 2: Run the new test and verify the red state**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest \
  tests.test_local_services.LocalCommandServiceTests.test_sync_freshness_ignores_foreign_and_unattributed_repair_sources \
  -v
```

Expected: FAIL because the current global repair query returns
`repair_required` instead of `queued`.

- [ ] **Step 3: Add failing owned-repair and foreign-activity tests**

Add these separate tests:

```python
def test_sync_freshness_prioritizes_owned_repair_over_owned_queue(self) -> None:
    store = HydraStore(self.database)
    try:
        repository = SyncStateRepository(store)
        repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="a/queued.jsonl",
            project_id="hprj_local_services",
            observed_at="2026-07-21T00:00:00Z",
        )
        repository.register_source(
            root_kind="sessions",
            source_locator="z/repair.jsonl",
            project_id="hprj_local_services",
        )
        repository.mark_repair_required(
            "sessions", "z/repair.jsonl", "2026-07-21T00:00:01Z",
        )
        freshness = LocalCommandServices(
            environ=self.environ,
        )._sync_freshness(store, "hprj_local_services")
    finally:
        store.close()

    self.assertEqual(freshness["state"], "repair_required")

def test_sync_freshness_ignores_foreign_queued_and_running_work(self) -> None:
    store = HydraStore(self.database)
    try:
        repository = SyncStateRepository(store)
        repository.register_and_enqueue(
            root_kind="sessions",
            source_locator="foreign.jsonl",
            project_id="hprj_foreign",
            observed_at="2026-07-21T00:00:00Z",
        )
        repository.record_hook_event_and_enqueue(
            event_key="foreign-event",
            project_id="hprj_foreign",
            session_key="foreign-session",
            turn_key="foreign-turn",
            event_kind="post_tool",
            observed_at="2026-07-21T00:00:00Z",
        )
        repository.mark_dirty(
            "hprj_foreign",
            "hprj_foreign",
            "project",
            "2026-07-21T00:00:00Z",
        )
        service = LocalCommandServices(
            environ=self.environ,
            clock=lambda: datetime(
                2026, 7, 21, 0, 0, 30, tzinfo=timezone.utc,
            ),
        )
        queued = service._sync_freshness(
            store, "hprj_local_services",
        )
        self.assertTrue(repository.acquire_lease(
            "foreign-worker",
            "2026-07-21T00:00:30Z",
            "2026-07-21T00:02:00Z",
        ))
        repository.claim_next(
            "foreign-worker",
            "2026-07-21T00:00:30Z",
            "2026-07-21T00:01:30Z",
        )
        repository.claim_hook_events(
            "foreign-worker",
            "2026-07-21T00:00:30Z",
            "2026-07-21T00:01:30Z",
        )
        repository.claim_dirty_roots(
            "foreign-worker",
            "2026-07-21T00:00:30Z",
            "2026-07-21T00:01:30Z",
        )
        running = service._sync_freshness(
            store, "hprj_local_services",
        )
    finally:
        store.close()

    self.assertEqual(queued["state"], "reconcile_required")
    self.assertEqual(running["state"], "reconcile_required")
```

Use literal project IDs and real `SyncStateRepository` operations. For the
running branch, acquire the real worker lease and claim the foreign queue item;
do not mock the repository.

- [ ] **Step 4: Run the added tests and verify the red state**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest \
  tests.test_local_services.LocalCommandServiceTests.test_sync_freshness_prioritizes_owned_repair_over_owned_queue \
  tests.test_local_services.LocalCommandServiceTests.test_sync_freshness_ignores_foreign_queued_and_running_work \
  -v
```

Expected: the owned-repair characterization passes, while foreign queued or
running work fails because current queries are installation-wide.

- [ ] **Step 5: Implement project-filtered local freshness**

Change `_sync_freshness` so:

```python
repair = connection.execute(
    """SELECT 1 FROM sync_source_registry
         WHERE source_state='repair_required' AND project_id=?
         LIMIT 1""",
    (project_id,),
).fetchone() is not None
```

For ingest queue checks, join the registry and filter the source owner:

```sql
SELECT 1
  FROM sync_ingest_queue AS queue
  JOIN sync_source_registry AS source
    ON source.root_kind=queue.root_kind
   AND source.source_locator=queue.source_locator
 WHERE source.project_id=?
   AND CASE
         WHEN queue.queue_state='queued' THEN 1
         WHEN queue.queue_state='claimed'
              AND queue.claim_expires_at IS NULL THEN 1
         WHEN queue.queue_state='claimed' THEN
              hydra_rfc3339_micros(queue.claim_expires_at)
              <=hydra_rfc3339_micros(?)
         ELSE 0
       END=1
 LIMIT 1
```

Use the same join for the running query with:

```sql
queue.queue_state='claimed'
AND queue.claim_expires_at IS NOT NULL
AND hydra_rfc3339_micros(queue.claim_expires_at)
    >hydra_rfc3339_micros(?)
```

Add `project_id=?` to hook outbox checks. Add project-filtered dirty-root
queries with:

```sql
project_id=?
AND (
  claim_owner IS NULL
  OR claim_expires_at IS NULL
  OR hydra_rfc3339_micros(claim_expires_at)
     <=hydra_rfc3339_micros(?)
)
```

for queued work, and:

```sql
project_id=?
AND claim_owner IS NOT NULL
AND claim_expires_at IS NOT NULL
AND hydra_rfc3339_micros(claim_expires_at)
    >hydra_rfc3339_micros(?)
```

for running work. Remove installation-wide `sync_jobs` checks from project
freshness. Keep the existing state priority and project reconciliation-fence
check.

- [ ] **Step 6: Run the focused local-service module**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest tests.test_local_services -v
```

Expected: PASS.

- [ ] **Step 7: Commit the isolated report fix**

```bash
git add src/hydra_codex/services.py tests/test_local_services.py
git commit -m "fix: scope report freshness by project"
```

### Task 2: Project-scope Dashboard materialized freshness

**Files:**
- Modify: `tests/test_dashboard_queries.py:1547-1590`
- Modify: `src/hydra_codex/dashboard_queries.py:208-256`
- Modify: `src/hydra_codex/dashboard_queries.py:608-925`
- Modify: `src/hydra_codex/dashboard_queries.py:1118-1250`

**Interfaces:**
- Consumes: a resolved catalog `project_id`.
- Produces: `DashboardQueryService._materialized_state(connection, project_id) -> tuple[int, dict[str, object]]`.

- [ ] **Step 1: Add a failing Dashboard task-page isolation test**

Materialize project A, register one repair-required source for project B and
one unattributed repair-required source, then read project A:

```python
def test_task_page_freshness_ignores_foreign_and_unattributed_repair(self) -> None:
    self.materialize("project-a", self.reports["project-a"])
    store = HydraStore(self.database)
    try:
        repository = SyncStateRepository(store)
        for locator, project_id in (
            ("foreign.jsonl", "project-b"),
            ("unattributed.jsonl", None),
        ):
            repository.register_source(
                root_kind="sessions",
                source_locator=locator,
                project_id=project_id,
            )
            repository.mark_repair_required(
                "sessions", locator, "2026-07-22T11:30:00Z",
            )
    finally:
        store.close()

    page = self.service.tasks(
        self.catalog_refs()["project-a"], cursor=None, limit=1,
    ).as_dict()

    self.assertEqual(
        page["items"][0]["sync_freshness"]["state"],
        "current",
    )
```

- [ ] **Step 2: Run the new Dashboard test and verify the red state**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest \
  tests.test_dashboard_queries.DashboardPublicQueryServiceTests.test_task_page_freshness_ignores_foreign_and_unattributed_repair \
  -v
```

Expected: FAIL with `repair_required` instead of `current`.

- [ ] **Step 3: Add an owned-repair Dashboard test**

Register a repair-required source with `project_id="project-a"`, read project
A's task page, and assert the unchanged freshness object has:

```python
{
    "schema_version": "hydra.sync-freshness/v1",
    "state": "repair_required",
    "data_revision": repository.data_revision(),
}
```

- [ ] **Step 4: Project-filter `_materialized_state` and reorder callers**

Change the signature to:

```python
@staticmethod
def _materialized_state(
    connection: sqlite3.Connection,
    project_id: str,
) -> tuple[int, dict[str, object]]:
```

Filter repair registry rows by `project_id`, join ingest queue rows to the
registry, and filter outbox and dirty-root rows by `project_id`. Remove global
`sync_jobs` branches.

In `_materialized_bootstrap_from_connection`, read the revision first, resolve
the selected catalog project, then call:

```python
freshness_revision, sync_freshness = self._materialized_state(
    connection, selected_item.project_id,
)
if freshness_revision != revision:
    raise ValueError("materialized sync revision changed during snapshot")
```

In `tasks`, resolve the public project reference before calling
`_materialized_state(store.connection, item.project_id)`. Preserve the
consistent read transaction and all existing public validation.

- [ ] **Step 5: Run Dashboard query tests**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_queries -v
```

Expected: PASS.

- [ ] **Step 6: Commit the Dashboard query fix**

```bash
git add src/hydra_codex/dashboard_queries.py tests/test_dashboard_queries.py
git commit -m "fix: isolate dashboard freshness by project"
```

### Task 3: Make global repair an explicit, non-repeating operator action

**Files:**
- Modify: `tests/test_dashboard_assets.py:780-860`
- Modify: `src/hydra_codex/dashboard_assets/app.js:39-62`
- Modify: `src/hydra_codex/dashboard_assets/app.js:448-473`
- Modify: `src/hydra_codex/dashboard_assets/index.html:35`
- Modify: `plugins/hydra-codex/skills/hydra-report/SKILL.md:14-29`
- Modify: `plugins/hydra-codex/skills/hydra-report/references/report-schema.md:15-28`
- Modify: `docs/dashboard-operations.md:128-180`

**Interfaces:**
- Produces: `retryAfterTerminal(jobKind: string, terminalState: string, retry: Function) -> Function | null`.
- Preserves: authenticated `POST /api/v1/repair` and CLI `repair --all`.

- [ ] **Step 1: Add a failing executable asset test for partial repair**

Export a wished-for pure helper from `app.js` and test it through the existing
Node module harness:

```python
@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute dashboard assets")
def test_partial_repair_requires_fresh_confirmation_before_retry(self) -> None:
    self.assertEqual(
        self.evaluate_app(
            "['repair', 'sync'].map(kind => "
            "subject.retryAfterTerminal(kind, 'partial', 'retry'))"
        ),
        [None, "retry"],
    )
```

Also update the existing asset contract to expect **Start global repair** and
the global-scope warning.

- [ ] **Step 2: Run the new asset test and verify the red state**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest \
  tests.test_dashboard_assets.DashboardAssetTests.test_partial_repair_requires_fresh_confirmation_before_retry \
  -v
```

Expected: FAIL because `retryAfterTerminal` is not exported.

- [ ] **Step 3: Implement the partial-repair guard**

Add:

```javascript
export function retryAfterTerminal(jobKind, terminalState, retry) {
  return jobKind === "repair" && terminalState === "partial" ? null : retry;
}
```

Use it in `pollRefresh` when passing the retry callback to `showAsyncState`.
A partial sync keeps **Sync again**. A partial repair shows its diagnostic with
no direct action button, so a retry must return through **Repair history** and
the confirmation.

- [ ] **Step 4: Strengthen the repair confirmation copy**

Replace the confirmation paragraph with:

```html
Full repair scans telemetry history across every Hydra project, may inspect
thousands of files, may take a long time, and may still finish partial. It is
not required to view the current report.
```

Label the confirmation button **Start global repair**. Keep accessible focus
transfer and the existing authenticated API request unchanged.

- [ ] **Step 5: Correct Hydra Report and operator documentation**

Change the reporting workflow so `repair_required`:

- identifies current-project historical incompleteness;
- does not invalidate already materialized facts;
- never causes the agent to claim repair is required to read or send a report;
- allows mentioning `repair --all` only after a user asks to recover missing
  history or explicitly requests global repair;
- requires the agent to explain global scope, potentially thousands of files,
  long runtime, and possible `partial` completion before presenting the command.

Update `report-schema.md` and `dashboard-operations.md` with the same contract.
Remove recovery text that automatically follows any `repair_required` state
with `repair --all`.

- [ ] **Step 6: Run focused asset and documentation-contract tests**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest \
  tests.test_dashboard_assets \
  tests.test_readme_contract \
  tests.test_workflow_contract \
  tests.test_plugin_distribution \
  -v
```

Expected: PASS.

- [ ] **Step 7: Verify obsolete mandatory-repair guidance is gone**

Run:

```bash
rg -n "must explicitly run|Full report requires|Полный отчёт потребует|Repair again|Start full repair" \
  plugins/hydra-codex/skills/hydra-report \
  docs/dashboard-operations.md \
  src/hydra_codex/dashboard_assets \
  tests/test_dashboard_assets.py
```

Expected: no matches.

- [ ] **Step 8: Commit the operator-safety changes**

```bash
git add \
  src/hydra_codex/dashboard_assets/app.js \
  src/hydra_codex/dashboard_assets/index.html \
  tests/test_dashboard_assets.py \
  plugins/hydra-codex/skills/hydra-report/SKILL.md \
  plugins/hydra-codex/skills/hydra-report/references/report-schema.md \
  docs/dashboard-operations.md
git commit -m "fix: make global repair explicitly optional"
```

### Task 4: Full verification and acceptance audit

**Files:**
- Verify: `docs/superpowers/specs/2026-07-30-project-scoped-freshness-design.md`
- Verify: every file changed in Tasks 1-3.

**Interfaces:**
- Consumes: the approved acceptance criteria.
- Produces: fresh test and diff evidence for completion.

- [ ] **Step 1: Run the complete repository test suite**

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
```

Expected: PASS with zero failures and zero errors.

- [ ] **Step 2: Run syntax and whitespace checks**

```bash
env PYTHONPATH="$PWD/src" python3.12 -m compileall -q src tests
git diff --check HEAD~3..HEAD
```

Expected: both commands exit 0 with no output.

- [ ] **Step 3: Audit the final diff against the approved spec**

Confirm each acceptance criterion with direct code or test evidence:

- foreign and unattributed repair do not affect project A;
- owned repair still reports `repair_required`;
- freshness remains schema v1 with exactly three keys;
- mandatory agent repair guidance is absent;
- Dashboard warns about global scope;
- partial repair has no one-click retry;
- no migration or deletion exists;
- `outputs/` remains untracked and untouched.

- [ ] **Step 4: Inspect repository state**

```bash
git status --short
git log -5 --oneline
```

Expected: only the pre-existing user-owned `outputs/` is untracked, with all
task changes committed.
