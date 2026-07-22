"""Fail-safe, privacy-safe local Hydra diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import stat

from .project import resolve_project
from .storage import MIGRATIONS, default_database_path


DOCTOR_SCHEMA = "hydra.doctor/v1"
_CHECK_CODES = (
    "project_resolution", "storage_available", "schema_current",
    "foreign_keys_ok", "integrity_ok", "storage_permissions_restricted",
)
_STATUSES = {"ok", "failed", "unavailable"}


@dataclass(frozen=True)
class DoctorCheck:
    code: str
    status: str

    def __post_init__(self) -> None:
        if self.code not in _CHECK_CODES or self.status not in _STATUSES:
            raise ValueError("invalid doctor check")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "status": self.status}


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    def __post_init__(self) -> None:
        if tuple(item.code for item in self.checks) != _CHECK_CODES:
            raise ValueError("doctor checks must be complete and ordered")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": DOCTOR_SCHEMA,
            "status": (
                "healthy"
                if all(item.status == "ok" for item in self.checks)
                else "degraded"
            ),
            "checks": [item.as_dict() for item in self.checks],
        }


def _checks(
    statuses: dict[str, str],
) -> DoctorReport:
    return DoctorReport(tuple(
        DoctorCheck(code, statuses.get(code, "unavailable"))
        for code in _CHECK_CODES
    ))


def _restricted_file(path: Path) -> bool:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode & 0o077 == 0


def _storage_permissions(database_path: Path) -> bool:
    candidates = tuple(
        path for path in (
            database_path,
            Path(str(database_path) + "-wal"),
            Path(str(database_path) + "-shm"),
        )
        if path.exists()
    )
    return bool(candidates) and all(_restricted_file(path) for path in candidates)


def run_doctor(*, cwd: Path, database_path: Path | None) -> DoctorReport:
    statuses: dict[str, str] = {}
    try:
        resolve_project(cwd)
    except Exception:
        statuses["project_resolution"] = "failed"
        return _checks(statuses)
    statuses["project_resolution"] = "ok"

    selected_path = default_database_path() if database_path is None else database_path
    if not selected_path.is_file():
        statuses["storage_available"] = "failed"
        return _checks(statuses)
    restricted = _storage_permissions(selected_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            selected_path.resolve().as_uri() + "?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only=ON")
    except Exception:
        if connection is not None:
            connection.close()
        statuses["storage_available"] = "failed"
        return _checks(statuses)
    try:
        statuses["storage_available"] = "ok"
        statuses["schema_current"] = (
            "ok"
            if int(connection.execute("PRAGMA user_version").fetchone()[0])
            == MIGRATIONS[-1][0]
            else "failed"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        statuses["foreign_keys_ok"] = (
            "ok"
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
            and connection.execute("PRAGMA foreign_key_check").fetchone() is None
            else "failed"
        )
        statuses["integrity_ok"] = (
            "ok"
            if connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            else "failed"
        )
        statuses["storage_permissions_restricted"] = (
            "ok" if restricted else "failed"
        )
    except Exception:
        for code in _CHECK_CODES[2:]:
            statuses.setdefault(code, "failed")
    finally:
        connection.close()
    return _checks(statuses)


def render_doctor(report: DoctorReport, output_format: str) -> str:
    payload = report.as_dict()
    if output_format == "json":
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if output_format != "markdown":
        raise ValueError("unsupported doctor format")
    lines = [
        "# Hydra doctor", "", f"Status: **{payload['status']}**", "",
        "| Check | Status |", "| --- | --- |",
    ]
    lines.extend(
        f"| `{item.code}` | `{item.status}` |" for item in report.checks
    )
    return "\n".join(lines) + "\n"
