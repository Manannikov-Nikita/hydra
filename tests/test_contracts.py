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
    ThreadSessionRecord,
    TokenSampleRecord,
    TurnRecord,
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
                task_family="foundation",
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
                task_family="foundation",
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
                task_family="foundation",
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
                task_family="foundation",
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
                task_family="foundation",
                confidence=1.1,
                note="too certain",
            )

    def test_task_family_is_required_short_model_semantics(self) -> None:
        with self.assertRaises(ValueError):
            ModelAnnotationInput(
                kind=AnnotationKind.PHASE,
                phase=AnnotationPhase.IMPLEMENT,
                cause=AnnotationCause.PROMPT,
                scope_change=ScopeChange.NONE,
                task_family="x" * 81,
                confidence=0.9,
                note="too broad",
            )

    def test_task_family_is_a_storage_safe_category(self) -> None:
        for family in (
            "quiz", "multi_answer_quiz", "review.fix",
            "multiple-answer-quiz", "release-workflow-hardening",
            "lesson-block-architecture", "feature_closeout_workflow",
        ):
            with self.subTest(valid=family):
                model = ModelAnnotationInput(
                    kind="phase", phase="implement", cause="prompt",
                    scope_change="none", task_family=family, confidence=0.9,
                    note="start",
                )
                self.assertEqual(model.task_family, family)
        for family in (
            "/Users/alice/private", "alice@example.com", "raw family with spaces",
            "019f75d4-5125-7343-8537-49b80f27f286", "password=private",
            "ABCD1234EFGH5678WXYZ", "customer-123456", "alice",
            "secret-token", "private-looking", "alice-review", "acme-workflow",
            "alice-termination-review",
        ):
            with self.subTest(invalid=family):
                with self.assertRaisesRegex(ValueError, "privacy-safe category"):
                    ModelAnnotationInput(
                        kind="phase", phase="implement", cause="prompt",
                        scope_change="none", task_family=family, confidence=0.9,
                        note="start",
                    )

    def test_model_input_rejects_untrusted_measurement_and_identity_fields(self) -> None:
        payload = {
            "kind": "phase",
            "phase": "implement",
            "cause": "prompt",
            "scope_change": "none",
            "task_family": "foundation",
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
            task_family="foundation",
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
        self.assertEqual(annotation.task_family, "foundation")
        self.assertEqual(annotation.provenance, Provenance.MODEL_REPORTED)

    def test_counts_reject_bool_string_and_float_values(self) -> None:
        for value in (True, "1", 1.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    AnnotationContext(
                        annotation_id="ann-1", project_id="project-1", session_id="session-1",
                        turn_id="turn-1", sequence=value, observed_at="2026-07-20T10:00:00Z",
                    )
                with self.assertRaises(ValueError):
                    TurnRecord(
                        turn_id="turn-1", session_id="session-1", ordinal=value,
                        observed_at="2026-07-20T10:00:00Z",
                    )
                with self.assertRaises(ValueError):
                    TokenSampleRecord(
                        sample_id="sample-1", session_id="session-1", turn_id="turn-1",
                        observed_at="2026-07-20T10:00:00Z", input_tokens=value,
                        output_tokens=1, provenance=Provenance.EXACT,
                    )
