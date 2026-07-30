#!/usr/bin/env python3
"""Refresh the governed official snapshots used by the daily INSIGHT View."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
from copy import deepcopy
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from insight_view import (
    MARKET_SOURCE_ID,
    MORTGAGE_SOURCE_ID,
    POLICY_SOURCE_ID,
    TIME_ZONE,
    InsightViewValidationError,
    clean,
    iso_date,
    load_snapshot,
    validate_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "config" / "insight-view-snapshot.json"
HPI_URL = (
    "https://landregistry.data.gov.uk/data/ukhpi/region/"
    "{region}/month.json?_pageSize=2&_sort=-refPeriodStart"
)
USER_AGENT = "INSIGHT daily official-data briefing/1.0 (+https://gaininsight.app)"
DATE_FORMATS = (
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = clean(data)
        if value:
            self.parts.append(value)

    def text(self) -> str:
        return clean(" ".join(self.parts))


def html_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return parser.text()


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            raise ValueError(f"HTTP {response.status} from {url}")
        return response.read()


def parse_observation_date(value: Any) -> date | None:
    text = clean(value)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_boe_csv(payload: bytes | str, series_id: str) -> list[tuple[date, float]]:
    text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
    output: dict[date, float] = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        observed = next((parse_observation_date(cell) for cell in row if parse_observation_date(cell)), None)
        values = []
        for cell in row[1:]:
            candidate = clean(cell).replace("%", "")
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", candidate):
                continue
            values.append(float(candidate))
        if observed and values:
            output[observed] = values[-1]
    rows = sorted(output.items())
    if not rows:
        raise ValueError(f"Bank of England CSV contains no {series_id} observations")
    if any(not 0 <= value <= 25 for _observed, value in rows):
        raise ValueError(f"Bank of England {series_id} contains an implausible rate")
    return rows


def parse_vote_summary(payload: bytes | str, announcement_date: date) -> dict[str, Any]:
    source = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    text = html_text(source)
    outcome_match = re.search(
        r"voted by a majority of\s+(\d+)\s*[–—-]\s*(\d+)\s+to\s+"
        r"(maintain|reduce|increase)\s+Bank Rate",
        text,
        re.I,
    )
    if outcome_match:
        votes_for = int(outcome_match.group(1))
        votes_against = int(outcome_match.group(2))
        action = outcome_match.group(3).lower()
    else:
        unanimous = re.search(
            r"voted unanimously to\s+(maintain|reduce|increase)\s+Bank Rate",
            text,
            re.I,
        )
        if not unanimous:
            raise ValueError("MPC summary does not expose a supported vote statement")
        votes_for = 9
        votes_against = 0
        action = unanimous.group(1).lower()
    outcome = {"maintain": "hold", "reduce": "cut", "increase": "raise"}[action]
    alternative = "none"
    if votes_against:
        alternatives = re.findall(
            r"(?:member|members)\s+voted to\s+(maintain|reduce|increase)\s+Bank Rate",
            text,
            re.I,
        )
        for alternative_action in alternatives:
            candidate = {
                "maintain": "hold",
                "reduce": "cut",
                "increase": "raise",
            }[alternative_action.lower()]
            if candidate != outcome:
                alternative = candidate
                break
        if alternative == "none":
            raise ValueError("MPC minority vote direction is unavailable")
    return {
        "announcementDate": announcement_date.isoformat(),
        "outcome": outcome,
        "for": votes_for,
        "against": votes_against,
        "alternative": alternative,
    }


def hpi_items(payload: bytes | str | Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        value = payload
    else:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        value = json.loads(text)
    result = value.get("result") if isinstance(value, Mapping) else None
    items = result.get("items") if isinstance(result, Mapping) else None
    if not isinstance(items, list):
        raise ValueError("UK HPI response does not contain result.items")
    valid = [
        dict(item)
        for item in items
        if isinstance(item, Mapping)
        and re.fullmatch(r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", clean(item.get("refMonth")))
    ]
    valid.sort(key=lambda item: clean(item["refMonth"]), reverse=True)
    if len(valid) < 2:
        raise ValueError("UK HPI response must contain the latest two months")
    return valid


def hpi_number(item: Mapping[str, Any], field: str) -> float:
    try:
        value = float(item[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"UK HPI item is missing {field}") from error
    if not -10_000_000 <= value <= 10_000_000:
        raise ValueError(f"UK HPI {field} is implausible")
    return value


def parse_hpi_market(
    uk_payload: bytes | str | Mapping[str, Any],
    surrey_payload: bytes | str | Mapping[str, Any],
    london_payload: bytes | str | Mapping[str, Any],
    *,
    retrieved_at: str,
    source_url: str,
) -> dict[str, Any]:
    uk = hpi_items(uk_payload)
    surrey = hpi_items(surrey_payload)
    london = hpi_items(london_payload)
    observation_month = clean(uk[0]["refMonth"])
    if clean(surrey[0]["refMonth"]) != observation_month or clean(london[0]["refMonth"]) != observation_month:
        raise ValueError("UK, Surrey and London HPI observation months do not align")
    return {
        "ukAveragePrice": round(hpi_number(uk[0], "averagePrice")),
        "ukAnnualChange": round(hpi_number(uk[0], "percentageAnnualChange"), 2),
        "ukPreviousAnnualChange": round(
            hpi_number(uk[1], "percentageAnnualChange"), 2
        ),
        "ukMonthlyChange": round(hpi_number(uk[0], "percentageChange"), 2),
        "surreyAveragePrice": round(hpi_number(surrey[0], "averagePrice")),
        "surreyAnnualChange": round(
            hpi_number(surrey[0], "percentageAnnualChange"), 2
        ),
        "surreyMonthlyChange": round(
            hpi_number(surrey[0], "percentageChange"), 2
        ),
        "londonAnnualChange": round(
            hpi_number(london[0], "percentageAnnualChange"), 2
        ),
        "londonPreviousAnnualChange": round(
            hpi_number(london[1], "percentageAnnualChange"), 2
        ),
        "observationMonth": observation_month,
        "retrievedAt": retrieved_at,
        "provisional": True,
        "sourceUrl": source_url,
    }


def mpc_summary_url(announcement_date: date) -> str:
    return (
        "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/"
        f"{announcement_date.year}/{announcement_date.strftime('%B').lower()}-"
        f"{announcement_date.year}"
    )


def next_decision(schedule: list[str], today: date) -> date:
    candidates = [iso_date(value) for value in schedule]
    result = next((value for value in candidates if value and value >= today), None)
    if not result:
        raise ValueError("MPC calendar has no current or future decision")
    return result


def latest_completed_decision(schedule: list[str], today: date) -> date:
    candidates = [iso_date(value) for value in schedule]
    result = next((value for value in reversed(candidates) if value and value < today), None)
    if not result:
        raise ValueError("MPC calendar has no completed decision")
    return result


def read_optional(path: Path | None) -> bytes | None:
    return path.read_bytes() if path else None


def collect_snapshot(
    existing: Mapping[str, Any],
    *,
    now: datetime,
    fetcher: Callable[[str], bytes] = fetch_bytes,
    policy_rate_csv: bytes | None = None,
    mortgage_csv: bytes | None = None,
    vote_html: bytes | None = None,
    uk_hpi_json: bytes | None = None,
    surrey_hpi_json: bytes | None = None,
    london_hpi_json: bytes | None = None,
) -> dict[str, Any]:
    validate_snapshot(existing)
    output = deepcopy(dict(existing))
    now_utc = now.astimezone(timezone.utc).replace(microsecond=0)
    collected_at = now_utc.isoformat().replace("+00:00", "Z")
    today = now.astimezone(ZoneInfo(TIME_ZONE)).date()
    stale: list[str] = []

    policy = output["policy"]
    policy_failed = False
    try:
        rate_payload = policy_rate_csv or fetcher(clean(policy["rateDownloadUrl"]))
        rate_rows = parse_boe_csv(rate_payload, "IUDBEDR")
        policy["bankRate"] = round(rate_rows[-1][1], 3)
        policy["observationDate"] = rate_rows[-1][0].isoformat()
    except (OSError, ValueError, KeyError):
        policy_failed = True
    try:
        completed = latest_completed_decision(policy["schedule"], today)
        summary_url = mpc_summary_url(completed)
        summary_payload = vote_html or fetcher(summary_url)
        vote = parse_vote_summary(summary_payload, completed)
        vote["sourceUrl"] = summary_url
        policy["latestVote"] = vote
    except (OSError, ValueError, KeyError):
        policy_failed = True
    try:
        policy["nextDecisionDate"] = next_decision(
            policy["schedule"], today
        ).isoformat()
    except (ValueError, KeyError):
        policy_failed = True
    if policy_failed:
        stale.append(POLICY_SOURCE_ID)

    mortgage = output["mortgage"]
    try:
        rate_payload = mortgage_csv or fetcher(clean(mortgage["downloadUrl"]))
        rate_rows = parse_boe_csv(rate_payload, "IUMBV34")
        latest_observation, latest_rate = rate_rows[-1]
        previous_rate = rate_rows[-2][1] if len(rate_rows) > 1 else None
        mortgage["rate"] = round(latest_rate, 3)
        mortgage["previousRate"] = (
            round(previous_rate, 3) if previous_rate is not None else None
        )
        mortgage["observationDate"] = latest_observation.isoformat()
        mortgage["retrievedAt"] = collected_at
    except (OSError, ValueError, KeyError):
        stale.append(MORTGAGE_SOURCE_ID)

    market = output["market"]
    try:
        uk_payload = uk_hpi_json or fetcher(HPI_URL.format(region="united-kingdom"))
        surrey_payload = surrey_hpi_json or fetcher(HPI_URL.format(region="surrey"))
        london_payload = london_hpi_json or fetcher(HPI_URL.format(region="london"))
        output["market"] = parse_hpi_market(
            uk_payload,
            surrey_payload,
            london_payload,
            retrieved_at=collected_at,
            source_url=HPI_URL.format(region="united-kingdom"),
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        stale.append(MARKET_SOURCE_ID)

    output["collectedAt"] = collected_at
    output["collectionStatus"] = {
        "mode": "live" if not stale else "last-known-good",
        "staleSources": sorted(set(stale)),
    }
    validate_snapshot(output)
    return output


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--generated-at", help="Reproducible ISO-8601 collection timestamp.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--bank-rate-csv", type=Path)
    parser.add_argument("--mortgage-csv", type=Path)
    parser.add_argument("--mpc-summary-html", type=Path)
    parser.add_argument("--uk-hpi-json", type=Path)
    parser.add_argument("--surrey-hpi-json", type=Path)
    parser.add_argument("--london-hpi-json", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    now = (
        datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
        if args.generated_at
        else datetime.now(timezone.utc)
    )
    if not now.tzinfo:
        now = now.replace(tzinfo=timezone.utc)

    def fetch_with_timeout(url: str) -> bytes:
        return fetch_bytes(url, timeout=args.timeout)

    snapshot = collect_snapshot(
        load_snapshot(args.snapshot),
        now=now,
        fetcher=fetch_with_timeout,
        policy_rate_csv=read_optional(args.bank_rate_csv),
        mortgage_csv=read_optional(args.mortgage_csv),
        vote_html=read_optional(args.mpc_summary_html),
        uk_hpi_json=read_optional(args.uk_hpi_json),
        surrey_hpi_json=read_optional(args.surrey_hpi_json),
        london_hpi_json=read_optional(args.london_hpi_json),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "collectedAt": snapshot["collectedAt"],
                "bankRate": snapshot["policy"]["bankRate"],
                "mortgageRate": snapshot["mortgage"]["rate"],
                "hpiMonth": snapshot["market"]["observationMonth"],
                "staleSources": snapshot["collectionStatus"]["staleSources"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, InsightViewValidationError) as error:
        print(f"INSIGHT View collection failed: {error}", file=sys.stderr)
        raise SystemExit(1)
