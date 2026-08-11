import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from insight_data_utils import (  # noqa: E402
    ADDRESS_CANONICALISATION_VERSION,
    PROPERTY_ADDRESS_PRESENTATIONS_PATH,
    canonical_display_address,
    canonicalise_property_addresses,
    property_record_id,
    read_js,
    reviewed_property_address_presentations,
    write_js,
)


SOURCE_PROPERTY_ID = "property:33 FAIRMILE AVENUE COBHAM KT11 2JA|KT112JA"
CANONICAL_PROPERTY_ID = (
    "property:COROMANDEL 33 FAIRMILE AVENUE COBHAM KT11 2JA|KT112JA"
)


def fairmile_transaction(transaction_id, date, price):
    return {
        "id": transaction_id,
        "propertyRecordId": SOURCE_PROPERTY_ID,
        "address": "33, FAIRMILE AVENUE, COBHAM, KT11 2JA",
        "saon": "",
        "paon": "33",
        "street": "FAIRMILE AVENUE",
        "locality": "",
        "town": "COBHAM",
        "postcode": "KT11 2JA",
        "market": "elmbridge-prime",
        "district": "Elmbridge",
        "price": price,
        "date": date,
        "propertyType": "Detached",
        "category": "A",
    }


class OwnerReviewedAddressPresentationTests(unittest.TestCase):
    def test_registry_is_single_property_owner_reviewed_and_self_consistent(self):
        payload = reviewed_property_address_presentations()

        self.assertEqual(
            payload["version"],
            "owner-reviewed-property-address-presentations-2026-08-11",
        )
        self.assertEqual(len(payload["presentations"]), 1)
        presentation = payload["presentations"][0]
        self.assertEqual(presentation["sourcePropertyId"], SOURCE_PROPERTY_ID)
        self.assertEqual(presentation["canonicalPropertyId"], CANONICAL_PROPERTY_ID)
        self.assertEqual(presentation["sourceFields"]["paon"], "33")
        self.assertEqual(
            presentation["canonicalFields"]["paon"],
            "COROMANDEL, 33",
        )
        self.assertEqual(presentation["review"]["status"], "owner-reviewed")

    def test_registry_rejects_ids_or_non_paon_fields_that_do_not_match(self):
        payload = json.loads(
            PROPERTY_ADDRESS_PRESENTATIONS_PATH.read_text(encoding="utf-8")
        )
        invalid_id = copy.deepcopy(payload)
        invalid_id["presentations"][0]["canonicalPropertyId"] = SOURCE_PROPERTY_ID

        changed_street = copy.deepcopy(payload)
        presentation = changed_street["presentations"][0]
        presentation["canonicalFields"]["street"] = "OTHER ROAD"
        presentation["canonicalPropertyId"] = (
            "property:COROMANDEL 33 OTHER ROAD COBHAM KT11 2JA|KT112JA"
        )

        for candidate, expected_message in (
            (invalid_id, "property IDs"),
            (changed_street, "may only add a reviewed name to PAON"),
        ):
            with self.subTest(expected_message=expected_message):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "presentations.json"
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected_message):
                        reviewed_property_address_presentations(path)

    def test_all_target_transactions_are_rekeyed_and_source_identity_is_retained(self):
        rows = [
            fairmile_transaction("lr-00000000000000000001", "2015-05-28", 2_000_000),
            fairmile_transaction("lr-00000000000000000002", "2025-05-28", 3_000_000),
        ]
        transaction_ids = [row["id"] for row in rows]

        canonical_rows, stats = canonicalise_property_addresses(rows)

        self.assertEqual([row["id"] for row in canonical_rows], transaction_ids)
        self.assertEqual(len(canonical_rows), len(rows))
        self.assertEqual(
            {row["address"] for row in canonical_rows},
            {"COROMANDEL, 33, FAIRMILE AVENUE, COBHAM, KT11 2JA"},
        )
        self.assertEqual({row["paon"] for row in canonical_rows}, {"COROMANDEL, 33"})
        self.assertEqual(
            {property_record_id(row) for row in canonical_rows},
            {CANONICAL_PROPERTY_ID},
        )
        self.assertEqual(stats["reviewedPresentationEntries"], 1)
        self.assertEqual(stats["reviewedPresentationProperties"], 1)
        self.assertEqual(stats["reviewedPresentationRows"], 2)
        self.assertEqual(stats["reviewedPresentationPropertiesRekeyed"], 1)
        self.assertEqual(stats["reviewedPresentationRowsRewritten"], 2)
        self.assertEqual(stats["sourceAddressVariantCount"], 1)
        self.assertEqual(
            stats["sourceAddressVariants"][CANONICAL_PROPERTY_ID],
            [{
                "propertyRecordId": SOURCE_PROPERTY_ID,
                "address": "33, FAIRMILE AVENUE, COBHAM, KT11 2JA",
                "postcode": "KT112JA",
            }],
        )

    def test_encountered_target_with_mismatched_structured_fields_fails_closed(self):
        row = fairmile_transaction(
            "lr-00000000000000000001",
            "2015-05-28",
            2_000_000,
        )
        # With no structured route, the original address still resolves to the
        # configured target ID. The presentation application must reject the
        # contradictory fields rather than silently trusting that address.
        row["street"] = ""

        with self.assertRaisesRegex(ValueError, "target fields do not match: street"):
            canonicalise_property_addresses([row])

    def test_writer_is_idempotent_and_rebases_existing_source_variants(self):
        rows = [
            fairmile_transaction("lr-00000000000000000001", "2015-05-28", 2_000_000),
            fairmile_transaction("lr-00000000000000000002", "2025-05-28", 3_000_000),
        ]
        prior_property_id = (
            "property:THIRTY THREE FAIRMILE AVENUE COBHAM KT11 2JA|KT112JA"
        )
        prior_address_stats = {
            "version": ADDRESS_CANONICALISATION_VERSION,
            "rows": 2,
            "canonicalProperties": 1,
            "sourceAddressIdentities": 2,
            "identityAliasesCollapsed": 1,
            "sourceAddressVariantProperties": 1,
            "sourceAddressVariantCount": 1,
            "sourceAddressVariants": {
                SOURCE_PROPERTY_ID: [{
                    "propertyRecordId": prior_property_id,
                    "address": "THIRTY THREE, FAIRMILE AVENUE, COBHAM, KT11 2JA",
                    "postcode": "KT112JA",
                }],
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.js"
            second = Path(directory) / "second.js"
            write_js(
                first,
                rows,
                {"addressCanonicalisation": prior_address_stats},
            )
            first_rows, _summary, first_meta = read_js(first)
            write_js(second, first_rows, first_meta)
            second_rows, _summary, second_meta = read_js(second)

        self.assertEqual(first_rows, second_rows)
        self.assertEqual(
            first_meta["addressCanonicalisation"],
            second_meta["addressCanonicalisation"],
        )
        self.assertEqual(
            {row["propertyRecordId"] for row in first_rows},
            {CANONICAL_PROPERTY_ID},
        )
        variants = first_meta["addressCanonicalisation"]["sourceAddressVariants"]
        self.assertEqual(
            {
                variant["propertyRecordId"]
                for variant in variants[CANONICAL_PROPERTY_ID]
            },
            {SOURCE_PROPERTY_ID, prior_property_id},
        )
        self.assertEqual(
            first_meta["addressCanonicalisation"][
                "reviewedPresentationPropertiesRekeyed"
            ],
            1,
        )

    def test_checked_in_target_resolves_to_the_reviewed_presentation(self):
        rows, _summary, _meta = read_js(ROOT / "outputs" / "surrey-transactions.js")
        target_rows = [
            row
            for row in rows
            if row.get("propertyRecordId")
            in {SOURCE_PROPERTY_ID, CANONICAL_PROPERTY_ID}
        ]

        self.assertGreaterEqual(len(target_rows), 1)
        self.assertEqual(
            {row["propertyRecordId"] for row in target_rows},
            {CANONICAL_PROPERTY_ID},
        )
        self.assertEqual(
            {row["address"] for row in target_rows},
            {"COROMANDEL, 33, FAIRMILE AVENUE, COBHAM, KT11 2JA"},
        )
        self.assertEqual(
            {row["paon"] for row in target_rows},
            {"COROMANDEL, 33"},
        )
        transaction_ids = {row["id"] for row in target_rows}
        canonical_rows, _stats = canonicalise_property_addresses(target_rows)
        self.assertEqual({row["id"] for row in canonical_rows}, transaction_ids)
        self.assertEqual(
            {property_record_id(row) for row in canonical_rows},
            {CANONICAL_PROPERTY_ID},
        )
        self.assertEqual(
            {canonical_display_address(row) for row in canonical_rows},
            {"COROMANDEL, 33, FAIRMILE AVENUE, COBHAM, KT11 2JA"},
        )


if __name__ == "__main__":
    unittest.main()
