"""Atomic private transport for model-supplied semantic annotations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any

from .annotation_core import deliver_spooled_annotation
from .annotation_persistence import binding_for_capability
from .annotation_types import (
    AnnotationConflict,
    CAPABILITY_PATTERN,
    CapabilityExpired,
    CapabilityRejected,
    capability_digest,
    timestamp,
)
from .contracts import ModelAnnotationInput
from .rollout_identity import Pseudonymizer
from .storage import HydraStore


_REQUEST_PREFIX = "hreq_v1_"
_REQUEST_PATTERN = re.compile(r"^hreq_v1_[A-Za-z0-9_-]{32}$")


def spool_directory(environ: Mapping[str, str]) -> Path:
    """Return the private process-shared spool selected by ``TMPDIR``."""
    configured = environ.get("TMPDIR")
    base = (
        Path(configured).expanduser()
        if isinstance(configured, str) and configured
        else Path(tempfile.gettempdir())
    )
    hydra = base / "Hydra"
    spool = hydra / "spool"
    for directory in (hydra, spool):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError("annotation spool is unavailable")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("annotation spool is unavailable")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    return spool


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage_annotation(
    environ: Mapping[str, str], capability: str, payload: Mapping[str, Any],
) -> Path:
    """Stage only untrusted semantics, a capability, and an opaque nonce."""
    if not isinstance(capability, str) or CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise ValueError("invalid annotation capability")
    nonce = _REQUEST_PREFIX + secrets.token_urlsafe(24)
    envelope = {
        "capability": capability,
        "payload": dict(payload),
        "request_nonce": nonce,
    }
    content = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ) + "\n"
    spool = spool_directory(environ)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{nonce}-", suffix=".tmp", dir=spool,
    )
    temporary = Path(temporary_name)
    target = spool / f"{nonce}.json"
    descriptor_open = True
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor_open = False
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temporary, target)
        _fsync_directory(spool)
        return target
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_envelope(
    path: Path, expected_identity: tuple[int, int],
) -> tuple[str, str, dict[str, Any]]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if (
            (int(details.st_dev), int(details.st_ino)) != expected_identity
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > 65_536
        ):
            raise ValueError("invalid annotation envelope file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict) or set(value) != {
        "capability", "payload", "request_nonce",
    }:
        raise ValueError("invalid annotation envelope")
    capability = value["capability"]
    nonce = value["request_nonce"]
    payload = value["payload"]
    if (
        not isinstance(capability, str)
        or CAPABILITY_PATTERN.fullmatch(capability) is None
        or not isinstance(nonce, str)
        or _REQUEST_PATTERN.fullmatch(nonce) is None
        or not isinstance(payload, dict)
    ):
        raise ValueError("invalid annotation envelope")
    ModelAnnotationInput.from_mapping(payload)
    return capability, nonce, payload


def _file_observation(path: Path) -> tuple[int, str, str, tuple[int, int]]:
    details = path.lstat()
    staged_ns = int(details.st_mtime_ns)
    staged = datetime.fromtimestamp(
        staged_ns / 1_000_000_000, tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    order = f"{staged_ns:020d}:{path.name}"
    return staged_ns, staged, order, (int(details.st_dev), int(details.st_ino))


def _latency_ms(staged_at: str, received_at: str) -> int:
    delta = timestamp(received_at) - timestamp(staged_at)
    return max(0, int(delta.total_seconds() * 1000))


def _transport_request_digest(
    keys: Pseudonymizer, turn_key: str, request_nonce: str,
) -> str:
    return "hreq_v1_" + keys.digest("event", f"{turn_key}/{request_nonce}")


def _record_transport(
    store: HydraStore,
    keys: Pseudonymizer,
    *,
    project_id: str,
    session_key: str,
    turn_key: str,
    request_digest: str | None,
    disposition: str,
    category: str | None,
    staged_at_ns: int,
    staged_at: str,
    staged_order: str,
    received_at: str,
) -> None:
    discriminator = request_digest or keys.digest(
        "diagnostic", f"transport-file/{turn_key}/{staged_order}",
    )
    transport_key = "htransport_v1_" + keys.digest(
        "event", f"{turn_key}/{disposition}/{category or 'accepted'}/{discriminator}",
    )
    with store.rollout_transaction() as connection:
        connection.execute(
            """INSERT INTO annotation_transport_events(
                   transport_key,project_id,session_key,turn_key,request_digest,
                   disposition,diagnostic_category,staged_at,staged_at_ns,
                   staged_order,received_at,latency_ms,provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'derived')
               ON CONFLICT(transport_key) DO NOTHING""",
            (
                transport_key, project_id, session_key, turn_key, request_digest,
                disposition, category, staged_at, staged_at_ns, staged_order,
                received_at, _latency_ms(staged_at, received_at),
            ),
        )


def _quarantine_directory(spool: Path) -> Path:
    directory = spool / "quarantine"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("annotation quarantine is unavailable")
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("annotation quarantine is unavailable")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return directory


def _require_identity(path: Path, expected_identity: tuple[int, int]) -> None:
    details = path.lstat()
    if (int(details.st_dev), int(details.st_ino)) != expected_identity:
        raise OSError("annotation envelope path was replaced")


def _quarantine(
    path: Path, spool: Path, category: str, expected_identity: tuple[int, int],
) -> None:
    _require_identity(path, expected_identity)
    directory = _quarantine_directory(spool)
    target = directory / f"{category}-{secrets.token_urlsafe(18)}.json"
    os.replace(path, target)
    try:
        descriptor = os.open(
            target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        descriptor = -1
    if descriptor >= 0:
        try:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    _fsync_directory(directory)
    _fsync_directory(spool)


def _acknowledge(path: Path, expected_identity: tuple[int, int]) -> None:
    _require_identity(path, expected_identity)
    path.unlink()


def drain_annotations(
    environ: Mapping[str, str],
    store: HydraStore,
    keys: Pseudonymizer,
    *,
    project_id: str,
    session_id: str,
    turn_id: str,
    observed_at: str,
    allow_session_turns: bool = False,
) -> int:
    """Drain envelopes bound to one host-attested turn, leaving others alone."""
    spool = spool_directory(environ)
    expected_session = keys.digest("identity", session_id)
    expected_turn = keys.digest("turn", turn_id)
    acknowledged = 0
    candidates: list[tuple[str, Path]] = []
    for path in spool.glob("*.json"):
        try:
            candidates.append((_file_observation(path)[2], path))
        except FileNotFoundError:
            continue
    for _candidate_order, path in sorted(candidates):
        try:
            staged_ns, staged_at, staged_order, file_identity = _file_observation(path)
        except FileNotFoundError:
            continue
        request_digest: str | None = None
        bound_session = expected_session
        bound_turn = expected_turn
        try:
            capability, nonce, payload = _read_envelope(path, file_identity)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            _record_transport(
                store, keys, project_id=project_id, session_key=expected_session,
                turn_key=expected_turn, request_digest=None,
                disposition="quarantined", category="malformed",
                staged_at_ns=staged_ns, staged_at=staged_at,
                staged_order=staged_order, received_at=observed_at,
            )
            _quarantine(path, spool, "malformed", file_identity)
            continue
        try:
            with store.rollout_transaction() as connection:
                binding = binding_for_capability(
                    connection, capability_digest(keys, capability),
                )
                if (
                    binding["project_id"] != project_id
                    or binding["session_key"] != expected_session
                    or (
                        not allow_session_turns
                        and binding["turn_key"] != expected_turn
                    )
                ):
                    continue
                bound_session = str(binding["session_key"])
                bound_turn = str(binding["turn_key"])
                request_digest = _transport_request_digest(
                    keys, bound_turn, nonce,
                )
                known_request = connection.execute(
                    "SELECT 1 FROM annotation_receipts WHERE request_digest=?",
                    (request_digest,),
                ).fetchone()
                if path.name != f"{nonce}.json":
                    category = "duplicate" if known_request is not None else "malformed"
                    _record_transport(
                        store, keys, project_id=project_id,
                        session_key=bound_session, turn_key=bound_turn,
                        request_digest=request_digest,
                        disposition="quarantined", category=category,
                        staged_at_ns=staged_ns, staged_at=staged_at,
                        staged_order=staged_order, received_at=observed_at,
                    )
                    _quarantine(path, spool, category, file_identity)
                    continue
                latest_order = connection.execute(
                    """SELECT MAX(staged_order) FROM annotation_transport_events
                        WHERE turn_key=? AND disposition='accepted'""",
                    (bound_turn,),
                ).fetchone()[0]
                if (
                    known_request is None
                    and latest_order is not None
                    and staged_order <= str(latest_order)
                ):
                    _record_transport(
                        store, keys, project_id=project_id,
                        session_key=bound_session, turn_key=bound_turn,
                        request_digest=request_digest,
                        disposition="quarantined", category="out_of_order",
                        staged_at_ns=staged_ns, staged_at=staged_at,
                        staged_order=staged_order, received_at=observed_at,
                    )
                    _quarantine(path, spool, "out_of_order", file_identity)
                    continue
            deliver_spooled_annotation(
                store,
                keys,
                capability,
                request_key=nonce,
                observed_at=observed_at,
                payload=payload,
            )
            _record_transport(
                store, keys, project_id=project_id, session_key=bound_session,
                turn_key=bound_turn, request_digest=request_digest,
                disposition="accepted", category=None,
                staged_at_ns=staged_ns, staged_at=staged_at,
                staged_order=staged_order, received_at=observed_at,
            )
            _acknowledge(path, file_identity)
            acknowledged += 1
        except CapabilityExpired:
            _record_transport(
                store, keys, project_id=project_id, session_key=bound_session,
                turn_key=bound_turn, request_digest=request_digest,
                disposition="quarantined", category="expired",
                staged_at_ns=staged_ns, staged_at=staged_at,
                staged_order=staged_order, received_at=observed_at,
            )
            _quarantine(path, spool, "expired", file_identity)
        except AnnotationConflict as error:
            category = (
                "out_of_order" if "out of order" in str(error) else "duplicate"
            )
            _record_transport(
                store, keys, project_id=project_id, session_key=bound_session,
                turn_key=bound_turn, request_digest=request_digest,
                disposition="quarantined", category=category,
                staged_at_ns=staged_ns, staged_at=staged_at,
                staged_order=staged_order, received_at=observed_at,
            )
            _quarantine(path, spool, category, file_identity)
        except CapabilityRejected:
            _record_transport(
                store, keys, project_id=project_id, session_key=bound_session,
                turn_key=bound_turn, request_digest=request_digest,
                disposition="quarantined", category="wrong_capability",
                staged_at_ns=staged_ns, staged_at=staged_at,
                staged_order=staged_order, received_at=observed_at,
            )
            _quarantine(path, spool, "wrong_capability", file_identity)
        except FileNotFoundError:
            continue
    if acknowledged:
        _fsync_directory(spool)
    return acknowledged
