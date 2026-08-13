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
import enrich_weekly_context as weekly_context  # noqa: E402


def property_args(**overrides):
    values = {
        "limit": 0,
        "missing_only": True,
        "geocode_refresh_days": 365,
        "timeout": 1,
        "retries": 0,
        "max_source_errors": 20,
        "disable_osm": True,
        "pause": 0,
        "progress_every": 100,
        "osm_refresh_days": 120,
        "osm_radius_m": 1800,
        "overpass_timeout": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DynamicContextTruthfulnessTests(unittest.TestCase):
    def test_property_context_alignment_removes_retired_live_flood_payload(self):
        item = {
            "id": "lr-test",
            "postcode": "KT10 0AA",
            "latitude": 51.35,
            "longitude": -0.36,
            "geocode": {"source": "Postcodes.io"},
            "environmentAgency": {"floodStatus": "legacy live status"},
        }

        enriched, _stats = property_context.enrich_transactions(
            [item], {}, property_args()
        )

        self.assertNotIn("environmentAgency", enriched[0])

    def test_daily_workflow_keeps_today_without_live_flood_producer(self):
        workflow = (ROOT / ".github" / "workflows" / "daily-intelligence.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 scripts/build_today_feed.py", workflow)
        self.assertNotIn("enrich_property_context.py", workflow)
        self.assertNotIn("force-flood-refresh", workflow)

    def test_monthly_missing_only_retains_complete_weekly_context(self):
        item = {
            "id": "lr-weekly",
            "latitude": 51.35,
            "longitude": -0.36,
            "planningConstraints": {
                "lookupStatus": "successful",
                "constraintCount": 0,
            },
            "ofsted": {
                "source": "DfE / Ofsted school data",
                "nearestSchools": [{"name": "Existing School"}],
            },
        }
        args = SimpleNamespace(
            disable_schools=False,
            missing_only=True,
            limit=0,
            max_source_errors=25,
            disable_planning_constraints=False,
            pause=0.15,
            progress_every=100,
        )
        with (
            patch.object(weekly_context, "school_rows", return_value=[{"name": "School"}]),
            patch.object(weekly_context, "ensure_coordinates") as coordinates,
            patch.object(weekly_context, "constraints_for_item") as constraints,
            patch.object(weekly_context, "schools_for_item") as schools,
        ):
            enriched, _stats, _loaded = weekly_context.enrich_transactions(
                [item], {}, args
            )
        coordinates.assert_not_called()
        constraints.assert_not_called()
        schools.assert_not_called()
        self.assertEqual(enriched[0], item)

    def test_missing_only_metadata_preserves_full_refresh_and_discloses_gap_fill(self):
        retained = {
            "planningConstraints": {
                "lookupStatus": "successful",
                "constraintCount": 0,
            },
            "ofsted": {
                "source": "DfE / Ofsted school data",
                "nearestSchools": [{"name": "Retained School"}],
            },
        }
        aligned = {
            "planningConstraints": {
                "lookupStatus": "successful",
                "constraintCount": 1,
            },
            "ofsted": {
                "source": "DfE / Ofsted school data",
                "nearestSchools": [{"name": "Aligned School"}],
            },
        }
        metadata = weekly_context.weekly_context_metadata(
            {"updatedAt": "2026-07-27T07:15:00Z"},
            [retained, aligned],
            {
                "planningConstraintResponses": 1,
                "planningConstraintSourceResponses": 1,
                "schools": 1,
                "schoolRemoteSourceLoads": 1,
            },
            missing_only=True,
            schools_loaded=True,
            aligned_at="2026-08-02T15:26:36Z",
        )

        self.assertEqual(metadata["updatedAt"], "2026-07-27T07:15:00Z")
        self.assertEqual(metadata["fullRefreshAt"], "2026-07-27T07:15:00Z")
        self.assertEqual(metadata["alignedAt"], "2026-08-02T15:26:36Z")
        self.assertEqual(metadata["alignmentMode"], "missing-only-gap-fill")
        planning = metadata["planningConstraints"]
        self.assertTrue(planning["sourceLoadedThisRun"])
        self.assertTrue(planning["sourceRefreshedThisRun"])
        self.assertFalse(planning["cacheLoadedThisRun"])
        self.assertEqual(planning["recordsAlignedThisRun"], 1)
        self.assertEqual(planning["recordsRetained"], 1)
        schools = metadata["schools"]
        self.assertTrue(schools["sourceLoadedThisRun"])
        self.assertTrue(schools["sourceRefreshedThisRun"])
        self.assertFalse(schools["cacheLoadedThisRun"])
        self.assertEqual(schools["recordsAlignedThisRun"], 1)
        self.assertEqual(schools["recordsRetained"], 1)

    def test_missing_only_metadata_does_not_claim_retained_sources_loaded(self):
        retained = {
            "planningConstraints": {
                "lookupStatus": "successful",
                "constraintCount": 0,
            },
            "ofsted": {
                "source": "DfE / Ofsted school data",
                "nearestSchools": [{"name": "Retained School"}],
            },
        }
        metadata = weekly_context.weekly_context_metadata(
            {
                "updatedAt": "2026-07-27T07:15:00Z",
                "fullRefreshAt": "2026-07-27T07:15:00Z",
            },
            [retained],
            {},
            missing_only=True,
            schools_loaded=False,
            aligned_at="2026-08-02T15:26:36Z",
        )

        self.assertEqual(metadata["updatedAt"], "2026-07-27T07:15:00Z")
        self.assertFalse(
            metadata["planningConstraints"]["sourceLoadedThisRun"]
        )
        self.assertFalse(
            metadata["planningConstraints"]["sourceRefreshedThisRun"]
        )
        self.assertEqual(
            metadata["planningConstraints"]["recordsRetained"], 1
        )
        self.assertTrue(metadata["schools"]["loaded"])
        self.assertFalse(metadata["schools"]["sourceLoadedThisRun"])
        self.assertFalse(metadata["schools"]["sourceRefreshedThisRun"])
        self.assertEqual(metadata["schools"]["recordsRetained"], 1)

    def test_full_weekly_refresh_advances_the_full_refresh_timestamp(self):
        metadata = weekly_context.weekly_context_metadata(
            {"updatedAt": "2026-07-27T07:15:00Z"},
            [],
            {},
            missing_only=False,
            schools_loaded=False,
            aligned_at="2026-08-03T07:15:00Z",
        )

        self.assertEqual(metadata["alignmentMode"], "full-refresh")
        self.assertEqual(metadata["alignedAt"], "2026-08-03T07:15:00Z")
        self.assertEqual(metadata["updatedAt"], "2026-08-03T07:15:00Z")
        self.assertEqual(metadata["fullRefreshAt"], "2026-08-03T07:15:00Z")

    def test_planning_constraint_provenance_distinguishes_cache_and_source(self):
        item = {"id": "lr-weekly", "postcode": "KT10 0AA"}
        args = SimpleNamespace(refresh_days=6, timeout=1, retries=0)
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        cache = {
            "planningConstraints": {
                "KT100AA": {
                    "status": "no_match",
                    "updatedAt": observed_at,
                    "data": {},
                }
            }
        }
        cache_state = {}
        with patch.object(weekly_context, "request_json") as request:
            weekly_context.constraints_for_item(
                item,
                51.35,
                -0.36,
                cache,
                args,
                run_state=cache_state,
            )

        request.assert_not_called()
        self.assertEqual(cache_state, {"cacheLoaded": True})

        source_state = {}
        with patch.object(weekly_context, "request_json", return_value={"entities": []}):
            weekly_context.constraints_for_item(
                item,
                51.35,
                -0.36,
                {},
                args,
                run_state=source_state,
            )

        self.assertEqual(source_state, {"sourceLoaded": True})

    def test_completeness_no_longer_gates_retired_nearby_planning(self):
        rows = completeness.coverage_rows(
            [{"planning": {"coverageStatus": "unknown"}}],
            now=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
        )

        self.assertNotIn("Planning query responses", {row["name"] for row in rows})


if __name__ == "__main__":
    unittest.main()
