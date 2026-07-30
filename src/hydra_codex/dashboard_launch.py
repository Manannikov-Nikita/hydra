"""Packaged Evidence Desk assets and bounded local launch lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
import secrets
import sqlite3
import stat
from types import MappingProxyType
from typing import Callable, TextIO
import webbrowser

from .dashboard_model import DashboardRefreshView, DashboardSnapshot
from .dashboard_queries import (
    DashboardQueryService,
    observe_resolved_project,
)
from .dashboard_refresh import (
    DashboardSnapshotCache,
    RefreshController,
)
from .dashboard_refresh_state import RefreshResult
from .dashboard_server import DashboardApplication, DashboardAsset, create_dashboard_server
from .dashboard_sync import DashboardSyncController
from .incremental_sync import TrustedSourceRoots
from .diagnostics import DoctorCheck, DoctorReport
from .exact_time import public_timestamp
from .project import ProjectResolution, resolve_project
from .rollout_identity import Pseudonymizer, RolloutRoot
from .services import configured_database_path, configured_installation_key_path
from .platform_paths import default_database_path
from .storage import MIGRATIONS, HydraStore, ValidatedStoreProvider


_ASSETS = MappingProxyType({
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/tokens.css": ("tokens.css", "text/css; charset=utf-8"),
    "/assets/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/assets/bootstrap.js": ("bootstrap.js", "text/javascript; charset=utf-8"),
    "/assets/api.js": ("api.js", "text/javascript; charset=utf-8"),
    "/assets/state.js": ("state.js", "text/javascript; charset=utf-8"),
    "/assets/dom.js": ("dom.js", "text/javascript; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    **{
        f"/assets/views/{name}.js": (
            f"views/{name}.js", "text/javascript; charset=utf-8",
        )
        for name in ("shell", "overview", "tasks", "compare", "health", "evidence")
    },
})


def load_dashboard_assets() -> Mapping[str, DashboardAsset]:
    """Read only the packaged, compile-time allowlisted dashboard resources."""
    root = resources.files("hydra_codex").joinpath("dashboard_assets")
    loaded = {
        route: DashboardAsset(content_type, root.joinpath(*name.split("/")).read_bytes())
        for route, (name, content_type) in _ASSETS.items()
    }
    return MappingProxyType(dict(sorted(loaded.items())))


def _trusted_roots(environ: Mapping[str, str]) -> tuple[RolloutRoot, ...]:
    home_value = environ.get("HOME")
    home = Path(home_value).expanduser() if home_value else Path.home()
    candidates = (
        (home / ".codex" / "sessions", "active"),
        (home / ".codex" / "archived_sessions", "archived"),
    )
    return tuple(
        RolloutRoot(path, label) for path, label in candidates if path.is_dir()
    )


def _sync_roots(environ: Mapping[str, str]) -> TrustedSourceRoots:
    home_value = environ.get("HOME")
    home = Path(home_value).expanduser() if home_value else Path.home()
    return TrustedSourceRoots(
        sessions=home / ".codex" / "sessions",
        archived_sessions=home / ".codex" / "archived_sessions",
    )


class _DisabledRefreshRunner:
    """Compatibility seam: dashboard refresh endpoints are now durable sync aliases."""

    def __init__(self, store_factory) -> None:
        self._store_factory = store_factory

    def run(self, _progress) -> RefreshResult:
        return RefreshResult({}, False, ("internal_failure",), 0, 0, 0)


_DOCTOR_CODES = (
    "project_resolution", "storage_available", "schema_current",
    "foreign_keys_ok", "integrity_ok", "storage_permissions_restricted",
)


def _idle_refresh() -> DashboardRefreshView:
    return DashboardRefreshView(None, "idle", None, None, None, {}, ())


def _bootstrap_database(
    path: Path,
) -> tuple[sqlite3.Connection | None, str]:
    """Open only bounded launch metadata; never migrate or integrity-scan here."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unavailable"
    if not stat.S_ISREG(mode):
        return None, "unavailable"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=0.1,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return connection, "current" if version == MIGRATIONS[-1][0] else "stale"
    except (OSError, sqlite3.Error, TypeError, ValueError):
        if connection is not None:
            connection.close()
        return None, "unavailable"


def _bootstrap_doctor(
    *, cwd: Path, database_path: Path, database_state: str,
) -> DoctorReport:
    statuses: dict[str, str] = {}
    try:
        resolve_project(cwd)
    except Exception:
        statuses["project_resolution"] = "failed"
    else:
        statuses["project_resolution"] = "ok"
    statuses["storage_available"] = (
        "ok" if database_state in {"current", "stale"} else "failed"
    )
    statuses["schema_current"] = (
        "ok" if database_state == "current"
        else "failed" if database_state == "stale"
        else "unavailable"
    )
    observed_file = False
    restricted = True
    for candidate in (
        database_path,
        Path(str(database_path) + "-wal"),
        Path(str(database_path) + "-shm"),
    ):
        try:
            candidate_mode = candidate.stat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            observed_file, restricted = True, False
            break
        observed_file = True
        if not stat.S_ISREG(candidate_mode) or stat.S_IMODE(candidate_mode) & 0o077:
            restricted = False
    statuses["storage_permissions_restricted"] = (
        "ok" if observed_file and restricted
        else "failed" if observed_file
        else "unavailable"
    )
    return DoctorReport(tuple(
        DoctorCheck(code, statuses.get(code, "unavailable"))
        for code in _DOCTOR_CODES
    ))


def _unavailable_snapshot(
    *, generated_at: str, doctor: DoctorReport,
) -> DashboardSnapshot:
    return DashboardSnapshot(
        generated_at,
        {
            "state": "unavailable",
            "doctor": {
                "scope": "global_launch_context",
                "report": doctor.as_dict(),
            },
        },
        (),
        None,
        None,
        None,
        _idle_refresh(),
    )


def _observe_launch_project(
    *,
    store_factory: Callable[[], HydraStore],
    query: DashboardQueryService,
    cache: DashboardSnapshotCache,
    resolution: ProjectResolution,
    observed_at: str,
) -> None:
    """Best-effort post-bind catalog observation and atomic cache publication."""
    try:
        store = store_factory()
    except Exception:
        return
    try:
        observe_resolved_project(store, resolution, observed_at)
        snapshots, _fallback = query.bootstrap_snapshots_from_connection(
            store.connection,
            refresh=_idle_refresh(),
            preferred_project_id=resolution.project_id,
        )
        cache.replace_all(snapshots)
    except Exception:
        return
    finally:
        try:
            store.close()
        except Exception:
            pass


def run_dashboard(
    *,
    port: int,
    no_open: bool,
    database_path: Path | None,
    environ: Mapping[str, str],
    installation_key_path: Path | None,
    cwd: Path,
    stdout: TextIO,
    browser_open: Callable[[str], object] = webbrowser.open,
) -> None:
    """Launch one loopback dashboard and keep its credential in one handoff."""
    selected_database = configured_database_path(environ, database_path)
    actual_database = (
        default_database_path() if selected_database is None else selected_database
    )
    key_path = configured_installation_key_path(environ, installation_key_path)
    key = Pseudonymizer.installation_key(key_path).key
    now = lambda: datetime.now(timezone.utc)
    store_provider = ValidatedStoreProvider(
        lambda: HydraStore.open_current(selected_database),
    )
    store_factory = store_provider.open
    bootstrap_connection, database_state = _bootstrap_database(actual_database)
    try:
        launch_project = resolve_project(cwd)
    except Exception:
        launch_project = None
    doctor = _bootstrap_doctor(
        cwd=cwd, database_path=actual_database, database_state=database_state,
    )
    query = DashboardQueryService(store_factory, key, now, doctor)
    initial_snapshots: dict[str, DashboardSnapshot] = {}
    queried_fallback: DashboardSnapshot | None = None
    if bootstrap_connection is not None and database_state == "current":
        try:
            initial_snapshots, queried_fallback = (
                query.bootstrap_snapshots_from_connection(
                    bootstrap_connection, refresh=_idle_refresh(),
                )
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            initial_snapshots = {}
            queried_fallback = None
            database_state = "unavailable"
        finally:
            bootstrap_connection.close()
    elif bootstrap_connection is not None:
        bootstrap_connection.close()
    if database_state == "unavailable":
        doctor = _bootstrap_doctor(
            cwd=cwd, database_path=actual_database, database_state=database_state,
        )
        query = DashboardQueryService(store_factory, key, now, doctor)
    fallback_snapshot = queried_fallback or _unavailable_snapshot(
        generated_at=public_timestamp(now()), doctor=doctor,
    )
    cache = DashboardSnapshotCache(initial_snapshots)
    controller = RefreshController(cache, _DisabledRefreshRunner(store_factory), clock=now)
    sync_controller = DashboardSyncController(
        store_factory=store_factory, roots=_sync_roots(environ),
        installation_key=key, clock=now, auto_activate=False,
    )
    token = secrets.token_urlsafe(32)
    application = DashboardApplication(
        token=token,
        query_service=query,
        refresh_controller=controller,
        sync_controller=sync_controller,
        snapshot_cache=cache,
        assets=load_dashboard_assets(),
        fallback_snapshot=fallback_snapshot,
        fallback_error=(
            "storage_unavailable" if database_state == "unavailable" else None
        ),
    )
    server = None
    try:
        server = create_dashboard_server(port=port, application=application)
        if database_state == "current" and launch_project is not None:
            _observe_launch_project(
                store_factory=store_factory,
                query=query,
                cache=cache,
                resolution=launch_project,
                observed_at=public_timestamp(now()),
            )
        host, actual_port = server.server_address
        authority = f"http://{host}:{actual_port}/"
        handoff = f"{authority}#token={token}"
        if no_open:
            stdout.write(handoff + "\n")
        else:
            browser_open(handoff)
            stdout.write(f"Hydra dashboard: {authority}\n")
        stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        sync_controller.close()
        controller.close()
        if server is not None:
            server.server_close()
