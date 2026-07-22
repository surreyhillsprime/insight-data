import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_data_completeness import historical_expansion_failures  # noqa: E402
from insight_data_utils import finalise_historical_expansion  # noqa: E402
from sweep_land_registry import preserve_existing_enrichments  # noqa: E402


def metadata(**overrides):
    historical = {
        "coverageFrom": "1995-01-01",
        "pre2010Transactions": 1,
        "existingEnrichmentsPreserved": 2,
        "samePropertyEnrichmentsReused": 1,
        "newTransactionsAtExpansion": 1,
        "newTransactionsPendingEnrichment": 1,
    }
    historical.update(overrides)
    return {"historicalExpansion": historical}


class HistoricalExpansionMetadataTests(unittest.TestCase):
    def test_sweep_records_initial_and_pending_counts_separately(self):
        transaction = {
            "address": "1 TEST ROAD, ESHER, KT10 0AA",
            "postcode": "KT10 0AA",
            "price": 2_000_000,
            "date": "2009-01-01",
            "propertyType": "Detached",
            "category": "A",
        }

        _rows, result = preserve_existing_enrichments(
            [transaction],
            {},
            [],
            {},
        )

        historical = result["historicalExpansion"]
        self.assertEqual(historical["newTransactionsAtExpansion"], 1)
        self.assertEqual(historical["newTransactionsPendingEnrichment"], 1)

    def test_final_full_pass_preserves_initial_cohort_and_clears_pending(self):
        result = finalise_historical_expansion(
            metadata(),
            final_pass_complete=True,
        )

        historical = result["historicalExpansion"]
        self.assertEqual(historical["newTransactionsAtExpansion"], 1)
        self.assertEqual(historical["newTransactionsPendingEnrichment"], 0)

    def test_legacy_pending_count_is_promoted_before_finalisation(self):
        legacy = metadata()
        del legacy["historicalExpansion"]["newTransactionsAtExpansion"]

        result = finalise_historical_expansion(legacy, final_pass_complete=True)

        self.assertEqual(
            result["historicalExpansion"],
            {
                **legacy["historicalExpansion"],
                "newTransactionsAtExpansion": 1,
                "newTransactionsPendingEnrichment": 0,
            },
        )

    def test_partial_pass_does_not_claim_completion(self):
        result = finalise_historical_expansion(
            metadata(),
            final_pass_complete=False,
        )

        self.assertEqual(
            result["historicalExpansion"]["newTransactionsPendingEnrichment"],
            1,
        )

    def test_strict_gate_requires_zero_pending_but_base_state_is_valid(self):
        rows = [{}, {}, {}]

        self.assertEqual(
            historical_expansion_failures(rows, metadata(), strict_metadata=False),
            [],
        )
        failures = historical_expansion_failures(
            rows,
            metadata(),
            strict_metadata=True,
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("remain pending enrichment", failures[0])

    def test_gate_rejects_incoherent_initial_accounting(self):
        failures = historical_expansion_failures(
            [{}, {}, {}],
            metadata(existingEnrichmentsPreserved=1),
        )

        self.assertEqual(len(failures), 1)
        self.assertIn("do not reconcile", failures[0])


if __name__ == "__main__":
    unittest.main()
