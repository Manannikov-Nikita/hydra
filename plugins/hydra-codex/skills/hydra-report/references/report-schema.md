# Hydra report interpretation

Hydra exposes a versioned `hydra.report/v2` document. Numeric facts contain:

- `value`: measured value or `null` when unavailable;
- `unit`: tokens, milliseconds, count, ratio, or percent;
- `provenance`: `exact`, `derived`, `model_reported`, or `estimated`;
- `lower_bound`: a safe observed minimum when the exact value is unavailable;
- `caveats`: machine-readable limits on interpretation.

`recorded_*` includes all observed model usage. `deduplicated_*` subtracts only
verified fork replay baselines. `working_tokens` is input minus cached input plus
output. `full_context` is input plus output. Reasoning stays a separate numeric
component and is not added to output again.

An incomplete task is cut off at its last trusted activity. A complete task is
cut off at the root completion event. `semantic_coverage` can be zero for legacy
tasks; Hydra does not infer phase labels from old transcript prose.

`semantic_breakdown` contains derived working-token, full-context, and
reasoning allocations for each explicitly marked phase plus an `unclassified`
bucket. Its `marker_count`, `self_report_missing`, and conflict diagnostics are
deterministic counts. Phase token attribution remains derived because Codex
reports usage per model call rather than per semantic operation.

`pilot_health` aggregates missing markers, semantic conflicts, instrumentation
calls, and schema diagnostics for the local project. Instrumentation overhead
remains estimated until calibrated and is never subtracted from observed
tokens. `trend_input` is evidence for a later comparable-family trend window;
a warning requires five completed comparable tasks and a second increasing
signal besides working tokens.
