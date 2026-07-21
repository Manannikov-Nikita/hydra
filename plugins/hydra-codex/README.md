# Hydra for Codex plugin

This plugin bundle is a post-pilot interface for Hydra reports and semantic
hooks. It does not replace the project-local CLI and hooks used during the
initial five-task pilot.

The bundled MCP server advertises `hydra.report` by default. It deliberately
does not advertise `hydra.annotate` until Codex supplies a trusted turn
transport that can bind identity outside model-controlled arguments. During
the pilot, the capability-bearing CLI command injected by the hook is the only
supported annotation path.

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

The checkout-local `.codex/hooks.json` remains the uninstalled fallback for the
Hydra repository itself. Its wrapper loads `src/` directly and delegates to the
same packaged runtime. When both sources are present, the plugin marks its hook
source and suppresses itself for events owned by the project manifest. This
keeps one model instruction and one Stop decision without disabling unrelated
project hooks.
