"""Persist privacy-normalized calls found inside a custom-exec program."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .rollout_identity import opaque
from .test_evidence import TestEvidenceBuffer
from .tool_normalization import CUSTOM_EXEC_DIAGNOSTICS, normalize_custom_exec
from .tool_spans import persist_tool_start


def persist_custom_tool_call(
    connection: Any,
    *,
    payload: dict[str, Any],
    session_key: str,
    source_digest: str,
    source_ordinal: int,
    observed_at: str | None,
    current_turn: str | None,
    project_root: Path,
    test_evidence: TestEvidenceBuffer,
    diagnose: Callable[[str], None],
) -> int:
    call_id = payload.get("call_id")
    if not isinstance(call_id, str):
        return 0
    turn_key = opaque("turn", current_turn) if current_turn else None
    outer_key = opaque("call", call_id)
    persist_tool_start(
        connection, session_key=session_key, call_key=outer_key,
        category="opaque_exec", tool_name="custom_exec", started_at=observed_at,
        turn_key=turn_key, source_digest=source_digest, source_ordinal=source_ordinal,
    )
    program = payload.get("input")
    if payload.get("name") != "exec" or not isinstance(program, str):
        return 0
    normalized = normalize_custom_exec(program, project_root=project_root)
    diagnostic_count = 0
    for reason in normalized.diagnostics:
        if reason in CUSTOM_EXEC_DIAGNOSTICS:
            diagnose("custom_exec_" + reason)
            diagnostic_count += 1
    for index, call in enumerate(normalized.calls):
        nested_key = opaque("call", f"{call_id}:{index}:{call.safe_name}")
        persist_tool_start(
            connection, session_key=session_key, call_key=nested_key,
            category=call.category, tool_name=call.safe_name, started_at=observed_at,
            turn_key=turn_key, source_digest=source_digest, source_ordinal=source_ordinal,
            provenance="lower_bound",
        )
        # The outer broker exposes only one aggregate outcome.  It cannot prove
        # which nested invocation succeeded, so nested file operands remain
        # transient normalization data and never become deterministic facts.
        if call.ephemeral_command is not None:
            test_evidence.intent(
                logical_call_id=f"{call_id}:{index}", model_call_id=call_id,
                command=call.ephemeral_command, session_key=session_key,
                line_number=source_ordinal, observed_at=observed_at,
                turn_key=turn_key, tool_call_key=nested_key,
            )
    return diagnostic_count
