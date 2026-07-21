# Hydra Hybrid Telemetry

Hydra combines measurements reconstructed from local Codex events with short,
model-reported semantic markers. The deterministic layer owns tokens, elapsed
time, tool calls, files, tests, and the subagent tree. The model may report only
the current phase, cause, scope change, blocker, and finish outcome. If the two
layers disagree, the deterministic observation wins and the report retains a
`semantic_conflict`.

The MVP supports local Codex App, CLI, and IDE tasks. Cloud-only and ephemeral
tasks that have no local event stream cannot be reconstructed.

## Install and enable the Hydra pilot

Hydra requires Python 3.12 and has no runtime dependencies outside the standard
library:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --no-build-isolation -e .
```

The repository opts in with a stable, non-secret project identifier:

```toml
# .hydra/project.toml
project_id = "hprj_4db8fca38ef042f3"
telemetry = "hybrid"
```

Different worktrees containing that same project ID contribute observations to
one project. Their relative worktree labels remain observations rather than new
project identities. The project-local `.codex/hooks.json` enables the pilot for
new Codex turns in this repository.

By default, private state is stored outside the repository at
`~/Library/Application Support/Hydra/hydra.sqlite3`; the installation HMAC key
lives beside it. Tests and isolated runs may set `HYDRA_DATABASE_PATH` and
`HYDRA_INSTALLATION_KEY_PATH`.

## CLI

Import the default active and archived local rollout roots:

```bash
hydra-codex ingest
```

An explicit rollout JSONL file or directory can be added without changing the
defaults:

```bash
hydra-codex ingest --source explicit=/absolute/path/to/rollouts
```

Versioned App Server and OTel JSONL exports can be imported in the same
transaction:

```bash
hydra-codex ingest \
  --event-source app-server-v2=/absolute/path/to/app-server.jsonl \
  --event-source otel-v1=/absolute/path/to/otel.jsonl
```

For one canonical session, exactly one token-total authority is selected:
rollout cumulative counters, then App Server cumulative totals, then OTel
per-call events as an explicitly estimated fallback. Lower-priority events stay
available as timestamped allocation hints but are never added to the selected
total.

`hydra-codex annotate` is normally invoked through the capability-bearing
command injected by `UserPromptSubmit`; it rejects calls without the trusted
turn capability. A model changes phase or reports a blocker with a short
marker, then sends one `finish` marker before its final response. Hooks, not the
model, bind project/session/turn identity and timestamps.

Reconciliation is repeatable and stores an immutable input digest:

```bash
hydra-codex reconcile
hydra-codex report --last 10 --format json
hydra-codex report --last 10 --format markdown --output hydra-report.md
hydra-codex report --last 10 --format html --output hydra-report.html
```

Reports print opaque public references such as `task_…`. Compare two of those
references without exposing internal session IDs:

```bash
hydra-codex compare task_a1b2 task_c3d4 --format markdown
```

## What is exact and what is semantic

For each cumulative usage epoch Hydra keeps the final observed counter rather
than summing intermediate cumulative events:

```text
working_tokens = input_tokens - cached_input_tokens + output_tokens
full_context = input_tokens + output_tokens
```

Reasoning tokens are reported separately and are not added to output again.
Verified child fork replay baselines are subtracted component by component;
missing baselines remain an explicit uncertainty. Root wall-clock and summed
agent-time are separate because subagents may overlap.

The rollout adapter deterministically classifies tool calls, relative file
reads/writes, test runner and targeted/full scope, structured product failures,
infrastructure failures, flaky retries, and verification after a code change.
The semantic marker explains why that interval happened. Phase token allocation
is still `derived`, because Codex exposes usage per model call rather than per
semantic operation.

Every numeric fact in `hydra.report/v2` has one provenance value:

- `exact`: directly observed and unambiguous;
- `derived`: deterministic calculation from observations;
- `model_reported`: semantic data supplied by the model;
- `estimated`: unavailable, lower-bound, or explicitly uncertain data.

Intervals without markers remain `unclassified`; Hydra does not infer labels
from historical assistant prose. Instrumentation tool calls are counted
separately. Their token impact remains estimated until calibration and is never
subtracted from the observed total.

## Hook lifecycle

`UserPromptSubmit` creates an opaque turn capability and records the initial
`understand` phase. A later annotation closes the preceding semantic interval.
At `Stop`, a missing finish marker requests exactly one retry. If the second
Stop still has no finish marker, Hydra records `self_report_missing` and fails
open so telemetry never blocks the user.

The checkout hook and packaged plugin use the same runtime. If both are enabled,
the explicit project hook owns the event and the plugin copy is suppressed
before any database mutation.

## Privacy and source policy

Hydra does not store raw prompts, assistant messages, tool output, command text,
patches, or search results. It retains allowlisted categories, hashes, lengths,
privacy-safe short notes, relative paths, and schema diagnostics. Annotation
arguments cannot contain tokens, times, file/test counts, paths, session IDs,
turn IDs, or timestamps.

The versioned rollout JSONL adapter is read-only. Hydra does not read or mutate
Codex internal SQLite databases as a telemetry API. Versioned App Server and
OpenTelemetry adapters are the forward-compatible exact-event surfaces; schema
drift is diagnosed without retaining an unknown payload.

## MCP, skill, and plugin status

The source plugin is packaged under `plugins/hydra-codex/` for the post-pilot
stage. Its MCP server advertises `hydra.report` by default. It deliberately does
not advertise `hydra.annotate` until Codex provides a trusted turn transport
that binds identity outside model-controlled MCP arguments. The capability CLI
remains the annotation fallback.

`$hydra-report` describes how to reconcile, compare, and explain facts without
upgrading estimates into exact measurements. A skill guides workflow and
reporting; it is not the guaranteed collection mechanism.

The first pilot is intentionally incomplete at repository delivery time. Run
five subsequent real Codex tasks with the local CLI and hooks, then evaluate
missing-marker rate, semantic conflicts, schema diagnostics, and observed
instrumentation calls. Do not enable trend warnings before five completed tasks
in the same `task_family`; a token increase also needs a second increasing
signal such as test retries, read amplification, review/fix cycles, or
compaction. Only after that pilot should the plugin become the default install.

## Authoritative Codex surfaces

- [Hooks](https://learn.chatgpt.com/docs/hooks)
- [App Server](https://learn.chatgpt.com/docs/app-server)
- [Observability and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
- [Skills](https://learn.chatgpt.com/docs/build-skills)
