# Hydra report interpretation

Hydra exposes a versioned `hydra.report/v4` document. `display_name` is a
validated, optional human-readable task label; the opaque `task_ref` remains
the stable copyable identity. Each report also carries a strict
`sync_freshness` snapshot (`hydra.sync-freshness/v1`) so a detached v4 item
retains the materialized-state revision and freshness at which it was read.
Numeric facts contain:

- `value`: measured value or `null` when unavailable;
- `unit`: tokens, milliseconds, count, ratio, or percent;
- `provenance`: `exact`, `derived`, `model_reported`, or `estimated`;
- `lower_bound`: a safe observed minimum when the exact value is unavailable;
- `caveats`: machine-readable limits on interpretation.

Recent reports are wrapped in `hydra.report-list/v2`. The wrapper keeps the
same `sync_freshness` value as every contained v4 item for list/v2
compatibility. It contains a monotonic `data_revision` and one state:

- `current`: materialized reports match all processed durable work;
- `queued` or `running`: wait for the worker, or explicitly run
  `hydra-codex sync`;
- `repair_required`: a source attributed to the selected project may need
  optional historical recovery; the current report remains readable and a
  global `hydra-codex repair --all` is not required for reporting;
- `reconcile_required`: no valid materialized report exists yet;
- `unknown`: freshness was not supplied by the calling surface.

`recorded_*` includes all observed model usage. `deduplicated_*` subtracts only
verified fork replay baselines. `working_tokens` is input minus cached input plus
output. `full_context` is input plus output. Reasoning stays a separate numeric
component and is not added to output again.

An incomplete task is cut off at its last trusted activity. A complete task is
cut off at the root completion event. `semantic_coverage` can be zero for legacy
tasks; Hydra does not infer phase labels from old transcript prose.

`semantic` contains derived working-token, full-context, and
reasoning allocations for each explicitly marked phase plus an `unclassified`
bucket. Its `marker_count`, `self_report_missing`, and conflict diagnostics are
deterministic counts. Phase token attribution remains derived because Codex
reports usage per model call rather than per semantic operation.

`semantic.annotations` contains bounded, redacted model-marker counts and a
maximum of twenty recent marker summaries. Its `test_evidence` is deterministic:
rows aggregate test scope, failure cause, retry kind, semantic phase, and model
cause. It never exposes commands, output, session/turn IDs, evidence keys, or
timestamps. Missing or malformed interval boundaries leave purpose
`unclassified` and add a schema diagnostic.

`pilot_health` aggregates missing markers, semantic conflicts, instrumentation
calls, and schema diagnostics for the local project. Instrumentation overhead
remains estimated until calibrated and is never subtracted from observed
tokens. Status remains `not_started`, `measuring`, or `awaiting_receipt`; the
report cannot claim a verified pilot without an external real-task receipt.

`trend.input` is evidence for a later comparable-family trend window and
`trend.result` is the conservative assessment. A warning requires the current
task plus four strictly earlier completed tasks in the same privacy-safe
`task_family`, token growth, and a second increasing deterministic signal.
Complete derived test-retry counts qualify; estimated, model-reported, partial,
future, and equal-time inputs do not.

Shell-derived `file_reads` and `file_writes` remain observed lower bounds.
Ambiguous compound commands, expansions, unsafe workdirs, and deferred calls
produce no inferred file facts rather than false precision.

## Pair comparison

`hydra.comparison/v2` retains the full public metric set for both task reports,
their raw deltas, and raw percent change. Interpretation is gated by `verdict`:

- `comparable`: complete same-family tasks, no scope exclusion, usable facts,
  and a current verified pilot receipt for both reports;
- `partial`: the family is compatible, but the receipt or evidence is not
  sufficient for a complete claim;
- `not_comparable`: known family mismatch or explicit scope/comparison guard;
- `unknown`: the task family or completion state is unavailable.

`reasons` contains privacy-safe categorical codes. Raw percent change is never
an automatic claim of improvement, regression, or causality. The comparison
continues to state that instrumentation was not subtracted from observed token
totals.
