# Upgrade and uninstall

Hydra keeps versioned, verified runtime bundles under the user's home directory.
An upgrade stages a complete candidate, validates it, refreshes the Codex
integration, and only then makes it current. If integration refresh fails, the
previous release remains active.

## Upgrade

Check without changing the installation:

```bash
hydra-codex upgrade --check
```

Install the latest strictly newer release:

```bash
hydra-codex upgrade
```

Hydra refuses malformed versions, downgrades, checksum mismatches, unsafe
archives, and concurrent installation activity. Repeating a completed upgrade
is a no-op; retrying an interrupted refresh resumes from the retained journal.

## Uninstall

Detach the Hydra plugin from Codex while retaining the standalone CLI:

```bash
hydra-codex uninstall --keep-cli
```

Detach the plugin and remove Hydra-owned CLI launchers and release bundles:

```bash
hydra-codex uninstall
```

Both commands require confirmation. For intentional automation, use the
non-interactive forms:

```bash
hydra-codex uninstall -y --keep-cli
hydra-codex uninstall -y
```

Uninstall preserves telemetry: it does not remove `hydra.sqlite3`, the
installation HMAC key, Codex rollout history, project `.hydra/project.toml`
files, worktrees, or user data. This separation prevents removal of the
application from silently deleting audit evidence.

To stop one repository from participating while preserving its accumulated
telemetry, run from that repository:

```bash
hydra-codex uninit . --confirmation "remove hydra project"
```

Review [privacy and retained data](privacy.md) before manually deleting any
database or key.
