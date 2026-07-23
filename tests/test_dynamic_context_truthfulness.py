import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_data_completeness as completeness  # noqa: E402
import enrich_daily_intelligence as daily  # noqa: E402
import enrich_property_context as property_context  # noqa: E402


def property_args(**overrides):
    values = {
        "limit": 0,
        "missing_only": True,
        "geocode_refresh_days": 365,
        "timeout": 1,
        "retries": 0,
        "max_source_errors": 20,
        "disable_environment_agency": False,
        "disable_osm": True,
        "pause": 0,
        "progress_every": 100,
        "flood_radius_km": 5,
        "flood_max_age_hours": 30,
        "flood_query_mode": "point",
        "force_flood_refresh": False,
        "osm_refresh_days": 120,
        "osm_radius_m": 1800,
        "overpass_timeout": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def daily_args(**overrides):
    values = {
        "limit": 0,
        "missing_only": False,
        "planning_days": 45,
        "planning_radius_m": 1200,
        "planning_limit": 50,
        "planning_max_pages": 20,
        "max_applications_per_property": 6,
        "refresh_hours": 20,
        "company_refresh_hours": 20,
        "geocode_refresh_days": 365,
        "timeout": 1,
        "retries": 0,
        "max_source_errors": 25,
        "disable_planning": False,
        "disable_companies_house": True,
        "pause": 0,
        "progress_every": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FloodTruthfulnessTests(unittest.TestCase):
    def setUp(self):
        self.prior = {
            "floodStatus": "1 active alert within 5km",
            "currentFloodAlertCount": 1,
            "highestCurrentSeverity": "Flood warning",
            "nearestFloodAlert": "Old warning",
            "searchRadius": "5km",
            "source": "Environment Agency Real Time flood-monitoring API",
            "updatedAt": "2026-07-17T05:00:00Z",
        }
        self.item = {
            "id": "lr-test",
            "postcode": "KT10 0AA",
            "latitude": 51.35,
            "longitude": -0.36,
            "geocode": {"source": "Postcodes.io"},
            "environmentAgency": self.prior,
            "openStreetMap": {"source": "OpenStreetMap via Overpass API"},
        }

    def test_missing_only_refreshes_existing_flood_observation(self):
        replacement = {
            "environmentAgency": {
                "floodStatus": "No current flood alert within 5km",
                "currentFloodAlertCount": 0,
                "highestCurrentSeverity": "None",
                "nearestFloodAlert": "",
                "searchRadius": "5km",
                "source": "Environment Agency Real Time flood-monitoring API",
                "observedAt": "2026-07-19T05:00:00Z",
                "updatedAt": "2026-07-19T05:00:00Z",
            }
        }
        with patch.object(property_context, "flood_context", return_value=replacement) as lookup:
            enriched, stats = property_context.enrich_transactions([self.item], {}, property_args())

        lookup.assert_called_once()
        self.assertEqual(enriched[0]["environmentAgency"], replacement["environmentAgency"])
        self.assertEqual(stats["environmentAgencyRequests"], 1)
        self.assertEqual(stats["environmentAgency"], 1)

    def test_missing_only_reuses_a_fresh_observation_within_the_ttl(self):
        item = dict(self.item)
        item["environmentAgency"] = {
            **self.prior,
            "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        with patch.object(property_context, "flood_context") as lookup:
            enriched, stats = property_context.enrich_transactions([item], {}, property_args())

        lookup.assert_not_called()
        self.assertEqual(enriched[0]["environmentAgency"], item["environmentAgency"])
        self.assertEqual(stats["environmentAgencyFreshRetained"], 1)

    def test_forced_refresh_replaces_an_observation_that_is_still_fresh(self):
        item = dict(self.item)
        item["environmentAgency"] = {
            **self.prior,
            "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        replacement = {
            "environmentAgency": {
                **item["environmentAgency"],
                "observedAt": "2026-07-23T20:00:00Z",
                "updatedAt": "2026-07-23T20:00:00Z",
            }
        }
        with patch.object(property_context, "flood_context", return_value=replacement) as lookup:
            enriched, stats = property_context.enrich_transactions(
                [item],
                {},
                property_args(force_flood_refresh=True),
            )

        lookup.assert_called_once()
        self.assertEqual(enriched[0]["environmentAgency"], replacement["environmentAgency"])
        self.assertEqual(stats["environmentAgency"], 1)
        self.assertEqual(stats["environmentAgencyFreshRetained"], 0)

    def test_daily_workflow_forces_flood_refresh_before_validation(self):
        workflow = (ROOT / ".github" / "workflows" / "daily-intelligence.yml").read_text(
            encoding="utf-8"
        )

        refresh = workflow.index("--force-flood-refresh")
        validation = workflow.index("python3 scripts/check_data_completeness.py")
        self.assertLess(refresh, validation)

    def test_request_failure_retains_the_prior_dated_observation(self):
        with patch.object(property_context, "flood_context", side_effect=RuntimeError("offline")):
            enriched, stats = property_context.enrich_transactions([self.item], {}, property_args())

        self.assertEqual(enriched[0]["environmentAgency"], self.prior)
        self.assertEqual(stats["environmentAgencyErrors"], 1)
        self.assertEqual(stats["environmentAgencyRetainedAfterError"], 1)

    def test_request_failure_without_prior_observation_does_not_invent_one(self):
        item = dict(self.item)
        item.pop("environmentAgency")
        with patch.object(property_context, "flood_context", side_effect=RuntimeError("offline")):
            enriched, _stats = property_context.enrich_transactions([item], {}, property_args())

        self.assertNotIn("environmentAgency", enriched[0])

    def test_malformed_flood_response_is_not_treated_as_no_alerts(self):
        with patch.object(property_context, "request_json", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "did not prove"):
                property_context.flood_context(51.35, -0.36, property_args())

    def test_bulk_polygon_snapshot_is_evaluated_locally(self):
        snapshot = {
            "observedAt": "2026-07-19T12:00:00Z",
            "alerts": [{
                "alert": {
                    "severityLevel": 2,
                    "severity": "Flood warning",
                    "description": "Test flood area",
                },
                "rings": [[
                    (-0.37, 51.34),
                    (-0.35, 51.34),
                    (-0.35, 51.36),
                    (-0.37, 51.36),
                    (-0.37, 51.34),
                ]],
            }],
        }
        near = property_context.flood_context_from_snapshot(51.35, -0.36, property_args(), snapshot)
        far = property_context.flood_context_from_snapshot(51.15, -0.70, property_args(), snapshot)

        self.assertEqual(near["environmentAgency"]["currentFloodAlertCount"], 1)
        self.assertEqual(near["environmentAgency"]["nearestFloodAlert"], "Test flood area")
        self.assertEqual(far["environmentAgency"]["currentFloodAlertCount"], 0)
        self.assertIn("bulk alert polygons", far["environmentAgency"]["source"])

    def test_flood_polygon_holes_are_not_treated_as_alert_area(self):
        geometry = {
            "features": [{
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-0.38, 51.34], [-0.34, 51.34], [-0.34, 51.38], [-0.38, 51.38], [-0.38, 51.34]],
                        [[-0.365, 51.355], [-0.355, 51.355], [-0.355, 51.365], [-0.365, 51.365], [-0.365, 51.355]],
                    ],
                }
            }]
        }
        polygons = property_context._geometry_polygons(geometry)

        self.assertEqual(len(polygons), 1)
        self.assertEqual(len(polygons[0]["holes"]), 1)
        self.assertEqual(property_context._distance_to_polygons_km(-0.37, 51.35, polygons, 0.2), 0.0)
        self.assertGreater(property_context._distance_to_polygons_km(-0.36, 51.36, polygons, 0.2), 0.2)

    def test_bulk_mode_loads_one_snapshot_for_all_property_evaluations(self):
        snapshot = {"observedAt": "2026-07-19T12:00:00Z", "alerts": []}
        second = {
            **self.item,
            "id": "lr-second",
            "postcode": "KT11 1AA",
            "latitude": 51.33,
            "longitude": -0.41,
        }
        with patch.object(property_context, "active_flood_snapshot", return_value=snapshot) as loader:
            enriched, stats = property_context.enrich_transactions(
                [self.item, second],
                {},
                property_args(flood_query_mode="bulk"),
            )

        loader.assert_called_once()
        self.assertEqual(stats["environmentAgencyRequests"], 1)
        self.assertEqual(stats["environmentAgencyEvaluations"], 2)
        self.assertTrue(all(item["environmentAgency"]["currentFloodAlertCount"] == 0 for item in enriched))

    def test_freshness_counts_expose_fresh_stale_and_missing_rows(self):
        now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
        rows = [
            {"environmentAgency": {"observedAt": "2026-07-19T05:00:00Z"}},
            {"environmentAgency": {"updatedAt": "2026-07-17T05:00:00Z"}},
            {},
        ]
        counts = property_context.flood_freshness_counts(rows, 30, now=now)

        self.assertEqual(counts, {"fresh": 1, "stale": 1, "missing": 1})

    def test_completeness_gate_does_not_count_a_stale_flood_observation(self):
        now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
        rows = completeness.coverage_rows(
            [{"environmentAgency": {"updatedAt": "2026-07-17T05:00:00Z"}}],
            now=now,
        )
        flood_row = next(row for row in rows if row["name"] == "Fresh flood status")

        self.assertEqual(flood_row["found"], 0)
        self.assertEqual(flood_row["coverage"], 0.0)


class PlanningTruthfulnessTests(unittest.TestCase):
    def test_empty_spatial_result_records_unknown_coverage_without_negative_claim(self):
        cache = {}
        with patch.object(daily, "request_json", return_value={"entities": []}), patch.object(
            daily, "utc_now", return_value="2026-07-19T05:00:00Z"
        ):
            result = daily.recent_planning_for_item(
                {"id": "lr-test", "postcode": "KT10 0AA"},
                51.35,
                -0.36,
                cache,
                daily_args(),
                date(2026, 6, 4),
            )

        context = result["planning"]
        self.assertEqual(context["coverageStatus"], "unknown")
        self.assertEqual(context["coverageReason"], "no-proven-within-radius-result")
        self.assertEqual(context["queryResultCount"], 0)
        self.assertNotIn("latestApplication", context)
        self.assertNotIn("recentApplicationCount", context)
        self.assertEqual(context["recentApplications"], [])
        self.assertEqual(next(iter(cache["planningApplications"].values()))["status"], "unknown")

    def test_positive_spatial_result_is_recorded_as_observed_not_complete_coverage(self):
        payload = {
            "entities": [{
                "name": "Replacement dwelling",
                "reference": "2026/1234",
                "start-date": "2026-07-01",
                "point": "POINT(-0.36 51.35)",
            }]
        }
        with patch.object(daily, "request_json", return_value=payload), patch.object(
            daily, "utc_now", return_value="2026-07-19T05:00:00Z"
        ):
            result = daily.recent_planning_for_item(
                {"id": "lr-test", "postcode": "KT10 0AA"},
                51.35,
                -0.36,
                {},
                daily_args(),
                date(2026, 6, 4),
            )

        context = result["planning"]
        self.assertEqual(context["coverageStatus"], "observed")
        self.assertEqual(context["coverageMode"], "positive-results-only")
        self.assertEqual(context["recentApplicationCount"], 1)
        self.assertIn("Replacement dwelling", context["latestApplication"])

    def test_square_prefilter_does_not_publish_an_entity_outside_the_stated_radius(self):
        payload = {
            "entities": [{
                "name": "Outside the circular radius",
                "reference": "2026/OUTSIDE",
                "start-date": "2026-07-01",
                "point": "POINT(-0.336 51.35)",
            }]
        }
        with patch.object(daily, "request_json", return_value=payload), patch.object(
            daily, "utc_now", return_value="2026-07-19T05:00:00Z"
        ):
            result = daily.recent_planning_for_item(
                {"id": "lr-test", "postcode": "KT10 0AA"},
                51.35,
                -0.36,
                {},
                daily_args(),
                date(2026, 6, 4),
            )

        context = result["planning"]
        self.assertEqual(context["coverageStatus"], "unknown")
        self.assertEqual(context["sourceResultCount"], 1)
        self.assertEqual(context["outsideRadiusResultCount"], 1)
        self.assertEqual(context["queryResultCount"], 0)
        self.assertNotIn("latestApplication", context)

    def test_planning_query_follows_every_declared_page_before_selecting_latest(self):
        pages = [
            {
                "entities": [{
                    "name": "Older application",
                    "reference": "2026/OLD",
                    "start-date": "2026-06-10",
                    "point": "POINT(-0.36 51.35)",
                }],
                "links": {"next": "/entity.json?offset=1"},
            },
            {
                "entities": [{
                    "name": "Newer application",
                    "reference": "2026/NEW",
                    "start-date": "2026-07-10",
                    "point": "POINT(-0.36 51.35)",
                }],
                "links": {"next": ""},
            },
        ]
        with patch.object(daily, "request_json", side_effect=pages) as requester, patch.object(
            daily, "utc_now", return_value="2026-07-19T05:00:00Z"
        ):
            result = daily.recent_planning_for_item(
                {"id": "lr-test", "postcode": "KT10 0AA"},
                51.35,
                -0.36,
                {},
                daily_args(planning_limit=1, planning_max_pages=5),
                date(2026, 6, 4),
            )

        context = result["planning"]
        self.assertEqual(requester.call_count, 2)
        self.assertEqual([call.kwargs["params"]["offset"] for call in requester.call_args_list], [0, 1])
        self.assertEqual(context["queryPages"], 2)
        self.assertEqual(context["recentApplicationCount"], 2)
        self.assertIn("Newer application", context["latestApplication"])

    def test_legacy_empty_negative_claim_is_normalised_fail_closed(self):
        legacy = {
            "source": "Planning Data API",
            "updatedAt": "2026-07-18T05:00:00Z",
            "recentApplicationCount": 0,
            "latestApplication": "No recent applications within 1.2km",
            "recentApplications": [],
        }
        normalised = daily.normalise_existing_planning(legacy)

        self.assertEqual(normalised["coverageStatus"], "unknown")
        self.assertNotIn("latestApplication", normalised)
        self.assertNotIn("recentApplicationCount", normalised)
        self.assertTrue(daily.planning_context_is_truthful(normalised))

    def test_request_failure_can_be_recorded_as_unavailable_without_a_negative_claim(self):
        with patch.object(daily, "utc_now", return_value="2026-07-19T05:00:00Z"):
            context = daily.unavailable_planning_context(
                daily_args(),
                date(2026, 6, 4),
                "request-failed",
            )

        self.assertEqual(context["coverageStatus"], "unavailable")
        self.assertNotIn("latestApplication", context)
        self.assertNotIn("recentApplicationCount", context)
        self.assertTrue(daily.planning_context_is_truthful(context))

    def test_enricher_marks_a_failed_planning_request_unavailable(self):
        item = {
            "id": "lr-test",
            "postcode": "KT10 0AA",
            "latitude": 51.35,
            "longitude": -0.36,
        }
        with patch.object(daily, "recent_planning_for_item", side_effect=RuntimeError("offline")), patch.object(
            daily, "utc_now", return_value="2026-07-19T05:00:00Z"
        ):
            enriched, stats = daily.enrich_transactions([item], {}, daily_args())

        self.assertEqual(enriched[0]["planning"]["coverageStatus"], "unavailable")
        self.assertEqual(enriched[0]["planning"]["coverageReason"], "request-failed")
        self.assertEqual(stats["planningErrors"], 1)

    def test_completeness_counts_current_unknown_as_attempt_but_rejects_legacy_claim(self):
        now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
        current_unknown = {
            "coverageStatus": "unknown",
            "coverageMode": "no-authoritative-negative-coverage",
            "updatedAt": "2026-07-19T05:00:00Z",
            "queryResultCount": 0,
            "recentApplications": [],
        }
        legacy = {
            "updatedAt": "2026-07-19T05:00:00Z",
            "recentApplicationCount": 0,
            "latestApplication": "No recent applications within 1.2km",
            "recentApplications": [],
        }
        rows = completeness.coverage_rows(
            [{"planning": current_unknown}, {"planning": legacy}],
            now=now,
        )
        planning_row = next(row for row in rows if row["name"] == "Planning query responses")

        self.assertEqual(planning_row["found"], 1)
        self.assertEqual(planning_row["coverage"], 50.0)

    def test_truthfulness_gate_rejects_an_unknown_record_with_a_negative_claim(self):
        now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
        invalid = {
            "coverageStatus": "unknown",
            "updatedAt": "2026-07-19T05:00:00Z",
            "recentApplicationCount": 0,
            "latestApplication": "No recent applications within 1.2km",
            "recentApplications": [],
        }
        meta = {
            "propertyContext": {
                "environmentAgency": {
                    "freshRecords": 0,
                    "staleRecords": 0,
                    "missingRecords": 1,
                    "maximumAgeHours": 30,
                }
            },
            "dailyIntelligence": {
                "planning": {
                    "records": 0,
                    "observedRecords": 0,
                    "unknownRecords": 0,
                    "unavailableRecords": 0,
                    "successfulResponses": 0,
                    "coverageMode": "positive-observations-only",
                }
            },
        }

        failures = completeness.dynamic_context_failures([{"planning": invalid}], meta, now=now)

        self.assertTrue(any(item.startswith("Planning truthfulness:") for item in failures))


if __name__ == "__main__":
    unittest.main()
