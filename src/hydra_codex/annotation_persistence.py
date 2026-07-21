"""Small SQL helpers for capability-backed semantic annotations."""

from __future__ import annotations

import sqlite3

from .annotation_types import AnnotationDisposition, AnnotationWrite, CapabilityRejected, TrustedAnnotationContext
from .contracts import AnnotationKind, ModelAnnotationInput, Provenance
from .redaction import redact_note
from .rollout_identity import Pseudonymizer


def stage_fact(
    connection: sqlite3.Connection,
    keys: Pseudonymizer,
    binding: sqlite3.Row,
    *,
    sequence: int | None,
    kind: str,
    observed_at: str,
    discriminator: str = "",
) -> None:
    fact_key = "hfact_v1_" + keys.digest(
        "diagnostic", f"{binding['turn_key']}/{sequence}/{kind}/{discriminator}"
    )
    connection.execute(
        """INSERT INTO semantic_fact_staging(
               fact_key,project_id,session_key,turn_key,sequence,fact_kind,observed_at,provenance)
           VALUES (?,?,?,?,?,?,?,'derived') ON CONFLICT(fact_key) DO NOTHING""",
        (
            fact_key, binding["project_id"], binding["session_key"], binding["turn_key"],
            sequence, kind, observed_at,
        ),
    )


def binding_for_capability(connection: sqlite3.Connection, digest: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT turn_capabilities.capability_digest,turn_capabilities.turn_key,
                  turn_capabilities.created_at AS capability_created_at,
                  turn_capabilities.expires_at,turn_capabilities.used_at,
                  turn_capabilities.revoked_at,turn_capabilities.stop_retry,
                  trusted_turn_bindings.project_id,trusted_turn_bindings.session_key,
                  trusted_turn_bindings.created_at AS binding_created_at,
                  trusted_turn_bindings.state,trusted_turn_bindings.last_sequence,
                  trusted_turn_bindings.finished_at,trusted_turn_bindings.first_stop_at
             FROM turn_capabilities JOIN trusted_turn_bindings USING(turn_key)
            WHERE capability_digest=?""",
        (digest,),
    ).fetchone()
    if row is None:
        raise CapabilityRejected("capability is unavailable")
    return row


def insert_annotation(
    connection: sqlite3.Connection,
    keys: Pseudonymizer,
    binding: sqlite3.Row,
    capability_digest: str,
    request_digest: str,
    payload_digest: str,
    context: TrustedAnnotationContext,
    model: ModelAnnotationInput,
    provenance: Provenance,
) -> AnnotationWrite:
    annotation_id = "hann_v1_" + keys.digest(
        "event", f"{binding['turn_key']}/{context.sequence}/{request_digest}"
    )
    connection.execute(
        """INSERT INTO annotations(
               annotation_id,project_id,session_id,turn_id,sequence,observed_at,kind,
               phase,cause,scope_change,task_family,confidence,outcome,provenance,
               note_redacted,note_hash,note_length)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            annotation_id, binding["project_id"], binding["session_key"], binding["turn_key"],
            context.sequence, context.observed_at, model.kind.value, model.phase.value,
            model.cause.value, model.scope_change.value, model.task_family, model.confidence,
            None if model.outcome is None else model.outcome.value, provenance.value,
            redact_note(model.note), keys.digest("diagnostic", "annotation-note/" + model.note),
            len(model.note),
        ),
    )
    connection.execute(
        """INSERT INTO annotation_receipts(
               annotation_id,turn_key,capability_digest,sequence,request_digest,payload_digest,
               first_received_at,last_received_at) VALUES (?,?,?,?,?,?,?,?)""",
        (
            annotation_id, binding["turn_key"], capability_digest, context.sequence,
            request_digest, payload_digest, context.observed_at, context.observed_at,
        ),
    )
    update_intervals(connection, keys, binding, annotation_id, context, model, provenance)
    connection.execute(
        "UPDATE trusted_turn_bindings SET last_sequence=? WHERE turn_key=?",
        (context.sequence, binding["turn_key"]),
    )
    connection.execute(
        "UPDATE turn_capabilities SET used_at=COALESCE(used_at,?) WHERE capability_digest=?",
        (context.observed_at, capability_digest),
    )
    if model.kind is AnnotationKind.FINISH:
        connection.execute(
            "UPDATE trusted_turn_bindings SET state='finished',finished_at=? WHERE turn_key=?",
            (context.observed_at, binding["turn_key"]),
        )
        connection.execute(
            "UPDATE turn_capabilities SET revoked_at=? WHERE turn_key=? AND revoked_at IS NULL",
            (context.observed_at, binding["turn_key"]),
        )
    return AnnotationWrite(annotation_id, context.sequence, AnnotationDisposition.INSERTED)


def update_intervals(
    connection: sqlite3.Connection,
    keys: Pseudonymizer,
    binding: sqlite3.Row,
    annotation_id: str,
    context: TrustedAnnotationContext,
    model: ModelAnnotationInput,
    provenance: Provenance,
) -> None:
    current = connection.execute(
        "SELECT * FROM semantic_intervals WHERE turn_key=? AND ended_at IS NULL",
        (binding["turn_key"],),
    ).fetchone()
    if (
        current is not None
        and model.kind is AnnotationKind.BLOCKER
        and current["phase"] != model.phase.value
    ):
        stage_fact(
            connection,
            keys,
            binding,
            sequence=context.sequence,
            kind="semantic_conflict",
            observed_at=context.observed_at,
            discriminator=model.phase.value,
        )
    if current is not None:
        connection.execute(
            """UPDATE semantic_intervals SET end_annotation_id=?,end_sequence=?,ended_at=?
                WHERE interval_key=?""",
            (annotation_id, context.sequence, context.observed_at, current["interval_key"]),
        )
    if model.kind is AnnotationKind.FINISH or (model.kind is AnnotationKind.BLOCKER and current is None):
        return
    phase = current["phase"] if model.kind is AnnotationKind.BLOCKER else model.phase.value
    interval_key = "hint_v1_" + keys.digest("event", "interval/" + annotation_id)
    connection.execute(
        """INSERT INTO semantic_intervals(
               interval_key,project_id,session_key,turn_key,start_annotation_id,start_sequence,
               started_at,phase,cause,provenance) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            interval_key, binding["project_id"], binding["session_key"], binding["turn_key"],
            annotation_id, context.sequence, context.observed_at, phase, model.cause.value,
            "derived" if provenance is Provenance.DERIVED else "model_reported",
        ),
    )
