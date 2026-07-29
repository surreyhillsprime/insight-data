import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from insight_data_utils import parse_window_json


LANES = (
    ("signals", "New signals"),
    ("opportunities", "Opportunities"),
)
ALLOWED_KINDS = {
    "signals": {
        "epc_observation",
        "property_planning",
        "sale_age_milestone",
    },
    "opportunities": {
        "property_opportunity",
    },
}
COMMON_ITEM_FIELDS = {
    "id",
    "lane",
    "kind",
    "title",
    "summary",
    "effectiveDate",
    "datePrecision",
    "confidence",
    "source",
    "sourceIds",
    "evidenceIds",
    "propertyIds",
    "place",
    "limitations",
}
USER_STATE_FIELDS = {
    "watch",
    "watched",
    "watching",
    "watchState",
    "workflowState",
    "userState",
    "read",
    "readAt",
    "reviewed",
    "reviewedAt",
    "dismissed",
    "dismissedAt",
}


def nested_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


class TodayFeedContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed_path = ROOT / "outputs" / "today-feed.js"
        cls.schema_path = ROOT / "config" / "today-feed.schema.json"
        if not cls.feed_path.is_file():
            raise AssertionError("Missing outputs/today-feed.js")
        if not cls.schema_path.is_file():
            raise AssertionError("Missing config/today-feed.schema.json")
        cls.feed_text = cls.feed_path.read_text(encoding="utf-8")
        cls.schema_text = cls.schema_path.read_text(encoding="utf-8")
        cls.schema = json.loads(cls.schema_text)
        cls.feed = parse_window_json(cls.feed_text, "INSIGHT_TODAY_FEED", None)
        cls.metadata = parse_window_json(cls.feed_text, "INSIGHT_TODAY_META", None)

    def test_schema_publishes_the_two_read_only_lanes(self):
        self.assertEqual(
            self.schema.get("$schema"),
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertEqual(
            self.schema.get("properties", {}).get("schemaVersion", {}).get("const"),
            2,
        )
        self.assertTrue(
            {"schemaVersion", "asOf", *(lane for lane, _label in LANES)}.issubset(
                set(self.schema.get("required") or [])
            )
        )
        self.assertFalse(self.schema.get("additionalProperties", True))
        for lane, _label in LANES:
            lane_schema = self.schema.get("properties", {}).get(lane, {})
            self.assertEqual(lane_schema.get("type"), "array", lane)
            self.assertIn("items", lane_schema, lane)
        serialised_schema = json.dumps(self.schema, sort_keys=True)
        for kinds in ALLOWED_KINDS.values():
            for kind in kinds:
                self.assertIn(kind, serialised_schema)
        self.assertNotIn("entity_news", serialised_schema)

    def test_feed_and_metadata_reconcile_exactly(self):
        self.assertIsInstance(self.feed, dict)
        self.assertIsInstance(self.metadata, dict)
        self.assertEqual(self.feed.get("schemaVersion"), 2)
        self.assertEqual(self.metadata.get("schemaVersion"), 2)
        self.assertEqual(self.metadata.get("asOf"), self.feed.get("asOf"))
        self.assertEqual(self.metadata.get("generatorVersion"), "today-feed-3")
        self.assertRegex(
            str(self.metadata.get("generatedAt") or ""),
            r"^\d{4}-\d{2}-\d{2}T",
        )
        self.assertEqual(
            set((self.metadata.get("sourceFingerprints") or {}).keys()),
            {"propertyRecords"},
        )
        self.assertEqual(
            set((self.metadata.get("sourceGeneratedAt") or {}).keys()),
            {"propertyRecords"},
        )
        self.assertIs(
            (self.metadata.get("criteria") or {}).get("newsRowsExcluded"),
            True,
        )
        self.assertIs(
            (self.metadata.get("criteria") or {}).get(
                "everyQualifyingSignalCreatesPropertyOpportunity"
            ),
            True,
        )
        self.assertEqual(
            (self.metadata.get("criteria") or {}).get("opportunityGrouping"),
            "one-per-property",
        )
        self.assertEqual(
            (self.metadata.get("criteria") or {}).get(
                "hotMinimumIndependentSourceFamilies"
            ),
            2,
        )
        self.assertEqual(
            (self.metadata.get("criteria") or {}).get("planningLookbackDays"),
            45,
        )
        self.assertNotIn(
            "newsMinimumScore",
            self.metadata.get("criteria") or {},
        )
        self.assertNotIn(
            "opportunityRequiresIndependentPropertySignals",
            self.metadata.get("criteria") or {},
        )
        self.assertNotIn(
            "opportunityRequiresIndependentSource",
            self.metadata.get("criteria") or {},
        )

        counts = {
            lane: len(self.feed.get(lane) or [])
            for lane, _label in LANES
        }
        self.assertEqual(self.metadata.get("counts"), counts)
        self.assertEqual(
            self.metadata.get("summary"),
            [
                {"id": lane, "label": label, "count": counts[lane]}
                for lane, label in LANES
            ],
        )
        self.assertIsInstance(self.metadata.get("limitations"), list)
        self.assertTrue(self.metadata["limitations"])

    def test_items_are_evidence_linked_and_lane_typed(self):
        seen_ids = set()
        signals = {
            item["id"]: item
            for item in self.feed.get("signals") or []
        }
        for lane, _label in LANES:
            items = self.feed.get(lane)
            self.assertIsInstance(items, list, lane)
            for index, item in enumerate(items):
                self.assertIsInstance(item, dict, f"{lane}[{index}]")
                self.assertFalse(
                    COMMON_ITEM_FIELDS - item.keys(),
                    f"{lane}[{index}] is missing {sorted(COMMON_ITEM_FIELDS - item.keys())}",
                )
                self.assertEqual(item["lane"], lane)
                self.assertIn(item["kind"], ALLOWED_KINDS[lane])
                self.assertTrue(item["id"])
                self.assertNotIn(item["id"], seen_ids)
                seen_ids.add(item["id"])
                self.assertIn(item["datePrecision"], {"day", "month", "year", "unknown"})
                self.assertIn(item["confidence"], {"high", "medium", "low"})
                self.assertIsInstance(item["sourceIds"], list)
                self.assertTrue(item["sourceIds"])
                self.assertIsInstance(item["evidenceIds"], list)
                self.assertTrue(item["evidenceIds"])
                self.assertIsInstance(item["propertyIds"], list)
                self.assertIsInstance(item["limitations"], list)
                self.assertTrue(item["limitations"])

                if lane == "opportunities":
                    self.assertTrue(item.get("directSignalId"))
                    self.assertIsInstance(item.get("corroborationIds"), list)
                    self.assertGreaterEqual(item.get("independentSourceCount", 0), 1)
                    self.assertGreaterEqual(item.get("indicatorKindCount", 0), 1)
                    self.assertIn(item.get("opportunityLevel"), {"Standard", "Hot"})
                    direct = signals[item["directSignalId"]]
                    corroboration = [
                        signals[identifier]
                        for identifier in item["corroborationIds"]
                    ]
                    self.assertTrue(all(
                        signal["property"]["propertyId"]
                        == item["property"]["propertyId"]
                        for signal in [direct, *corroboration]
                    ))
                    self.assertEqual(
                        len({
                            signal["sourceFamily"]
                            for signal in [direct, *corroboration]
                        }),
                        item["independentSourceCount"],
                    )
                    self.assertEqual(
                        len({
                            signal["kind"]
                            for signal in [direct, *corroboration]
                        }),
                        item["indicatorKindCount"],
                    )
                    self.assertEqual(
                        item["opportunityLevel"],
                        "Hot" if item["independentSourceCount"] >= 2 else "Standard",
                    )
                    self.assertEqual(
                        {
                            signal["id"]
                            for signal in self.feed["signals"]
                            if signal["property"]["propertyId"]
                            == item["property"]["propertyId"]
                        },
                        {direct["id"], *item["corroborationIds"]},
                    )
                self.assertNotEqual(item.get("kind"), "entity_news")
                self.assertNotEqual(item.get("sourceFamily"), "news")
                self.assertNotEqual(item.get("coverage", {}).get("sourceKey"), "news")
                self.assertNotIn(
                    "news",
                    item.get("attributes", {}).get(
                        "corroborationSourceFamilies",
                        [],
                    ),
                )

        self.assertEqual(
            {
                signal["property"]["propertyId"]
                for signal in self.feed["signals"]
            },
            {
                opportunity["property"]["propertyId"]
                for opportunity in self.feed["opportunities"]
            },
        )

    def test_feed_contains_no_user_workflow_or_watching_state(self):
        feed_keys = set(nested_keys(self.feed))
        metadata_keys = set(nested_keys(self.metadata))
        schema_keys = set(nested_keys(self.schema))
        self.assertFalse(USER_STATE_FIELDS & feed_keys)
        self.assertFalse(USER_STATE_FIELDS & metadata_keys)
        self.assertFalse(USER_STATE_FIELDS & schema_keys)
        combined_text = "\n".join((self.feed_text, self.schema_text))
        self.assertNotRegex(combined_text, r"(?i)\bwatch(?:ed|ing)?\b")
        self.assertNotRegex(combined_text, r"(?i)\bworkflow state\b")

    def test_opportunity_copy_does_not_claim_private_intent(self):
        prohibited_claims = (
            r"\blikely vendor\b",
            r"\blikely seller\b",
            r"\blikely to sell\b",
            r"\bwill sell\b",
            r"\bmust sell\b",
            r"\bmotivated seller\b",
            r"\boff-market opportunity\b",
            r"\bowner (?:is|appears|seems) (?:preparing|planning|intending) to sell\b",
        )
        for lane, _label in LANES:
            for item in self.feed.get(lane) or []:
                visible_copy = " ".join(
                    str(item.get(key) or "")
                    for key in ("title", "summary")
                )
                for pattern in prohibited_claims:
                    self.assertNotRegex(visible_copy, re.compile(pattern, re.I))


if __name__ == "__main__":
    unittest.main()
