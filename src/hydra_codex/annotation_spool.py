"""Atomic private transport for model-supplied semantic annotations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
from .sync_state import SyncStateRepository


_REQUEST_PREFIX = "hreq_v1_"
_REQUEST_PATTERN = re.compile(r"^hreq_v1_[A-Za-z0-9_-]{32}$")
_QUARANTINE_CATEGORIES = frozenset({
    "malformed", "expired", "duplicate", "out_of_order", "wrong_capability",
})
_CLAIM_VERSION = "hydra.annotation-quarantine-claim/v1"


@dataclass(frozen=True)
class FileObservation:
    staged_at_ns: int
    staged_at: str
    staged_order: str
    identity: tuple[int, int]
    file_key: str


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


def _file_observation(path: Path, keys: Pseudonymizer) -> FileObservation:
    details = path.lstat()
    staged_ns = int(details.st_mtime_ns)
    staged = datetime.fromtimestamp(
        staged_ns / 1_000_000_000, tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    identity = (int(details.st_dev), int(details.st_ino))
    identity_text = f"{identity[0]}/{identity[1]}"
    file_key = "hspool_v1_" + keys.digest("event", f"spool-file/{identity_text}")
    order_key = "horder_v1_" + keys.digest("event", f"spool-order/{identity_text}")
    return FileObservation(
        staged_at_ns=staged_ns,
        staged_at=staged,
        staged_order=f"{staged_ns:020d}:{order_key}",
        identity=identity,
        file_key=file_key,
    )


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
    file_key: str | None = None,
) -> None:
    discriminator = (
        request_digest
        if disposition == "accepted"
        else file_key or request_digest or keys.digest(
            "diagnostic", f"transport-file/{turn_key}/{staged_order}",
        )
    )
    transport_key = "htransport_v1_" + keys.digest(
        "event", f"{turn_key}/{disposition}/{category or 'accepted'}/{discriminator}",
    )
    with store.rollout_transaction() as connection:
        inserted = connection.execute(
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
        ).rowcount
        if inserted:
            SyncStateRepository(store).mark_dirty_in_transaction(
                connection,
                project_id=project_id,
                root_key=project_id,
                root_kind="project",
                observed_at=received_at,
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


def _claim_tag(keys: Pseudonymizer, payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return "hclaim_v1_" + keys.digest("diagnostic", canonical)


def _claim_metadata(
    keys: Pseudonymizer,
    observation: FileObservation,
    *,
    project_id: str,
    session_key: str,
    turn_key: str,
    request_digest: str | None,
    category: str,
    received_at: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": _CLAIM_VERSION,
        "file_key": observation.file_key,
        "project_id": project_id,
        "session_key": session_key,
        "turn_key": turn_key,
        "request_digest": request_digest,
        "category": category,
        "staged_at": observation.staged_at,
        "staged_at_ns": observation.staged_at_ns,
        "staged_order": observation.staged_order,
        "received_at": received_at,
    }
    payload["tag"] = _claim_tag(keys, payload)
    return payload


def _write_claim_once(directory: Path, path: Path, payload: Mapping[str, object]) -> None:
    content = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".hydra-claim-", suffix=".tmp", dir=directory,
    )
    temporary = Path(temporary_name)
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
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
        _fsync_directory(directory)
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


def _read_claim(path: Path, keys: Pseudonymizer) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > 8192
        ):
            raise ValueError("invalid quarantine claim file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected_fields = {
        "version", "file_key", "project_id", "session_key", "turn_key",
        "request_digest", "category", "staged_at", "staged_at_ns",
        "staged_order", "received_at", "tag",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("invalid quarantine claim")
    unsigned = {key: item for key, item in value.items() if key != "tag"}
    tag = value["tag"]
    if not isinstance(tag, str) or not secrets.compare_digest(tag, _claim_tag(keys, unsigned)):
        raise ValueError("invalid quarantine claim")
    if (
        value["version"] != _CLAIM_VERSION
        or not isinstance(value["file_key"], str)
        or re.fullmatch(r"hspool_v1_[0-9a-f]{64}", value["file_key"]) is None
        or not isinstance(value["category"], str)
        or value["category"] not in _QUARANTINE_CATEGORIES
        or not all(
            isinstance(value[field], str) and bool(value[field])
            for field in ("project_id", "session_key", "turn_key")
        )
        or (
            value["request_digest"] is not None
            and (
                not isinstance(value["request_digest"], str)
                or re.fullmatch(r"hreq_v1_[0-9a-f]{64}", value["request_digest"]) is None
            )
        )
        or isinstance(value["staged_at_ns"], bool)
        or not isinstance(value["staged_at_ns"], int)
        or value["staged_at_ns"] < 0
        or not isinstance(value["staged_order"], str)
        or re.fullmatch(
            r"[0-9]{20}:horder_v1_[0-9a-f]{64}", value["staged_order"],
        ) is None
        or not isinstance(value["staged_at"], str)
        or not isinstance(value["received_at"], str)
    ):
        raise ValueError("invalid quarantine claim")
    timestamp(str(value["staged_at"]))
    timestamp(str(value["received_at"]))
    return value


def _quarantine(
    path: Path,
    spool: Path,
    category: str,
    observation: FileObservation,
    *,
    keys: Pseudonymizer,
    project_id: str,
    session_key: str,
    turn_key: str,
    request_digest: str | None,
    received_at: str,
) -> dict[str, object]:
    directory = _quarantine_directory(spool)
    proposed = _claim_metadata(
        keys,
        observation,
        project_id=project_id,
        session_key=session_key,
        turn_key=turn_key,
        request_digest=request_digest,
        category=category,
        received_at=received_at,
    )
    claim_path = directory / f"{observation.file_key}.claim"
    _write_claim_once(directory, claim_path, proposed)
    claim = _read_claim(claim_path, keys)
    if claim["file_key"] != observation.file_key:
        raise OSError("quarantine claim identity is inconsistent")
    _require_identity(path, observation.identity)
    target = directory / f"{claim['category']}-{observation.file_key}.json"
    if target.exists():
        raise OSError("quarantine claim target already exists")
    os.rename(path, target)
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
    return claim


def _record_quarantine_claim(
    store: HydraStore, keys: Pseudonymizer, claim: Mapping[str, object],
) -> None:
    _record_transport(
        store,
        keys,
        project_id=str(claim["project_id"]),
        session_key=str(claim["session_key"]),
        turn_key=str(claim["turn_key"]),
        request_digest=(
            None
            if claim["request_digest"] is None
            else str(claim["request_digest"])
        ),
        disposition="quarantined",
        category=str(claim["category"]),
        staged_at_ns=int(claim["staged_at_ns"]),
        staged_at=str(claim["staged_at"]),
        staged_order=str(claim["staged_order"]),
        received_at=str(claim["received_at"]),
        file_key=str(claim["file_key"]),
    )


def _quarantine_and_record(
    path: Path,
    spool: Path,
    store: HydraStore,
    keys: Pseudonymizer,
    category: str,
    observation: FileObservation,
    *,
    project_id: str,
    session_key: str,
    turn_key: str,
    request_digest: str | None,
    received_at: str,
) -> None:
    claim = _quarantine(
        path,
        spool,
        category,
        observation,
        keys=keys,
        project_id=project_id,
        session_key=session_key,
        turn_key=turn_key,
        request_digest=request_digest,
        received_at=received_at,
    )
    _record_quarantine_claim(store, keys, claim)


def _recover_quarantine(
    store: HydraStore, keys: Pseudonymizer, spool: Path,
) -> None:
    directory = _quarantine_directory(spool)
    for claim_path in sorted(directory.glob("hspool_v1_*.claim")):
        try:
            claim = _read_claim(claim_path, keys)
            target = directory / f"{claim['category']}-{claim['file_key']}.json"
            observation = _file_observation(target, keys)
            if observation.file_key != claim["file_key"]:
                continue
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        _record_quarantine_claim(store, keys, claim)


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
    _recover_quarantine(store, keys, spool)
    acknowledged = 0
    candidates: list[tuple[str, Path]] = []
    for path in spool.glob("*.json"):
        try:
            candidates.append((_file_observation(path, keys).staged_order, path))
        except FileNotFoundError:
            continue
    for _candidate_order, path in sorted(candidates):
        try:
            observation = _file_observation(path, keys)
        except FileNotFoundError:
            continue
        request_digest: str | None = None
        bound_session = expected_session
        bound_turn = expected_turn
        try:
            capability, nonce, payload = _read_envelope(path, observation.identity)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            _quarantine_and_record(
                path,
                spool,
                store,
                keys,
                "malformed",
                observation,
                project_id=project_id,
                session_key=expected_session,
                turn_key=expected_turn,
                request_digest=None,
                received_at=observed_at,
            )
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
                    _quarantine_and_record(
                        path,
                        spool,
                        store,
                        keys,
                        category,
                        observation,
                        project_id=project_id,
                        session_key=bound_session,
                        turn_key=bound_turn,
                        request_digest=request_digest,
                        received_at=observed_at,
                    )
                    continue
                latest_order = connection.execute(
                    """SELECT MAX(staged_order) FROM annotation_transport_events
                        WHERE turn_key=? AND disposition='accepted'""",
                    (bound_turn,),
                ).fetchone()[0]
                if (
                    known_request is None
                    and latest_order is not None
                    and observation.staged_order <= str(latest_order)
                ):
                    _quarantine_and_record(
                        path,
                        spool,
                        store,
                        keys,
                        "out_of_order",
                        observation,
                        project_id=project_id,
                        session_key=bound_session,
                        turn_key=bound_turn,
                        request_digest=request_digest,
                        received_at=observed_at,
                    )
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
                staged_at_ns=observation.staged_at_ns,
                staged_at=observation.staged_at,
                staged_order=observation.staged_order, received_at=observed_at,
                file_key=observation.file_key,
            )
            _acknowledge(path, observation.identity)
            acknowledged += 1
        except CapabilityExpired:
            _quarantine_and_record(
                path,
                spool,
                store,
                keys,
                "expired",
                observation,
                project_id=project_id,
                session_key=bound_session,
                turn_key=bound_turn,
                request_digest=request_digest,
                received_at=observed_at,
            )
        except AnnotationConflict as error:
            category = (
                "out_of_order" if "out of order" in str(error) else "duplicate"
            )
            _quarantine_and_record(
                path,
                spool,
                store,
                keys,
                category,
                observation,
                project_id=project_id,
                session_key=bound_session,
                turn_key=bound_turn,
                request_digest=request_digest,
                received_at=observed_at,
            )
        except CapabilityRejected:
            _quarantine_and_record(
                path,
                spool,
                store,
                keys,
                "wrong_capability",
                observation,
                project_id=project_id,
                session_key=bound_session,
                turn_key=bound_turn,
                request_digest=request_digest,
                received_at=observed_at,
            )
        except FileNotFoundError:
            continue
    if acknowledged:
        _fsync_directory(spool)
    return acknowledged
