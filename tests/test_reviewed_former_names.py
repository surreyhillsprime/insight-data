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
    PROPERTY_ADDRESS_ALIASES_PATH,
    read_js,
    reviewed_alias_content_metadata,
    reviewed_former_name_members_by_canonical_property,
    reviewed_former_name_metadata,
    reviewed_property_address_aliases,
    write_js,
)


APPROVED_NEW_LINEAGES = {
    "property:ARASAN MANOR RAVENSCROFT ROAD WEYBRIDGE KT13 0NX|KT130NX":
        "property:CHILTON HOUSE RAVENSCROFT ROAD WEYBRIDGE KT13 0NX|KT130NX",
    "property:BROOKE HOUSE QUEENS DRIVE OXSHOTT LEATHERHEAD KT22 0PF|KT220PF":
        "property:BALMORAL QUEENS DRIVE OXSHOTT LEATHERHEAD KT22 0PF|KT220PF",
    "property:CHAPTERS OLD AVENUE WEYBRIDGE KT13 0QB|KT130QB":
        "property:FORTUNE MANOR OLD AVENUE WEYBRIDGE KT13 0QB|KT130QB",
    "property:DUNMORE WEST ROAD WEYBRIDGE KT13 0LZ|KT130LZ":
        "property:THIRLSTONE WEST ROAD ST GEORGES HILL WEYBRIDGE KT13 0LZ|KT130LZ",
}


class ReviewedFormerNameTests(unittest.TestCase):
    def test_registry_marks_only_explicit_retired_identity_members(self):
        registry = reviewed_property_address_aliases()
        former_members = reviewed_former_name_members_by_canonical_property()

        self.assertEqual(registry["version"], "reviewed-property-address-aliases-2026-08-05")
        self.assertEqual(
            registry["contentVersion"],
            "reviewed-property-address-alias-content-2026-08-11",
        )
        self.assertEqual(
            registry["formerNamesVersion"],
            "reviewed-former-property-names-2026-08-11",
        )
        self.assertEqual(len(registry["groups"]), 29)
        self.assertEqual(
            reviewed_alias_content_metadata(
                registry,
                {group["canonicalPropertyId"] for group in registry["groups"]},
            )["reviewedAliasContentFingerprint"],
            "299a7b7312915df84fb767bb70c878c1b4b70f3f066d508c8d85801397d57c10",
        )
        self.assertEqual(len(former_members), 23)
        self.assertEqual(sum(map(len, former_members.values())), 24)
        for canonical_id, former_ids in former_members.items():
            group = next(
                item
                for item in registry["groups"]
                if item["canonicalPropertyId"] == canonical_id
            )
            self.assertNotIn(canonical_id, former_ids)
            self.assertTrue(set(former_ids).issubset(group["members"]))
        for canonical_id, former_id in APPROVED_NEW_LINEAGES.items():
            self.assertEqual(former_members[canonical_id], [former_id])

    def test_registry_rejects_duplicate_canonical_or_unknown_former_members(self):
        payload = json.loads(
            PROPERTY_ADDRESS_ALIASES_PATH.read_text(encoding="utf-8")
        )
        candidate_groups = [
            item for item in payload["groups"] if item.get("formerNameMembers")
        ]
        invalid_payloads = []
        for values in (
            [candidate_groups[0]["canonicalPropertyId"]],
            ["property:UNKNOWN HOUSE TEST ROAD ESHER KT10 0AA|KT100AA"],
            [candidate_groups[0]["formerNameMembers"][0]] * 2,
        ):
            candidate = copy.deepcopy(payload)
            target = next(
                item
                for item in candidate["groups"]
                if item["id"] == candidate_groups[0]["id"]
            )
            target["formerNameMembers"] = values
            invalid_payloads.append(candidate)

        for candidate in invalid_payloads:
            with self.subTest(values=candidate["groups"][6]["formerNameMembers"]):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "aliases.json"
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "former-name members"):
                        reviewed_property_address_aliases(path)

    def test_checked_in_metadata_exactly_matches_registry_and_source_ledger(self):
        rows, _summary, metadata = read_js(
            ROOT / "outputs" / "surrey-transactions.js"
        )
        canonical_ids = {row["propertyRecordId"] for row in rows}
        address_meta = metadata["addressCanonicalisation"]
        expected = reviewed_former_name_metadata(
            reviewed_property_address_aliases(),
            canonical_ids,
        )
        expected.update(
            reviewed_alias_content_metadata(
                reviewed_property_address_aliases(),
                canonical_ids,
            )
        )
        for key, value in expected.items():
            self.assertEqual(address_meta[key], value)

        variants = address_meta["sourceAddressVariants"]
        for canonical_id, former_ids in address_meta[
            "reviewedFormerNameMembers"
        ].items():
            retired_ids = {
                item["propertyRecordId"] for item in variants[canonical_id]
            }
            self.assertTrue(set(former_ids).issubset(retired_ids))
        for canonical_id, former_id in APPROVED_NEW_LINEAGES.items():
            self.assertIn(canonical_id, canonical_ids)
            self.assertNotIn(former_id, canonical_ids)

    def test_canonical_reduction_preserves_the_richer_prior_identity_ledger(self):
        rows = [
            {
                "id": "lr-00000000000000000001",
                "propertyRecordId": "property:DUNMORE WEST ROAD WEYBRIDGE KT13 0LZ|KT130LZ",
                "address": "DUNMORE, WEST ROAD, WEYBRIDGE, KT13 0LZ",
                "paon": "DUNMORE",
                "saon": "",
                "street": "WEST ROAD",
                "locality": "",
                "town": "WEYBRIDGE",
                "postcode": "KT13 0LZ",
                "market": "elmbridge-prime",
                "district": "Elmbridge",
                "price": 11_750_000,
                "date": "2024-05-31",
                "propertyType": "Detached",
                "category": "A",
            },
            {
                "id": "lr-00000000000000000002",
                "propertyRecordId": APPROVED_NEW_LINEAGES[
                    "property:DUNMORE WEST ROAD WEYBRIDGE KT13 0LZ|KT130LZ"
                ],
                "address": "THIRLSTONE, WEST ROAD, ST GEORGES HILL, WEYBRIDGE, KT13 0LZ",
                "paon": "THIRLSTONE",
                "saon": "",
                "street": "WEST ROAD",
                "locality": "ST GEORGES HILL",
                "town": "WEYBRIDGE",
                "postcode": "KT13 0LZ",
                "market": "elmbridge-prime",
                "district": "Elmbridge",
                "price": 3_625_000,
                "date": "2021-07-12",
                "propertyType": "Detached",
                "category": "A",
            },
        ]
        thirlstone_id = rows[1]["propertyRecordId"]
        prior_legacy_id = (
            "property:THIRLSTONE HOUSE WEST ROAD WEYBRIDGE KT13 0LZ|KT130LZ"
        )
        prior_meta = {
            "addressCanonicalisation": {
                "version": ADDRESS_CANONICALISATION_VERSION,
                "rows": 2,
                "canonicalProperties": 2,
                "sourceAddressIdentities": 3,
                "identityAliasesCollapsed": 1,
                "sourceAddressVariantProperties": 1,
                "sourceAddressVariantCount": 1,
                "sourceAddressVariants": {
                    thirlstone_id: [{
                        "propertyRecordId": prior_legacy_id,
                        "address": "THIRLSTONE HOUSE, WEST ROAD, WEYBRIDGE, KT13 0LZ",
                        "postcode": "KT130LZ",
                    }],
                },
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            write_js(output, rows, prior_meta)
            canonical_rows, _summary, metadata = read_js(output)

        canonical_id = (
            "property:DUNMORE WEST ROAD WEYBRIDGE KT13 0LZ|KT130LZ"
        )
        stats = metadata["addressCanonicalisation"]
        self.assertEqual(
            {row["propertyRecordId"] for row in canonical_rows},
            {canonical_id},
        )
        self.assertEqual(stats["canonicalProperties"], 1)
        self.assertEqual(stats["sourceAddressIdentities"], 3)
        self.assertEqual(stats["identityAliasesCollapsed"], 2)
        self.assertEqual(
            {
                variant["propertyRecordId"]
                for variant in stats["sourceAddressVariants"][canonical_id]
            },
            {thirlstone_id, prior_legacy_id},
        )
        self.assertEqual(
            stats["reviewedFormerNameMembers"][canonical_id],
            [thirlstone_id],
        )

    def test_planning_only_names_do_not_gain_sales_identity_authority(self):
        registry_text = PROPERTY_ADDRESS_ALIASES_PATH.read_text(encoding="utf-8")
        self.assertNotIn("WITS END", registry_text)
        self.assertNotIn("TWO WAYS", registry_text)


if __name__ == "__main__":
    unittest.main()
