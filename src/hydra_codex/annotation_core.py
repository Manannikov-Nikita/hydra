"""Capability boundary for cooperative hook-attested semantic annotations.

"Trusted" database/type names are retained for schema compatibility. They mean
that identity was supplied by the configured hook path rather than by annotation
arguments; the local hook executable is not an authenticated security boundary.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Mapping

from .annotation_persistence import binding_for_capability, insert_annotation, stage_fact
from .annotation_types import (
    AnnotationConflict,
    AnnotationDisposition,
    AnnotationWrite,
    CapabilityExpired,
    CapabilityRejected,
    ConflictDecision,
    IssuedCapability,
    StopState,
    TrustedAnnotationContext,
    TrustedTurnContext,
    capability_digest,
    timestamp,
)
from .contracts import AnnotationKind, ModelAnnotationInput, Provenance
from .rollout_identity import Pseudonymizer
from .storage import HydraStore


def _annotation_payload(model: ModelAnnotationInput) -> dict[str, object]:
    return {
        "kind": model.kind.value,
        "phase": model.phase.value,
        "cause": model.cause.value,
        "scope_change": model.scope_change.value,
        "task_family": model.task_family,
        "confidence": model.confidence,
        "note": model.note,
        "outcome": None if model.outcome is None else model.outcome.value,
    }


def issue_capability(
    store: HydraStore,
    keys: Pseudonymizer,
    context: TrustedTurnContext,
    *,
    expires_at: str,
) -> IssuedCapability:
    created = timestamp(context.observed_at)
    expires = timestamp(expires_at)
    if expires <= created:
        raise ValueError("capability expiry must follow creation")
    token = "hcap_v1_" + secrets.token_urlsafe(32)
    digest = capability_digest(keys, token)
    session_key = keys.digest("identity", context.session_id)
    turn_key = keys.digest("turn", context.turn_id)

    with store.rollout_transaction() as connection:
        session = connection.execute(
            "SELECT project_id FROM sessions WHERE session_id=?", (session_key,)
        ).fetchone()
        if session is not None and session["project_id"] != context.project_id:
            raise CapabilityRejected("trusted session binding is inconsistent")
        connection.execute(
            """INSERT INTO sessions(session_id,project_id,worktree_path,started_at,provenance)
               VALUES (?,?,'.',?,'derived') ON CONFLICT(session_id) DO NOTHING""",
            (session_key, context.project_id, context.observed_at),
        )
        turn = connection.execute(
            "SELECT session_id FROM turns WHERE turn_id=?", (turn_key,)
        ).fetchone()
        if turn is not None and turn["session_id"] != session_key:
            raise CapabilityRejected("trusted turn binding is inconsistent")
        connection.execute(
            """INSERT INTO turns(turn_id,session_id,ordinal,observed_at,provenance)
               VALUES (?,?,0,?,'derived') ON CONFLICT(turn_id) DO NOTHING""",
            (turn_key, session_key, context.observed_at),
        )
        binding = connection.execute(
            "SELECT project_id,session_key,state FROM trusted_turn_bindings WHERE turn_key=?",
            (turn_key,),
        ).fetchone()
        if binding is not None and (
            binding["project_id"] != context.project_id or binding["session_key"] != session_key
        ):
            raise CapabilityRejected("trusted turn binding is inconsistent")
        if binding is not None and binding["state"] == "finished":
            raise CapabilityRejected("turn is already finished")
        connection.execute(
            """INSERT INTO trusted_turn_bindings(turn_key,project_id,session_key,created_at)
               VALUES (?,?,?,?) ON CONFLICT(turn_key) DO NOTHING""",
            (turn_key, context.project_id, session_key, context.observed_at),
        )
        connection.execute(
            """INSERT INTO turn_capabilities(
                   capability_digest,turn_key,created_at,expires_at)
               VALUES (?,?,?,?)""",
            (digest, turn_key, context.observed_at, expires_at),
        )
    return IssuedCapability(token=token, expires_at=expires_at)


def record_initial_understand(
    store: HydraStore,
    keys: Pseudonymizer,
    capability: str,
    context: TrustedAnnotationContext,
    *,
    task_family: str,
) -> AnnotationWrite:
    if context.sequence != 0:
        raise ValueError("initial understand must have sequence zero")
    model = ModelAnnotationInput.from_mapping({
        "kind": "phase",
        "phase": "understand",
        "cause": "prompt",
        "scope_change": "none",
        "task_family": task_family,
        "confidence": 1.0,
        "note": "",
    })
    return _record_annotation(
        store, keys, capability, context, model, provenance=Provenance.DERIVED
    )


def annotate_with_capability(
    store: HydraStore,
    keys: Pseudonymizer,
    capability: str,
    context: TrustedAnnotationContext,
    payload: Mapping[str, Any],
) -> AnnotationWrite:
    model = ModelAnnotationInput.from_mapping(payload)
    return _record_annotation(
        store, keys, capability, context, model, provenance=Provenance.MODEL_REPORTED
    )


def finish_turn(
    store: HydraStore,
    keys: Pseudonymizer,
    capability: str,
    context: TrustedAnnotationContext,
    payload: Mapping[str, Any],
) -> AnnotationWrite:
    model = ModelAnnotationInput.from_mapping(payload)
    if model.kind is not AnnotationKind.FINISH:
        raise ValueError("finish_turn requires a finish annotation")
    return _record_annotation(
        store, keys, capability, context, model, provenance=Provenance.MODEL_REPORTED
    )


def _require_observation_after_issue(binding: Mapping[str, Any], observed_at: str) -> None:
    observed = timestamp(observed_at)
    if observed < timestamp(binding["binding_created_at"]):
        raise CapabilityRejected("trusted observation predates turn binding")
    if observed < timestamp(binding["capability_created_at"]):
        raise CapabilityRejected("trusted observation predates capability")


def _require_new_request_authorized(
    binding: Mapping[str, Any], observed_at: str
) -> None:
    if binding["state"] == "finished" or binding["revoked_at"] is not None:
        raise CapabilityRejected("capability is unavailable")
    if timestamp(observed_at) >= timestamp(binding["expires_at"]):
        raise CapabilityExpired("capability has expired")


def _record_annotation(
    store: HydraStore,
    keys: Pseudonymizer,
    capability: str,
    context: TrustedAnnotationContext,
    model: ModelAnnotationInput,
    *,
    provenance: Provenance,
) -> AnnotationWrite:
    observed = timestamp(context.observed_at)
    capability_key = capability_digest(keys, capability)
    canonical_payload = json.dumps(
        _annotation_payload(model), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    payload_digest = "hpay_v1_" + keys.digest("diagnostic", canonical_payload)
    result: AnnotationWrite | ConflictDecision

    with store.rollout_transaction() as connection:
        binding = binding_for_capability(connection, capability_key)
        _require_observation_after_issue(binding, context.observed_at)
        request_digest = "hreq_v1_" + keys.digest(
            "event", f"{binding['turn_key']}/{context.request_key}"
        )
        by_sequence = connection.execute(
            "SELECT * FROM annotation_receipts WHERE turn_key=? AND sequence=?",
            (binding["turn_key"], context.sequence),
        ).fetchone()
        by_request = connection.execute(
            "SELECT * FROM annotation_receipts WHERE request_digest=?", (request_digest,)
        ).fetchone()
        previous = connection.execute(
            "SELECT observed_at FROM annotations WHERE turn_id=? AND sequence=?",
            (binding["turn_key"], binding["last_sequence"]),
        ).fetchone()
        existing = by_sequence or by_request
        is_exact_retry = existing is not None and (
            existing["sequence"] == context.sequence
            and existing["request_digest"] == request_digest
            and existing["payload_digest"] == payload_digest
        )
        if is_exact_retry:
            last_seen = max(
                (existing["last_received_at"], context.observed_at),
                key=timestamp,
            )
            connection.execute(
                """UPDATE annotation_receipts
                      SET retry_count=retry_count+1,last_received_at=?
                    WHERE annotation_id=?""",
                (last_seen, existing["annotation_id"]),
            )
            result = AnnotationWrite(
                existing["annotation_id"], context.sequence, AnnotationDisposition.RETRIED
            )
        else:
            _require_new_request_authorized(binding, context.observed_at)
            if existing is not None:
                conflict_kind = (
                    "annotation_sequence_conflict"
                    if by_sequence is not None
                    else "annotation_request_conflict"
                )
                stage_fact(
                    connection,
                    keys,
                    binding,
                    sequence=context.sequence,
                    kind=conflict_kind,
                    observed_at=context.observed_at,
                    discriminator=payload_digest,
                )
                result = ConflictDecision("annotation request conflicts with an accepted observation")
            elif (
                context.sequence != binding["last_sequence"] + 1
                or (previous is not None and observed < timestamp(previous["observed_at"]))
            ):
                stage_fact(
                    connection,
                    keys,
                    binding,
                    sequence=context.sequence,
                    kind="annotation_out_of_order",
                    observed_at=context.observed_at,
                    discriminator=payload_digest,
                )
                result = ConflictDecision("annotation sequence is out of order")
            else:
                result = insert_annotation(
                    connection,
                    keys,
                    binding,
                    capability_key,
                    request_digest,
                    payload_digest,
                    context,
                    model,
                    provenance,
                )
    if isinstance(result, ConflictDecision):
        raise AnnotationConflict(result.message)
    return result


def observe_stop(
    store: HydraStore,
    keys: Pseudonymizer,
    capability: str,
    *,
    observed_at: str,
    retry_active: bool,
    retry_expires_at: str | None = None,
) -> StopState:
    if not isinstance(retry_active, bool):
        raise ValueError("retry_active must be a boolean")
    observed = timestamp(observed_at)
    retry_expires = None if retry_expires_at is None else timestamp(retry_expires_at)
    if retry_expires is not None and retry_expires <= observed:
        raise ValueError("stop retry expiry must follow observation")
    capability_key = capability_digest(keys, capability)
    with store.rollout_transaction() as connection:
        binding = binding_for_capability(connection, capability_key)
        _require_observation_after_issue(binding, observed_at)
        latest_annotation = connection.execute(
            """SELECT annotations.observed_at FROM annotations
                JOIN annotation_receipts USING(annotation_id)
                WHERE annotation_receipts.turn_key=?
                ORDER BY annotation_receipts.sequence DESC LIMIT 1""",
            (binding["turn_key"],),
        ).fetchone()
        open_interval = connection.execute(
            """SELECT started_at FROM semantic_intervals
                WHERE turn_key=? AND ended_at IS NULL""",
            (binding["turn_key"],),
        ).fetchone()
        stop_floors = [
            binding["first_stop_at"],
            binding["finished_at"],
            None if latest_annotation is None else latest_annotation["observed_at"],
            None if open_interval is None else open_interval["started_at"],
        ]
        if any(floor is not None and observed < timestamp(floor) for floor in stop_floors):
            raise CapabilityRejected("trusted stop observation is out of order")
        turn_retry = connection.execute(
            "SELECT MAX(stop_retry) FROM turn_capabilities WHERE turn_key=?",
            (binding["turn_key"],),
        ).fetchone()[0]
        if (turn_retry > 0) != (binding["first_stop_at"] is not None):
            raise CapabilityRejected("stop retry state is inconsistent")
        finish = connection.execute(
            """SELECT 1 FROM annotations
                JOIN annotation_receipts USING(annotation_id)
               WHERE annotation_receipts.turn_key=? AND annotations.kind='finish' LIMIT 1""",
            (binding["turn_key"],),
        ).fetchone()
        if finish is not None:
            return StopState.FINISHED
        if binding["state"] == "finished":
            return StopState.SELF_REPORT_MISSING
        if turn_retry == 0:
            connection.execute(
                """UPDATE trusted_turn_bindings
                      SET first_stop_at=? WHERE turn_key=? AND first_stop_at IS NULL""",
                (observed_at, binding["turn_key"]),
            )
            connection.execute(
                "UPDATE turn_capabilities SET stop_retry=1 WHERE turn_key=?",
                (binding["turn_key"],),
            )
            if not retry_active and retry_expires_at is not None:
                connection.execute(
                    """UPDATE turn_capabilities SET expires_at=?
                         WHERE turn_key=? AND revoked_at IS NULL""",
                    (retry_expires_at, binding["turn_key"]),
                )
        if not retry_active:
            return StopState.RETRY_REQUIRED
        connection.execute(
            """UPDATE trusted_turn_bindings
                  SET state='finished',finished_at=? WHERE turn_key=?""",
            (observed_at, binding["turn_key"]),
        )
        connection.execute(
            "UPDATE turn_capabilities SET revoked_at=COALESCE(revoked_at,?) WHERE turn_key=?",
            (observed_at, binding["turn_key"]),
        )
        connection.execute(
            "UPDATE semantic_intervals SET ended_at=? WHERE turn_key=? AND ended_at IS NULL",
            (observed_at, binding["turn_key"]),
        )
        stage_fact(
            connection,
            keys,
            binding,
            sequence=None,
            kind="self_report_missing",
            observed_at=observed_at,
        )
        return StopState.SELF_REPORT_MISSING
