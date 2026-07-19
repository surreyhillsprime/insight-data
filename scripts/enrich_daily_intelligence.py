#!/usr/bin/env python3
"""Daily INSIGHT enrichment for fast-moving planning and company signals."""

import argparse
import base64
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from insight_data_utils import (
    DEFAULT_INPUT_JS,
    approx_walk_time,
    cache_fresh,
    clean,
    coordinates_from_item,
    ensure_coordinates,
    format_distance,
    haversine_metres,
    load_cache,
    normalise_postcode,
    parse_float,
    read_js,
    request_json,
    utc_now,
    wkt_square,
    write_cache,
    write_js,
)


CACHE_VERSION = 1
DEFAULT_CACHE = DEFAULT_INPUT_JS.parents[1] / "work" / "daily-intelligence-cache.json"
PLANNING_ENTITY_API = "https://www.planning.data.gov.uk/entity.json"
COMPANIES_HOUSE_API = "https://api.company-information.service.gov.uk"
PLANNING_COVERAGE_OBSERVED = "observed"
PLANNING_COVERAGE_UNKNOWN = "unknown"
PLANNING_COVERAGE_STATUSES = {PLANNING_COVERAGE_OBSERVED, PLANNING_COVERAGE_UNKNOWN, "unavailable"}
PLANNING_COVERAGE_NOTE = (
    "Planning Data coverage varies by authority; an empty spatial response is not evidence "
    "that no nearby applications exist."
)


def nested_value(source, path):
    value = source
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def first_value(source, paths):
    for path in paths:
        value = nested_value(source, path)
        if value not in (None, ""):
            return value
    return ""


def entity_list(payload):
    if not isinstance(payload, dict):
        return []
    for key in ("entities", "items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def next_page_link(payload):
    """Return the API's declared next-page link, across supported link shapes."""

    links = payload.get("links") if isinstance(payload, dict) else None
    if isinstance(links, dict):
        value = links.get("next")
        if isinstance(value, dict):
            value = value.get("href") or value.get("url")
        return clean(value)
    if isinstance(links, list):
        for value in links:
            if not isinstance(value, dict):
                continue
            if clean(value.get("rel") or value.get("name")).lower() == "next":
                return clean(value.get("href") or value.get("url"))
    return ""


def entity_value(entity, names):
    if not isinstance(entity, dict):
        return ""
    for name in names:
        variants = {name, name.replace("_", "-"), name.replace("-", "_")}
        for variant in variants:
            if entity.get(variant) not in (None, ""):
                return entity.get(variant)
    return ""


def point_from_wkt(value):
    text = clean(value)
    if not text.upper().startswith("POINT"):
        return None, None
    parts = text[text.find("(") + 1:text.find(")")].replace(",", " ").split()
    if len(parts) < 2:
        return None, None
    lon = parse_float(parts[0])
    lat = parse_float(parts[1])
    return lat, lon


def entity_distance(entity, lat, lon):
    point = entity.get("point") or entity.get("geometry")
    point_lat, point_lon = point_from_wkt(point)
    if point_lat is None or point_lon is None:
        return None
    return round(haversine_metres(lat, lon, point_lat, point_lon))


def parse_entity_date(entity):
    return clean(entity_value(entity, [
        "start-date",
        "start_date",
        "entry-date",
        "entry_date",
        "decision-date",
        "decision_date",
        "received_date",
        "valid_date",
    ]))


def application_label(application):
    name = clean(application.get("name") or application.get("description") or application.get("address") or "Planning application")
    reference = clean(application.get("reference"))
    if reference and reference.lower() not in name.lower():
        return f"{name} ({reference})"
    return name


def planning_context_is_truthful(context):
    if not isinstance(context, dict):
        return False
    status = clean(context.get("coverageStatus")).lower()
    applications = context.get("recentApplications")
    applications = applications if isinstance(applications, list) else []
    latest = clean(context.get("latestApplication"))
    if status == PLANNING_COVERAGE_OBSERVED:
        return (
            context.get("coverageMode") == "positive-results-only"
            and bool(applications)
            and bool(latest)
            and not latest.lower().startswith("no recent")
        )
    if status in PLANNING_COVERAGE_STATUSES - {PLANNING_COVERAGE_OBSERVED}:
        return (
            context.get("coverageMode") == "no-authoritative-negative-coverage"
            and not applications
            and not latest
            and "recentApplicationCount" not in context
        )
    return False


def planning_context_is_current(context, refresh_hours):
    if not planning_context_is_truthful(context):
        return False
    return cache_fresh(
        {"updatedAt": context.get("updatedAt")},
        refresh_hours * 60 * 60,
    )


def normalise_existing_planning(context):
    if not isinstance(context, dict):
        return None
    if planning_context_is_truthful(context):
        return dict(context)

    applications = context.get("recentApplications")
    applications = applications if isinstance(applications, list) else []
    base = {
        key: context[key]
        for key in ("source", "updatedAt", "period", "searchRadius")
        if context.get(key) not in (None, "")
    }
    base.setdefault("source", "Planning Data API")
    base["coverageNote"] = PLANNING_COVERAGE_NOTE
    if applications:
        base.update({
            "coverageStatus": PLANNING_COVERAGE_OBSERVED,
            "coverageMode": "positive-results-only",
            "queryResultCount": len(applications),
            "recentApplicationCount": len(applications),
            "latestApplication": application_label(applications[0]),
            "latestDecision": clean(first_value(applications[0], ["decision", "status"])),
            "recentApplications": applications,
        })
        return base

    base.update({
        "coverageStatus": PLANNING_COVERAGE_UNKNOWN,
        "coverageMode": "no-authoritative-negative-coverage",
        "coverageReason": "legacy-or-empty-spatial-result-unproven",
        "queryResultCount": 0,
        "recentApplications": [],
    })
    return base


def unavailable_planning_context(args, since, reason):
    return {
        "source": "Planning Data API",
        "updatedAt": utc_now(),
        "period": f"Since {since.isoformat()}",
        "searchRadius": format_distance(args.planning_radius_m),
        "coverageStatus": "unavailable",
        "coverageMode": "no-authoritative-negative-coverage",
        "coverageReason": reason,
        "coverageNote": PLANNING_COVERAGE_NOTE,
        "recentApplications": [],
    }


def planning_cache_key(item, since, radius_m):
    postcode = normalise_postcode(item.get("postcode"))
    return f"{postcode or item.get('id')}|{since.isoformat()}|{int(radius_m)}"


def recent_planning_for_item(item, lat, lon, cache, args, since):
    key = planning_cache_key(item, since, args.planning_radius_m)
    planning_cache = cache.setdefault("planningApplications", {})
    cached = planning_cache.get(key)
    cached_data = cached.get("data") if isinstance(cached, dict) else None
    cached_context = cached_data.get("planning") if isinstance(cached_data, dict) else None
    if cache_fresh(cached, args.refresh_hours * 60 * 60) and planning_context_is_truthful(cached_context):
        return cached_data

    params = {
        "dataset": "planning-application",
        "geometry": wkt_square(lat, lon, args.planning_radius_m),
        "geometry_relation": "intersects",
        "start_date_year": since.year,
        "start_date_month": since.month,
        "start_date_day": since.day,
        "start_date_match": "since",
        "limit": args.planning_limit,
    }
    entities = []
    query_pages = 0
    offset = 0
    while True:
        payload = request_json(
            PLANNING_ENTITY_API,
            params={**params, "offset": offset},
            timeout=args.timeout,
            retries=args.retries,
            user_agent="INSIGHT daily planning monitor",
        )
        query_pages += 1
        entities.extend(entity_list(payload))
        if not next_page_link(payload):
            break
        if query_pages >= args.planning_max_pages:
            raise RuntimeError(
                f"Planning Data pagination exceeded the {args.planning_max_pages}-page safety cap"
            )
        offset += args.planning_limit

    applications = []
    unlocated_results = 0
    outside_radius_results = 0
    for entity in entities:
        distance_m = entity_distance(entity, lat, lon)
        if distance_m is None:
            unlocated_results += 1
            continue
        if distance_m > args.planning_radius_m:
            outside_radius_results += 1
            continue
        app = {
            "name": clean(entity_value(entity, ["name", "description", "development_description", "proposal"])),
            "reference": clean(entity_value(entity, ["reference", "application_reference", "planning_application_reference"])),
            "status": clean(entity_value(entity, ["status", "application_status", "decision"])),
            "decision": clean(entity_value(entity, ["decision", "decision_type"])),
            "date": parse_entity_date(entity),
            "address": clean(entity_value(entity, ["address", "site_address", "site"])),
            "dataset": clean(entity.get("dataset") or "planning-application"),
        }
        if distance_m is not None:
            app["metres"] = distance_m
            app["distance"] = format_distance(distance_m)
            app["walkTime"] = approx_walk_time(distance_m)
        url = clean(entity_value(entity, ["documentation_url", "document_url", "url", "planning_application_url"]))
        if url:
            app["url"] = url
        if not app["name"]:
            app["name"] = app["address"] or app["reference"] or "Planning application"
        applications.append({key: value for key, value in app.items() if value not in ("", None)})

    applications.sort(key=lambda row: (row.get("date", ""), -row.get("metres", 0)), reverse=True)
    limited = applications[: args.max_applications_per_property]
    observed_at = utc_now()
    context = {
        "source": "Planning Data API",
        "updatedAt": observed_at,
        "period": f"Since {since.isoformat()}",
        "searchRadius": format_distance(args.planning_radius_m),
        "queryResultCount": len(applications),
        "sourceResultCount": len(entities),
        "queryPages": query_pages,
        "unlocatedResultCount": unlocated_results,
        "outsideRadiusResultCount": outside_radius_results,
        "recentApplications": limited,
        "coverageNote": PLANNING_COVERAGE_NOTE,
    }
    if limited:
        context.update({
            "coverageStatus": PLANNING_COVERAGE_OBSERVED,
            "coverageMode": "positive-results-only",
            "recentApplicationCount": len(applications),
            "latestApplication": application_label(limited[0]),
            "latestDecision": clean(first_value(limited[0], ["decision", "status"])),
        })
        cache_status = "matched"
    else:
        context.update({
            "coverageStatus": PLANNING_COVERAGE_UNKNOWN,
            "coverageMode": "no-authoritative-negative-coverage",
            "coverageReason": "no-proven-within-radius-result",
        })
        cache_status = "unknown"
    data = {"planning": context}
    planning_cache[key] = {"status": cache_status, "updatedAt": observed_at, "data": data}
    return data


def company_number_from_item(item):
    value = first_value(item, [
        "companiesHouse.companyNumber",
        "ownership.companyNumber",
        "title.companyNumber",
        "hmLandRegistry.companyNumber",
        "companyNumber",
    ])
    cleaned = clean(value).upper().replace(" ", "")
    return cleaned if cleaned else ""


def companies_house_headers(api_key):
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def company_cache_key(company_number):
    return clean(company_number).upper().replace(" ", "")


def companies_house_for_item(company_number, cache, args):
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key or not company_number:
        return None
    key = company_cache_key(company_number)
    store = cache.setdefault("companiesHouse", {})
    cached = store.get(key)
    if cache_fresh(cached, args.company_refresh_hours * 60 * 60):
        return cached.get("data")
    headers = companies_house_headers(api_key)
    profile = request_json(
        f"{COMPANIES_HOUSE_API}/company/{key}",
        timeout=args.timeout,
        retries=args.retries,
        headers=headers,
        user_agent="INSIGHT Companies House monitor",
    )
    if not isinstance(profile, dict) or profile.get("status") == 404:
        store[key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    filings = request_json(
        f"{COMPANIES_HOUSE_API}/company/{key}/filing-history",
        params={"items_per_page": 5},
        timeout=args.timeout,
        retries=args.retries,
        headers=headers,
        user_agent="INSIGHT Companies House monitor",
    )
    psc = request_json(
        f"{COMPANIES_HOUSE_API}/company/{key}/persons-with-significant-control",
        params={"items_per_page": 5},
        timeout=args.timeout,
        retries=args.retries,
        headers=headers,
        user_agent="INSIGHT Companies House monitor",
    )
    psc_items = []
    for person in psc.get("items", []) if isinstance(psc, dict) else []:
        name = clean(person.get("name"))
        kind = clean(person.get("kind")).replace("-", " ")
        if name:
            psc_items.append({"name": name, "type": kind.title() if kind else "PSC"})
    filing_items = []
    for filing in filings.get("items", []) if isinstance(filings, dict) else []:
        description = clean(filing.get("description")).replace("-", " ")
        date = clean(filing.get("date"))
        if description or date:
            filing_items.append({"name": description.title() if description else "Filing", "date": date})
    data = {
        "companiesHouse": {
            "source": "Companies House API",
            "updatedAt": utc_now(),
            "ownerCompany": clean(profile.get("company_name")),
            "companyNumber": key,
            "companyStatus": clean(profile.get("company_status")).replace("-", " ").title(),
            "companyType": clean(profile.get("type")).replace("-", " ").title(),
            "personsWithSignificantControl": psc_items,
            "recentFilings": filing_items,
        }
    }
    store[key] = {"status": "matched", "updatedAt": utc_now(), "data": data}
    return data


def enrich_transactions(transactions, cache, args):
    since = (datetime.now(timezone.utc).date() - timedelta(days=args.planning_days))
    enriched = []
    stats = Counter()
    disabled = set()
    limit = args.limit if args.limit and args.limit > 0 else None
    planning_donors = {}
    if args.missing_only:
        for transaction in transactions:
            postcode = normalise_postcode(transaction.get("postcode"))
            planning = normalise_existing_planning(transaction.get("planning"))
            if postcode and planning and planning_context_is_current(planning, args.refresh_hours):
                planning_donors.setdefault(postcode, planning)

    for index, item in enumerate(transactions, start=1):
        output = dict(item)
        if output.get("planning"):
            output["planning"] = normalise_existing_planning(output.get("planning"))
            if output["planning"] != item.get("planning"):
                stats["planningNormalised"] += 1
        if limit and index > limit:
            enriched.append(output)
            continue

        if args.missing_only and not planning_context_is_truthful(output.get("planning")):
            donor = planning_donors.get(normalise_postcode(item.get("postcode")))
            if donor:
                output["planning"] = donor
                stats["planningReused"] += 1

        lat = lon = None
        if "geocode" not in disabled:
            try:
                lat, lon, coord_data = ensure_coordinates(item, cache, args)
                if coord_data:
                    output.update(coord_data)
                    stats["postcodes"] += 1
            except Exception as exc:
                stats["geocodeErrors"] += 1
                print(f"Postcode geocode skipped for {item.get('id')}: {exc}", file=sys.stderr)
                if args.max_source_errors and stats["geocodeErrors"] >= args.max_source_errors:
                    disabled.add("geocode")
        else:
            lat, lon = coordinates_from_item(output)

        has_reusable_planning = args.missing_only and planning_context_is_current(
            output.get("planning"),
            args.refresh_hours,
        )
        if lat is not None and lon is not None and "planning" not in disabled and not args.disable_planning and not has_reusable_planning:
            try:
                planning = recent_planning_for_item(item, lat, lon, cache, args, since)
                if planning:
                    output.update(planning)
                    stats["planning"] += 1
            except Exception as exc:
                stats["planningErrors"] += 1
                print(f"Planning Data skipped for {item.get('id')}: {exc}", file=sys.stderr)
                existing = output.get("planning")
                if not (
                    planning_context_is_truthful(existing)
                    and existing.get("coverageStatus") == PLANNING_COVERAGE_OBSERVED
                ):
                    output["planning"] = unavailable_planning_context(args, since, "request-failed")
                if args.max_source_errors and stats["planningErrors"] >= args.max_source_errors:
                    disabled.add("planning")
        elif not args.disable_planning and "planning" in disabled:
            existing = output.get("planning")
            if not (
                planning_context_is_truthful(existing)
                and existing.get("coverageStatus") == PLANNING_COVERAGE_OBSERVED
            ):
                output["planning"] = unavailable_planning_context(
                    args,
                    since,
                    "source-disabled-after-request-failures",
                )
        elif (lat is None or lon is None) and not args.disable_planning:
            existing = output.get("planning")
            if not (
                planning_context_is_truthful(existing)
                and existing.get("coverageStatus") == PLANNING_COVERAGE_OBSERVED
            ):
                output["planning"] = unavailable_planning_context(args, since, "missing-coordinates")

        company_number = company_number_from_item(output)
        if company_number and "companiesHouse" not in disabled and not args.disable_companies_house:
            try:
                company = companies_house_for_item(company_number, cache, args)
                if company:
                    output.update(company)
                    stats["companiesHouse"] += 1
            except Exception as exc:
                stats["companiesHouseErrors"] += 1
                print(f"Companies House skipped for {item.get('id')}: {exc}", file=sys.stderr)
                if args.max_source_errors and stats["companiesHouseErrors"] >= args.max_source_errors:
                    disabled.add("companiesHouse")

        enriched.append(output)
        if args.pause:
            time.sleep(args.pause)
        if index % args.progress_every == 0:
            print(f"Processed {index}/{len(transactions)} properties; daily fields so far: {dict(stats)}", flush=True)

    return enriched, stats


def parse_args():
    parser = argparse.ArgumentParser(description="Refresh INSIGHT daily planning and company intelligence.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="Input INSIGHT JS feed.")
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS), help="Output INSIGHT JS feed.")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="Daily intelligence cache path.")
    parser.add_argument("--limit", type=int, default=0, help="Only enrich the first N records.")
    parser.add_argument("--timeout", type=float, default=20, help="API request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries for transient API failures.")
    parser.add_argument("--pause", type=float, default=0.15, help="Pause between property lookups.")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress every N records.")
    parser.add_argument("--max-source-errors", type=int, default=25, help="Disable a source after this many errors. Use 0 to never disable.")
    parser.add_argument("--geocode-refresh-days", type=int, default=365, help="Postcode coordinate cache lifetime.")
    parser.add_argument("--refresh-hours", type=int, default=20, help="Planning result cache lifetime.")
    parser.add_argument("--company-refresh-hours", type=int, default=20, help="Companies House cache lifetime.")
    parser.add_argument("--planning-days", type=int, default=45, help="Look back this many days for planning applications.")
    parser.add_argument("--planning-radius-m", type=int, default=1200, help="Planning search radius around each property.")
    parser.add_argument("--planning-limit", type=int, default=50, help="Planning API limit per property.")
    parser.add_argument("--planning-max-pages", type=int, default=20, help="Fail closed if one planning query exceeds this many API pages.")
    parser.add_argument("--max-applications-per-property", type=int, default=6, help="Store this many recent planning application summaries.")
    parser.add_argument("--disable-planning", action="store_true", help="Skip Planning Data API.")
    parser.add_argument("--missing-only", action="store_true", help="Preserve existing intelligence and populate only transactions that still lack it.")
    parser.add_argument("--disable-companies-house", action="store_true", help="Skip Companies House API.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write outputs.")
    return parser.parse_args()


def main():
    args = parse_args()
    args.progress_every = max(1, args.progress_every)
    transactions, _summary, meta = read_js(args.input_js)
    cache = load_cache(args.cache, CACHE_VERSION)
    print(f"Transactions: {len(transactions)}")
    enriched, stats = enrich_transactions(transactions, cache, args)
    print("Daily intelligence summary: " + ", ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    if args.dry_run:
        return 0

    planning_records = [
        item.get("planning") for item in enriched
        if planning_context_is_truthful(item.get("planning"))
    ]
    meta["dailyIntelligence"] = {
        "updatedAt": utc_now(),
        "planning": {
            "source": "Planning Data API",
            "records": len(planning_records),
            "observedRecords": sum(
                1 for item in planning_records
                if item.get("coverageStatus") == PLANNING_COVERAGE_OBSERVED
            ),
            "unknownRecords": sum(
                1 for item in planning_records
                if item.get("coverageStatus") == PLANNING_COVERAGE_UNKNOWN
            ),
            "unavailableRecords": sum(
                1 for item in planning_records
                if item.get("coverageStatus") == "unavailable"
            ),
            "successfulResponses": sum(
                1 for item in planning_records
                if item.get("coverageStatus") in {PLANNING_COVERAGE_OBSERVED, PLANNING_COVERAGE_UNKNOWN}
            ),
            "coverageMode": "positive-observations-only",
            "lookbackDays": args.planning_days,
            "radiusMetres": args.planning_radius_m,
        },
        "companiesHouse": {
            "source": "Companies House API",
            "records": sum(1 for item in enriched if item.get("companiesHouse")),
            "requiresCompanyNumber": True,
        },
    }
    write_cache(args.cache, cache, CACHE_VERSION)
    write_js(args.write_js, enriched, meta)
    print(f"Updated {args.write_js}")
    print(f"Updated {args.cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
