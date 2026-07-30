# Project-Scoped Freshness and Safe Repair Guidance Design

## Summary

Hydra reports must describe the freshness of the selected project, not the
health of every telemetry source in the installation. A source belonging to a
different project, or a legacy source that has not been attributed to any
project, must not make the current project's report say `repair_required`.

The public `hydra.sync-freshness/v1` schema remains unchanged. Global history
repair remains available as an explicit operator action, but Hydra Report,
Dashboard copy, and recovery documentation must describe it as optional global
maintenance rather than a prerequisite for reading a task report.

## Problem

Both the CLI report service and the Dashboard materialized-report reader
currently derive `repair_required` from an installation-wide query:

```sql
SELECT 1
  FROM sync_source_registry
 WHERE source_state='repair_required'
 LIMIT 1
```

The query ignores the selected `project_id`. One damaged historical source can
therefore mark unrelated projects as needing repair. The Hydra Report skill
then tells an agent that a full report requires `hydra-codex repair --all`.
That command is the only full directory walker and scans the trusted active and
archived Codex session roots across every project. It can inspect thousands of
files and may still finish `partial` when a historical source cannot be
attributed or repaired.

This creates an incorrect escalation:

1. A report for project A is readable.
2. An unrelated source from project B or an unattributed legacy source is
   `repair_required`.
3. Project A's report inherits the global state.
4. The reporting instruction presents global repair as required.
5. The operator starts an expensive scan that is unrelated to project A and
   may not clear the global condition.

## Goals

- Make `sync_freshness.state` describe only the selected project.
- Preserve `hydra.sync-freshness/v1` and its exact public fields.
- Keep current task reports readable when unrelated global history needs
  maintenance.
- Make repair guidance optional, explicit about scope, and proportional to the
  user's request.
- Prevent the Dashboard from encouraging an immediate repeated full scan after
  a `partial` repair.
- Add regression coverage for cross-project isolation in both CLI reports and
  Dashboard materialized reads.

## Non-goals

- No storage migration or new database column.
- No `hydra.sync-freshness/v2`.
- No `repair --project` command.
- No preflight source count. Obtaining an exact count would itself require the
  global discovery walk that this change is intended to avoid triggering
  casually.
- No automatic quarantine, deletion, or rewriting of irreparable historical
  sources.
- No change to the explicit `hydra-codex repair --all` execution semantics.

## Chosen architecture

### Project-scoped freshness

The source registry already stores an optional `project_id`. Freshness queries
will use the selected project identity as their scope:

- `repair_required` considers only registry rows whose `project_id` equals the
  selected project.
- queued or claimed rollout sources are joined to their registry row and
  filtered by the selected project.
- hook outbox rows and dirty reconciliation roots are filtered directly by
  their `project_id`.
- installation-wide sync-job state does not independently make a project
  `queued` or `running`; project-owned queued, claimed, or dirty work is the
  evidence for those states.
- a registry row with `project_id IS NULL` remains global operator-maintenance
  evidence but does not contaminate any project report.
- after project work is otherwise current, the existing project reconciliation
  fence continues to determine `reconcile_required`.

State priority remains:

```text
repair_required -> queued -> running -> current -> reconcile_required
```

The final `reconcile_required` transition remains conditional on the selected
project's source-fact fence.

### Public report compatibility

The public freshness object remains exactly:

```json
{
  "schema_version": "hydra.sync-freshness/v1",
  "state": "current",
  "data_revision": 123
}
```

No global maintenance field is added to task reports. Global repair health is
an operator concern and remains on operator surfaces rather than becoming part
of every project report.

### CLI report path

`LocalCommandServices._sync_freshness(store, project_id)` will apply
project filters to registry, ingest queue, hook outbox, and dirty-root checks.
The report and compare schemas do not change.

An active global repair job alone does not make a project report `running`.
If that job is processing the selected project, the selected project's source
or dirty-root state supplies the project-scoped signal.

### Dashboard materialized-report path

`DashboardQueryService._materialized_state` will accept a selected
`project_id`. Callers must resolve the public project reference before asking
for freshness:

- the bootstrap snapshot resolves the selected catalog project and then
  computes freshness for that project;
- paginated task reads resolve the project before computing freshness;
- an empty catalog has no project freshness and does not derive state from
  unrelated global work.

The Dashboard's existing project catalog `current`/`stale` state remains
separate from the embedded task-report `sync_freshness` contract.

### Hydra Report guidance

The Hydra Report skill and report-schema reference will state:

- `repair_required` means at least one source attributed to the current project
  needs historical recovery;
- already materialized report facts remain readable, with freshness caveats;
- the agent must not claim that repair is required merely to read or send the
  report;
- the agent must not recommend or launch `repair --all` unless the user asks to
  recover missing historical evidence or explicitly requests global repair;
- before mentioning the command as an action, the agent must explain that it
  scans active and archived telemetry across every project, may inspect
  thousands of files, may take a long time, and may still finish `partial`;
- `queued` or `running` continues to permit a bounded `hydra-codex sync`.

### Dashboard repair guard

The Dashboard confirmation will say that full repair:

- scans telemetry history across every Hydra project;
- may inspect thousands of files;
- may take a long time;
- may still finish `partial`;
- is not required to view the current report.

The confirmation button will be labelled **Start global repair**.

When repair finishes `partial`, the Dashboard will show the terminal
diagnostic without a one-click **Repair again** action. A deliberate retry
must start again from the normal **Repair history** control and pass through
the global-scope confirmation. Normal bounded sync retains its existing retry
behavior.

### Operator documentation

Dashboard operations documentation will distinguish:

- bounded project work: wait for the worker or run `hydra-codex sync`;
- optional historical recovery: run global repair only when missing history is
  important enough to justify the scan;
- `repair_required` does not invalidate already materialized facts;
- a `partial` repair is a terminal maintenance result, not an instruction to
  repeat the same full scan automatically.

## Data flow

```text
selected cwd/public project ref
            |
            v
       opaque project_id
            |
            v
project-filtered registry / queue / outbox / dirty-root checks
            |
            v
 hydra.sync-freshness/v1
            |
            +--> CLI/MCP report
            |
            +--> Dashboard materialized task payload

global repair job and unattributed legacy sources
            |
            v
operator repair surfaces only
```

## Error and privacy behavior

- Public reports continue to expose only the existing categorical state and
  monotonic data revision.
- No source path, locator, project identity, or repair count enters the public
  report.
- Unknown or unattributed sources fail closed into operator maintenance rather
  than being guessed into a project.
- A source explicitly attributed to the selected project still produces
  `repair_required`.
- Existing storage and reconciliation errors keep their current behavior.

## Testing

Implementation follows red-green-refactor:

1. Replace the existing installation-wide local-service expectation with two
   regression cases:
   - a foreign or unattributed repair source does not affect the selected
     project;
   - a repair source attributed to the selected project wins over its queued
     work.
2. Add project-isolation cases for queued and running work so foreign worker
   activity cannot change the selected project's state.
3. Add Dashboard query coverage showing that a foreign repair source does not
   alter embedded task-report freshness, while an owned repair source does.
4. Exercise Dashboard behavior to prove a `partial` repair has no immediate
   retry action and a fresh repair still requires the global confirmation.
5. Run focused local-service, reporting, Dashboard query, and Dashboard asset
   tests.
6. Run the complete repository test suite and the repository's standard static
   or packaging checks before completion.

## Acceptance criteria

- A report for project A is not `repair_required`, `queued`, or `running`
  solely because project B or an unattributed source has that state.
- A project-A source in `repair_required` still makes project A
  `repair_required`.
- The JSON schema and exact freshness keys remain
  `hydra.sync-freshness/v1`, `state`, and `data_revision`.
- Hydra Report never presents `repair --all` as required to read or send an
  existing report.
- Dashboard repair confirmation accurately describes global scope and cost.
- Dashboard does not offer one-click repeat repair after a `partial` result.
- No migration is added.
- Existing user data and historical telemetry are preserved.
