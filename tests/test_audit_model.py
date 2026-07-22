from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from hydra_codex.audit_model import (
    AUDIT_SCHEMA,
    AuditEvidenceRegistry,
    AuditFact,
    AuditReport,
)
from hydra_codex.reporting import NumericFact


class AuditEvidenceRegistryTests(unittest.TestCase):
    def test_registry_preserves_zero_unavailable_and_lower_bound_as_distinct_facts(self) -> None:
        registry = AuditEvidenceRegistry()

        zero_ref = registry.register(
            "headline.test_runs",
            NumericFact(0, "count", "derived", lower_bound=0),
        )
        unavailable_ref = registry.register(
            "headline.file_reads",
            NumericFact(
                None,
                "count",
                "estimated",
                ("file_observation_unavailable",),
                lower_bound=3,
            ),
        )

        self.assertEqual(AUDIT_SCHEMA, "hydra.audit/v1")
        self.assertNotEqual(zero_ref, unavailable_ref)
        evidence = {item.evidence_id: item for item in registry.evidence}
        self.assertEqual(
            evidence[zero_ref].as_dict(),
            {
                "caveats": [],
                "evidence_id": zero_ref,
                "fact": "headline.test_runs",
                "lower_bound": 0,
                "provenance": "derived",
                "unit": "count",
                "value": 0,
            },
        )
        self.assertEqual(evidence[unavailable_ref].value, None)
        self.assertEqual(evidence[unavailable_ref].lower_bound, 3)
        self.assertEqual(
            evidence[unavailable_ref].caveats,
            ("file_observation_unavailable",),
        )

    def test_registry_rejects_duplicate_fact_paths_and_records_are_frozen(self) -> None:
        registry = AuditEvidenceRegistry()
        registry.register("transport.delivery_failures", NumericFact(0, "count", "exact"))

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(
                "transport.delivery_failures",
                NumericFact(1, "count", "exact"),
            )
        with self.assertRaises(FrozenInstanceError):
            registry.evidence[0].value = 1  # type: ignore[misc]

    def test_registry_ids_are_stable_across_registration_order(self) -> None:
        facts = {
            "headline.working_tokens": NumericFact(12, "tokens", "derived"),
            "storage.database_bytes": AuditFact(4096, "bytes", "exact"),
        }
        forward = AuditEvidenceRegistry()
        reverse = AuditEvidenceRegistry()
        forward_refs = {name: forward.register(name, fact) for name, fact in facts.items()}
        reverse_refs = {
            name: reverse.register(name, facts[name]) for name in reversed(tuple(facts))
        }

        self.assertEqual(forward_refs, reverse_refs)
        self.assertEqual(
            tuple(item.fact for item in forward.evidence),
            tuple(sorted(facts)),
        )


class AuditReferenceClosureTests(unittest.TestCase):
    @staticmethod
    def _sample():
        from tests.test_audit_renderers import sample_audit

        return sample_audit()

    def test_report_rejects_dangling_and_malformed_structured_references(self) -> None:
        original = self._sample()
        cases = (
            "ev_0000000000000000",
            0,
        )

        for replacement in cases:
            with self.subTest(replacement=replacement):
                payload = original.as_dict()
                payload["cohort"]["headline"]["working_tokens"] = replacement
                with self.assertRaisesRegex(ValueError, "evidence reference"):
                    AuditReport.create(
                        pilot_snapshot=payload["pilot_snapshot"],
                        cohort=payload["cohort"],
                        collection=payload["collection"],
                        storage_health=payload["storage_health"],
                        evidence_appendix=original.evidence_appendix,
                    )

    def test_report_rejects_an_orphan_appendix_record(self) -> None:
        original = self._sample()
        registry = AuditEvidenceRegistry()
        registry.register("zz.orphan", AuditFact(1, "count", "exact"))
        appendix = tuple(sorted(
            original.evidence_appendix + registry.evidence,
            key=lambda item: item.fact,
        ))
        payload = original.as_dict()

        with self.assertRaisesRegex(ValueError, "orphan evidence"):
            AuditReport.create(
                pilot_snapshot=payload["pilot_snapshot"],
                cohort=payload["cohort"],
                collection=payload["collection"],
                storage_health=payload["storage_health"],
                evidence_appendix=appendix,
            )

    def test_report_does_not_treat_domain_text_as_an_evidence_reference(self) -> None:
        original = self._sample()
        payload = original.as_dict()
        payload["cohort"]["task_family"] = "ev_research"

        report = AuditReport.create(
            pilot_snapshot=payload["pilot_snapshot"],
            cohort=payload["cohort"],
            collection=payload["collection"],
            storage_health=payload["storage_health"],
            evidence_appendix=original.evidence_appendix,
        )

        self.assertEqual(report.as_dict()["cohort"]["task_family"], "ev_research")


if __name__ == "__main__":
    unittest.main()
