#!/usr/bin/env python3
"""Shared helpers for INSIGHT GitHub data refresh jobs."""

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_JS = ROOT / "outputs" / "surrey-transactions.js"
PROPERTY_ADDRESS_ALIASES_PATH = ROOT / "config" / "property-address-aliases.json"
PROPERTY_ADDRESS_PRESENTATIONS_PATH = ROOT / "config" / "property-address-presentations.json"
POSTCODES_API = "https://api.postcodes.io/postcodes/"
FEED_SCHEMA_VERSION = 3
PROPERTY_RECORD_SCHEMA_VERSION = 1
ADDRESS_CANONICALISATION_VERSION = "structured-delivery-point-v1"
_POSTCODE_AT_END_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\s*$", re.I)
_ADDRESS_PRESENTATION_FIELDS = ("saon", "paon", "street", "locality", "town", "postcode")

# This is the public row contract, not merely a schema hint. New top-level
# fields must be reviewed here before any workflow can publish them.
PUBLIC_TRANSACTION_FIELDS = frozenset({
    "id",
    "propertyRecordId",
    "market",
    "district",
    "address",
    "paon",
    "saon",
    "street",
    "locality",
    "town",
    "postcode",
    "price",
    "priceText",
    "date",
    "propertyType",
    "estateId",
    "estate",
    "estateClassification",
    "estateType",
    "estateRuleId",
    "estateRegistryVersion",
    "estateEvidenceStatus",
    "estateReviewStatus",
    "source",
    "kind",
    "category",
    "epcMatched",
    "floorAreaSqm",
    "floorAreaSqft",
    "pricePerSqft",
    "epcRating",
    "epcRegistrationDate",
    "epcSource",
    "latitude",
    "longitude",
    "coordinateSource",
    "coordinatePrecision",
    "geocode",
    "environmentAgency",
    "ordnanceSurvey",
    "uprn",
    "ofsted",
    "planningConstraints",
    "historicEngland",
})

# These fields existed in the pre-v1.5.1 public feed. The writer removes them
# during migration, while the validator rejects any published recurrence.
RESTRICTED_PUBLIC_TRANSACTION_FIELDS = frozenset({
    "epcAddress",
    "epcCertificateNumber",
    "epcMatchScore",
    "epcSearch",
    "epcSearchDiagnostics",
    "epcMatchDiagnostics",
    "epcHistory",
    "epcSourceAddress",
    "sourceAddress",
    "searchAddress",
    "queryAddress",
    "openStreetMap",
    "companiesHouse",
    "planning",
    "planningHistory",
})


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def normalise_postcode(value):
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def canonical_address(value):
    """Return the fail-closed full-address identity shared by INSIGHT feeds."""

    text = re.sub(r"['\u2018\u2019\u02bc]", "", clean(value).upper())
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def canonical_display_address(item):
    """Return a structured display address without repeated locality noise.

    Historic HMLR rows sometimes repeat the postal town in both ``locality``
    and ``town`` (for example ``WEYBRIDGE, WEYBRIDGE``).  That is a source
    formatting variation, not a distinct delivery point, so it must not enter
    the canonical address or property identity.
    """

    if not isinstance(item, dict):
        return clean(item)
    saon = clean(item.get("saon"))
    paon = clean(item.get("paon"))
    street = clean(item.get("street"))
    locality = clean(item.get("locality"))
    town = clean(item.get("town"))
    postcode = clean(item.get("postcode"))
    if locality and canonical_address(locality) == canonical_address(town):
        locality = ""
    parts = [saon, paon, street, locality, town, postcode]
    if any(parts[:4]):
        return ", ".join(part for part in parts if part)
    return clean(item.get("address"))


def structured_delivery_point_key(item):
    """Return a conservative locality-independent HMLR delivery-point key.

    PAON, SAON, street and postcode identify the structured delivery point.
    Locality and postal-town wording are deliberately excluded because HMLR
    has changed or omitted those descriptive fields between repeat sales.
    Sparse rows fail closed and return an empty key.
    """

    if not isinstance(item, dict):
        return ""
    saon = canonical_address(item.get("saon"))
    paon = canonical_address(item.get("paon"))
    street = canonical_address(item.get("street"))
    route = street or canonical_address(item.get("locality"))
    postcode = normalise_postcode(item.get("postcode"))
    if not paon or not route or not postcode:
        return ""
    return "|".join((saon, paon, route, postcode))


def numbered_delivery_point_key(item):
    """Return a guarded house-number alias key for named/unnamed PAON drift.

    The complete numeric signature is retained.  Thus ``TOROSA, 30`` and
    ``30`` can align, while ``PLOT 1, 12`` cannot align with bare ``12``.
    """

    if not isinstance(item, dict):
        return ""
    paon = canonical_address(item.get("paon"))
    numeric_signature = tuple(
        re.findall(r"(?<![A-Z0-9])\d+[A-Z]?(?![A-Z0-9])", paon)
    )
    saon = canonical_address(item.get("saon"))
    street = canonical_address(item.get("street"))
    route = street or canonical_address(item.get("locality"))
    postcode = normalise_postcode(item.get("postcode"))
    if not numeric_signature or not route or not postcode:
        return ""
    return "|".join((saon, ",".join(numeric_signature), route, postcode))


def reviewed_property_address_aliases(path=PROPERTY_ADDRESS_ALIASES_PATH):
    """Load and strictly validate the small reviewed cross-identity registry."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("groups"), list):
        raise ValueError("Property-address alias registry is missing schemaVersion 1 groups")
    seen_members = set()
    for group in payload["groups"]:
        members = group.get("members") if isinstance(group, dict) else None
        canonical = group.get("canonicalPropertyId") if isinstance(group, dict) else None
        if (
            not isinstance(members, list)
            or len(members) < 2
            or not all(isinstance(value, str) and value.startswith("property:") for value in members)
            or canonical not in members
        ):
            raise ValueError("Property-address alias group is malformed")
        overlap = seen_members.intersection(members)
        if overlap:
            raise ValueError("Property-address alias member occurs in multiple groups: " + ", ".join(sorted(overlap)))
        seen_members.update(members)
    return payload


def reviewed_alias_postcodes_by_canonical_property(path=PROPERTY_ADDRESS_ALIASES_PATH):
    """Return every reviewed historic postcode needed for a full history lookup."""

    payload = reviewed_property_address_aliases(path)
    result = {}
    for group in payload["groups"]:
        postcodes = {
            normalise_postcode(member.rpartition("|")[2])
            for member in group["members"]
            if member.rpartition("|")[1] and member.rpartition("|")[2] != "NOPOSTCODE"
        }
        result[group["canonicalPropertyId"]] = tuple(sorted(postcodes))
    return result


def reviewed_property_address_presentations(path=PROPERTY_ADDRESS_PRESENTATIONS_PATH):
    """Load fail-closed owner-reviewed address-presentation overrides.

    These overrides are deliberately separate from the cross-identity alias
    registry. They may add a reviewed house name to one already identified
    delivery point, but cannot alter its unit, route, locality, town or
    postcode. Both configured property IDs are derived from the structured
    fields so a typo cannot silently rekey another property.
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        set(payload) != {"schemaVersion", "version", "presentations"}
        or payload.get("schemaVersion") != 1
        or not isinstance(payload.get("version"), str)
        or not clean(payload.get("version"))
        or not isinstance(payload.get("presentations"), list)
    ):
        raise ValueError(
            "Property-address presentation registry is missing schemaVersion 1 presentations"
        )

    seen_ids = set()
    seen_property_ids = set()
    for presentation in payload["presentations"]:
        if not isinstance(presentation, dict) or set(presentation) != {
            "id",
            "sourcePropertyId",
            "canonicalPropertyId",
            "sourceFields",
            "canonicalFields",
            "review",
        }:
            raise ValueError("Property-address presentation entry is malformed")
        presentation_id = presentation["id"]
        if (
            not isinstance(presentation_id, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", presentation_id)
            or presentation_id in seen_ids
        ):
            raise ValueError("Property-address presentation ID is malformed or duplicated")
        seen_ids.add(presentation_id)

        source_fields = presentation["sourceFields"]
        canonical_fields = presentation["canonicalFields"]
        for label, fields in (
            ("sourceFields", source_fields),
            ("canonicalFields", canonical_fields),
        ):
            if (
                not isinstance(fields, dict)
                or set(fields) != set(_ADDRESS_PRESENTATION_FIELDS)
                or not all(
                    isinstance(fields[field], str)
                    and fields[field] == clean(fields[field])
                    for field in _ADDRESS_PRESENTATION_FIELDS
                )
                or not all(fields[field] for field in ("paon", "street", "town", "postcode"))
            ):
                raise ValueError(f"Property-address presentation {label} is malformed")

        source_property_id = presentation["sourcePropertyId"]
        canonical_property_id = presentation["canonicalPropertyId"]
        expected_source_id = property_record_id({
            **source_fields,
            "address": canonical_display_address(source_fields).upper(),
        })
        expected_canonical_id = property_record_id({
            **canonical_fields,
            "address": canonical_display_address(canonical_fields).upper(),
        })
        if (
            source_property_id != expected_source_id
            or canonical_property_id != expected_canonical_id
            or source_property_id == canonical_property_id
            or source_property_id in seen_property_ids
            or canonical_property_id in seen_property_ids
        ):
            raise ValueError(
                "Property-address presentation property IDs do not match unique configured fields"
            )
        seen_property_ids.update((source_property_id, canonical_property_id))

        unchanged_fields = set(_ADDRESS_PRESENTATION_FIELDS) - {"paon"}
        if any(
            canonical_address(source_fields[field])
            != canonical_address(canonical_fields[field])
            for field in unchanged_fields
        ):
            raise ValueError(
                "Property-address presentation may only add a reviewed name to PAON"
            )
        source_paon = clean(source_fields["paon"]).upper()
        canonical_paon = clean(canonical_fields["paon"]).upper()
        if (
            not re.fullmatch(r"\d+[A-Z]?(?:-\d+[A-Z]?)?", source_paon)
            or not canonical_paon.endswith(", " + source_paon)
            or not clean(canonical_paon[: -(len(source_paon) + 2)])
        ):
            raise ValueError(
                "Property-address presentation must add a house name before the unchanged number"
            )

        review = presentation["review"]
        if (
            not isinstance(review, dict)
            or set(review) != {"status", "date", "basis", "scope"}
            or review.get("status") != "owner-reviewed"
            or not isinstance(review.get("date"), str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review["date"])
            or not all(
                isinstance(review.get(field), str) and clean(review[field])
                for field in ("basis", "scope")
            )
        ):
            raise ValueError(
                "Property-address presentation must contain an owner-reviewed decision"
            )
    return payload


def apply_reviewed_property_address_presentations(rows, registry):
    """Apply validated single-property presentations and return audit stats."""

    def normalised_field(field, value):
        if field == "postcode":
            return normalise_postcode(value)
        return canonical_address(value)

    matched_properties = 0
    matched_rows = 0
    rekeyed_properties = 0
    rewritten_rows = 0
    for presentation in registry["presentations"]:
        source_property_id = presentation["sourcePropertyId"]
        canonical_property_id = presentation["canonicalPropertyId"]
        indexes_by_id = {source_property_id: [], canonical_property_id: []}
        for index, item in enumerate(rows):
            current_id = property_record_id(item)
            if current_id in indexes_by_id:
                indexes_by_id[current_id].append(index)
        indexes = indexes_by_id[source_property_id] + indexes_by_id[canonical_property_id]
        if not indexes:
            continue

        matched_properties += 1
        matched_rows += len(indexes)
        if indexes_by_id[source_property_id]:
            rekeyed_properties += 1
        for current_id, current_indexes in indexes_by_id.items():
            expected_fields = (
                presentation["sourceFields"]
                if current_id == source_property_id
                else presentation["canonicalFields"]
            )
            for index in current_indexes:
                mismatches = [
                    field
                    for field in _ADDRESS_PRESENTATION_FIELDS
                    if normalised_field(field, rows[index].get(field))
                    != normalised_field(field, expected_fields[field])
                ]
                if mismatches:
                    raise ValueError(
                        f"Property-address presentation {presentation['id']} target fields "
                        "do not match: " + ", ".join(mismatches)
                    )

        canonical_fields = presentation["canonicalFields"]
        for index in indexes:
            before = tuple(
                clean(rows[index].get(field))
                for field in ("address", *_ADDRESS_PRESENTATION_FIELDS)
            )
            rows[index].update(canonical_fields)
            rows[index]["address"] = canonical_display_address(rows[index]).upper()
            if property_record_id(rows[index]) != canonical_property_id:
                raise ValueError(
                    f"Property-address presentation {presentation['id']} produced an unexpected property ID"
                )
            after = tuple(
                clean(rows[index].get(field))
                for field in ("address", *_ADDRESS_PRESENTATION_FIELDS)
            )
            rewritten_rows += before != after

    return {
        "reviewedPresentationRegistryVersion": registry["version"],
        "reviewedPresentationEntries": len(registry["presentations"]),
        "reviewedPresentationProperties": matched_properties,
        "reviewedPresentationRows": matched_rows,
        "reviewedPresentationPropertiesRekeyed": rekeyed_properties,
        "reviewedPresentationRowsRewritten": rewritten_rows,
    }


def canonicalise_property_addresses(transactions):
    """Canonicalise address variants across a complete transaction snapshot.

    Exact structured delivery points, guarded named/numbered PAON variants and
    the small evidence-reviewed alias registry are considered.  The most
    informative address variant is selected deterministically and applied to
    every repeat sale.  This keeps full canonical addresses while preventing
    administrative wording changes and verified renames from creating phantom
    properties.
    """

    rows = [dict(item) for item in transactions]
    original_rows = [dict(item) for item in rows]
    legacy_property_id_by_index = [
        clean(item.get("propertyRecordId"))
        if clean(item.get("propertyRecordId")).startswith("property:")
        else (
            f"property:{canonical_address(item.get('address'))}|{normalise_postcode(item.get('postcode')) or 'NOPOSTCODE'}"
            if canonical_address(item.get("address")) else ""
        )
        for item in rows
    ]
    legacy_property_ids = {value for value in legacy_property_id_by_index if value}
    redundant_localities = sum(
        bool(canonical_address(item.get("locality")))
        and canonical_address(item.get("locality")) == canonical_address(item.get("town"))
        for item in rows
    )
    groups = {}
    for index, item in enumerate(rows):
        key = structured_delivery_point_key(item)
        if key:
            groups.setdefault(key, []).append(index)
    structured_alias_groups = sum(
        len({legacy_property_id_by_index[index] for index in indexes if legacy_property_id_by_index[index]}) > 1
        for indexes in groups.values()
    )
    structured_aliases_collapsed = sum(
        max(
            0,
            len({legacy_property_id_by_index[index] for index in indexes if legacy_property_id_by_index[index]}) - 1,
        )
        for indexes in groups.values()
    )

    def donor_rank(index):
        candidate = rows[index]
        locality = clean(candidate.get("locality"))
        town = clean(candidate.get("town"))
        informative_locality = bool(
            locality and canonical_address(locality) != canonical_address(town)
        )
        paon_tokens = canonical_address(candidate.get("paon")).split()
        named_paon = any(
            not re.fullmatch(r"\d+[A-Z]?", token)
            for token in paon_tokens
        )
        return (
            named_paon,
            informative_locality,
            bool(town),
            clean(candidate.get("date"))[:10],
            canonical_display_address(candidate),
        )

    def apply_group(indexes):
        donor = rows[max(indexes, key=donor_rank)]
        canonical_fields = {
            key: clean(donor.get(key))
            for key in ("saon", "paon", "street", "locality", "town", "postcode")
        }
        if (
            canonical_fields["locality"]
            and canonical_address(canonical_fields["locality"])
            == canonical_address(canonical_fields["town"])
        ):
            canonical_fields["locality"] = ""
        for index in indexes:
            rows[index].update(canonical_fields)
            rows[index]["address"] = canonical_display_address(rows[index]).upper()

    for indexes in groups.values():
        apply_group(indexes)

    numbered_groups = {}
    for index, item in enumerate(rows):
        key = numbered_delivery_point_key(item)
        if key:
            numbered_groups.setdefault(key, []).append(index)
    applied_numbered_groups = 0
    for indexes in numbered_groups.values():
        property_ids = {property_record_id(rows[index]) for index in indexes}
        if len(property_ids) < 2:
            continue
        # Fail closed on contradictory source classifications or two different
        # sale facts on the same date.  These conditions suggest separate units,
        # redevelopment plots or a source error rather than a harmless alias.
        conflicts = any(
            len({clean(rows[index].get(field)).upper() for index in indexes if clean(rows[index].get(field))}) > 1
            for field in ("district", "market", "propertyType", "estateId")
        )
        sale_dates = {}
        for index in indexes:
            date = clean(rows[index].get("date"))[:10]
            fact = (
                rows[index].get("price"),
                clean(rows[index].get("propertyType")).upper(),
                clean(rows[index].get("category")).upper(),
            )
            sale_dates.setdefault(date, set()).add(fact)
        if conflicts or any(len(facts) > 1 for facts in sale_dates.values()):
            continue
        apply_group(indexes)
        applied_numbered_groups += 1

    alias_registry = reviewed_property_address_aliases()
    applied_reviewed_groups = 0
    for group in alias_registry["groups"]:
        members = set(group["members"])
        indexes = [
            index for index, legacy_id in enumerate(legacy_property_id_by_index)
            if legacy_id in members
        ]
        present_members = {legacy_property_id_by_index[index] for index in indexes}
        canonical_indexes = [
            index for index in indexes
            if legacy_property_id_by_index[index] == group["canonicalPropertyId"]
        ]
        if len(present_members) < 2 or not canonical_indexes:
            continue
        canonical_index = max(canonical_indexes, key=donor_rank)
        canonical_fields = {
            key: clean(rows[canonical_index].get(key))
            for key in ("saon", "paon", "street", "locality", "town", "postcode")
        }
        if (
            canonical_fields["locality"]
            and canonical_address(canonical_fields["locality"])
            == canonical_address(canonical_fields["town"])
        ):
            canonical_fields["locality"] = ""
        for index in indexes:
            rows[index].update(canonical_fields)
            rows[index]["address"] = canonical_display_address(rows[index]).upper()
        applied_reviewed_groups += 1

    presentation_registry = reviewed_property_address_presentations()
    presentation_stats = apply_reviewed_property_address_presentations(
        rows,
        presentation_registry,
    )

    # Structured canonicalisation cannot safely infer identity for sparse rows,
    # but it can still remove an exact locality/town repetition from display.
    grouped_indexes = {index for indexes in groups.values() for index in indexes}
    for index, item in enumerate(rows):
        if index in grouped_indexes:
            continue
        locality = clean(item.get("locality"))
        town = clean(item.get("town"))
        if locality and canonical_address(locality) == canonical_address(town):
            item["locality"] = ""
            item["address"] = canonical_display_address(item).upper()

    compared_fields = ("address", "saon", "paon", "street", "locality", "town", "postcode")
    changed_rows = sum(
        tuple(clean(before.get(key)) for key in compared_fields)
        != tuple(clean(after.get(key)) for key in compared_fields)
        for before, after in zip(original_rows, rows)
    )

    canonical_property_ids = {
        property_record_id(item)
        for item in rows
        if property_record_id(item)
    }
    source_variant_candidates = {}
    for index, (original, canonical) in enumerate(zip(original_rows, rows)):
        canonical_property_id = property_record_id(canonical)
        legacy_property_id = legacy_property_id_by_index[index]
        source_address = (
            clean(original.get("address")) or canonical_display_address(original)
        ).upper()
        source_postcode = normalise_postcode(original.get("postcode"))
        if (
            canonical_property_id
            and legacy_property_id
            and canonical_address(source_address)
        ):
            candidate = {
                "propertyRecordId": legacy_property_id,
                "address": source_address,
                "postcode": source_postcode,
                "_rank": (
                    legacy_property_id == canonical_property_id,
                    clean(original.get("date"))[:10],
                    canonical_address(source_address),
                ),
            }
            variants = source_variant_candidates.setdefault(canonical_property_id, {})
            prior = variants.get(legacy_property_id)
            if prior is None or candidate["_rank"] > prior["_rank"]:
                variants[legacy_property_id] = candidate

    # Retain every source identity which no longer exists in the final
    # canonical set. This is the complete retired-ID mapping, not merely the
    # net reduction in property count: a rewrite can replace one legacy ID
    # with one new canonical ID without changing the total, but planning
    # history still needs the retired address as lookup evidence.
    source_address_variants = {}
    for canonical_property_id, variants in sorted(source_variant_candidates.items()):
        aliases = []
        for legacy_property_id in sorted(variants):
            if legacy_property_id == canonical_property_id:
                continue
            aliases.append({
                key: value
                for key, value in variants[legacy_property_id].items()
                if key != "_rank"
            })
        if aliases:
            source_address_variants[canonical_property_id] = aliases
    source_address_variant_count = sum(
        len(variants) for variants in source_address_variants.values()
    )
    stats = {
        "version": ADDRESS_CANONICALISATION_VERSION,
        "identityKey": "normalised-saon-paon-street-or-locality-postcode",
        "rows": len(rows),
        "rowsCanonicalised": changed_rows,
        "redundantLocalitiesRemoved": redundant_localities,
        "structuredPropertyGroups": len(groups),
        "structuredAliasGroups": structured_alias_groups,
        "structuredAliasesCollapsed": structured_aliases_collapsed,
        "numberedAliasGroups": applied_numbered_groups,
        "reviewedAliasRegistryVersion": alias_registry["version"],
        "reviewedAliasGroups": applied_reviewed_groups,
        **presentation_stats,
        "sourceAddressIdentities": len(legacy_property_ids),
        "canonicalProperties": len(canonical_property_ids),
        "identityAliasesCollapsed": max(0, len(legacy_property_ids) - len(canonical_property_ids)),
        "sourceAddressVariantProperties": len(source_address_variants),
        "sourceAddressVariantCount": source_address_variant_count,
        "sourceAddressVariants": source_address_variants,
    }
    return rows, stats


def property_record_id(item):
    """Return the canonical property id without trusting approximate UPRNs."""

    if isinstance(item, dict):
        # Identity is the producer-canonicalised full display address.  The
        # batch canonicaliser rewrites that field before publication; keeping
        # this helper address-led also makes independent validation catch a
        # stale or contradictory structured projection.
        address = canonical_address(item.get("address"))
        if not address:
            address = canonical_address(canonical_display_address(item))
        postcode = normalise_postcode(item.get("postcode"))
        if not postcode:
            match = _POSTCODE_AT_END_RE.search(clean(item.get("address")).upper())
            if match:
                postcode = normalise_postcode("".join(match.groups()))
        if not address and not postcode:
            return clean(item.get("propertyRecordId"))
    else:
        address = canonical_address(item)
        postcode = ""
    return f"property:{address}|{postcode or 'NOPOSTCODE'}" if address else ""


def public_transaction(item):
    """Return one fail-closed public row, removing only known legacy leakage."""

    fields = set(item)
    unknown = fields - PUBLIC_TRANSACTION_FIELDS - RESTRICTED_PUBLIC_TRANSACTION_FIELDS
    if unknown:
        raise ValueError(
            "Unreviewed public transaction fields: " + ", ".join(sorted(unknown))
        )
    output = {key: value for key, value in item.items() if key in PUBLIC_TRANSACTION_FIELDS}
    schools = output.get("ofsted")
    if isinstance(schools, dict) and schools.get("source") in {
        "DfE / Ofsted school data",
        "Get Information About Schools Surrey extract",
    }:
        output["ofsted"] = {
            **schools,
            "source": "DfE Get Information about Schools (GIAS)",
        }
    return output


def publication_contract_failures(transactions):
    """Describe public-row contract violations without mutating the feed."""

    failures = []
    restricted = sorted({
        key
        for item in transactions
        for key in item
        if key in RESTRICTED_PUBLIC_TRANSACTION_FIELDS
    })
    if restricted:
        failures.append(
            "Publication contract: restricted fields are present: " + ", ".join(restricted)
        )
    unknown = sorted({
        key
        for item in transactions
        for key in item
        if key not in PUBLIC_TRANSACTION_FIELDS
        and key not in RESTRICTED_PUBLIC_TRANSACTION_FIELDS
    })
    if unknown:
        failures.append(
            "Publication contract: unreviewed fields are present: " + ", ".join(unknown)
        )
    return failures


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def planning_constraint_lookup_succeeded(item):
    """Return whether a row carries explicit evidence of a completed lookup."""

    context = item.get("planningConstraints") if isinstance(item, dict) else None
    if not isinstance(context, dict):
        return False
    return clean(context.get("lookupStatus")).lower() in {"success", "successful"}


def planning_constraint_has_positive_result(item):
    """Return whether the static lookup found one or more mapped constraints."""

    context = item.get("planningConstraints") if isinstance(item, dict) else None
    if not isinstance(context, dict):
        return False
    count = parse_float(context.get("constraintCount"))
    return count is not None and count > 0


def planning_constraint_coverage_counts(transactions):
    """Reconcile static Planning Data lookup outcomes to transaction rows."""

    successful = sum(1 for item in transactions if planning_constraint_lookup_succeeded(item))
    positive = sum(1 for item in transactions if planning_constraint_has_positive_result(item))
    return {
        "successfulResponses": successful,
        "positiveRecords": positive,
        "missingResponses": len(transactions) - successful,
    }


def numeric(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def parse_window_json(text, name, default):
    prefix = f"window.{name} = "
    for line in text.splitlines():
        if line.startswith(prefix) and line.endswith(";"):
            return json.loads(line[len(prefix) : -1])
    return default


def read_js(path=DEFAULT_INPUT_JS):
    text = Path(path).read_text(encoding="utf-8")
    return (
        parse_window_json(text, "SURREY_LAND_REG_TRANSACTIONS", []),
        parse_window_json(text, "SURREY_LAND_REG_SUMMARY", {}),
        parse_window_json(text, "SURREY_LAND_REG_META", {}),
    )


def summary_by_market(transactions):
    grouped = {}
    for item in transactions:
        grouped.setdefault(item.get("market", ""), []).append(item)
    summary = {}
    for market, items in grouped.items():
        if not market or not items:
            continue
        ppsf_values = [item.get("pricePerSqft") for item in items if numeric(item.get("pricePerSqft"))]
        summary[market] = {
            "count": len(items),
            "avg": round(sum(item["price"] for item in items) / len(items)),
            "latest": max(item["date"] for item in items),
            "max": max(item["price"] for item in items),
        }
        if ppsf_values:
            summary[market]["avgPricePerSqft"] = round(sum(ppsf_values) / len(ppsf_values))
            summary[market]["epcMatched"] = len(ppsf_values)
    return summary


def recompute_coverage_metadata(transactions, meta):
    """Replay row-level totals so schema migration cannot leave stale metadata."""

    meta = dict(meta)

    def populated(key):
        return sum(1 for item in transactions if item.get(key) not in (None, "", [], {}))

    epc = dict(meta.get("epcEnrichment") or {})
    if epc:
        matched = sum(1 for item in transactions if item.get("epcMatched") is True and numeric(item.get("floorAreaSqft")))
        epc["matched"] = matched
        epc["coveragePercent"] = round(matched * 100 / len(transactions), 1) if transactions else 0
        meta["epcEnrichment"] = epc

    context = dict(meta.get("propertyContext") or {})
    if context:
        postcodes = dict(context.get("postcodes") or {})
        if postcodes:
            postcodes["matched"] = populated("geocode")
            context["postcodes"] = postcodes
        environment = dict(context.get("environmentAgency") or {})
        if environment:
            environment["records"] = populated("environmentAgency")
            context["environmentAgency"] = environment
        context.pop("openStreetMap", None)
        meta["propertyContext"] = context

    os_refresh = dict(meta.get("osRefresh") or {})
    if os_refresh:
        os_refresh["uprnMatches"] = populated("ordnanceSurvey")
        meta["osRefresh"] = os_refresh

    weekly = dict(meta.get("weeklyContext") or {})
    if weekly:
        constraints = dict(weekly.get("planningConstraints") or {})
        if constraints:
            coverage = planning_constraint_coverage_counts(transactions)
            constraints.update(coverage)
            constraints["records"] = coverage["successfulResponses"]
            constraints["coverageMode"] = "explicit-per-row-success"
            weekly["planningConstraints"] = constraints
        # Statutory listed-building evidence is owned exclusively by the
        # dedicated Historic England sync and its top-level heritageSync
        # metadata. Never revive the legacy Planning Data weekly summary while
        # migrating a downloaded feed.
        weekly.pop("historicEngland", None)
        schools = dict(weekly.get("schools") or {})
        if schools:
            schools["records"] = populated("ofsted")
            schools["source"] = "DfE Get Information about Schools (GIAS)"
            weekly["schools"] = schools
        meta["weeklyContext"] = weekly

    daily = dict(meta.get("dailyIntelligence") or {})
    if daily:
        daily.pop("planning", None)
        daily.pop("companiesHouse", None)
        if any(key != "updatedAt" for key in daily):
            meta["dailyIntelligence"] = daily
        else:
            meta.pop("dailyIntelligence", None)
    return meta


def finalise_historical_expansion(meta, *, final_pass_complete):
    """Preserve the expansion cohort and close its pending count fail-safely.

    The Land Registry sweep records how many rows initially had no reusable
    enrichment. Later enrichers must not erase that audit value when the last
    full enrichment pass reduces the number still pending to zero.
    """

    metadata = dict(meta)
    historical = metadata.get("historicalExpansion")
    if not isinstance(historical, dict):
        return metadata

    historical = dict(historical)
    initial = historical.get("newTransactionsAtExpansion")
    pending = historical.get("newTransactionsPendingEnrichment")
    if type(initial) is not int or initial < 0:
        if type(pending) is int and pending >= 0:
            initial = pending
        elif final_pass_complete:
            raise ValueError(
                "Historical expansion metadata has no valid initial cohort count"
            )

    normalised = {
        key: value
        for key, value in historical.items()
        if key not in {
            "newTransactionsAtExpansion",
            "newTransactionsPendingEnrichment",
        }
    }
    if type(initial) is int and initial >= 0:
        normalised["newTransactionsAtExpansion"] = initial
    if final_pass_complete:
        normalised["newTransactionsPendingEnrichment"] = 0
    elif "newTransactionsPendingEnrichment" in historical:
        normalised["newTransactionsPendingEnrichment"] = pending
    metadata["historicalExpansion"] = normalised
    return metadata


def valid_address_canonicalisation_ledger(candidate):
    return (
        isinstance(candidate, dict)
        and candidate.get("version") == ADDRESS_CANONICALISATION_VERSION
        and isinstance(candidate.get("canonicalProperties"), int)
        and isinstance(candidate.get("sourceAddressIdentities"), int)
        and candidate["sourceAddressIdentities"] >= candidate["canonicalProperties"]
        and candidate.get("identityAliasesCollapsed")
        == candidate["sourceAddressIdentities"] - candidate["canonicalProperties"]
        and isinstance(candidate.get("sourceAddressVariants"), dict)
        and all(
            isinstance(variants, list)
            for variants in candidate["sourceAddressVariants"].values()
        )
        and candidate.get("sourceAddressVariantProperties")
        == len(candidate["sourceAddressVariants"])
        and candidate.get("sourceAddressVariantCount")
        == sum(len(variants) for variants in candidate["sourceAddressVariants"].values())
    )


def valid_address_canonicalisation_stats(candidate, row_count, canonical_property_count):
    return (
        valid_address_canonicalisation_ledger(candidate)
        and candidate.get("rows") == row_count
        and candidate.get("canonicalProperties") == canonical_property_count
    )


def merge_address_canonicalisation_stats(computed, candidates, transactions):
    current_ids = {item["propertyRecordId"] for item in transactions}
    row_count = len(transactions)
    canonical_count = len(current_ids)
    all_candidates = [computed, *candidates]
    current_candidates = [
        candidate
        for candidate in all_candidates
        if valid_address_canonicalisation_stats(
            candidate,
            row_count,
            canonical_count,
        )
    ]
    base = dict(max(
        current_candidates,
        default=computed,
        key=lambda candidate: (
            candidate.get("sourceAddressIdentities", 0),
            candidate.get("sourceAddressVariantCount", 0),
        ),
    ))
    presentation_version = computed.get("reviewedPresentationRegistryVersion")
    presentation_candidates = [
        candidate
        for candidate in all_candidates
        if valid_address_canonicalisation_ledger(candidate)
        and candidate.get("reviewedPresentationRegistryVersion") == presentation_version
    ]
    base.update({
        "reviewedPresentationRegistryVersion": presentation_version,
        "reviewedPresentationEntries": computed.get("reviewedPresentationEntries", 0),
        **{
            key: max(
                (candidate.get(key, 0) for candidate in presentation_candidates),
                default=0,
            )
            for key in (
                "reviewedPresentationProperties",
                "reviewedPresentationRows",
                "reviewedPresentationPropertiesRekeyed",
                "reviewedPresentationRowsRewritten",
            )
        },
    })
    merged = {}
    legacy_targets = {}
    collapsed = int(base.get("identityAliasesCollapsed") or 0)
    current_redirects = {}
    if valid_address_canonicalisation_ledger(computed):
        for canonical_id, variants in computed["sourceAddressVariants"].items():
            if canonical_id not in current_ids:
                continue
            for variant in variants:
                legacy_id = clean(variant.get("propertyRecordId"))
                if legacy_id.startswith("property:") and legacy_id not in current_ids:
                    current_redirects[legacy_id] = canonical_id
    for candidate in all_candidates:
        if not valid_address_canonicalisation_ledger(candidate):
            continue
        candidate_variants = candidate["sourceAddressVariants"]
        if set(candidate_variants).issubset(current_ids):
            collapsed = max(
                collapsed,
                int(candidate.get("identityAliasesCollapsed") or 0),
            )
        for candidate_canonical_id, variants in candidate_variants.items():
            canonical_id = (
                candidate_canonical_id
                if candidate_canonical_id in current_ids
                else current_redirects.get(candidate_canonical_id)
            )
            if canonical_id not in current_ids:
                continue
            target_variants = merged.setdefault(canonical_id, {})
            for variant in variants:
                if not isinstance(variant, dict):
                    raise ValueError("Address canonicalisation variant must be an object")
                legacy_id = clean(variant.get("propertyRecordId"))
                if (
                    not legacy_id.startswith("property:")
                    or legacy_id in current_ids
                    or legacy_id == canonical_id
                ):
                    raise ValueError("Address canonicalisation legacy ID is active or malformed")
                prior_target = legacy_targets.setdefault(legacy_id, canonical_id)
                if prior_target != canonical_id:
                    raise ValueError("Address canonicalisation legacy ID maps to multiple properties")
                target_variants.setdefault(legacy_id, dict(variant))
    merged_variants = {
        canonical_id: [variants[key] for key in sorted(variants)]
        for canonical_id, variants in sorted(merged.items())
        if variants
    }
    base.update({
        "rows": row_count,
        "canonicalProperties": canonical_count,
        "sourceAddressIdentities": canonical_count + collapsed,
        "identityAliasesCollapsed": collapsed,
        "sourceAddressVariantProperties": len(merged_variants),
        "sourceAddressVariantCount": sum(
            len(variants) for variants in merged_variants.values()
        ),
        "sourceAddressVariants": merged_variants,
    })
    return base


def write_js(path, transactions, meta, address_stats=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    transactions, computed_address_stats = canonicalise_property_addresses(transactions)
    canonical_transactions = []
    for item in transactions:
        output = dict(item)
        output["propertyRecordId"] = property_record_id(output)
        canonical_transactions.append(public_transaction(output))
    meta = recompute_coverage_metadata(canonical_transactions, meta)
    meta["schemaVersion"] = FEED_SCHEMA_VERSION
    meta["propertyRecordSchemaVersion"] = PROPERTY_RECORD_SCHEMA_VERSION
    meta["canonicalPropertyRecords"] = len({item["propertyRecordId"] for item in canonical_transactions})
    meta["propertyIdentityMode"] = "full-normalised-address-plus-postcode-fail-closed"
    meta["addressCanonicalisation"] = merge_address_canonicalisation_stats(
        computed_address_stats,
        (address_stats, meta.get("addressCanonicalisation")),
        canonical_transactions,
    )
    content = "\n".join(
        [
            "window.SURREY_LAND_REG_TRANSACTIONS = " + json.dumps(canonical_transactions, separators=(",", ":")) + ";",
            "window.SURREY_LAND_REG_SUMMARY = " + json.dumps(summary_by_market(canonical_transactions), separators=(",", ":")) + ";",
            "window.SURREY_LAND_REG_META = " + json.dumps(meta, separators=(",", ":")) + ";",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def load_cache(path, version):
    path = Path(path)
    if not path.exists():
        return {"version": version}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": version}
    if payload.get("version") != version:
        return {"version": version}
    return payload


def write_cache(path, cache, version):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache["version"] = version
    cache["updatedAt"] = utc_now()
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cache_fresh(record, refresh_seconds):
    if not record or refresh_seconds <= 0:
        return False
    try:
        updated = datetime.fromisoformat(record.get("updatedAt", "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - updated).total_seconds() < refresh_seconds


def request_json(url, *, params=None, method="GET", data=None, timeout=20, retries=2, headers=None, user_agent=None):
    if params:
        separator = "&" if "?" in url else "?"
        url = url + separator + urllib.parse.urlencode(params, doseq=True)
    body = data.encode("utf-8") if isinstance(data, str) else data
    request_headers = {
        "Accept": "application/json",
        "User-Agent": user_agent or "INSIGHT Surrey data refresh",
    }
    request_headers.update(headers or {})
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"status": 404, "items": [], "result": None}
            if exc.code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                wait = parse_float(exc.headers.get("Retry-After")) or min(120, 4 * (attempt + 1) ** 2)
                print(f"API returned HTTP {exc.code}; waiting {wait:.0f}s before retry {attempt + 1}/{retries}.", flush=True)
                time.sleep(wait)
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:260]}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(str(exc)) from exc
    return {}


def coordinates_from_item(item):
    lon = parse_float(item.get("longitude") or item.get("lon"))
    lat = parse_float(item.get("latitude") or item.get("lat"))
    for container_key in ("geocode", "location"):
        container = item.get(container_key)
        if isinstance(container, dict):
            lon = lon if lon is not None else parse_float(container.get("longitude") or container.get("lon"))
            lat = lat if lat is not None else parse_float(container.get("latitude") or container.get("lat"))
    if lat is None or lon is None:
        return None, None
    return lat, lon


def postcode_lookup(postcode, cache, *, refresh_days=365, timeout=15, retries=1):
    key = normalise_postcode(postcode)
    if not key:
        return None
    store = cache.setdefault("postcodes", {})
    cached = store.get(key)
    if cache_fresh(cached, refresh_days * 24 * 60 * 60):
        return cached.get("data")
    payload = request_json(
        POSTCODES_API + urllib.parse.quote(clean(postcode)),
        timeout=timeout,
        retries=retries,
        user_agent="INSIGHT postcode geocoding",
    )
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        store[key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    lon = parse_float(result.get("longitude"))
    lat = parse_float(result.get("latitude"))
    if lon is None or lat is None:
        store[key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    data = {
        "longitude": round(lon, 7),
        "latitude": round(lat, 7),
        "coordinateSource": "Postcodes.io",
        "coordinatePrecision": "postcode-centroid",
        "geocode": {
            "source": "Postcodes.io",
            "precision": "Postcode centroid",
            "postcodeDistrict": clean(result.get("outcode")),
            "adminDistrict": clean(result.get("admin_district")),
            "region": clean(result.get("region")),
            "country": clean(result.get("country")),
        },
    }
    store[key] = {"status": "matched", "updatedAt": utc_now(), "data": data}
    return data


def ensure_coordinates(item, cache, args):
    lat, lon = coordinates_from_item(item)
    if lat is not None and lon is not None:
        return lat, lon, {}
    data = postcode_lookup(
        item.get("postcode"),
        cache,
        refresh_days=getattr(args, "geocode_refresh_days", 365),
        timeout=getattr(args, "timeout", 20),
        retries=getattr(args, "retries", 1),
    )
    if not data:
        return None, None, {}
    return data["latitude"], data["longitude"], data


def haversine_metres(lat1, lon1, lat2, lon2):
    radius = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_distance(metres):
    if metres is None:
        return ""
    if metres < 950:
        return f"{round(metres / 10) * 10:.0f}m"
    return f"{metres / 1000:.1f}km"


def approx_walk_time(metres):
    if metres is None:
        return ""
    minutes = max(1, round((metres * 1.25) / 80))
    return f"c. {minutes} min"


def wkt_square(lat, lon, radius_m):
    lat_delta = radius_m / 111_320
    lon_delta = radius_m / (111_320 * max(0.2, math.cos(math.radians(lat))))
    west = lon - lon_delta
    east = lon + lon_delta
    south = lat - lat_delta
    north = lat + lat_delta
    return f"POLYGON(({west:.7f} {south:.7f},{east:.7f} {south:.7f},{east:.7f} {north:.7f},{west:.7f} {north:.7f},{west:.7f} {south:.7f}))"
