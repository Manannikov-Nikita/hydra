"""Lifecycle wiring for the lease-coordinated incremental MCP worker."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .dashboard_sync import DashboardSyncController
from .incremental_sync import TrustedSourceRoots
from .rollout_identity import Pseudonymizer
from .services import configured_database_path, configured_installation_key_path
from .storage import HydraStore


Clock = Callable[[], datetime]


def create_mcp_sync_controller(
    environ: Mapping[str, str],
    *,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> DashboardSyncController:
    """Host the same durable worker as the dashboard without coupling it to reports."""
    if not isinstance(environ, Mapping):
        raise TypeError("MCP sync environment must be a mapping")
    selected_database = configured_database_path(environ, None)
    key_path = configured_installation_key_path(environ, None)
    installation_key = Pseudonymizer.installation_key(key_path).key
    home_value = environ.get("HOME")
    home = (
        Path(home_value).expanduser()
        if isinstance(home_value, str) and home_value
        else Path.home()
    )
    roots = TrustedSourceRoots(
        sessions=home / ".codex" / "sessions",
        archived_sessions=home / ".codex" / "archived_sessions",
    )
    controller = DashboardSyncController(
        store_factory=lambda: HydraStore.open_current(selected_database),
        roots=roots,
        installation_key=installation_key,
        clock=clock,
    )
    controller.activate()
    return controller
