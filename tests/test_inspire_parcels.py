import json
import hashlib
import math
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

from build_property_uprn_links import empty_feed
from collect_inspire_parcels import (
    DownloadLinkParser,
    OSTN15_GRID_SHA256,
    authority_download_matches,
    audit_source,
    build_feed,
    configure_ostn15_transform,
    coverage_metadata,
    polygon_measurements,
    publication_time,
    publish_runtime_feeds,
    translated_signed_area,
)
from insight_data_utils import read_js
from reconcile_inspire_parcel_coverage import reconcile_feed
from validate_inspire_parcels import (
    feed_failures,
    parse_feed,
    registry_failures,
    retired_property_ids_from_metadata,
)
from validate_property_uprn_links import validation_failures as uprn_failures
from validate_inspire_parcel_review_queue import (
    QUEUE_SEMANTICS,
    parse_queue,
    validation_failures as review_queue_failures,
)
from runtime_release import finalise_body, parse_runtime
from validate_inspire_json_schemas import is_utc_second_timestamp, validator as schema_validator


ROOT = Path(__file__).resolve().parents[1]


def feature_xml(inspire_id, exterior, interiors=()):
    def pos_list(ring):
        return " ".join(str(value) for point in ring for value in point)

    holes = "".join(
        f"<gml:interior><gml:LinearRing><gml:posList>{pos_list(ring)}</gml:posList></gml:LinearRing></gml:interior>"
        for ring in interiors
    )
    return (
        "<wfs:member><LR:PREDEFINED>"
        "<LR:GEOMETRY><gml:Polygon srsName=\"urn:ogc:def:crs:EPSG::27700\">"
        f"<gml:exterior><gml:LinearRing><gml:posList>{pos_list(exterior)}</gml:posList></gml:LinearRing></gml:exterior>{holes}"
        "</gml:Polygon></LR:GEOMETRY>"
        f"<LR:INSPIREID>{inspire_id}</LR:INSPIREID><LR:VALIDFROM>2020-01-01T00:00:00Z</LR:VALIDFROM>"
        "<LR:BEGINLIFESPANVERSION>2020-01-01T00:00:00Z</LR:BEGINLIFESPANVERSION>"
        "</LR:PREDEFINED></wfs:member>"
    )


def write_source_zip(path, features):
    xml = (
        "<?xml version=\"1.0\"?><wfs:FeatureCollection "
        "xmlns:wfs=\"http://www.opengis.net/wfs/2.0\" "
        "xmlns:gml=\"http://www.opengis.net/gml/3.2\" "
        "xmlns:LR=\"www.landregistry.gov.uk\" "
        f"numberMatched=\"{len(features)}\" numberReturned=\"{len(features)}\" "
        "timeStamp=\"2026-08-02T04:00:00Z\">"
        + "".join(features)
        + "</wfs:FeatureCollection>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Land_Registry_Cadastral_Parcels.gml", xml)


class InspireGeometryTests(unittest.TestCase):
    def test_download_parser_associates_authority_row_with_generic_link_text(self):
        parser = DownloadLinkParser()
        parser.feed(
            '<table><tr><td>Elmbridge Borough Council</td>'
            '<td><a href="/download/resource/123">Download .gml</a></td></tr></table>'
        )
        self.assertEqual(parser.links, [("Elmbridge Borough Council Download .gml", "/download/resource/123")])

    def test_woking_download_match_does_not_capture_wokingham(self):
        parser = DownloadLinkParser()
        parser.feed(
            '<table><tr><td>Woking Borough Council</td><td><a href="/download/1">Download .gml</a></td></tr>'
            '<tr><td>Wokingham Borough Council</td><td><a href="/download/2">Download .gml</a></td></tr></table>'
        )
        matches = authority_download_matches(parser.links, {"name": "Woking Borough Council", "slug": "Woking_Borough_Council"})
        self.assertEqual(matches, [("Woking Borough Council Download .gml", "/download/1")])

    def test_translated_area_avoids_large_coordinate_cancellation(self):
        ring = [(500000.0, 150000.0), (500010.0, 150000.0), (500010.0, 150010.0), (500000.0, 150010.0), (500000.0, 150000.0)]
        self.assertEqual(translated_signed_area(ring), 100.0)
        area, centroid = polygon_measurements([ring])
        self.assertEqual(area, 100.0)
        self.assertEqual(centroid, (500005.0, 150005.0))

    def test_known_unselected_zero_area_interior_is_quarantined(self):
        outer = [(500000, 150000), (500010, 150000), (500010, 150010), (500000, 150010), (500000, 150000)]
        zero_hole = [(500001, 150001), (500002, 150002), (500003, 150003), (500001, 150001)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Waverley_Borough_Council.zip"
            write_source_zip(path, [feature_xml("53843805", outer, [zero_hole])])
            authority = {"name": "Waverley Borough Council", "slug": "Waverley_Borough_Council", "minimumFeatures": 1}
            stats, selected, digests, hits, candidates = audit_source(path, authority, set(), {"53843805"})
            self.assertEqual(stats["quarantinedInspireIds"], ["53843805"])
            self.assertEqual(selected, {})
            self.assertIn("53843805", digests)
            self.assertEqual(hits, {})
            self.assertEqual(candidates, {})

    def test_selected_or_unexpected_zero_area_ring_fails_closed(self):
        outer = [(500000, 150000), (500010, 150000), (500010, 150010), (500000, 150010), (500000, 150000)]
        zero_hole = [(500001, 150001), (500002, 150002), (500003, 150003), (500001, 150001)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.zip"
            write_source_zip(path, [feature_xml("53843805", outer, [zero_hole])])
            authority = {"name": "Test", "slug": "test", "minimumFeatures": 1}
            with self.assertRaisesRegex(ValueError, "structurally quarantined"):
                audit_source(path, authority, {"53843805"}, {"53843805"})
            with self.assertRaisesRegex(ValueError, "Unexpected zero-area"):
                audit_source(path, authority, set(), set())

    def test_transform_rejects_any_unpinned_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.tif"
            path.write_bytes(b"not the official grid")
            with self.assertRaisesRegex(ValueError, "grid hash mismatch"):
                configure_ostn15_transform(path)
        self.assertEqual(OSTN15_GRID_SHA256, "5d6ed64d2119952c4c559fa1fccbc594b6520fc3ec3ef2fc10be13202c4384fa")


class InspirePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed_path = ROOT / "outputs" / "inspire-parcels.js"
        cls.registry_path = ROOT / "config" / "inspire-parcel-associations.json"
        cls.authorities_path = ROOT / "config" / "inspire-authorities.json"
        cls.transitions_path = ROOT / "config" / "inspire-association-transitions.json"
        cls.feed_schema_path = ROOT / "config" / "inspire-parcels.schema.json"
        cls.transactions_path = ROOT / "outputs" / "surrey-transactions.js"
        cls.feed = parse_feed(cls.feed_path)
        cls.registry = json.loads(cls.registry_path.read_text())
        cls.authorities = json.loads(cls.authorities_path.read_text())
        cls.transitions = json.loads(cls.transitions_path.read_text())["records"]
        cls.feed_schema = json.loads(cls.feed_schema_path.read_text())
        transactions, _summary, metadata = read_js(cls.transactions_path)
        cls.canonical_ids = {row["propertyRecordId"] for row in transactions}
        cls.retired_property_ids = retired_property_ids_from_metadata(metadata)

    def test_registry_baseline_and_semantics(self):
        self.assertEqual(registry_failures(self.registry, self.canonical_ids), [])
        self.assertEqual(self.registry["approvalBaseline"], {
            "canonicalProperties": 3766,
            "automaticIndicative": 2871,
            "reviewedIndicative": 357,
            "associatedProperties": 3228,
            "coveragePercent": 85.7143,
            "semantics": "minimum approved association provenance; live coverage denominator is rebuilt from the current canonical transaction feed",
        })

    def test_checked_in_feed_passes_full_contract(self):
        self.assertEqual(
            feed_failures(
                self.feed,
                self.registry,
                self.authorities,
                self.canonical_ids,
                self.feed_path.stat().st_size,
                hashlib.sha256(self.registry_path.read_bytes()).hexdigest(),
                self.transitions,
                self.retired_property_ids,
            ),
            [],
        )

    def test_identity_migration_transition_uses_a_retired_property_id(self):
        canonical_property_id = (
            "property:COROMANDEL 33 FAIRMILE AVENUE COBHAM KT11 2JA|KT112JA"
        )
        transition = next(
            item
            for item in self.transitions
            if item["transitionId"] == "inspire-transition-coromandel-canonical-id-20260811"
        )
        self.assertEqual(
            transition["propertyId"],
            "property:33 FAIRMILE AVENUE COBHAM KT11 2JA|KT112JA",
        )
        self.assertIn(transition["propertyId"], self.retired_property_ids)
        self.assertEqual(transition["action"], "remove")
        self.assertIsNone(transition["replacementParcelId"])
        registry_record = next(
            item
            for item in self.registry["records"]
            if item["propertyId"] == canonical_property_id
        )
        self.assertEqual(registry_record["inspireId"], "34084671")
        self.assertEqual(
            self.feed["associationsByProperty"][canonical_property_id][
                "primaryParcelId"
            ],
            "34084671",
        )

    def test_reviewed_parent_parcel_replacements_are_public_transitions(self):
        expected = {
            "property:COUCHMORE HOUSE LITTLEWORTH ROAD ESHER KT10 9TN|KT109TN": {
                "parcel": "34359650",
                "oldParcel": "34360203",
                "area": 5191.38,
                "squareFeet": 55880,
                "acres": 1.2828,
                "centroid": [-0.3461646, 51.3756511],
                "bbox": [-0.34660869, 51.37512151, -0.34576131, 51.37612235],
                "boundary": 13.425,
            },
            "property:MAGPIE MANOR CHURCH ROAD CLAYGATE ESHER KT10 0JP|KT100JP": {
                "parcel": "34439175",
                "oldParcel": "34433086",
                "area": 1510.53,
                "squareFeet": 16259,
                "acres": 0.3733,
                "centroid": [-0.3376192, 51.3583163],
                "bbox": [-0.3380629, 51.35808051, -0.33721, 51.35856849],
                "boundary": 9.2563,
            },
        }
        transitions_by_property = {transition["propertyId"]: transition for transition in self.transitions}
        for property_id, values in expected.items():
            with self.subTest(property_id=property_id):
                transition = transitions_by_property[property_id]
                self.assertEqual(transition["action"], "replace")
                self.assertEqual(transition["previousParcelId"], values["oldParcel"])
                self.assertEqual(transition["replacementParcelId"], values["parcel"])
                self.assertEqual(transition["priorAssociationReleaseId"], "inspire-parcels-2026-08-02-94e51b3bafeb")
                association = self.feed["associationsByProperty"][property_id]
                self.assertEqual(association["primaryParcelId"], values["parcel"])
                self.assertEqual(association["associationStatus"], "reviewed_indicative")
                self.assertEqual(association["boundaryDistanceMetres"], values["boundary"])
                self.assertNotIn(values["oldParcel"], self.feed["parcelsById"])
                parcel = self.feed["parcelsById"][values["parcel"]]
                self.assertEqual(parcel["areaSquareMetres"], values["area"])
                self.assertEqual(parcel["areaSquareFeet"], values["squareFeet"])
                self.assertEqual(parcel["areaAcres"], values["acres"])
                self.assertEqual(parcel["centroid"], values["centroid"])
                self.assertEqual(parcel["bbox"], values["bbox"])
        self.assertEqual(self.feed["coverage"]["associatedProperties"], 3228)
        self.assertEqual(self.feed["coverage"]["automaticIndicative"], 2870)
        self.assertEqual(self.feed["coverage"]["reviewedIndicative"], 358)

    def test_feed_has_exact_approved_indexes_and_no_uprn(self):
        self.assertEqual(len(self.feed["associationsByProperty"]), 3228)
        self.assertEqual(len(self.feed["parcelsById"]), 3228)
        self.assertNotIn('"uprn"', self.feed_path.read_text().casefold())
        self.assertEqual(self.feed["source"]["sourceFeatureOccurrences"], 525580)
        self.assertEqual(self.feed["source"]["sourceDistinctInspireIds"], 523956)
        self.assertEqual(self.feed["source"]["sourceDuplicateOccurrences"], 1624)

    def test_monthly_workflow_tracks_hmlr_cadence_and_pinned_grid(self):
        workflow = (ROOT / ".github/workflows/monthly-inspire-parcels.yml").read_text()
        self.assertIn('cron: "30 20 1-9 * *"', workflow)
        self.assertIn("pyproj==3.6.1", workflow)
        self.assertIn(OSTN15_GRID_SHA256, workflow)
        self.assertIn("--download-current", workflow)
        self.assertIn("outputs/surrey-transactions.js", workflow)
        self.assertIn("outputs/inspire-parcel-review-queue.js", workflow)
        self.assertIn("jsonschema==4.23.0", workflow)
        self.assertIn("queue: max", workflow)
        checkout = workflow.index("uses: actions/checkout@v4")
        synchronise = workflow.index('git fetch origin "$GITHUB_REF_NAME"', checkout)
        setup = workflow.index("- name: Set up Python", checkout)
        self.assertLess(checkout, synchronise)
        self.assertLess(synchronise, setup)
        self.assertIn('git checkout -B "$GITHUB_REF_NAME" "origin/$GITHUB_REF_NAME"', workflow[checkout:setup])

    def test_static_notice_uses_year_template_while_feed_uses_snapshot_year(self):
        notice = (ROOT / "DATA-NOTICES.md").read_text()
        self.assertIn("[year of supply or date of publication]", notice)
        self.assertNotIn("database rights 2026", notice)
        self.assertIn("actual year\nderived from its HMLR source snapshot", notice)
        snapshot_year = self.feed["source"]["sourceSnapshot"][13:17]
        self.assertIn(f"database rights {snapshot_year}", self.feed["source"]["hmlrAttribution"])
        self.assertIn(f"database rights {snapshot_year}", self.feed["source"]["osAttribution"])

    def test_plus_one_canonical_property_lowers_live_coverage_without_invalidating_baseline(self):
        extra = "property:99 NEW ROAD GUILDFORD GU1 9ZZ|GU19ZZ"
        expanded = set(self.canonical_ids) | {extra}
        self.assertEqual(registry_failures(self.registry, expanded), [])
        coverage = coverage_metadata(expanded, self.feed["associationsByProperty"])
        self.assertEqual(coverage["canonicalProperties"], len(expanded))
        self.assertEqual(coverage["associatedProperties"], 3228)
        self.assertEqual(
            coverage["unassociatedProperties"],
            len(expanded) - len(self.feed["associationsByProperty"]),
        )
        self.assertLess(
            coverage["coveragePercent"],
            self.feed["coverage"]["coveragePercent"],
        )

    def test_live_denominator_is_independent_of_the_historical_approval_baseline(self):
        self.assertEqual(self.registry["approvalBaseline"]["canonicalProperties"], 3766)
        self.assertEqual(
            self.feed["coverage"]["canonicalProperties"],
            len(self.canonical_ids),
        )
        self.assertEqual(registry_failures(self.registry, self.canonical_ids), [])

    def test_coverage_reconciliation_preserves_audited_evidence_and_is_idempotent(self):
        extra = "property:99 NEW ROAD GUILDFORD GU1 9ZZ|GU19ZZ"
        expanded = set(self.canonical_ids) | {extra}
        registry_sha256 = hashlib.sha256(self.registry_path.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inspire-parcels.js"
            path.write_bytes(self.feed_path.read_bytes())
            before = parse_feed(path)

            changed, reconciled = reconcile_feed(
                path,
                canonical_ids=expanded,
                registry=self.registry,
                authorities=self.authorities,
                feed_schema=self.feed_schema,
                registry_sha256=registry_sha256,
                configured_transitions=self.transitions,
                retired_property_ids=self.retired_property_ids,
                publication_time="2026-08-12T12:00:00Z",
            )

            self.assertTrue(changed)
            self.assertEqual(
                reconciled["coverage"],
                coverage_metadata(expanded, before["associationsByProperty"]),
            )
            for key in (
                "schemaVersion",
                "canonicalIdentityMode",
                "associationSemantics",
                "source",
                "associationTransitions",
                "associationsByProperty",
                "parcelsById",
            ):
                self.assertEqual(reconciled[key], before[key])
            self.assertEqual(
                reconciled["source"]["sourceSnapshot"],
                before["source"]["sourceSnapshot"],
            )
            self.assertNotEqual(reconciled["releaseId"], before["releaseId"])
            parsed, _raw_core, digest = parse_runtime(
                path,
                "window.INSIGHT_INSPIRE_PARCELS",
            )
            self.assertEqual(parsed, reconciled)
            self.assertTrue(reconciled["releaseId"].endswith("-" + digest))
            published_bytes = path.read_bytes()

            changed_again, same_feed = reconcile_feed(
                path,
                canonical_ids=expanded,
                registry=self.registry,
                authorities=self.authorities,
                feed_schema=self.feed_schema,
                registry_sha256=registry_sha256,
                configured_transitions=self.transitions,
                retired_property_ids=self.retired_property_ids,
                publication_time="2026-08-13T12:00:00Z",
            )

            self.assertFalse(changed_again)
            self.assertEqual(same_feed, reconciled)
            self.assertEqual(path.read_bytes(), published_bytes)

    def test_coverage_reconciliation_blocks_non_coverage_contract_failures(self):
        extra = "property:99 NEW ROAD GUILDFORD GU1 9ZZ|GU19ZZ"
        expanded = set(self.canonical_ids) | {extra}
        registry_sha256 = hashlib.sha256(self.registry_path.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inspire-parcels.js"
            core = {
                key: deepcopy(value)
                for key, value in self.feed.items()
                if key not in {"generatedAt", "releaseId"}
            }
            core["source"]["displayCrs"] = "EPSG:3857"
            body, _release_id = finalise_body(
                core,
                "2026-08-12T12:00:00Z",
                "inspire-parcels",
                core["source"]["sourceSnapshot"].removeprefix("hmlr-inspire-"),
            )
            path.write_text(
                "window.INSIGHT_INSPIRE_PARCELS = " + body + ";\n",
                encoding="utf-8",
            )
            invalid_bytes = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "source/display CRS"):
                reconcile_feed(
                    path,
                    canonical_ids=expanded,
                    registry=self.registry,
                    authorities=self.authorities,
                    feed_schema=self.feed_schema,
                    registry_sha256=registry_sha256,
                    configured_transitions=self.transitions,
                    retired_property_ids=self.retired_property_ids,
                    publication_time="2026-08-13T12:00:00Z",
                )
            self.assertEqual(path.read_bytes(), invalid_bytes)

    def test_coverage_reconciliation_blocks_unknown_associations(self):
        contracted = set(self.canonical_ids)
        contracted.remove(next(iter(self.feed["associationsByProperty"])))
        registry_sha256 = hashlib.sha256(self.registry_path.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inspire-parcels.js"
            path.write_bytes(self.feed_path.read_bytes())
            original_bytes = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "unknown canonical property"):
                reconcile_feed(
                    path,
                    canonical_ids=contracted,
                    registry=self.registry,
                    authorities=self.authorities,
                    feed_schema=self.feed_schema,
                    registry_sha256=registry_sha256,
                    configured_transitions=self.transitions,
                    retired_property_ids=self.retired_property_ids,
                    publication_time="2026-08-13T12:00:00Z",
                )
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_coverage_reconciliation_blocks_schema_only_defects(self):
        extra = "property:99 NEW ROAD GUILDFORD GU1 9ZZ|GU19ZZ"
        expanded = set(self.canonical_ids) | {extra}
        registry_sha256 = hashlib.sha256(self.registry_path.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inspire-parcels.js"
            core = {
                key: deepcopy(value)
                for key, value in self.feed.items()
                if key not in {"generatedAt", "releaseId"}
            }
            core["debug"] = {"privateNotes": "must never be carried into publication"}
            body, _release_id = finalise_body(
                core,
                "2026-08-12T12:00:00Z",
                "inspire-parcels",
                core["source"]["sourceSnapshot"].removeprefix("hmlr-inspire-"),
            )
            path.write_text(
                "window.INSIGHT_INSPIRE_PARCELS = " + body + ";\n",
                encoding="utf-8",
            )
            invalid_bytes = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "published JSON Schema"):
                reconcile_feed(
                    path,
                    canonical_ids=expanded,
                    registry=self.registry,
                    authorities=self.authorities,
                    feed_schema=self.feed_schema,
                    registry_sha256=registry_sha256,
                    configured_transitions=self.transitions,
                    retired_property_ids=self.retired_property_ids,
                    publication_time="2026-08-13T12:00:00Z",
                )
            self.assertEqual(path.read_bytes(), invalid_bytes)

    def test_monthly_property_refresh_reconciles_inspire_before_sales(self):
        workflow = (ROOT / ".github/workflows/monthly-property-refresh.yml").read_text()
        reconcile_job = workflow.index("reconcile-inspire-coverage:")
        sales_job = workflow.index("refresh-sales-history:")
        self.assertLess(reconcile_job, sales_job)
        self.assertIn(
            "needs: reconcile-inspire-coverage",
            workflow[sales_job:],
        )
        reconciliation = workflow[reconcile_job:sales_job]
        self.assertIn("scripts/reconcile_inspire_parcel_coverage.py", reconciliation)
        self.assertIn("scripts/validate_inspire_parcels.py", reconciliation)
        self.assertIn("git add outputs/inspire-parcels.js", reconciliation)

    def test_raw_core_mutation_changes_release_digest(self):
        core = {key: deepcopy(value) for key, value in self.feed.items() if key not in {"generatedAt", "releaseId"}}
        _body, release_before = finalise_body(core, "2026-08-10T22:00:00Z", "inspire-parcels", "2026-08-02")
        first_property = next(iter(core["associationsByProperty"]))
        core["associationsByProperty"][first_property]["boundaryDistanceMetres"] += 0.0001
        _body, release_after = finalise_body(core, "2026-08-10T22:00:00Z", "inspire-parcels", "2026-08-02")
        self.assertNotEqual(release_before, release_after)

    def test_T1_T2_plus_canonical_plus_accepted_link_noop_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.js"
            review_path = Path(directory) / "review.js"
            core_t1 = {
                "schemaVersion": 1,
                "source": {"sourceSnapshot": "hmlr-inspire-2026-08-02"},
                "coverage": {"canonicalProperties": 1},
                "associationsByProperty": {},
            }
            review_core = {"schemaVersion": 1, "sourceSnapshot": "hmlr-inspire-2026-08-02", "candidatesByProperty": {}}
            feed_t1, _review_t1, changed_t1, _ = publish_runtime_feeds(core_t1, review_core, path, review_path, "2026-08-10T10:00:00Z")
            self.assertTrue(changed_t1)
            bytes_t1 = path.read_bytes()
            sha_t1 = hashlib.sha256(bytes_t1).hexdigest()
            mtime_t1 = path.stat().st_mtime_ns
            feed_t2, _review_t2, changed_t2, review_changed_t2 = publish_runtime_feeds(deepcopy(core_t1), deepcopy(review_core), path, review_path, "2026-08-10T11:00:00Z")
            self.assertFalse(changed_t2)
            self.assertFalse(review_changed_t2)
            self.assertEqual(path.read_bytes(), bytes_t1)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha_t1)
            self.assertEqual(path.stat().st_mtime_ns, mtime_t1)
            self.assertEqual((feed_t2["generatedAt"], feed_t2["releaseId"]), (feed_t1["generatedAt"], feed_t1["releaseId"]))
            plus_canonical = deepcopy(core_t1)
            plus_canonical["coverage"]["canonicalProperties"] = 2
            feed_t3, _review_t3, changed_t3, _ = publish_runtime_feeds(plus_canonical, review_core, path, review_path, "2026-08-10T12:00:00Z")
            self.assertTrue(changed_t3)
            self.assertEqual(feed_t3["generatedAt"], "2026-08-10T12:00:00Z")
            self.assertNotEqual(feed_t3["releaseId"], feed_t1["releaseId"])
            plus_link = deepcopy(plus_canonical)
            plus_link["associationsByProperty"]["property:new|NEW"] = {"primaryParcelId": "1"}
            feed_t4, _review_t4, changed_t4, _ = publish_runtime_feeds(plus_link, review_core, path, review_path, "2026-08-10T13:00:00Z")
            self.assertTrue(changed_t4)
            self.assertNotEqual(feed_t4["releaseId"], feed_t3["releaseId"])
            bytes_t4 = path.read_bytes()
            mtime_t4 = path.stat().st_mtime_ns
            feed_t5, _review_t5, changed_t5, _ = publish_runtime_feeds(deepcopy(plus_link), deepcopy(review_core), path, review_path, "2026-08-10T14:00:00Z")
            self.assertFalse(changed_t5)
            self.assertEqual(path.read_bytes(), bytes_t4)
            self.assertEqual(path.stat().st_mtime_ns, mtime_t4)
            self.assertEqual((feed_t5["generatedAt"], feed_t5["releaseId"]), (feed_t4["generatedAt"], feed_t4["releaseId"]))

    def test_semantically_equal_reordered_core_is_a_new_raw_byte_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.js"
            review_path = Path(directory) / "review.js"
            core = {
                "schemaVersion": 1,
                "source": {"sourceSnapshot": "hmlr-inspire-2026-08-02"},
                "coverage": {"canonicalProperties": 1},
                "associationsByProperty": {},
            }
            review = {
                "schemaVersion": 1,
                "sourceSnapshot": "hmlr-inspire-2026-08-02",
                "candidatesByProperty": {},
            }
            first, _review, changed, _review_changed = publish_runtime_feeds(
                core, review, path, review_path, "2026-08-10T10:00:00Z"
            )
            self.assertTrue(changed)
            reordered = dict(reversed(list(core.items())))
            second, _review, changed, _review_changed = publish_runtime_feeds(
                reordered, review, path, review_path, "2026-08-10T11:00:00Z"
            )
            self.assertTrue(changed)
            self.assertEqual(second["generatedAt"], "2026-08-10T11:00:00Z")
            self.assertNotEqual(second["releaseId"], first["releaseId"])


class UPRNOnboardingTests(unittest.TestCase):
    class IdentityTransformer:
        @staticmethod
        def transform(x, y, direction=None):
            return x, y

    def build_synthetic(self, directory, links, prior=None, transitions=None):
        source = Path(directory) / "Test_Authority.zip"
        ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        write_source_zip(source, [feature_xml("100", ring)])
        config = {
            "sourcePage": "https://example.test/inspire",
            "publicationCadence": "first Sunday of each month; previous-month data",
            "minimumDistinctInspireIds": 1,
            "knownSourceQuarantines": [],
            "authorities": [{"name": "Test Authority", "slug": "Test_Authority", "minimumFeatures": 1}],
        }
        registry = {
            "canonicalIdentityMode": "full-normalised-address-plus-postcode-fail-closed",
            "records": [],
            "approvalBaseline": {"canonicalProperties": 0, "automaticIndicative": 0, "reviewedIndicative": 0, "associatedProperties": 0, "coveragePercent": 0, "semantics": "test"},
            "sourceStudy": {},
        }
        property_id = "property:1 TEST ROAD TEST GU1 1AA|GU11AA"
        uprn_feed = {"linksByProperty": {property_id: links} if links else {}}
        return build_feed(
            config, registry, Path(directory), self.IdentityTransformer(), {property_id}, set(), uprn_feed,
            "a" * 64, prior or {}, transitions or [],
            None, None, [],
        )

    def link(self):
        return {
            "propertyId": "property:1 TEST ROAD TEST GU1 1AA|GU11AA",
            "sourceId": "addressbase",
            "uprn": "100000000001",
            "matchStatus": "confirmed_address_match",
            "evidenceTier": "authoritative_address_source",
            "longitude": 5,
            "latitude": 5,
            "coordinateSource": "AddressBase Premium",
            "sourceSnapshot": "addressbase-2026-08",
            "checkedAt": "2026-08-10T12:00:00Z",
            "limitations": [],
        }

    def test_plus_one_accepted_authoritative_link_auto_associates_unique_clear_parcel(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, queue = self.build_synthetic(directory, self.link())
        property_id = self.link()["propertyId"]
        association = feed["associationsByProperty"][property_id]
        self.assertEqual(association["primaryParcelId"], "100")
        self.assertEqual(association["evidenceTier"], "authoritative_uprn_indicative")
        self.assertEqual(association["boundaryDistanceMetres"], 5.0)
        self.assertEqual(queue["candidatesByProperty"][property_id]["outcome"], "automatically_associated_indicative")

    def test_any_prior_association_cannot_disappear_without_reviewed_transition(self):
        property_id = self.link()["propertyId"]
        prior = {property_id: {"primaryParcelId": "100", "matchMethod": "accepted-authoritative-uprn-unique-clear-containment"}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "explicit reviewed association transition"):
                self.build_synthetic(directory, None, prior=prior)
            transition = {
                "transitionId": "inspire-transition-test",
                "propertyId": property_id,
                "previousParcelId": "100",
                "priorAssociationReleaseId": "inspire-parcels-2026-08-02-aaaaaaaaaaaa",
                "priorSourceSnapshot": "hmlr-inspire-2026-08-02",
                "reviewedAt": "2026-08-10T12:00:00Z",
                "reviewedBy": "test",
                "reason": "fixture revocation",
                "action": "remove",
                "replacementParcelId": None,
            }
            # The reviewed transition is published as durable append-only history.
            feed, _queue = build_feed(
                {
                    "sourcePage": "https://example.test/inspire",
                    "publicationCadence": "first Sunday of each month; previous-month data",
                    "minimumDistinctInspireIds": 1,
                    "knownSourceQuarantines": [],
                    "authorities": [{"name": "Test Authority", "slug": "Test_Authority", "minimumFeatures": 1}],
                },
                {"canonicalIdentityMode": "full-normalised-address-plus-postcode-fail-closed", "records": [], "approvalBaseline": {}, "sourceStudy": {}},
                Path(directory), self.IdentityTransformer(), {property_id}, set(), {"linksByProperty": {}}, "a" * 64,
                prior, [transition], "inspire-parcels-2026-08-02-aaaaaaaaaaaa", "hmlr-inspire-2026-08-02", [],
            )
            self.assertNotIn(property_id, feed["associationsByProperty"])
            self.assertEqual(feed["associationTransitions"], [transition])

    def test_two_successive_replacements_form_an_append_only_property_chain(self):
        property_id = self.link()["propertyId"]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Test_Authority.zip"
            features = []
            for parcel_id, offset in (("100", 0), ("101", 20), ("102", 40)):
                ring = [(offset, 0), (offset + 10, 0), (offset + 10, 10), (offset, 10), (offset, 0)]
                features.append(feature_xml(parcel_id, ring))
            write_source_zip(source, features)
            config = {
                "sourcePage": "https://example.test/inspire",
                "publicationCadence": "first Sunday of each month; previous-month data",
                "minimumDistinctInspireIds": 1,
                "knownSourceQuarantines": [],
                "authorities": [{"name": "Test Authority", "slug": "Test_Authority", "minimumFeatures": 1}],
            }

            def registry(parcel_id):
                return {
                    "canonicalIdentityMode": "full-normalised-address-plus-postcode-fail-closed",
                    "records": [{
                        "propertyId": property_id,
                        "inspireId": parcel_id,
                        "associationStatus": "automatic_indicative",
                        "matchMethod": "fixture",
                        "evidenceTier": "transaction_linked_indicative",
                        "spatialClassification": "unique_interior_clear",
                        "boundaryDistanceMetres": 5.0,
                        "reviewDecision": None,
                    }],
                    "approvalBaseline": {},
                    "sourceStudy": {},
                }

            prior_release = "inspire-parcels-2026-08-02-aaaaaaaaaaaa"
            prior_snapshot = "hmlr-inspire-2026-08-02"
            first = {
                "transitionId": "inspire-transition-first",
                "propertyId": property_id,
                "previousParcelId": "100",
                "priorAssociationReleaseId": prior_release,
                "priorSourceSnapshot": prior_snapshot,
                "reviewedAt": "2026-08-10T12:00:00Z",
                "reviewedBy": "test",
                "reason": "first reviewed parcel successor",
                "action": "replace",
                "replacementParcelId": "101",
            }
            prior_association = {property_id: {"primaryParcelId": "100"}}
            feed_one, _queue = build_feed(
                config, registry("101"), Path(directory), self.IdentityTransformer(), {property_id}, set(),
                {"linksByProperty": {}}, "a" * 64, prior_association, [first], prior_release, prior_snapshot, [],
            )
            feed_one_body, _release = finalise_body(
                feed_one, "2026-08-10T13:00:00Z", "inspire-parcels", "2026-08-02"
            )
            published_one = json.loads(feed_one_body)
            second = {
                "transitionId": "inspire-transition-second",
                "propertyId": property_id,
                "previousParcelId": "101",
                "priorAssociationReleaseId": published_one["releaseId"],
                "priorSourceSnapshot": prior_snapshot,
                "reviewedAt": "2026-08-10T14:00:00Z",
                "reviewedBy": "test",
                "reason": "second reviewed parcel successor",
                "action": "replace",
                "replacementParcelId": "102",
            }
            feed_two, _queue = build_feed(
                config, registry("102"), Path(directory), self.IdentityTransformer(), {property_id}, set(),
                {"linksByProperty": {}}, "b" * 64, published_one["associationsByProperty"], [first, second],
                published_one["releaseId"], prior_snapshot, [first],
            )
            self.assertEqual(feed_two["associationsByProperty"][property_id]["primaryParcelId"], "102")
            self.assertEqual(feed_two["associationTransitions"], [first, second])


class PropertyUprnContractTests(unittest.TestCase):
    def setUp(self):
        self.a = "property:1 EXAMPLE ROAD GUILDFORD GU1 1AA|GU11AA"
        self.b = "property:2 EXAMPLE ROAD GUILDFORD GU1 1AA|GU11AA"
        self.canonical = {self.a, self.b}

    def link(self, property_id, uprn="100000000001"):
        return {
            "propertyId": property_id,
            "sourceId": "addressbase",
            "uprn": uprn,
            "matchStatus": "confirmed_address_match",
            "evidenceTier": "authoritative_address_source",
            "longitude": -0.5,
            "latitude": 51.2,
            "coordinateSource": "AddressBase Premium",
            "sourceSnapshot": "example-2026-08",
            "checkedAt": "2026-08-10T12:00:00Z",
            "limitations": [],
        }

    def source(self):
        return {
            "sourceId": "addressbase",
            "name": "AddressBase Premium",
            "sourceSnapshot": "example-2026-08",
            "checkedAt": "2026-08-10T12:00:00Z",
            "coordinateBasis": "authoritative_address_point",
            "licenceOrEntitlement": {
                "type": "commercial_entitlement",
                "reference": "test entitlement",
                "permitsPublicDerivedPublication": True,
            },
            "redistributionClassification": "licensed_for_insight_publication",
        }

    def test_empty_feed_is_valid_and_fail_closed(self):
        self.assertEqual(uprn_failures(empty_feed(), self.canonical), [])

    def test_non_explicit_status_and_nonfinite_coordinate_are_rejected(self):
        feed = empty_feed()
        feed["sources"] = [self.source()]
        feed["linksByProperty"][self.a] = self.link(self.a)
        feed["linksByProperty"][self.a]["matchStatus"] = "candidate"
        feed["linksByProperty"][self.a]["longitude"] = math.nan
        failures = uprn_failures(feed, self.canonical)
        self.assertTrue(any("explicit accepted state" in failure for failure in failures))
        self.assertTrue(any("finite GB coordinate" in failure for failure in failures))

    def test_duplicate_uprn_requires_one_consistent_shared_review(self):
        feed = empty_feed()
        feed["sources"] = [self.source()]
        feed["linksByProperty"] = {self.a: self.link(self.a), self.b: self.link(self.b)}
        self.assertTrue(any("multiple properties" in failure for failure in uprn_failures(feed, self.canonical)))
        review = {
            "status": "approved_shared_hierarchy",
            "reviewId": "shared-1",
            "reviewedAt": "2026-08-10T12:00:00Z",
            "relatedPropertyIds": [self.a, self.b],
            "rationale": "Reviewed parent/child address hierarchy",
        }
        for link in feed["linksByProperty"].values():
            link["matchStatus"] = "reviewed_accepted"
            link["evidenceTier"] = "reviewed"
            link["sharedUprnReview"] = dict(review)
        self.assertEqual(uprn_failures(feed, self.canonical), [])
        feed["linksByProperty"][self.b]["sharedUprnReview"]["reviewId"] = "different"
        self.assertTrue(any("multiple properties" in failure for failure in uprn_failures(feed, self.canonical)))


class InspireReviewQueueContractTests(unittest.TestCase):
    def setUp(self):
        self.property_id = "property:1 TEST ROAD GUILDFORD GU1 1AA|GU11AA"
        self.parcel = {
            "inspireId": "100",
            "validFrom": "2020-01-01T00:00:00Z",
            "beginLifespanVersion": "2020-01-01T00:00:00Z",
            "authorities": ["Test Authority"],
            "areaSquareMetres": 100.0,
            "areaSquareFeet": 1076,
            "areaAcres": 0.0247,
            "areaBasis": "planar area of the HMLR INSPIRE index polygon in EPSG:27700",
            "isExactLegalExtent": False,
            "centroid": [-0.495, 51.205],
            "bbox": [-0.5, 51.2, -0.49, 51.21],
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-0.5, 51.2], [-0.49, 51.2], [-0.49, 51.21],
                    [-0.5, 51.21], [-0.5, 51.2],
                ]],
            },
        }
        self.core = {
            "schemaVersion": 1,
            "canonicalIdentityMode": "full-normalised-address-plus-postcode-fail-closed",
            "sourceSnapshot": "hmlr-inspire-2026-08-02",
            "queueSemantics": QUEUE_SEMANTICS,
            "counts": {
                "linksEvaluated": 1,
                "automaticallyAssociatedIndicative": 0,
                "reviewOrRejected": 1,
            },
            "candidatesByProperty": {
                self.property_id: {
                    "propertyId": self.property_id,
                    "linkMatchStatus": "confirmed_address_match",
                    "linkEvidenceTier": "authoritative_address_source",
                    "coordinateSource": "AddressBase Premium",
                    "linkSourceSnapshot": "addressbase-2026-08",
                    "candidateParcelIds": ["100"],
                    "boundaryDistancesMetres": {"100": 1.5},
                    "outcome": "review_required_boundary_proximity",
                    "titleConfirmed": False,
                    "exactUprnIdentityConfirmed": False,
                    "legalBoundaryConfirmed": False,
                }
            },
            "candidateParcelsById": {"100": self.parcel},
        }
        self.parcel_feed = {
            "source": {"sourceSnapshot": "hmlr-inspire-2026-08-02"},
            "associationsByProperty": {},
            "parcelsById": {},
        }

    def finalised(self, core, directory):
        body, release_id = finalise_body(
            core, "2026-08-10T12:00:00Z", "inspire-parcel-review", "2026-08-02"
        )
        path = Path(directory) / "review.js"
        path.write_text(f"window.INSIGHT_INSPIRE_PARCEL_REVIEW_QUEUE = {body};\n")
        return parse_queue(path), release_id

    def test_nonempty_review_queue_passes_exact_schema_and_python_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, _release_id = self.finalised(deepcopy(self.core), directory)
        self.assertEqual(review_queue_failures(queue, self.parcel_feed, {self.property_id}), [])
        schema = json.loads((ROOT / "config/inspire-parcel-review-queue.schema.json").read_text())
        schema_validator()(queue, schema)

    def test_recomputed_valid_digest_does_not_hide_extra_or_private_fields(self):
        schema = json.loads((ROOT / "config/inspire-parcel-review-queue.schema.json").read_text())
        mutations = []
        extra_top = deepcopy(self.core)
        extra_top["debug"] = "must not publish"
        mutations.append(("extra_top", extra_top))
        leaked_uprn = deepcopy(self.core)
        leaked_uprn["candidatesByProperty"][self.property_id]["uprn"] = "100000000001"
        mutations.append(("leaked_uprn", leaked_uprn))
        private_parcel = deepcopy(self.core)
        private_parcel["candidateParcelsById"]["100"]["privateNotes"] = "internal"
        mutations.append(("private_parcel", private_parcel))
        for name, mutated in mutations:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                queue, release_id = self.finalised(mutated, directory)
                self.assertEqual(queue["releaseId"], release_id)
                failures = review_queue_failures(queue, self.parcel_feed, {self.property_id})
                self.assertTrue(failures)
                with self.assertRaises(Exception):
                    schema_validator()(queue, schema)

    def test_candidate_parcel_area_bbox_and_winding_are_reconciled(self):
        malformed = deepcopy(self.core)
        parcel = malformed["candidateParcelsById"]["100"]
        parcel["areaSquareFeet"] = 999
        parcel["bbox"] = [-0.6, 51.2, -0.49, 51.21]
        parcel["geometry"]["coordinates"][0].reverse()
        with tempfile.TemporaryDirectory() as directory:
            queue, _release_id = self.finalised(malformed, directory)
        failures = review_queue_failures(queue, self.parcel_feed, {self.property_id})
        self.assertTrue(any("square-foot conversion" in failure for failure in failures))
        self.assertTrue(any("bbox does not reconcile" in failure for failure in failures))
        self.assertTrue(any("non-canonical winding" in failure for failure in failures))


class SharedContractFixtureTests(unittest.TestCase):
    def test_schema_date_time_checker_does_not_depend_on_optional_rfc3339_package(self):
        self.assertTrue(is_utc_second_timestamp("2026-08-10T12:00:00Z"))
        self.assertFalse(is_utc_second_timestamp("not-a-date"))
        self.assertFalse(is_utc_second_timestamp("2026-02-30T12:00:00Z"))
        self.assertFalse(is_utc_second_timestamp("2026-08-10T12:00:00+00:00"))

    def test_frozen_raw_core_and_valid_invalid_uprn_corpus(self):
        fixture = json.loads((ROOT / "tests/fixtures/inspire-contract-fixtures.json").read_text())
        numeric = fixture["numericRawCoreFixture"]
        body, release_id = finalise_body(
            numeric["core"], "2026-08-10T12:00:00Z", "raw-core-fixture", "2026-08-10"
        )
        self.assertEqual(body, numeric["fullBody"])
        self.assertEqual(release_id, numeric["expectedReleaseId"])
        validate = schema_validator()
        schema = json.loads((ROOT / "config/property-uprn-links.schema.json").read_text())
        valid = fixture["uprnValid"]
        validate(valid["payload"], schema)
        self.assertEqual(uprn_failures(valid["payload"], set(valid["canonicalPropertyIds"])), [])
        for case in fixture["uprnInvalid"]:
            with self.subTest(case=case["name"]):
                with self.assertRaises(Exception):
                    validate(case["payload"], schema)
                self.assertTrue(uprn_failures(case["payload"], set(valid["canonicalPropertyIds"])))


if __name__ == "__main__":
    unittest.main()
