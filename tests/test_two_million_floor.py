import unittest
from pathlib import Path


from sweep_land_registry import (
    PRICE_FLOOR,
    archive_row,
    metadata,
    normalise_rows,
    sparql_query,
    velocity_cutoff_date,
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

    def test_strict_gate_requires_the_epc_pass_to_finish(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" / "check_data_completeness.py").read_text(encoding="utf-8")
        self.assertIn('epc_meta.get("status")', source)
        self.assertIn("enrichment has not completed across the full transaction universe", source)
        self.assertIn('epc_meta.get("requested")', source)
        self.assertIn("resolved and pending counts do not reconcile", source)
        self.assertIn("lookups remain pending", source)


if __name__ == "__main__":
    unittest.main()
