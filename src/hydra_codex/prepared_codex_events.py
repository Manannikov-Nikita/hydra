"""Single-pass, privacy-safe preparation and trusted attribution of Codex events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import stat
from typing import Mapping, TYPE_CHECKING

from .codex_events import (
    APP_SERVER_V2,
    OTEL_LOG_V1,
    CodexEventBatch,
    EventAdapterError,
    read_codex_event_stream,
)
from .rollout_identity import Pseudonymizer
from .rollout_sources import (
    SOURCE_CHANGED_MESSAGE,
    SourceChanged,
    SourceStat,
    open_source,
    source_stat,
)

if TYPE_CHECKING:
    from .codex_event_ingest import CodexEventSource


_ATTRIBUTION_CODES = frozenset({
    "event_attribution_unavailable",
    "event_attribution_ambiguous",
})
_KEY_BINDING_DOMAIN = b"hydra/prepared-codex-event-key/v1"
_PAYLOAD_SEAL_DOMAIN = b"hydra/prepared-codex-event-payload/v1\x00"
_EVENT_SCHEMAS = frozenset({APP_SERVER_V2, OTEL_LOG_V1})


def _validate_hash_key(hash_key: bytes) -> None:
    if not isinstance(hash_key, bytes) or len(hash_key) != 32:
        raise EventAdapterError("privacy key must be exactly 32 bytes")


def _key_binding(hash_key: bytes) -> str:
    _validate_hash_key(hash_key)
    return hmac.new(hash_key, _KEY_BINDING_DOMAIN, hashlib.sha256).hexdigest()


def _lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _batch_payload(batch: CodexEventBatch) -> dict[str, object]:
    events: list[dict[str, object]] = []
    for event in batch.events:
        value = asdict(event)
        if event.tool is not None:
            tool = value.get("tool")
            if not isinstance(tool, dict):
                raise EventAdapterError("prepared event source payload is invalid")
            tool["ephemeral"] = {
                "command": event.tool.ephemeral_command,
                "output": event.tool.ephemeral_output,
                "workdir": event.tool.ephemeral_workdir,
                "file_writes": list(event.tool.ephemeral_file_writes),
            }
        events.append(value)
    return {
        "schema": batch.schema,
        "events": events,
        "issues": [asdict(issue) for issue in batch.issues],
    }


def _payload_seal(
    hash_key: bytes,
    *,
    schema: str,
    line_count: int,
    byte_count: int,
    path: Path,
    source_details: SourceStat,
    raw_digest: str,
    location_key: str,
    key_binding: str,
    batch: CodexEventBatch,
    thread_keys: tuple[str, ...],
) -> str:
    payload = {
        "schema": schema,
        "line_count": line_count,
        "byte_count": byte_count,
        "canonical_path": str(path),
        "source_stat": asdict(source_details),
        "raw_digest": raw_digest,
        "location_key": location_key,
        "key_binding": key_binding,
        "batch": _batch_payload(batch),
        "thread_keys": list(thread_keys),
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EventAdapterError("prepared event source payload is invalid") from error
    return hmac.new(
        hash_key, _PAYLOAD_SEAL_DOMAIN + encoded, hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class PreparedCodexEventSource:
    """One exact event stream, normalized without retaining raw content."""

    schema: str
    line_count: int
    byte_count: int
    path: Path = field(repr=False)
    source_stat: SourceStat = field(repr=False)
    raw_digest: str = field(repr=False)
    location_key: str = field(repr=False)
    key_binding: str = field(repr=False)
    payload_seal: str = field(repr=False)
    batch: CodexEventBatch = field(repr=False)
    thread_keys: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if self.schema not in _EVENT_SCHEMAS:
            raise ValueError("unsupported event source schema")
        for value in (self.line_count, self.byte_count):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("event source counts must be non-negative integers")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("prepared event path must be absolute")
        for name, value in (
            ("raw event digest", self.raw_digest),
            ("event location key", self.location_key),
            ("event key binding", self.key_binding),
            ("event payload seal", self.payload_seal),
        ):
            if not _lower_sha256(value):
                raise ValueError(f"{name} must be lowercase sha256 hex")
        if self.batch.schema != self.schema:
            raise ValueError("prepared event schema must match its batch")
        expected_threads = tuple(sorted({
            thread_key
            for event in self.batch.events
            for thread_key in (
                event.thread_key,
                event.parent_thread_key,
                event.child_thread_key,
            )
            if thread_key is not None
        }))
        if self.thread_keys != expected_threads:
            raise ValueError("prepared thread keys must exactly match the event batch")


@dataclass(frozen=True)
class _ProjectRootIdentity:
    dev: int
    ino: int
    mode: int


@dataclass(frozen=True)
class PreparedEventAttribution:
    """One exact trusted project/worktree/root binding, hidden from repr."""

    project_id: str = field(repr=False)
    worktree_path: str = field(repr=False)
    project_root: Path = field(repr=False)
    root_identity: _ProjectRootIdentity = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise ValueError("attributed project id must be non-empty")
        if not isinstance(self.worktree_path, str) or not self.worktree_path:
            raise ValueError("attributed worktree path must be non-empty")
        if not isinstance(self.project_root, Path) or not self.project_root.is_absolute():
            raise ValueError("attributed project root must be an absolute path")
        if not isinstance(self.root_identity, _ProjectRootIdentity):
            raise ValueError("attributed project root identity is required")


@dataclass(frozen=True)
class EventAttributionDiagnostic:
    """Categorical public failure without source or project identifiers."""

    code: str

    def __post_init__(self) -> None:
        if self.code not in _ATTRIBUTION_CODES:
            raise ValueError("unsupported event attribution diagnostic")


def prepare_codex_event_source(
    source: CodexEventSource, *, hash_key: bytes,
) -> PreparedCodexEventSource:
    """Hash and parse one stat-bound physical stream exactly once."""
    binding = _key_binding(hash_key)
    hasher = Pseudonymizer(hash_key)
    requested = Path(source.path).expanduser().absolute()
    expected_stat = source_stat(requested)
    try:
        canonical = requested.resolve(strict=True)
    except OSError as error:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error
    if source_stat(requested) != expected_stat or source_stat(canonical) != expected_stat:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE)
    raw_digest = hashlib.sha256()
    line_count = 0
    byte_count = 0

    def measured_lines(handle):
        nonlocal line_count, byte_count
        for raw_line in handle:
            line_count += 1
            byte_count += len(raw_line)
            raw_digest.update(raw_line)
            yield raw_line

    with open_source(requested, expected_stat) as handle:
        batch = read_codex_event_stream(
            measured_lines(handle), schema=source.schema, privacy_key=hash_key,
        )
    digest = raw_digest.hexdigest()
    location = hasher.digest("path", str(canonical))
    thread_keys = tuple(sorted({
        thread_key
        for event in batch.events
        for thread_key in (
            event.thread_key,
            event.parent_thread_key,
            event.child_thread_key,
        )
        if thread_key is not None
    }))
    seal = _payload_seal(
        hash_key,
        schema=source.schema,
        line_count=line_count,
        byte_count=byte_count,
        path=canonical,
        source_details=expected_stat,
        raw_digest=digest,
        location_key=location,
        key_binding=binding,
        batch=batch,
        thread_keys=thread_keys,
    )
    return PreparedCodexEventSource(
        source.schema, line_count, byte_count, canonical, expected_stat,
        digest, location, binding, seal, batch, thread_keys,
    )


def validate_prepared_codex_event_sources_key(
    sources: tuple[PreparedCodexEventSource, ...], hash_key: bytes,
) -> None:
    """Bind prepared opaque identities to the exact key that produced them."""
    expected = _key_binding(hash_key)
    hasher = Pseudonymizer(hash_key)
    for source in sources:
        if not isinstance(source, PreparedCodexEventSource):
            raise TypeError("prepared sources must be PreparedCodexEventSource values")
        PreparedCodexEventSource.__post_init__(source)
        if not hmac.compare_digest(source.key_binding, expected):
            raise EventAdapterError("prepared event source key mismatch")
        expected_location = hasher.digest("path", str(source.path))
        if not hmac.compare_digest(source.location_key, expected_location):
            raise EventAdapterError("prepared event source location mismatch")
        expected_seal = _payload_seal(
            hash_key,
            schema=source.schema,
            line_count=source.line_count,
            byte_count=source.byte_count,
            path=source.path,
            source_details=source.source_stat,
            raw_digest=source.raw_digest,
            location_key=source.location_key,
            key_binding=source.key_binding,
            batch=source.batch,
            thread_keys=source.thread_keys,
        )
        if not hmac.compare_digest(source.payload_seal, expected_seal):
            raise EventAdapterError("prepared event source payload mismatch")


def _current_project_root(
    value: object,
) -> tuple[Path, _ProjectRootIdentity] | None:
    if not isinstance(value, Path) or not value.is_absolute():
        return None
    try:
        before = value.stat(follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            return None
        canonical = value.resolve(strict=True)
        after = value.stat(follow_symlinks=False)
    except OSError:
        return None
    identity = lambda details: _ProjectRootIdentity(
        int(details.st_dev), int(details.st_ino), int(details.st_mode),
    )
    if canonical != value or identity(before) != identity(after):
        return None
    return value, identity(before)


def revalidate_prepared_event_attribution(
    attribution: PreparedEventAttribution,
) -> None:
    """Reject a project root that no longer has the attributed identity."""
    current = _current_project_root(attribution.project_root)
    if current is None or current[1] != attribution.root_identity:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE)


def attribute_prepared_codex_event_source(
    connection: sqlite3.Connection,
    prepared: PreparedCodexEventSource,
    worktrees: Mapping[tuple[str, str], tuple[Path, ...]],
) -> PreparedEventAttribution | EventAttributionDiagnostic:
    """Bind every event thread through trusted stored session capabilities only."""
    thread_keys = prepared.thread_keys
    if not thread_keys:
        return EventAttributionDiagnostic("event_attribution_unavailable")
    placeholders = ",".join("?" for _key in thread_keys)
    rows = connection.execute(
        f"""SELECT DISTINCT
                   sessions.session_id,sessions.project_id,sessions.worktree_path
              FROM sessions
             WHERE sessions.session_id IN ({placeholders})
               AND EXISTS (
                   SELECT 1 FROM trusted_turn_bindings
                    WHERE trusted_turn_bindings.session_key=sessions.session_id
                      AND trusted_turn_bindings.project_id=sessions.project_id
               )""",
        thread_keys,
    ).fetchall()
    if {str(row[0]) for row in rows} != set(thread_keys):
        return EventAttributionDiagnostic("event_attribution_unavailable")
    bindings = {(str(row[1]), str(row[2])) for row in rows}
    if len(bindings) != 1:
        return EventAttributionDiagnostic("event_attribution_ambiguous")
    project_id, worktree_path = next(iter(bindings))
    configured_roots = worktrees.get((project_id, worktree_path), ())
    if not isinstance(configured_roots, tuple):
        return EventAttributionDiagnostic("event_attribution_unavailable")
    roots = tuple(_current_project_root(root) for root in configured_roots)
    if any(root is None for root in roots):
        return EventAttributionDiagnostic("event_attribution_unavailable")
    if not roots:
        return EventAttributionDiagnostic("event_attribution_unavailable")
    if len(roots) != 1:
        return EventAttributionDiagnostic("event_attribution_ambiguous")
    root = roots[0]
    assert root is not None
    project_root, root_identity = root
    return PreparedEventAttribution(
        project_id, worktree_path, project_root, root_identity,
    )
