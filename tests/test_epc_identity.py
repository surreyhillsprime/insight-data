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


if __name__ == "__main__":
    unittest.main()
