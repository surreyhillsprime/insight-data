import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_title_history import (  # noqa: E402
    RequestPacer,
    cache_coverage,
    fetch_batch,
    load_seed_history,
    main as collect_title_history_main,
    migrate_existing_history,
    retry_wait_seconds,
    seed_record_is_fresh,
)
from validate_sales_history_feed import (  # noqa: E402
    ADDRESS_DATA_USE,
    ATTRIBUTION,
    REDISTRIBUTION_RIGHTS,
    SOURCE_LICENCE_URL,
    SOURCE_NAME,
    assignment,
    base_feed_identity,
    read_base_feed,
    sha256_json,
    validate,
)


def timestamp(days_ago=0):
    value = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_assignments(path, assignments):
    path.write_text(
        "\n".join(
            f"window.{name} = {json.dumps(value, separators=(',', ':'))};"
            for name, value in assignments.items()
        )
        + "\n",
        encoding="utf-8",
    )


class SalesHistoryPublicationContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.base_path = self.root / "base.js"
        self.sales_path = self.root / "sales.js"
        self.property_one = "property:1 TEST ROAD|KT100AA"
        self.property_two = "property:2 TEST ROAD|KT100AA"
        self.base_rows = [
            {"id": "txn-one", "propertyRecordId": self.property_one},
            {"id": "txn-two", "propertyRecordId": self.property_two},
        ]
        self.checked_at = timestamp()
        write_assignments(
            self.base_path,
            {"SURREY_LAND_REG_TRANSACTIONS": self.base_rows},
        )

    def tearDown(self):
        self.directory.cleanup()

    def assignments(self):
        sale = {
            "id": "hmlr-sale-one",
            "address": "1 TEST ROAD",
            "postcode": "KT10 0AA",
            "price": 2_000_000,
            "date": "2025-01-02",
            "source": SOURCE_NAME,
        }
        complete = {
            "propertyRecordId": self.property_one,
            "coverageStatus": "complete",
            "totalTransactions": 1,
            "latestTransaction": sale,
            "transactions": [sale],
            "source": SOURCE_NAME,
            "updatedAt": self.checked_at,
        }
        unavailable = {
            "propertyRecordId": self.property_two,
            "coverageStatus": "unavailable",
            "coverageReason": "No postcode in the source Price Paid record",
            "totalTransactions": 0,
            "latestTransaction": None,
            "transactions": [],
            "source": SOURCE_NAME,
            "updatedAt": self.checked_at,
        }
        histories = {
            self.property_one: complete,
            self.property_two: unavailable,
            "txn-one": complete,
            "txn-two": unavailable,
        }
        _, _, _, base_fingerprint = base_feed_identity(self.base_rows)
        metadata = {
            "schemaVersion": 1,
            "deploymentMode": "commercial",
            "publicationStatus": "complete",
            "coverageMode": "full-available-price-paid-history",
            "coverageStatus": "complete-accounted",
            "source": SOURCE_NAME,
            "sourceLicenceUrl": SOURCE_LICENCE_URL,
            "redistributionRights": REDISTRIBUTION_RIGHTS,
            "addressDataUse": ADDRESS_DATA_USE,
            "attribution": ATTRIBUTION,
            "coverageFrom": "1995",
            "updatedAt": self.checked_at,
            "sourceCheckedAt": self.checked_at,
            "freshnessWindowDays": 45,
            "propertiesRequested": 2,
            "propertiesChecked": 1,
            "propertiesUnavailable": 1,
            "propertiesNotChecked": 0,
            "propertiesWithHistory": 1,
            "propertiesCheckedNoHistory": 0,
            "transactionsFound": 1,
            "lookupKeys": 4,
            "canonicalPropertyRecords": 2,
            "transactionAliases": 2,
            "baseFeedFingerprint": base_fingerprint,
            "historyFingerprint": sha256_json(histories),
        }
        return {
            "SURREY_SALES_HISTORY": histories,
            "SURREY_SALES_HISTORY_META": metadata,
        }

    def write_sales(self, assignments):
        write_assignments(self.sales_path, assignments)

    def make_seed_stale(self, assignments, days_ago=60):
        stale_at = timestamp(days_ago=days_ago)
        histories = assignments["SURREY_SALES_HISTORY"]
        complete = copy.deepcopy(histories[self.property_one])
        complete["updatedAt"] = stale_at
        histories[self.property_one] = complete
        histories["txn-one"] = complete
        metadata = assignments["SURREY_SALES_HISTORY_META"]
        metadata["sourceCheckedAt"] = stale_at
        metadata["historyFingerprint"] = sha256_json(histories)
        return stale_at

    def test_accepts_exact_canonical_and_transaction_identity_universe(self):
        self.write_sales(self.assignments())
        result = validate(
            self.sales_path,
            base_feed=self.base_path,
            minimum_properties_with_history=1,
            minimum_transactions=1,
            maximum_properties_unavailable=1,
        )
        self.assertEqual(result["canonicalPropertyRecords"], 2)
        self.assertEqual(result["transactionAliases"], 2)

    def test_rejects_property_denominator_coverage_regression(self):
        self.write_sales(self.assignments())
        with self.assertRaisesRegex(ValueError, "property-denominator"):
            validate(
                self.sales_path,
                base_feed=self.base_path,
                minimum_property_coverage_percent=51,
            )

    def test_history_migration_unions_old_property_alias_records(self):
        canonical_id = "property:CHIMNEYS YAFFLE ROAD WEYBRIDGE KT13 0QF|KT130QF"
        transactions = [
            {
                "id": "lr-old",
                "propertyRecordId": canonical_id,
                "address": "CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, KT13 0QF",
                "postcode": "KT13 0QF",
            },
            {
                "id": "lr-new",
                "propertyRecordId": canonical_id,
                "address": "CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, KT13 0QF",
                "postcode": "KT13 0QF",
            },
        ]
        old_ids = [
            "property:CHIMNEYS YAFFLE ROAD WEYBRIDGE WEYBRIDGE KT13 0QF|KT130QF",
            canonical_id,
        ]
        prior_history = {}
        for index, (transaction_id, old_id) in enumerate(
            zip(("lr-old", "lr-new"), old_ids),
            start=1,
        ):
            sale = {
                "id": f"hmlr-{index}",
                "address": (
                    "CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, WEYBRIDGE, KT13 0QF"
                    if index == 1
                    else "CHIMNEYS, YAFFLE ROAD, WEYBRIDGE, KT13 0QF"
                ),
                "postcode": "KT13 0QF",
                "price": index * 1_000_000,
                "date": f"200{index}-01-01",
                "source": SOURCE_NAME,
            }
            record = {
                "propertyRecordId": old_id,
                "coverageStatus": "complete",
                "transactions": [sale],
                "updatedAt": self.checked_at,
                "matchMethod": "exact-address",
            }
            prior_history[old_id] = record
            prior_history[transaction_id] = record

        history, metadata = migrate_existing_history(
            transactions,
            prior_history,
            {"sourceCheckedAt": self.checked_at},
            "commercial",
        )

        self.assertEqual(history[canonical_id]["totalTransactions"], 2)
        self.assertEqual(
            history[canonical_id]["matchMethod"],
            "canonical-property-address-alias-union",
        )
        self.assertIs(history["lr-old"], history[canonical_id])
        self.assertIs(history["lr-new"], history[canonical_id])
        self.assertEqual(metadata["canonicalPropertyRecords"], 1)
        self.assertEqual(metadata["transactionsFound"], 2)

    def test_rejects_same_count_base_identity_pair_mutation(self):
        assignments = self.assignments()
        histories = assignments["SURREY_SALES_HISTORY"]
        histories["txn-one"] = histories[self.property_two]
        histories["txn-two"] = histories[self.property_one]
        assignments["SURREY_SALES_HISTORY_META"]["historyFingerprint"] = (
            sha256_json(histories)
        )
        self.write_sales(assignments)
        write_assignments(
            self.base_path,
            {
                "SURREY_LAND_REG_TRANSACTIONS": [
                    {"id": "txn-one", "propertyRecordId": self.property_two},
                    {"id": "txn-two", "propertyRecordId": self.property_one},
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "baseFeedFingerprint"):
            validate(self.sales_path, base_feed=self.base_path)

    def test_complete_record_must_include_each_sale_proven_by_the_base(self):
        assignments = self.assignments()
        self.write_sales(assignments)
        write_assignments(
            self.base_path,
            {"SURREY_LAND_REG_TRANSACTIONS": [
                {
                    "id": "txn-one",
                    "propertyRecordId": self.property_one,
                    "date": "2026-01-02",
                    "price": 2_000_000,
                },
                {"id": "txn-two", "propertyRecordId": self.property_two},
            ]},
        )
        with self.assertRaisesRegex(ValueError, "omits a sale proven"):
            validate(self.sales_path, base_feed=self.base_path)

    def test_rejects_content_mutation_with_unchanged_history_fingerprint(self):
        assignments = self.assignments()
        changed = copy.deepcopy(
            assignments["SURREY_SALES_HISTORY"][self.property_one]
        )
        changed["transactions"][0]["price"] = 2_100_000
        changed["latestTransaction"] = changed["transactions"][0]
        assignments["SURREY_SALES_HISTORY"][self.property_one] = changed
        assignments["SURREY_SALES_HISTORY"]["txn-one"] = changed
        self.write_sales(assignments)
        with self.assertRaisesRegex(ValueError, "historyFingerprint"):
            validate(self.sales_path, base_feed=self.base_path)

    def test_failed_refresh_never_presents_retained_rows_as_complete(self):
        status, reason, rows, checked_at = cache_coverage(
            "KT100AA",
            {"KT100AA"},
            {
                "updatedAt": timestamp(days_ago=60),
                "rows": [{"tx": "stale-retained-row"}],
                "lastError": "TimeoutError: source unavailable",
            },
        )
        self.assertEqual(status, "unavailable")
        self.assertIn("source unavailable", reason)
        self.assertEqual(rows, [])
        self.assertEqual(checked_at, "")

    def test_stale_successful_cache_cannot_override_a_fresher_seed(self):
        status, reason, rows, checked_at = cache_coverage(
            "KT100AA",
            {"KT100AA"},
            {
                "updatedAt": timestamp(days_ago=60),
                "rows": [{"tx": "stale-row-without-an-error"}],
            },
            refresh_days=28,
        )
        self.assertEqual(status, "unavailable")
        self.assertIn("stale", reason)
        self.assertEqual(rows, [])
        self.assertEqual(checked_at, "")

    def test_fresh_complete_publication_can_seed_a_self_healing_refresh(self):
        record = self.assignments()["SURREY_SALES_HISTORY"][self.property_one]
        now = datetime.now(timezone.utc)
        self.assertTrue(
            seed_record_is_fresh(record, self.property_one, 28, now=now)
        )
        unavailable = copy.deepcopy(record)
        unavailable["coverageStatus"] = "unavailable"
        self.assertFalse(
            seed_record_is_fresh(unavailable, self.property_one, 28, now=now)
        )
        self.assertFalse(
            seed_record_is_fresh(record, self.property_two, 28, now=now)
        )
        stale = copy.deepcopy(record)
        stale["updatedAt"] = timestamp(days_ago=29)
        self.assertFalse(
            seed_record_is_fresh(stale, self.property_one, 28, now=now)
        )
        self.assertFalse(
            seed_record_is_fresh(
                record,
                self.property_one,
                28,
                required_sales=[{
                    "date": "2026-08-01",
                    "price": 2_500_000,
                }],
                now=now,
            )
        )

    def test_stale_seed_with_tampered_history_is_rejected(self):
        assignments = self.assignments()
        self.make_seed_stale(assignments)
        changed = copy.deepcopy(
            assignments["SURREY_SALES_HISTORY"][self.property_one]
        )
        changed["transactions"][0]["price"] = 2_100_000
        changed["latestTransaction"] = changed["transactions"][0]
        assignments["SURREY_SALES_HISTORY"][self.property_one] = changed
        assignments["SURREY_SALES_HISTORY"]["txn-one"] = changed
        self.write_sales(assignments)
        with self.assertRaisesRegex(ValueError, "historyFingerprint"):
            load_seed_history(self.sales_path, 28)

    def test_exact_commercial_base_allows_valid_seed_reuse(self):
        self.write_sales(self.assignments())
        history = load_seed_history(self.sales_path, 28)
        self.assertEqual(history[self.property_one]["coverageStatus"], "complete")

    def test_stale_unbound_seed_rejects_a_forged_base_identity(self):
        assignments = self.assignments()
        self.make_seed_stale(assignments)
        assignments["SURREY_SALES_HISTORY_META"]["baseFeedFingerprint"] = "0" * 64
        assignments["SURREY_SALES_HISTORY_META"]["historyFingerprint"] = sha256_json(
            assignments["SURREY_SALES_HISTORY"]
        )
        self.write_sales(assignments)
        with self.assertRaisesRegex(ValueError, "transaction aliases"):
            load_seed_history(self.sales_path, 28)

    def test_valid_stale_seed_triggers_source_refresh(self):
        assignments = self.assignments()
        stale_at = self.make_seed_stale(assignments, days_ago=29)
        self.write_sales(assignments)
        with self.assertRaisesRegex(ValueError, "stale"):
            validate(
                self.sales_path,
                base_feed=self.base_path,
                maximum_age_days=28,
            )
        current_base = self.root / "current-base.js"
        output = self.root / "aligned-sales.js"
        cache = self.root / "cache.json"
        write_assignments(
            current_base,
            {
                "SURREY_LAND_REG_TRANSACTIONS": [{
                    "id": "txn-one",
                    "propertyRecordId": self.property_one,
                    "address": "1 TEST ROAD",
                    "postcode": "KT10 0AA",
                    "date": "2025-01-02",
                    "price": 2_000_000,
                    "propertyType": "Detached",
                    "category": "A",
                }]
            },
        )
        refreshed_row = {
            "tx": "hmlr-refreshed",
            "paon": "1",
            "street": "TEST ROAD",
            "postcode": "KT10 0AA",
            "price": "2000000",
            "date": "2025-01-02",
            "propertyType": (
                "http://landregistry.data.gov.uk/def/common/detached"
            ),
            "category": (
                "http://landregistry.data.gov.uk/def/ppi/"
                "standard-price-paid-transaction"
            ),
        }
        argv = [
            "collect_title_history.py",
            "--input", str(current_base),
            "--output", str(output),
            "--cache", str(cache),
            "--seed-feed", str(self.sales_path),
            "--deployment-mode", "commercial",
            "--refresh-days", "28",
            "--workers", "1",
            "--batch-size", "1",
            "--pause", "0",
        ]
        with patch.object(sys, "argv", argv), patch(
            "collect_title_history.fetch_batch",
            return_value={"KT100AA": [refreshed_row]},
        ) as fetch:
            collect_title_history_main()

        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args, (["KT10 0AA"], 90))
        self.assertIsInstance(fetch.call_args.kwargs["request_pacer"], RequestPacer)
        text = output.read_text(encoding="utf-8")
        histories = assignment(text, "SURREY_SALES_HISTORY")
        metadata = assignment(text, "SURREY_SALES_HISTORY_META")
        self.assertEqual(
            histories[self.property_one]["transactions"][0]["id"],
            "hmlr-refreshed",
        )
        self.assertNotEqual(histories[self.property_one]["updatedAt"], stale_at)
        self.assertNotEqual(metadata["sourceCheckedAt"], stale_at)
        validate(
            output,
            base_feed=current_base,
            minimum_properties_with_history=1,
            minimum_transactions=1,
            maximum_properties_unavailable=0,
            maximum_age_days=1,
        )

    def test_valid_prior_publication_beats_an_incomplete_fresh_cache(self):
        self.write_sales(self.assignments())
        current_base = self.root / "current-base.js"
        output = self.root / "aligned-sales.js"
        cache = self.root / "cache.json"
        write_assignments(
            current_base,
            {
                "SURREY_LAND_REG_TRANSACTIONS": [{
                    "id": "txn-one",
                    "propertyRecordId": self.property_one,
                    "address": "1 TEST ROAD",
                    "postcode": "KT10 0AA",
                    "date": "2025-01-02",
                    "price": 2_000_000,
                    "propertyType": "Detached",
                    "category": "A",
                }]
            },
        )
        cache.write_text(json.dumps({
            "version": 1,
            "postcodes": {
                "KT100AA": {"updatedAt": timestamp(), "rows": []}
            },
        }), encoding="utf-8")
        argv = [
            "collect_title_history.py",
            "--input", str(current_base),
            "--output", str(output),
            "--cache", str(cache),
            "--seed-feed", str(self.sales_path),
            "--deployment-mode", "commercial",
            "--refresh-days", "28",
        ]
        with patch.object(sys, "argv", argv), patch(
            "collect_title_history.fetch_batch"
        ) as fetch:
            collect_title_history_main()
        fetch.assert_not_called()
        text = output.read_text(encoding="utf-8")
        histories = json.loads(
            text.split("window.SURREY_SALES_HISTORY = ", 1)[1].split(";", 1)[0]
        )
        self.assertEqual(
            histories[self.property_one]["transactions"][0]["id"],
            "hmlr-sale-one",
        )

    def test_force_refresh_can_consume_the_just_fetched_cache(self):
        status, _reason, rows, checked_at = cache_coverage(
            "KT100AA",
            {"KT100AA"},
            {
                "updatedAt": timestamp(),
                "rows": [{"tx": "newly-fetched-row"}],
            },
            refresh_days=None,
        )
        self.assertEqual(status, "complete")
        self.assertEqual(rows, [{"tx": "newly-fetched-row"}])
        self.assertTrue(checked_at)

    def test_fetch_batch_retries_rate_limit_and_honours_retry_after(self):
        rate_limit = HTTPError(
            url="https://landregistry.example/query",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "7"},
            fp=None,
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "results": {"bindings": []}
        }).encode("utf-8")
        with patch(
            "collect_title_history.urllib.request.urlopen",
            side_effect=[rate_limit, response],
        ) as urlopen, patch("collect_title_history.time.sleep") as sleep:
            result = fetch_batch(["KT10 0AA"], 90)

        self.assertEqual(result, {"KT100AA": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(7.0)

    def test_fetch_batch_exhausts_bounded_rate_limit_retries(self):
        errors = [
            HTTPError(
                url="https://landregistry.example/query",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=None,
            )
            for _ in range(3)
        ]
        with patch(
            "collect_title_history.urllib.request.urlopen",
            side_effect=errors,
        ) as urlopen, patch("collect_title_history.time.sleep") as sleep:
            with self.assertRaises(HTTPError):
                fetch_batch(["KT10 0AA"], 90, retries=2)

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_request_pacer_spaces_starts_across_shared_workers(self):
        pacer = RequestPacer(1)
        with patch(
            "collect_title_history.time.monotonic",
            side_effect=[10.0, 10.25, 11.5, 11.75, 12.5],
        ), patch("collect_title_history.time.sleep") as sleep:
            pacer.wait()
            pacer.wait()
            pacer.wait()

        self.assertEqual(sleep.call_args_list, [call(0.75), call(0.75)])

    def test_request_pacer_applies_server_cooldown_to_shared_workers(self):
        pacer = RequestPacer(1)
        with patch(
            "collect_title_history.time.monotonic",
            side_effect=[10.0, 10.25, 17.0],
        ), patch("collect_title_history.time.sleep") as sleep:
            pacer.defer(7)
            pacer.wait()

        sleep.assert_called_once_with(6.75)

    def test_rate_limit_wait_is_bounded_with_safe_fallback(self):
        self.assertEqual(retry_wait_seconds("3600", 0), 30)
        self.assertEqual(retry_wait_seconds(None, 0), 4)

    def test_unavailable_property_regression_is_fail_closed(self):
        self.write_sales(self.assignments())
        with self.assertRaisesRegex(ValueError, "unavailable-property ceiling"):
            validate(
                self.sales_path,
                base_feed=self.base_path,
                maximum_properties_unavailable=0,
            )

    def test_source_freshness_is_oldest_complete_lookup_not_rewrite_time(self):
        assignments = self.assignments()
        oldest_check = timestamp(days_ago=5)
        complete = copy.deepcopy(
            assignments["SURREY_SALES_HISTORY"][self.property_one]
        )
        complete["updatedAt"] = oldest_check
        assignments["SURREY_SALES_HISTORY"][self.property_one] = complete
        assignments["SURREY_SALES_HISTORY"]["txn-one"] = complete
        assignments["SURREY_SALES_HISTORY_META"]["historyFingerprint"] = (
            sha256_json(assignments["SURREY_SALES_HISTORY"])
        )
        self.write_sales(assignments)
        with self.assertRaisesRegex(ValueError, "oldest complete lookup"):
            validate(self.sales_path, base_feed=self.base_path)

        assignments["SURREY_SALES_HISTORY_META"]["sourceCheckedAt"] = oldest_check
        self.write_sales(assignments)
        validate(self.sales_path, base_feed=self.base_path, maximum_age_days=10)
        with self.assertRaisesRegex(ValueError, "stale"):
            validate(self.sales_path, base_feed=self.base_path, maximum_age_days=1)

    def test_checked_in_sales_feed_preserves_reviewed_counts(self):
        base_rows = read_base_feed(ROOT / "outputs" / "surrey-transactions.js")
        canonical_properties = {
            item["propertyRecordId"] for item in base_rows
        }
        result = validate(
            ROOT / "outputs" / "sales-history.js",
            base_feed=ROOT / "outputs" / "surrey-transactions.js",
            minimum_property_coverage_percent=99,
            minimum_transactions=6735,
            maximum_properties_unavailable=4,
            maximum_age_days=45,
        )
        self.assertEqual(result["canonicalPropertyRecords"], len(canonical_properties))
        self.assertEqual(result["transactionAliases"], len(base_rows))
        self.assertEqual(
            result["lookupKeys"], len(canonical_properties) + len(base_rows)
        )
        self.assertGreaterEqual(
            result["propertiesWithHistory"] * 100,
            len(canonical_properties) * 99,
        )
        self.assertGreaterEqual(result["transactionsFound"], 6735)

    def test_workflows_apply_remote_contract_after_rebase_and_daily(self):
        sales_workflow = (
            ROOT / ".github" / "workflows" / "sales-history-feed.yml"
        ).read_text(encoding="utf-8")
        pull_index = sales_workflow.rindex("git pull --rebase --autostash")
        add_index = sales_workflow.rindex("git add outputs/sales-history.js")
        pulled_revalidation_index = sales_workflow.index(
            "python3 scripts/validate_sales_history_feed.py",
            pull_index,
            add_index,
        )
        retry_index = sales_workflow.rindex("for attempt in 1 2 3; do")
        rebase_index = sales_workflow.index(
            'git rebase --autostash "origin/$GITHUB_REF_NAME"', retry_index
        )
        retry_revalidation_index = sales_workflow.index(
            "python3 scripts/validate_sales_history_feed.py", rebase_index
        )
        push_index = sales_workflow.index(
            'git push origin "HEAD:$GITHUB_REF_NAME"', retry_revalidation_index
        )
        self.assertLess(pull_index, pulled_revalidation_index)
        self.assertLess(pulled_revalidation_index, add_index)
        self.assertLess(add_index, retry_index)
        self.assertLess(rebase_index, retry_revalidation_index)
        self.assertLess(retry_revalidation_index, push_index)
        self.assertEqual(
            sales_workflow.count("--minimum-property-coverage-percent 99"),
            3,
        )
        self.assertEqual(sales_workflow.count("--minimum-transactions 6735"), 3)
        self.assertEqual(
            sales_workflow.count("--maximum-properties-unavailable 4"),
            3,
        )
        self.assertIn("--refresh-days 28", sales_workflow)
        self.assertIn("--workers 1", sales_workflow)
        self.assertIn("--pause 1", sales_workflow)
        self.assertIn("--seed-feed outputs/sales-history.js", sales_workflow)
        self.assertIn("check_data_completeness.py --base-only", sales_workflow)
        preflight_index = sales_workflow.index(
            "check_data_completeness.py --base-only"
        )
        producer_index = sales_workflow.index("collect_title_history.py")
        self.assertLess(preflight_index, producer_index)

        monthly = (
            ROOT / ".github" / "workflows" / "monthly-property-refresh.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("--workers 1", monthly)
        self.assertIn("--pause 1", monthly)

        daily = (
            ROOT / ".github" / "workflows" / "data-completeness.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts/validate_sales_history_feed.py", daily)
        self.assertIn("--minimum-property-coverage-percent 99", daily)
        self.assertIn("--minimum-transactions 6735", daily)
        self.assertIn("--maximum-properties-unavailable 4", daily)
        self.assertIn("scripts/validate_planning_feed.py", daily)
        self.assertIn("--minimum-property-coverage-percent 79", daily)
        self.assertIn("--minimum-applications 20000", daily)


if __name__ == "__main__":
    unittest.main()
