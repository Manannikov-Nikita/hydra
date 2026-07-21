# Hydra for Codex plugin

This plugin bundle is a post-pilot interface for Hydra reports, semantic hooks,
and the typed MCP tools. It does not replace the project-local CLI and hooks
used during the initial five-task pilot.

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
