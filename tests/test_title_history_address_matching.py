import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_title_history import matched_history_rows  # noqa: E402


def source_row(tx, paon, street, town, postcode, date, price):
    return {
        "tx": tx,
        "paon": paon,
        "saon": "",
        "street": street,
        "locality": "",
        "town": town,
        "postcode": postcode,
        "date": date,
        "price": price,
        "propertyType": "detached",
        "category": "A",
    }


class TitleHistoryAddressMatchingTests(unittest.TestCase):
    def test_same_date_and_price_at_neighbouring_addresses_selects_one_anchor(self):
        item = {
            "address": "75, EFFINGHAM ROAD, SURBITON, KT6 5JR",
            "paon": "75",
            "saon": "",
            "street": "EFFINGHAM ROAD",
            "locality": "",
            "town": "SURBITON",
            "postcode": "KT6 5JR",
        }
        rows = [
            source_row("tx-75-base", "75", "EFFINGHAM ROAD", "SURBITON", "KT6 5JR", "2020-01-10", 2_500_000),
            source_row("tx-77-base", "77", "EFFINGHAM ROAD", "SURBITON", "KT6 5JR", "2020-01-10", 2_500_000),
            source_row("tx-75-old", "75", "EFFINGHAM ROAD", "SURBITON", "KT6 5JR", "2005-02-03", 900_000),
            source_row("tx-77-old", "77", "EFFINGHAM ROAD", "SURBITON", "KT6 5JR", "2006-04-05", 950_000),
        ]
        known_sales = [{
            "id": "lr-00000000000000000001",
            "address": item["address"],
            "date": "2020-01-10",
            "price": 2_500_000,
        }]

        matched, _method = matched_history_rows(item, rows, known_sales)

        self.assertEqual(
            {row["tx"] for row in matched},
            {"tx-75-base", "tx-75-old"},
        )

    def test_exact_source_ledger_variant_extends_history_without_fuzzy_matching(self):
        item = {
            "address": "FORTUNE HOUSE, KNOWLE LANE, CRANLEIGH, GU6 8JP",
            "paon": "FORTUNE HOUSE",
            "saon": "",
            "street": "KNOWLE LANE",
            "locality": "",
            "town": "CRANLEIGH",
            "postcode": "GU6 8JP",
        }
        rows = [
            source_row("tx-current", "FORTUNE HOUSE", "KNOWLE LANE", "CRANLEIGH", "GU6 8JP", "2020-06-01", 3_000_000),
            source_row("tx-legacy", "FORTUNE HOUSE", "KNOWLE LANE", "CRANLEIGH", "GU6 8UW", "2001-06-01", 1_000_000),
        ]
        variants = [{
            "propertyRecordId": "property:FORTUNE HOUSE KNOWLE LANE CRANLEIGH GU6 8UW|GU68UW",
            "address": "FORTUNE HOUSE, KNOWLE LANE, CRANLEIGH, GU6 8UW",
            "postcode": "GU68UW",
        }]

        matched, _method = matched_history_rows(
            item,
            rows,
            [{"address": item["address"], "date": "2020-06-01", "price": 3_000_000}],
            variants,
        )

        self.assertEqual(
            {row["tx"] for row in matched},
            {"tx-current", "tx-legacy"},
        )


if __name__ == "__main__":
    unittest.main()
