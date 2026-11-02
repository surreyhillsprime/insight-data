import hashlib
import tempfile
import unittest
from pathlib import Path

from build_hmlr_mapping_packet import build_packet, legacy_mapping_candidates, source_property_key, lookup_bytes, verified_inputs


TX = "{00000000-0000-0000-0000-000000000001}"
TX2 = "{00000000-0000-0000-0000-000000000002}"
UPRN = "100000000001"


def ppd(tx=TX, status="A", price="3000000"):
    return [tx, price, "2026-06-01", "GU1 1AA", "D", "N", "F", "1", "", "TEST ROAD", "", "GUILDFORD", "GUILDFORD", "SURREY", "A", status]


class HmlrMappingPacketTests(unittest.TestCase):
    def fixtures(self):
        row = ppd()
        key = source_property_key(row)
        transactions = [{"id": "lr-legacy", "propertyRecordId": key}]
        parcels = {
            "source": {"sourceSnapshot": "hmlr-inspire-2026-09-06"},
            "releaseId": "inspire-parcels-fixture",
            "parcelsById": {"100": {}},
            "associationsByProperty": {key: {"parcelIds": ["100"], "matchMethod": "reviewed_indicative"}},
        }
        manifest = {"release": "2026-09", "publishedDate": "2026-10-28", "files": {}}
        return row, transactions, parcels, manifest

    def packet(self, rows=None, uprns=None, inspire=None, prior=None):
        row, transactions, parcels, manifest = self.fixtures()
        return build_packet(manifest, [row] if rows is None else rows,
                            {TX: [UPRN]} if uprns is None else uprns,
                            {TX: ["100", "200"]} if inspire is None else inspire,
                            transactions, prior or {}, parcels)

    def test_all_official_parcels_and_raw_source_cells_are_retained(self):
        row = self.packet()["officialTransactionCandidates"][0]
        self.assertEqual(row["inspireIds"], ["100", "200"])
        self.assertEqual(row["rawFields"], ppd())
        self.assertEqual(row["sourceNamespace"], "hmlr_ppd_transaction_uuid")
        self.assertEqual(row["geometryResolution"], "not_checked")

    def test_legacy_coverage_is_complete_without_assigning_official_uuid_or_old_extent(self):
        row, transactions, parcels, _ = self.fixtures()
        transactions.append({"id": "lr-unresolved", "propertyRecordId": "property:UNMATCHED|GU11AA"})
        result = legacy_mapping_candidates(transactions, parcels)
        self.assertEqual(len(result), 2)
        self.assertEqual([item["disposition"] for item in result], ["current_property_association", "unresolved"])
        self.assertTrue(all(not item["officialTransactionUuidAssigned"] for item in result))
        self.assertTrue(all(not item["historicalSaleExtentConfirmed"] for item in result))

    def test_changes_and_deletions_below_the_price_floor_remain_for_known_uuid(self):
        changed = ppd(status="C", price="1000000")
        changed[7] = "99"
        deleted = ppd(TX2, status="D", price="100")
        deleted[13] = "OTHER COUNTY"
        packet = self.packet([changed, deleted], uprns={}, inspire={}, prior={"transactions": {TX: {}, TX2: {}}})
        rows = packet["officialTransactionCandidates"]
        self.assertEqual([item["operation"] for item in rows], ["C", "D"])
        self.assertEqual(rows[1]["disposition"], "withdrawn_source_transaction")

    def test_lower_value_history_for_existing_exact_address_is_selected(self):
        packet = self.packet([ppd(price="500000")], inspire={})
        rows = packet["officialTransactionCandidates"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selectionReasons"], ["existing_exact_address_reference_candidate"])
        self.assertEqual(rows[0]["existingCanonicalIdentityAdmission"], "required")

    def test_missing_freehold_and_leasehold_links_have_explicit_different_outcomes(self):
        freehold = ppd()
        leasehold = ppd(TX2)
        leasehold[6] = "L"
        packet = self.packet([freehold, leasehold], inspire={}, uprns={})
        self.assertEqual([item["disposition"] for item in packet["officialTransactionCandidates"]],
                         ["not_supplied_by_hmlr", "outside_freehold_lookup_scope"])

    def test_duplicate_uuid_source_events_are_not_deduplicated_or_silently_ordered(self):
        packet = self.packet([ppd(), ppd(status="C")])
        rows = packet["officialTransactionCandidates"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(item["sameUuidSourceOccurrences"] == 2 for item in rows))
        self.assertTrue(all(item["sourceOperationOrdering"] == "review_required" for item in rows))

    def test_mixed_month_lookup_or_invalid_polygon_id_fails(self):
        with self.assertRaisesRegex(ValueError, "absent"):
            self.packet(inspire={TX2: ["100"]})
        with self.assertRaisesRegex(ValueError, "field width"):
            self.packet(inspire={TX: ["1000000000"]})

    def test_missing_geometry_does_not_become_a_legacy_mapping(self):
        _, transactions, parcels, _ = self.fixtures()
        parcels["parcelsById"] = {}
        with self.assertRaisesRegex(ValueError, "geometry"):
            legacy_mapping_candidates(transactions, parcels)

    def test_verified_lookup_bytes_preserve_multiple_parcels_but_reject_duplicate_pairs(self):
        raw = f'"{TX}","200"\n"{TX}","100"\n'.encode()
        self.assertEqual(lookup_bytes(raw, "inspire"), {TX: ["100", "200"]})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            lookup_bytes(raw + f'"{TX}","100"\n'.encode(), "inspire")

    def test_hash_and_same_publication_date_are_required_before_parsing_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest = {"release": "2026-09", "publishedDate": "2026-10-28", "files": {}}
            for kind, name in [("ppd", "pp-monthly-update-new-version.csv"), ("uprn", "pp-uprn-lookup-sep-2026.csv"), ("inspire", "pp-inspire-id-lookup-sep-2026.csv")]:
                raw = (kind + " verified bytes").encode()
                (path / name).write_bytes(raw)
                manifest["files"][kind] = {"file": name, "url": "https://price-paid-data.publicdata.landregistry.gov.uk/" + name,
                                           "sha256": hashlib.sha256(raw).hexdigest(), "lastModified": "Wed, 28 Oct 2026 10:12:02 GMT"}
            verified = verified_inputs(path, manifest)
            self.assertEqual(verified["ppd"], b"ppd verified bytes")
            manifest["files"]["uprn"]["lastModified"] = "Thu, 29 Oct 2026 10:12:02 GMT"
            with self.assertRaisesRegex(ValueError, "publication date"):
                verified_inputs(path, manifest)
            (path / manifest["files"]["ppd"]["file"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verified_inputs(path, manifest)

    def test_changed_geometry_hold_withholds_active_ids_and_preserves_prior_evidence(self):
        _, transactions, parcels, _ = self.fixtures()
        holds = {"records": [{"propertyReference": transactions[0]["propertyRecordId"], "inspireId": "100",
                               "fromSourceSnapshot": "hmlr-inspire-2026-09-06", "reason": "source split requires independent identity review"}]}
        result = legacy_mapping_candidates(transactions, parcels, holds)[0]
        self.assertEqual(result["inspireIds"], [])
        self.assertEqual(result["heldPriorInspireIds"], ["100"])
        self.assertEqual(result["disposition"], "review_required_geometry_change")
        holds["records"][0]["fromSourceSnapshot"] = "hmlr-inspire-2026-11-01"
        self.assertEqual(legacy_mapping_candidates(transactions, parcels, holds)[0]["disposition"], "current_property_association")


if __name__ == "__main__":
    unittest.main()
