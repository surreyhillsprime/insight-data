#!/usr/bin/env python3
"""Attach licensed property-level planning history to the INSIGHT feed.

The source may be a local CSV/JSON file or an HTTPS URL. It must contain one
row/object per application. Council portal HTML is deliberately not scraped:
several portal licences prohibit copying or redistributing their records.
"""

import argparse
import csv
import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from io import StringIO
from pathlib import Path

from insight_data_utils import DEFAULT_INPUT_JS, clean, normalise_postcode, read_js, utc_now, write_js


POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)
FIELD_ALIASES = {
    "authority": ("authority", "local_authority", "localAuthority", "council"),
    "reference": ("reference", "application_reference", "applicationReference", "planning_reference"),
    "siteAddress": ("siteAddress", "site_address", "address", "location"),
    "postcode": ("postcode", "site_postcode", "sitePostcode"),
    "proposal": ("proposal", "description", "development_description", "developmentDescription"),
    "applicationType": ("applicationType", "application_type", "type"),
    "status": ("status", "application_status", "applicationStatus"),
    "decision": ("decision", "decision_type", "decisionType"),
    "receivedDate": ("receivedDate", "received_date", "date_received"),
    "validatedDate": ("validatedDate", "validated_date", "date_validated"),
    "decisionDate": ("decisionDate", "decision_date", "date_decision"),
    "portalUrl": ("portalUrl", "portal_url", "url", "application_url", "applicationUrl"),
    "uprn": ("uprn", "UPRN"),
}


def first_field(row, aliases):
    for name in aliases:
        value = row.get(name)
        if value not in (None, ""):
            return clean(value)
    return ""


def read_source(source, timeout=30):
    if source.startswith("https://"):
        request = urllib.request.Request(source, headers={"User-Agent": "INSIGHT licensed planning history importer"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8-sig", errors="replace")
            content_type = response.headers.get("Content-Type", "")
    else:
        path = Path(source).expanduser()
        text = path.read_text(encoding="utf-8-sig")
        content_type = "application/json" if path.suffix.lower() == ".json" else "text/csv"
    stripped = text.lstrip()
    if "json" in content_type.lower() or stripped.startswith(("[", "{")):
        payload = json.loads(text)
        if isinstance(payload, dict):
            for key in ("applications", "records", "items", "results", "data"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            raise ValueError("Planning JSON must contain an applications, records, items, results, or data array")
        if not isinstance(payload, list):
            raise ValueError("Planning JSON must be an array of applications")
        return payload
    return list(csv.DictReader(StringIO(text)))


def postcode_from(value):
    match = POSTCODE_RE.search(clean(value).upper())
    return normalise_postcode(match.group(1)) if match else ""


def normalise_application(row):
    app = {field: first_field(row, aliases) for field, aliases in FIELD_ALIASES.items()}
    app["postcode"] = normalise_postcode(app["postcode"]) or postcode_from(app["siteAddress"])
    if not app["reference"] or not app["siteAddress"]:
        return None
    return {key: value for key, value in app.items() if value}


def address_tokens(value):
    text = clean(value).upper()
    text = POSTCODE_RE.sub(" ", text)
    text = re.sub(r"\b(SURREY|UNITED KINGDOM|UK)\b", " ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    ignored = {"THE", "ROAD", "RD", "STREET", "ST", "AVENUE", "AVE", "LANE", "LN"}
    return [token for token in text.split() if token not in ignored]


def address_score(property_address, application_address):
    left = address_tokens(property_address)
    right = address_tokens(application_address)
    if not left or not right:
        return 0.0
    left_set, right_set = set(left), set(right)
    overlap = len(left_set & right_set) / max(1, len(left_set | right_set))
    sequence = SequenceMatcher(None, " ".join(left), " ".join(right)).ratio()
    identifier = left[0]
    identifier_match = identifier in right_set
    score = (overlap * 0.55) + (sequence * 0.35) + (0.10 if identifier_match else 0)
    if any(char.isdigit() for char in identifier) and not identifier_match:
        score *= 0.45
    return round(min(1.0, score), 3)


def application_date(app):
    return app.get("decisionDate") or app.get("validatedDate") or app.get("receivedDate") or ""


def property_uprn(item):
    return clean(item.get("uprn") or (item.get("ordnanceSurvey") or {}).get("uprn"))


def match_applications(item, postcode_index, uprn_index, minimum_score):
    uprn = property_uprn(item)
    if uprn and uprn_index.get(uprn):
        return sorted(uprn_index[uprn], key=application_date, reverse=True), "uprn", 1.0
    postcode = normalise_postcode(item.get("postcode"))
    candidates = postcode_index.get(postcode, [])
    matches = []
    best_score = 0.0
    for app in candidates:
        score = address_score(item.get("address"), app.get("siteAddress"))
        if score >= minimum_score:
            matched = dict(app)
            matched["matchConfidence"] = score
            matches.append(matched)
            best_score = max(best_score, score)
    matches.sort(key=application_date, reverse=True)
    return matches, "postcode-and-address", best_score


def enrich(transactions, applications, minimum_score=0.72):
    postcode_index = defaultdict(list)
    uprn_index = defaultdict(list)
    unique = {}
    for raw in applications:
        if not isinstance(raw, dict):
            continue
        app = normalise_application(raw)
        if not app:
            continue
        key = (app.get("authority", "").lower(), app["reference"].lower())
        unique[key] = app
    for app in unique.values():
        if app.get("postcode"):
            postcode_index[app["postcode"]].append(app)
        if app.get("uprn"):
            uprn_index[app["uprn"]].append(app)

    stats = Counter(sourceApplications=len(unique))
    enriched = []
    checked_at = utc_now()
    for item in transactions:
        output = dict(item)
        matches, method, confidence = match_applications(item, postcode_index, uprn_index, minimum_score)
        stats["propertiesChecked"] += 1
        if matches:
            latest = matches[0]
            output["planningHistory"] = {
                "source": "Licensed local-authority planning data",
                "updatedAt": checked_at,
                "authority": latest.get("authority") or item.get("district", ""),
                "totalApplications": len(matches),
                "latestApplication": latest,
                "applications": matches,
                "matchMethod": method,
                "matchConfidence": confidence,
            }
            stats["propertiesWithHistory"] += 1
            stats["applicationMatches"] += len(matches)
        enriched.append(output)
    return enriched, stats


def parse_args():
    parser = argparse.ArgumentParser(description="Attach licensed property-level planning history to INSIGHT.")
    parser.add_argument("--source", required=True, help="Licensed planning CSV/JSON file or HTTPS URL.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="Input INSIGHT transaction feed.")
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS), help="Output INSIGHT transaction feed.")
    parser.add_argument("--minimum-address-score", type=float, default=0.72, help="Minimum postcode/address match confidence.")
    parser.add_argument("--timeout", type=float, default=30, help="HTTPS source timeout.")
    parser.add_argument("--dry-run", action="store_true", help="Report matches without writing the feed.")
    return parser.parse_args()


def main():
    args = parse_args()
    transactions, _summary, meta = read_js(args.input_js)
    applications = read_source(args.source, timeout=args.timeout)
    enriched, stats = enrich(transactions, applications, args.minimum_address_score)
    print("Planning history summary: " + ", ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    if args.dry_run:
        return 0
    meta["planningHistory"] = {
        "updatedAt": utc_now(),
        "source": "Licensed local-authority planning data",
        "propertiesChecked": stats["propertiesChecked"],
        "propertiesWithHistory": stats["propertiesWithHistory"],
        "applicationsFound": stats["applicationMatches"],
    }
    write_js(args.write_js, enriched, meta)
    print(f"Updated {args.write_js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
