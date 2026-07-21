---
name: hydra-report
description: Reconcile and explain privacy-safe Hydra telemetry for local Codex tasks. Use when the user asks about token usage, task time, files read or changed, test repetitions, subagents, semantic phase coverage, trends, or comparisons between Codex tasks.
---

# Hydra Report

## Overview

Build reports from Hydra's deterministic ledger and clearly separate exact,
derived, model-reported, estimated, and unavailable facts. Never reconstruct
raw prompts or turn an agent's prose summary into an exact measurement.

## Workflow

1. Confirm that the current directory belongs to a Hydra project containing
   `.hydra/project.toml`.
2. Prefer the `hydra.report` MCP tool when available. It reconciles before
   rendering. Otherwise run `hydra-codex ingest`, `hydra-codex reconcile`, and
   `hydra-codex report --last N --format json` in that order.
3. For a requested pair, run
   `hydra-codex compare THREAD_A THREAD_B --format json`. Accept only opaque
   public references printed by Hydra, never internal session identifiers.
4. Read every fact's `provenance`, `lower_bound`, and `caveats` before
   explaining it. Preserve `unavailable` and `unclassified` rather than
   guessing.
5. Treat a trend warning as valid only when Hydra reports at least five
   completed tasks in the same `task_family` and a second exact signal besides
   token growth.

## Live annotations

During an instrumented task, call `hydra.annotate` only when the semantic phase
changes, a blocker appears, or the task finishes. Keep `note` under 240
characters. Report purpose and outcome only. Never supply tokens, elapsed time,
test or file counts, paths, `session_id`, `thread_id`, `turn_id`, or timestamps;
trusted hooks add identity and time.

For the MVP, follow the exact capability-bearing CLI command injected by the
turn hook. The typed MCP annotation tool is post-pilot: use it only when the
Codex integration supplies trusted turn capability out of band. Never add a
capability to the semantic tool arguments or choose a turn by `cwd`.

Do not annotate historical tasks after the fact. A historical report with zero
semantic coverage is valid evidence that no phase labels existed.

## Reporting rules

- Lead with the outcome, then show the smallest useful metric table.
- Keep wall-clock separate from summed agent-time when subagents overlap.
- Reasoning is a separate numeric counter; do not add it to output again.
- Never subtract instrumentation calls from observed token totals. Before
  calibration, instrumentation overhead is estimated or unavailable.
- When semantic labels disagree with test or tool evidence, deterministic facts
  win and the report retains the `semantic_conflict` caveat.
- Read [report-schema.md](references/report-schema.md) for field meanings.
