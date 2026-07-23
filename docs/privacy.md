# Privacy and retained data

Hydra is local telemetry. It reconstructs deterministic facts from local Codex
event streams and stores short, redacted semantic markers supplied through the
hook capability.

## Default database locations

- macOS: `~/Library/Application Support/Hydra/hydra.sqlite3`
- Linux: `~/.local/share/hydra/hydra.sqlite3`
- Linux with an absolute `XDG_DATA_HOME`:
  `$XDG_DATA_HOME/hydra/hydra.sqlite3`

The installation identity key (`rollout-hmac.key`) and Codex integration receipt
live beside the database. Database, WAL, key, receipt, spool, and quarantine
locations are private per-user state. Release bundles live under `~/.hydra/`,
and the command launcher is `~/.local/bin/hydra-codex`.

## What persists

Hydra stores normalized token counters, timestamps, provenance, tool and test
categories, opaque hashes, lengths, safe relative paths, project and task
pseudonyms, schema diagnostics, and redacted model notes. It also keeps
immutable pilot receipts and audit evidence needed to explain historical
reports.

## What never persists by default

Deterministic adapters do not store raw prompts, assistant messages, tool
output, command text, patches, search results, session IDs, or turn IDs. The
short model note is redacted and constrained, but it is not a general-purpose
content classifier; do not place secrets or transcript excerpts in it.

Hydra reads rollout JSONL files through versioned, read-only adapters. It does
not use Codex's internal SQLite database as a telemetry API and does not modify
rollout history.

## Removal semantics

`hydra-codex uninstall` removes Hydra-owned integration and runtime files but
preserves telemetry and identity data. `hydra-codex uninstall --keep-cli`
preserves the CLI as well. Project uninitialization removes only Hydra's
project-local configuration after exact confirmation. Deleting retained
telemetry is a separate, explicit operator decision; Hydra provides no
retention-delete command.

The local hook capability limits accidental or model-supplied identity changes,
but it is cooperative instrumentation rather than authentication against
another process running as the same operating-system user.
