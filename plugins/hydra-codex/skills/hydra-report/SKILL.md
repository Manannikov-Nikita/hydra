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
   Read the `hydra.comparison/v2` verdict before interpreting the deltas.
   `partial`, `not_comparable`, and `unknown` preserve raw evidence but do not
   support a claim that one architecture or workflow caused an improvement.
4. Read every fact's `provenance`, `lower_bound`, and `caveats` before
   explaining it. Preserve `unavailable` and `unclassified` rather than
   guessing.
5. Treat a trend warning as valid only when Hydra reports the current task plus
   four strictly earlier completed tasks in the same `task_family`, token
   growth, and a second complete deterministic signal. A derived test-retry
   count may qualify; estimated, model-reported, partial, future, or equal-time
   evidence may not.

## Live annotations

During an instrumented task, call `hydra.annotate` only when the semantic phase
changes, a blocker appears, or the task finishes. Keep `note` under 240
characters. Redaction is not a general content classifier: report purpose and
outcome only, and never paste prompt, transcript, or tool output. Never supply tokens, elapsed time,
test or file counts, paths, `session_id`, `thread_id`, `turn_id`, or timestamps;
configured hooks add identity and time in the cooperative local workflow. This
is hook-attested telemetry, not cryptographic authentication against another
process running as the same user.

The CLI stages one private atomic envelope under `$TMPDIR/Hydra/spool`; it does
not open Hydra's global SQLite database. `PostToolUse` drains normally, with
`UserPromptSubmit` and `Stop` as safety nets. Stop drains before it decides
whether to return the one permitted fresh capability-bearing finish retry.

Use a lowercase categorical `task_family` such as `multiple-answer-quiz`, not a
prompt excerpt, user identifier, path, email address, UUID, or secret.
Use a public terminal category such as `quiz`, `workflow`, `architecture`,
`hardening`, `review`, `report`, `tests`, `runtime`, or `docs`; otherwise use
`unclassified` rather than inventing a person- or customer-derived label.

For the MVP, follow the exact capability-bearing CLI command injected by the
turn hook. The typed MCP annotation tool is post-pilot: use it only when the
Codex integration supplies an authenticated turn capability outside model
arguments. Never add a
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
- Use `semantic.annotations.test_evidence` to explain whether repeated tests
  were product failures, flaky retries, infrastructure recovery, or final
  verification. Never infer that purpose from command text.
- Read [report-schema.md](references/report-schema.md) for field meanings.
- Treat `percent_change` as a raw arithmetic delta. Use comparative language
  only when the verdict is `comparable`; even then, do not claim causality from
  a single pair.
- Local `doctor` and `storage` commands are operator-only diagnostics and
  maintenance. Do not invoke them through model-controlled MCP tools.
