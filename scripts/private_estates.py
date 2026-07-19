#!/usr/bin/env python3
"""Compile and apply INSIGHT's audited Surrey private-estate rules.

The research registry is deliberately broader than the installed classifier.
This module activates only the reviewed core: ready/partial-ready entities and
whole-road rules, plus the postcode-bounded Knott Park segment. Matching is
fail-closed and uses structured HM Land Registry address fields only.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "private-estates.installation-candidate.json"
GEOMETRY_PATH = ROOT / "config" / "private-estates.map-geometry.installation-candidate.json"

ACTIVE_INSTALL_STATUSES = frozenset({"ready", "partial_ready"})
EXPECTED_ACTIVE_DEFINITION_COUNT = 23
EXPECTED_ACTIVE_RULE_COUNT = 137

MARKET_BY_AUTHORITY = {
    "Elmbridge": "elmbridge-prime",
    "Epsom and Ewell": "epsom-ewell",
    "Guildford": "guildford-district",
    "Mole Valley": "mole-valley",
    "Reigate and Banstead": "reigate-banstead",
    "Runnymede": "runnymede-wentworth",
    "Spelthorne": "spelthorne",
    "Surrey Heath": "surrey-heath",
    "Tandridge": "tandridge-oxted",
    "Waverley": "waverley-south-surrey",
    "Woking": "woking-district",
}

_KNOTT_SEGMENT = ("knott-park", "The Chase", "segment")
_DISABLED_ALIASES = {("burwood-park", "Manor Park Drive")}
_HOUSE_RANGE_SCOPES = frozenset({"house_range", "odd_house_range", "even_house_range"})


def clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def normalise_text(value: Any) -> str:
    """Return a conservative comparable form without fuzzy abbreviations."""

    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.upper().replace("&", " AND ")
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def normalise_postcode(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def slug(value: Any) -> str:
    return normalise_text(value).lower().replace(" ", "-")


def stable_rule_id(estate_id: str, canonical_street: str, scope: str) -> str:
    """Build a stable identifier independent of aliases or display copy."""

    return f"pe:{estate_id}:{slug(canonical_street)}:{slug(scope)}"


def read_registry(path: Path = REGISTRY_PATH) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_geometry(path: Path = GEOMETRY_PATH) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _compile_house_ranges(road: Mapping[str, Any]) -> list[Dict[str, Any]]:
    """Compile deliberately simple numeric PAON ranges; reject prose or suffixes."""

    scope = clean(road.get("scope"))
    if scope not in _HOUSE_RANGE_SCOPES:
        return []
    compiled = []
    for value in road.get("houseRanges", []):
        match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)(?:\s+(odd|even))?\s*", clean(value), re.IGNORECASE)
        if not match:
            return []
        minimum, maximum = int(match.group(1)), int(match.group(2))
        if minimum > maximum:
            return []
        parity = clean(match.group(3)).lower()
        scope_parity = "odd" if scope == "odd_house_range" else "even" if scope == "even_house_range" else ""
        if parity and scope_parity and parity != scope_parity:
            return []
        compiled.append({"minimum": minimum, "maximum": maximum, "parity": parity or scope_parity})
    return compiled


def _is_audit_safe_rule(estate: Mapping[str, Any], road: Mapping[str, Any]) -> bool:
    if road.get("ruleStatus") == "hold":
        return False
    scope = clean(road.get("scope"))
    if scope == "whole":
        return True
    if scope in _HOUSE_RANGE_SCOPES:
        return bool(_compile_house_ranges(road))
    special = (estate.get("id"), road.get("canonical"), scope)
    return special == _KNOTT_SEGMENT and bool(road.get("postcodes"))


def _aliases_for(estate_id: str, road: Mapping[str, Any]) -> list[str]:
    aliases = []
    for alias in road.get("aliases", []):
        if (estate_id, clean(alias)) in _DISABLED_ALIASES:
            continue
        aliases.append(clean(alias))
    return aliases


def compile_registry(registry: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Compile the reviewed, explicitly product-scoped runtime subset."""

    source = dict(registry or read_registry())
    definitions = []
    rules = []
    held_rule_count = 0
    excluded_non_whole_rule_count = 0
    disabled_alias_count = 0
    excluded_product_cohort_count = 0
    product_cohorts = source.get("productCohorts") or {}
    traditional_ids = {clean(value) for value in product_cohorts.get("traditionalPrivateEstateIds", [])}
    managed_ids = {clean(value) for value in product_cohorts.get("managedDesignEstateIds", [])}
    product_estate_ids = traditional_ids | managed_ids
    if traditional_ids & managed_ids:
        raise ValueError("An estate cannot be in both traditional and managed/design product cohorts")

    for estate in source.get("entities", []):
        estate_id = clean(estate.get("id"))
        install_status = clean(estate.get("installStatus"))
        if install_status not in ACTIVE_INSTALL_STATUSES:
            continue
        if estate_id not in product_estate_ids:
            excluded_product_cohort_count += 1
            continue

        name = clean(estate.get("name"))
        classification = clean(estate.get("classification"))
        estate_type = "managed_design_estate" if estate_id in managed_ids else "traditional_private_estate"
        authorities = [clean(value) for value in estate.get("localAuthorities", []) if clean(value)]
        towns = [clean(value) for value in estate.get("towns", []) if clean(value)]
        if not estate_id or not name or not authorities or not towns:
            raise ValueError(f"Active estate {estate_id or name!r} lacks an id, name, authority or place guard")
        markets = {MARKET_BY_AUTHORITY.get(authority, "") for authority in authorities}
        markets.discard("")
        if len(markets) != 1:
            raise ValueError(f"Active estate {estate_id!r} does not resolve to exactly one INSIGHT market")
        market = next(iter(markets))

        estate_rule_ids = []
        for road in estate.get("roads", []):
            if road.get("ruleStatus") == "hold":
                held_rule_count += 1
                continue
            if not _is_audit_safe_rule(estate, road):
                excluded_non_whole_rule_count += 1
                continue

            canonical = clean(road.get("canonical"))
            scope = clean(road.get("scope"))
            if not canonical:
                raise ValueError(f"Active estate {estate_id!r} contains a rule without a canonical street")
            aliases = _aliases_for(estate_id, road)
            disabled_alias_count += len(road.get("aliases", [])) - len(aliases)
            rule_id = stable_rule_id(estate_id, canonical, scope)
            postcodes = [clean(value).upper() for value in road.get("postcodes", []) if clean(value)]
            house_number_ranges = _compile_house_ranges(road)
            evidence_status = clean(road.get("membershipStatus")) or "verified"
            rule = {
                "ruleId": rule_id,
                "estateId": estate_id,
                "estate": name,
                "estateClassification": classification,
                "estateType": estate_type,
                "installStatus": install_status,
                "canonicalStreet": canonical,
                "normalisedStreet": normalise_text(canonical),
                "aliases": aliases,
                "normalisedAliases": [normalise_text(value) for value in aliases],
                "scope": scope,
                "localAuthorities": authorities,
                "normalisedLocalAuthorities": [normalise_text(value) for value in authorities],
                "towns": towns,
                "normalisedTowns": [normalise_text(value) for value in towns],
                "postcodes": postcodes,
                "normalisedPostcodes": [normalise_postcode(value) for value in postcodes],
                "houseNumberRanges": house_number_ranges,
                "evidenceStatus": evidence_status,
                "reviewStatus": install_status,
            }
            rules.append(rule)
            estate_rule_ids.append(rule_id)

        display_aliases = [clean(value) for value in estate.get("displayAliases", []) if clean(value)]
        definitions.append({
            "id": estate_id,
            "estateId": estate_id,
            "name": name,
            "aliases": display_aliases,
            "displayAliases": display_aliases,
            "classification": classification,
            "estateType": estate_type,
            "installStatus": install_status,
            "market": market,
            "localAuthorities": authorities,
            "normalisedLocalAuthorities": [normalise_text(value) for value in authorities],
            "towns": towns,
            "normalisedTowns": [normalise_text(value) for value in towns],
            "ruleIds": estate_rule_ids,
            "ruleCount": len(estate_rule_ids),
            "activeRuleCount": len(estate_rule_ids),
            "evidenceStatus": "verified_core",
            "reviewStatus": install_status,
        })

    rule_ids = [rule["ruleId"] for rule in rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("The compiled private-estate registry contains duplicate rule ids")
    if len(definitions) != EXPECTED_ACTIVE_DEFINITION_COUNT:
        raise ValueError(
            f"Expected {EXPECTED_ACTIVE_DEFINITION_COUNT} active estate definitions, got {len(definitions)}"
        )
    if len(rules) != EXPECTED_ACTIVE_RULE_COUNT:
        raise ValueError(f"Expected {EXPECTED_ACTIVE_RULE_COUNT} active estate rules, got {len(rules)}")

    return {
        "schemaVersion": 1,
        "registryVersion": clean(source.get("registryVersion")),
        "asOf": clean(source.get("asOf")),
        "activeInstallStatuses": sorted(ACTIVE_INSTALL_STATUSES),
        "definitions": definitions,
        "rules": rules,
        "metadata": {
            "sourceEntityCount": len(source.get("entities", [])),
            "activeDefinitionCount": len(definitions),
            "activeRuleCount": len(rules),
            "heldRuleCount": held_rule_count,
            "excludedNonWholeRuleCount": excluded_non_whole_rule_count,
            "disabledAliasCount": disabled_alias_count,
            "excludedProductCohortCount": excluded_product_cohort_count,
        },
    }


@lru_cache(maxsize=1)
def load_compiled_registry() -> Dict[str, Any]:
    return compile_registry()


def _rules_by_street(compiled: Mapping[str, Any]) -> Dict[str, list[Mapping[str, Any]]]:
    index: Dict[str, list[Mapping[str, Any]]] = {}
    for rule in compiled.get("rules", []):
        spellings: Iterable[str] = [rule.get("normalisedStreet", ""), *rule.get("normalisedAliases", [])]
        for spelling in spellings:
            if spelling:
                index.setdefault(spelling, []).append(rule)
    return index


def classify_estate(
    record: Mapping[str, Any],
    compiled: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Classify one structured address, returning an empty mapping on doubt.

    Street, district and at least one configured locality/town must match
    exactly after punctuation normalisation. Rules carrying postcodes also
    require an exact postcode. Any ambiguous multi-rule result is discarded.
    Legacy ``record['estate']`` is intentionally never read.
    """

    if not isinstance(record, Mapping):
        return {}
    street = normalise_text(record.get("street"))
    authority = normalise_text(record.get("district"))
    locality = normalise_text(record.get("locality"))
    town = normalise_text(record.get("town"))
    postcode = normalise_postcode(record.get("postcode"))
    paon = clean(record.get("paon"))
    if not street or not authority or not (locality or town):
        return {}

    runtime = compiled or load_compiled_registry()
    place_values = {value for value in (locality, town) if value}
    matches = []
    for rule in _rules_by_street(runtime).get(street, []):
        if authority not in rule.get("normalisedLocalAuthorities", []):
            continue
        if place_values.isdisjoint(rule.get("normalisedTowns", [])):
            continue
        required_postcodes = rule.get("normalisedPostcodes", [])
        if required_postcodes and postcode not in required_postcodes:
            continue
        house_ranges = rule.get("houseNumberRanges", [])
        if house_ranges:
            if not re.fullmatch(r"\d+", paon):
                continue
            house_number = int(paon)
            if not any(
                item["minimum"] <= house_number <= item["maximum"]
                and (not item.get("parity") or house_number % 2 == (1 if item["parity"] == "odd" else 0))
                for item in house_ranges
            ):
                continue
        matches.append(rule)

    if len(matches) != 1:
        return {}
    rule = matches[0]
    return {
        "estateId": rule["estateId"],
        "estate": rule["estate"],
        "estateClassification": rule["estateClassification"],
        "estateType": rule["estateType"],
        "estateRuleId": rule["ruleId"],
        "estateRegistryVersion": runtime["registryVersion"],
        "estateEvidenceStatus": rule["evidenceStatus"],
        "estateReviewStatus": rule["reviewStatus"],
    }


def estate_for_record(
    record: Mapping[str, Any],
    compiled: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Compatibility name for callers that need the full derived metadata."""

    return classify_estate(record, compiled=compiled)
