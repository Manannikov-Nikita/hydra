from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from hydra_codex.audit_model import AUDIT_SCHEMA, AuditEvidenceRegistry, AuditFact
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


if __name__ == "__main__":
    unittest.main()
