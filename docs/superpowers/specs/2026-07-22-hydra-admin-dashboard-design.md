# Hydra Multi-project Admin Dashboard Design

Date: 2026-07-22
Status: approved design, pending implementation plan

## Summary

Hydra will add a local multi-project administration interface for inspecting
privacy-safe Codex telemetry. The interface is a loopback-only application
bundled with the dependency-free Python package. It adopts the compact visual
language of the approved single-task reference while retaining the evidence
closure, provenance, caveats, and comparability guards of Hydra's canonical
reports.

The first version is read-only except for one explicit Refresh action. Refresh
imports the known local event sources and reconciles public task reports in a
background job. Pilot lifecycle and storage maintenance remain CLI-only.

## Goals

- Make all observed Hydra projects discoverable from one local interface.
- Move from project overview to one task, comparison, system diagnostic, or
  evidence record without reading raw JSON or SQLite.
- Preserve the exact deterministic/model boundary and the meaning of
  provenance, lower bounds, caveats, zero, and unavailable values.
- Make stale, refreshing, partial, failed, and current data states explicit.
- Keep browser-visible data privacy-safe and free of raw filesystem paths,
  prompts, commands, internal session identifiers, and tool output.
- Ship without a Node runtime, CDN, or third-party Python runtime dependency.

## Non-goals

- Team hosting, remote access, accounts, or cloud synchronization.
- Starting or closing pilots from the browser.
- Database compaction, retention, deletion, or editing telemetry.
- A general SQL explorer or raw event/log viewer.
- A separate mobile product or mobile-first information architecture.
- Replacing canonical JSON, Markdown, or static HTML reports.

## Product Contract

The default register is product. The primary user is a developer or technical
lead comparing work across several local repositories. The interface is calm,
precise, and trustworthy. It behaves like an Evidence Desk rather than a KPI
wall, terminal dump, or neon observability room.

The only browser-triggered mutation is Refresh. Every other screen is read-only.
Canonical report and audit schemas remain unchanged and continue to be the
portable evidence artifacts.

## Architecture

### Package boundary

The dashboard adds four focused capabilities under `src/hydra_codex/`:

1. `dashboard_model.py` defines immutable public dashboard DTOs and schema
   validation.
2. `dashboard_queries.py` assembles project summaries and a selected-project
   snapshot from existing report, pilot, doctor, storage, and comparison
   services.
3. `dashboard_refresh.py` owns one background ingest/reconcile job and exposes
   immutable progress snapshots. It always uses Hydra's trusted global default
   rollout/event discovery; it does not resolve sources from project paths.
4. `dashboard_server.py` serves bundled assets and the versioned loopback API.

Static HTML, CSS, and JavaScript assets live in the Python package and are
included in wheel/sdist verification. The frontend does not invoke CLI
subprocesses and never opens SQLite.

### CLI

`hydra-codex dashboard` starts the server on `127.0.0.1` using an available port
and opens the default browser. Supported operational flags are limited to:

- `--port PORT` for a deliberate fixed loopback port; `0` remains the default.
- `--no-open` for headless or scripted startup.
- the existing trusted database override used by Hydra tests and isolated runs.

The command prints a privacy-safe startup status. It never prints the launch
token after the initial URL handoff and never binds a non-loopback address.

### Project catalog

Existing Hydra tables carry internal `project_id` values but no safe catalog.
The dashboard introduces a small internal project catalog containing:

- internal `project_id`;
- optional sanitized `display_name` from `.hydra/project.toml`;
- first-seen and last-seen timestamps.

No project root or worktree path is added to this catalog. Existing projects
are discovered from distinct stored project observations and use
`Project <short-public-ref>` until a later trusted project resolution supplies a
display name. Public project references are generated through the installation
pseudonymizer and are never reversible in the browser.

`.hydra/project.toml` accepts an optional non-empty `display_name`. `project_id`
remains the stable identity and shared-worktree contract.

The catalog is for identity and presentation, not source discovery. Refresh
scans only Hydra's trusted global active/archive roots and versioned event-source
configuration, then uses deterministic event attribution to identify affected
projects. Missing project roots and stale catalog entries therefore cannot make
the server guess or accept a browser-supplied path.

## Public Data Contract

The main response is `hydra.dashboard/v1`. It contains:

- generated timestamp and snapshot freshness;
- lightweight project summaries;
- selected public project reference;
- selected-project overview, recent task summaries, current pilot summary,
  storage status, and safe doctor status;
- optional selected task detail using `hydra.report/v3` semantics;
- current Refresh status.

Task collections are bounded and paginated. A project summary never embeds all
task details or evidence records. Existing `hydra.comparison/v2` semantics are
reused for pairwise comparison. Evidence lookup returns exactly one complete
public evidence record by evidence ID; the browser never downloads the full
appendix merely to render an overview.

Every numeric field retains `value`, `unit`, `provenance`, `lower_bound`, and
`caveats`. No renderer is allowed to replace `None` with zero.

## Loopback API

The initial API surface is:

- `GET /api/v1/snapshot?project=<public-ref>&task=<public-ref>`
- `GET /api/v1/tasks?project=<public-ref>&cursor=<opaque>&limit=<bounded>`
- `GET /api/v1/compare?left=<task-ref>&right=<task-ref>`
- `GET /api/v1/evidence/<evidence-id>`
- `POST /api/v1/refresh`
- `GET /api/v1/refresh/<refresh-id>`

All selectors are validated opaque public references. The server never accepts
filesystem paths, SQL fragments, source roots, project IDs, or session IDs.
Unknown references return categorical `not_found` responses without exposing
internal values.

## Refresh Lifecycle

Refresh is explicit and single-flight:

1. The client sends an empty-body Refresh request and receives `202 Accepted`
   with an opaque job ID.
2. The job scans Hydra's trusted global default sources. Stored event identity
   deterministically attributes observations to projects; no catalog path is
   consulted.
3. The existing valid snapshots remain available and are marked refreshing.
4. The job emits privacy-safe stages: `discover`, `inspect`, `scan`, and
   `reconcile`, with deterministic counters already supported by ingest.
5. Affected catalog projects are reconciled in deterministic order. Shared
   worktrees retain one stable project identity.
6. The client polls only the active job status.
7. Success atomically replaces each affected project's public snapshot.
8. Partial or failed refresh retains prior snapshots and records a sanitized
   categorical diagnostic.

A second request while a job is active returns that active job instead of
starting concurrent import. The first version has no cancel control and does not
run periodic background ingest.

## Security and Privacy

- The server binds only `127.0.0.1`; IPv4/IPv6 ambiguity is not accepted
  silently.
- A cryptographically random per-launch token is placed in the initial URL
  fragment. Bootstrap JavaScript validates it, immediately removes the fragment
  with `history.replaceState()`, stores it only in tab-scoped `sessionStorage`,
  and sends it in an Authorization header. It expires with the server process
  and is never written to localStorage.
- Host must always match the exact `127.0.0.1:<port>` authority. State-changing
  requests require an exact same-origin Origin. GET/HEAD requests may omit
  Origin, but any supplied Origin must match exactly.
- CORS is disabled.
- Responses include a restrictive CSP, `frame-ancestors 'none'`,
  `base-uri 'none'`, `form-action 'none'`, `nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, and no external
  assets.
- Refresh accepts no user-provided command, path, project ID, or source root.
- Error responses use allowlisted diagnostic codes and never raw exception text.
- Browser payload privacy is tested against Hydra's existing private-field
  vocabulary and adversarial display names/notes.

## Information Architecture

### Projects

A persistent desktop rail lists safe project names and current freshness. The
selected project remains stable across navigation. At narrow desktop widths the
rail becomes a native project selector.

### Overview

The project header shows identity, latest activity, current/stale state, and the
Refresh action. Three metric summaries show working tokens, full context, and
wall-clock. A single stacked phase bar shows working-token allocation with
visible values, percentages, and semantic coverage. Recent tasks, pilot
readiness, and inline instrumentation health follow without additional card
grids.

### Tasks

Tasks use master-detail. The collection supports family/status filters and
bounded pagination. Selecting a task shows the approved reference composition:

- public task metadata;
- three headline facts;
- one stacked phase allocation;
- deterministic facts;
- model marker timeline;
- test/retry evidence;
- pilot health and trend context.

Selection does not open a modal and remains keyboard-operable.

### Compare

The user selects two tasks. All comparison dimensions render simultaneously
using a shared-scale table or paired horizontal marks. Raw values and deltas are
always visible; improvement/regression language appears only for a `comparable`
verdict. Partial, unknown, and not-comparable reasons stay prominent.

### System health

Transport, schema diagnostics, storage, and doctor results use quiet grouped
rows. Errors and warnings are textual and actionable. The page does not invent a
health score.

### Evidence

An evidence ID search opens one complete record in a non-modal side panel or
deep-linked region. Provenance, unit, lower bound, and caveats remain visible.
The hundreds of records in a canonical audit are never expanded by default.

## Visual System

`DESIGN.md` is normative. The dashboard uses the Evidence Desk composition:

- wide, mostly unframed workspace;
- only three headline cards;
- restrained neutral layers and one blue interaction accent;
- stable phase colors used only for data marks and swatches;
- quiet divider-based tables;
- system theme by default and a persisted manual light/dark choice;
- no gradients, glass, decorative grids, wide ambient shadows, oversized radii,
  or decorative motion.

Motion is limited to short state transitions for selection, disclosure, theme,
and Refresh. `prefers-reduced-motion` disables nonessential transitions.

## Error and Empty States

- **Database unavailable:** a full-page safe diagnostic with Retry and no raw
  path or exception.
- **No projects:** onboarding that explains project opt-in and initial ingest.
- **Needs refresh:** current data remains visible with an explicit stale label.
- **Refresh failed or partial:** the prior snapshot remains; a categorical reason
  and Retry are shown.
- **Project/task disappeared:** selection returns to the nearest valid parent and
  reports that the previous public reference is unavailable.
- **Metric unavailable:** an explicit unavailable value with provenance/caveat,
  never zero or a blank cell.
- **No comparable baseline:** raw values remain visible and interpretive language
  is suppressed.

## Accessibility

- WCAG AA contrast for text, states, and focus indicators.
- Native controls, native tab order, visible focus, and current-page semantics.
- Page and section headings form a valid hierarchy.
- Tables use captions, scoped headers, and end-aligned tabular numeric cells.
- Charts expose concise screen-reader summaries and visible label/value fallbacks.
- Color is paired with text and stable labels.
- Theme and project selection work with keyboard only.
- Narrow desktop layouts do not clip controls or page content; irreducible tables
  use local horizontal scrolling.

## Testing Strategy

### Model and query tests

- Dashboard DTO validation, immutability, deterministic ordering, and schema
  version.
- Public reference projection and private-field rejection.
- Project discovery, fallback labels, optional display names, and shared-worktree
  identity.
- Global trusted-source Refresh attribution across two projects, including
  shared worktrees, missing roots, and stale catalog entries.
- Preservation of zero, unavailable, lower bounds, provenance, and caveats.
- Bounded task pagination and evidence lookup.

### Refresh tests

- RED/GREEN coverage for single-flight behavior, progress ordering, successful
  replacement, partial failure, retry, and prior-snapshot retention.
- No subprocess invocation and no browser-supplied source paths.

### HTTP tests

- Loopback-only bind, launch token, Host/Origin rejection, no CORS, CSP/security
  headers, method/content-type restrictions, and bounded request sizes.
- Fragment scrubbing, tab-scoped token recovery after reload, no-referrer and
  no-store behavior.
- Invalid references and errors remain categorical and privacy-safe.
- Static assets are packaged and served with correct content types.

### Renderer and browser tests

- Escaping of display names, semantic notes, task families, and evidence data.
- Required landmarks, headings, captions, labels, chart alternatives, and focus
  states.
- Real browser verification of both themes, keyboard navigation, project/task
  selection, Compare, Refresh progress/failure, empty states, and narrow desktop
  behavior.
- Automated unit and integration tests remain dependency-free; browser QA is a
  release verification step rather than a shipped runtime dependency.

### Regression gate

- Focused dashboard suites.
- Full Python unit suite, compile check, wheel/sdist content verification, and
  diff check.
- Existing JSON, Markdown, static report, audit, plugin, hook, and MCP contracts
  remain green.

## Rollout

1. Add project catalog and immutable dashboard DTO/query layer.
2. Add Refresh controller with progress and single-flight tests.
3. Add secured loopback HTTP server and packaged static assets.
4. Build Overview and project navigation.
5. Add Tasks master-detail, Compare, System health, and Evidence lookup.
6. Complete accessibility, theme, failure-state, packaging, and browser QA.

The first pilot uses the local Hydra repository and at least one second project
already present in the shared database. Success requires switching projects,
refreshing safely, tracing one task to evidence, comparing two tasks without
overclaiming, and completing the browser and regression gates above.

## Acceptance Criteria

- `hydra-codex dashboard` opens a secured loopback-only multi-project interface.
- No runtime dependency beyond Python 3.12 standard library is added.
- The browser never receives private IDs, paths, prompts, commands, or tool
  output.
- Project switching, explicit Refresh, task drill-down, comparison, health, and
  evidence lookup work on real Hydra data.
- Refresh is single-flight, reports progress, retains prior data on failure, and
  never blocks navigation.
- The UI follows `DESIGN.md`, supports light/dark themes, keyboard use, reduced
  motion, and WCAG AA contrast.
- Canonical report/audit schemas and existing CLI/MCP/plugin behavior remain
  backward compatible.
- Focused and full verification suites pass, and real browser QA covers both
  themes and all principal states.
