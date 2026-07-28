# Hydra Incremental Telemetry and Understandable Dashboard

**Goal:** Replace global refresh scans with a durable event-first sync pipeline,
make reports read-only, persist sync progress, and display meaningful project and
task names without storing private raw payloads.

**Base:** `origin/main@9bcf06ff661df5ac74629b0078ffccf064af75f8`

**Constraints:**

- Existing public opaque references remain compatible.
- Public APIs never expose absolute source paths or raw prompts/tool payloads.
- Normal sync performs no global rollout directory walk.
- Full discovery is available only through an explicit resumable repair/backfill.
- Only one MCP/dashboard worker owns the durable lease at a time.
- Every production change follows a failing-test-first TDD cycle.

## Task 1: Durable sync state

**Files:** `src/hydra_codex/storage.py`, a focused sync-state module, and focused
storage tests.

1. Add failing migration and transaction tests for source registry, checkpoints,
   queue, worker lease, dirty roots, persisted jobs, repair frontier, and global
   data revision.
2. Add the private SQLite migration with constrained state values and indexes.
3. Add small transactional repository methods for enqueue, lease, checkpoint,
   dirty-root, job, frontier, and revision operations.
4. Verify upgrade from the previous schema and busy/concurrent-worker behavior.

## Task 2: Incremental reader and worker

**Files:** `src/hydra_codex/rollout_sources.py`, a focused sync worker module,
existing ingest/reconcile seams, and focused tests.

1. Add failing tests proving append-only reads begin at the durable byte offset.
2. Cover partial final lines, crash/restart, idempotent replay, truncate,
   prefix rewrite, inode replacement, symlink rejection, and source-local
   `repair_required`.
3. Implement trusted root-relative source locators and prefix anchors.
4. Process queued sources under the worker lease and reconcile only dirty
   project/task roots.
5. Implement resumable batched repair/backfill with a persisted directory
   frontier; normal sync must never call global discovery.

## Task 3: Hooks, CLI, MCP, labels, and report contract

**Files:** hook runtime, CLI parser/commands, annotations, project configuration,
report contracts/services, MCP server, and focused tests.

1. Add a privacy-safe `task_label` validator and annotation plumbing.
2. Enqueue the current trusted session from lifecycle/annotation/tool hooks while
   preserving the rule that raw prompts and tool input/output are discarded.
3. Add `sync` and explicit `repair --all` CLI commands.
4. Make `hydra.report` read materialized state only and remove hidden
   `ingest`/`reconcile` subprocess calls.
5. Add report schema v4 `display_name` and `sync_freshness`, preserving opaque
   refs and prior-version compatibility.

## Task 4: Dashboard APIs and UI

**Files:** dashboard contracts/queries/server/refresh integration, JavaScript
assets/views, CSS if needed, and focused tests.

1. Add dashboard schema v2 `data_revision` and persisted sync summary.
2. Add `GET/POST /api/v1/sync`, `GET /api/v1/changes?after=`, and
   `POST /api/v1/repair`; keep `/refresh` as a compatibility alias.
3. Load materialized SQLite state immediately, resume observation of persisted
   jobs after reload, and poll changes once per second.
4. Rename Refresh to Sync now and present Repair history as a separate explicit
   expensive action.
5. Show project display names and task labels as primary text; retain copyable
   short refs as secondary text and use `family · date · short-ref` for legacy
   tasks.

## Task 5: Integration and acceptance

1. Run focused suites after every task and the complete unit suite at the end.
2. Prove 1,500 unchanged registered sources cause neither a directory walk nor
   content reads during normal sync.
3. Verify persisted job reload, concurrent workers, database busy behavior, API
   privacy, migration compatibility, and report equivalence.
4. Run independent spec and code-quality reviews, address findings, and re-run
   verification before handoff.
