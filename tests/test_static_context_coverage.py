import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_data_completeness import (  # noqa: E402
    MINIMUM_COVERAGE,
    coverage_rows,
    coverage_threshold_failures,
    static_context_failures,
)
from enrich_weekly_context import constraints_for_item  # noqa: E402
from insight_data_utils import (  # noqa: E402
    planning_constraint_coverage_counts,
    recompute_coverage_metadata,
)


def args(**overrides):
    values = {
        "refresh_days": 6,
        "timeout": 1,
        "retries": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def transaction(**overrides):
    item = {
        "id": "lr-test",
        "postcode": "KT10 0AA",
    }
    item.update(overrides)
    return item


def successful_constraints(count=0, **overrides):
    context = {
        "source": "Planning Data API",
        "updatedAt": "2026-07-22T10:00:00Z",
        "lookupStatus": "successful",
        "constraintCount": count,
    }
    context.update(overrides)
    return {"planningConstraints": context}


class StaticContextCoverageTests(unittest.TestCase):
    def test_property_context_release_gates_require_near_complete_lookup_coverage(self):
        for name in (
            "Coordinates",
            "Fresh flood status",
            "School lookups",
            "Planning constraint lookups",
            "Planning query responses",
        ):
            self.assertEqual(MINIMUM_COVERAGE[name], 99.0)

    def test_fresh_legacy_no_match_cache_becomes_an_explicit_successful_lookup(self):
        cache = {
            "planningConstraints": {
                "KT100AA": {
                    "status": "no_match",
                    "updatedAt": "2099-01-01T00:00:00Z",
                    "data": {},
                }
            }
        }
        with patch("enrich_weekly_context.request_json") as request:
            result = constraints_for_item(transaction(), 51.3, -0.4, cache, args())

        self.assertFalse(request.called)
        self.assertEqual(result["planningConstraints"]["lookupStatus"], "successful")
        self.assertEqual(result["planningConstraints"]["constraintCount"], 0)
        self.assertEqual(
            cache["planningConstraints"]["KT100AA"]["data"],
            result,
        )

    def test_fresh_legacy_match_preserves_the_positive_constraint_count(self):
        cache = {
            "planningConstraints": {
                "KT100AA": {
                    "status": "matched",
                    "updatedAt": "2099-01-01T00:00:00Z",
                    "data": {
                        "planningConstraints": {
                            "source": "Planning Data API",
                            "updatedAt": "2099-01-01T00:00:00Z",
                            "constraintCount": 2,
                            "greenBelt": "Green belt: Elmbridge",
                        }
                    },
                }
            }
        }
        result = constraints_for_item(transaction(), 51.3, -0.4, cache, args())

        self.assertEqual(result["planningConstraints"]["lookupStatus"], "successful")
        self.assertEqual(result["planningConstraints"]["constraintCount"], 2)
        self.assertEqual(
            result["planningConstraints"]["greenBelt"],
            "Green belt: Elmbridge",
        )

    def test_empty_live_response_is_stored_as_successful_no_match_evidence(self):
        cache = {}
        with patch("enrich_weekly_context.request_json", return_value={"entities": []}):
            result = constraints_for_item(transaction(), 51.3, -0.4, cache, args())

        self.assertEqual(result["planningConstraints"]["lookupStatus"], "successful")
        self.assertEqual(result["planningConstraints"]["constraintCount"], 0)
        self.assertEqual(
            cache["planningConstraints"]["KT100AA"]["status"],
            "no_match",
        )

    def test_row_and_metadata_counts_keep_success_and_positive_results_separate(self):
        items = [
            successful_constraints(0),
            successful_constraints(2, floodRiskZone="Flood risk zone: 3"),
            {"planningConstraints": {"constraintCount": 1, "greenBelt": "Green belt"}},
            {},
        ]
        expected = {
            "successfulResponses": 2,
            "positiveRecords": 2,
            "missingResponses": 2,
        }
        self.assertEqual(planning_constraint_coverage_counts(items), expected)

        metadata = recompute_coverage_metadata(
            items,
            {"weeklyContext": {"planningConstraints": {"source": "Planning Data API"}}},
        )
        self.assertEqual(
            metadata["weeklyContext"]["planningConstraints"],
            {
                "source": "Planning Data API",
                "records": 2,
                **expected,
                "coverageMode": "explicit-per-row-success",
            },
        )
        self.assertEqual(static_context_failures(items, metadata), [])

    def test_static_lookup_threshold_is_strict_only_and_never_blocks_base_only(self):
        items = [successful_constraints() for _ in range(98)] + [{}, {}]
        row = next(
            item for item in coverage_rows(items)
            if item["name"] == "Planning constraint lookups"
        )
        self.assertEqual(row["coverage"], 98.0)
        self.assertEqual(
            coverage_threshold_failures([row], strict_metadata=False),
            [],
        )
        self.assertEqual(
            coverage_threshold_failures(
                [row], strict_metadata=True, base_only=True
            ),
            [],
        )
        failures = coverage_threshold_failures([row], strict_metadata=True)
        self.assertEqual(len(failures), 1)
        self.assertIn("98.0% is below 99.0%", failures[0])

    def test_strict_metadata_reconciliation_rejects_stale_positive_totals(self):
        items = [successful_constraints(0), successful_constraints(1)]
        metadata = {
            "weeklyContext": {
                "planningConstraints": {
                    "records": 2,
                    "successfulResponses": 2,
                    "positiveRecords": 0,
                    "missingResponses": 0,
                    "coverageMode": "explicit-per-row-success",
                }
            }
        }
        failures = static_context_failures(items, metadata)
        self.assertEqual(len(failures), 1)
        self.assertIn("positiveRecords reports 0, expected 1", failures[0])


if __name__ == "__main__":
    unittest.main()
