#!/usr/bin/env python3
"""Build shared cross-runtime INSPIRE/UPRN raw-release contract fixtures."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from runtime_release import finalise_body


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "inspire-contract-fixtures.json"


def main() -> int:
    property_id = "property:1 TEST ROAD GUILDFORD GU1 1AA|GU11AA"
    source = {
        "sourceId": "addressbase",
        "name": "AddressBase Premium",
        "sourceSnapshot": "addressbase-2026-08",
        "checkedAt": "2026-08-10T12:00:00Z",
        "coordinateBasis": "authoritative_address_point",
        "licenceOrEntitlement": {
            "type": "commercial_entitlement",
            "reference": "fixture entitlement",
            "permitsPublicDerivedPublication": True,
        },
        "redistributionClassification": "licensed_for_insight_publication",
    }
    link = {
        "propertyId": property_id,
        "sourceId": "addressbase",
        "uprn": "100000000001",
        "matchStatus": "confirmed_address_match",
        "evidenceTier": "authoritative_address_source",
        "longitude": -0.5,
        "latitude": 51.2,
        "coordinateSource": "AddressBase Premium",
        "sourceSnapshot": "addressbase-2026-08",
        "checkedAt": "2026-08-10T12:00:00Z",
        "limitations": [],
    }
    core = {
        "schemaVersion": 1,
        "canonicalIdentityMode": "full-normalised-address-plus-postcode-fail-closed",
        "identityWarning": "UPRN evidence never creates, merges or replaces canonical INSIGHT property identity",
        "sources": [source],
        "linksByProperty": {property_id: link},
    }
    body, release_id = finalise_body(core, "2026-08-10T12:00:00Z", "property-uprn-links", "2026-08-10")
    valid = json.loads(body)
    def refinalise(payload):
        mutated_core = {key: value for key, value in payload.items() if key not in {"generatedAt", "releaseId"}}
        mutated_body, _release = finalise_body(mutated_core, "2026-08-10T12:00:00Z", "property-uprn-links", "2026-08-10")
        return json.loads(mutated_body)

    invalid = []
    missing_source_id = copy.deepcopy(valid)
    del missing_source_id["linksByProperty"][property_id]["sourceId"]
    invalid.append({"name": "missing_link_source_id", "payload": refinalise(missing_source_id)})
    invalid_status = copy.deepcopy(valid)
    invalid_status["linksByProperty"][property_id]["matchStatus"] = "candidate"
    invalid.append({"name": "non_explicit_match_status", "payload": refinalise(invalid_status)})
    invalid_time = copy.deepcopy(valid)
    invalid_time["linksByProperty"][property_id]["checkedAt"] = "not-a-date"
    invalid.append({"name": "invalid_checked_at", "payload": refinalise(invalid_time)})
    invalid_rights = copy.deepcopy(valid)
    invalid_rights["sources"][0]["redistributionClassification"] = "internal_only_no_publication"
    invalid_rights["sources"][0]["licenceOrEntitlement"]["permitsPublicDerivedPublication"] = False
    invalid.append({"name": "non_publishable_source_rights", "payload": refinalise(invalid_rights)})

    numeric_core = {
        "schemaVersion": 1,
        "numericProbe": {
            "smallExponent": 1e-7,
            "areaSquareMetres": 33804.9,
            "coordinate": [-0.39071577, 51.33777448],
        },
    }
    numeric_body, numeric_release = finalise_body(
        numeric_core,
        "2026-08-10T12:00:00Z",
        "raw-core-fixture",
        "2026-08-10",
    )
    fixture = {
        "schemaVersion": 1,
        "rawReleaseRule": "SHA-256 exact minified UTF-8 core JSON bytes; generatedAt then releaseId are appended as the final two top-level members and excluded from the digest",
        "numericRawCoreFixture": {
            "core": numeric_core,
            "fullBody": numeric_body,
            "expectedReleaseId": numeric_release,
        },
        "uprnValid": {
            "canonicalPropertyIds": [property_id],
            "payload": valid,
            "expectedReleaseId": release_id,
        },
        "uprnInvalid": invalid,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Preserve insertion order here: the fixture deliberately proves that the
    # release digest is over the producer's exact bytes, not a re-sorted or
    # re-serialised semantic JSON object.
    OUTPUT.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
