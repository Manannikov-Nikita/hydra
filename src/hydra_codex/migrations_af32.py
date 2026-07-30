"""Fence durable sync jobs against writers opened before this schema."""

from __future__ import annotations


AF32_SYNC_JOB_WRITER_PROTOCOL = 53


def _writer_fence_sql(operation: str) -> str:
    action = operation.lower()
    return f"""CREATE TRIGGER sync_job_current_writer_{action}
BEFORE {operation} ON sync_jobs
WHEN hydra_sync_job_writer_protocol() IS NOT {AF32_SYNC_JOB_WRITER_PROTOCOL}
BEGIN
    SELECT RAISE(ABORT, 'current sync job writer is required');
END"""


AF32_REQUIRED_TRIGGER_SQL = {
    f"sync_job_current_writer_{operation.lower()}":
        _writer_fence_sql(operation)
    for operation in ("INSERT", "UPDATE", "DELETE")
}


AF32_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (53, tuple(AF32_REQUIRED_TRIGGER_SQL.values())),
)
