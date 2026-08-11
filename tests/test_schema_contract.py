import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from insight_data_utils import (  # noqa: E402
    ADDRESS_CANONICALISATION_VERSION,
    FEED_SCHEMA_VERSION,
    PROPERTY_RECORD_SCHEMA_VERSION,
    canonicalise_property_addresses,
    property_record_id,
    read_js,
    reviewed_property_address_aliases,
    write_js,
)
from migrate_feed_schema import migrate_transaction  # noqa: E402
from property_records import property_record_id as record_property_id  # noqa: E402


def transaction(**overrides):
    row = {
        "id": "legacy-id",
        "address": "1 Test Road, Esher, KT10 0AA",
        "postcode": "KT10 0AA",
        "market": "elmbridge-prime",
        "district": "Elmbridge",
        "price": 2_000_000,
        "date": "2026-01-01",
        "propertyType": "Detached",
        "category": "A",
    }
    row.update(overrides)
    return row


class FeedSchemaContractTests(unittest.TestCase):
    def test_chimneys_redundant_locality_is_one_property(self):
        rows = [
            {
                "address": "CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, WEYBRIDGE, KT13 0QF",
                "paon": "CHIMNEYS",
                "saon": "",
                "street": "YAFFLE ROAD",
                "locality": "WEYBRIDGE",
                "town": "WEYBRIDGE",
                "postcode": "KT13 0QF",
                "date": "2004-07-16",
            },
            {
                "address": "CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, KT13 0QF",
                "paon": "CHIMNEYS",
                "saon": "",
                "street": "YAFFLE ROAD",
                "locality": "",
                "town": "WEYBRIDGE",
                "postcode": "KT13 0QF",
                "date": "2020-09-21",
            },
        ]

        canonical, stats = canonicalise_property_addresses(rows)

        self.assertEqual(
            {row["address"] for row in canonical},
            {"CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, KT13 0QF"},
        )
        self.assertEqual({row["locality"] for row in canonical}, {""})
        self.assertEqual(len({property_record_id(row) for row in canonical}), 1)
        self.assertEqual(stats["version"], ADDRESS_CANONICALISATION_VERSION)
        self.assertEqual(stats["identityAliasesCollapsed"], 1)

    def test_guarded_numeric_alias_keeps_complete_number_signature(self):
        rows = [
            {
                "address": "LE CHENE 12, TEST ROAD, ESHER, KT10 0AA",
                "paon": "LE CHENE, 12",
                "saon": "",
                "street": "TEST ROAD",
                "locality": "",
                "town": "ESHER",
                "postcode": "KT10 0AA",
                "date": "2010-01-01",
            },
            {
                "address": "PLOT 1 12, TEST ROAD, ESHER, KT10 0AA",
                "paon": "PLOT 1, 12",
                "saon": "",
                "street": "TEST ROAD",
                "locality": "",
                "town": "ESHER",
                "postcode": "KT10 0AA",
                "date": "2020-01-01",
            },
        ]

        canonical, _stats = canonicalise_property_addresses(rows)

        self.assertEqual(len({property_record_id(row) for row in canonical}), 2)

    def test_reviewed_alias_registry_is_small_and_disjoint(self):
        payload = reviewed_property_address_aliases()
        members = [
            member
            for group in payload["groups"]
            for member in group["members"]
        ]
        self.assertEqual(payload["version"], "reviewed-property-address-aliases-2026-08-05")
        self.assertEqual(
            payload["contentVersion"],
            "reviewed-property-address-alias-content-2026-08-11",
        )
        self.assertEqual(len(payload["groups"]), 29)
        self.assertEqual(len(members), len(set(members)))

    def test_checked_in_feed_publishes_complete_source_variant_ledger(self):
        rows, _summary, metadata = read_js(
            ROOT / "outputs" / "surrey-transactions.js"
        )
        address_meta = metadata["addressCanonicalisation"]
        canonical_property_ids = {row["propertyRecordId"] for row in rows}
        self.assertEqual(address_meta["rows"], len(rows))
        self.assertEqual(
            metadata["canonicalPropertyRecords"],
            len(canonical_property_ids),
        )
        self.assertEqual(
            address_meta["canonicalProperties"],
            len(canonical_property_ids),
        )
        self.assertEqual(
            address_meta["sourceAddressIdentities"],
            address_meta["canonicalProperties"]
            + address_meta["identityAliasesCollapsed"],
        )
        self.assertGreater(address_meta["identityAliasesCollapsed"], 0)
        self.assertEqual(
            address_meta["sourceAddressVariantProperties"],
            len(address_meta["sourceAddressVariants"]),
        )
        self.assertGreater(address_meta["sourceAddressVariantCount"], 0)
        self.assertEqual(
            address_meta["sourceAddressVariantCount"],
            sum(
                len(variants)
                for variants in address_meta["sourceAddressVariants"].values()
            ),
        )
        chimneys = [
            row for row in rows
            if "CHIMNEYS" in row.get("address", "")
            and "YAFFLE ROAD" in row.get("address", "")
        ]
        self.assertGreaterEqual(len(chimneys), 2)
        self.assertEqual(
            {row["propertyRecordId"] for row in chimneys},
            {"property:CHIMNEYS YAFFLE ROAD WEYBRIDGE KT13 0QF|KT130QF"},
        )
        self.assertEqual(
            {row["address"] for row in chimneys},
            {"CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, KT13 0QF"},
        )

    def test_repeat_writer_preserves_richest_pre_rewrite_variant_ledger(self):
        rows = [
            {
                "id": "lr-00000000000000000001",
                "address": "30, TEST ROAD, ESHER, KT10 0AA",
                "paon": "30",
                "saon": "",
                "street": "TEST ROAD",
                "locality": "",
                "town": "ESHER",
                "postcode": "KT10 0AA",
                "market": "elmbridge-prime",
                "price": 3_000_000,
                "date": "2010-01-01",
            },
            {
                "id": "lr-00000000000000000002",
                "address": "TOROSA, 30, TEST ROAD, ESHER, KT10 0AA",
                "paon": "TOROSA, 30",
                "saon": "",
                "street": "TEST ROAD",
                "locality": "",
                "town": "ESHER",
                "postcode": "KT10 0AA",
                "market": "elmbridge-prime",
                "price": 4_000_000,
                "date": "2020-01-01",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.js"
            second = Path(directory) / "second.js"
            write_js(first, rows, {})
            canonical_rows, _summary, first_meta = read_js(first)
            rewritten_rows, rewritten_stats = canonicalise_property_addresses(
                canonical_rows
            )
            self.assertEqual(rewritten_stats["sourceAddressVariantCount"], 0)
            write_js(
                second,
                rewritten_rows,
                first_meta,
                address_stats=rewritten_stats,
            )
            _rows, _summary, second_meta = read_js(second)

        self.assertEqual(
            second_meta["addressCanonicalisation"],
            first_meta["addressCanonicalisation"],
        )

    def test_shared_writer_upgrades_legacy_metadata_and_property_identity(self):
        row = transaction(uprn="approximate-postcode-centroid")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            write_js(output, [row], {"schemaVersion": 2})
            rows, _summary, metadata = read_js(output)

        self.assertEqual(FEED_SCHEMA_VERSION, 3)
        self.assertEqual(metadata["schemaVersion"], FEED_SCHEMA_VERSION)
        self.assertEqual(metadata["propertyRecordSchemaVersion"], PROPERTY_RECORD_SCHEMA_VERSION)
        self.assertEqual(metadata["canonicalPropertyRecords"], 1)
        self.assertEqual(
            metadata["propertyIdentityMode"],
            "full-normalised-address-plus-postcode-fail-closed",
        )
        self.assertEqual(rows[0]["propertyRecordId"], property_record_id(row))
        self.assertNotIn("approximate-postcode-centroid", rows[0]["propertyRecordId"])

    def test_missing_postcode_fails_closed_instead_of_merging(self):
        row = transaction(address="The Old Grove, High Pitfold, Hindhead", postcode="")
        self.assertEqual(
            property_record_id(row),
            "property:THE OLD GROVE HIGH PITFOLD HINDHEAD|NOPOSTCODE",
        )

    def test_migration_recomputes_stable_transaction_and_property_ids(self):
        row = transaction(
            geocode={"source": "Postcodes.io", "precision": "Postcode centroid"},
        )
        migrated = migrate_transaction(row)
        self.assertRegex(migrated["id"], r"^lr-[0-9a-f]{20}$")
        self.assertEqual(migrated["propertyRecordId"], property_record_id(row))
        self.assertEqual(migrated["coordinateSource"], "Postcodes.io")
        self.assertEqual(migrated["coordinatePrecision"], "postcode-centroid")

    def test_current_schema_migration_can_preserve_existing_row_shape(self):
        row = transaction(
            geocode={"source": "Postcodes.io", "precision": "Postcode centroid"},
        )

        migrated = migrate_transaction(
            row,
            backfill_coordinate_provenance=False,
        )

        self.assertNotIn("coordinateSource", migrated)
        self.assertNotIn("coordinatePrecision", migrated)

    def test_migration_preserves_existing_stable_transaction_id(self):
        row = transaction(id="lr-1234567890abcdef1234")
        self.assertEqual(
            migrate_transaction(row)["id"],
            "lr-1234567890abcdef1234",
        )

    def test_property_record_builder_trusts_published_canonical_identity(self):
        canonical_id = "property:CHIMNEYS YAFFLE ROAD WEYBRIDGE KT13 0QF|KT130QF"
        row = {
            "propertyRecordId": canonical_id,
            "address": "CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, WEYBRIDGE, KT13 0QF",
            "postcode": "KT13 0QF",
        }
        self.assertEqual(record_property_id(row), canonical_id)

    def test_independent_enrichers_publish_through_canonical_writer(self):
        for filename in ("enrich_epc_data.py", "enrich_property_context.py"):
            source = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
            self.assertIn("from insight_data_utils import write_js as write_canonical_js", source)
            self.assertTrue(
                "write_canonical_js(args.write_js, enriched, meta)" in source
                or "write_canonical_js_atomic(args.write_js, enriched, meta)" in source
            )


if __name__ == "__main__":
    unittest.main()
