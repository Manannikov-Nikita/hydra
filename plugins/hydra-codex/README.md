# Hydra for Codex plugin

This plugin bundle is a post-pilot interface for Hydra reports and semantic
hooks. It does not replace the project-local CLI and hooks used during the
initial five-task pilot.

The bundled MCP server advertises `hydra.report` by default. It deliberately
does not advertise `hydra.annotate` until Codex supplies an authenticated turn
transport outside model-controlled arguments. During the pilot, the
capability-bearing CLI command injected by the cooperative hook is the only
supported annotation path. That command atomically stages a private envelope
under `$TMPDIR/Hydra/spool`; `PostToolUse` normally drains it, while
`UserPromptSubmit` and `Stop` provide safety-net drains. The model-side command
never writes the global Hydra database directly.

The report tool returns the same `hydra.report/v3` contract as the CLI,
including provenance-aware semantic markers, deterministic test evidence,
pilot health, and conservative trend results. The source bundle is packaged
but is not automatically enabled by installing the Python package; activation
remains a post-pilot operator step.

## Installation prerequisite

hydra-codex must be installed in the environment that starts Codex before
this plugin is enabled. The hook manifest invokes the installed
`hydra-codex-hook` entrypoint, while the MCP manifest invokes
`hydra-codex-mcp`. If either executable is unavailable, install the Python
package first; do not rewrite the plugin to select a turn by `cwd`.

## Post-pilot activation

The Python wheel and source distribution contain this complete plugin bundle.
After installing `hydra-codex` in the environment that starts Codex, locate the
installed, immutable bundle with:

```bash
hydra-codex-plugin path
```

Pass that returned directory to the plugin installation flow exposed by the
Codex host. If the host or operator needs a separately managed copy, materialize
the same complete bundle into a new directory and activate that directory:

```bash
hydra-codex-plugin materialize /absolute/path/to/hydra-codex-plugin
```

Materialization refuses to overwrite an existing path. Python integrations may
use `hydra_codex.plugin_bundle.plugin_bundle_path` and
`hydra_codex.plugin_bundle.materialize_plugin_bundle` for the same operations.
Neither operation enables the post-pilot plugin automatically.

The checkout-local `.codex/hooks.json` remains the uninstalled fallback for the
Hydra repository itself. Its wrapper loads `src/` directly and delegates to the
same packaged runtime. When both sources are present, the plugin marks its hook
source and suppresses itself for events owned by the project manifest. This
keeps one model instruction, one annotation drain, and one Stop decision without
disabling unrelated project hooks.
