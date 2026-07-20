# Hydra Hybrid Telemetry

Hydra keeps a small, local SQLite ledger for trusted hook observations and
separate model-reported annotations. The package uses only the Python standard
library and resolves a stable opaque ID from `.hydra/project.toml`; each
worktree contributes its relative path as an observation rather than becoming a
new project identity.

Privacy is structural: the database schema stores redacted annotation notes,
their SHA-256 hashes, and lengths. It deliberately has no prompt, message, or
tool-output columns. Model annotation input cannot declare identity, timing,
token, file, or test measurements; those must come from trusted integrations.
Each model annotation also carries a required `task_family` (non-empty text of
at most 80 characters), so later reconciliation can group semantic work without
capturing raw interaction content.

When version 2 upgrades a version 1 database, it re-sanitizes every retained
legacy note inside the migration transaction. Rows whose project/session/turn
links contradict each other are removed and recorded as hash-only quarantine
conflicts before relational triggers are installed.

This foundation provides contracts, identity discovery, and versioned SQLite
migrations only. Rollout parsing, reports, hooks, MCP, and plugin behavior are
intentionally out of scope.
