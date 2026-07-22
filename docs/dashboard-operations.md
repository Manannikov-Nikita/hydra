# Hydra dashboard operations

This is the supported local procedure for collecting Hydra telemetry from Codex
tasks and showing it in the loopback dashboard. It covers the repository-local
pilot hooks. The packaged plugin remains a post-pilot installation path.

## 1. Install the local CLI

Hydra requires Python 3.12. From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

Either activate the environment:

```bash
source .venv/bin/activate
hydra-codex --help
```

or use the explicit entry point in every command:

```bash
.venv/bin/hydra-codex --help
```

This guide uses the explicit path. A fresh shell will not find the executable
by its short name merely because it was installed into `.venv`.

## 2. Opt the repository into Hydra

The project needs a stable identifier at `.hydra/project.toml`:

```toml
project_id = "hprj_4db8fca38ef042f3"
telemetry = "hybrid"
```

The identifier is not a secret. Worktrees that carry the same identifier are
reported as one project. An optional `display_name` may contain 1 to 80 safe
characters and is used only for presentation.

The pilot hook manifest is `.codex/hooks.json`. It must contain the three
turn-scoped command hooks:

- `UserPromptSubmit` opens a trusted turn, records `understand`, and supplies a
  capability-bearing annotation command to the model.
- `PostToolUse` normally drains staged semantic markers into Hydra storage.
- `Stop` performs a final drain and asks once for a missing `finish` marker.

Do not place session IDs, turn IDs, timestamps, token counts, file counts, or
test counts in model annotations. The hook supplies identity and time.

## 3. Trust and verify the hooks in Codex

1. Open the repository, or a descendant directory, as the Codex project.
2. Trust the project when Codex requests it.
3. Open `/hooks` and review the three Hydra commands. They must resolve to this
   repository's `integrations/codex/hook.py` and use Python 3.12.
4. Approve new or changed command hooks. Review them again after editing
   `.codex/hooks.json`.
5. Start a **new task**. Existing tasks may have been created before the hook
   manifest was trusted or loaded.

The hook is deliberately fail-open: telemetry must never prevent the user from
working. Therefore silence is not proof that collection works; run the canary
below.

## 4. Run a new-task canary

In a new Codex task in this repository, ask for a small read-only operation and
name a public task family, for example:

```text
Read README.md and name one operational risk. Do not change files.
For semantic markers use task_family telemetry-analysis.
```

Expected behavior:

1. `UserPromptSubmit` supplies Hydra instructions and records `understand`.
2. The model submits phase markers during the task.
3. Before the final response, the model submits one `finish` marker.
4. If it omits `finish`, the first `Stop` response supplies one complete retry
   command. A repeated omission records `self_report_missing` and does not block
   the user.

After the canary has finished, import and reconcile from the Hydra repository:

```bash
.venv/bin/hydra-codex doctor --format markdown
.venv/bin/hydra-codex ingest
.venv/bin/hydra-codex reconcile
.venv/bin/hydra-codex report --last 1 --format markdown
```

The doctor should report healthy project resolution, storage, schema, foreign
keys, integrity, and file permissions. The latest report should show the canary
task, its `telemetry-analysis` family, and semantic coverage. If it does not,
follow [Chat telemetry is missing](#chat-telemetry-is-missing).

## 5. Start the dashboard

Normal interactive startup opens the default browser and chooses an available
loopback port:

```bash
.venv/bin/hydra-codex dashboard
```

For a scripted or Codex-controlled launch, keep the process attached to its
owning terminal and request the private handoff URL explicitly:

```bash
.venv/bin/hydra-codex dashboard --no-open --port 0
```

The server binds only to `127.0.0.1`. The initial URL fragment contains a
per-launch credential. The page removes it immediately and retains it only in
that tab's `sessionStorage`. Do not paste the handoff URL into logs, issues, or
chat messages.

Dashboard launch reads only bounded catalog metadata. A project may initially
appear stale or require Refresh even when the database already contains tasks.
That is expected.

## 6. Refresh the dashboard

Press **Refresh** in the top bar. This is the only browser-triggered mutation.
Hydra then scans the trusted default roots:

- `~/.codex/sessions`;
- `~/.codex/archived_sessions`.

It reports `discover`, `inspect`, `scan`, and `reconcile` progress. A second
Refresh request reuses the active job instead of starting a concurrent import.
Existing valid evidence stays visible until the new project snapshots are
ready.

Refresh is intentionally manual. The following actions do **not** refresh the
dashboard:

- finishing a Codex task;
- reloading the page;
- leaving the dashboard open;
- running only `hydra-codex annotate`.

After new tasks finish, press Refresh again. For deterministic CLI automation,
run `ingest` and `reconcile` before starting a fresh dashboard process.

## 7. Recover from common states

### `source_changed`

One of the rollout files or its owning root changed while Hydra was validating
it. This is common when Refresh scans the currently active task that is still
writing its own rollout.

1. Leave the previous dashboard snapshot in place; Hydra already retains it.
2. Finish or pause the task that is writing the source.
3. Wait until the rollout is stable.
4. Press **Refresh again**.

Repeatedly pressing Refresh again from the same actively writing task can reproduce the
same partial result. `ingest --source label=/path` does not isolate the import:
explicit sources are added to the default active and archived roots. There is no
supported exclusive-source flag in the current CLI.

### `database_busy`

Another Hydra writer holds the database. Let the current ingest, reconcile, or
Refresh finish, then press **Refresh again**. Do not start parallel full-history imports.

### `reconciliation_stale`

The observations changed after the last reconciliation. Once writers are idle,
run:

```bash
.venv/bin/hydra-codex ingest
.venv/bin/hydra-codex reconcile
```

Then press **Refresh again** or restart the dashboard.

### `storage_unavailable`

Run the privacy-safe diagnostic first:

```bash
.venv/bin/hydra-codex doctor --format markdown
```

Confirm that the CLI, hooks, and dashboard use the same default database and
installation key. Isolated runs that set `HYDRA_DATABASE_PATH` must also set the
matching `HYDRA_INSTALLATION_KEY_PATH`. Correct access or schema problems before
retrying. Do not delete the database, key, WAL, or evidence as a recovery step.

### `reopen_dashboard`

A normal reload in the original tab reuses its tab-scoped credential. A new
tab, a cleared session, or a restarted server has no valid credential. Stop the
old process safely and launch `.venv/bin/hydra-codex dashboard` again to obtain
a fresh private handoff. Do not move the credential to `localStorage` or a
shared URL.

### `internal_failure`

Keep the prior snapshot, run doctor, and retry after other writers stop. If the
state repeats, stop and relaunch the dashboard, then reproduce with the explicit
CLI sequence `ingest` followed by `reconcile`. Hydra intentionally exposes only
the categorical browser diagnostic; inspect the local command exit status
rather than publishing raw paths or payloads.

### Chat telemetry is missing

1. Verify that the task's working directory resolves upward to
   `.hydra/project.toml`.
2. Check `/hooks` for `UserPromptSubmit`, `PostToolUse`, and `Stop`.
3. Trust any new or changed commands.
4. Start a new task and repeat the canary.
5. Run doctor, ingest, reconcile, and `report --last 1` before opening the
   dashboard.

If both the checkout hook and packaged plugin are enabled, the explicit project
hook owns the event and the plugin copy suppresses itself. Do not add a second
copy of the same project hook to compensate for missing telemetry.

## 8. Stop safely

Keep the dashboard process attached to its owning terminal. Stop it with
`Ctrl+C`. Hydra closes the Refresh controller and loopback server; the launch
credential expires with that process.

Do not use broad process-kill commands, delete SQLite files, or remove rollout
history. If the terminal was lost, identify the exact Hydra dashboard process
and its loopback port before stopping only that process. Restarting the
dashboard is safe and does not delete the retained database, audit receipts, or
source logs.

## 9. Minimal operational checklist

- `.venv/bin/hydra-codex --help` succeeds.
- `.hydra/project.toml` resolves from the task's working directory.
- `/hooks` shows the three reviewed and trusted Hydra hooks.
- A new-task canary produces a latest report with semantic markers.
- `doctor` is healthy before relying on the dashboard.
- The dashboard was opened from its current private launch handoff.
- Refresh reached `succeeded`, or a partial state was handled without discarding
  the previous snapshot.
- The dashboard is stopped with `Ctrl+C` when the review session ends.
