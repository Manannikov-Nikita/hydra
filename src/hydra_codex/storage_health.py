"""Privacy-safe storage health, audit baselines, and explicit maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .audit_builder import StorageHealthSnapshot
from .storage import HydraStore, StorageUnavailable


STORAGE_STATUS_SCHEMA = "hydra.storage-status/v1"
STORAGE_COMPACT_SCHEMA = "hydra.storage-compact/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GROWTH_FACTS = (
    "database_bytes", "wal_bytes", "rollout_sources", "rollout_events",
    "codex_event_sources", "codex_events",
)


def current_storage_health(
    store: HydraStore,
    project_id: str,
) -> StorageHealthSnapshot:
    """Read exact current sizes and project-scoped counts without maintenance."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be non-empty")
    database_path = store.database_path
    wal_path = Path(str(database_path) + "-wal")

    def count(query: str) -> int:
        return int(store.connection.execute(query, (project_id,)).fetchone()[0])

    return StorageHealthSnapshot(
        database_bytes=database_path.stat().st_size,
        wal_bytes=wal_path.stat().st_size if wal_path.is_file() else 0,
        rollout_sources=count(
            """SELECT COUNT(DISTINCT r.source_digest)
                 FROM rollout_sources r
                 JOIN rollout_logical_sources l
                   ON l.logical_source_key=r.logical_source_key
                WHERE l.project_id=?"""
        ),
        rollout_events=count(
            """SELECT COUNT(DISTINCT e.event_key)
                 FROM rollout_events e
                 JOIN rollout_logical_sources l
                   ON l.logical_source_key=e.logical_source_key
                WHERE l.project_id=?"""
        ),
        codex_event_sources=count(
            "SELECT COUNT(*) FROM codex_event_sources WHERE project_id=?"
        ),
        codex_events=count(
            "SELECT COUNT(*) FROM codex_events WHERE project_id=?"
        ),
        schema_version=store.schema_version(),
    )


def _observed_at(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds",
    ).replace("+00:00", "Z")


def record_audit_snapshot(
    store: HydraStore,
    *,
    project_id: str,
    audit_sha256: str,
    observed_at: datetime,
    health: StorageHealthSnapshot,
) -> bool:
    """Append one audit baseline, deduplicating the same public audit bytes."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be non-empty")
    if not isinstance(audit_sha256, str) or _SHA256.fullmatch(audit_sha256) is None:
        raise ValueError("audit_sha256 must be lowercase SHA-256")
    snapshot_id = "hstorage_v1_" + hashlib.sha256(
        f"{project_id}\0{audit_sha256}".encode("utf-8")
    ).hexdigest()
    before = store.connection.total_changes
    with store.rollout_transaction() as connection:
        connection.execute(
            """INSERT INTO storage_audit_snapshots(
                   snapshot_id,project_id,observed_at,audit_sha256,
                   database_bytes,wal_bytes,rollout_sources,rollout_events,
                   codex_event_sources,codex_events,schema_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id,audit_sha256) DO NOTHING""",
            (
                snapshot_id, project_id, _observed_at(observed_at), audit_sha256,
                health.database_bytes, health.wal_bytes, health.rollout_sources,
                health.rollout_events, health.codex_event_sources,
                health.codex_events, health.schema_version,
            ),
        )
    return store.connection.total_changes > before


def _health_dict(health: StorageHealthSnapshot) -> dict[str, int]:
    return {
        "database_bytes": health.database_bytes,
        "wal_bytes": health.wal_bytes,
        "rollout_sources": health.rollout_sources,
        "rollout_events": health.rollout_events,
        "codex_event_sources": health.codex_event_sources,
        "codex_events": health.codex_events,
        "schema_version": health.schema_version,
    }


@dataclass(frozen=True)
class StorageStatus:
    current: StorageHealthSnapshot
    baseline: StorageHealthSnapshot | None

    def as_dict(self) -> dict[str, object]:
        current = _health_dict(self.current)
        baseline = None if self.baseline is None else _health_dict(self.baseline)
        growth = None if baseline is None else {
            fact: current[fact] - baseline[fact] for fact in _GROWTH_FACTS
        }
        return {
            "schema_version": STORAGE_STATUS_SCHEMA,
            "baseline_state": "unavailable" if baseline is None else "available",
            "current": current,
            "baseline": baseline,
            "growth": growth,
            "diagnostics": (
                [{"code": "growth_baseline_unavailable", "severity": "info"}]
                if baseline is None else []
            ),
        }


def storage_status(store: HydraStore, project_id: str) -> StorageStatus:
    current = current_storage_health(store, project_id)
    row = store.connection.execute(
        """SELECT database_bytes,wal_bytes,rollout_sources,rollout_events,
                  codex_event_sources,codex_events,schema_version
             FROM storage_audit_snapshots
            WHERE project_id=?
            ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    baseline = None if row is None else StorageHealthSnapshot(
        database_bytes=int(row[0]), wal_bytes=int(row[1]),
        rollout_sources=int(row[2]), rollout_events=int(row[3]),
        codex_event_sources=int(row[4]), codex_events=int(row[5]),
        schema_version=int(row[6]),
    )
    return StorageStatus(current=current, baseline=baseline)


def render_storage_status(status: StorageStatus, output_format: str) -> str:
    payload = status.as_dict()
    if output_format == "json":
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if output_format != "markdown":
        raise ValueError("unsupported storage status format")
    lines = ["# Hydra storage status", "", f"Baseline: **{payload['baseline_state']}**", ""]
    current = payload["current"]
    assert isinstance(current, dict)
    lines.extend(("| Fact | Current |", "| --- | ---: |"))
    lines.extend(f"| `{fact}` | {current[fact]} |" for fact in current)
    if payload["baseline"] is None:
        lines.extend(("", "Growth baseline unavailable (`growth_baseline_unavailable`)."))
    else:
        growth = payload["growth"]
        assert isinstance(growth, dict)
        lines.extend(("", "## Growth since latest audit", "", "| Fact | Delta |", "| --- | ---: |"))
        lines.extend(f"| `{fact}` | {growth[fact]:+d} |" for fact in growth)
    return "\n".join(lines) + "\n"


def _table_facts(store: HydraStore) -> dict[str, tuple[int, str]]:
    names = tuple(
        str(row[0])
        for row in store.connection.execute(
            """SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name"""
        ).fetchall()
    )
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names):
        raise StorageUnavailable("storage schema contains an unsafe table identifier")
    facts: dict[str, tuple[int, str]] = {}
    for name in names:
        columns = tuple(
            (str(row[1]), int(row[5]))
            for row in store.connection.execute(f'PRAGMA table_info("{name}")')
        )
        primary_key = tuple(
            column for column, ordinal in sorted(
                ((column, ordinal) for column, ordinal in columns if ordinal),
                key=lambda item: item[1],
            )
        )
        order_columns = primary_key or tuple(column for column, _ordinal in columns)
        order_by = ",".join(f'"{column}"' for column in order_columns)
        digest = hashlib.sha256()
        count = 0
        for row in store.connection.execute(
            f'SELECT * FROM "{name}" ORDER BY {order_by}'
        ):
            count += 1
            for value in tuple(row):
                if value is None:
                    encoded = b"n"
                elif isinstance(value, bytes):
                    encoded = b"b" + value
                else:
                    encoded = b"t" + str(value).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        facts[name] = (count, digest.hexdigest())
    return facts


@dataclass(frozen=True)
class StorageCompaction:
    rows_preserved: bool
    table_count: int
    total_rows: int
    pilot_receipts: int
    audit_snapshots: int
    evidence_rows: int
    database_bytes_before: int
    database_bytes_after: int
    wal_bytes_before: int
    wal_bytes_after: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": STORAGE_COMPACT_SCHEMA,
            "status": "complete",
            "rows_preserved": self.rows_preserved,
            "table_count": self.table_count,
            "total_rows": self.total_rows,
            "pilot_receipts": self.pilot_receipts,
            "audit_snapshots": self.audit_snapshots,
            "evidence_rows": self.evidence_rows,
            "database_bytes": {
                "before": self.database_bytes_before,
                "after": self.database_bytes_after,
            },
            "wal_bytes": {
                "before": self.wal_bytes_before,
                "after": self.wal_bytes_after,
            },
        }


def _file_sizes(store: HydraStore) -> tuple[int, int]:
    wal = Path(str(store.database_path) + "-wal")
    return (
        store.database_path.stat().st_size,
        wal.stat().st_size if wal.is_file() else 0,
    )


def compact_storage(store: HydraStore) -> StorageCompaction:
    """Checkpoint and VACUUM while proving every user-table row is retained."""
    store.connection.commit()
    before = _table_facts(store)
    database_before, wal_before = _file_sizes(store)
    checkpoint = store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint is None or int(checkpoint[0]) != 0:
        raise StorageUnavailable("storage checkpoint is busy")
    store.connection.execute("VACUUM")
    checkpoint = store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint is None or int(checkpoint[0]) != 0:
        raise StorageUnavailable("storage checkpoint is busy")
    after = _table_facts(store)
    if before != after:
        raise StorageUnavailable("storage maintenance changed retained rows")
    database_after, wal_after = _file_sizes(store)
    evidence_tables = {
        "annotations", "codex_events", "file_observations", "rollout_events",
        "rollout_test_runs", "token_snapshots", "tool_spans",
    }
    return StorageCompaction(
        rows_preserved=True,
        table_count=len(after),
        total_rows=sum(count for count, _digest in after.values()),
        pilot_receipts=after.get("pilot_receipts", (0, ""))[0],
        audit_snapshots=after.get("storage_audit_snapshots", (0, ""))[0],
        evidence_rows=sum(
            after.get(table, (0, ""))[0] for table in evidence_tables
        ),
        database_bytes_before=database_before,
        database_bytes_after=database_after,
        wal_bytes_before=wal_before,
        wal_bytes_after=wal_after,
    )


def render_storage_compaction(result: StorageCompaction, output_format: str) -> str:
    payload = result.as_dict()
    if output_format == "json":
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if output_format != "markdown":
        raise ValueError("unsupported storage compaction format")
    return (
        "# Hydra storage compaction\n\n"
        "Status: **complete**\n\n"
        f"- Rows preserved: `{str(result.rows_preserved).lower()}`\n"
        f"- Tables checked: `{result.table_count}`\n"
        f"- Total rows: `{result.total_rows}`\n"
        f"- Pilot receipts: `{result.pilot_receipts}`\n"
        f"- Audit snapshots: `{result.audit_snapshots}`\n"
        f"- Evidence rows: `{result.evidence_rows}`\n"
    )
