# Hydra for Codex plugin

This plugin bundle provides Hydra reports and semantic hooks through Codex. It
ships in the same verified Hydra release as the CLI and must match that
release's runtime version.

For the public standalone release, install the CLI bundle first and let Hydra
register this exact versioned marketplace:

```bash
curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
hydra-codex install -y
```

Start a new Codex task after installation. The raw `main/install.sh` URL is a
mutable bootstrap trust boundary; the downloaded versioned archive is checked
against `SHA256SUMS`, and users who need publisher identity should verify its
GitHub artifact attestation.

The bundled MCP server advertises `hydra.report` by default. It deliberately
does not advertise `hydra.annotate` until Codex supplies an authenticated turn
transport outside model-controlled arguments. During the pilot, the
capability-bearing CLI command injected by the cooperative hook is the only
supported annotation path. That command atomically stages a private envelope
under `$TMPDIR/Hydra/spool`; `PostToolUse` normally drains it, while
`UserPromptSubmit` and `Stop` provide safety-net drains. The model-side command
never writes the global Hydra database directly.

In recent-report mode, the tool returns `hydra.report-list/v2`, containing
`hydra.report/v4` items with privacy-safe task display names, provenance-aware
semantic markers, deterministic test evidence, pilot health, conservative
trend results, and the same materialized-state `sync_freshness` snapshot as the
list. Pilot mode returns the canonical `hydra.audit/v1` document. Reading a
report never starts ingest or reconciliation; the MCP lifecycle hosts the
separate lease-coordinated incremental worker.

The plugin deliberately does not expose doctor or storage maintenance. Run
`hydra-codex doctor`, `hydra-codex storage status`, and confirmed compaction
locally from the CLI so a model-controlled MCP call cannot trigger maintenance.

## Install into Codex

After installing the Hydra runtime, reconcile its bundled local marketplace
and exact plugin version through the supported Codex plugin commands:

```bash
hydra-codex install -y
```

Start a new Codex task after installation so the task loads the plugin. Running
the same command again is a no-op. `hydra-codex upgrade` refreshes the Codex integration atomically
with the runtime, so no separate install or refresh command is needed after an
upgrade.

To inspect the exact supported commands without checking Codex or changing
state:

```bash
hydra-codex install --print-config codex
```

To remove only the receipt-owned Codex integration while preserving the Hydra
CLI, telemetry, installation identity, and project files:

```bash
hydra-codex uninstall --keep-cli
```

Mutating commands require `-y` in non-interactive use or an explicit
confirmation on a terminal. Hydra uses only `codex plugin marketplace` and
`codex plugin` commands. It refuses ambiguous ownership and does not rewrite
unrelated Codex configuration.

The checkout-local `.codex/hooks.json` remains the uninstalled fallback for the
Hydra repository itself. Its wrapper loads `src/` directly and delegates to the
same packaged runtime. When both sources are present, the plugin marks its hook
source and suppresses itself for events owned by the project manifest. This
keeps one model instruction, one annotation drain, and one Stop decision without
disabling unrelated project hooks.
