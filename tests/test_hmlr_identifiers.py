import copy
import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from collect_hmlr_identifiers import (
    build_payload, latest_lookup, lookup_rows, os_coordinates, preferred_records, update_state,
)
from enrich_os_uprn import apply_hmlr_link, enrich_transactions
from sweep_land_registry import archive_row, normalise_rows
from uprn_priority import preferred_property_uprn
from runtime_release import public_review_decision
from tests import test_inspire_parcels as inspire_tests


TX = "{00000000-0000-0000-0000-000000000001}"
TX2 = "{00000000-0000-0000-0000-000000000002}"
UPRN = "100000000001"
PROPERTY = "property:1 TEST ROAD GUILDFORD GU1 1AA|GU11AA"


def ppd(tx=TX, status="A", date="2026-06-01", price="3000000"):
    return [tx, price, date, "GU1 1AA", "D", "N", "F", "1", "", "TEST ROAD", "", "GUILDFORD", "GUILDFORD", "SURREY", "A", status]


def manifest(release="2026-07"):
    return {"release": release, "checkedAt": "2026-08-28T12:00:00Z", "publishedDate": "2026-08-28", "files": {"uprn": {"sha256": "a" * 64}}}


class HmlrIdentifierTests(unittest.TestCase):
    def test_private_review_metadata_is_not_added_to_the_native_public_contract(self):
        review = {"decision": "approve_indicative_parcel", "decisionBatch": "fixture", "reviewedAt": "2026-08-28T12:00:00Z", "semantics": "indicative only", "reviewedBy": "Fixture reviewer", "evidence": "private report"}
        projected = public_review_decision(review)
        self.assertEqual(set(projected), {"decision", "decisionBatch", "reviewedAt", "semantics"})
        self.assertEqual(review["evidence"], "private report")
        self.assertIsNone(public_review_decision(None))

    def state(self, rows=None, uprns=None):
        rows = [ppd()] if rows is None else rows
        return update_state({"schemaVersion": 1, "publications": {}, "transactions": {}}, rows,
                            {TX: [UPRN]} if uprns is None else uprns, {TX: ["100"]}, manifest())

    def canonical(self, rows=None):
        return normalise_rows([archive_row(row) for row in (rows or [ppd()])])[1]

    def test_exact_transaction_join_preserves_canonical_identity(self):
        state = self.state()
        self.assertEqual(state["transactions"][TX]["propertyId"], PROPERTY)
        self.assertEqual(set(preferred_records(state, self.canonical())), {PROPERTY})
        changed_price = self.canonical([ppd(price="3100000")])
        self.assertEqual(preferred_records(state, changed_price), {})

    def test_monthly_absence_retains_prior_links_and_pending_new_properties(self):
        old = self.state()
        later = update_state(old, [ppd(TX2)], {TX2: [UPRN]}, {}, manifest("2026-08"))
        self.assertEqual(later["transactions"][TX], old["transactions"][TX])
        self.assertEqual(len(later["transactions"]), 2)
        self.assertEqual(preferred_records(later, []), {})
        self.assertEqual(set(preferred_records(later, self.canonical())), {PROPERTY})

    def test_latest_sale_wins_over_later_publication_of_an_older_sale(self):
        rows = [ppd(), ppd(TX2, date="2020-01-01")]
        state = self.state(rows, {TX: [UPRN], TX2: ["100000000002"]})
        state["transactions"][TX2]["publication"] = "2026-08"
        selected = preferred_records(state, self.canonical(rows))
        self.assertEqual([tx for tx, _ in selected[PROPERTY]], [TX])

    def test_later_sale_without_identifier_does_not_retract_prior_hmlr_link(self):
        rows = [ppd(), ppd(TX2, date="2026-07-01")]
        state = self.state(rows, {TX: [UPRN]})
        selected = preferred_records(state, self.canonical(rows))
        self.assertEqual([tx for tx, _ in selected[PROPERTY]], [TX])

    def test_multiple_uprns_and_same_date_disagreement_require_review(self):
        state = self.state(uprns={TX: [UPRN, "100000000002"]})
        with self.assertRaisesRegex(ValueError, "multiple UPRNs"):
            preferred_records(state, self.canonical())
        rows = [ppd(), ppd(TX2, price="3100000")]
        state = self.state(rows, {TX: [UPRN], TX2: ["100000000002"]})
        with self.assertRaisesRegex(ValueError, "multiple UPRNs"):
            preferred_records(state, self.canonical(rows))

    def test_correction_replaces_uuid_evidence_and_deletion_blocks_stale_base(self):
        old = self.state()
        corrected = update_state(old, [ppd(status="C")], {TX: ["100000000009"]}, {}, manifest("2026-08"))
        self.assertEqual(corrected["transactions"][TX]["uprns"], ["100000000009"])
        withdrawn = update_state(old, [ppd(status="D")], {}, {}, manifest("2026-08"))
        with self.assertRaisesRegex(ValueError, "Withdrawn"):
            preferred_records(withdrawn, self.canonical())
        self.assertFalse(old["transactions"][TX]["withdrawn"])

    def test_mixed_publication_uuids_duplicate_rows_and_duplicate_lookup_pairs_fail(self):
        with self.assertRaisesRegex(ValueError, "absent"):
            self.state(uprns={TX2: [UPRN]})
        with self.assertRaisesRegex(ValueError, "Duplicate transaction"):
            self.state([ppd(), ppd()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lookup.csv"
            path.write_text(f'"{TX}","{UPRN}"\n"{TX}","{UPRN}"\n')
            with self.assertRaisesRegex(ValueError, "Duplicate identifier"):
                lookup_rows(path)

    def test_os_join_requires_exact_uprn_and_rejects_missing_or_duplicate_points(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "os.zip"
            row = [UPRN, "500000", "150000", "51.25", "-0.4"]
            for rows, message in (([row], None), ([], "missing"), ([row, row], "Duplicate")):
                content = io.StringIO()
                writer = csv.writer(content)
                writer.writerow(["UPRN", "X_COORDINATE", "Y_COORDINATE", "LATITUDE", "LONGITUDE"])
                writer.writerows(rows)
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("Data/points.csv", content.getvalue())
                if message:
                    with self.assertRaisesRegex(ValueError, message):
                        os_coordinates(path, {UPRN})
                else:
                    self.assertEqual(os_coordinates(path, {UPRN})[UPRN], {"latitude": 51.25, "longitude": -0.4})

    def test_published_points_are_authoritative_and_shared_identifiers_fail(self):
        state = self.state()
        preferred = preferred_records(state, self.canonical())
        coords = {UPRN: {"longitude": -0.4, "latitude": 51.25}}
        source = {"version": "2026-08", "sha256": "b" * 64}
        payload = build_payload(state, preferred, coords, source)
        link = payload["linksByProperty"][PROPERTY]
        self.assertEqual((link["matchStatus"], link["evidenceTier"]), ("confirmed_address_match", "authoritative_address_source"))
        self.assertEqual(link["uprn"], UPRN)
        self.assertNotIn("titleNumber", link)
        preferred["property:2 TEST ROAD GUILDFORD GU1 1AA|GU11AA"] = preferred[PROPERTY]
        with self.assertRaisesRegex(ValueError, "shared"):
            build_payload(state, preferred, coords, source)

    def test_live_link_discovery_uses_latest_official_month_not_calendar_guess(self):
        html = ('https://price-paid-data.publicdata.landregistry.gov.uk/pp-uprn-lookup-jul-2026.csv '
                'https://price-paid-data.publicdata.landregistry.gov.uk/pp-uprn-lookup-aug-2026.csv')
        self.assertEqual(latest_lookup(html, "pp-uprn-lookup")[1], "2026-08")
        with self.assertRaises(ValueError):
            latest_lookup('https://example.test/pp-uprn-lookup-aug-2026.csv', "pp-uprn-lookup")

    def test_hmlr_wins_over_both_legacy_fields_and_nearest_matching_is_not_called(self):
        item = {"propertyRecordId": PROPERTY, "uprn": "2", "ordnanceSurvey": {"uprn": "3"}}
        link = {"uprn": UPRN, "checkedAt": "2026-08-28T12:00:00Z", "sourceSnapshot": "hmlr"}
        self.assertEqual(preferred_property_uprn(item, {PROPERTY: link}), UPRN)
        self.assertEqual(preferred_property_uprn(item, {}), "2")
        updated = apply_hmlr_link(item, link)
        self.assertEqual(updated["uprn"], updated["ordnanceSurvey"]["uprn"])
        args = SimpleNamespace(hmlr_only=False, limit=0, progress_every=25)
        with patch("enrich_os_uprn.hmlr_links", return_value={PROPERTY: link}), \
             patch("enrich_os_uprn.uprn_rows", return_value=([{"postcode": "", "lat": 51.25, "lon": -0.4}], True)), \
             patch("enrich_os_uprn.match_uprn", side_effect=AssertionError("fallback called")):
            rows, stats, _, _ = enrich_transactions([item], {}, args)
        self.assertEqual(rows[0]["uprn"], UPRN)
        self.assertEqual(stats["hmlrUprnMatches"], 1)
        self.assertEqual(item["uprn"], "2")

    def test_hmlr_parcel_lookup_disagreement_never_promotes_an_os_containing_parcel(self):
        harness = inspire_tests.UPRNOnboardingTests()
        link = harness.link()
        link["sourceId"] = "hmlr_ppd_uprn_fixture"
        property_id = link["propertyId"]
        for official, outcome in ((set(), "review_required_no_hmlr_parcel_link"), ({"200"}, "review_required_hmlr_parcel_mismatch"), ({"100"}, "automatically_associated_indicative")):
            with self.subTest(official=official), tempfile.TemporaryDirectory() as directory:
                feed, queue = harness.build_synthetic(directory, link, hmlr_parcel_ids={property_id: official})
                self.assertEqual(queue["candidatesByProperty"][property_id]["outcome"], outcome)
                self.assertEqual(property_id in feed["associationsByProperty"], outcome == "automatically_associated_indicative")


if __name__ == "__main__":
    unittest.main()
