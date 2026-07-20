"""Opaque rollout identities and explicit-root discovery."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import tempfile
from typing import Iterable

from .rollout_privacy import LOCATION_TYPES


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
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
        path = directory / "rollout-hmac.key"
        if path.exists():
            key = path.read_bytes()
            if len(key) != 32:
                raise ValueError("invalid installation pseudonymization key")
            return cls(key)
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
            winner = path.read_bytes()
            if len(winner) != 32:
                raise ValueError("invalid installation pseudonymization key")
            return cls(winner)
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
