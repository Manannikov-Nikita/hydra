"""Privacy-safe preparation and exact project grouping for dashboard events."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3

from .codex_event_ingest import CodexEventSource
from .codex_events import EventAdapterError
from .dashboard_refresh_plan import ProjectPartition
from .prepared_codex_events import (
    EventAttributionDiagnostic,
    PreparedCodexEventSource,
    PreparedEventAttribution,
    attribute_prepared_codex_event_source,
    prepare_codex_event_source,
)


@dataclass(frozen=True)
class PreparedProjectEventGroup:
    """Prepared sources sharing one exact trusted project-root identity."""

    project_id: str = field(repr=False)
    project_root: Path = field(repr=False)
    attributions: tuple[
        tuple[PreparedCodexEventSource, PreparedEventAttribution], ...
    ] = field(repr=False)

    @property
    def prepared_sources(self) -> tuple[PreparedCodexEventSource, ...]:
        return tuple(item[0] for item in self.attributions)


@dataclass(frozen=True)
class PreparedDashboardEvents:
    groups: tuple[PreparedProjectEventGroup, ...] = field(repr=False)
    diagnostic_codes: tuple[str, ...]

    def for_project(self, project_id: str) -> tuple[PreparedProjectEventGroup, ...]:
        return tuple(group for group in self.groups if group.project_id == project_id)


def current_worktree_roots(
    partitions: Iterable[ProjectPartition],
) -> Mapping[tuple[str, str], tuple[Path, ...]]:
    """Deduplicate exact rollout-derived roots for trusted event attribution."""
    roots: dict[tuple[str, str], set[Path]] = {}
    for partition in partitions:
        for worktree in partition.worktrees:
            resolutions = (
                tuple(item.resolution for item in worktree.sources)
                if worktree.sources else (worktree.resolution,)
            )
            for resolution in resolutions:
                key = (partition.project_id, str(resolution.worktree_path))
                roots.setdefault(key, set()).add(worktree.project_root)
    return {
        key: tuple(sorted(values, key=str))
        for key, values in sorted(roots.items())
    }


def prepare_dashboard_events(
    connection: sqlite3.Connection,
    sources: Iterable[object],
    partitions: Iterable[ProjectPartition],
    hash_key: bytes,
    *,
    progress: Callable[[bool], None],
    error_code: Callable[[Exception], str],
    preparer: Callable[..., PreparedCodexEventSource] = prepare_codex_event_source,
    attributor: Callable[..., object] = attribute_prepared_codex_event_source,
) -> PreparedDashboardEvents:
    """Read each configured stream once and group only exact trusted bindings."""
    roots = current_worktree_roots(partitions)
    diagnostics: set[str] = set()
    attributed: list[tuple[PreparedCodexEventSource, PreparedEventAttribution]] = []
    for source in sources:
        progress(False)
        if not isinstance(source, CodexEventSource):
            diagnostics.add("event_attribution_unavailable")
            continue
        try:
            prepared = preparer(source, hash_key=hash_key)
        except EventAdapterError:
            diagnostics.add("event_attribution_unavailable")
            continue
        except Exception as error:
            diagnostics.add(error_code(error))
            continue
        progress(True)
        try:
            attribution = attributor(connection, prepared, roots)
        except EventAdapterError:
            diagnostics.add("event_attribution_unavailable")
            continue
        except Exception as error:
            diagnostics.add(error_code(error))
            continue
        if isinstance(attribution, EventAttributionDiagnostic):
            diagnostics.add(attribution.code)
        elif isinstance(attribution, PreparedEventAttribution):
            attributed.append((prepared, attribution))
        else:
            diagnostics.add("internal_failure")

    grouped: dict[
        tuple[str, Path, object],
        list[tuple[PreparedCodexEventSource, PreparedEventAttribution]],
    ] = {}
    for prepared, attribution in attributed:
        key = (
            attribution.project_id,
            attribution.project_root,
            attribution.root_identity,
        )
        grouped.setdefault(key, []).append((prepared, attribution))
    groups = tuple(
        PreparedProjectEventGroup(
            project_id,
            project_root,
            tuple(sorted(items, key=lambda item: item[0].location_key)),
        )
        for (project_id, project_root, _identity), items in sorted(
            grouped.items(), key=lambda item: (item[0][0], str(item[0][1])),
        )
    )
    return PreparedDashboardEvents(groups, tuple(sorted(diagnostics)))
