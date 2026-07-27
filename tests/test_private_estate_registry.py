import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_private_estates_asset import build_asset_payload
from private_estates import classify_estate, load_compiled_registry


def record(street, postcode=""):
    return {
        "street": street,
        "district": "Elmbridge",
        "town": "Oxshott",
        "postcode": postcode,
    }


class CorrectedSixRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_compiled_registry.cache_clear()
        cls.compiled = load_compiled_registry()
        cls.asset = build_asset_payload()

    def test_compiled_registry_and_asset_use_signed_off_counts(self):
        self.assertEqual(
            self.compiled["registryVersion"],
            "surrey-private-estates-2026-07-22-corrected-6",
        )
        self.assertEqual(len(self.compiled["definitions"]), 24)
        self.assertEqual(len(self.compiled["rules"]), 155)
        crown = next(
            item
            for item in self.compiled["definitions"]
            if item["estateId"] == "crown-estate-oxshott"
        )
        self.assertEqual(crown["installStatus"], "partial_ready")
        self.assertEqual(crown["activeRuleCount"], 18)
        self.assertEqual(self.asset["registryVersion"], self.compiled["registryVersion"])
        self.assertEqual(self.asset["metadata"]["activeDefinitionCount"], 24)
        self.assertEqual(self.asset["metadata"]["activeRuleCount"], 155)
        self.assertEqual(self.asset["metadata"]["navigationAnchorCount"], 24)

    def test_crown_estate_matches_only_the_reviewed_core(self):
        for street, postcode in (
            ("Birds Hill Road", "KT22 0NJ"),
            ("Moles Hill", "KT22 0QB"),
            ("Fair Oak Close", "KT22 0TJ"),
        ):
            with self.subTest(street=street):
                match = classify_estate(record(street, postcode), compiled=self.compiled)
                self.assertEqual(match["estateId"], "crown-estate-oxshott")
                self.assertEqual(match["estate"], "Crown Estate, Oxshott")

        for street, postcode in (
            ("Highfield Close", "KT22 0QA"),
            ("Goldrings Road", ""),
            ("Holtwood Road", ""),
        ):
            with self.subTest(street=street):
                self.assertEqual(
                    classify_estate(record(street, postcode), compiled=self.compiled),
                    {},
                )


if __name__ == "__main__":
    unittest.main()
