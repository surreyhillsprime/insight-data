import json
import inspect
import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from today_feed import build_today_feed, canonical_json  # noqa: E402
from validate_today_feed import (  # noqa: E402
    read_today_feed,
    validate_today_feed,
)


def coverage(source_key, mode="full-available-history"):
    return {
        "status": "complete",
        "complete": True,
        "coverageMode": mode,
        "basis": f"Fixture coverage for {source_key}",
        "checkedAt": "2026-07-20T00:00:00Z",
        "limitations": [f"Fixture {source_key} limitation."],
        "evidenceIds": [f"evidence:coverage:{source_key}"],
    }


def evidence(evidence_id, source, source_id, effective_date, data):
    return {
        "evidenceId": evidence_id,
        "propertyId": data.get("propertyId", ""),
        "type": data.get("type", ""),
        "source": source,
        "sourceId": source_id,
        "effectiveDate": effective_date,
        "data": data,
    }


def fixture_record(
    property_id,
    address,
    latest_sale,
    *,
    estate="Example Estate",
    events=None,
    evidence_rows=None,
):
    return {
        "schemaVersion": 1,
        "propertyId": property_id,
        "canonicalAddress": address,
        "postcode": "KT10 0AA",
        "profile": {
            "market": "elmbridge-prime",
            "district": "Elmbridge",
            "town": "ESHER",
            "estate": estate,
            "estateId": "example-estate",
        },
        "metrics": {"latestSaleDate": latest_sale},
        "coverage": {
            "sales": coverage("sales", "price-paid-from-date"),
            "epc": coverage("epc", "matched-certificates-on-qualifying-transactions"),
            "planning": coverage("planning"),
        },
        "events": list(events or []),
        "evidence": list(evidence_rows or []),
        "context": {
            "latitude": 51.35,
            "longitude": -0.36,
            "coordinatePrecision": "postcode-centroid",
        },
    }


class TodayFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (ROOT / "config" / "today-feed.schema.json").read_text(encoding="utf-8")
        )

    def fixture(self):
        property_a = "property:1 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        sale_a_id = "evidence:sale:a"
        epc_a_id = "evidence:epc:a"
        record_a = fixture_record(
            property_a,
            "1 EXAMPLE ROAD ESHER KT10 0AA",
            "1998-01-10",
            events=[
                {
                    "eventId": "event:sale:a",
                    "propertyId": property_a,
                    "type": "sale",
                    "date": "1998-01-10",
                    "datePrecision": "day",
                    "summary": "Registered sale for £2.1m",
                    "source": "HM Land Registry Price Paid Data",
                    "evidenceIds": [sale_a_id],
                },
                {
                    "eventId": "event:epc:a",
                    "propertyId": property_a,
                    "type": "epc_certificate",
                    "date": "2026-07-01",
                    "datePrecision": "day",
                    "summary": "EPC certificate: 4,000 sq ft, rating C",
                    "source": "MHCLG EPC Register",
                    "evidenceIds": [epc_a_id],
                },
            ],
            evidence_rows=[
                evidence(
                    sale_a_id,
                    "HM Land Registry Price Paid Data",
                    "sale-a",
                    "1998-01-10",
                    {"propertyId": property_a, "type": "sale", "date": "1998-01-10"},
                ),
                evidence(
                    epc_a_id,
                    "MHCLG EPC Register",
                    "epc-a",
                    "2026-07-01",
                    {
                        "propertyId": property_a,
                        "type": "epc_certificate",
                        "date": "2026-07-01",
                        "rating": "C",
                        "floorAreaSqft": 4000,
                    },
                ),
            ],
        )

        property_b = "property:2 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        sale_b_id = "evidence:sale:b"
        planning_b_id = "evidence:planning:b"
        record_b = fixture_record(
            property_b,
            "2 EXAMPLE ROAD ESHER KT10 0AA",
            "2024-02-01",
            events=[
                {
                    "eventId": "event:sale:b",
                    "propertyId": property_b,
                    "type": "sale",
                    "date": "2024-02-01",
                    "datePrecision": "day",
                    "summary": "Registered sale for £3m",
                    "source": "HM Land Registry Price Paid Data",
                    "evidenceIds": [sale_b_id],
                },
                {
                    "eventId": "event:planning:b",
                    "propertyId": property_b,
                    "type": "planning_application",
                    "date": "2026",
                    "datePrecision": "year",
                    "summary": "Two-storey rear extension",
                    "source": "Elmbridge Borough Council planning search",
                    "evidenceIds": [planning_b_id],
                },
            ],
            evidence_rows=[
                evidence(
                    sale_b_id,
                    "HM Land Registry Price Paid Data",
                    "sale-b",
                    "2024-02-01",
                    {"propertyId": property_b, "type": "sale", "date": "2024-02-01"},
                ),
                evidence(
                    planning_b_id,
                    "Elmbridge Borough Council planning search",
                    "PLAN/2026/2",
                    "2026",
                    {
                        "propertyId": property_b,
                        "type": "planning_application",
                        "date": "2026",
                        "datePrecision": "year",
                        "reference": "PLAN/2026/2",
                        "proposal": "Two-storey rear extension",
                        "siteAddress": "2 Example Road Esher KT10 0AA",
                        "matchConfidence": 1.0,
                        "portalUrl": "https://planning.example/PLAN-2026-2",
                    },
                ),
            ],
        )

        property_c = "property:3 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        sale_c_id = "evidence:sale:c"
        record_c = fixture_record(
            property_c,
            "3 EXAMPLE ROAD ESHER KT10 0AA",
            "2018-01-01",
            estate="Another Estate",
            events=[{
                "eventId": "event:sale:c",
                "propertyId": property_c,
                "type": "sale",
                "date": "2018-01-01",
                "datePrecision": "day",
                "summary": "Registered sale for £2.5m",
                "source": "HM Land Registry Price Paid Data",
                "evidenceIds": [sale_c_id],
            }],
            evidence_rows=[evidence(
                sale_c_id,
                "HM Land Registry Price Paid Data",
                "sale-c",
                "2018-01-01",
                {"propertyId": property_c, "type": "sale", "date": "2018-01-01"},
            )],
        )
        news = [{
            "id": "news-example",
            "title": "Record plot transaction on Example Estate",
            "url": "https://news.example/example-estate",
            "source": "Example News",
            "sourceId": "example-news",
            "rightsMode": "link-only",
            "publishedAt": "2026-07-20T09:00:00Z",
            "score": 75,
            "location": "Example Estate",
            "matchType": "estate",
            "topics": ["Transaction"],
            "reason": "Matches Example Estate · Transaction",
        }]
        records = {
            property_a: record_a,
            property_b: record_b,
            property_c: record_c,
        }
        return records, news

    def build(self, records):
        return build_today_feed(
            records,
            {
                "datasetFingerprint": "a" * 64,
                "generatedAt": "2026-07-21T00:00:00Z",
            },
            as_of="2026-07-25",
            generated_at="2026-07-25T08:00:00Z",
        )

    def test_feed_is_deterministic_and_excludes_year_only_planning(self):
        records, news = self.fixture()
        feed, metadata = self.build(records)
        reversed_feed, reversed_metadata = self.build(
            dict(reversed(list(records.items())))
        )

        self.assertEqual(canonical_json(feed), canonical_json(reversed_feed))
        self.assertEqual(canonical_json(metadata), canonical_json(reversed_metadata))
        planning = [item for item in feed["signals"] if item["kind"] == "property_planning"]
        self.assertEqual(planning, [])

    def test_fresh_high_confidence_exact_property_planning_generates_signal(self):
        records, _news = self.fixture()
        property_id = "property:2 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        record = records[property_id]
        event = next(item for item in record["events"] if item["type"] == "planning_application")
        planning_evidence = next(
            item for item in record["evidence"] if item["type"] == "planning_application"
        )
        event.update({"date": "2026-07-10", "datePrecision": "day"})
        planning_evidence["effectiveDate"] = "2026-07-10"
        planning_evidence["data"].update({
            "date": "2026-07-10",
            "datePrecision": "day",
        })

        feed, metadata = self.build(records)

        planning = [item for item in feed["signals"] if item["kind"] == "property_planning"]
        self.assertEqual(len(planning), 1)
        self.assertEqual(planning[0]["property"]["propertyId"], property_id)
        self.assertEqual(planning[0]["effectiveDate"], "2026-07-10")
        self.assertEqual(planning[0]["rank"], 88)
        self.assertEqual(planning[0]["attributes"]["matchConfidence"], 1)
        self.assertEqual(planning[0]["attributes"]["planningRecordType"], "application")
        self.assertTrue(planning[0]["fact"].startswith("Planning application"))
        opportunity = next(
            item for item in feed["opportunities"]
            if item["property"]["propertyId"] == property_id
        )
        self.assertEqual(opportunity["opportunityLevel"], "Standard")
        self.assertEqual(opportunity["independentSourceCount"], 1)
        self.assertEqual(opportunity["corroborationIds"], [])
        self.assertEqual(metadata["criteria"]["planningLookbackDays"], 45)
        validate_today_feed(feed, metadata, self.schema)

    def test_planning_approval_requires_an_explicit_positive_decision(self):
        records, _news = self.fixture()
        property_id = "property:2 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        record = records[property_id]
        event = next(item for item in record["events"] if item["type"] == "planning_application")
        planning_evidence = next(
            item for item in record["evidence"] if item["type"] == "planning_application"
        )
        event.update({"date": "2026-07-10", "datePrecision": "day"})
        planning_evidence["effectiveDate"] = "2026-07-10"
        planning_evidence["data"].update({
            "date": "2026-07-10",
            "datePrecision": "day",
            "status": "FINAL DECISION",
            "decision": "Approve",
        })

        approved, approved_meta = self.build(records)
        approval = next(
            item for item in approved["signals"]
            if item["kind"] == "property_planning"
        )
        self.assertEqual(approval["attributes"]["planningRecordType"], "approval")
        self.assertTrue(approval["fact"].startswith("Planning approval"))
        validate_today_feed(approved, approved_meta, self.schema)

        planning_evidence["data"]["decision"] = "Refused"
        refused, refused_meta = self.build(records)
        application = next(
            item for item in refused["signals"]
            if item["kind"] == "property_planning"
        )
        self.assertEqual(application["attributes"]["planningRecordType"], "application")
        self.assertTrue(application["fact"].startswith("Planning application"))
        validate_today_feed(refused, refused_meta, self.schema)

    def test_property_planning_default_window_is_exactly_45_days(self):
        records, _news = self.fixture()
        property_id = "property:2 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        record = records[property_id]
        event = next(item for item in record["events"] if item["type"] == "planning_application")
        planning_evidence = next(
            item for item in record["evidence"] if item["type"] == "planning_application"
        )

        def set_observed(day):
            event.update({"date": day, "datePrecision": "day"})
            planning_evidence["effectiveDate"] = day
            planning_evidence["data"].update({"date": day, "datePrecision": "day"})

        set_observed("2026-06-11")
        included, included_meta = self.build(records)
        self.assertTrue(any(
            item["kind"] == "property_planning"
            and item["property"]["propertyId"] == property_id
            for item in included["signals"]
        ))
        self.assertEqual(included_meta["criteria"]["planningLookbackDays"], 45)

        set_observed("2026-06-10")
        excluded, _excluded_meta = self.build(records)
        self.assertFalse(any(
            item["kind"] == "property_planning"
            and item["property"]["propertyId"] == property_id
            for item in excluded["signals"]
        ))
        self.assertIn(
            'parser.add_argument("--planning-lookback-days", type=int, default=45)',
            (ROOT / "scripts" / "build_today_feed.py").read_text(encoding="utf-8"),
        )

    def test_canonical_today_has_no_news_input_provenance_or_rows(self):
        records, news = self.fixture()
        feed, metadata = self.build(records)

        parameters = inspect.signature(build_today_feed).parameters
        self.assertNotIn("news_items", parameters)
        self.assertNotIn("news_metadata", parameters)
        self.assertNotIn(
            "--news",
            (ROOT / "scripts" / "build_today_feed.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(set(metadata["sourceFingerprints"]), {"propertyRecords"})
        self.assertEqual(set(metadata["sourceGeneratedAt"]), {"propertyRecords"})
        self.assertIs(metadata["criteria"]["newsRowsExcluded"], True)
        self.assertNotIn("newsMinimumScore", metadata["criteria"])
        for lane in ("signals", "opportunities"):
            for item in feed[lane]:
                self.assertNotEqual(item.get("kind"), "entity_news")
                self.assertNotEqual(item.get("sourceFamily"), "news")
                self.assertNotEqual(item.get("coverage", {}).get("sourceKey"), "news")
                self.assertNotIn(
                    "news",
                    item.get("attributes", {}).get(
                        "corroborationSourceFamilies",
                        [],
                    ),
                )
                self.assertFalse(
                    {
                        str(reference.get(field) or "")
                        for reference in item.get("evidence", [])
                        for field in ("evidenceId", "sourceId")
                    }.intersection({article["id"] for article in news})
                )

    def test_every_signal_property_has_one_standard_or_hot_opportunity(self):
        records, _news = self.fixture()
        property_id = "property:1 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        record = records[property_id]
        planning_evidence_id = "evidence:planning:a"
        record["events"].append({
            "eventId": "event:planning:a",
            "propertyId": property_id,
            "type": "planning_application",
            "date": "2026-07-10",
            "datePrecision": "day",
            "summary": "Single-storey rear extension",
            "source": "Elmbridge Borough Council planning search",
            "evidenceIds": [planning_evidence_id],
        })
        record["evidence"].append(evidence(
            planning_evidence_id,
            "Elmbridge Borough Council planning search",
            "PLAN/2026/A",
            "2026-07-10",
            {
                "propertyId": property_id,
                "type": "planning_application",
                "date": "2026-07-10",
                "datePrecision": "day",
                "reference": "PLAN/2026/A",
                "proposal": "Single-storey rear extension",
                "siteAddress": "1 Example Road Esher KT10 0AA",
                "matchConfidence": 1.0,
                "portalUrl": "https://planning.example/PLAN-2026-A",
            },
        ))

        feed, metadata = self.build(records)
        signals = {item["id"]: item for item in feed["signals"]}
        self.assertEqual(len(feed["opportunities"]), 1)
        opportunity = feed["opportunities"][0]
        self.assertEqual(opportunity["property"]["propertyId"], property_id)
        direct = signals[opportunity["directSignalId"]]
        corroboration = [
            signals[corroboration_id]
            for corroboration_id in opportunity["corroborationIds"]
        ]
        self.assertEqual(
            opportunity["independentSourceCount"],
            len({direct["sourceFamily"], *(item["sourceFamily"] for item in corroboration)}),
        )
        self.assertEqual(opportunity["opportunityLevel"], "Hot")
        self.assertEqual(opportunity["indicatorKindCount"], 2)
        self.assertEqual(
            {direct["id"], *opportunity["corroborationIds"]},
            {
                item["id"] for item in feed["signals"]
                if item["property"]["propertyId"] == property_id
            },
        )
        self.assertTrue(all(
            item["property"]["propertyId"] == property_id
            for item in corroboration
        ))
        self.assertIs(
            metadata["criteria"]["everyQualifyingSignalCreatesPropertyOpportunity"],
            True,
        )
        self.assertEqual(metadata["criteria"]["opportunityGrouping"], "one-per-property")
        self.assertEqual(metadata["criteria"]["hotMinimumIndependentSourceFamilies"], 2)

        validate_today_feed(feed, metadata, self.schema)

    def test_duplicate_planning_reference_prefers_cleaner_canonical_match(self):
        records, _news = self.fixture()
        clean_id = "property:2 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        clean_record = records[clean_id]
        event = next(
            item for item in clean_record["events"]
            if item["type"] == "planning_application"
        )
        planning_evidence = next(
            item for item in clean_record["evidence"]
            if item["type"] == "planning_application"
        )
        event.update({"date": "2026-07-10", "datePrecision": "day"})
        planning_evidence["effectiveDate"] = "2026-07-10"
        planning_evidence["data"].update({
            "date": "2026-07-10",
            "datePrecision": "day",
        })

        malformed_id = "property:2 EXAMPLE ROAD ESHER ESHER KT10 0AA|KT100AA"
        malformed = copy.deepcopy(clean_record)
        malformed["propertyId"] = malformed_id
        malformed["canonicalAddress"] = "2 EXAMPLE ROAD ESHER ESHER KT10 0AA"
        for row in [*malformed["events"], *malformed["evidence"]]:
            row["propertyId"] = malformed_id
            if isinstance(row.get("data"), dict):
                row["data"]["propertyId"] = malformed_id
        records[malformed_id] = malformed

        feed, metadata = self.build(records)
        planning = [
            item for item in feed["signals"]
            if item["kind"] == "property_planning"
        ]
        self.assertEqual(len(planning), 1)
        self.assertEqual(planning[0]["property"]["propertyId"], clean_id)
        self.assertNotIn(
            malformed_id,
            {
                item["property"]["propertyId"]
                for item in feed["opportunities"]
            },
        )
        validate_today_feed(feed, metadata, self.schema)

    def test_epc_signal_requires_a_uniquely_mapped_source_observation(self):
        records, news = self.fixture()
        original_id = "property:1 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        duplicate_id = "property:4 EXAMPLE ROAD ESHER KT10 0AA|KT100AA"
        duplicate_evidence_id = "evidence:epc:duplicate"
        records[duplicate_id] = fixture_record(
            duplicate_id,
            "4 EXAMPLE ROAD ESHER KT10 0AA",
            "2020-01-01",
            events=[{
                "eventId": "event:epc:duplicate",
                "propertyId": duplicate_id,
                "type": "epc_certificate",
                "date": "2026-07-01",
                "datePrecision": "day",
                "summary": "EPC certificate: rating C",
                "source": "MHCLG EPC Register",
                "evidenceIds": [duplicate_evidence_id],
            }],
            evidence_rows=[evidence(
                duplicate_evidence_id,
                "MHCLG EPC Register",
                "epc-a",
                "2026-07-01",
                {
                    "propertyId": duplicate_id,
                    "type": "epc_certificate",
                    "date": "2026-07-01",
                    "rating": "C",
                },
            )],
        )

        feed, metadata = self.build(records)
        epc_property_ids = {
            item["property"]["propertyId"]
            for item in feed["signals"]
            if item["kind"] == "epc_observation"
        }
        self.assertNotIn(original_id, epc_property_ids)
        self.assertNotIn(duplicate_id, epc_property_ids)
        self.assertFalse(any(
            "epc-a" in item["sourceIds"]
            for item in feed["signals"]
            if item["kind"] == "epc_observation"
        ))
        validate_today_feed(feed, metadata, self.schema)

    def test_sale_age_signal_is_a_recent_average_gap_crossing(self):
        as_of = date(2026, 7, 25)
        gap_days = 3652

        def sale_row(property_id, suffix, sold):
            evidence_id = f"evidence:sale:{suffix}"
            return (
                {
                    "eventId": f"event:sale:{suffix}",
                    "propertyId": property_id,
                    "type": "sale",
                    "date": sold.isoformat(),
                    "datePrecision": "day",
                    "summary": "Registered Price Paid sale",
                    "source": "HM Land Registry Price Paid Data",
                    "evidenceIds": [evidence_id],
                },
                evidence(
                    evidence_id,
                    "HM Land Registry Price Paid Data",
                    f"sale-{suffix}",
                    sold.isoformat(),
                    {
                        "propertyId": property_id,
                        "type": "sale",
                        "date": sold.isoformat(),
                    },
                ),
            )

        training_id = "property:TRAINING|KT100AA"
        training_start = date(2000, 1, 1)
        training_end = training_start + timedelta(days=gap_days)
        training_rows = [
            sale_row(training_id, "training-a", training_start),
            sale_row(training_id, "training-b", training_end),
        ]
        training = fixture_record(
            training_id,
            "1 TRAINING ROAD ESHER KT10 0AA",
            training_end.isoformat(),
            events=[row[0] for row in training_rows],
            evidence_rows=[row[1] for row in training_rows],
        )

        targets = {}
        for name, days_since_crossing in (
            ("recent", 5),
            ("expired", 30),
            ("future", -1),
        ):
            property_id = f"property:{name.upper()}|KT100AA"
            latest_sale = as_of - timedelta(days=gap_days + days_since_crossing)
            event_row, evidence_row = sale_row(property_id, name, latest_sale)
            record = fixture_record(
                property_id,
                f"{name.upper()} HOUSE ESHER KT10 0AA",
                latest_sale.isoformat(),
                events=[event_row],
                evidence_rows=[evidence_row],
            )
            record["profile"]["market"] = f"sparse-{name}"
            targets[name] = record

        records = {training_id: training, **{
            record["propertyId"]: record for record in targets.values()
        }}

        def build_for(snapshot_date):
            return build_today_feed(
                records,
                {
                    "datasetFingerprint": "b" * 64,
                    "generatedAt": "2026-07-25T08:00:00Z",
                },
                as_of=snapshot_date,
                generated_at="2026-07-25T08:00:00Z",
            )

        feed, metadata = build_for(as_of.isoformat())
        crossings = [
            item for item in feed["signals"]
            if item["kind"] == "sale_age_milestone"
        ]
        self.assertEqual(
            [item["property"]["propertyId"] for item in crossings],
            [targets["recent"]["propertyId"]],
        )
        crossing = crossings[0]
        self.assertEqual(crossing["effectiveDate"], (as_of - timedelta(days=5)).isoformat())
        self.assertEqual(crossing["attributes"]["daysSinceCrossing"], 5)
        self.assertEqual(crossing["attributes"]["crossingWindowDays"], 30)
        self.assertEqual(crossing["attributes"]["holdingIntervalCohortBasis"], "overall")
        self.assertEqual(crossing["attributes"]["holdingIntervalSampleSize"], 1)
        self.assertEqual(metadata["criteria"]["saleAgeCrossingWindowDays"], 30)

        next_feed, _next_metadata = build_for((as_of + timedelta(days=1)).isoformat())
        next_crossing = next(
            item for item in next_feed["signals"]
            if item["kind"] == "sale_age_milestone"
            and item["property"]["propertyId"] == targets["recent"]["propertyId"]
        )
        self.assertEqual(next_crossing["id"], crossing["id"])
        self.assertEqual(next_crossing["effectiveDate"], crossing["effectiveDate"])

        expired_feed, _expired_metadata = build_for((as_of + timedelta(days=25)).isoformat())
        self.assertNotIn(
            targets["recent"]["propertyId"],
            {
                item["property"]["propertyId"]
                for item in expired_feed["signals"]
                if item["kind"] == "sale_age_milestone"
            },
        )

    def test_published_today_asset_is_non_empty_and_valid(self):
        feed, metadata = read_today_feed(ROOT / "outputs" / "today-feed.js")
        validate_today_feed(feed, metadata, self.schema)
        self.assertTrue(feed["signals"])
        self.assertTrue(feed["opportunities"])
        self.assertEqual(
            {
                item["property"]["propertyId"]
                for item in feed["signals"]
            },
            {
                item["property"]["propertyId"]
                for item in feed["opportunities"]
            },
        )
        self.assertIs(metadata["criteria"]["newsRowsExcluded"], True)
        self.assertNotIn("newsMinimumScore", metadata["criteria"])
        for lane in ("signals", "opportunities"):
            self.assertFalse(
                any(
                    item.get("kind") == "entity_news"
                    or item.get("sourceFamily") == "news"
                    for item in feed[lane]
                )
            )
        for signal in feed["signals"]:
            floor_area_sqm = signal.get("attributes", {}).get("floorAreaSqm")
            if isinstance(floor_area_sqm, float):
                self.assertFalse(floor_area_sqm.is_integer())


if __name__ == "__main__":
    unittest.main()
