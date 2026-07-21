"""Opaque rollout identities and explicit-root discovery."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Iterable

from .rollout_privacy import LOCATION_TYPES


def _read_private_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("installation pseudonymization key must be a regular file")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise PermissionError("installation pseudonymization key has a different owner")
        if os.name == "posix" and stat.S_IMODE(details.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise PermissionError("installation pseudonymization key must be mode 0600")
        key = os.read(descriptor, 33)
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
            raise RuntimeError("installation pseudonymization key changed while opening")
        if len(key) != 32:
            raise ValueError("invalid installation pseudonymization key")
        return key
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class IngestReport:
    files_seen: int
    unique_sources: int
    diagnostics: int


@dataclass(frozen=True)
class RolloutRoot:
    path: Path | str
    label: str = "explicit"

    def __post_init__(self) -> None:
        if self.label not in LOCATION_TYPES:
            raise ValueError("rollout root label must be active, archived, or explicit")


@dataclass(frozen=True)
class Pseudonymizer:
    key: bytes

    @classmethod
    def installation(cls, directory: Path) -> "Pseudonymizer":
        return cls.installation_key(directory / "rollout-hmac.key")

    @classmethod
    def installation_key(cls, path: Path) -> "Pseudonymizer":
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
        if path.exists():
            return cls(_read_private_key(path))
        key = secrets.token_bytes(32)
        descriptor, temporary = tempfile.mkstemp(prefix=".rollout-key-", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            return cls(_read_private_key(path))
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def digest(self, domain: str, value: str) -> str:
        domains = {"identity", "conversation", "turn", "call", "path", "command", "source", "event", "diagnostic", "capability"}
        if domain not in domains:
            raise ValueError("unsupported pseudonymization domain")
        return hmac.new(
            self.key, f"hydra/{domain}/".encode("utf-8") + value.encode("utf-8"), hashlib.sha256,
        ).hexdigest()


ACTIVE_HASHER: ContextVar[Pseudonymizer | None] = ContextVar("hydra_rollout_hasher", default=None)


def opaque(domain: str, value: str) -> str:
    hasher = ACTIVE_HASHER.get()
    if hasher is None:
        raise RuntimeError("rollout pseudonymizer is required")
    return hasher.digest(domain, value)


def discover_rollouts(roots: Iterable[Path | str | RolloutRoot]) -> tuple[Path, ...]:
    found: set[Path] = set()
    for root in roots:
        path = Path(root.path if isinstance(root, RolloutRoot) else root)
        if path.is_file() and path.suffix == ".jsonl":
            found.add(path.resolve())
        elif path.is_dir():
            found.update(candidate.resolve() for candidate in path.rglob("*.jsonl") if candidate.is_file())
    return tuple(sorted(found))
