# Troubleshooting

Hydra fails closed when it cannot prove release, path, or integration ownership.
Errors are intentionally categorical and may omit private paths.

## `hydra-codex` is not on `PATH`

The public installer creates `~/.local/bin/hydra-codex`. Add that directory to
`PATH`, then open a fresh shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
hydra-codex --version
```

For a developer installation, activate `.venv` or invoke
`.venv/bin/hydra-codex` explicitly.

## Unsupported platform

Standalone archives are published only for:

- macOS `arm64`;
- macOS `x86_64`;
- Linux `x86_64`.

The installer rejects every other operating-system and architecture pair
before downloading a runtime.

## Codex is missing or incompatible

`hydra-codex install` requires the `codex` command and its JSON plugin
management interface. If Codex is missing or incompatible, update Codex first;
Hydra does not edit unknown configuration files as a fallback.

## Integration ownership conflict

Hydra records the exact marketplace selector, runtime version, and receipt it
owns. A foreign marketplace or plugin with the same names produces an
integration ownership conflict. Inspect the current Codex plugin list and
remove or rename the foreign entry yourself; Hydra will not overwrite it.

## Checksum or archive verification failure

Do not bypass a checksum failure. Remove the failed download, confirm the
version and native target, then retry from the official release. `SHA256SUMS`
detects a byte mismatch; verify GitHub's artifact attestation when publisher
identity matters. Never substitute a digest copied from an unrelated mirror.

## Status is read-only

`hydra-codex status . --json` reports state but never repairs it, imports
rollouts, updates Codex, or mutates the database. Run the explicit `install`,
`upgrade`, `ingest`, or `reconcile` command for the corresponding action.

## Dashboard port conflict

The dashboard accepts `--port`, not a host override, and always binds to
loopback:

```bash
hydra-codex dashboard --cwd /path/to/project --port 0 --no-open
```

Use port `0` to select a free port. If a fixed dashboard port is occupied,
close its owning process or choose a different port; do not kill unrelated
processes by name.

## Installer lock recovery

`$HOME/.hydra-installer-lock` is normally a permanent private regular file with
mode `0600` and exact first line `hydra-installer-lock/v2`. Its presence is not
an error and is not evidence that an installer is running. Hydra uses an
operating-system lock on this file to serialize install, upgrade, and uninstall.
Do not remove the valid v2 file.

A malformed file, symlink, foreign owner, unsafe mode, or a legacy directory at
that path is rejected. This is deliberate: Hydra cannot safely guess whether
another installer owns an ambiguous object.

Manual recovery is allowed only after you prove that no installer is running:
close every shell or automation that started Hydra installation, upgrade, or
uninstall; inspect the process list; and confirm no process holds the path open
(for example, with `lsof "$HOME/.hydra-installer-lock"` where `lsof` is
available). If any ownership is uncertain, stop and preserve the object.

After proving there is no installer, move a malformed or legacy directory aside
for inspection using an explicit, non-existing backup path. Never use recursive
deletion or a wildcard against `$HOME`. A later Hydra command can create the
canonical v2 file. Preserve the backup until the installation succeeds and its
origin is understood.

## Mutable bootstrap URL

The public one-line installer reads `install.sh` from the mutable `main` branch.
Review the script or pin an audited commit URL when this trust boundary is not
acceptable. Standalone release archives are versioned and attested; the first
public release canary must also verify GitHub's immutable-release setting. The
raw bootstrap URL is not immutable.
