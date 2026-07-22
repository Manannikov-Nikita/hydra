# Hydra Multi-project Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `hydra-codex dashboard`, a secured loopback-only Evidence Desk for browsing privacy-safe telemetry across local Hydra projects, explicitly refreshing trusted sources, comparing tasks, checking health, and resolving one evidence record at a time.

**Architecture:** A dependency-free Python 3.12 backend exposes one immutable `hydra.dashboard/v1` public model through a pure request/response application and a thin `ThreadingHTTPServer` adapter bound to `127.0.0.1`. A trusted global refresh planner scans active/archive rollout roots once, attributes each source through its own `SourceScan.cwd`, reconciles affected projects in a background single-flight controller, and swaps only valid public snapshots into an in-memory cache. Packaged CSP-compatible HTML/CSS/ES modules render the approved Evidence Desk without reading SQLite or private IDs in the browser.

**Tech Stack:** Python 3.12 standard library, SQLite through existing `HydraStore`, `http.server`, frozen dataclasses, `importlib.resources`, self-contained HTML/CSS/JavaScript ES modules, `unittest`, and real browser QA as a release-only verification step.

## Global Constraints

- Bind only to IPv4 `127.0.0.1`; there is no `--host` option and no remote/team mode.
- Add no Python runtime dependency, Node runtime, frontend framework, CDN, external font, external image, or source map.
- Browser-visible payloads contain no raw filesystem paths, project IDs, session/turn IDs, prompts, commands, tool output, source roots, or exception strings.
- Every numeric fact preserves `value`, `unit`, `provenance`, `lower_bound`, and `caveats`; `None` is never rendered as zero.
- The only browser-triggered write is an explicit, empty-body Refresh; pilot and storage maintenance remain CLI-only.
- Refresh uses trusted global active/archive roots and deterministic source attribution; it never accepts a path, source root, command, project ID, session ID, or database fragment from HTTP.
- Keep prior valid project snapshots readable throughout Refresh and after partial/failed Refresh.
- Keep canonical `hydra.report/v3`, `hydra.comparison/v2`, `hydra.audit/v1`, JSON, Markdown, static HTML, MCP, hook, and plugin contracts backward compatible.
- Use the Evidence Desk visual contract in `DESIGN.md`: at most three headline cards, one phase bar, divider-based tables, restrained blue interaction accent, stable phase colors, no gradients/glass/decorative grids/ambient shadows.
- Support system theme plus persisted manual light/dark preference, keyboard operation, visible focus, reduced motion, valid headings, captions/scoped headers, and WCAG AA contrast.
- Work from an isolated worktree created from commit `bbcb92a`; do not carry the main checkout's unrelated uncommitted plugin/ingest changes into the branch.

---

## Locked File Structure

```text
src/hydra_codex/
├── dashboard_model.py          # immutable public DTOs and privacy/schema validation
├── dashboard_queries.py        # catalog, public-ref resolution, pagination and public queries
├── dashboard_refresh.py        # trusted attribution, single-flight worker and snapshot cache
├── dashboard_server.py         # pure HTTP application, loopback adapter and launch lifecycle
├── migrations_u21.py           # schema 37 project catalog
└── dashboard_assets/
    ├── index.html               # static landmarks; no inline script/style/data
    ├── tokens.css               # DESIGN.md tokens and two themes
    ├── dashboard.css            # Evidence Desk layout, focus, reduced motion, narrow desktop
    ├── bootstrap.js             # fragment credential scrub and theme bootstrap
    ├── api.js                   # authenticated same-origin API client
    ├── state.js                 # immutable UI state and route transitions
    ├── dom.js                   # text-only DOM/format helpers
    ├── app.js                   # orchestration, navigation and Refresh polling
    └── views/
        ├── shell.js
        ├── overview.js
        ├── tasks.js
        ├── compare.js
        ├── health.js
        └── evidence.js
tests/
├── test_dashboard_model.py
├── test_dashboard_queries.py
├── test_dashboard_refresh.py
├── test_dashboard_server.py
├── test_dashboard_assets.py
└── test_dashboard_distribution.py
```

`report_renderers.py`, `audit_renderers.py`, and `pilot_renderers.py` remain independent portable artifact renderers. The dashboard consumes their public models, never their HTML.

---

### Task 1: Privacy-safe project catalog and public project references

**Files:**
- Create: `src/hydra_codex/migrations_u21.py`
- Modify: `src/hydra_codex/storage.py`
- Modify: `src/hydra_codex/project.py`
- Modify: `src/hydra_codex/public_refs.py`
- Create: `src/hydra_codex/dashboard_queries.py`
- Modify: `tests/test_project.py`
- Create: `tests/test_dashboard_queries.py`
- Modify: `tests/test_migrations_b2.py`

**Interfaces:**
- Consumes: `HydraStore`, `ProjectResolution`, `Pseudonymizer.key`, stored `project_id` observations.
- Produces: `ProjectResolution.display_name: str | None`; `project_catalog_references(ids, key) -> PublicReferenceProjection`; `sync_project_catalog(store, observed_at) -> tuple[CatalogProject, ...]`; `observe_resolved_project(store, resolution, observed_at) -> None`.

- [ ] **Step 1: Write migration and config RED tests**

```python
def test_project_config_accepts_sanitized_display_name(self) -> None:
    root = self.project("project-a", display_name="  Hydra   Core  ")
    self.assertEqual(resolve_project(root).display_name, "Hydra Core")

def test_project_config_rejects_control_bidi_and_overlong_names(self) -> None:
    for value in ("Hydra\nCore", "Hydra\u202eCore", "x" * 81):
        with self.subTest(value=repr(value)), self.assertRaises(ValueError):
            resolve_project(self.project("project-a", display_name=value))

def test_schema_37_catalog_has_no_path_columns(self) -> None:
    store = HydraStore(self.database)
    columns = {row[1] for row in store.connection.execute("PRAGMA table_info(dashboard_projects)")}
    self.assertEqual(columns, {"project_id", "display_name", "first_seen_at", "last_seen_at"})
```

- [ ] **Step 2: Run RED tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_project tests.test_dashboard_queries tests.test_migrations_b2`

Expected: failures for missing schema 37, `display_name`, catalog functions, and project references.

- [ ] **Step 3: Add schema 37 and strict trusted display-name parsing**

```python
# migrations_u21.py
U21_MIGRATIONS = ((37, ("""CREATE TABLE dashboard_projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CHECK(display_name IS NULL OR (length(display_name) BETWEEN 1 AND 80))
) WITHOUT ROWID""",)),)
U21_REQUIRED_SCHEMA = {
    "dashboard_projects": {"project_id", "display_name", "first_seen_at", "last_seen_at"},
}
```

Append `U21_MIGRATIONS` after `T20_MIGRATIONS`, include `U21_REQUIRED_SCHEMA` in `_validate_schema()`, and preserve the migration rollback/tamper patterns used by schema 36.

In `project.py`, normalize `display_name` by Unicode NFC, trim/collapse horizontal whitespace, reject Unicode control/format/bidi characters, and enforce 1–80 characters. Keep `project_id` unchanged and add `display_name: str | None = None` to the frozen `ProjectResolution`.

- [ ] **Step 4: Add a separate project HMAC domain without changing task refs**

```python
def project_catalog_references(
    project_ids: Iterable[str], installation_key: bytes,
) -> PublicReferenceProjection:
    return _project_references(
        project_ids,
        installation_key,
        domain=b"hydra/public-project-ref/v1/",
        prefix="project_",
        minimum_length=12,
    )
```

Refactor the existing task function through `_project_references(...)` while pinning its existing domain `hydra/public-task-ref/v1/`, prefix `task_`, collision expansion, and byte output in regression tests.

- [ ] **Step 5: Implement catalog synchronization without paths**

```python
@dataclass(frozen=True)
class CatalogProject:
    project_id: str = field(repr=False)
    display_name: str | None
    first_seen_at: str
    last_seen_at: str

def sync_project_catalog(
    store: HydraStore, observed_at: str,
) -> tuple[CatalogProject, ...]:
    rows = store.connection.execute("""WITH observations(project_id, seen_at) AS (
        SELECT project_id, started_at FROM sessions
        UNION ALL SELECT project_id, COALESCE(last_activity_at, started_at)
          FROM rollout_sessions
        UNION ALL SELECT project_id, started_at FROM reconciliation_runs
        UNION ALL SELECT project_id, started_at FROM pilot_runs
        UNION ALL SELECT project_id, observed_at FROM storage_audit_snapshots
    )
    SELECT project_id, MIN(COALESCE(seen_at, ?)), MAX(COALESCE(seen_at, ?))
      FROM observations WHERE project_id <> '' GROUP BY project_id
      ORDER BY project_id""", (observed_at, observed_at)).fetchall()
    with store.rollout_transaction() as connection:
        for project_id, first_seen, last_seen in rows:
            connection.execute("""INSERT INTO dashboard_projects(
                project_id,display_name,first_seen_at,last_seen_at)
                VALUES (?,NULL,?,?)
                ON CONFLICT(project_id) DO UPDATE SET
                  first_seen_at=MIN(first_seen_at,excluded.first_seen_at),
                  last_seen_at=MAX(last_seen_at,excluded.last_seen_at)""",
                (project_id, first_seen, last_seen))
    return _catalog_rows(store.connection)

def observe_resolved_project(
    store: HydraStore, resolution: ProjectResolution, observed_at: str,
) -> None:
    with store.rollout_transaction() as connection:
        connection.execute("""INSERT INTO dashboard_projects(
            project_id,display_name,first_seen_at,last_seen_at) VALUES (?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
              display_name=COALESCE(excluded.display_name,display_name),
              last_seen_at=MAX(last_seen_at,excluded.last_seen_at)""",
            (resolution.project_id, resolution.display_name, observed_at, observed_at))
```

`sync_project_catalog` must derive distinct IDs and bounded first/last timestamps from stored project-bearing observations (`sessions`, `rollout_sessions`, `reconciliation_runs`, `pilot_runs`, `storage_audit_snapshots`) and upsert only identity/timestamps. It must not persist `project_root`, `worktree_path`, `cwd`, or source paths. `observe_resolved_project` may add the trusted sanitized display name.

- [ ] **Step 6: Run GREEN and migration regression tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_project tests.test_dashboard_queries tests.test_migrations_b2 tests.test_storage`

Expected: all tests pass; existing task refs remain byte-identical; schema version is 37.

- [ ] **Step 7: Commit the catalog slice**

```bash
git add src/hydra_codex/migrations_u21.py src/hydra_codex/storage.py \
  src/hydra_codex/project.py src/hydra_codex/public_refs.py \
  src/hydra_codex/dashboard_queries.py tests/test_project.py \
  tests/test_dashboard_queries.py tests/test_migrations_b2.py
git commit -m "feat(dashboard): add privacy-safe project catalog"
```

---

### Task 2: Immutable dashboard model and read-only public queries

**Files:**
- Create: `src/hydra_codex/dashboard_model.py`
- Create: `src/hydra_codex/public_payload.py`
- Modify: `src/hydra_codex/dashboard_queries.py`
- Modify: `src/hydra_codex/audit_builder.py`
- Modify: `src/hydra_codex/pilot.py`
- Modify: `src/hydra_codex/audit_service.py`
- Create: `tests/test_dashboard_model.py`
- Modify: `tests/test_dashboard_queries.py`
- Modify: `tests/test_pilot.py`
- Modify: `tests/test_audit_service.py`

**Interfaces:**
- Consumes: `TaskReport.as_dict()`, `compare_reports`, `storage_status`, `DoctorReport`, canonical audit evidence, project catalog.
- Produces: `DashboardSnapshot.as_dict()`, `DashboardTaskPage.as_dict()`, `DashboardQueryService.snapshot/tasks/compare/evidence`, `read_pilot_status()`.

- [ ] **Step 1: Write DTO immutability, fact-preservation, and privacy RED tests**

```python
def test_snapshot_is_immutable_and_canonical(self) -> None:
    snapshot = self.snapshot()
    self.assertEqual(snapshot.as_dict()["schema_version"], "hydra.dashboard/v1")
    with self.assertRaises(FrozenInstanceError):
        snapshot.generated_at = "changed"  # type: ignore[misc]

def test_unavailable_fact_does_not_become_zero(self) -> None:
    payload = self.snapshot_without_tasks().as_dict()
    fact = payload["project"]["overview"]["headline"]["working_tokens"]
    self.assertIsNone(fact["value"])
    self.assertEqual(fact["provenance"], "estimated")
    self.assertIn("no_reconciled_tasks", fact["caveats"])

def test_public_payload_rejects_private_vocabulary_recursively(self) -> None:
    for key in ("project_id", "session_id", "turn_id", "path", "prompt", "command"):
        with self.subTest(key=key), self.assertRaises(ValueError):
            reject_private_fields({"safe": [{key: "secret"}]})
```

- [ ] **Step 2: Run DTO RED tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_model tests.test_dashboard_queries`

Expected: import failures for `dashboard_model` and missing query contracts.

- [ ] **Step 3: Centralize public-field rejection and define frozen DTOs**

Create `public_payload.py` with the existing audit private-field vocabulary plus `source_root`, `tool_output`, and `worktree_path`. Move `audit_builder._reject_private_fields` to the public helper without changing audit behavior.

Define these validated frozen dataclasses in `dashboard_model.py`:

```python
DASHBOARD_SCHEMA = "hydra.dashboard/v1"

@dataclass(frozen=True)
class DashboardRefreshView:
    refresh_ref: str | None
    state: str
    stage: str | None
    started_at: str | None
    finished_at: str | None
    progress: Mapping[str, NumericFact]
    diagnostic_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "refresh_ref": self.refresh_ref,
            "state": self.state,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": {key: value.as_dict() for key, value in self.progress.items()},
            "diagnostic_codes": list(self.diagnostic_codes),
        }

@dataclass(frozen=True)
class DashboardProjectSummary:
    project_ref: str
    display_name: str
    last_activity_at: str | None
    freshness_state: str
    task_count: NumericFact

    def as_dict(self) -> dict[str, object]:
        return {
            "project_ref": self.project_ref,
            "display_name": self.display_name,
            "last_activity_at": self.last_activity_at,
            "freshness_state": self.freshness_state,
            "task_count": self.task_count.as_dict(),
        }

@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: str
    freshness: Mapping[str, object]
    projects: tuple[DashboardProjectSummary, ...]
    selected_project_ref: str | None
    project_json: str | None
    selected_task_json: str | None
    refresh: DashboardRefreshView

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": DASHBOARD_SCHEMA,
            "generated_at": self.generated_at,
            "freshness": dict(self.freshness),
            "projects": [item.as_dict() for item in self.projects],
            "selected_project_ref": self.selected_project_ref,
            "project": None if self.project_json is None else json.loads(self.project_json),
            "selected_task": (
                None if self.selected_task_json is None
                else json.loads(self.selected_task_json)
            ),
            "refresh": self.refresh.as_dict(),
        }
        reject_private_fields(payload)
        return payload

@dataclass(frozen=True)
class DashboardTaskPage:
    generated_at: str
    project_ref: str
    items_json: tuple[str, ...]
    limit: int
    next_cursor: str | None

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": "hydra.dashboard-task-list/v1",
            "generated_at": self.generated_at,
            "project_ref": self.project_ref,
            "items": [json.loads(item) for item in self.items_json],
            "page": {
                "limit": self.limit,
                "next_cursor": self.next_cursor,
                "has_more": self.next_cursor is not None,
            },
        }
        reject_private_fields(payload)
        return payload
```

Store nested public structures as canonical JSON or immutable mappings, validate the exact schema on construction, sort mappings/tuples deterministically, and call `reject_private_fields()` on every serialized response.

- [ ] **Step 4: Add a truly read-only pilot status path**

```python
def read_pilot_status(
    store: HydraStore, project_id: str, pilot_id: str,
) -> PilotStatus:
    """Build status from already-enrolled stored tasks without writes."""
    return _build_pilot_status(store, project_id, pilot_id, enroll=False)

def pilot_status(store: HydraStore, project_id: str, pilot_id: str) -> PilotStatus:
    return _build_pilot_status(store, project_id, pilot_id, enroll=True)
```

Preserve existing `pilot_status()` behavior. Allow `build_pilot_audit(..., refresh_enrollment=False)` to use `read_pilot_status()` for GET/evidence paths; retain the current default for CLI audit generation.

- [ ] **Step 5: Implement public query service with project-scoped selectors**

Implement `DashboardQueryService(store_factory, installation_key, clock,
doctor_report)` with exact methods `snapshot(*, project_ref, task_ref, refresh)
-> DashboardSnapshot`, `tasks(project_ref, *, cursor, limit=50) ->
DashboardTaskPage`, `compare(project_ref, left, right) -> ComparisonReport`, and
`evidence(project_ref, evidence_id) -> AuditEvidence`.

Every method opens/closes its own `HydraStore`; SQLite connections are never shared between HTTP and Refresh threads. Resolve `project_ref` by rebuilding the full catalog projection internally. Never accept `project_id`.

Use the latest reconciled task as the explicit overview basis:

```json
{"basis":{"kind":"latest_task","task_ref":"task_0123456789ab"},"headline":{"working_tokens":{"value":1,"unit":"tokens","provenance":"derived","lower_bound":null,"caveats":[]}}}
```

Do not sum task metrics. With no report, return estimated unavailable facts with `no_reconciled_tasks`. Task order is `last_activity_at DESC, task_ref ASC`; the opaque cursor is the last public task ref and is valid only within the selected project/order. Default limit is 50, maximum 100.

Require `project=<project_ref>` for both Compare and Evidence because task refs and canonical evidence IDs are project-scoped. Compare returns canonical `hydra.comparison/v2` directly. Evidence builds/reads only the selected project's latest pilot audit and returns exactly one `AuditEvidence`, never its appendix.

- [ ] **Step 6: Prove GET queries do not write**

Snapshot `connection.total_changes` and table digests around snapshot/tasks/compare/evidence tests. Assert zero changes, stable project scoping, deterministic ordering, collision-safe refs, bounded pagination, adversarial display-name/note escaping, and categorical `KeyError("unknown public reference")` without selector echo.

- [ ] **Step 7: Run GREEN query and compatibility suites**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_model tests.test_dashboard_queries tests.test_pilot tests.test_audit_service tests.test_audit_model`

Expected: all tests pass; public GET query fixtures do not change SQLite.

- [ ] **Step 8: Commit the model/query slice**

```bash
git add src/hydra_codex/dashboard_model.py src/hydra_codex/public_payload.py \
  src/hydra_codex/dashboard_queries.py src/hydra_codex/audit_builder.py \
  src/hydra_codex/pilot.py src/hydra_codex/audit_service.py \
  tests/test_dashboard_model.py tests/test_dashboard_queries.py \
  tests/test_pilot.py tests/test_audit_service.py
git commit -m "feat(dashboard): expose immutable public snapshots"
```

---

### Task 3: Trusted global Refresh and single-flight snapshot retention

**Files:**
- Create: `src/hydra_codex/dashboard_refresh.py`
- Modify: `src/hydra_codex/rollout.py`
- Modify: `src/hydra_codex/rollout_identity.py`
- Modify: `src/hydra_codex/rollout_sources.py`
- Create: `tests/test_dashboard_refresh.py`
- Modify: `tests/test_rollout_ingest.py`

**Interfaces:**
- Consumes: trusted active/archive `RolloutRoot`s, `SourceScan.cwd`, `resolve_project`, `ingest_rollouts`, `reconcile_project`, `DashboardQueryService`.
- Produces: `trusted_rollout_roots`, `plan_global_rollout_ingest`, `RefreshController.start/get/close`, `DashboardSnapshotCache`.

- [ ] **Step 1: Write global attribution and single-flight RED tests**

```python
def test_two_projects_are_discovered_once_partitioned_and_reconciled_once(self) -> None:
    result = self.runner.run(self.progress)
    self.assertEqual(self.discovery.calls, 1)
    self.assertEqual(self.ingest.groups, (("project-a", "worktree-a"), ("project-b", "worktree-b")))
    self.assertEqual(self.reconcile.projects, ("project-a", "project-b"))

def test_second_start_reuses_active_job(self) -> None:
    first, first_reused = self.controller.start()
    second, second_reused = self.controller.start()
    self.assertFalse(first_reused)
    self.assertTrue(second_reused)
    self.assertEqual(first.refresh_ref, second.refresh_ref)
    self.assertEqual(self.runner.calls, 1)

def test_partial_refresh_keeps_failed_project_snapshot(self) -> None:
    before = self.cache.snapshot("project_failed")
    self.runner.fail_project("project_failed")
    self.controller.run_to_completion()
    self.assertIs(self.cache.snapshot("project_failed"), before)
```

- [ ] **Step 2: Run Refresh RED tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_refresh`

Expected: import failure for the missing Refresh module.

- [ ] **Step 3: Add one-pass trusted source attribution**

```python
@dataclass(frozen=True)
class AttributedRollout:
    path: Path = field(repr=False)
    label: str
    project_id: str = field(repr=False)
    project_root: Path = field(repr=False)
    scan: SourceScan = field(repr=False)

def trusted_rollout_roots(environ: Mapping[str, str]) -> tuple[RolloutRoot, ...]:
    home = Path(environ.get("HOME") or Path.home()).expanduser()
    candidates = (
        RolloutRoot(home / ".codex" / "sessions", "active"),
        RolloutRoot(home / ".codex" / "archived_sessions", "archived"),
    )
    return tuple(item for item in candidates if item.path.is_dir())
```

Implement `plan_global_rollout_ingest(store, roots, installation_key,
progress) -> GlobalRolloutPlan`; the returned frozen plan contains sorted
`AttributedRollout` partitions plus categorical diagnostics and exposes no raw
paths from `as_dict()` or `repr()`.

Discover active/archive files once. Reject symlinks and candidates resolving outside canonical trusted roots. Reuse existing keyed location state for unchanged sources. For new/changed sources, call `scan_source()` once, resolve only its trusted `scan.cwd`, and group by `(project_id, project_root)` so shared worktrees retain one identity but normalize relative paths against their own root.

Extend `ingest_rollouts(store, roots, project_root, project_id,
model_causes=None, hash_key=None, progress=None, prepared_scans=None)` so the
global planner supplies an already computed `SourceScan`; do not read the same
large JSONL twice. Preserve the current CLI signature/default behavior and
progress stages. Configured App Server/OTel files without an unambiguous stored
thread-to-project binding are not guessed: skip them with
`event_attribution_unavailable` or `event_attribution_ambiguous`.

- [ ] **Step 4: Implement per-project atomic Refresh and immutable cache**

```python
RefreshState = Literal["queued", "running", "succeeded", "partial", "failed"]
RefreshStage = Literal["discover", "inspect", "scan", "reconcile"]

@dataclass(frozen=True)
class RefreshSnapshot:
    refresh_ref: str
    state: RefreshState
    stage: RefreshStage | None
    current: int
    total: int
    started_at: str
    finished_at: str | None
    diagnostic_codes: tuple[str, ...]
    affected_projects: int
    refreshed_projects: int

class DashboardSnapshotCache:
    def __init__(self, snapshots: Mapping[str, DashboardSnapshot]) -> None:
        self._lock = threading.Lock()
        self._snapshots = MappingProxyType(dict(sorted(snapshots.items())))

    def get(self, project_ref: str) -> DashboardSnapshot | None:
        with self._lock:
            return self._snapshots.get(project_ref)

    def replace_many(self, snapshots: Mapping[str, DashboardSnapshot]) -> None:
        with self._lock:
            merged = {**self._snapshots, **snapshots}
            self._snapshots = MappingProxyType(dict(sorted(merged.items())))
```

Implement `RefreshController.start() -> tuple[RefreshSnapshot, bool]` to reuse
an active job or atomically create/start one worker, `get(refresh_ref) ->
RefreshSnapshot` to return only the active/latest job, and `close(timeout=5.0)
-> None` to join only its owned worker.

One lock protects active state and cache publication. The worker opens/closes its own store, processes sources serially, reconciles each affected project once in sorted order, builds all successful replacement DTOs locally, then publishes them with one lock acquisition. The previous cache stays available while running; a failed project keeps its exact previous DTO. A terminal job permits one new job; there is no cancel or periodic ingest.

Normalize exceptions to an allowlist (`storage_unavailable`, `source_changed`, `project_root_unavailable`, `reconciliation_stale`, `database_busy`, `event_attribution_unavailable`, `internal_failure`) without `str(error)`. Counters are monotonic and contain no filename/path.

- [ ] **Step 5: Cover source cache, symlink, failure, and thread-affinity edges**

Add tests for unchanged-source no-rescan, two worktrees/one project, missing root, changed source, escaping symlink, partial success, complete failure, retry after terminal, ordered stages, worker-owned store, controller shutdown, no subprocess use, and absence of private values in `repr()`/`as_dict()`.

- [ ] **Step 6: Run GREEN Refresh and ingest suites**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_refresh tests.test_rollout_ingest tests.test_rollout_review_fixes`

Expected: all tests pass and every source is fully scanned at most once per Refresh.

- [ ] **Step 7: Commit the Refresh slice**

```bash
git add src/hydra_codex/dashboard_refresh.py src/hydra_codex/rollout.py \
  src/hydra_codex/rollout_identity.py src/hydra_codex/rollout_sources.py \
  tests/test_dashboard_refresh.py tests/test_rollout_ingest.py
git commit -m "feat(dashboard): add trusted single-flight refresh"
```

---

### Task 4: Secured loopback application, HTTP adapter, and CLI command

**Files:**
- Create: `src/hydra_codex/dashboard_server.py`
- Modify: `src/hydra_codex/cli.py`
- Create: `tests/test_dashboard_server.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: query service, Refresh controller/cache, packaged assets, configured database/key paths.
- Produces: pure `DashboardApplication.handle()`, `create_dashboard_server()`, `run_dashboard()`, CLI `hydra-codex dashboard`.

- [ ] **Step 1: Write pure HTTP/security and CLI RED tests**

```python
def test_api_requires_exact_host_origin_and_bearer(self) -> None:
    ok = self.app.handle(self.get("/api/v1/snapshot", auth=self.token, host=self.authority))
    self.assertEqual(ok.status, 200)
    for request in (
        self.get("/api/v1/snapshot", auth=None),
        self.get("/api/v1/snapshot", auth=self.token, host="localhost:9999"),
        self.get("/api/v1/snapshot", auth=self.token, origin="https://evil.invalid"),
    ):
        with self.subTest(request=request):
            self.assertIn(self.app.handle(request).status, {400, 401, 403})

def test_refresh_requires_exact_empty_post(self) -> None:
    response = self.app.handle(self.post("/api/v1/refresh", body=b"{}"))
    self.assertEqual(response.status, 400)
    self.assertEqual(self.controller.starts, 0)

def test_dashboard_cli_has_no_host_option(self) -> None:
    parser = build_parser()
    arguments = parser.parse_args(["dashboard", "--port", "0", "--no-open"])
    self.assertEqual((arguments.port, arguments.no_open), (0, True))
    with self.assertRaises(SystemExit):
        parser.parse_args(["dashboard", "--host", "0.0.0.0"])
```

- [ ] **Step 2: Run server RED tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_server tests.test_cli`

Expected: missing server and parser command failures.

- [ ] **Step 3: Implement pure request/response application**

```python
@dataclass(frozen=True)
class DashboardRequest:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes = b""

@dataclass(frozen=True)
class DashboardResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
```

Implement `DashboardApplication.handle(request: DashboardRequest) ->
DashboardResponse` as the single validation/dispatch/error-sanitization entry
point. Keep route functions private and return a `DashboardResponse` on every
path; never delegate an error to `BaseHTTPRequestHandler`'s HTML responses.

Static `/` and exact `/assets/...` routes are unauthenticated bootstrap resources. Every `/api/v1/*` route requires exactly one `Authorization: Bearer <token>` compared with `secrets.compare_digest`. Require exactly one exact Host. GET/HEAD may omit Origin but any supplied Origin must equal `http://<authority>`; POST requires exactly one exact Origin.

Implement:

```text
GET  /api/v1/snapshot?project=&task=
GET  /api/v1/tasks?project=&cursor=&limit=
GET  /api/v1/compare?project=&left=&right=
GET  /api/v1/evidence/<ev>?project=
POST /api/v1/refresh
GET  /api/v1/refresh/<refresh_ref>
```

Reject duplicate/unexpected query fields, over-2048-byte targets, transfer/content encoding, duplicate/invalid Content-Length, any Refresh body, invalid per-endpoint refs, encoded slash/traversal, and unsupported verbs. `OPTIONS` returns JSON 405. All errors use `hydra.dashboard-error/v1` and allowlisted codes only; do not echo selectors or exceptions.

Every response, including error/HEAD, adds:

```text
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; manifest-src 'none'; worker-src 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cache-Control: no-store
X-Frame-Options: DENY
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
```

Never emit `Access-Control-Allow-*`; suppress `BaseHTTPRequestHandler` request/version logs.

- [ ] **Step 4: Add thin IPv4 loopback server and one-time token handoff**

Implement exact public functions `create_dashboard_server(*, port,
application_factory) -> ThreadingHTTPServer` and `run_dashboard(*, port,
no_open, database_path, environ, installation_key_path, cwd, stdout,
browser_open=webbrowser.open) -> None`.

Construct only `ThreadingHTTPServer(("127.0.0.1", port), handler)` and verify its bound address. Generate `secrets.token_urlsafe(32)`. Default launch passes the credential-bearing fragment URL only to `browser_open` and prints a token-free authority. `--no-open` prints the credential-bearing launch URL exactly once as the explicit initial handoff; subsequent output contains no token. Close the Refresh controller and server on `KeyboardInterrupt`/normal shutdown.

Add the direct CLI branch next to ingest (do not extend `LocalCommandServices`, which owns bounded request/response operations): `dashboard`, common `--db/--cwd`, `_nonnegative_port`, `--port 0`, `--no-open`.

- [ ] **Step 5: Test security matrix and loopback smoke**

Cover wrong/duplicate auth, Host and Origin; null/IPv6/trailing-dot origins; non-empty body; transfer encoding; target/query bounds; no CORS; categorical 401/404/405/500; HEAD headers; exact assets/content types; token-free logs; database-unavailable response; server header without Python version; and one short-lived `http.client` smoke against port 0. Assert no bind other than `127.0.0.1` is expressible.

- [ ] **Step 6: Run GREEN HTTP/CLI tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_server tests.test_cli tests.test_local_services`

Expected: all tests pass; smoke server exits cleanly.

- [ ] **Step 7: Commit the loopback slice**

```bash
git add src/hydra_codex/dashboard_server.py src/hydra_codex/cli.py \
  tests/test_dashboard_server.py tests/test_cli.py
git commit -m "feat(dashboard): serve secured loopback API"
```

---

### Task 5: Packaged Evidence Desk shell, projects, and Overview

**Files:**
- Create: `src/hydra_codex/dashboard_assets/index.html`
- Create: `src/hydra_codex/dashboard_assets/tokens.css`
- Create: `src/hydra_codex/dashboard_assets/dashboard.css`
- Create: `src/hydra_codex/dashboard_assets/bootstrap.js`
- Create: `src/hydra_codex/dashboard_assets/api.js`
- Create: `src/hydra_codex/dashboard_assets/state.js`
- Create: `src/hydra_codex/dashboard_assets/dom.js`
- Create: `src/hydra_codex/dashboard_assets/app.js`
- Create: `src/hydra_codex/dashboard_assets/views/shell.js`
- Create: `src/hydra_codex/dashboard_assets/views/overview.js`
- Modify: `src/hydra_codex/dashboard_server.py`
- Modify: `pyproject.toml`
- Create: `tests/test_dashboard_assets.py`
- Create: `tests/test_dashboard_distribution.py`

**Interfaces:**
- Consumes: `hydra.dashboard/v1`, API error/refresh contracts, `DESIGN.md` tokens.
- Produces: authenticated dashboard boot, theme/project navigation, Overview, packaged asset loader.

- [ ] **Step 1: Write asset, privacy, accessibility, and distribution RED tests**

```python
def test_asset_inventory_is_self_contained_and_csp_compatible(self) -> None:
    assets = load_dashboard_assets()
    self.assertEqual(set(assets), EXPECTED_ASSETS)
    joined = "\n".join(item.decode("utf-8") for item in assets.values())
    self.assertNotRegex(joined, r"https?://|<script[^>]+src=[\"']https?:")
    self.assertNotRegex(joined, r"innerHTML|outerHTML|insertAdjacentHTML|eval\(|new Function")

def test_bootstrap_scrubs_fragment_before_first_api_call(self) -> None:
    source = self.asset("bootstrap.js")
    self.assertLess(source.index("history.replaceState"), source.index("hydra-dashboard-ready"))
    self.assertIn("sessionStorage", source)
    self.assertNotIn("localStorage.setItem(AUTH", source)

def test_index_has_static_landmarks_and_no_inline_code(self) -> None:
    html = self.asset("index.html")
    for marker in ("<header", "<nav", "<main", 'aria-live="polite"'):
        self.assertIn(marker, html)
    self.assertNotRegex(html, r"<script(?![^>]+src=)|<style")
```

- [ ] **Step 2: Run asset RED tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_assets tests.test_dashboard_distribution`

Expected: missing asset loader/files and distribution entries.

- [ ] **Step 3: Build token bootstrap and safe DOM foundation**

`bootstrap.js` must validate one exact base64url fragment credential, put it in a tab-scoped session key, immediately scrub the fragment with `history.replaceState`, and publish only an in-memory ready event. On reload it reuses `sessionStorage`; on 401 it clears it. If storage is unavailable, the current page works in memory and reload shows “reopen dashboard”. The credential never enters `localStorage`, logs, DOM, URL query, or error text.

Theme logic follows system preference until the first manual choice, stores only `light|dark` under `hydra-admin-theme`, updates an accessible label, and tolerates storage failure.

`dom.js` exposes only text-safe helpers:

```javascript
export function el(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "text") node.textContent = String(value);
    else if (name.startsWith("on") || name === "html") throw new Error("unsafe attribute");
    else node.setAttribute(name, String(value));
  }
  for (const child of children) node.append(child);
  return node;
}
```

No dynamic surface uses HTML strings.

- [ ] **Step 4: Build static shell, project switcher, and Overview**

Use a persistent desktop project rail and native narrow-desktop selector. Route with post-bootstrap fragments (`#overview`, `#tasks`, `#compare`, `#health`, `#evidence`); the launch credential fragment is removed before routing. Project switches preserve the current page and clear an invalid task selection.

Overview renders exactly three cards for latest-task working tokens, full context, and wall-clock; one labelled/ARIA-described phase bar with visible values and percentages; recent tasks; pilot readiness; and inline instrumentation health. Render `0`, unavailable, estimated, lower-bound, stale, refreshing, partial, and failed states as distinct text, not color alone.

- [ ] **Step 5: Implement DESIGN.md CSS and package assets**

Create exact light/dark variables from `DESIGN.md`, stable phase colors, visible `:focus-visible`, local table overflow, `prefers-reduced-motion`, narrow-desktop rail replacement, and print-safe light output. Do not use gradients, backdrop filters, ambient shadows, decorative animation, or radii beyond the design tokens.

Add:

```toml
[tool.setuptools.package-data]
hydra_codex = [
  "dashboard_assets/*.html",
  "dashboard_assets/*.css",
  "dashboard_assets/*.js",
  "dashboard_assets/views/*.js",
]
```

Load assets only with `importlib.resources.files("hydra_codex")` and an exact allowlist; never join a request path to the filesystem.

- [ ] **Step 6: Run GREEN asset and package tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_assets tests.test_dashboard_distribution tests.test_plugin_distribution`

Expected: all tests pass; wheel/sdist contain each asset once and byte-identically; static report byte snapshots remain unchanged.

- [ ] **Step 7: Commit the Overview slice**

```bash
git add src/hydra_codex/dashboard_assets src/hydra_codex/dashboard_server.py \
  pyproject.toml tests/test_dashboard_assets.py tests/test_dashboard_distribution.py
git commit -m "feat(dashboard): add Evidence Desk overview"
```

---

### Task 6: Tasks, Compare, System Health, Evidence, and Refresh UX

**Files:**
- Create: `src/hydra_codex/dashboard_assets/views/tasks.js`
- Create: `src/hydra_codex/dashboard_assets/views/compare.js`
- Create: `src/hydra_codex/dashboard_assets/views/health.js`
- Create: `src/hydra_codex/dashboard_assets/views/evidence.js`
- Modify: `src/hydra_codex/dashboard_assets/app.js`
- Modify: `src/hydra_codex/dashboard_assets/state.js`
- Modify: `src/hydra_codex/dashboard_assets/dashboard.css`
- Modify: `tests/test_dashboard_assets.py`
- Modify: `tests/test_dashboard_server.py`

**Interfaces:**
- Consumes: task page, canonical comparison, scoped evidence record, refresh status.
- Produces: all remaining approved admin surfaces and principal error/empty states.

- [ ] **Step 1: Write view-contract RED tests**

```python
def test_all_product_surfaces_are_reachable_and_accessible(self) -> None:
    sources = self.all_javascript()
    for route in ("tasks", "compare", "health", "evidence"):
        self.assertIn(f'route: "{route}"', sources)
    for marker in ("caption", "aria-current", "aria-live", "aria-describedby"):
        self.assertIn(marker, sources)

def test_compare_interpretation_is_guarded_by_verdict(self) -> None:
    source = self.asset("views/compare.js")
    self.assertIn('comparison.verdict === "comparable"', source)
    self.assertIn("not-comparable", source)

def test_evidence_request_is_project_scoped(self) -> None:
    source = self.asset("api.js")
    self.assertIn("project", source)
    self.assertIn("encodeURIComponent(evidenceRef)", source)
```

- [ ] **Step 2: Run view RED tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_assets tests.test_dashboard_server`

Expected: missing view modules and route behavior failures.

- [ ] **Step 3: Implement Tasks master-detail**

Add family/status filters and bounded next-page control. A selected task renders public metadata, exactly three headline facts, one phase bar, deterministic facts, marker timeline, test/retry evidence, pilot health, and trend context. Selection stays in-page, survives valid navigation, restores focus predictably, and never opens a modal.

- [ ] **Step 4: Implement Compare and interpretation guard**

Select two tasks from the same project. Render all canonical comparison dimensions at once with baseline/current/delta/percent, provenance and caveats. Improvement/regression wording is allowed only when `verdict === "comparable"`; partial/unknown/not-comparable states show raw values and reasons without directional claims.

- [ ] **Step 5: Implement System Health and one-record Evidence Desk**

Render doctor, storage, schema, transport and instrumentation results as grouped rows without a synthetic health score. Mark `project_resolution` as global launch-context/unavailable rather than implying selected-project health.

Evidence accepts one `ev_[0-9a-f]{16}` value, automatically scopes it to the selected project, and renders exactly one complete record with fact, value, unit, provenance, lower bound and caveats. It never preloads/downloads the full appendix.

- [ ] **Step 6: Implement Refresh progress and failure states**

Refresh uses an explicit button, announces `discover → inspect → scan → reconcile` in the live region, polls only the returned active ref, disables duplicate mutation while active, and keeps all prior content navigable. A reused active job is announced, not duplicated. Terminal partial/failed states show only categorical diagnostics plus Retry.

Also implement full-page database unavailable, no-project onboarding, stale/needs-refresh, disappeared project/task fallback, unavailable metric, and no-comparable-baseline states.

- [ ] **Step 7: Run GREEN view/API tests**

Run: `env PYTHONPATH=src python3.12 -m unittest tests.test_dashboard_assets tests.test_dashboard_server tests.test_dashboard_queries tests.test_dashboard_refresh`

Expected: all tests pass; no private vocabulary or unsafe DOM sinks appear.

- [ ] **Step 8: Commit the complete UI slice**

```bash
git add src/hydra_codex/dashboard_assets tests/test_dashboard_assets.py \
  tests/test_dashboard_server.py
git commit -m "feat(dashboard): complete telemetry evidence workflows"
```

---

### Task 7: Full regression, installed-package smoke, and real browser QA

**Files:**
- Modify only files implicated by failures found in this task.
- Create no production planning artifact beyond this checked implementation plan.

**Interfaces:**
- Consumes: complete dashboard implementation.
- Produces: verified installed CLI/server, browser receipt, and clean review-ready branch.

- [ ] **Step 1: Run focused dashboard verification**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest \
  tests.test_dashboard_model tests.test_dashboard_queries \
  tests.test_dashboard_refresh tests.test_dashboard_server \
  tests.test_dashboard_assets tests.test_dashboard_distribution
```

Expected: all focused tests pass.

- [ ] **Step 2: Run complete regression and compile gates**

Run:

```bash
env PYTHONPATH=src python3.12 -m unittest discover -s tests -t .
python3.12 -m compileall -q src tests
git diff --check
```

Expected: complete suite passes, compilation is clean, and diff check has no output.

- [ ] **Step 3: Build and test installed artifacts**

Run in an isolated test virtualenv that contains the repository's `test` extra:

```bash
python3.12 -m build
python3.12 -m pip install --force-reinstall dist/hydra_codex-0.1.0-py3-none-any.whl
hydra-codex dashboard --help
```

Expected: wheel/sdist build, installed command exposes `dashboard`, and packaged assets load through `importlib.resources` without repository paths.

- [ ] **Step 4: Run real browser QA on real Hydra data**

Start the installed command with an isolated database copy and `--port 0`. Verify:

1. Initial token fragment disappears before the first API response; reload works in the same tab and a tokenless new tab fails safely.
2. No external requests, console errors, CSP violations, token-bearing logs, or CORS headers.
3. System light/dark and manual theme persistence.
4. Keyboard-only project switch, all five destinations, task master-detail, Compare, Evidence and Refresh.
5. Exactly three headline cards and one labelled phase bar; visible facts match API JSON.
6. `0`, unavailable, estimated and lower-bound facts stay distinct.
7. Comparable and not-comparable examples apply the interpretation guard.
8. Evidence loads one scoped complete record and no appendix.
9. Refresh is single-flight, remains navigable, reports stages, and retains old data on an injected partial failure.
10. Empty database, stale task, disappeared selection and database-unavailable states.
11. Narrow desktop replaces rail with the native selector without page-level horizontal overflow.
12. Reduced motion removes nonessential transitions.

Stop only the dashboard server owned by this QA run and verify its loopback port is free. Preserve the isolated database.

- [ ] **Step 5: Obtain independent exact-HEAD review**

Review the entire range from `bbcb92a` to exact HEAD for spec conformance, privacy leaks, source attribution, thread/SQLite safety, HTTP request smuggling/origin gaps, unsafe DOM rendering, accessibility, and canonical-schema regressions. Resolve every Critical/Important finding with a RED/GREEN test and repeat review until approved.

- [ ] **Step 6: Commit verification-only fixes if any**

```bash
git add src/hydra_codex/dashboard_model.py src/hydra_codex/dashboard_queries.py \
  src/hydra_codex/dashboard_refresh.py src/hydra_codex/dashboard_server.py \
  src/hydra_codex/dashboard_assets tests/test_dashboard_model.py \
  tests/test_dashboard_queries.py tests/test_dashboard_refresh.py \
  tests/test_dashboard_server.py tests/test_dashboard_assets.py \
  tests/test_dashboard_distribution.py
git commit -m "fix(dashboard): close verification findings"
```

Skip this commit when no fix was required. Finish with a clean worktree and the exact commands/results in the handoff.

---

## Final Acceptance Matrix

| Requirement | Proof |
| --- | --- |
| Secured multi-project launch | CLI + loopback smoke + browser token scrub |
| No dependencies or external assets | `pyproject.toml`, asset scan, wheel/sdist inventory, network panel |
| Privacy-safe browser boundary | recursive DTO validator, adversarial query tests, payload scan |
| Projects/Overview/Tasks/Compare/Health/Evidence | focused model/API/assets tests + browser QA |
| Single-flight retained Refresh | concurrency/partial failure tests + live injected failure |
| Fact semantics preserved | DTO fact tests + visible/API browser parity |
| Theme/keyboard/reduced motion/WCAG | CSS contract tests + keyboard/theme browser receipt |
| Canonical contracts unchanged | full unit suite and existing byte/schema snapshots |
