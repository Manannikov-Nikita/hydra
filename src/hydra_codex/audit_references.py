"""Typed evidence-reference closure checks for ``hydra.audit/v1``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re


_EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{16}\Z")
_PHASE_REFS = ("working_tokens", "full_context_tokens", "reasoning_tokens")
_HEADLINE_REFS = ("working_tokens", "wall_clock_ms", "test_runs", "semantic_coverage")


def _mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    return value


def _sequence(value: object, location: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{location} must be a collection")
    return value


def _required(item: Mapping[str, object], name: str, location: str) -> object:
    if name not in item:
        raise ValueError(f"{location} is missing an evidence reference")
    return item[name]


def _add_reference(refs: set[str], value: object, location: str) -> None:
    if not isinstance(value, str) or _EVIDENCE_ID.fullmatch(value) is None:
        raise ValueError(f"{location} must be an evidence reference")
    refs.add(value)


def _add_reference_map(refs: set[str], value: object, location: str) -> None:
    supplied = _mapping(value, location)
    for name, reference in supplied.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{location} has an invalid reference name")
        _add_reference(refs, reference, f"{location}.{name}")


def _add_named_refs(
    refs: set[str],
    item: Mapping[str, object],
    names: tuple[str, ...],
    location: str,
) -> None:
    for name in names:
        _add_reference(refs, _required(item, name, location), f"{location}.{name}")


def _add_phase_refs(refs: set[str], value: object, location: str) -> None:
    for index, raw_phase in enumerate(_sequence(value, location)):
        phase = _mapping(raw_phase, f"{location}[{index}]")
        _add_named_refs(refs, phase, _PHASE_REFS, f"{location}[{index}]")


def validate_reference_closure(
    *,
    cohort: object,
    collection: object,
    storage_health: object,
    appendix_ids: tuple[str, ...],
) -> None:
    """Require every typed reference to resolve and every record to be used."""
    refs: set[str] = set()
    cohort_map = _mapping(cohort, "cohort")
    _add_named_refs(
        refs,
        _mapping(_required(cohort_map, "headline", "cohort"), "cohort.headline"),
        _HEADLINE_REFS,
        "cohort.headline",
    )
    _add_phase_refs(
        refs,
        _required(cohort_map, "phase_allocation", "cohort"),
        "cohort.phase_allocation",
    )
    transport = _mapping(
        _required(cohort_map, "transport", "cohort"),
        "cohort.transport",
    )
    drain = _mapping(
        _required(transport, "pending_annotation_drain", "cohort.transport"),
        "cohort.transport.pending_annotation_drain",
    )
    _add_reference(
        refs,
        _required(drain, "evidence_id", "cohort.transport.pending_annotation_drain"),
        "cohort.transport.pending_annotation_drain.evidence_id",
    )
    _add_reference_map(
        refs,
        _required(transport, "evidence_refs", "cohort.transport"),
        "cohort.transport.evidence_refs",
    )

    collection_map = _mapping(collection, "collection")
    for index, raw_item in enumerate(
        _sequence(_required(collection_map, "overview", "collection"), "collection.overview")
    ):
        item = _mapping(raw_item, f"collection.overview[{index}]")
        _add_named_refs(refs, item, _HEADLINE_REFS, f"collection.overview[{index}]")
    for index, raw_task in enumerate(
        _sequence(_required(collection_map, "tasks", "collection"), "collection.tasks")
    ):
        location = f"collection.tasks[{index}]"
        task = _mapping(raw_task, location)
        _add_named_refs(
            refs,
            _mapping(_required(task, "headline", location), f"{location}.headline"),
            _HEADLINE_REFS,
            f"{location}.headline",
        )
        _add_phase_refs(
            refs,
            _required(task, "phase_allocation", location),
            f"{location}.phase_allocation",
        )
        topology = _mapping(
            _required(task, "agent_topology", location),
            f"{location}.agent_topology",
        )
        _add_named_refs(
            refs, topology, ("sessions", "subagents"), f"{location}.agent_topology",
        )
        tools = _mapping(
            _required(task, "tool_file_test", location),
            f"{location}.tool_file_test",
        )
        for name, reference in tools.items():
            if name == "test_evidence":
                for row_index, raw_row in enumerate(
                    _sequence(reference, f"{location}.tool_file_test.test_evidence")
                ):
                    row_location = f"{location}.tool_file_test.test_evidence[{row_index}]"
                    row = _mapping(raw_row, row_location)
                    _add_reference(
                        refs,
                        _required(row, "count", row_location),
                        f"{row_location}.count",
                    )
            else:
                _add_reference(refs, reference, f"{location}.tool_file_test.{name}")
        _add_reference_map(
            refs,
            _required(task, "issues", location),
            f"{location}.issues",
        )
        comparison = _mapping(
            _required(task, "comparability", location),
            f"{location}.comparability",
        )
        _add_reference(
            refs,
            _required(comparison, "baseline_working_tokens", f"{location}.comparability"),
            f"{location}.comparability.baseline_working_tokens",
        )
        for marker_index, raw_marker in enumerate(
            _sequence(_required(task, "semantic_markers", location), f"{location}.semantic_markers")
        ):
            marker_location = f"{location}.semantic_markers[{marker_index}]"
            marker = _mapping(raw_marker, marker_location)
            _add_reference(
                refs,
                _required(marker, "confidence", marker_location),
                f"{marker_location}.confidence",
            )
        for name in ("pilot_evidence_refs", "evidence_refs"):
            _add_reference_map(
                refs,
                _required(task, name, location),
                f"{location}.{name}",
            )

    storage = _mapping(storage_health, "storage_health")
    _add_reference_map(
        refs,
        _required(storage, "evidence_refs", "storage_health"),
        "storage_health.evidence_refs",
    )

    appendix = set(appendix_ids)
    dangling = refs - appendix
    if dangling:
        raise ValueError("audit contains a dangling evidence reference")
    orphaned = appendix - refs
    if orphaned:
        raise ValueError("audit contains orphan evidence records")
