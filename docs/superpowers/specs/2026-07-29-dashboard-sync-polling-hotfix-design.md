# Dashboard Sync Polling Hotfix Design

## Problem

The dashboard currently treats any rejected `syncStatus` request as proof that
the persisted sync job failed. A transient `database_busy`, temporary storage
failure, invalid response, or network interruption therefore changes the UI to
`Sync failed` even while `/api/v1/sync` still reports `state: running`.

## Required behavior

- A transient status-read failure must keep the active job UI busy and retry
  automatically.
- A successful status read resets the consecutive-failure counter.
- Five consecutive status-read failures end automatic polling.
- Exhausted polling must display `Sync status unavailable` or
  `Repair status unavailable`; it must not claim the persisted job failed.
- `reopen_dashboard` remains immediately actionable and is not retried.
- `Sync failed` and `Repair history failed` remain reserved for terminal job
  payloads whose `state` is `failed`.
- Existing evidence stays visible throughout polling and recovery.
- No dashboard API or persisted job schema changes are introduced.

## Implementation

Add a small exported polling helper to `dashboard_assets/app.js`. It owns the
one-second cadence, consecutive-failure counter, successful-reset behavior, and
terminal callback. `pollRefresh` supplies the real `syncStatus` request and the
existing rendering callback. The outer active-job catch uses a dedicated
status-unavailable title so a transport failure cannot be confused with a
terminal worker failure.

## Verification

The Node-backed dashboard asset contract will execute the real helper with:

1. `temporary failure -> running -> succeeded`, proving the failure is retried;
2. repeated failures, proving polling stops after five attempts;
3. a successful response between failures, proving the counter resets;
4. sync and repair kinds, proving transport copy never says the job failed.

The full Python suite and a browser run against the local dashboard must remain
green. Browser acceptance requires a transient status failure to leave the job
in a loading/retrying state and the later terminal payload to determine the
outcome.
