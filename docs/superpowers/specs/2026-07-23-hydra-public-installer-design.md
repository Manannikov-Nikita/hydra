# Public Hydra Installer Design

## Summary

Hydra will be installable by a normal Codex user without cloning this
repository, installing Python, creating a virtual environment, or copying
checkout-local hooks. The primary distribution channel is a public GitHub
repository at `Manannikov-Nikita/hydra` with versioned GitHub Release assets
and a one-line POSIX installer.

The first public release supports:

- macOS arm64;
- macOS x86_64;
- Linux x86_64.

Windows and Linux arm64 are explicitly outside the first release.

## User experience

The complete first-time flow is:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh |
  sh

hydra-codex install -y

cd /path/to/project
hydra-codex init .
hydra-codex status . --json
hydra-codex dashboard
```

The user does not need Python, `pip`, `uv`, `pipx`, a virtual environment,
repository source files, or administrator privileges.

The command groups are:

```text
hydra-codex install [-y] [--refresh] [--print-config codex]
hydra-codex init [path] [--name NAME]
hydra-codex status [path] [--json]
hydra-codex upgrade [--check]
hydra-codex uninstall [--keep-cli]
hydra-codex uninit [path] --confirmation "remove hydra project"
hydra-codex hook
hydra-codex mcp
```

Existing telemetry, reporting, dashboard, doctor, storage, and pilot commands
remain available.

## Chosen architecture

### Versioned standalone bundle

Each release publishes one archive per supported platform. The archive is a
self-contained application bundle with an embedded Python runtime and the
Hydra package, dashboard assets, Codex plugin, local marketplace manifest, and
license. It does not depend on the target machine's Python installation.

The installer places releases under:

```text
~/.hydra/
  versions/
    <version>/
      bin/hydra-codex
      runtime/
      marketplace/
      LICENSE
  current -> versions/<version>
```

It exposes the stable entry point:

```text
~/.local/bin/hydra-codex -> ~/.hydra/current/bin/hydra-codex
```

The bundle uses one public executable. Plugin hooks and MCP invoke
`hydra-codex hook` and `hydra-codex mcp`. Existing
`hydra-codex-hook`, `hydra-codex-mcp`, and `hydra-codex-plugin` console
scripts remain in Python distributions for backward compatibility, but public
release assets do not duplicate the embedded runtime for each entry point.

### Installer

`install.sh`:

1. Detects `Darwin` or `Linux` and the supported machine architecture.
2. Resolves the requested version, or the latest stable release when no
   version is provided.
3. Downloads the matching archive and release checksum file over HTTPS.
4. Verifies SHA-256 before extracting any executable content.
5. Extracts into a private staging directory under `~/.hydra`.
6. Verifies the expected bundle layout and executable version.
7. Atomically moves the staged release into `versions/<version>`.
8. Atomically switches `current` and the `~/.local/bin` launcher.
9. Prints a PATH warning and exact remediation when `~/.local/bin` is not
   visible to the current shell.

It never uses `sudo`, overwrites unrelated files, or removes an older working
version during installation. A failure before the final symlink switch leaves
the previous version active.

### Codex integration

The standalone bundle contains a complete local Codex marketplace with the
existing `hydra-codex` plugin. `hydra-codex install`:

1. Verifies that a compatible `codex` executable is available.
2. Installs or refreshes the bundled local marketplace through the supported
   `codex plugin marketplace` command.
3. Installs the `hydra-codex` plugin through `codex plugin add`.
4. Verifies that the installed plugin resolves the same Hydra version.
5. Reports that newly installed hooks apply to new Codex tasks.

`--print-config codex` is read-only and prints the proposed marketplace,
plugin, hook, and MCP configuration. `--refresh` updates only an existing
Hydra integration. `-y` accepts the normal global integration defaults without
interactive questions.

The plugin is global, but its hook is opt-in at the project boundary. It does
not persist telemetry when the current working directory does not resolve to
a valid `.hydra/project.toml`.

### Project initialization

`hydra-codex init [path]` writes only:

```toml
schema_version = 1
project_id = "hprj_<random identifier>"
display_name = "<sanitized directory name or --name>"
telemetry = "hybrid"
```

The generated project ID follows one canonical pattern and is
cryptographically random. It is stable, non-secret, and intended to be
committed so that worktrees of the same project share one Hydra identity.

Initialization:

- accepts a directory or descendant path and resolves a canonical project
  target;
- refuses a filesystem root or the user's home directory unless a future,
  separately designed override is introduced;
- refuses symlink aliases that make the target identity ambiguous;
- creates `.hydra/project.toml` atomically;
- preserves an existing valid configuration byte for byte;
- fails closed on malformed or conflicting Hydra configuration;
- never creates the telemetry database inside the project;
- never writes project-local Codex hooks;
- is safe under repeated and concurrent execution.

`hydra-codex uninit` requires the exact confirmation string and removes only
the project configuration that Hydra owns. It does not remove SQLite data,
worktrees, source files, Codex configuration, or plugin state.

### Status and diagnostics

`hydra-codex status [path] --json` is read-only and returns stable,
machine-readable state for:

- CLI version and supported platform;
- installation layout and active version;
- Codex executable availability;
- Hydra marketplace and plugin integration state;
- project initialization and project identity validity;
- storage path, schema availability, and integrity state;
- whether a new Codex task is required after integration changes;
- privacy-safe next actions.

An uninitialized directory is a valid status result with
`initialized: false`; it is not a command failure. `doctor` remains the deeper
diagnostic command.

## Platform storage

macOS retains the existing default:

```text
~/Library/Application Support/Hydra/
```

Linux uses:

```text
$XDG_DATA_HOME/hydra/
```

or, when `XDG_DATA_HOME` is unset:

```text
~/.local/share/hydra/
```

The database, installation key, annotation spool, and receipts remain outside
project repositories. Existing environment-variable overrides continue to
work for tests and operator-controlled isolation.

No automatic macOS data migration is needed because its current path is
unchanged.

## Upgrade and removal

`hydra-codex upgrade --check` performs a read-only release check.
`hydra-codex upgrade` downloads and verifies the new release through the same
installer primitives, switches atomically, refreshes the existing Codex
integration, and retains the previous version for rollback.

`hydra-codex uninstall --keep-cli` removes only the Codex plugin and
marketplace integration. Full uninstall removes Hydra launchers and installed
version bundles after detaching the integration. It does not remove the
telemetry database or installation key. Data deletion remains an explicit,
separately confirmed storage operation.

## Public repository and release pipeline

The public repository is `Manannikov-Nikita/hydra`. It includes:

- an MIT License;
- complete package and project metadata;
- the POSIX installer;
- public installation, upgrade, removal, privacy, and troubleshooting docs;
- a normal quality workflow for pushes and pull requests;
- a release workflow triggered only by version tags.

For each `v*` tag, the release workflow:

1. Checks out the exact tag into a clean workspace.
2. Runs the full source test suite.
3. Builds the three supported standalone bundles.
4. Runs installed-bundle acceptance tests without relying on source imports or
   a system Python runtime.
5. Runs an end-to-end fake-home canary for install, Codex integration, project
   initialization, hook handling, ingest, reconciliation, and dashboard
   startup.
6. Produces SHA-256 checksums and GitHub artifact attestations.
7. Publishes immutable release assets only after every matrix job succeeds.

Local ignored `dist/` contents are never used as publication input.

## Error handling and privacy

User-facing failures identify the failed stage without leaking private paths,
rollout names, prompts, model output, hook payloads, or capabilities.

The following conditions fail without partial project or integration state:

- unsupported platform or architecture;
- checksum mismatch;
- incomplete or wrong-version bundle;
- unavailable or incompatible Codex CLI;
- malformed marketplace or plugin receipt;
- malformed existing project configuration;
- unwritable project directory;
- concurrent initialization conflict;
- failed plugin installation or verification.

Hook collection remains fail-open for Codex work: telemetry failure never
blocks the user's task. Installation, initialization, upgrade, and removal
commands are fail-closed because they mutate durable configuration.

## Testing strategy

Implementation follows test-driven development.

### Unit and integration coverage

- platform and data-directory resolution;
- project ID generation and validation;
- empty, initialized, malformed, symlinked, home, and filesystem-root targets;
- byte-stable repeated initialization;
- concurrent initialization;
- atomic-write cleanup after failure;
- status JSON for every supported state;
- install, refresh, print-config, upgrade, rollback, and uninstall transitions;
- preservation of unrelated Codex configuration;
- plugin/runtime version parity;
- checksum and archive-layout rejection;
- privacy-safe errors.

### Distribution coverage

- wheel and source archive compatibility;
- standalone macOS arm64, macOS x86_64, and Linux x86_64 bundles;
- absence of a system-Python dependency in release execution;
- complete dashboard and plugin assets;
- exact release version embedded in all surfaces;
- clean-tag provenance;
- public installer against a local fake release server;
- unsupported platform diagnostics.

### End-to-end acceptance

In an isolated home directory:

1. Install one release.
2. Configure a fake or real Codex CLI through `hydra-codex install`.
3. Initialize a foreign repository.
4. Deliver `UserPromptSubmit`, `PostToolUse`, and `Stop` hook events.
5. Record a semantic annotation through the issued capability.
6. Ingest and reconcile a synthetic rollout.
7. Start the dashboard on loopback.
8. Verify that the new project and task are visible.
9. Upgrade to a second test release and verify rollback safety.
10. Remove integration and CLI while preserving the database.

## Rollout

1. Implement and verify all source, installer, and standalone-bundle behavior
   locally.
2. Re-authenticate GitHub CLI for `Manannikov-Nikita`.
3. Create the public `Manannikov-Nikita/hydra` repository.
4. Push the reviewed source without publishing a release.
5. Run the public quality workflow.
6. Publish the first version tag.
7. Verify the installer on clean macOS arm64, macOS x86_64, and Linux x86_64
   environments.
8. Run a real Codex canary in a foreign project.
9. Announce the installer only after the release and canary receipts are
   complete.

## Acceptance criteria

- A user can install Hydra from one shell command without Python or `sudo`.
- `hydra-codex install -y` installs a version-matched Codex plugin without
  manual hook or MCP editing.
- `hydra-codex init .` safely and idempotently opts any ordinary project into
  Hydra.
- `hydra-codex status . --json` accurately explains both initialized and
  uninitialized states.
- A new Codex task in an initialized project records deterministic telemetry
  and semantic markers.
- The dashboard displays that project after refresh.
- Upgrade failure preserves the prior working installation.
- Uninstall preserves user telemetry data by default.
- Release assets are checksum-verified, built from the exact tag, and pass
  clean-environment acceptance tests.
- The existing editable developer workflow and current macOS data remain
  compatible.

## Explicit non-goals for the first release

- Windows support;
- Linux arm64 support;
- PyPI as a public installation channel;
- automatic deletion of telemetry data;
- automatic project-local hook generation;
- cloud-only Codex task recovery;
- interactive hosted dashboards;
- team or organization telemetry aggregation.
