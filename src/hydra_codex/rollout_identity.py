"""Opaque rollout identities and explicit-root discovery."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Iterable

from .rollout_privacy import LOCATION_TYPES
from .rollout_sources import (
    SOURCE_CHANGED_MESSAGE,
    SourceChanged,
    SourceStat,
    source_stat,
)


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
class TrustedRolloutCandidate:
    path: Path = field(repr=False)
    label: str
    root: Path = field(repr=False)
    root_is_file: bool = field(repr=False)

    def __post_init__(self) -> None:
        if self.label not in {"active", "archived"}:
            raise ValueError("trusted rollout label must be active or archived")


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


def _trusted_root_specs(roots: Iterable[RolloutRoot]) -> tuple[RolloutRoot, ...]:
    specs = tuple(roots)
    if any(
        not isinstance(item, RolloutRoot) or item.label not in {"active", "archived"}
        for item in specs
    ):
        raise ValueError("trusted rollout roots must be labeled active or archived")
    return tuple(sorted(specs, key=lambda item: (item.label, str(Path(item.path)))))


def discover_trusted_rollouts(
    roots: Iterable[RolloutRoot],
) -> tuple[TrustedRolloutCandidate, ...]:
    """Discover canonical JSONL files without following any trusted-root symlink."""
    found: dict[Path, TrustedRolloutCandidate] = {}
    for item in _trusted_root_specs(roots):
        requested = Path(item.path)
        try:
            details = requested.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(details.st_mode):
            continue
        if stat.S_ISREG(details.st_mode):
            if requested.suffix != ".jsonl":
                continue
            canonical = requested.resolve(strict=True)
            found.setdefault(
                canonical,
                TrustedRolloutCandidate(canonical, item.label, canonical, True),
            )
            continue
        if not stat.S_ISDIR(details.st_mode):
            continue
        try:
            canonical_root = requested.resolve(strict=True)
        except OSError:
            continue
        for directory, names, files in os.walk(
            canonical_root, topdown=True, followlinks=False,
        ):
            current = Path(directory)
            safe_names: list[str] = []
            for name in sorted(names):
                try:
                    child = current / name
                    child_details = child.lstat()
                except OSError:
                    continue
                if stat.S_ISDIR(child_details.st_mode) and not stat.S_ISLNK(
                    child_details.st_mode
                ):
                    safe_names.append(name)
            names[:] = safe_names
            for name in sorted(files):
                if not name.endswith(".jsonl"):
                    continue
                candidate = current / name
                try:
                    candidate_details = candidate.lstat()
                    if not stat.S_ISREG(candidate_details.st_mode):
                        continue
                    canonical = candidate.resolve(strict=True)
                except OSError:
                    continue
                if not canonical.is_relative_to(canonical_root):
                    continue
                found.setdefault(
                    canonical,
                    TrustedRolloutCandidate(
                        canonical, item.label, canonical_root, False,
                    ),
                )
    return tuple(found[path] for path in sorted(found))


def revalidate_trusted_rollout(candidate: TrustedRolloutCandidate) -> SourceStat:
    """Fail closed if a discovered candidate or any trusted component changed."""
    if not isinstance(candidate, TrustedRolloutCandidate):
        raise SourceChanged(SOURCE_CHANGED_MESSAGE)
    try:
        root_details = candidate.root.lstat()
        if stat.S_ISLNK(root_details.st_mode):
            raise SourceChanged(SOURCE_CHANGED_MESSAGE)
        if candidate.root_is_file:
            if candidate.path != candidate.root or not stat.S_ISREG(root_details.st_mode):
                raise SourceChanged(SOURCE_CHANGED_MESSAGE)
        else:
            if not stat.S_ISDIR(root_details.st_mode):
                raise SourceChanged(SOURCE_CHANGED_MESSAGE)
            relative = candidate.path.relative_to(candidate.root)
            current = candidate.root
            for index, part in enumerate(relative.parts):
                current /= part
                details = current.lstat()
                if stat.S_ISLNK(details.st_mode):
                    raise SourceChanged(SOURCE_CHANGED_MESSAGE)
                final = index == len(relative.parts) - 1
                if final and not stat.S_ISREG(details.st_mode):
                    raise SourceChanged(SOURCE_CHANGED_MESSAGE)
                if not final and not stat.S_ISDIR(details.st_mode):
                    raise SourceChanged(SOURCE_CHANGED_MESSAGE)
            canonical = current.resolve(strict=True)
            if canonical != candidate.path or not canonical.is_relative_to(candidate.root):
                raise SourceChanged(SOURCE_CHANGED_MESSAGE)
        return source_stat(candidate.path)
    except SourceChanged:
        raise
    except (OSError, ValueError) as error:
        raise SourceChanged(SOURCE_CHANGED_MESSAGE) from error
