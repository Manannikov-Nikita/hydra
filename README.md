# Hydra Hybrid Telemetry

Hydra combines measurements reconstructed from local Codex events with short,
model-reported semantic markers. The deterministic layer owns tokens, elapsed
time, tool calls, files, tests, and the subagent tree. The model may report only
the current phase, cause, scope change, blocker, and finish outcome. If the two
layers disagree, the deterministic observation wins and the report retains a
`semantic_conflict`.

The MVP supports local Codex App, CLI, and IDE tasks. Cloud-only and ephemeral
tasks that have no local event stream cannot be reconstructed.

## Public standalone installation

Install the checksum-validated release runtime, connect its bundled plugin to
Codex, then initialize the project:

```bash
curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
hydra-codex install -y
cd /path/to/project
hydra-codex init .
hydra-codex status . --json
hydra-codex dashboard
```

Start a new Codex task after enabling the plugin. The dashboard binds only to
loopback. For explicit project, port, and browser behavior:

```bash
hydra-codex dashboard --cwd /path/to/project --port 0 --no-open
```

The raw `main/install.sh` bootstrap URL is mutable. Read
[the installation guide](docs/installation.md) for the trust boundary and
standalone targets, then use:

- [Upgrade and uninstall](docs/upgrade-and-uninstall.md)
- [Privacy and retained data](docs/privacy.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release and attestation process](docs/release-process.md)

## Developer installation and Hydra pilot

Hydra requires Python 3.12 and has no runtime dependencies outside the standard
library:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

Activate that environment before using the short `hydra-codex` examples below:

```bash
source .venv/bin/activate
```

Without activation, invoke the entry point explicitly as
`.venv/bin/hydra-codex`. Installing into `.venv` does not add `hydra-codex` to
the `PATH` of a fresh shell.

For the complete verification suite, including real wheel and source-archive
content checks, install the declared test tooling and run:

```bash
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m unittest discover
```

The `test` extra is build-time tooling only; installed Hydra runtime remains
dependency-free.

The repository opts in with a stable, non-secret project identifier:

```toml
# .hydra/project.toml
project_id = "hprj_4db8fca38ef042f3"
telemetry = "hybrid"
```

Different worktrees containing that same project ID contribute observations to
one project. Their relative worktree labels remain observations rather than new
project identities. The project-local `.codex/hooks.json` enables the pilot for
new Codex turns in this repository. In Codex, trust the project, review `/hooks`,
and confirm that `UserPromptSubmit`, `PostToolUse`, and `Stop` point to this
repository's `integrations/codex/hook.py`. Start a new task after first enabling
or changing those hooks; an already-open task does not prove that the new hook
manifest was loaded.

By default, private state is stored outside the repository. macOS uses
`~/Library/Application Support/Hydra/hydra.sqlite3`; Linux uses
`~/.local/share/hydra/hydra.sqlite3`, or
`$XDG_DATA_HOME/hydra/hydra.sqlite3` when `XDG_DATA_HOME` is set. The
installation HMAC key lives beside it. Tests and isolated runs may set
`HYDRA_DATABASE_PATH` and `HYDRA_INSTALLATION_KEY_PATH`.

For the supported chat-to-dashboard startup, canary, Sync, recovery, and
shutdown procedure, see [Dashboard operations](docs/dashboard-operations.md).

## Quick dashboard startup

From the Hydra repository, after trusting its hooks and starting a new Codex
task:

```bash
.venv/bin/hydra-codex doctor --format markdown
.venv/bin/hydra-codex dashboard
```

The dashboard binds only to `127.0.0.1` and normally opens the browser itself.
It intentionally starts from a bounded stored snapshot; it does not scan local
Codex history on launch. Hooks enqueue new source bytes, and the MCP/dashboard
worker consumes that queue automatically. **Sync now** explicitly drains the
same bounded queue. **Repair history** is the separate resumable full walk.

## CLI

Process only durable hook/source queue entries:

```bash
hydra-codex sync
```

Run the explicit resumable history repair when a backfill is required:

```bash
hydra-codex repair --all
```

The legacy full import remains available for controlled migrations and
diagnostics:

```bash
hydra-codex ingest
```

On an interactive terminal, ingest reports privacy-safe `discover`, `inspect`,
`scan`, and `reconcile` counters on stderr; JSON stdout remains unchanged. A
first run after a source-state migration may scan every local rollout, while
subsequent unchanged locations use metadata-only checks.

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

App Server notifications do not themselves provide a universal event
timestamp. A live capture therefore writes one strict receipt envelope per
line: `{"received_at":"<RFC3339 UTC>","message":{...JSON-RPC notification...}}`.
Hydra treats `received_at` as the observed time. Unwrapped legacy exports remain
readable for backfill, but events without an embedded timestamp keep estimated
timestamp provenance rather than receiving a fabricated time.

For one canonical session, exactly one token-total authority is selected:
rollout cumulative counters, then App Server cumulative totals, then OTel
per-call events as an explicitly estimated fallback. Lower-priority events stay
available as timestamped allocation hints but are never added to the selected
total.

`hydra-codex annotate` is normally invoked through the capability-bearing
command injected by `UserPromptSubmit`; it rejects calls without the hook-issued
turn capability. A model changes phase or reports a blocker with a short
marker, then sends one `finish` marker before its final response. The CLI writes
only that semantic payload, capability, and an opaque request nonce to a private
atomic envelope under `$TMPDIR/Hydra/spool`; it never opens the global Hydra
database. `PostToolUse`, with `UserPromptSubmit` and `Stop` as safety nets,
drains acknowledged envelopes. Hooks, not the model, bind project/session/turn
identity, sequence, and timestamps.

This is a cooperative instrumentation boundary, not local-process
authentication: another process running as the same user can invoke the hook
entrypoint with a forged envelope. Hook-derived session/turn observations are
therefore marked `derived`, and Hydra never describes them as cryptographic or
out-of-band proof. Annotation arguments still cannot supply session or turn IDs.

Reconciliation is repeatable and stores an immutable input digest:

```bash
hydra-codex reconcile
hydra-codex report --last 10 --format json
hydra-codex report --last 10 --format markdown --output hydra-report.md
hydra-codex report --last 10 --format html --output hydra-report.html
```

Generate one canonical pilot audit as JSON, Markdown, or static HTML:

```bash
hydra-codex audit \
  --pilot hpilot_v1_0123456789abcdef0123456789abcdef \
  --format json \
  --output hydra-audit.json
```

`hydra.audit/v1` contains the exact public `hydra.pilot/v1` snapshot, the
cohort overview and per-task views, current read-only storage health, and one
exact-once evidence appendix. Its canonical JSON bytes can be passed directly
to `hydra-codex pilot close`. Because a bare CLI or MCP call has no
host-attested turn context, it does not drain pending annotations or invent
session or turn identity; the audit records that pending drain as unavailable.

Reports print opaque public references such as `task_…`. Compare two of those
references without exposing internal session IDs:

```bash
hydra-codex compare task_a1b2 task_c3d4 --format markdown
```

Comparison output uses `hydra.comparison/v2`. Its verdict is one of
`comparable`, `partial`, `not_comparable`, or `unknown`. Hydra calls a pair
comparable only when both tasks are complete, share one known task family,
have usable deterministic evidence, pass scope guards, and are backed by a
current verified pilot receipt. Every verdict retains the raw delta and raw
percent change; a percentage is not described as an improvement or regression
when the pair is not comparable.

Check the local installation without exposing paths, project IDs, or SQLite
error text:

```bash
hydra-codex doctor --format markdown
hydra-codex storage status --format markdown
```

`doctor` emits `hydra.doctor/v1` categorical checks for project resolution,
storage availability, schema, foreign keys, integrity, and restrictive file
permissions. `storage status` emits `hydra.storage-status/v1` and compares
current DB/WAL and event counts with the latest successful canonical audit.
Before the first audit it reports `growth_baseline_unavailable` instead of
inventing a trend.

Maintenance is explicit and never performs retention deletion:

```bash
hydra-codex storage compact \
  --confirmation "compact hydra database" \
  --format markdown
```

Compaction only checkpoints WAL and runs SQLite `VACUUM`. It fingerprints every
user table before and after, then reports that evidence rows, immutable pilot
receipts, and audit snapshots were retained. Worktrees, rollout files, and
semantic evidence are not deleted.

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
Direct, unambiguous `rg`, `sed`, `cat`, `head`, and `tail` commands contribute
privacy-safe relative file lower bounds; compound, expanded, deferred, or
otherwise ambiguous shell expressions contribute no guessed file facts.

The semantic marker explains why an interval happened. Report v4 also exposes
a deterministic test-evidence cross-tab by scope, failure, retry kind, semantic
phase, and semantic cause. This distinguishes final verification from product,
flaky, and infrastructure retries without retaining command text or output.
Phase token allocation is still `derived`, because Codex exposes usage per
model call rather than per semantic operation.

Every numeric fact in `hydra.report/v4` has one provenance value:

- `exact`: directly observed and unambiguous;
- `derived`: deterministic calculation from observations;
- `model_reported`: semantic data supplied by the model;
- `estimated`: unavailable, lower-bound, or explicitly uncertain data.

Intervals without markers remain `unclassified`; Hydra does not infer labels
from historical assistant prose. Instrumentation tool calls are counted
separately. Their token impact remains estimated until calibration and is never
subtracted from the observed total.

## Hook lifecycle

`UserPromptSubmit` first drains pending annotations, then creates an opaque turn
capability and records the initial `understand` phase. `PostToolUse` performs the
normal drain; only a successfully persisted envelope is deleted. Malformed,
expired, duplicate, and out-of-order envelopes are moved to a private quarantine
with categorical diagnostics. A later annotation closes the preceding semantic
interval. `Stop` drains before deciding. On the first missing finish it returns
one complete executable finish command with a fresh capability. If the repeated
Stop still has no finish marker, Hydra records `self_report_missing` and fails
open so telemetry never blocks the user.

The checkout hook and packaged plugin use the same runtime. If both are enabled,
the explicit project hook owns the event and the plugin copy is suppressed
before any database mutation.

## Incremental sync

Hooks persist allowlisted lifecycle facts and enqueue only validated,
root-relative source locators. While MCP or the dashboard is running, one
lease-coordinated worker processes new JSONL bytes from durable checkpoints and
reconciles only dirty projects. `hydra-codex sync` performs the same bounded
queue drain explicitly; `hydra-codex repair --all` is the separate resumable,
potentially expensive history walk. One repair invocation continues through
bounded batches until the persisted frontier is complete; if it cannot make
progress (for example, another worker owns the lease), it exits with a
privacy-safe `partial` diagnostic and a later invocation resumes that frontier.

`hydra.report` reads materialized SQLite state and never starts ingest or
reconciliation. The `hydra.report-list/v2` wrapper and every contained
`hydra.report/v4` item carry the same `sync_freshness` snapshot and
`data_revision`, so callers can distinguish current, queued, running,
repair-required, and unknown state without scanning source directories.

## Privacy and source policy

Hydra's deterministic adapters do not store raw prompts, assistant messages,
tool output, command text, patches, or search results. They retain allowlisted
categories, hashes, lengths, relative paths, and schema diagnostics. A short
model note is the only free-text telemetry field: common secrets, paths, email
addresses and phone numbers are redacted, but this is not a general-purpose
content classifier. Agents must never paste prompt, transcript, or tool output
into it. Annotation
arguments cannot contain tokens, times, file/test counts, paths, session IDs,
turn IDs, or timestamps. `task_family` is a lowercase categorical code such as
`multiple-answer-quiz`; every separator-delimited segment must come from
Hydra's small public taxonomy, and unsafe or private-looking values are rejected before
storage and unsafe legacy values never form a trend cohort.
An optional `task_label` supplies the human-readable task name shown in reports
and the dashboard. It is Unicode-normalized, limited to 80 characters, and
rejects control/bidi characters, paths, and secret-like content. Common
accepted `task_family` terminals include `quiz`, `workflow`, `architecture`,
`hardening`, `review`, `report`, `tests`, `runtime`, and `docs`; use
`unclassified` when no public category fits rather than embedding an identity.

The versioned rollout JSONL adapter is read-only. Hydra does not read or mutate
Codex internal SQLite databases as a telemetry API. Versioned App Server and
OpenTelemetry adapters are the forward-compatible exact-event surfaces; schema
drift is diagnosed without retaining an unknown payload.

## MCP, skill, and plugin status

The source plugin is packaged under `plugins/hydra-codex/` for the post-pilot
stage. Its MCP server advertises `hydra.report` by default and hosts the durable
incremental worker independently of report calls. The tool accepts
exactly one of `last` for the existing report list or `pilot` for a canonical
pilot audit, plus the requested format. It deliberately does
not advertise `hydra.annotate` until Codex provides an authenticated turn
transport outside model-controlled MCP arguments. The cooperative capability CLI
remains the annotation fallback.

Doctor and storage maintenance remain CLI-only. The plugin MCP surface does
not expose local diagnostics or mutating maintenance commands.

Wheel and source distributions ship that same canonical bundle. Use
`hydra-codex-plugin path` to locate it, or
`hydra-codex-plugin materialize /absolute/new/path` to copy it without
overwriting an existing directory, then activate the returned directory through
the plugin installation flow provided by the Codex host. The equivalent Python
API is `hydra_codex.plugin_bundle`.

`$hydra-report` describes how to read, compare, and explain materialized facts without
upgrading estimates into exact measurements. A skill guides workflow and
reporting; it is not the guaranteed collection mechanism.

The first pilot is intentionally incomplete at repository delivery time. Run
five subsequent real Codex tasks with the local CLI and hooks, then evaluate
missing-marker rate, semantic conflicts, schema diagnostics, and observed
instrumentation calls. Do not enable trend warnings before five completed tasks
in the same `task_family`; a token increase also needs a second increasing
deterministic signal such as a complete test-retry count, read amplification,
review/fix cycles, or compaction. Only strictly earlier tasks form the baseline.
The fifth task leaves pilot status at `awaiting_receipt`; Hydra never marks the
pilot verified merely because five fixtures or tasks exist. Only after a real
pilot receipt should the plugin become the default install.

Use this operational sequence for the first real cohort:

```bash
hydra-codex pilot start --target 5 --task-family telemetry-analysis
# Complete five subsequent real tasks with hooks enabled.
hydra-codex pilot status --format markdown
hydra-codex audit --pilot hpilot_v1_... --format json --output hydra-audit.json
hydra-codex pilot close --pilot hpilot_v1_... \
  --audit-json hydra-audit.json --decision verified
```

The operator owns the go/no-go decision after reading the canonical audit. A
receipt must be explicitly `verified` or `rejected`; threshold failures cannot
be closed as verified. Keep the JSON audit as the immutable decision input.
For a rejected cohort, repeat `pilot close` with `--decision rejected`, correct
the instrumentation, and start a new pilot rather than rewriting evidence.

## Authoritative Codex surfaces

- [Hooks](https://learn.chatgpt.com/docs/hooks)
- [App Server](https://learn.chatgpt.com/docs/app-server)
- [Observability and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
- [Skills](https://learn.chatgpt.com/docs/build-skills)
