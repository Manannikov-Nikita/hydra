from __future__ import annotations

import unittest

from hydra_codex.contracts import (
    AnnotationCause,
    AnnotationContext,
    AnnotationKind,
    AnnotationPhase,
    AnnotationRecord,
    ModelAnnotationInput,
    Outcome,
    Provenance,
    ScopeChange,
    materialize_annotation,
)


class AnnotationContractTests(unittest.TestCase):
    def test_invalid_annotation_enum_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ModelAnnotationInput(
                kind="not-a-kind",
                phase=AnnotationPhase.IMPLEMENT,
                cause=AnnotationCause.PROMPT,
                scope_change=ScopeChange.NONE,
                confidence=0.9,
                note="start",
            )

    def test_finish_annotation_requires_outcome(self) -> None:
        with self.assertRaises(ValueError):
            ModelAnnotationInput(
                kind=AnnotationKind.FINISH,
                phase=AnnotationPhase.IMPLEMENT,
                cause=AnnotationCause.PROMPT,
                scope_change=ScopeChange.NONE,
                confidence=0.9,
                note="done",
            )

    def test_non_finish_annotation_rejects_outcome(self) -> None:
        with self.assertRaises(ValueError):
            ModelAnnotationInput(
                kind=AnnotationKind.PHASE,
                phase=AnnotationPhase.IMPLEMENT,
                cause=AnnotationCause.PROMPT,
                scope_change=ScopeChange.NONE,
                confidence=0.9,
                outcome=Outcome.SUCCESS,
                note="started",
            )

    def test_note_must_not_exceed_240_characters(self) -> None:
        with self.assertRaises(ValueError):
            ModelAnnotationInput(
                kind=AnnotationKind.PHASE,
                phase=AnnotationPhase.IMPLEMENT,
                cause=AnnotationCause.PROMPT,
                scope_change=ScopeChange.NONE,
                confidence=0.9,
                note="x" * 241,
            )

    def test_confidence_must_be_a_number_from_zero_to_one(self) -> None:
        with self.assertRaises(ValueError):
            ModelAnnotationInput(
                kind=AnnotationKind.PHASE,
                phase=AnnotationPhase.IMPLEMENT,
                cause=AnnotationCause.PROMPT,
                scope_change=ScopeChange.NONE,
                confidence=1.1,
                note="too certain",
            )

    def test_model_input_rejects_untrusted_measurement_and_identity_fields(self) -> None:
        payload = {
            "kind": "phase",
            "phase": "implement",
            "cause": "prompt",
            "scope_change": "none",
            "confidence": 0.9,
            "note": "begin",
        }
        for forbidden in (
            "tokens",
            "duration_ms",
            "file_count",
            "test_count",
            "session_id",
            "turn_id",
            "timestamp",
        ):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValueError):
                    ModelAnnotationInput.from_mapping({**payload, forbidden: 1})

    def test_trusted_context_is_required_to_materialize_an_annotation(self) -> None:
        model_input = ModelAnnotationInput(
            kind=AnnotationKind.FINISH,
            phase=AnnotationPhase.IMPLEMENT,
            cause=AnnotationCause.PROMPT,
            scope_change=ScopeChange.NONE,
            confidence=0.9,
            outcome=Outcome.SUCCESS,
            note="completed",
        )
        annotation = materialize_annotation(
            model_input,
            AnnotationContext(
                annotation_id="ann-1",
                project_id="project-1",
                session_id="session-1",
                turn_id="turn-1",
                sequence=2,
                observed_at="2026-07-20T10:00:00Z",
            ),
        )

        self.assertIsInstance(annotation, AnnotationRecord)
        self.assertEqual(annotation.session_id, "session-1")
        self.assertEqual(annotation.provenance, Provenance.MODEL_REPORTED)
