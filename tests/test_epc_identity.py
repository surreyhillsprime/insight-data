"""Offline identity/admission tests; all certificate identifiers are synthetic."""

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import enrich_epc_data as epc


def sale(paon="2", street="HIGH STREET", saon="", town="BAGSHOT", postcode="GU19 5AE", **extra):
    return {"id": "sale-1", "paon": paon, "saon": saon, "street": street,
            "town": town, "locality": "", "postcode": postcode,
            "address": ", ".join(value for value in [saon, paon, street, town, postcode] if value),
            "price": 3_000_000, "date": "2025-01-01", **extra}


def record(address, area=200, number="synthetic-certificate-1", date="2024-01-01", **extra):
    return {"status": "matched", "searchedAt": "2026-07-07T12:00:00Z", "epc": {
        "epcMatched": True, "epcAddress": address, "epcCertificateNumber": number,
        "epcRegistrationDate": date, "epcRating": "D", "epcSource": "MHCLG EPC Register",
        "floorAreaSqm": area, "floorAreaSqft": round(area * epc.SQM_TO_SQFT),
        "pricePerSqft": 99999, "epcMatchScore": 1.0, **extra}}


def args(**extra):
    return SimpleNamespace(limit=0, max_run_minutes=0, refresh_days=90, page_size=10,
                           min_score=.55, max_certificate_fetches=8, pause=0,
                           max_errors=25, fail_if_no_matches_after=0, progress_every=100, **extra)


class EPCIdentityTests(unittest.TestCase):
    def assert_identity(self, transaction, address, allowed):
        candidate = {"addressLine1": address}
        self.assertEqual(epc.delivery_identity(transaction, candidate)[0], allowed)
        if not allowed:
            self.assertEqual(epc.address_score(transaction, candidate), 0)

    def test_confirmed_wrong_flat_primary_number_and_named_annexe(self):
        self.assert_identity(sale("KINGSBRIDGE HOUSE, 14", "THAMES STREET", "FLAT 21",
                                  "STAINES-UPON-THAMES", "TW18 4RB"),
                             "Flat 8, Kingsbridge House, 14 Thames Street, STAINES-UPON-THAMES, TW18 4RB", False)
        self.assert_identity(sale(), "8, High Street, BAGSHOT, GU19 5AE", False)
        target = sale("WOODSIDE HOUSE", "COCKCROW HILL", town="SURBITON", postcode="KT6 5HE", locality="LONG DITTON")
        self.assert_identity(target, "The Annexe, The Long House, Cockcrow Hill, St.Mary's Road, Long Ditton, SURBITON, KT6 5HE", False)
        self.assert_identity(target, "Woodside House, Cockcrow Hill, St. Marys Road, SURBITON, KT6 5HE", True)

    def test_single_digit_neighbours_never_clear_score_or_cache_guard(self):
        for primary, wrong, road in [("7", "6", "FORT ROAD"), ("5", "9", "FOX WOOD"), ("4", "5", "ASHCROFT PARK")]:
            with self.subTest(road=road):
                target = sale(primary, road)
                cached = record(f"{wrong}, {road}, BAGSHOT, GU19 5AE")
                self.assert_identity(target, cached["epc"]["epcAddress"], False)
                self.assertFalse(epc.cache_record_is_fresh(cached, 90, target))

    def test_complete_letters_unit_ranges_and_secondary_numbers_are_identity(self):
        for paon, saon, candidate, allowed in [
            ("1A", "", "1A HIGH STREET", True), ("1A", "", "1 HIGH STREET", False),
            ("1A", "", "1B HIGH STREET", False), ("1", "FLAT A", "FLAT B 1 HIGH STREET", False),
            ("1", "FLAT A", "APARTMENT A 1 HIGH STREET", True),
            ("1-3", "", "1–3 HIGH STREET", True), ("1-3", "", "1 HIGH STREET", False),
            ("1-3", "", "1 3 HIGH STREET", False), ("14", "PLOT 1", "14 HIGH STREET", False),
            ("14", "", "FLAT 1 14 HIGH STREET", False), ("14", "FLAT 1", "14 HIGH STREET", False),
        ]:
            with self.subTest(paon=paon, saon=saon, candidate=candidate):
                self.assert_identity(sale(paon, saon=saon), candidate + ", BAGSHOT, GU19 5AE", allowed)

    def test_benign_street_punctuation_locality_and_position_bounded_the(self):
        target = sale("THE WILLOWS", "ST MARY'S ROAD", town="SURBITON", postcode="KT6 5HE", locality="LONG DITTON")
        self.assert_identity(target, "Willows, St. Marys Rd., Long Ditton, SURBITON KT6 5HE", True)
        self.assert_identity(target, "The Willows, St Marys Road, KT6 5HE", True)
        self.assert_identity(target, "Willows Annexe, St Marys Road, KT6 5HE", False)
        self.assert_identity(sale("OLD THE BARN"), "OLD BARN, HIGH STREET GU19 5AE", False)

    def test_postcode_missing_conflicting_or_multiple_fails_closed(self):
        for address in ["2 HIGH STREET", "2 HIGH STREET GU19 5AB", "2 HIGH STREET GU19 5AE GU19 5AB"]:
            self.assert_identity(sale(), address, False)
        self.assert_identity(sale(postcode=""), "2 HIGH STREET GU19 5AE", False)

    def test_street_inside_house_name_requires_the_complete_exact_delivery_prefix(self):
        for name, street, locality in [("PARKLANDS HOUSE", "PARKLANDS", ""),
                                       ("COMBE LANE FARM", "COMBE LANE", ""),
                                       ("FOREST GREEN HOUSE", "", "FOREST GREEN")]:
            with self.subTest(name=name):
                target = sale(name, street, locality=locality)
                road = street or locality
                address = f"{name}, {road}, BAGSHOT, GU19 5AE"
                self.assert_identity(target, address, True)
                self.assert_identity(target, f"OTHER {name}, {road}, BAGSHOT, GU19 5AE", False)
                for extent in ["ANNEXE", "FLAT 1", "4"]:
                    self.assert_identity(target, f"{name}, {road}, {extent}, BAGSHOT, GU19 5AE", False)
                rows, _cache, report = epc.revalidate_retained_cache([target], {"records": {"retained": record(address)}})
                self.assertTrue(rows[0]["epcMatched"])
                self.assertEqual(report["counts"], {"replaced": 1})

    def test_suffix_unit_or_annexe_cannot_be_discarded_as_locality_context(self):
        for suffix in ["Annexe", "Flat 1", "Unit B", "Room 1", "Plot 2", "Suite A", "Maisonette", "Apartment 3", "Ground Floor", "Basement", "Rear", "Penthouse", "Outbuilding", "Stable Block", "Garage", "East Wing", "Cottage", "Lodge", "Barn", "Bungalow", "Detached Building"]:
            with self.subTest(suffix=suffix):
                address = f"2 High Street, {suffix}, Bagshot, GU19 5AE"
                self.assert_identity(sale(), address, False)
                cached = record(address)
                self.assertFalse(epc.cache_record_is_fresh(cached, 90, sale()))
                rows, _, report = epc.revalidate_retained_cache([sale()], {"records": {"retained": cached}})
                self.assertFalse(rows[0]["epcMatched"])
                self.assertEqual(report["decisions"][0]["candidateRejections"], {"unaccounted_suffix_unit_or_extent": 1})
        for suffix in ["8", "1A", "3-5"]:
            self.assert_identity(sale(), f"2 High Street, {suffix}, BAGSHOT GU19 5AE", False)

    def test_complete_suffix_requires_declared_or_specifically_reviewed_context(self):
        for unexplained in ["Coach House", "Upper", "Studio", "Loft", "Different Village", "Unknown Road"]:
            self.assert_identity(sale(), f"2 High Street, {unexplained}, Bagshot, GU19 5AE", False)
        target = sale(locality="THE SANDS", district="SURREY HEATH")
        self.assert_identity(target, "2 High Street, The Sands, Bagshot, Surrey Heath, Surrey, GU19 5AE", True)
        self.assert_identity(target, "2 High Street, The Sands, Coach House, Bagshot, GU19 5AE", False)
        target = sale("WOODSIDE HOUSE", "COCKCROW HILL", town="SURBITON", postcode="KT6 5HE", locality="LONG DITTON")
        self.assert_identity(target, "Woodside House, Cockcrow Hill, St Marys Road, Long Ditton, Surbiton, KT6 5HE", True)
        self.assert_identity(target, "Woodside House, Cockcrow Hill, Unknown Road, Long Ditton, Surbiton, KT6 5HE", False)
        self.assert_identity(target, "Woodside House, Cockcrow Hill, St Marys Road, Studio, Surbiton, KT6 5HE", False)
        other = sale("WOODSIDE HOUSE", "HIGH STREET", town="SURBITON", postcode="KT6 5HE")
        self.assert_identity(other, "Woodside House, High Street, St Marys Road, Surbiton, KT6 5HE", False)
        other = {**target, "postcode": "KT6 5HF"}
        self.assert_identity(other, "Woodside House, Cockcrow Hill, St Marys Road, Surbiton, KT6 5HF", False)

    def test_live_search_rejects_wrong_identity_before_fetch_even_with_zero_threshold(self):
        wrong = {"certificateNumber": "wrong", "addressLine1": "8 HIGH STREET GU19 5AE", "totalFloorArea": 100}
        with patch.object(epc, "search_candidates", return_value=[wrong]), patch.object(epc, "fetch_certificate") as fetch:
            result = epc.best_epc_match(sale(), "unused", 10, 0, 8)
        self.assertEqual(result["status"], "no_match")
        fetch.assert_not_called()

    def test_full_certificate_cannot_change_identity_after_exact_search_hit(self):
        exact = {"certificateNumber": "exact", "addressLine1": "2 HIGH STREET GU19 5AE"}
        wrong = {"certificateNumber": "exact", "addressLine1": "8 HIGH STREET GU19 5AE", "totalFloorArea": 100}
        with patch.object(epc, "search_candidates", return_value=[exact]), patch.object(epc, "fetch_certificate", return_value=wrong):
            result = epc.best_epc_match(sale(), "unused", 10, 0, 8)
        self.assertEqual(result["status"], "no_match")

    def test_matched_cache_is_not_fresh_without_certificate_identity_and_valid_area(self):
        exact = record("2 HIGH STREET GU19 5AE")
        self.assertTrue(epc.cache_record_is_fresh(exact, 90, sale()))
        self.assertFalse(epc.cache_record_is_fresh(exact, 90))
        self.assertFalse(epc.cache_record_is_fresh(exact, 90, sale("8")))
        for field, value in [("epcAddress", ""), ("epcCertificateNumber", ""), ("epcRegistrationDate", "invalid"), ("epcRegistrationDate", "2024-1-1"), ("floorAreaSqm", None), ("epcRating", []), ("epcRating", "84")]:
            broken = copy.deepcopy(exact); broken["epc"][field] = value
            self.assertFalse(epc.cache_record_is_fresh(broken, 90, sale()))

    def test_revalidation_recovers_exact_older_key_and_recalculates_each_sale(self):
        target = sale("WOODSIDE HOUSE", "COCKCROW HILL", town="SURBITON", postcode="KT6 5HE")
        repeated = {**target, "id": "sale-2", "price": 4_000_000, "date": "2026-01-01"}
        wrong = record("THE ANNEXE THE LONG HOUSE COCKCROW HILL KT6 5HE", 33, "wrong")
        exact = record("WOODSIDE HOUSE COCKCROW HILL ST MARYS ROAD KT6 5HE", 409, "exact", "2016-11-15")
        cache = {"records": {epc.stable_transaction_key(target): wrong, "old-locality-key": exact}}
        before = copy.deepcopy((cache, [target, repeated]))
        with patch.object(epc, "request_json", side_effect=AssertionError("Offline means no API")):
            rows, reviewed, report = epc.revalidate_retained_cache([target, repeated], cache)
        self.assertEqual((cache, [target, repeated]), before)
        self.assertEqual(report["counts"], {"replaced": 2})
        self.assertEqual(report["sourceChecksPerformed"], 0)
        for old, row in zip([target, repeated], rows):
            self.assertEqual(row["floorAreaSqm"], 409)
            self.assertEqual(row["pricePerSqft"], round(old["price"] / round(409 * epc.SQM_TO_SQFT)))
            self.assertEqual({k: v for k, v in row.items() if k not in epc.PUBLIC_EPC_FIELDS}, old)
            self.assertNotIn("epcCertificateNumber", row)
            self.assertEqual(reviewed["records"][epc.stable_transaction_key(old)]["searchedAt"], exact["searchedAt"])

    def test_selects_latest_exact_not_newer_neighbour_and_deduplicates_certificate(self):
        records = {"old": record("2 HIGH STREET GU19 5AE", 200, "old", "2020-01-01"),
                   "new": record("2 HIGH STREET GU19 5AE", 220, "new", "2025-01-01"),
                   "wrong": record("8 HIGH STREET GU19 5AE", 300, "wrong", "2026-01-01")}
        records["repeat"] = copy.deepcopy(records["new"]); records["repeat"]["epc"]["pricePerSqft"] = 23
        rows, _cache, report = epc.revalidate_retained_cache([sale()], {"records": records})
        self.assertEqual(rows[0]["floorAreaSqm"], 220)
        self.assertEqual(report["decisions"][0]["eligibleRetainedCertificates"], 2)

    def test_contradictory_same_id_or_same_latest_day_facts_are_unknown(self):
        for number in ["same", "different"]:
            records = {"a": record("2 HIGH STREET GU19 5AE", 200, "same"),
                       "b": record("2 HIGH STREET GU19 5AE", 201, number)}
            rows, _cache, report = epc.revalidate_retained_cache([sale()], {"records": records})
            self.assertFalse(rows[0]["epcMatched"])
            self.assertNotIn("floorAreaSqm", rows[0])
            self.assertEqual(report["counts"], {"unknown": 1})

    def test_duplicate_conflicting_certificate_cannot_bypass_normal_cache_reuse(self):
        target = sale()
        cache = {"records": {epc.stable_transaction_key(target): record("2 HIGH STREET GU19 5AE"),
                             "other-target": record("8 HIGH STREET GU19 5AE")}}
        rows, _, _, _ = epc.enrich_transactions([target], cache, "", args())
        self.assertFalse(rows[0]["epcMatched"])
        self.assertEqual(epc.terminal_cache_accounting([target], cache, 90)["resolved"], 0)

    def test_unknown_review_distinguishes_names_units_numbers_and_annexe(self):
        for target, address, expected in [
            (sale(), "8 HIGH STREET GU19 5AE", "primary_number_conflict_or_missing"),
            (sale("2", saon="FLAT 1"), "FLAT 2 2 HIGH STREET GU19 5AE", "unit_identifier_conflict"),
            (sale("WILLOWS"), "ANNEXE WILLOWS HIGH STREET GU19 5AE", "annexe_extent_conflict"),
            (sale("WILLOWS"), "OLD BIRCH HOUSE HIGH STREET GU19 5AE", "unresolved_named_identity_or_alias"),
        ]:
            cache = {"records": {epc.stable_transaction_key(target): record(address)}}
            _rows, _cache, report = epc.revalidate_retained_cache([target], cache)
            self.assertEqual(report["decisions"][0]["previousMatchIdentity"], expected)
            self.assertEqual(report["decisions"][0]["candidateRejections"], {expected: 1})

    def test_limits_and_abort_cannot_republish_unvalidated_unprocessed_rows(self):
        first, second = sale(), sale("8", id="sale-2", floorAreaSqm=200, pricePerSqft=99999)
        run_args = args(); run_args.limit = 1
        rows, _, _, _ = epc.enrich_transactions([first, second], {"records": {}}, "", run_args)
        self.assertTrue(all(row["epcMatched"] is False for row in rows))
        self.assertNotIn("pricePerSqft", rows[1])
        run_args.limit = 0; run_args.fail_if_no_matches_after = 1
        rows, _, _, reason = epc.enrich_transactions([first, second], {"records": {}}, "", run_args)
        self.assertTrue(reason)
        self.assertNotIn("floorAreaSqm", rows[1])

    def test_matching_counts_cannot_skip_repair_of_wrong_published_tuple(self):
        target = sale(floorAreaSqm=33, floorAreaSqft=355, pricePerSqft=8451, epcMatched=True)
        cache = {"records": {epc.stable_transaction_key(target): record("2 HIGH STREET GU19 5AE", 409)}}
        self.assertEqual(epc.terminal_cache_accounting([target], cache, 90)["resolved"], 1)
        self.assertFalse(epc.publication_matches_cache([target], cache))
        rows, _, _, _ = epc.enrich_transactions([target], cache, "", args())
        self.assertTrue(epc.publication_matches_cache(rows, cache))

    def test_unknown_clears_every_dependent_fact_without_altering_source_sale(self):
        target = sale(floorAreaSqm=40, floorAreaSqft=431, pricePerSqft=8000, epcMatched=True,
                      epcRating="C", epcRegistrationDate="2025-01-01", epcSource="MHCLG EPC Register")
        wrong = record("8 HIGH STREET GU19 5AE")
        cache = {"records": {epc.stable_transaction_key(target): wrong}}
        rows, _, _, _ = epc.enrich_transactions([target], cache, "", args())
        self.assertFalse(rows[0]["epcMatched"])
        self.assertEqual(set(rows[0]) & epc.PUBLIC_EPC_FIELDS, {"epcMatched"})
        self.assertEqual(rows[0]["price"], target["price"])
        self.assertEqual(epc.terminal_cache_accounting([target], cache, 90)["resolved"], 0)

    def test_offline_cli_ignores_available_token_and_never_writes_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            source = path / "input.js"; cache = path / "cache.json"
            source.write_text('window.SURREY_LAND_REG_TRANSACTIONS = ' + json.dumps([sale()]) + ';\n')
            cache.write_text(json.dumps({"version": epc.CACHE_VERSION, "records": {"any": record("2 HIGH STREET GU19 5AE")}}))
            before = (source.read_bytes(), cache.read_bytes())
            with patch.object(sys, "argv", ["enrich_epc_data", "--revalidate-cache-only", "--input-js", str(source), "--cache", str(cache)]), patch.dict("os.environ", {"EPC_BEARER_TOKEN": "must-not-be-used"}), patch.object(epc, "request_json", side_effect=AssertionError("network")), patch.object(epc, "write_cache", side_effect=AssertionError("cache write")), patch.object(epc, "write_canonical_js_atomic", side_effect=AssertionError("feed write")), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(epc.main(), 0)
            self.assertEqual((source.read_bytes(), cache.read_bytes()), before)
            self.assertEqual(json.loads(output.getvalue())["sourceChecksPerformed"], 0)

    def test_review_only_unknown_cannot_count_as_a_fresh_register_no_match(self):
        target = sale()
        wrong = record("8 HIGH STREET GU19 5AE")
        wrong["searchedAt"] = epc.utc_now()
        _rows, reviewed, _report = epc.revalidate_retained_cache(
            [target], {"version": epc.CACHE_VERSION, "records": {epc.stable_transaction_key(target): wrong}})
        unknown = reviewed["records"][epc.stable_transaction_key(target)]
        self.assertEqual(unknown["searchedAt"], wrong["searchedAt"])
        self.assertFalse(epc.cache_record_is_fresh(unknown, 90, target))
        self.assertEqual(epc.terminal_cache_accounting([target], reviewed, 90), {
            "requested": 1, "resolved": 0, "pending": 1, "errors": 0,
            "matchedCacheRecords": 0, "noMatchCacheRecords": 0})
        actual_search = {"status": "no_match", "searchedAt": epc.utc_now()}
        self.assertTrue(epc.cache_record_is_fresh(actual_search, 90, target))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "input.js"
            cache = Path(temporary) / "cache.json"
            source.write_text('window.SURREY_LAND_REG_TRANSACTIONS = ' + json.dumps([target]) + ';\n')
            cache.write_text(json.dumps(reviewed))
            before = source.read_bytes(), cache.read_bytes()
            with patch.object(sys, "argv", ["enrich_epc_data", "--input-js", str(source), "--cache", str(cache)]), patch.object(epc.os, "getenv", return_value=""), patch.object(epc, "write_cache", side_effect=AssertionError("cache write")), patch.object(epc, "write_canonical_js_atomic", side_effect=AssertionError("feed write")), redirect_stdout(io.StringIO()):
                self.assertEqual(epc.main(), 2)
            self.assertEqual((source.read_bytes(), cache.read_bytes()), before)


class EPCWholePropertyAreaTests(unittest.TestCase):
    # Minimal measurement excerpts from MHCLG's official SAP12 SAP/RdSAP
    # fixtures and domestic-view regression expectations (both publish 98m²).
    @staticmethod
    def sap(schema="SAP-Schema-12.0"):
        return {"schema_type": schema, "sap_building_parts": [{
            "building_part_number": 1,
            "sap_floor_dimensions": [
                {"storey": 0, "heat_loss_area": 28.8, "total_floor_area": 28.8},
                {"storey": 1, "heat_loss_area": 0, "total_floor_area": 28.8},
                {"storey": 2, "heat_loss_area": 0, "total_floor_area": 40},
            ],
        }], "co2_emissions_current_per_floor_area": 20}

    @staticmethod
    def rdsap():
        return {"schema_type": "SAP-Schema-12.0", "sap_building_parts": [
            {"building_part_number": 1, "identifier": "Main Dwelling",
             "sap_room_in_roof": {"floor_area": 18.3},
             "sap_floor_dimensions": [
                 {"floor": 0, "total_floor_area": 31.26},
                 {"floor": 1, "total_floor_area": 31.26},
             ]},
            {"building_part_number": 2, "identifier": "Extension 1",
             "sap_floor_dimensions": [
                 {"floor": 0, "total_floor_area": 8.47},
                 {"floor": 1, "total_floor_area": 8.47},
             ]},
        ]}

    def test_declared_whole_total_precedes_part_measurements_and_intensities(self):
        source = self.sap("SAP-Schema-13.0")
        source["total_floor_area"] = 69
        evidence = epc.floor_area_evidence(source)
        self.assertEqual(evidence["areaSqm"], 69)
        self.assertEqual(evidence["basis"], "declared-whole-property-area")
        self.assertEqual(evidence["components"], [{"path": "$.total_floor_area", "areaSqm": 69}])
        self.assertEqual(epc.floor_area_from_certificate(source), 69)

    def test_reviewed_direct_aliases_remain_supported(self):
        for key in epc.AREA_KEYS:
            with self.subTest(key=key):
                self.assertEqual(epc.floor_area_from_certificate({key: "120.25"}), 120.25)
        self.assertEqual(epc.floor_area_from_certificate({"total_floor_area": 25}), 25)
        self.assertEqual(epc.floor_area_from_certificate({"total_floor_area": 4000}), 4000)

    def test_numeric_strings_and_explicit_square_metre_wrappers(self):
        for value in [120.25, "120.25", "1.2025e2",
                      {"value": "120.25", "quantity": "square metres"},
                      {"value": "120.25", "quantity": "sq m"},
                      {"value": 120.25, "quantity": "m²"}]:
            with self.subTest(value=value):
                self.assertEqual(epc.floor_area_from_certificate({"total_floor_area": value}), 120.25)

    def test_invalid_explicit_total_never_falls_back_to_a_partial_or_nested_area(self):
        invalid = [None, True, False, 0, -120, 24, 4001, float("inf"), float("nan"),
                   "NaN", "Infinity", "-120", "approx 120", "120 m2", "120-150",
                   "1e-999999999", [120], {"value": 120},
                   {"value": 120, "quantity": "square feet"},
                   {"value": True, "quantity": "square metres"},
                   {"value": 120, "quantity": "square metres", "other": 1}]
        for value in invalid:
            with self.subTest(value=value):
                source = self.sap()
                source["total_floor_area"] = value
                self.assertIsNone(epc.floor_area_evidence(source))

    def test_conflicting_direct_aliases_are_not_arbitrarily_selected(self):
        self.assertIsNone(epc.floor_area_evidence({"total_floor_area": 120, "totalFloorArea": 121}))
        self.assertIsNone(epc.floor_area_evidence({"total_floor_area": None, "totalFloorArea": 120}))
        self.assertEqual(epc.floor_area_from_certificate({"total_floor_area": "120", "totalFloorArea": 120}), 120)

    def test_legacy_sap_sums_all_storeys_not_first_heat_loss_area(self):
        evidence = epc.floor_area_evidence(self.sap())
        self.assertEqual(evidence["basis"], "sap-building-parts-sum")
        self.assertEqual(evidence["areaSqm"], 98)
        self.assertEqual(evidence["unroundedAreaSqm"], 97.6)
        self.assertEqual([item["areaSqm"] for item in evidence["components"]], [28.8, 28.8, 40])
        self.assertEqual([item["floor"] for item in evidence["components"]], [0, 1, 2])
        self.assertTrue(all(item["path"].endswith(".total_floor_area") for item in evidence["components"]))

    def test_legacy_rdsap_includes_every_extension_and_room_in_roof(self):
        evidence = epc.floor_area_evidence(self.rdsap())
        self.assertEqual(evidence["areaSqm"], 98)
        self.assertEqual(evidence["unroundedAreaSqm"], 97.76)
        self.assertEqual(len(evidence["components"]), 5)
        roof = [item for item in evidence["components"] if ".sap_room_in_roof." in item["path"]]
        self.assertEqual(roof, [{"path": "$.sap_building_parts[0].sap_room_in_roof.floor_area",
                                "areaSqm": 18.3, "buildingPartNumber": 1}])
        # Equal areas on distinct floors are separate contributions.
        self.assertEqual(sum(item["areaSqm"] == 31.26 for item in evidence["components"]), 2)
        self.assertEqual(sum(item["areaSqm"] == 8.47 for item in evidence["components"]), 2)

    def test_nested_derivation_requires_an_explicit_reviewed_schema(self):
        self.assertEqual(epc.floor_area_from_certificate(self.sap("SAP-Schema-13.0")), 98)
        alias = self.sap()
        alias["schemaType"] = alias.pop("schema_type")
        self.assertEqual(epc.floor_area_from_certificate(alias), 98)
        alias["schema_type"] = "SAP-Schema-13.0"
        self.assertIsNone(epc.floor_area_evidence(alias))
        for schema in ["", "SAP-Schema-14.0", "RdSAP-Schema-21.0.1", None, [], 12]:
            with self.subTest(schema=schema):
                self.assertIsNone(epc.floor_area_evidence(self.sap(schema)))

    def test_unrelated_floor_area_keys_and_carbon_intensity_cannot_supply_area(self):
        for value in [
            {"co2_emissions_current_per_floor_area": 200},
            {"nested": {"total_floor_area": 200}},
            {"sap_floor_dimensions": [{"total_floor_area": 200}]},
            {"heat_loss_area": 200},
        ]:
            self.assertIsNone(epc.floor_area_evidence({"schema_type": "SAP-Schema-12.0", **value}))
        source = self.sap()
        for floor in source["sap_building_parts"][0]["sap_floor_dimensions"]:
            floor.pop("total_floor_area")
            floor["heat_loss_area"] = 200
        self.assertIsNone(epc.floor_area_evidence(source))

    def test_duplicate_part_or_floor_identity_is_rejected_without_deduplicating_equal_areas(self):
        duplicate_part = self.rdsap()
        duplicate_part["sap_building_parts"][1]["building_part_number"] = "1"
        self.assertIsNone(epc.floor_area_evidence(duplicate_part))
        duplicate_floor = self.sap()
        duplicate_floor["sap_building_parts"][0]["sap_floor_dimensions"][1]["storey"] = "0"
        self.assertIsNone(epc.floor_area_evidence(duplicate_floor))
        missing_main = self.sap()
        missing_main["sap_building_parts"][0]["building_part_number"] = 2
        self.assertIsNone(epc.floor_area_evidence(missing_main))
        mixed_identities = self.sap()
        dimension = mixed_identities["sap_building_parts"][0]["sap_floor_dimensions"][1]
        dimension["floor"] = dimension.pop("storey")
        self.assertIsNone(epc.floor_area_evidence(mixed_identities))
        self.assertEqual(epc.floor_area_from_certificate(self.rdsap()), 98)

    def test_partial_or_malformed_measurement_arrays_cannot_be_summed(self):
        malformed = []
        for part_value in [None, {}, [], "missing"]:
            source = self.sap()
            source["sap_building_parts"] = part_value
            malformed.append(source)
        for dimensions in [None, {}, [], [None], [{"storey": 0}], [{"total_floor_area": 200}],
                           [{"storey": 0, "floor": 0, "total_floor_area": 200}],
                           [{"storey": True, "total_floor_area": 200}],
                           [{"storey": 0.5, "total_floor_area": 200}]]:
            source = self.sap()
            source["sap_building_parts"][0]["sap_floor_dimensions"] = dimensions
            malformed.append(source)
        for number in [None, True, 0, -1, "unknown"]:
            source = self.sap()
            source["sap_building_parts"][0]["building_part_number"] = number
            malformed.append(source)
        for roof in [None, {}, [], {"floor_area": None}, {"floor_area": -10}]:
            source = self.rdsap()
            source["sap_building_parts"][0]["sap_room_in_roof"] = roof
            malformed.append(source)
        for index, source in enumerate(malformed):
            with self.subTest(index=index):
                self.assertIsNone(epc.floor_area_evidence(source))

    def test_component_units_and_numeric_strings_are_validated_before_aggregation(self):
        source = self.sap()
        source["sap_building_parts"][0]["sap_floor_dimensions"][0]["total_floor_area"] = {
            "value": "28.8", "quantity": "square metres"}
        self.assertEqual(epc.floor_area_from_certificate(source), 98)
        source["sap_building_parts"][0]["sap_floor_dimensions"][0]["total_floor_area"]["quantity"] = "square feet"
        self.assertIsNone(epc.floor_area_evidence(source))

    def test_decimal_aggregate_uses_postgresql_half_up_and_whole_property_bounds(self):
        source = self.sap()
        floors = source["sap_building_parts"][0]["sap_floor_dimensions"]
        floors[:] = [{"storey": 0, "total_floor_area": "28.5"},
                     {"storey": 1, "total_floor_area": "30"}]
        self.assertEqual(epc.floor_area_from_certificate(source), 59)
        floors[:] = [{"storey": 0, "total_floor_area": "100.49999999999999999999999999999"}]
        self.assertEqual(epc.floor_area_from_certificate(source), 100)
        for values, expected in [([12, 12], None), ([12.3, 12.3], 25), ([2000, 2000], 4000),
                                 ([2000, 2001], None)]:
            floors[:] = [{"storey": i, "total_floor_area": value} for i, value in enumerate(values)]
            self.assertEqual(epc.floor_area_from_certificate(source), expected)

    def test_evidence_is_pure_and_contains_only_measurement_provenance(self):
        source = self.rdsap()
        source["assessor"] = {"contact": "private-source-sentinel"}
        source["sap_building_parts"][0]["unrelated"] = "private-source-sentinel"
        before = copy.deepcopy(source)
        evidence = epc.floor_area_evidence(source)
        self.assertEqual(source, before)
        self.assertNotIn("private-source-sentinel", json.dumps(evidence))
        self.assertEqual(set(evidence), {"areaSqm", "basis", "schemaType", "components", "unroundedAreaSqm"})

    def test_same_part_roof_99_is_counted_once_with_both_source_paths(self):
        for identity_key in ("floor", "storey"):
            source = self.sap()
            part = source["sap_building_parts"][0]
            part["sap_floor_dimensions"] = [
                {identity_key: 0, "total_floor_area": 120},
                {identity_key: 99, "total_floor_area": "30.25"},
            ]
            part["sap_room_in_roof"] = {"floor_area": {"value": 30.25, "quantity": "sq m"}}
            before = copy.deepcopy(source)
            evidence = epc.floor_area_evidence(source)
            self.assertEqual(source, before)
            self.assertEqual(evidence["areaSqm"], 150)
            self.assertEqual(evidence["unroundedAreaSqm"], 150.25)
            self.assertEqual(len(evidence["components"]), 2)
            roof = evidence["components"][1]
            self.assertEqual(roof["path"], "$.sap_building_parts[0].sap_floor_dimensions[1].total_floor_area")
            self.assertEqual(roof["equivalentSourcePaths"], ["$.sap_building_parts[0].sap_room_in_roof.floor_area"])
            self.assertEqual(roof["reconciliation"], "same-building-part-roof-99")

    def test_conflicting_roof_99_area_rejects_derived_total_but_declared_total_wins(self):
        source = self.sap("SAP-Schema-13.0")
        part = source["sap_building_parts"][0]
        part["sap_floor_dimensions"] = [{"floor": 0, "total_floor_area": 120},
                                        {"floor": 99, "total_floor_area": "30.250000000000001"}]
        part["sap_room_in_roof"] = {"floor_area": "30.25"}
        self.assertIsNone(epc.floor_area_evidence(source))
        source["total_floor_area"] = 160
        evidence = epc.floor_area_evidence(source)
        self.assertEqual(evidence["areaSqm"], 160)
        self.assertEqual(evidence["basis"], "declared-whole-property-area")

    def test_roof_99_reconciliation_is_separate_for_each_building_part(self):
        source = self.rdsap()
        for part in source["sap_building_parts"]:
            part["sap_floor_dimensions"] = [{"floor": 0, "total_floor_area": 40},
                                            {"floor": 1, "total_floor_area": 40},
                                            {"floor": 99, "total_floor_area": 20}]
            part["sap_room_in_roof"] = {"floor_area": 20}
        evidence = epc.floor_area_evidence(source)
        self.assertEqual(evidence["areaSqm"], 200)
        self.assertEqual(len(evidence["components"]), 6)
        reconciled = [component for component in evidence["components"] if "reconciliation" in component]
        self.assertEqual([component["buildingPartNumber"] for component in reconciled], [1, 2])
        self.assertNotEqual(reconciled[0]["equivalentSourcePaths"], reconciled[1]["equivalentSourcePaths"])

    def test_equal_ordinary_floor_and_roof_areas_remain_distinct(self):
        source = self.sap()
        part = source["sap_building_parts"][0]
        part["sap_floor_dimensions"] = [{"storey": 0, "total_floor_area": 40},
                                        {"storey": 1, "total_floor_area": 40}]
        part["sap_room_in_roof"] = {"floor_area": 40}
        evidence = epc.floor_area_evidence(source)
        self.assertEqual(evidence["areaSqm"], 120)
        self.assertEqual(len(evidence["components"]), 3)
        self.assertFalse(any("reconciliation" in component for component in evidence["components"]))

    def test_roof_99_without_separate_roof_area_remains_one_floor(self):
        source = self.sap()
        source["sap_building_parts"][0]["sap_floor_dimensions"] = [
            {"storey": 0, "total_floor_area": 120}, {"storey": 99, "total_floor_area": 30}]
        evidence = epc.floor_area_evidence(source)
        self.assertEqual(evidence["areaSqm"], 150)
        self.assertFalse(any("reconciliation" in component for component in evidence["components"]))


class EPCRecoveryIdentityTests(unittest.TestCase):
    """Recovery is opt-in, bound to the cohort, and replayable from source facts."""

    def target(self, **changes):
        import epc_recovery_identity as recovery
        row = sale(**changes)
        row.setdefault("propertyRecordId", "property:" + recovery._canonical_text(row["address"])
                       + "|" + epc.normalise_postcode(row["postcode"]))
        return row

    def context(self, rows, claims=None, properties=None):
        import epc_recovery_identity as recovery
        payload = {"schemaVersion": 1, "cohortIdentitySha256": recovery.cohort_digest(rows),
                   "frozenAppCommit": "a" * 40,
                   "sourceHashes": {"hmlrLinks": "b" * 64, "reviewedAliases": "c" * 64,
                                    "councils": "d" * 64, "uprnDiscovery": "e" * 64},
                   "properties": properties if properties is not None else {
                       rows[0]["propertyRecordId"]: claims or {}}}
        return recovery.load_recovery_context(payload, rows, expected_sha256=recovery.context_digest(payload))

    def official(self, uprn="100000001"):
        return {"authoritativeUprn": {"uprn": uprn,
                "sourceId": "hmlr_ppd_uprn_202607_os_202608_test", "sourceSnapshot": "2026-08"}}

    def certificate(self, address, **changes):
        return {"addressLine1": address, "certificateNumber": "synthetic-recovery-1",
                "registrationDate": "2024-01-01", **changes}

    def cached(self, full, summary=None):
        import epc_recovery_identity as recovery
        number = epc.extract_certificate_number(full) or epc.extract_certificate_number(summary or {})
        cached = record(epc.candidate_address(full), number=number,
                        date=epc.extract_registration_date(full))
        cached["certificateIdentity"] = recovery.certificate_identity_snapshot(full)
        cached["certificateFetch"] = {"requestedNumber": number,
                                       "returnedNumber": epc.extract_certificate_number(full)}
        if summary is not None:
            cached["summaryIdentity"] = recovery.certificate_identity_snapshot(summary)
        return cached

    def test_context_requires_external_hash_full_cohort_and_exact_transaction_membership(self):
        import epc_recovery_identity as recovery
        target = self.target(postcode="")
        payload = {"schemaVersion": 1, "cohortIdentitySha256": recovery.cohort_digest([target]),
                   "frozenAppCommit": "a" * 40, "sourceHashes": {},
                   "properties": {target["propertyRecordId"]: {}}}
        for expected, rows in [("0" * 64, [target]),
                               (recovery.context_digest(payload), [{**target, "price": 4}])]:
            with self.assertRaises(ValueError):
                recovery.load_recovery_context(payload, rows, expected_sha256=expected)
        context = self.context([target])
        for change in [{"id": "different"}, {"town": "WOKING"}, {"price": 5}]:
            with self.assertRaises(ValueError):
                recovery.resolve_identity({**target, **change}, {}, context)
        with self.assertRaises(TypeError):
            context.properties[target["propertyRecordId"]]["councilCode"] = "E07000000"
        with self.assertRaises(ValueError):
            recovery.resolve_identity(target, {}, {"claimedVerified": True})
        recovery.resolve_identity({**target, "floorAreaSqm": 500}, {}, context)

    def test_exact_old_guard_is_default_and_untargeted_properties_stay_strict(self):
        import epc_recovery_identity as recovery
        target = self.target(postcode="")
        full = self.certificate("2 HIGH STREET BAGSHOT GU19 5AE")
        self.assertFalse(epc.delivery_identity(target, full)[0])
        self.assertFalse(recovery.resolve_identity(target, full, self.context([target], properties={}))[0])
        self.assertEqual(recovery.resolve_identity(target, full, self.context([target])),
                         (True, "exact_full_delivery_with_independent_locality"))
        self.assertEqual(target["postcode"], "")

    def test_no_postcode_needs_complete_independent_context_not_source_postcode(self):
        import epc_recovery_identity as recovery
        target = self.target(paon="WILLOW HOUSE", postcode="", locality="THE GREEN")
        context = self.context([target], {"councilCode": "E07000001"})
        full = self.certificate("WILLOW HOUSE HIGH STREET THE GREEN BAGSHOT GU19 5AE")
        self.assertTrue(recovery.resolve_identity(target, full, context)[0])
        for address in ["WILLOW HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "WILLOW HOUSE HIGH STREET THE GREEN GU19 5AE",
                        "WILLOW HOUSE LOW STREET THE GREEN BAGSHOT GU19 5AE",
                        "OTHER HOUSE HIGH STREET THE GREEN BAGSHOT GU19 5AE",
                        "FLAT 1 WILLOW HOUSE HIGH STREET THE GREEN BAGSHOT GU19 5AE",
                        "WILLOW HOUSE HIGH STREET ANNEXE THE GREEN BAGSHOT GU19 5AE"]:
            with self.subTest(address=address):
                self.assertFalse(recovery.resolve_identity(target, self.certificate(address), context)[0])
        for extra in [{"postTown": "WOKING"}, {"council_code": "E07000002"}, {"postcode": "KT6 5HE"}]:
            self.assertFalse(recovery.resolve_identity(target, {**full, **extra}, context)[0])
        missing_town = {**target, "town": ""}
        self.assertFalse(recovery.resolve_identity(missing_town, full, self.context([missing_town]))[0])
        other = {**target, "id": "other", "propertyRecordId": "property:OTHER|GU195AE"}
        self.assertFalse(recovery.resolve_identity(target, full, self.context([target, other]))[0])

    def test_official_uprn_permits_named_alias_but_never_wrong_extent_or_road(self):
        import epc_recovery_identity as recovery
        target = self.target(paon="WILLOW HOUSE")
        context = self.context([target], self.official())
        full = self.certificate("CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE", uprn="000100000001")
        self.assertFalse(epc.delivery_identity(target, full)[0])
        self.assertEqual(recovery.resolve_identity(target, full, context),
                         (True, "hmlr_uprn_with_delivery_extent"))
        for address in ["CEDAR HOUSE 8 HIGH STREET BAGSHOT GU19 5AE",
                        "FLAT 1 CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "ANNEXE CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "GROUND FLOOR CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "BASEMENT CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "GARAGE CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "REAR WING CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "CEDAR HOUSE LOW STREET BAGSHOT GU19 5AE",
                        "CEDAR HOUSE HIGH STREET WOKING GU19 5AE",
                        "CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AB"]:
            self.assertFalse(recovery.resolve_identity(target, {**full, "addressLine1": address}, context)[0])
        self.assertFalse(recovery.resolve_identity(target, {**full, "uprn": "100000002"}, context)[0])
        cottage = {**full, "addressLine1": "CEDAR COTTAGE HIGH STREET BAGSHOT GU19 5AE"}
        self.assertTrue(recovery.resolve_identity(target, cottage, context)[0])
        for expected, changed in [("WEST WING WILLOW HOUSE", "EAST WING CEDAR HOUSE"),
                                  ("LEFT GARAGE WILLOW HOUSE", "RIGHT GARAGE CEDAR HOUSE")]:
            partial = self.target(paon=expected, postcode="")
            scoped = self.context([partial], self.official())
            wrong = self.certificate(changed + " HIGH STREET BAGSHOT GU19 5AE", uprn="100000001")
            self.assertFalse(recovery.resolve_identity(partial, wrong, scoped)[0])
            exact = self.certificate(expected + " HIGH STREET BAGSHOT GU19 5AE", uprn="100000001")
            self.assertTrue(recovery.resolve_identity(partial, exact, scoped)[0])
            self.assertIsNotNone(epc.validated_cached_epc(partial, self.cached(exact), recovery_context=scoped))

    def test_no_postcode_rejects_other_canonical_delivery_and_explicit_locality_conflict(self):
        import epc_recovery_identity as recovery
        target = self.target(postcode="", locality="THE GREEN")
        full = self.certificate("2 HIGH STREET THE GREEN BAGSHOT GU19 5AE")
        context = self.context([target])
        self.assertTrue(recovery.resolve_identity(target, {**full, "locality": "THE GREEN"}, context)[0])
        self.assertFalse(recovery.resolve_identity(target, {**full, "locality": "OTHER VILLAGE"}, context)[0])
        unknown = self.target(postcode="")
        source = self.certificate("2 HIGH STREET BAGSHOT GU19 5AE", locality="THE GREEN")
        self.assertTrue(recovery.resolve_identity(unknown, source, self.context([unknown]))[0])
        other = self.target(postcode="GU19 5AE", locality="THE GREEN", id="sale-2", county="SURREY",
                            address="2 HIGH STREET THE GREEN BAGSHOT SURREY GU19 5AE")
        self.assertTrue(epc.delivery_identity(other, full)[0])
        self.assertFalse(recovery.resolve_identity(target, full, self.context([target, other]))[0])

    def test_discovery_uprns_only_query_and_cannot_admit_wrong_delivery(self):
        import epc_recovery_identity as recovery
        target = self.target(paon="WILLOW HOUSE")
        context = self.context([target], {"discoveryUprns": ["100000001"]})
        self.assertIn({"uprn": "100000001"}, recovery.discovery_queries(target, context))
        for address in ["CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "FLAT 1 WILLOW HOUSE HIGH STREET BAGSHOT GU19 5AE",
                        "WILLOW HOUSE LOW STREET BAGSHOT GU19 5AE"]:
            self.assertFalse(recovery.resolve_identity(target, self.certificate(address, uprn="100000001"), context)[0])
        claims = {**self.official(), "discoveryUprns": ["000100000001", "100000002"]}
        queries = recovery.discovery_queries(target, self.context([target], claims))
        self.assertEqual(sum("uprn" in query for query in queries), 2)
        for values in [["0"], [True], ["1", "01"], [str(i) for i in range(1, 6)]]:
            with self.assertRaises(ValueError):
                self.context([target], {"discoveryUprns": values})

    def test_missing_postcode_discovery_adds_bounded_contiguous_address_variants(self):
        import epc_recovery_identity as recovery
        target = self.target(paon="WILLOW HOUSE, 14", saon="FLAT 2", postcode="")
        claims = {"councilCode": "E07000001"}
        queries = recovery.discovery_queries(target, self.context([target], claims))
        self.assertEqual({query["address"] for query in queries if "address" in query}, {
            target["address"], "FLAT 2 WILLOW HOUSE 14 HIGH STREET", "HIGH STREET BAGSHOT"})
        self.assertFalse(any("council[]" in query or "councilCode" in query for query in queries))
        self.assertFalse(any(query == {"address": "HIGH STREET"} for query in queries))
        known = self.target(paon="WILLOW HOUSE, 14", saon="FLAT 2")
        queries = recovery.discovery_queries(known, self.context([known], claims))
        self.assertEqual(sum("address" in query for query in queries), 1)
        self.assertFalse(any("council[]" in query for query in queries))
        duplicate = self.target(postcode="", address="2 HIGH STREET")
        queries = recovery.discovery_queries(duplicate, self.context([duplicate]))
        self.assertEqual(sum(query == {"address": "2 HIGH STREET"} for query in queries), 1)

    def test_shared_official_uprn_and_alias_members_are_rejected(self):
        first, second = self.target(), self.target(paon="4", id="sale-2")
        with self.assertRaises(ValueError):
            self.context([first, second], properties={first["propertyRecordId"]: self.official(),
                                                       second["propertyRecordId"]: self.official()})
        with self.assertRaises(ValueError):
            self.context([first, second], {"reviewedAliases": [{"groupId": "reviewed-test",
                         "members": [first["propertyRecordId"], second["propertyRecordId"]]}]})

    def test_reviewed_alias_requires_complete_literal_member_and_postcode(self):
        import epc_recovery_identity as recovery
        target = self.target(paon="WILLOW HOUSE")
        claims = {"reviewedAliases": [{"groupId": "reviewed-test", "members": [target["propertyRecordId"],
                          "property:CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE|GU195AE"]}]}
        context = self.context([target], claims)
        full = self.certificate("CEDAR HOUSE HIGH STREET BAGSHOT", postcode="GU19 5AE")
        self.assertEqual(recovery.resolve_identity(target, full, context), (True, "reviewed_full_address_alias"))
        for changes in [{"addressLine1": "CEDAR HOUSE HIGH STREET"}, {"postcode": "GU19 5AB"},
                        {"addressLine1": "THE ANNEXE CEDAR HOUSE HIGH STREET BAGSHOT"}]:
            self.assertFalse(recovery.resolve_identity(target, {**full, **changes}, context)[0])
        self.assertTrue(recovery.replay_cached_identity(target, self.cached(full, full), context)[0])

    def test_summary_only_uprn_requires_crossvalidated_full_identity_and_keeps_omission(self):
        import epc_recovery_identity as recovery
        target = self.target(paon="WILLOW HOUSE")
        context = self.context([target], self.official())
        full = self.certificate("CEDAR HOUSE HIGH STREET BAGSHOT GU19 5AE")
        full.pop("certificateNumber")
        summary = {**full, "certificateNumber": "synthetic-recovery-1", "uprn": "100000001"}
        self.assertEqual(recovery.resolve_identity(target, full, context, summary),
                         (True, "hmlr_uprn_with_crossvalidated_summary"))
        cached = self.cached(full, summary)
        self.assertNotIn("uprn", cached["certificateIdentity"])
        self.assertNotIn("certificateNumber", cached["certificateIdentity"])
        self.assertIsNotNone(epc.validated_cached_epc(target, cached, recovery_context=context))
        for changes in [{"addressLine1": "OTHER HOUSE HIGH STREET BAGSHOT GU19 5AE"},
                        {"registrationDate": "2024-02-01"}, {"postcode": "GU19 5AB"}]:
            self.assertFalse(recovery.resolve_identity(target, full, context, {**summary, **changes})[0])
        conflict = {**full, "uprn": "100000002"}
        self.assertFalse(recovery.resolve_identity(target, conflict, context, summary)[0])

    def test_original_unstructured_exact_full_address_replays_with_snapshots(self):
        import epc_recovery_identity as recovery
        target = self.target(paon="", street="", address="2 HIGH STREET BAGSHOT GU19 5AE")
        full = self.certificate("2 HIGH STREET BAGSHOT GU19 5AE")
        context = self.context([target])
        self.assertEqual(recovery.resolve_identity(target, full, context), (True, "exact_full_address"))
        self.assertTrue(recovery.replay_cached_identity(target, self.cached(full, full), context)[0])

    def test_cache_claims_cannot_replace_context_or_original_source_facts(self):
        import epc_recovery_identity as recovery
        target = self.target(postcode="")
        context = self.context([target])
        full = self.certificate("2 HIGH STREET BAGSHOT GU19 5AE")
        cached = self.cached(full, full)
        self.assertIsNone(epc.validated_cached_epc(target, cached))
        for mutate in [lambda value: value.pop("certificateIdentity"),
                       lambda value: value["certificateFetch"].update(requestedNumber="wrong"),
                       lambda value: value["certificateFetch"].update(returnedNumber="wrong"),
                       lambda value: value["certificateFetch"].update(uprn="100000001"),
                       lambda value: value["certificateIdentity"].update(addressLine1="8 HIGH STREET BAGSHOT GU19 5AE"),
                       lambda value: value["summaryIdentity"].update(registrationDate="2024-02-01"),
                       lambda value: value["certificateIdentity"].update(rawPayload="private")]:
            changed = copy.deepcopy(cached)
            mutate(changed)
            changed["recoveryMethod"] = "exact_full_delivery_with_independent_locality"
            changed["claimedContextHash"] = context.payload_sha256
            self.assertIsNone(epc.validated_cached_epc(target, changed, recovery_context=context))
        explicit = self.cached({**full, "uprn": "100000001"})
        explicit["certificateFetch"]["uprn"] = "000100000001"
        self.assertIsNotNone(epc.validated_cached_epc(target, explicit, recovery_context=context))
        explicit["certificateFetch"]["uprn"] = "100000002"
        self.assertIsNone(epc.validated_cached_epc(target, explicit, recovery_context=context))

    def test_recovery_replays_retained_index_reprices_sales_and_publication_stays_private(self):
        import epc_recovery_identity as recovery
        target = self.target(postcode="")
        repeated = {**target, "id": "sale-2", "price": 4_000_000, "date": "2026-01-01"}
        context = self.context([target, repeated])
        full = self.certificate("2 HIGH STREET BAGSHOT GU19 5AE")
        cached = self.cached(full, full)
        cache = {"records": {epc.stable_transaction_key(target): cached}}
        before = copy.deepcopy(([target, repeated], cache))
        with patch.object(epc, "request_json", side_effect=AssertionError("No provider calls")):
            rows, reviewed, report = epc.revalidate_retained_cache([target, repeated], cache, context)
        self.assertEqual(([target, repeated], cache), before)
        self.assertEqual(report["sourceChecksPerformed"], 0)
        for old, row in zip([target, repeated], rows):
            self.assertTrue(row["epcMatched"])
            self.assertEqual(row["pricePerSqft"], round(old["price"] / row["floorAreaSqft"]))
            self.assertEqual({key: value for key, value in row.items() if key not in epc.PUBLIC_EPC_FIELDS}, old)
        self.assertTrue(epc.publication_matches_cache(rows, reviewed, context))
        self.assertFalse(epc.publication_matches_cache(rows, reviewed))
        self.assertEqual(epc.terminal_cache_accounting(rows, reviewed, 10000, context)["matchedCacheRecords"], 2)
        self.assertFalse(any(key in json.dumps(rows) for key in ["certificateIdentity", "summaryIdentity", "recoveryMethod", "synthetic-recovery"]))

    def test_same_certificate_conflicting_source_uprns_are_quarantined_regardless_of_order(self):
        target = self.target(postcode="")
        context = self.context([target])
        first = self.cached(self.certificate("2 HIGH STREET BAGSHOT GU19 5AE", uprn="100000001"))
        second = self.cached(self.certificate("2 HIGH STREET BAGSHOT GU19 5AE", uprn="100000002"))
        for values in [(first, second), (second, first)]:
            cache = {"records": dict(zip([epc.stable_transaction_key(target), "other"], values))}
            _index, conflicts = epc.retained_certificate_index(cache, context)
            self.assertEqual(conflicts, {"synthetic-recovery-1"})
            rows, _, _ = epc.revalidate_retained_cache([target], cache, context)
            self.assertFalse(rows[0]["epcMatched"])


if __name__ == "__main__":
    unittest.main()
