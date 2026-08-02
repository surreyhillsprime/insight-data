import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import enrich_listed_buildings as heritage  # noqa: E402
import check_data_completeness as completeness  # noqa: E402
from build_heritage_address_ledger import sha256_lines  # noqa: E402
from enrich_weekly_context import (  # noqa: E402
    CONSTRAINT_DATASETS,
    constraints_for_item,
)
from insight_data_utils import property_record_id, read_js, write_js  # noqa: E402


def args(**overrides):
    values = {
        "candidate_radius_metres": 250,
        "name_radius_metres": 2000,
        "page_size": 2,
        "max_pages": 10,
        "timeout": 1,
        "retries": 0,
        "minimum_source_records": 1,
        "maximum_source_records": 20,
        "bbox": heritage.DEFAULT_BBOX,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def transaction(**overrides):
    item = {
        "id": "lr-test",
        "address": "BIRCH HOUSE, 1 TEST ROAD, ESHER, KT10 0AA",
        "paon": "BIRCH HOUSE",
        "saon": "",
        "street": "TEST ROAD",
        "locality": "",
        "town": "ESHER",
        "district": "Elmbridge",
        "postcode": "KT10 0AA",
        "market": "elmbridge-prime",
        "price": 2_500_000,
        "date": "2026-01-01",
        "latitude": 51.35,
        "longitude": -0.36,
        "coordinatePrecision": "postcode-centroid",
        "geocode": {"precision": "Postcode centroid"},
    }
    item.update(overrides)
    item["propertyRecordId"] = property_record_id(item)
    return item


def point(number="1234567", **overrides):
    item = {
        "listEntryNumber": number,
        "grade": "II",
        "name": "BIRCH HOUSE",
        "url": (
            "https://historicengland.org.uk/listing/the-list/list-entry/" + number
        ),
        "longitude": -0.36,
        "latitude": 51.35,
        "locations": [(-0.36, 51.35)],
        "listDate": "1980-01-01",
    }
    item.update(overrides)
    return item


def polygon(number="1234567"):
    return {
        "listEntryNumber": number,
        "grade": "II",
        "rings": [[
            (-0.361, 51.349),
            (-0.359, 51.349),
            (-0.359, 51.351),
            (-0.361, 51.351),
            (-0.361, 51.349),
        ]],
        "bbox": (-0.361, 51.349, -0.359, 51.351),
        "areaHectares": 0.5,
    }


def source(points=None, polygons=None):
    return {
        "points": points if points is not None else [point()],
        "polygons": polygons if polygons is not None else [polygon()],
        "sourceUpdatedAt": "2026-07-25T23:00:00Z",
        "sourceFingerprint": "a" * 64,
    }


def reviewed_mapping(row, *entry_numbers):
    return {
        row["propertyRecordId"]: {
            "propertyRecordId": row["propertyRecordId"],
            "status": "confirmed_listed",
            "listEntryNumbers": list(entry_numbers or ("1234567",)),
            "reviewedBy": "test",
            "reviewedAt": "2026-07-26",
        }
    }


def metadata_for_test(*positional, **keywords):
    """Build production-shaped metadata around deliberately tiny fixtures."""

    metadata = getattr(heritage, "metadata_for_run")(*positional, **keywords)
    metadata["heritageSync"]["sourceRecordsFetched"] = (
        heritage.DEFAULT_MIN_SOURCE_RECORDS
    )
    metadata["heritageSync"]["polygonRecordsFetched"] = (
        heritage.DEFAULT_MIN_POLYGON_RECORDS
    )
    return metadata


class HistoricEnglandContractTests(unittest.TestCase):
    def test_weekly_constraint_lookup_no_longer_requests_or_emits_listed_buildings(self):
        self.assertNotIn("listed-building", CONSTRAINT_DATASETS)
        with patch(
            "enrich_weekly_context.request_json",
            return_value={
                "entities": [{
                    "dataset": "listed-building",
                    "ListEntry": "1234567",
                    "Grade": "II",
                }]
            },
        ):
            result = constraints_for_item(
                transaction(),
                51.35,
                -0.36,
                {},
                SimpleNamespace(refresh_days=0, timeout=1, retries=0),
            )
        self.assertNotIn("historicEngland", result)

    def test_arcgis_multipoint_geometry_is_retained_for_candidate_indexing(self):
        fields = {
            "listEntryNumber": "ListEntry",
            "name": "Name",
            "grade": "Grade",
            "listDate": "ListDate",
            "amendDate": "AmendDate",
        }
        record = heritage.normalise_point_feature({
            "attributes": {
                "ListEntry": "1234567",
                "Name": "BIRCH HOUSE",
                "Grade": "Grade II*",
                "ListDate": 315532800000,
                "AmendDate": None,
            },
            "geometry": {
                "points": [[-0.36, 51.35], [-0.359, 51.351]],
            },
        }, fields)
        self.assertEqual(record["grade"], "II*")
        self.assertEqual(len(record["locations"]), 2)
        self.assertEqual(record["longitude"], -0.36)

    def test_arcgis_dates_are_always_interpreted_as_epoch_milliseconds(self):
        self.assertEqual(
            heritage.timestamp_from_arcgis(7_862_400_000),
            "1970-04-02T00:00:00Z",
        )
        self.assertEqual(
            heritage.date_from_arcgis(7_862_400_000),
            "1970-04-02",
        )
        self.assertEqual(
            heritage.date_from_arcgis(-3_628_281_600_000),
            "",
        )

    def test_multipoint_query_retains_only_locations_inside_reviewed_bbox(self):
        record = point(
            longitude=-0.36,
            latitude=51.35,
            locations=[
                (-0.36, 51.35),
                (-1.2, 51.35),
            ],
        )
        retained = heritage.retain_point_locations_in_bbox(
            record,
            heritage.DEFAULT_BBOX,
        )
        self.assertEqual(retained["locations"], [(-0.36, 51.35)])
        self.assertEqual(
            (retained["longitude"], retained["latitude"]),
            (-0.36, 51.35),
        )
        with self.assertRaisesRegex(ValueError, "no point inside"):
            heritage.retain_point_locations_in_bbox(
                point(
                    longitude=-1.2,
                    locations=[(-1.2, 51.35)],
                ),
                heritage.DEFAULT_BBOX,
            )

    def test_arbitrary_outside_multipoints_are_dropped_under_strict_gates(self):
        inside = point("1234567")
        envelope_only = point(
            "7654321",
            longitude=-1.2,
            latitude=51.019,
            locations=[(-1.2, 51.019), (0.2, 51.6)],
        )
        retained, dropped = heritage.filter_point_snapshot_to_bbox(
            [inside, envelope_only],
            heritage.DEFAULT_BBOX,
            max_dropped=1,
            max_dropped_fraction=0.5,
        )
        self.assertEqual(
            [item["listEntryNumber"] for item in retained],
            ["1234567"],
        )
        self.assertEqual(dropped, ["7654321"])
        with self.assertRaisesRegex(ValueError, "implausible envelope-only"):
            heritage.filter_point_snapshot_to_bbox(
                [inside, envelope_only],
                heritage.DEFAULT_BBOX,
                max_dropped=1,
                max_dropped_fraction=0.1,
            )
        with self.assertRaisesRegex(ValueError, "implausible envelope-only"):
            heritage.filter_point_snapshot_to_bbox(
                [inside, envelope_only],
                heritage.DEFAULT_BBOX,
                max_dropped=0,
                max_dropped_fraction=1,
            )

    def test_source_layer_pin_requires_item_id_name_id_and_geometry(self):
        valid = {
            "id": 0,
            "name": "Listed Building points",
            "type": "Feature Layer",
            "geometryType": "esriGeometryMultipoint",
            "serviceItemId": heritage.SOURCE_ITEM_ID,
        }
        heritage.validate_layer_pin(
            valid,
            layer_id=0,
            name="Listed Building points",
            geometry_type="esriGeometryMultipoint",
        )
        for field, bad_value in (
            ("id", 9),
            ("name", "Other points"),
            ("geometryType", "esriGeometryPoint"),
            ("serviceItemId", ""),
        ):
            changed = {**valid, field: bad_value}
            with self.assertRaises(RuntimeError):
                heritage.validate_layer_pin(
                    changed,
                    layer_id=0,
                    name="Listed Building points",
                    geometry_type="esriGeometryMultipoint",
                )

    def test_listing_currency_excludes_unrelated_service_root_edits(self):
        point_metadata = {"editingInfo": {"lastEditDate": 1_700_000_000_000}}
        polygon_metadata = {"editingInfo": {"lastEditDate": 1_700_000_001_000}}
        unrelated_service_metadata = {
            "editingInfo": {"lastEditDate": 1_700_000_009_000}
        }
        listed_currency = heritage.listed_building_source_last_edit_date(
            point_metadata,
            polygon_metadata,
        )
        self.assertEqual(listed_currency, "2023-11-14T22:13:21Z")
        self.assertNotEqual(
            listed_currency,
            heritage.source_last_edit_date(
                unrelated_service_metadata,
                point_metadata,
                polygon_metadata,
            ),
        )

    def test_arcgis_pagination_retains_every_page_and_rejects_repeated_ids(self):
        field_map = {
            "objectId": "OBJECTID",
            "listEntryNumber": "ListEntry",
            "name": "Name",
            "grade": "Grade",
        }
        first = {
            "features": [
                {"attributes": {"OBJECTID": 1}},
                {"attributes": {"OBJECTID": 2}},
            ],
            "exceededTransferLimit": True,
        }
        second = {
            "features": [{"attributes": {"OBJECTID": 3}}],
            "exceededTransferLimit": False,
        }
        with patch.object(heritage, "request_json", side_effect=[first, second]) as request:
            features = heritage.fetch_layer_features(
                "https://example.test/layer",
                {},
                field_map,
                heritage.DEFAULT_BBOX,
                args(),
            )
        self.assertEqual(len(features), 3)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].kwargs["params"]["resultOffset"],
            2,
        )

    def test_arcgis_declared_count_is_read_for_pagination_reconciliation(self):
        with patch.object(
            heritage,
            "request_json",
            return_value={"count": 10273},
        ) as request:
            count = heritage.fetch_layer_count(
                "https://example.test/layer",
                heritage.DEFAULT_BBOX,
                args(),
            )
        self.assertEqual(count, 10273)
        self.assertEqual(
            request.call_args.kwargs["params"]["returnCountOnly"],
            "true",
        )

    def test_invalid_grade_and_escaped_geography_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            heritage.normalise_grade("locally listed")
        escaped = point(longitude=-4.0, locations=[(-4.0, 51.35)])
        with self.assertRaisesRegex(ValueError, "escaped"):
            heritage.validate_source_snapshot(
                [escaped],
                [],
                heritage.DEFAULT_BBOX,
                minimum=1,
                maximum=20,
            )

    def test_boundary_polygons_without_retained_points_are_strictly_capped(self):
        retained, dropped = heritage.filter_polygon_snapshot_to_retained_points(
            [polygon("1234567"), polygon("7654321")],
            [point("1234567")],
            max_dropped=1,
            max_dropped_fraction=0.5,
        )
        self.assertEqual(
            [item["listEntryNumber"] for item in retained],
            ["1234567"],
        )
        self.assertEqual(dropped, ["7654321"])
        cohort_polygons = [
            polygon(str(1_000_000 + index))
            for index in range(100)
        ]
        cohort_points = [
            point(str(1_000_000 + index))
            for index in range(94)
        ]
        retained, dropped = heritage.filter_polygon_snapshot_to_retained_points(
            cohort_polygons,
            cohort_points,
        )
        self.assertEqual((len(retained), len(dropped)), (94, 6))
        with self.assertRaisesRegex(ValueError, "implausible orphan cohort"):
            heritage.filter_polygon_snapshot_to_retained_points(
                cohort_polygons,
                cohort_points[:89],
            )
        with self.assertRaisesRegex(ValueError, "implausible orphan cohort"):
            heritage.filter_polygon_snapshot_to_retained_points(
                [polygon("1234567"), polygon("7654321")],
                [point("1234567")],
                max_dropped=1,
                max_dropped_fraction=0.1,
            )
        with self.assertRaisesRegex(ValueError, "implausible orphan cohort"):
            heritage.filter_polygon_snapshot_to_retained_points(
                [polygon("1234567"), polygon("7654321")],
                [point("1234567")],
                max_dropped=0,
                max_dropped_fraction=1,
            )

    def test_postcode_centroid_never_confirms_polygon_containment(self):
        row = transaction()
        properties, projections, _snapshot = heritage.build_projections(
            [row],
            source(polygons=[polygon()]),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        projection = projections[row["propertyRecordId"]]
        self.assertFalse(properties[row["propertyRecordId"]]["coordinateTrusted"])
        self.assertEqual(projection["status"], "unknown")
        self.assertEqual(projection["entries"], [])

    def test_genuine_polygon_only_confirms_a_property_level_coordinate(self):
        row = transaction(
            coordinatePrecision="property",
            geocode={"precision": "Property address point"},
        )
        _properties, projections, _snapshot = heritage.build_projections(
            [row],
            source(polygons=[polygon()]),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        projection = projections[row["propertyRecordId"]]
        self.assertEqual(projection["status"], "confirmed_listed")
        self.assertEqual(
            projection["entries"][0]["matchMethod"],
            "genuine_polygon_contains",
        )
        self.assertEqual(
            projection["entries"][0]["matchConfidence"],
            "confirmed",
        )

    def test_reviewed_override_retains_every_official_entry(self):
        row = transaction()
        second = point(
            "7654321",
            grade="II*",
            name="BIRCH HOUSE GATE LODGE",
            longitude=-0.3602,
            locations=[(-0.3602, 51.35)],
        )
        mapping = {
            row["propertyRecordId"]: {
                "propertyRecordId": row["propertyRecordId"],
                "status": "confirmed_listed",
                "listEntryNumbers": ["1234567", "7654321"],
                "reviewedBy": "test",
                "reviewedAt": "2026-07-26",
            }
        }
        _properties, projections, _snapshot = heritage.build_projections(
            [row],
            source(points=[point(), second]),
            mapping,
            args(),
            "2026-07-26T12:00:00Z",
        )
        projection = projections[row["propertyRecordId"]]
        self.assertEqual(projection["status"], "confirmed_listed")
        self.assertEqual(len(projection["entries"]), 2)
        self.assertEqual(
            {entry["listEntryNumber"] for entry in projection["entries"]},
            {"1234567", "7654321"},
        )
        self.assertTrue(
            all(entry["matchMethod"] == "reviewed_override" for entry in projection["entries"])
        )

    def test_single_reviewed_entry_refines_all_rows_and_preserves_centroid(self):
        geocode = {
            "source": "Postcodes.io",
            "precision": "Postcode centroid",
        }
        first = transaction(
            id="lr-first",
            date="2025-01-01",
            coordinateSource="Postcodes.io",
            geocode=geocode,
        )
        second = transaction(
            id="lr-second",
            date="2026-01-01",
            coordinateSource="Postcodes.io",
            geocode=geocode,
        )
        designation = point(
            longitude=-0.355,
            latitude=51.355,
            locations=[(-0.355, 51.355)],
        )
        current_source = source(points=[designation])
        properties, projections, snapshot = heritage.build_projections(
            [first, second],
            current_source,
            reviewed_mapping(first),
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([first, second], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            current_source,
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
            published_transactions=enriched,
        )
        for row in enriched:
            self.assertEqual((row["latitude"], row["longitude"]), (51.355, -0.355))
            self.assertEqual(row["coordinateSource"], heritage.SOURCE_NAME)
            self.assertEqual(
                row["coordinatePrecision"],
                heritage.CONFIRMED_LOCATION_PRECISION,
            )
            self.assertEqual(
                (
                    row["geocode"]["postcodeCentroidLatitude"],
                    row["geocode"]["postcodeCentroidLongitude"],
                ),
                (51.35, -0.36),
            )
        self.assertEqual(
            metadata["heritageSync"]["confirmedLocationsApplied"],
            1,
        )
        self.assertEqual(
            metadata["heritageSync"]["confirmedLocationMode"],
            heritage.CONFIRMED_LOCATION_MODE,
        )
        self.assertEqual(
            heritage.heritage_contract_failures(
                enriched,
                metadata,
                require_complete=True,
            ),
            [],
        )

    def test_refinement_rerun_never_overwrites_original_centroid(self):
        row = transaction(
            coordinateSource="Postcodes.io",
            geocode={
                "source": "Postcodes.io",
                "precision": "Postcode centroid",
            },
        )
        first_source = source(points=[point(
            longitude=-0.355,
            latitude=51.355,
            locations=[(-0.355, 51.355)],
        )])
        first_properties, first_projections, _snapshot = heritage.build_projections(
            [row],
            first_source,
            reviewed_mapping(row),
            args(),
            "2026-07-26T12:00:00Z",
        )
        first_publication = heritage.apply_projections(
            [row],
            first_projections,
        )
        changed_source = {
            **source(points=[point(
                longitude=-0.354,
                latitude=51.354,
                locations=[(-0.354, 51.354)],
            )]),
            "sourceUpdatedAt": "2026-07-27T12:00:00Z",
            "sourceFingerprint": "c" * 64,
        }
        second_properties, second_projections, _snapshot = heritage.build_projections(
            first_publication,
            changed_source,
            reviewed_mapping(row),
            args(),
            "2026-07-27T12:00:00Z",
        )
        second_publication = heritage.apply_projections(
            first_publication,
            second_projections,
        )
        self.assertEqual(
            input_digest := heritage.input_fingerprint(first_properties),
            heritage.input_fingerprint(second_properties),
        )
        self.assertRegex(input_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            (
                second_publication[0]["geocode"]["postcodeCentroidLatitude"],
                second_publication[0]["geocode"]["postcodeCentroidLongitude"],
            ),
            (51.35, -0.36),
        )
        self.assertEqual(
            (
                second_publication[0]["latitude"],
                second_publication[0]["longitude"],
            ),
            (51.354, -0.354),
        )

    def test_refinement_fingerprint_is_stable_without_top_level_provenance(self):
        row = transaction(
            coordinateSource=None,
            coordinatePrecision=None,
            geocode={
                "source": "Postcodes.io",
                "precision": "Postcode centroid",
            },
        )
        current_source = source(points=[point(
            longitude=-0.355,
            latitude=51.355,
            locations=[(-0.355, 51.355)],
        )])
        first_properties, first_projections, _snapshot = heritage.build_projections(
            [row],
            current_source,
            reviewed_mapping(row),
            args(),
            "2026-07-26T12:00:00Z",
        )
        publication = heritage.apply_projections([row], first_projections)
        second_properties, _second_projections, _snapshot = heritage.build_projections(
            publication,
            current_source,
            reviewed_mapping(row),
            args(),
            "2026-07-26T13:00:00Z",
        )
        self.assertEqual(
            heritage.input_fingerprint(first_properties),
            heritage.input_fingerprint(second_properties),
        )

    def test_input_fingerprint_includes_every_structured_address_component(self):
        first = transaction()
        second = {
            **first,
            "saon": "WEST WING",
            "street": "RENAMED TEST ROAD",
            "locality": "CLAYGATE",
            "town": "ESHER TOWN",
        }
        self.assertNotEqual(
            heritage.input_fingerprint(heritage.build_properties([first])),
            heritage.input_fingerprint(heritage.build_properties([second])),
        )

    def test_ineligible_refinement_rolls_back_and_never_moves_other_matches(self):
        row = transaction(
            coordinateSource="Postcodes.io",
            geocode={
                "source": "Postcodes.io",
                "precision": "Postcode centroid",
            },
        )
        single = source(points=[point(
            longitude=-0.355,
            latitude=51.355,
            locations=[(-0.355, 51.355)],
        )])
        _properties, projections, _snapshot = heritage.build_projections(
            [row],
            single,
            reviewed_mapping(row),
            args(),
            "2026-07-26T12:00:00Z",
        )
        refined = heritage.apply_projections([row], projections)

        multipoint = source(points=[point(
            locations=[(-0.355, 51.355), (-0.356, 51.356)],
        )])
        _properties, projections, _snapshot = heritage.build_projections(
            refined,
            multipoint,
            reviewed_mapping(row),
            args(),
            "2026-07-27T12:00:00Z",
        )
        rolled_back = heritage.apply_projections(refined, projections)[0]
        self.assertEqual(
            (rolled_back["latitude"], rolled_back["longitude"]),
            (51.35, -0.36),
        )
        self.assertEqual(rolled_back["coordinateSource"], "Postcodes.io")
        self.assertEqual(
            rolled_back["coordinatePrecision"],
            "Postcode centroid",
        )

        removed_override_source = source(
            points=[point(
                name="OTHER BUILDING",
                longitude=-0.5,
                latitude=51.45,
                locations=[(-0.5, 51.45)],
            )],
            polygons=[],
        )
        _properties, projections, _snapshot = heritage.build_projections(
            refined,
            removed_override_source,
            {},
            args(),
            "2026-07-27T12:00:00Z",
        )
        removed_override = heritage.apply_projections(refined, projections)[0]
        self.assertEqual(
            removed_override["historicEngland"]["status"],
            "unknown",
        )
        self.assertEqual(
            (removed_override["latitude"], removed_override["longitude"]),
            (51.35, -0.36),
        )

        second = point(
            "7654321",
            locations=[(-0.354, 51.354)],
        )
        _properties, projections, _snapshot = heritage.build_projections(
            [row],
            source(points=[point(), second]),
            reviewed_mapping(row, "1234567", "7654321"),
            args(),
            "2026-07-26T12:00:00Z",
        )
        multiple_entries = heritage.apply_projections([row], projections)[0]
        self.assertEqual(
            (multiple_entries["latitude"], multiple_entries["longitude"]),
            (51.35, -0.36),
        )
        self.assertNotEqual(
            multiple_entries.get("coordinateSource"),
            heritage.SOURCE_NAME,
        )

        trusted = transaction(
            coordinateSource="OS Places",
            coordinatePrecision="property-address-point",
            geocode={"precision": "Property address point"},
        )
        _properties, projections, _snapshot = heritage.build_projections(
            [trusted],
            single,
            reviewed_mapping(trusted),
            args(),
            "2026-07-26T12:00:00Z",
        )
        trusted_publication = heritage.apply_projections([trusted], projections)[0]
        self.assertEqual(
            (trusted_publication["latitude"], trusted_publication["longitude"]),
            (51.35, -0.36),
        )
        self.assertEqual(trusted_publication["coordinateSource"], "OS Places")

    def test_unreviewed_distance_match_and_polygon_confirmation_never_refine_coordinates(self):
        unreviewed = transaction()
        _properties, projections, _snapshot = heritage.build_projections(
            [unreviewed],
            source(),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        unreviewed_publication = heritage.apply_projections(
            [unreviewed],
            projections,
        )[0]
        self.assertEqual(
            unreviewed_publication["historicEngland"]["status"],
            "unknown",
        )
        self.assertNotEqual(
            unreviewed_publication.get("coordinateSource"),
            heritage.SOURCE_NAME,
        )

        polygon_match = transaction(
            coordinateSource="OS Places",
            coordinatePrecision="property-address-point",
            geocode={"precision": "Property address point"},
        )
        _properties, projections, _snapshot = heritage.build_projections(
            [polygon_match],
            source(polygons=[polygon()]),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        polygon_publication = heritage.apply_projections(
            [polygon_match],
            projections,
        )[0]
        self.assertEqual(
            polygon_publication["historicEngland"]["entries"][0]["matchMethod"],
            "genuine_polygon_contains",
        )
        self.assertEqual(polygon_publication["coordinateSource"], "OS Places")

    def test_metadata_grade_totals_count_confirmed_entries_not_properties(self):
        row = transaction()
        second = point(
            "7654321",
            grade="II*",
            name="BIRCH HOUSE GATE LODGE",
            longitude=-0.3602,
            locations=[(-0.3602, 51.35)],
        )
        mapping = {
            row["propertyRecordId"]: {
                "propertyRecordId": row["propertyRecordId"],
                "status": "confirmed_listed",
                "listEntryNumbers": ["1234567", "7654321"],
                "reviewedBy": "test",
                "reviewedAt": "2026-07-26",
            }
        }
        properties, projections, snapshot = heritage.build_projections(
            [row],
            source(points=[point(), second]),
            mapping,
            args(),
            "2026-07-26T12:00:00Z",
        )
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            source(points=[point(), second]),
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        self.assertEqual(metadata["heritageSync"]["confirmedListed"], 1)
        self.assertEqual(metadata["heritageSync"]["confirmedEntries"], 2)
        self.assertEqual(metadata["heritageSync"]["confirmedUniqueListEntries"], 2)
        self.assertEqual(
            metadata["heritageSync"]["confirmedEntryGradeCounts"],
            {"I": 0, "II*": 1, "II": 1},
        )

    def test_unreviewed_multipoint_never_creates_a_centroid_candidate(self):
        row = transaction()
        multi = point(
            locations=[(-0.36, 51.35), (-0.362, 51.352)],
        )
        _properties, projections, _snapshot = heritage.build_projections(
            [row],
            source(points=[multi]),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        projection = projections[row["propertyRecordId"]]
        self.assertEqual(projection["status"], "unknown")
        self.assertEqual(projection["entries"], [])

    def test_every_transaction_for_one_property_gets_identical_state(self):
        first = transaction(id="lr-first", date="2025-01-01")
        second = transaction(id="lr-second", date="2026-01-01")
        properties, projections, snapshot = heritage.build_projections(
            [first, second],
            source(),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([first, second], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            source(),
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        self.assertEqual(enriched[0]["historicEngland"], enriched[1]["historicEngland"])
        self.assertEqual(
            heritage.heritage_contract_failures(
                enriched,
                metadata,
                require_complete=True,
            ),
            [],
        )
        self.assertEqual(metadata["heritageSync"]["propertiesAccountedFor"], 1)

    def test_public_contract_rejects_invalid_status_method_pairings(self):
        row = transaction()
        properties, projections, snapshot = heritage.build_projections(
            [row],
            source(),
            reviewed_mapping(row),
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([row], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            source(),
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        enriched[0]["historicEngland"]["entries"][0]["matchMethod"] = (
            "nearby_nhle_point"
        )
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(
            any("unproved match method" in failure for failure in failures)
        )

    def test_validator_reconciles_provenance_and_recomputes_fingerprints(self):
        row = transaction()
        properties, projections, snapshot = heritage.build_projections(
            [row],
            source(),
            reviewed_mapping(row),
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([row], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            source(),
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        enriched[0]["historicEngland"]["sourceSnapshot"] = (
            "nhle-2026-07-25-ffffffffffff"
        )
        metadata["heritageSync"]["inputFingerprint"] = "f" * 64
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(any("sourceSnapshot disagrees" in item for item in failures))
        self.assertTrue(any("inputFingerprint does not match" in item for item in failures))
        self.assertFalse(any("outputFingerprint does not match" in item for item in failures))

        enriched[0]["historicEngland"]["entries"][0]["name"] = "CHANGED NAME"
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(any("outputFingerprint does not match" in item for item in failures))

    def test_timestamp_only_source_change_is_semantically_unchanged(self):
        row = transaction()
        first_source = source()
        first_properties, first_projections, first_snapshot = (
            heritage.build_projections(
                [row],
                first_source,
                {},
                args(),
                "2026-07-26T12:00:00Z",
            )
        )
        first_metadata = metadata_for_test(
            {},
            first_properties,
            first_projections,
            first_source,
            first_snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        later_source = {
            **first_source,
            "sourceUpdatedAt": "2026-07-27T12:00:00Z",
        }
        later_properties, later_projections, later_snapshot = (
            heritage.build_projections(
                [row],
                later_source,
                {},
                args(),
                "2026-07-27T12:00:00Z",
            )
        )
        later_metadata = metadata_for_test(
            {},
            later_properties,
            later_projections,
            later_source,
            later_snapshot,
            "b" * 64,
            "2026-07-27T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        self.assertNotEqual(
            first_metadata["heritageSync"]["sourceSnapshot"],
            later_metadata["heritageSync"]["sourceSnapshot"],
        )
        self.assertEqual(
            first_metadata["heritageSync"]["outputFingerprint"],
            later_metadata["heritageSync"]["outputFingerprint"],
        )
        self.assertTrue(
            heritage.semantic_inputs_unchanged(
                first_metadata,
                later_metadata,
            )
        )

    def test_validator_returns_failures_for_malformed_entries_dates_and_timestamps(self):
        row = transaction()
        properties, projections, snapshot = heritage.build_projections(
            [row],
            source(),
            reviewed_mapping(row),
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([row], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            source(),
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        context = enriched[0]["historicEngland"]
        context["checkedAt"] = "2026-07-26"
        context["entries"][0]["listDate"] = "1980"
        context["entries"][0]["distanceMetres"] = 1.5
        context["entries"].append("not-an-object")
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(any("checkedAt is not an ISO timestamp" in item for item in failures))
        self.assertTrue(any("listDate is not ISO" in item for item in failures))
        self.assertTrue(any("distanceMetres" in item for item in failures))
        self.assertTrue(any("entry is not an object" in item for item in failures))

    def test_validator_is_total_for_malformed_outer_json(self):
        self.assertEqual(
            heritage.heritage_contract_failures({}, {}, require_complete=True),
            ["Heritage sync: transaction payload is not an array"],
        )
        self.assertEqual(
            heritage.heritage_contract_failures([], [], require_complete=True),
            ["Heritage sync: metadata root is not an object"],
        )
        failures = heritage.heritage_contract_failures(
            [None, "bad", {}],
            {"heritageSync": {"status": "malformed"}},
            require_complete=True,
        )
        self.assertTrue(any("transaction is not an object" in item for item in failures))
        self.assertTrue(any("historicEngland state is missing" in item for item in failures))

    def test_validator_reconciles_refined_coordinate_eligibility_and_count(self):
        row = transaction(
            coordinateSource="Postcodes.io",
            geocode={
                "source": "Postcodes.io",
                "precision": "Postcode centroid",
            },
        )
        current_source = source(points=[point(
            longitude=-0.355,
            latitude=51.355,
            locations=[(-0.355, 51.355)],
        )])
        properties, projections, snapshot = heritage.build_projections(
            [row],
            current_source,
            reviewed_mapping(row),
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([row], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            current_source,
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
            published_transactions=enriched,
        )
        metadata["heritageSync"]["confirmedLocationsApplied"] = 0
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(
            any("confirmedLocationsApplied" in item for item in failures)
        )

        metadata["heritageSync"]["confirmedLocationsApplied"] = 1
        enriched[0]["historicEngland"]["status"] = "candidate_review"
        enriched[0]["historicEngland"]["entries"][0].update({
            "matchMethod": "nearby_nhle_point",
            "matchConfidence": "review_required",
            "distanceMetres": 0,
        })
        metadata["heritageSync"]["outputFingerprint"] = (
            heritage.publication_fingerprint(enriched)
        )
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(
            any("requires one confirmed reviewed entry" in item for item in failures)
        )

    def test_validator_requires_the_exact_heritage_metadata_field_set(self):
        row = transaction()
        current_source = source()
        properties, projections, snapshot = heritage.build_projections(
            [row],
            current_source,
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([row], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            current_source,
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
            published_transactions=enriched,
        )
        self.assertEqual(
            set(metadata["heritageSync"]),
            heritage.HERITAGE_METADATA_FIELDS,
        )
        metadata["heritageSync"]["unreviewedField"] = True
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(
            any(
                "field set is invalid" in item and "unreviewedField" in item
                for item in failures
            )
        )

    def test_validator_rejects_collapsed_source_counts_and_geography_drift(self):
        row = transaction()
        current_source = source()
        properties, projections, snapshot = heritage.build_projections(
            [row],
            current_source,
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([row], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            current_source,
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
            published_transactions=enriched,
        )
        metadata["heritageSync"]["sourceRecordsFetched"] = 1
        metadata["heritageSync"]["polygonRecordsFetched"] = 1
        metadata["heritageSync"]["geography"]["bbox"][0] = -1
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(any("13,000-17,000" in item for item in failures))
        self.assertTrue(any("500-800" in item for item in failures))
        self.assertTrue(any("approved search envelope" in item for item in failures))

    def test_validator_rejects_refined_coordinate_drift_and_bad_provenance(self):
        geocode = {
            "source": "Postcodes.io",
            "precision": "Postcode centroid",
        }
        first = transaction(
            id="lr-first",
            date="2025-01-01",
            coordinateSource="Postcodes.io",
            geocode=geocode,
        )
        second = transaction(
            id="lr-second",
            date="2026-01-01",
            coordinateSource="Postcodes.io",
            geocode=geocode,
        )
        current_source = source(points=[point(
            longitude=-0.355,
            latitude=51.355,
            locations=[(-0.355, 51.355)],
        )])
        properties, projections, snapshot = heritage.build_projections(
            [first, second],
            current_source,
            reviewed_mapping(first),
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([first, second], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            current_source,
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
            published_transactions=enriched,
        )
        enriched[0]["latitude"] = 999
        enriched[0]["geocode"]["source"] = "Other"
        failures = heritage.heritage_contract_failures(
            enriched,
            metadata,
            require_complete=True,
        )
        self.assertTrue(any("not valid WGS84" in item for item in failures))
        self.assertTrue(any("Postcodes.io" in item for item in failures))
        self.assertTrue(any("coordinates disagree" in item for item in failures))

    def test_unreviewed_properties_are_unknown_with_or_without_coordinates(self):
        far_source = source(points=[point(
            longitude=0.1,
            latitude=51.6,
            locations=[(0.1, 51.6)],
        )])
        located = transaction()
        missing = transaction(
            id="lr-missing",
            address="2 TEST ROAD, ESHER, KT10 0AA",
            paon="2",
            latitude=None,
            longitude=None,
            coordinatePrecision=None,
            geocode=None,
        )
        _properties, projections, _snapshot = heritage.build_projections(
            [located, missing],
            far_source,
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        self.assertEqual(
            projections[located["propertyRecordId"]]["status"],
            "unknown",
        )
        self.assertEqual(
            projections[missing["propertyRecordId"]]["status"],
            "unknown",
        )

    def test_atomic_size_gate_preserves_the_existing_file(self):
        row = transaction()
        properties, projections, snapshot = heritage.build_projections(
            [row],
            source(),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        enriched = heritage.apply_projections([row], projections)
        metadata = metadata_for_test(
            {},
            properties,
            projections,
            source(),
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            output.write_text("last known good\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "grow the feed"):
                heritage.atomic_write(output, enriched, metadata, 1)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "last known good\n",
            )

    def test_override_ledger_verifies_full_address_identity_and_review(self):
        row = transaction()
        valid = {
            "schemaVersion": 1,
            "updatedAt": "2026-07-26",
            "productionRequired": True,
            "mappings": [{
                "propertyRecordId": row["propertyRecordId"],
                "address": row["address"],
                "postcode": row["postcode"],
                "status": "confirmed_listed",
                "listEntryNumbers": ["1234567"],
                "reviewedBy": "Reviewer",
                "reviewedAt": "2026-07-26",
                "evidenceUrl": (
                    "https://historicengland.org.uk/listing/the-list/list-entry/1234567"
                ),
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            ledger.write_text(json.dumps(valid), encoding="utf-8")
            mappings, digest = heritage.load_overrides(
                ledger,
                property_ids={row["propertyRecordId"]},
            )
        self.assertIn(row["propertyRecordId"], mappings)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_override_ledger_rejects_candidates_bad_dates_and_nonofficial_evidence(self):
        row = transaction()
        base = {
            "schemaVersion": 1,
            "updatedAt": "2026-07-26",
            "productionRequired": True,
            "mappings": [{
                "propertyRecordId": row["propertyRecordId"],
                "status": "confirmed_listed",
                "listEntryNumbers": ["1234567"],
                "reviewedBy": "Reviewer",
                "reviewedAt": "2026-07-26",
                "evidenceUrl": (
                    "https://historicengland.org.uk/listing/the-list/list-entry/1234567"
                ),
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            for field, value, message in (
                ("status", "candidate_review", "cannot publish a candidate"),
                ("reviewedAt", "26 July 2026", "ISO"),
                ("evidenceUrl", "https://example.test/1234567", "official"),
            ):
                payload = json.loads(json.dumps(base))
                payload["mappings"][0][field] = value
                ledger.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    heritage.load_overrides(
                        ledger,
                        property_ids={row["propertyRecordId"]},
                    )

    def test_address_ledger_covers_the_complete_reviewed_property_universe(self):
        payload = json.loads(
            (ROOT / "config" / "heritage-listing-overrides.json").read_text(
                encoding="utf-8"
            )
        )
        mappings = payload["mappings"]
        mapping_ids = {item["propertyRecordId"] for item in mappings}
        self.assertTrue(
            all(
                item["listEntryNumbers"]
                if item["status"] == "confirmed_listed"
                else not item["listEntryNumbers"]
                for item in mappings
            )
        )
        self.assertTrue(
            all(
                "not legal proof" in item["note"].lower()
                for item in mappings
                if item["status"] == "no_direct_match"
            )
        )
        audit = json.loads(
            (ROOT / "config" / "heritage-listing-address-audit.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(mappings), audit["canonicalPropertyCount"])
        self.assertEqual(len(mapping_ids), audit["canonicalPropertyCount"])
        self.assertEqual(
            Counter(item["status"] for item in mappings),
            Counter({
                "confirmed_listed": audit["confirmedPropertyCount"],
                "no_direct_match": (
                    audit["documentedNoDirectPropertyCount"]
                    + audit["genericNoDirectPropertyCount"]
                ),
                "unknown": audit["unknownPropertyCount"],
            }),
        )
        retired_confirmed = [
            item
            for item in audit.get("retiredMappings", [])
            if item["status"] == "confirmed_listed"
        ]
        reviewed_confirmed = audit["confirmedMappings"] + retired_confirmed
        self.assertEqual(
            len(reviewed_confirmed),
            143,
            "Every confirmed decision in the reviewed corpus must remain active or retired",
        )
        self.assertEqual(
            len({
                number
                for item in reviewed_confirmed
                for number in item["listEntryNumbers"]
            }),
            140,
            "Retiring a property must not erase its confirmed statutory evidence",
        )
        self.assertEqual(
            sum(audit["confirmedGradeCounts"].values()),
            audit["confirmedPropertyCount"],
        )
        audit_pairs = sorted(
            f"{item['propertyRecordId']}|{number}"
            for item in audit["confirmedMappings"]
            for number in item["listEntryNumbers"]
        )
        ledger_pairs = sorted(
            f"{item['propertyRecordId']}|{number}"
            for item in mappings
            if item["status"] == "confirmed_listed"
            for number in item["listEntryNumbers"]
        )
        self.assertEqual(audit_pairs, ledger_pairs)
        self.assertEqual(
            audit["confirmedPairDigest"],
            sha256_lines(audit_pairs),
        )
        transactions, _summary, _metadata = read_js(
            ROOT / "outputs" / "surrey-transactions.js"
        )
        current_property_ids = set(heritage.build_properties(transactions))
        self.assertEqual(mapping_ids, current_property_ids)
        self.assertEqual(
            audit["canonicalPropertyDigest"],
            sha256_lines(current_property_ids),
        )
        retired = {
            item["propertyRecordId"]: item
            for item in audit.get("retiredMappings", [])
        }
        beresford = retired.get(
            "property:BERESFORD COURT WESTERHAM ROAD OXTED RH8 0SL|RH80SL"
        )
        self.assertIsNotNone(beresford)
        self.assertEqual(beresford["status"], "confirmed_listed")
        self.assertEqual(beresford["listEntryNumbers"], ["1029786"])

    def test_publication_removes_legacy_weekly_planning_data_heritage_metadata(self):
        row = transaction()
        properties, projections, snapshot = heritage.build_projections(
            [row],
            source(),
            {},
            args(),
            "2026-07-26T12:00:00Z",
        )
        metadata = metadata_for_test(
            {
                "weeklyContext": {
                    "historicEngland": {
                        "source": "Planning Data API listed-building dataset",
                        "records": 0,
                    },
                    "schools": {"records": 1},
                }
            },
            properties,
            projections,
            source(),
            snapshot,
            "b" * 64,
            "2026-07-26T12:00:00Z",
            heritage.DEFAULT_BBOX,
        )
        self.assertNotIn("historicEngland", metadata["weeklyContext"])
        self.assertIn("schools", metadata["weeklyContext"])

    def test_workflow_runs_daily_and_monthly_alignment_invokes_same_script(self):
        daily = (
            ROOT / ".github" / "workflows" / "heritage-listed-buildings.yml"
        ).read_text(encoding="utf-8")
        monthly = (
            ROOT / ".github" / "workflows" / "monthly-property-refresh.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "15 8 * * *"', daily)
        self.assertIn("group: insight-data-refresh", daily)
        self.assertLess(
            daily.index("reconcile_heritage_address_audit.py --write"),
            daily.index("python3 scripts/enrich_listed_buildings.py"),
        )
        self.assertIn("enrich_listed_buildings.py --validate-only", daily)
        self.assertEqual(daily.rstrip().splitlines()[-1].strip(), "fi")
        self.assertIn("python3 scripts/enrich_listed_buildings.py", monthly)
        self.assertLess(
            monthly.index("reconcile_heritage_address_audit.py --write"),
            monthly.index("python3 scripts/enrich_listed_buildings.py"),
        )
        self.assertIn(
            "STAGING_BRANCH: automation/monthly-${{ github.run_id }}-${{ github.run_attempt }}",
            monthly,
        )
        self.assertIn('git push origin "HEAD:$RELEASE_BRANCH"', monthly)
        sweep_index = monthly.index("python3 scripts/sweep_land_registry.py")
        self.assertNotIn(
            "unittest discover -s tests",
            monthly[:sweep_index],
        )
        self.assertIn(
            "check_data_completeness.py --base-only",
            monthly[:sweep_index],
        )
        final_checkout = monthly.index(
            "- name: Check out committed expanded context",
            monthly.index("materialise-today:"),
        )
        final_publish = monthly.index("- name: Publish one validated monthly snapshot")
        self.assertIn("fetch-depth: 0", monthly[final_checkout:final_publish])
        self.assertIn(
            "+refs/heads/$RELEASE_BRANCH:refs/remotes/origin/$RELEASE_BRANCH",
            monthly,
        )
        self.assertGreater(
            monthly.index('git push origin "HEAD:$RELEASE_BRANCH"'),
            monthly.index("check_data_completeness.py --strict-metadata"),
        )

    def test_monthly_base_only_can_precede_heritage_alignment(self):
        existing = transaction()
        new_property = transaction(
            id="lr-new",
            address="2 TEST ROAD, ESHER, KT10 0AA",
            paon="2",
        )
        new_property.pop("historicEngland", None)
        stale_metadata = {
            "heritageSync": {
                "status": "complete",
                "propertiesAccountedFor": 1,
            }
        }
        self.assertEqual(
            completeness.dependent_heritage_failures(
                [existing, new_property],
                stale_metadata,
                base_only=True,
            ),
            [],
        )
        self.assertTrue(
            completeness.dependent_heritage_failures(
                [existing, new_property],
                stale_metadata,
                base_only=False,
            )
        )

    def test_full_completeness_fails_if_required_heritage_layer_disappears(self):
        self.assertTrue(heritage.heritage_publication_required())
        failures = completeness.dependent_heritage_failures(
            [transaction()],
            {},
            base_only=False,
        )
        self.assertTrue(any("no property states" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
