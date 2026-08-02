import unittest
from datetime import datetime, timezone
from pathlib import Path


from sweep_land_registry import (
    PRICE_FLOOR,
    archive_row,
    metadata,
    normalise_rows,
    sparql_query,
    velocity_cutoff_date,
)
from transaction_exclusions import (
    excluded_transaction_failures,
    london_today,
    transaction_exclusion_metadata,
)


def archive_values(price):
    return [
        "{transaction-id}",
        str(price),
        "2025-02-03 00:00",
        "KT10 9AA",
        "D",
        "N",
        "F",
        "2",
        "",
        "TEST ROAD",
        "ESHER",
        "ESHER",
        "ELMBRIDGE",
        "SURREY",
        "A",
        "A",
    ]


class TwoMillionFloorTests(unittest.TestCase):
    def test_floor_is_two_million_in_sparql(self):
        self.assertEqual(PRICE_FLOOR, 2_000_000)
        self.assertIn("FILTER(?price >= 2000000)", sparql_query())

    def test_archive_accepts_floor_and_rejects_below_floor(self):
        self.assertIsNotNone(archive_row(archive_values(2_000_000)))
        self.assertIsNone(archive_row(archive_values(1_999_999)))

    def test_normalised_feed_has_canonical_property_and_floor_metadata(self):
        _raw_count, rows = normalise_rows([
            {
                "price": 2_000_000,
                "date": "2025-02-03",
                "postcode": "KT10 9AA",
                "propertyType": "D",
                "paon": "2",
                "saon": "",
                "street": "TEST ROAD",
                "locality": "ESHER",
                "town": "ESHER",
                "district": "ELMBRIDGE",
                "county": "SURREY",
                "category": "A",
            },
            {
                "price": 1_999_999,
                "date": "2025-02-03",
                "postcode": "KT10 9AA",
                "propertyType": "D",
                "paon": "3",
                "street": "TEST ROAD",
                "town": "ESHER",
                "district": "ELMBRIDGE",
                "county": "SURREY",
                "category": "A",
            },
        ])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["propertyRecordId"].startswith("property:"))
        feed_meta = metadata(1, rows)
        self.assertEqual(feed_meta["priceFloor"], 2_000_000)
        self.assertEqual(feed_meta["canonicalPropertyRecords"], 1)

    def test_velocity_cutoff_uses_two_month_maturity_lag(self):
        self.assertEqual(velocity_cutoff_date("2026-05-06"), "2026-03-31")

    def test_reviewed_cherry_tree_lane_input_error_is_excluded_exactly(self):
        bad = {
            "tx": "http://landregistry.data.gov.uk/data/ppi/transaction/559C5AD5-8FA4-AFE3-E063-4804A8C004A4/current",
            "price": 39_500_000,
            "date": "2026-05-11",
            "postcode": "GU7 3RS",
            "propertyType": "T",
            "paon": "42",
            "street": "CHERRY TREE LANE",
            "town": "GODALMING",
            "district": "WAVERLEY",
            "category": "A",
        }
        archive_bad = {**bad, "tx": "{559C5AD5-8FA4-AFE3-E063-4804A8C004A4}"}
        reformatted_bad = {**bad, "paon": "42A", "street": "CHERRY TREE LN"}
        nearby_legitimate = {**bad, "tx": "DIFFERENT-ID", "paon": "44", "price": 3_950_000}
        raw_count, rows = normalise_rows([bad, archive_bad, reformatted_bad, nearby_legitimate])
        self.assertEqual(raw_count, 4)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "44, CHERRY TREE LANE, GODALMING, GU7 3RS")
        self.assertEqual(rows[0]["price"], 3_950_000)

    def test_reviewed_exclusion_is_a_published_feed_gate(self):
        bad_published_row = {
            "address": "42, CHERRY TREE LANE, GODALMING, GU7 3RS",
            "postcode": "GU7 3RS",
            "price": 39_500_000,
            "date": "2026-05-11",
            "propertyType": "Terraced",
            "category": "A",
        }
        self.assertEqual(len(excluded_transaction_failures([bad_published_row])), 1)
        self.assertEqual(transaction_exclusion_metadata()["reviewedExclusionCount"], 1)

    def test_exclusion_review_day_uses_the_product_london_timezone(self):
        boundary = datetime(2026, 8, 1, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(london_today(boundary).isoformat(), "2026-08-02")

    def test_strict_gate_requires_the_epc_pass_to_finish(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" / "check_data_completeness.py").read_text(encoding="utf-8")
        self.assertIn('epc_meta.get("status")', source)
        self.assertIn("enrichment has not completed across the full transaction universe", source)
        self.assertIn('epc_meta.get("requested")', source)
        self.assertIn("resolved and pending counts do not reconcile", source)
        self.assertIn("lookups remain pending", source)


if __name__ == "__main__":
    unittest.main()
