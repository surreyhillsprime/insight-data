import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_data_completeness as completeness  # noqa: E402
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

    def test_completeness_no_longer_gates_retired_nearby_planning(self):
        rows = completeness.coverage_rows(
            [{"planning": {"coverageStatus": "unknown"}}],
            now=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
        )

        self.assertNotIn("Planning query responses", {row["name"] for row in rows})


if __name__ == "__main__":
    unittest.main()
