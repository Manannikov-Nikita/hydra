"""Private trusted-source planner for one global dashboard refresh."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3

from .project import ProjectNotFound, ProjectResolution, resolve_project
from .rollout import UnchangedLocationAttribution, unchanged_location_attribution
from .rollout_identity import (
    Pseudonymizer,
    RolloutRoot,
    TrustedRolloutCandidate,
    discover_trusted_rollouts,
    revalidate_trusted_rollout,
)
from .rollout_sources import (
    SourceChanged, SourceScan, SourceStat, open_source, scan_source,
)
from .storage import HydraStore, StorageUnavailable


@dataclass(frozen=True)
class AttributedRollout:
    candidate: TrustedRolloutCandidate = field(repr=False)
    scan: SourceScan = field(repr=False)
    resolution: ProjectResolution = field(repr=False)


@dataclass(frozen=True)
class CachedRollout:
    project_id: str = field(repr=False)
    logical: str = field(repr=False)
    revision: str = field(repr=False)
    location: str = field(repr=False)
    label: str
    candidate: TrustedRolloutCandidate = field(repr=False)
    source_stat: SourceStat = field(repr=False)


@contextmanager
def guard_cached_rollout(item: CachedRollout) -> Iterator[None]:
    """Hold one exact trusted source descriptor across its cached DB update."""
    if revalidate_trusted_rollout(item.candidate) != item.source_stat:
        raise SourceChanged("rollout source changed during refresh")
    with open_source(item.candidate.path, item.source_stat):
        if revalidate_trusted_rollout(item.candidate) != item.source_stat:
            raise SourceChanged("rollout source changed during refresh")
        yield
        if revalidate_trusted_rollout(item.candidate) != item.source_stat:
            raise SourceChanged("rollout source changed during refresh")


def refresh_cached_location(store: HydraStore, item: CachedRollout) -> None:
    """Refresh one exact cached location label inside its project transaction."""
    cursor = store.connection.execute(
        """UPDATE rollout_source_locations SET location_type=?
              WHERE logical_source_key=? AND location_key=? AND revision_digest=?
                AND EXISTS (SELECT 1 FROM rollout_logical_sources AS logical
                 WHERE logical.logical_source_key=? AND logical.project_id=?)""",
        (
            item.label, item.logical, item.location, item.revision,
            item.logical, item.project_id,
        ),
    )
    if cursor.rowcount != 1:
        raise SourceChanged("rollout source changed during refresh")


@dataclass(frozen=True)
class WorktreePartition:
    project_root: Path = field(repr=False)
    resolution: ProjectResolution = field(repr=False)
    sources: tuple[AttributedRollout, ...] = field(repr=False)


@dataclass(frozen=True)
class ProjectPartition:
    project_id: str = field(repr=False)
    worktrees: tuple[WorktreePartition, ...] = field(repr=False)
    cached: tuple[CachedRollout, ...] = field(default=(), repr=False)

    @property
    def cached_count(self) -> int:
        return len(self.cached)


@dataclass(frozen=True)
class GlobalRolloutPlan:
    partitions: tuple[ProjectPartition, ...] = field(repr=False)
    discovered_count: int
    inspected_count: int
    scanned_count: int
    diagnostic_codes: tuple[str, ...]

    @property
    def project_count(self) -> int:
        return len(self.partitions)

    @property
    def source_count(self) -> int:
        return sum(
            len(worktree.sources)
            for project in self.partitions for worktree in project.worktrees
        ) + sum(project.cached_count for project in self.partitions)

    def as_dict(self) -> dict[str, object]:
        return {
            "project_count": self.project_count,
            "source_count": self.source_count,
            "sources_discovered": self.discovered_count,
            "sources_inspected": self.inspected_count,
            "sources_scanned": self.scanned_count,
            "diagnostic_codes": list(self.diagnostic_codes),
        }


def trusted_rollout_roots(environ: Mapping[str, str]) -> tuple[RolloutRoot, ...]:
    value = environ.get("HOME")
    home = Path(value).expanduser() if isinstance(value, str) and value else Path.home()
    candidates = (
        RolloutRoot(home / ".codex" / "sessions", "active"),
        RolloutRoot(home / ".codex" / "archived_sessions", "archived"),
    )
    return tuple(item for item in candidates if Path(item.path).is_dir())


def _diagnostic(error: Exception) -> str:
    if isinstance(error, StorageUnavailable):
        return "storage_unavailable"
    if isinstance(error, SourceChanged):
        return "source_changed"
    if isinstance(error, sqlite3.OperationalError):
        code = getattr(error, "sqlite_errorcode", None)
        if isinstance(code, int) and code & 0xFF in {
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
        }:
            return "database_busy"
        return "internal_failure"
    if isinstance(error, (ProjectNotFound, ValueError, OSError)):
        return "project_root_unavailable"
    return "internal_failure"


def _cached(
    binding: UnchangedLocationAttribution,
    location: str,
    label: str,
    candidate: TrustedRolloutCandidate,
    source_stat: SourceStat,
) -> CachedRollout:
    return CachedRollout(
        binding.project_id, binding.logical, binding.revision, location, label,
        candidate, source_stat,
    )


def plan_global_rollout_ingest(
    store: HydraStore,
    roots: Iterable[RolloutRoot],
    installation_key: bytes,
    progress: Callable[[str, int, int], None] | None = None,
    *,
    discover: Callable[[Iterable[RolloutRoot]], tuple[TrustedRolloutCandidate, ...]] = discover_trusted_rollouts,
    scanner: Callable[[Path, bytes, Callable[[str, str], str]], SourceScan] = scan_source,
    revalidate: Callable[[TrustedRolloutCandidate], SourceStat] = revalidate_trusted_rollout,
    resolver: Callable[[Path | str], ProjectResolution] = resolve_project,
) -> GlobalRolloutPlan:
    """Discover once and build path-private prepared project partitions."""
    if not isinstance(installation_key, bytes) or len(installation_key) != 32:
        raise ValueError("installation_key must contain exactly 32 bytes")
    candidates = tuple(sorted(discover(tuple(roots)), key=lambda item: str(item.path)))
    if progress is not None:
        progress("discover", len(candidates), len(candidates))
    keys = Pseudonymizer(installation_key)
    grouped: dict[tuple[str, Path], list[AttributedRollout]] = {}
    cached: dict[str, list[CachedRollout]] = {}
    diagnostics: set[str] = set()
    inspected = scanned = 0
    for candidate in candidates:
        inspected += 1
        if progress is not None:
            progress("inspect", inspected, len(candidates))
        try:
            before = revalidate(candidate)
            location = keys.digest("source", str(candidate.path))
            unchanged = unchanged_location_attribution(
                store.connection, location, before,
            )
            if unchanged is not None:
                if revalidate(candidate) != before:
                    raise SourceChanged("rollout source changed during ingest")
                cached.setdefault(unchanged.project_id, []).append(
                    _cached(unchanged, location, candidate.label, candidate, before),
                )
                continue
            scan = scanner(candidate.path, installation_key, keys.digest)
            scanned += 1
            if progress is not None:
                progress("scan", scanned, len(candidates))
            if scan.source_stat != before or scan.path != candidate.path:
                raise SourceChanged("rollout source changed during ingest")
            if scan.cwd is None or not Path(scan.cwd).is_absolute():
                raise ProjectNotFound("rollout source has no absolute project cwd")
            resolution = resolver(scan.cwd)
            grouped.setdefault(
                (resolution.project_id, resolution.project_root), [],
            ).append(AttributedRollout(candidate, scan, resolution))
        except Exception as error:
            diagnostics.add(_diagnostic(error))
    worktrees: dict[str, list[WorktreePartition]] = {}
    for (project_id, project_root), sources in sorted(
        grouped.items(), key=lambda item: (item[0][0], str(item[0][1])),
    ):
        ordered = tuple(sorted(sources, key=lambda item: str(item.candidate.path)))
        worktrees.setdefault(project_id, []).append(
            WorktreePartition(project_root, ordered[0].resolution, ordered),
        )
    project_ids = sorted(set(worktrees) | set(cached))
    partitions = tuple(
        ProjectPartition(
            project_id,
            tuple(worktrees.get(project_id, ())),
            tuple(sorted(cached.get(project_id, ()), key=lambda item: item.location)),
        )
        for project_id in project_ids
    )
    return GlobalRolloutPlan(
        partitions, len(candidates), inspected, scanned, tuple(sorted(diagnostics)),
    )
