# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder definition for the native Hydra runtime."""

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).parent
PACKAGE_ROOT = PROJECT_ROOT / "src" / "hydra_codex"
DASHBOARD_ASSETS = (
    "index.html",
    "tokens.css",
    "dashboard.css",
    "bootstrap.js",
    "api.js",
    "state.js",
    "dom.js",
    "app.js",
    "views/shell.js",
    "views/overview.js",
    "views/tasks.js",
    "views/compare.js",
    "views/health.js",
    "views/evidence.js",
)
DEFERRED_HYDRA_MODULES = (
    "hydra_codex.annotation_core",
    "hydra_codex.annotation_persistence",
    "hydra_codex.annotation_spool",
    "hydra_codex.annotation_types",
    "hydra_codex.audit_builder",
    "hydra_codex.audit_coherence",
    "hydra_codex.audit_detail_renderers",
    "hydra_codex.audit_model",
    "hydra_codex.audit_service",
    "hydra_codex.classifier",
    "hydra_codex.cli",
    "hydra_codex.codex_event_ingest",
    "hydra_codex.codex_events",
    "hydra_codex.contracts",
    "hydra_codex.custom_tool_persistence",
    "hydra_codex.dashboard_contract",
    "hydra_codex.dashboard_launch",
    "hydra_codex.dashboard_model",
    "hydra_codex.diagnostics",
    "hydra_codex.exact_time",
    "hydra_codex.hook_runtime",
    "hydra_codex.install_layout",
    "hydra_codex.installation_cli",
    "hydra_codex.js_literal_fields",
    "hydra_codex.mcp_server",
    "hydra_codex.migrations_i9",
    "hydra_codex.otel_allocation",
    "hydra_codex.pilot",
    "hydra_codex.pilot_renderers",
    "hydra_codex.project",
    "hydra_codex.project_config",
    "hydra_codex.project_schema",
    "hydra_codex.public_refs",
    "hydra_codex.reconcile_engine",
    "hydra_codex.reconcile_reports",
    "hydra_codex.redaction",
    "hydra_codex.report_operations",
    "hydra_codex.report_semantics",
    "hydra_codex.reporting",
    "hydra_codex.rollout_identity",
    "hydra_codex.rollout_persistence",
    "hydra_codex.rollout_privacy",
    "hydra_codex.rollout_reconcile",
    "hydra_codex.runtime_entrypoint",
    "hydra_codex.semantic",
    "hydra_codex.services",
    "hydra_codex.shell_facts",
    "hydra_codex.storage_health",
    "hydra_codex.task_tree_types",
    "hydra_codex.test_evidence",
    "hydra_codex.token_selection",
)

datas = []
for relative in DASHBOARD_ASSETS:
    destination = Path("hydra_codex") / "dashboard_assets" / Path(relative).parent
    datas.append((str(PACKAGE_ROOT / "dashboard_assets" / relative), str(destination)))

# The builder creates metadata from the staged canonical version. Using
# copy_metadata("hydra-codex") would copy the build environment's version and
# make a genuine second fixture release report stale distribution metadata.
metadata = tuple((PROJECT_ROOT / "src").glob("hydra_codex-*.dist-info"))
if len(metadata) != 1:
    raise RuntimeError("exact staged Hydra package metadata is required")
datas.append((str(metadata[0]), metadata[0].name))
datas.append((str(PROJECT_ROOT / "LICENSE"), metadata[0].name))

analysis = Analysis(
    [str(PROJECT_ROOT / "packaging" / "_frozen_main.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=list(DEFERRED_HYDRA_MODULES),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="hydra-codex",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="hydra-codex",
)
