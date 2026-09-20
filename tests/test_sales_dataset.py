import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sweep_land_registry as sweep
import collect_title_history as collector
from build_sales_dataset import build_dataset, validate_envelope, verified_base_acquisition
from validate_sales_history_feed import (
    ADDRESS_DATA_USE, ATTRIBUTION, REDISTRIBUTION_RIGHTS, SOURCE_NAME,
    base_feed_identity, sha256_json,
)


def fixture():
    _, rows = sweep.normalise_rows([
        {"tx": "source-private-uuid", "price": 2500000, "date": "2026-07-14", "postcode": "KT10 9AA",
         "propertyType": "D", "paon": "1", "street": "TEST ROAD", "town": "ESHER", "district": "ELMBRIDGE", "category": "A"},
        {"tx": "other-private-uuid", "price": 2200000, "date": "2025-06-01", "postcode": "KT10 9AA",
         "propertyType": "D", "paon": "2", "street": "TEST ROAD", "town": "ESHER", "district": "ELMBRIDGE", "category": "A"},
    ])
    meta = sweep.metadata(2, rows)
    meta.update(sourceCheckedAt="2026-09-21T10:00:00Z", sourceRefreshFrom="2025-01-01",
                sourceRefreshMode="annual-archives-rolling", sourceFetchStatus="verified")
    histories = {}
    for row in rows:
        sale = {key: row[key] for key in ("price", "priceText", "date", "propertyType", "category")}
        sale.update(id="raw-provider-" + row["id"], source=SOURCE_NAME, sourceUrl="https://provider.invalid/private")
        record = {"propertyRecordId": row["propertyRecordId"], "coverageStatus": "complete", "coverageFrom": "1995",
                  "source": SOURCE_NAME, "totalTransactions": 1, "transactions": [sale], "latestTransaction": sale,
                  "updatedAt": "2026-09-20T09:00:00Z", "uprn": "private-uprn"}
        histories[row["propertyRecordId"]] = record
        histories[row["id"]] = record
    history_meta = {
        "schemaVersion": 1, "deploymentMode": "commercial", "publicationStatus": "complete",
        "coverageMode": "full-available-price-paid-history", "coverageStatus": "complete-accounted",
        "source": SOURCE_NAME, "redistributionRights": REDISTRIBUTION_RIGHTS, "addressDataUse": ADDRESS_DATA_USE,
        "attribution": ATTRIBUTION, "coverageFrom": "1995", "sourceCheckedAt": "2026-09-20T09:00:00Z",
        "updatedAt": "2026-09-21T10:00:00Z", "freshnessWindowDays": 45,
        "propertiesRequested": 2, "propertiesChecked": 2, "propertiesUnavailable": 0, "propertiesNotChecked": 0,
        "propertiesWithHistory": 2, "propertiesCheckedNoHistory": 0, "transactionsFound": 2,
        "lookupKeys": 4, "canonicalPropertyRecords": 2, "transactionAliases": 2,
        "baseFeedFingerprint": base_feed_identity(rows)[3], "historyFingerprint": sha256_json(histories),
    }
    return rows, meta, histories, history_meta


class SalesDatasetTests(unittest.TestCase):
    now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)

    def build(self, values=None, **kwargs):
        return build_dataset(*(values or fixture()), now=self.now, **kwargs)

    def test_coherent_projection_contains_no_private_context_or_provider_ids(self):
        values = fixture()
        values[0][0].update(epcMatched=True, uprn="private-uprn", floorAreaSqft=4321,
                            pricePerSqft=1000, historicEngland={"providerToken": "secret"}, epcCertificate="private-epc")
        envelope = self.build(values)
        self.assertEqual(envelope["sourceCheckedAt"], "2026-09-21T10:00:00Z")
        self.assertEqual(envelope["sourceRefreshFrom"], "2025-01-01")
        self.assertEqual(envelope["publishedAt"], "2026-09-21T12:00:00Z")
        payload = envelope["payload"]
        self.assertEqual(envelope["contentSha256"], hashlib.sha256(payload.encode("utf-8")).hexdigest())
        for private in ("private-uprn", "private-epc", "raw-provider", "sourceUrl", "provider.invalid", "floorAreaSqft", "pricePerSqft", "epcMatched", "historicEngland"):
            self.assertNotIn(private, payload)
        decoded = json.loads(payload)
        self.assertEqual(decoded["metadata"]["velocityCutoffDate"], "2026-05-31")
        self.assertEqual(decoded["historyMetadata"]["transactionAliases"], 0)
        self.assertEqual(decoded["historyMetadata"]["historyFingerprint"], sha256_json(decoded["historyByProperty"]))
        self.assertEqual(decoded["historyMetadata"]["sourceCheckedAt"], "2026-09-20T09:00:00Z")
        self.assertTrue(all(record["updatedAt"] == "2026-09-20T09:00:00Z" for record in decoded["historyByProperty"].values()))

    def test_unchanged_verified_check_preserves_content_hash(self):
        values = fixture()
        first = self.build(values)
        values[1]["sourceCheckedAt"] = "2026-09-21T11:00:00Z"
        second = self.build(values)
        self.assertEqual(first["contentSha256"], second["contentSha256"])
        self.assertNotEqual(first["sourceCheckedAt"], second["sourceCheckedAt"])

    def test_actual_property_history_checks_remain_truthful_and_change_generation(self):
        values = fixture()
        first = self.build(values)
        values[3]["sourceCheckedAt"] = "2026-09-21T11:00:00Z"
        values[3]["updatedAt"] = "2026-09-21T11:00:00Z"
        for record in values[2].values():
            record["updatedAt"] = "2026-09-21T11:00:00Z"
        second = self.build(values)
        self.assertNotEqual(first["contentSha256"], second["contentSha256"])
        self.assertEqual(first["sourceCheckedAt"], second["sourceCheckedAt"])

    def test_standalone_snapshot_validates_without_legacy_feeds_and_rejects_tampering(self):
        envelope = self.build()
        payload = validate_envelope(envelope, now=self.now)
        self.assertEqual(len(payload["transactions"]), 2)
        changed = copy.deepcopy(envelope)
        changed["payload"] += " "
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_envelope(changed, now=self.now)
        payload["transactions"][0]["epcMatched"] = True
        changed["payload"] = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        changed["contentSha256"] = hashlib.sha256(changed["payload"].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            validate_envelope(changed, now=self.now)

    def test_new_base_sale_is_not_bounded_by_oldest_other_property_history(self):
        values = fixture()
        newest = max(values[0], key=lambda row: row["date"])
        newest["date"] = "2026-09-21"
        history = values[2][newest["propertyRecordId"]]
        history["transactions"][0]["date"] = "2026-09-21"
        history["updatedAt"] = "2026-09-21T10:00:00Z"
        values[1].update(to="2026-09-21", latestObservedSaleDate="2026-09-21", velocityCutoffDate="2026-07-31")
        envelope = self.build(values)
        self.assertEqual(json.loads(envelope["payload"])["metadata"]["velocityCutoffDate"], "2026-07-31")
        self.assertEqual(envelope["sourceCheckedAt"], "2026-09-21T10:00:00Z")

    def test_later_publication_can_carry_deletions_and_regressed_latest_date(self):
        values = fixture()
        first = self.build(values, published_at="2026-09-21T11:00:00Z")
        removed = max(values[0], key=lambda row: row["date"])
        values[0].remove(removed)
        values[2].pop(removed["id"])
        values[2].pop(removed["propertyRecordId"])
        preserved_source = {key: value for key, value in values[1].items() if key.startswith("source")}
        values[1].update(sweep.metadata(1, values[0]))
        values[1].update(preserved_source)
        values[3]["baseFeedFingerprint"] = base_feed_identity(values[0])[3]
        second = self.build(values)
        self.assertNotEqual(first["contentSha256"], second["contentSha256"])
        self.assertGreater(second["publishedAt"], first["publishedAt"])
        self.assertLess(json.loads(second["payload"])["metadata"]["to"], json.loads(first["payload"])["metadata"]["to"])

    def test_changed_historic_sale_changes_hash_without_advancing_latest_date(self):
        values = fixture()
        first = self.build(values)
        row = min(values[0], key=lambda row: row["date"])
        row["price"] += 10000
        row["priceText"] = "£2.21m"
        history = values[2][row["propertyRecordId"]]["transactions"][0]
        history.update(price=row["price"], priceText=row["priceText"])
        second = self.build(values)
        self.assertNotEqual(first["contentSha256"], second["contentSha256"])
        self.assertEqual(json.loads(first["payload"])["metadata"]["to"], json.loads(second["payload"])["metadata"]["to"])

    def test_missing_stale_retained_and_future_acquisitions_fail_closed(self):
        for mutation in ({"sourceCheckedAt": None}, {"sourceCheckedAt": "2026-07-01T00:00:00Z"},
                         {"sourceFetchStatus": "retained"}, {"sourceCheckedAt": "2026-09-22T00:00:00Z"},
                         {"sourceRefreshFrom": "1995-01-01"}):
            with self.subTest(mutation=mutation):
                values = fixture()
                values[1].update(mutation)
                with self.assertRaises(ValueError):
                    self.build(values)

    def test_missing_changed_history_and_incorrect_cutoff_are_rejected(self):
        values = fixture()
        values[1]["velocityCutoffDate"] = "2026-06-30"
        with self.assertRaisesRegex(ValueError, "cutoff"):
            self.build(values)
        values = fixture()
        values[2][values[0][0]["propertyRecordId"]]["transactions"][0]["price"] += 1
        with self.assertRaisesRegex(ValueError, "omits"):
            self.build(values)
        values = fixture()
        values[2].pop(values[0][0]["propertyRecordId"])
        with self.assertRaisesRegex(ValueError, "property set"):
            self.build(values)

    def test_source_failure_does_not_fallback_to_csv_or_publish(self):
        args = SimpleNamespace(use_current_cache=False, archive_all_years=False,
                               preserve_from_js="", write_js="absent-test-source.js", no_fetch=False,
                               refresh_history=False)
        with patch.object(sweep, "parse_args", return_value=args), \
             patch.object(sweep, "read_existing_js", return_value=([], {})), \
             patch.object(sweep, "fetch_rows", side_effect=TimeoutError("official sources failed")), \
             patch.object(sweep, "read_csv") as cached, patch.object(sweep, "write_js") as write:
            with self.assertRaisesRegex(RuntimeError, "retaining the prior publication"):
                sweep.main()
            cached.assert_not_called()
            write.assert_not_called()

    def test_acquisition_stamp_requires_successful_fetch_and_records_scope(self):
        acquisition = {}
        with patch.object(sweep, "historical_rows", return_value=[]), \
             patch.object(sweep, "fetch_current_rows", side_effect=TimeoutError("query")), \
             patch.object(sweep, "current_rows_from_archives", return_value=[]):
            sweep.fetch_rows(acquisition=acquisition)
        self.assertEqual(acquisition["sourceFetchStatus"], "verified")
        self.assertEqual(acquisition["sourceRefreshMode"], "annual-archives-rolling")
        verified_base_acquisition(acquisition)
        failed = {}
        with patch.object(sweep, "historical_rows", return_value=[]), \
             patch.object(sweep, "fetch_current_rows", side_effect=TimeoutError("query")), \
             patch.object(sweep, "current_rows_from_archives", side_effect=TimeoutError("archive")):
            with self.assertRaises(TimeoutError):
                sweep.fetch_rows(acquisition=failed)
        self.assertNotIn("sourceCheckedAt", failed)

    def test_workflows_publish_snapshot_with_its_exact_source_feeds(self):
        root = Path(__file__).resolve().parents[1]
        monthly = (root / ".github/workflows/monthly-property-refresh.yml").read_text()
        sales = (root / ".github/workflows/sales-history-feed.yml").read_text()
        self.assertIn("python3 scripts/build_sales_dataset.py --check", monthly)
        self.assertIn("git add outputs/today-feed.js outputs/sales-dataset.json", monthly)
        self.assertEqual(sales.count("python3 scripts/build_sales_dataset.py"), 2)
        self.assertEqual(sales.count("--if-verified"), 2)
        self.assertIn("git add outputs/sales-history.js outputs/today-feed.js", sales)
        independent = (root / ".github/workflows/sales-dataset-feed.yml").read_text()
        self.assertIn("git add outputs/sales-dataset.json", independent)
        self.assertIn('--write-js "$RUNNER_TEMP/native-sales-transactions.js"', independent)
        self.assertNotIn("scripts/enrich", independent)
        self.assertNotIn("epc-cache", independent)

    def test_boolean_schema_versions_are_rejected(self):
        values = fixture()
        values[1]["propertyRecordSchemaVersion"] = True
        with self.assertRaises(ValueError):
            self.build(values)
        values = fixture()
        values[3]["schemaVersion"] = True
        with self.assertRaises(ValueError):
            self.build(values)
        envelope = self.build()
        envelope["schemaVersion"] = True
        with self.assertRaises(ValueError):
            validate_envelope(envelope, now=self.now)

    def test_legacy_unverified_base_keeps_prior_native_snapshot_unchanged(self):
        import build_sales_dataset as producer
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.js"
            output = Path(directory) / "sales-dataset.json"
            base.write_text('window.SURREY_LAND_REG_TRANSACTIONS = [];\nwindow.SURREY_LAND_REG_META = {};\n')
            output.write_text("prior native snapshot")
            with patch.object(sys, "argv", ["producer", "--transactions", str(base), "--output", str(output), "--if-verified"]):
                producer.main()
            self.assertEqual(output.read_text(), "prior native snapshot")

    def test_changed_properties_detect_same_count_correction_deletion_and_old_new_identities(self):
        prior = fixture()[0]
        changed = copy.deepcopy(prior)
        changed[0]["price"] += 10000
        expected = {prior[0]["propertyRecordId"]}
        self.assertEqual(collector.changed_base_properties(changed, prior), expected)
        older = {**prior[0], "id": "lr-" + "f" * 20, "date": "2020-01-02"}
        self.assertEqual(collector.changed_base_properties(prior, prior + [older]), expected)
        moved = copy.deepcopy(prior)
        moved[0]["propertyRecordId"] = "property:CORRECTED EXACT ID|KT109AA"
        self.assertEqual(collector.changed_base_properties(moved, prior), expected | {moved[0]["propertyRecordId"]})
        enriched = copy.deepcopy(prior)
        enriched[0].update(epcMatched=True, floorAreaSqft=4321, estateRegistryVersion="new-policy")
        self.assertEqual(collector.changed_base_properties(enriched, prior), set())

    def test_withdrawn_sale_forces_actual_history_fetch_despite_fresh_seed_and_cache(self):
        rows, _meta, histories, _history_meta = fixture()
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for record in histories.values():
            record["updatedAt"] = checked_at
        target = rows[0]
        withdrawn = {**target, "id": "lr-" + "f" * 20, "date": "2020-01-02"}
        withdrawn_history = {key: withdrawn[key] for key in ("price", "priceText", "date", "propertyType", "category")}
        withdrawn_history.update(id="withdrawn-history-sale", source=SOURCE_NAME)
        histories[target["propertyRecordId"]]["transactions"].append(withdrawn_history)
        histories[target["propertyRecordId"]]["totalTransactions"] = 2
        raw = {"tx": "fresh-source-sale", "paon": target["paon"], "street": target["street"],
               "postcode": target["postcode"], "price": str(target["price"]), "date": target["date"],
               "propertyType": "D", "category": "A"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current, prior, output = root / "base.js", root / "prior.js", root / "history.js"
            for path, ledger in ((current, rows), (prior, rows + [withdrawn])):
                path.write_text("window.SURREY_LAND_REG_TRANSACTIONS = " + json.dumps(ledger) + ";\n")
            cache = {"version": 1, "postcodes": {"KT109AA": {"updatedAt": checked_at, "rows": [raw]}}}
            argv = ["collector", "--input", str(current), "--prior-base-feed", str(prior),
                    "--output", str(output), "--cache", str(root / "cache.json"),
                    "--seed-feed", str(root / "seed.js"), "--deployment-mode", "commercial", "--pause", "0"]
            with patch.object(sys, "argv", argv), patch.object(collector, "load_seed_history", return_value=histories), \
                 patch.object(collector, "load_cache", return_value=cache), \
                 patch.object(collector, "fetch_batch", return_value={"KT109AA": [raw]}) as fetch:
                collector.main()
            fetch.assert_called_once()
            result = collector.assignment(output.read_text(), "SURREY_SALES_HISTORY")
            actual = result[target["propertyRecordId"]]["transactions"]
            self.assertTrue(actual)
            self.assertNotIn("2020-01-02", {sale["date"] for sale in actual})


if __name__ == "__main__":
    unittest.main()
