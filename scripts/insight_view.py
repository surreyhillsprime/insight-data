#!/usr/bin/env python3
"""Build and validate the governed daily INSIGHT View briefing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
GENERATOR_VERSION = "insight-view-1"
VIEW_NAME = "INSIGHT_VIEW"
META_NAME = "INSIGHT_VIEW_META"
TIME_ZONE = "Europe/London"
HEADING = "INSIGHT View"
CATEGORY = "Rates and market"
POLICY_SOURCE_ID = "bank-of-england-mpc"
MORTGAGE_SOURCE_ID = "bank-of-england-iumbv34"
MARKET_SOURCE_ID = "hm-land-registry-uk-hpi"
ALLOWED_OUTCOMES = {"hold", "cut", "raise"}
ALLOWED_ALTERNATIVES = ALLOWED_OUTCOMES | {"none"}
ALLOWED_TRENDS = {"up", "down", "unchanged", "unknown"}


class InsightViewValidationError(ValueError):
    """Raised when a daily briefing violates its publication contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(clean(value)[:10])
    except (TypeError, ValueError):
        return None


def iso_datetime(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise InsightViewValidationError(f"{label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise InsightViewValidationError(f"{label} must be numeric") from error
    if not math.isfinite(parsed):
        raise InsightViewValidationError(f"{label} must be finite")
    return parsed


def integer(value: Any, label: str) -> int:
    parsed = number(value, label)
    if not parsed.is_integer():
        raise InsightViewValidationError(f"{label} must be a whole number")
    return int(parsed)


def load_snapshot(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InsightViewValidationError("INSIGHT View snapshot must be an object")
    validate_snapshot(value)
    return value


def validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("schemaVersion") != SCHEMA_VERSION:
        raise InsightViewValidationError(
            f"INSIGHT View snapshot schemaVersion must be {SCHEMA_VERSION}"
        )
    if snapshot.get("timeZone") != TIME_ZONE:
        raise InsightViewValidationError(f"INSIGHT View timeZone must be {TIME_ZONE}")
    if not iso_datetime(snapshot.get("collectedAt")):
        raise InsightViewValidationError("INSIGHT View collectedAt must be ISO-8601")

    policy = snapshot.get("policy")
    mortgage = snapshot.get("mortgage")
    market = snapshot.get("market")
    if not all(isinstance(value, Mapping) for value in (policy, mortgage, market)):
        raise InsightViewValidationError("Policy, mortgage and market snapshots are required")

    bank_rate = number(policy.get("bankRate"), "policy.bankRate")
    if not 0 <= bank_rate <= 25:
        raise InsightViewValidationError("policy.bankRate is outside a credible range")
    if not iso_date(policy.get("observationDate")):
        raise InsightViewValidationError("policy.observationDate must be YYYY-MM-DD")
    next_decision = iso_date(policy.get("nextDecisionDate"))
    if not next_decision:
        raise InsightViewValidationError("policy.nextDecisionDate must be YYYY-MM-DD")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clean(policy.get("nextDecisionTime"))):
        raise InsightViewValidationError("policy.nextDecisionTime must be HH:MM")
    if not clean(policy.get("sourceUrl")).startswith("https://"):
        raise InsightViewValidationError("policy.sourceUrl must use HTTPS")
    if clean(policy.get("rateSeriesId")) != "IUDBEDR":
        raise InsightViewValidationError("policy.rateSeriesId must be IUDBEDR")
    if not clean(policy.get("rateDownloadUrl")).startswith("https://"):
        raise InsightViewValidationError("policy.rateDownloadUrl must use HTTPS")
    schedule = policy.get("schedule")
    if (
        not isinstance(schedule, list)
        or not schedule
        or any(not iso_date(value) for value in schedule)
        or schedule != sorted(set(schedule))
        or clean(policy.get("nextDecisionDate")) not in schedule
    ):
        raise InsightViewValidationError("policy.schedule must be sorted, unique and include the next decision")

    vote = policy.get("latestVote")
    if not isinstance(vote, Mapping):
        raise InsightViewValidationError("policy.latestVote is required")
    if not iso_date(vote.get("announcementDate")):
        raise InsightViewValidationError("policy.latestVote.announcementDate must be YYYY-MM-DD")
    if vote.get("outcome") not in ALLOWED_OUTCOMES:
        raise InsightViewValidationError("policy.latestVote.outcome is invalid")
    allowed_alternatives = ALLOWED_ALTERNATIVES - {vote.get("outcome")}
    if vote.get("alternative") not in allowed_alternatives:
        raise InsightViewValidationError("policy.latestVote.alternative is invalid")
    votes_for = integer(vote.get("for"), "policy.latestVote.for")
    votes_against = integer(vote.get("against"), "policy.latestVote.against")
    if votes_for < 1 or votes_against < 0 or votes_for + votes_against > 12:
        raise InsightViewValidationError("policy.latestVote vote split is invalid")
    if not clean(vote.get("sourceUrl")).startswith("https://"):
        raise InsightViewValidationError("policy.latestVote.sourceUrl must use HTTPS")

    mortgage_rate = number(mortgage.get("rate"), "mortgage.rate")
    if not 0 < mortgage_rate <= 25:
        raise InsightViewValidationError("mortgage.rate is outside a credible range")
    previous_rate = mortgage.get("previousRate")
    if previous_rate is not None and not 0 < number(previous_rate, "mortgage.previousRate") <= 25:
        raise InsightViewValidationError("mortgage.previousRate is outside a credible range")
    if clean(mortgage.get("seriesId")) != "IUMBV34":
        raise InsightViewValidationError("mortgage.seriesId must be IUMBV34")
    if not iso_date(mortgage.get("observationDate")):
        raise InsightViewValidationError("mortgage.observationDate must be YYYY-MM-DD")
    if not iso_datetime(mortgage.get("retrievedAt")):
        raise InsightViewValidationError("mortgage.retrievedAt must be ISO-8601")
    for field in ("sourceUrl", "downloadUrl"):
        if not clean(mortgage.get(field)).startswith("https://"):
            raise InsightViewValidationError(f"mortgage.{field} must use HTTPS")
    if "75% LTV" not in clean(mortgage.get("qualifier")):
        raise InsightViewValidationError("mortgage qualifier must disclose the 75% LTV basis")

    uk_price = integer(market.get("ukAveragePrice"), "market.ukAveragePrice")
    if not 50_000 <= uk_price <= 2_000_000:
        raise InsightViewValidationError("market.ukAveragePrice is outside a credible range")
    for field in (
        "ukAnnualChange",
        "ukPreviousAnnualChange",
        "ukMonthlyChange",
        "surreyAnnualChange",
        "surreyMonthlyChange",
        "londonAnnualChange",
        "londonPreviousAnnualChange",
    ):
        value = number(market.get(field), f"market.{field}")
        if not -50 <= value <= 50:
            raise InsightViewValidationError(f"market.{field} is outside a credible range")
    surrey_price = integer(market.get("surreyAveragePrice"), "market.surreyAveragePrice")
    if not 100_000 <= surrey_price <= 5_000_000:
        raise InsightViewValidationError("market.surreyAveragePrice is outside a credible range")
    if not re.fullmatch(r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", clean(market.get("observationMonth"))):
        raise InsightViewValidationError("market.observationMonth must be YYYY-MM")
    if not iso_datetime(market.get("retrievedAt")):
        raise InsightViewValidationError("market.retrievedAt must be ISO-8601")
    if not isinstance(market.get("provisional"), bool):
        raise InsightViewValidationError("market.provisional must be boolean")
    if not clean(market.get("sourceUrl")).startswith("https://"):
        raise InsightViewValidationError("market.sourceUrl must use HTTPS")

    status = snapshot.get("collectionStatus")
    if not isinstance(status, Mapping) or not isinstance(status.get("staleSources"), list):
        raise InsightViewValidationError("collectionStatus.staleSources must be an array")
    allowed_source_ids = {POLICY_SOURCE_ID, MORTGAGE_SOURCE_ID, MARKET_SOURCE_ID}
    if any(value not in allowed_source_ids for value in status.get("staleSources", [])):
        raise InsightViewValidationError("collectionStatus contains an unknown source id")


def policy_signal(outcome: str) -> tuple[str, str]:
    return {
        "hold": ("Hold favoured", "hold"),
        "cut": ("Cut favoured", "cut"),
        "raise": ("Rise favoured", "raise"),
    }[outcome]


def mortgage_trend(rate: float, previous_rate: Any) -> str:
    if previous_rate is None:
        return "unknown"
    delta = rate - float(previous_rate)
    if delta > 0.005:
        return "up"
    if delta < -0.005:
        return "down"
    return "unchanged"


def days_until(briefing_date: date, decision_date: date) -> int:
    return (decision_date - briefing_date).days


def decision_sentence(briefing_date: date, decision_date: date, decision_time: str) -> str:
    remaining = days_until(briefing_date, decision_date)
    time_label = "noon" if decision_time == "12:00" else decision_time
    if remaining == 0:
        return f"The next MPC decision is due today at {time_label}."
    if remaining == 1:
        return f"The next MPC decision is tomorrow at {time_label}."
    return f"There are {remaining} days until the next MPC decision at {time_label}."


def movement_sentence(rate: float, previous_rate: Any) -> str:
    trend = mortgage_trend(rate, previous_rate)
    if trend == "unknown":
        return ""
    previous = float(previous_rate)
    if trend == "unchanged":
        return f"It is unchanged from {previous:.2f}% in the previous monthly observation."
    direction = "up" if trend == "up" else "down"
    return f"It is {direction} from {previous:.2f}% in the previous monthly observation."


def market_direction(current: float, previous: float) -> str:
    if current < previous - 0.05:
        return f"annual growth has slowed from {previous:.1f}%"
    if current > previous + 0.05:
        return f"annual growth has strengthened from {previous:.1f}%"
    return f"annual growth is broadly unchanged from {previous:.1f}%"


def source_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    policy = snapshot["policy"]
    mortgage = snapshot["mortgage"]
    market = snapshot["market"]
    return [
        {
            "id": POLICY_SOURCE_ID,
            "name": "Bank of England",
            "url": clean(policy["sourceUrl"]),
            "observedAt": clean(policy["observationDate"]),
            "rights": "Open Government Licence v3.0",
        },
        {
            "id": MORTGAGE_SOURCE_ID,
            "name": "BoE IUMBV34",
            "url": clean(mortgage["sourceUrl"]),
            "observedAt": clean(mortgage["observationDate"]),
            "rights": "Open Government Licence v3.0",
        },
        {
            "id": MARKET_SOURCE_ID,
            "name": "UK HPI",
            "url": clean(market["sourceUrl"]),
            "observedAt": clean(market["observationMonth"]),
            "rights": "Open Government Licence v3.0",
        },
    ]


def market_news_synopsis(
    news_items: list[Mapping[str, Any]] | None,
    briefing_day: date,
) -> str:
    """Summarise fresh governed headlines without copying article bodies."""
    earliest = briefing_day - timedelta(days=6)
    eligible: list[tuple[datetime, int, Mapping[str, Any]]] = []
    for item in news_items or []:
        published = iso_datetime(item.get("publishedAt"))
        published_day = (
            published.astimezone(ZoneInfo(TIME_ZONE)).date() if published else None
        )
        if (
            not published_day
            or published_day < earliest
            or published_day > briefing_day
            or clean(item.get("rightsMode")) != "link-only"
            or not clean(item.get("title"))
            or not clean(item.get("source"))
        ):
            continue
        eligible.append((published, int(item.get("score") or 0), item))
    eligible.sort(key=lambda row: (row[0], row[1]), reverse=True)

    selected: list[Mapping[str, Any]] = []
    publisher_keys: set[str] = set()
    for _, _, item in eligible:
        publisher_key = clean(
            item.get("publisherGroup")
            or item.get("sourceId")
            or item.get("source")
        ).lower()
        if publisher_key and publisher_key not in publisher_keys:
            selected.append(item)
            publisher_keys.add(publisher_key)
        if len(selected) == 3:
            break
    if len(selected) < 3:
        for _, _, item in eligible:
            if item not in selected:
                selected.append(item)
            if len(selected) == 3:
                break

    if not selected:
        return (
            "There are no qualifying Market News updates within the last seven days. "
            "INSIGHT will keep checking the governed link-only sources; a quiet feed is "
            "unknown context, not negative property evidence."
        )

    clauses = [
        f"{clean(item['source'])}: “{clean(item['title']).rstrip('.')}”"
        for item in selected
    ]
    if len(clauses) == 1:
        updates = clauses[0]
    elif len(clauses) == 2:
        updates = " and ".join(clauses)
    else:
        updates = "; ".join(clauses[:-1]) + f"; and {clauses[-1]}"
    return (
        f"Relevant Market News updates include {updates}. "
        "These are sourced, link-only context; they do not constitute evidence of "
        "seller intent or create a property opportunity."
    )


def build_insight_view(
    snapshot: Mapping[str, Any],
    *,
    news_items: list[Mapping[str, Any]] | None = None,
    briefing_date: str | date | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    validate_snapshot(snapshot)
    collected = iso_datetime(snapshot["collectedAt"])
    if isinstance(briefing_date, date):
        briefing_day = briefing_date
    elif briefing_date:
        briefing_day = iso_date(briefing_date)
    else:
        briefing_day = collected.astimezone(ZoneInfo(TIME_ZONE)).date()
    if not briefing_day:
        raise InsightViewValidationError("briefing date must use YYYY-MM-DD")
    generated = iso_datetime(generated_at) if generated_at else collected
    if not generated:
        raise InsightViewValidationError("generatedAt must be ISO-8601")

    policy = snapshot["policy"]
    mortgage = snapshot["mortgage"]
    market = snapshot["market"]
    vote = policy["latestVote"]
    next_decision = iso_date(policy["nextDecisionDate"])
    if next_decision < briefing_day:
        raise InsightViewValidationError("next MPC decision cannot be before the briefing date")

    bank_rate = float(policy["bankRate"])
    fixed_rate = float(mortgage["rate"])
    previous_fixed = mortgage.get("previousRate")
    signal_value, outcome_word = policy_signal(clean(vote["outcome"]))
    spread = fixed_rate - bank_rate
    movement = movement_sentence(fixed_rate, previous_fixed)
    decision = decision_sentence(
        briefing_day,
        next_decision,
        clean(policy["nextDecisionTime"]),
    )
    vote_detail = (
        f"Latest MPC vote · {int(vote['for'])}–{int(vote['against'])} to {outcome_word}"
    )
    mortgage_relationship = (
        f"{abs(spread):.2f} percentage points {'above' if spread >= 0 else 'below'} "
        f"the {bank_rate:.2f}% policy rate"
    )
    first_paragraph = (
        "The cost of mortgage finance remains materially above Bank Rate. "
        f"The Bank of England’s quoted two-year fixed rate at 75% LTV is "
        f"{fixed_rate:.2f}%, {mortgage_relationship}. "
        f"{movement + ' ' if movement else ''}"
        f"{decision} The latest MPC vote favoured a {outcome_word} by "
        f"{int(vote['for'])}–{int(vote['against'])}, so the immediate signal is "
        f"{'steady policy' if outcome_word == 'hold' else outcome_word + ' pressure'} "
        "with borrowing costs still elevated; this is a recorded voting signal, not a forecast."
    )

    uk_change = float(market["ukAnnualChange"])
    uk_previous = float(market["ukPreviousAnnualChange"])
    london_change = float(market["londonAnnualChange"])
    market_phrase = market_direction(uk_change, uk_previous)
    london_phrase = (
        f"up {london_change:.1f}%"
        if london_change >= 0
        else f"down {abs(london_change):.1f}%"
    )
    surrey_change = float(market["surreyAnnualChange"])
    surrey_monthly = float(market["surreyMonthlyChange"])
    surrey_phrase = (
        f"{abs(surrey_change):.1f}% {'higher' if surrey_change >= 0 else 'lower'} "
        "than a year ago"
    )
    second_paragraph = (
        f"Nationally, average UK prices are {abs(uk_change):.1f}% "
        f"{'higher' if uk_change >= 0 else 'lower'} than a year ago at "
        f"£{int(market['ukAveragePrice']):,}, but {market_phrase}. "
        f"Surrey averages £{int(market['surreyAveragePrice']):,}, "
        f"{surrey_phrase} and {'up' if surrey_monthly >= 0 else 'down'} "
        f"{abs(surrey_monthly):.1f}% month on month, while London is "
        f"{london_phrase} year on year. That divergence argues for property-level "
        "and micro-market evidence rather than assuming the national headline applies evenly."
    )
    third_paragraph = market_news_synopsis(news_items, briefing_day)

    view = {
        "schemaVersion": SCHEMA_VERSION,
        "briefingDate": briefing_day.isoformat(),
        "generatedAt": generated.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "timeZone": TIME_ZONE,
        "heading": HEADING,
        "category": CATEGORY,
        "policy": {
            "bankRate": round(bank_rate, 3),
            "bankRateObservedOn": clean(policy["observationDate"]),
            "nextDecisionDate": next_decision.isoformat(),
            "nextDecisionTime": clean(policy["nextDecisionTime"]),
            "countdownDays": days_until(briefing_day, next_decision),
            "signalValue": signal_value,
            "signalDetail": vote_detail,
            "latestVote": {
                "announcementDate": clean(vote["announcementDate"]),
                "outcome": clean(vote["outcome"]),
                "for": int(vote["for"]),
                "against": int(vote["against"]),
                "alternative": clean(vote["alternative"]),
            },
            "sourceId": POLICY_SOURCE_ID,
        },
        "mortgage": {
            "rate": round(fixed_rate, 3),
            "previousRate": (
                round(float(previous_fixed), 3) if previous_fixed is not None else None
            ),
            "trend": mortgage_trend(fixed_rate, previous_fixed),
            "observationDate": clean(mortgage["observationDate"]),
            "retrievedAt": clean(mortgage["retrievedAt"]),
            "seriesId": clean(mortgage["seriesId"]),
            "qualifier": clean(mortgage["qualifier"]),
            "sourceId": MORTGAGE_SOURCE_ID,
        },
        "market": {
            "ukAveragePrice": int(market["ukAveragePrice"]),
            "ukAnnualChange": round(uk_change, 2),
            "ukPreviousAnnualChange": round(uk_previous, 2),
            "ukMonthlyChange": round(float(market["ukMonthlyChange"]), 2),
            "surreyAveragePrice": int(market["surreyAveragePrice"]),
            "surreyAnnualChange": round(surrey_change, 2),
            "surreyMonthlyChange": round(surrey_monthly, 2),
            "londonAnnualChange": round(london_change, 2),
            "londonPreviousAnnualChange": round(
                float(market["londonPreviousAnnualChange"]), 2
            ),
            "observationMonth": clean(market["observationMonth"]),
            "retrievedAt": clean(market["retrievedAt"]),
            "provisional": bool(market["provisional"]),
            "sourceId": MARKET_SOURCE_ID,
        },
        "narrative": [first_paragraph, second_paragraph, third_paragraph],
        "sources": source_rows(snapshot),
        "staleSources": list(snapshot["collectionStatus"]["staleSources"]),
        "limitations": [
            "The MPC signal reports the latest official vote; it is not a forecast of the next decision.",
            "The mortgage snapshot is the Bank of England quoted two-year fixed rate at 75% LTV, not a whole-market best-buy average.",
            "The latest UK HPI estimate is provisional and may be revised.",
        ],
    }
    view["fingerprint"] = hashlib.sha256(canonical_json(view).encode("utf-8")).hexdigest()
    validate_insight_view(view)
    return view


def insight_view_metadata(view: Mapping[str, Any]) -> dict[str, Any]:
    validate_insight_view(view)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "asOf": clean(view["briefingDate"]),
        "generatedAt": clean(view["generatedAt"]),
        "generatorVersion": GENERATOR_VERSION,
        "datasetFingerprint": clean(view["fingerprint"]),
        "sourceCount": len(view["sources"]),
        "staleSources": list(view["staleSources"]),
    }


def write_insight_view(path: str | Path, view: Mapping[str, Any]) -> None:
    metadata = insight_view_metadata(view)
    output = (
        f"window.{VIEW_NAME} = {canonical_json(dict(view))};\n"
        f"window.{META_NAME} = {canonical_json(metadata)};\n"
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(output, encoding="utf-8")


def validate_insight_view(view: Mapping[str, Any]) -> None:
    expected_keys = {
        "schemaVersion",
        "briefingDate",
        "generatedAt",
        "timeZone",
        "heading",
        "category",
        "policy",
        "mortgage",
        "market",
        "narrative",
        "sources",
        "staleSources",
        "limitations",
        "fingerprint",
    }
    if set(view) != expected_keys:
        raise InsightViewValidationError("INSIGHT View contains unexpected or missing fields")
    briefing_day = iso_date(view.get("briefingDate"))
    generated = iso_datetime(view.get("generatedAt"))
    if (
        view.get("schemaVersion") != SCHEMA_VERSION
        or view.get("timeZone") != TIME_ZONE
        or view.get("heading") != HEADING
        or view.get("category") != CATEGORY
        or not briefing_day
        or not generated
    ):
        raise InsightViewValidationError("INSIGHT View identity or dates are invalid")
    if generated.astimezone(ZoneInfo(TIME_ZONE)).date() != briefing_day:
        raise InsightViewValidationError(
            "INSIGHT View generatedAt must fall on its Europe/London briefing date"
        )
    policy = view.get("policy")
    mortgage = view.get("mortgage")
    market = view.get("market")
    if not all(isinstance(value, Mapping) for value in (policy, mortgage, market)):
        raise InsightViewValidationError("INSIGHT View snapshots are required")
    if (
        set(policy)
        != {
            "bankRate",
            "bankRateObservedOn",
            "nextDecisionDate",
            "nextDecisionTime",
            "countdownDays",
            "signalValue",
            "signalDetail",
            "latestVote",
            "sourceId",
        }
        or not 0 <= number(policy.get("bankRate"), "policy.bankRate") <= 25
        or not iso_date(policy.get("bankRateObservedOn"))
        or not iso_date(policy.get("nextDecisionDate"))
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clean(policy.get("nextDecisionTime")))
        or integer(policy.get("countdownDays"), "policy.countdownDays") < 0
        or clean(policy.get("signalValue")) not in {"Hold favoured", "Cut favoured", "Rise favoured"}
        or not clean(policy.get("signalDetail")).startswith("Latest MPC vote · ")
        or policy.get("sourceId") != POLICY_SOURCE_ID
    ):
        raise InsightViewValidationError("INSIGHT View policy snapshot is invalid")
    bank_rate_day = iso_date(policy.get("bankRateObservedOn"))
    decision_day = iso_date(policy.get("nextDecisionDate"))
    if (
        bank_rate_day > briefing_day
        or (briefing_day - bank_rate_day).days > 90
        or decision_day < briefing_day
        or integer(policy.get("countdownDays"), "policy.countdownDays")
        != (decision_day - briefing_day).days
    ):
        raise InsightViewValidationError(
            "INSIGHT View policy dates or decision countdown are stale or inconsistent"
        )
    vote = policy.get("latestVote")
    if (
        not isinstance(vote, Mapping)
        or set(vote) != {"announcementDate", "outcome", "for", "against", "alternative"}
        or vote.get("outcome") not in ALLOWED_OUTCOMES
        or vote.get("alternative") not in ALLOWED_ALTERNATIVES - {vote.get("outcome")}
        or not iso_date(vote.get("announcementDate"))
        or integer(vote.get("for"), "policy.latestVote.for") < 1
        or integer(vote.get("against"), "policy.latestVote.against") < 0
    ):
        raise InsightViewValidationError("INSIGHT View latest vote is invalid")
    vote_day = iso_date(vote.get("announcementDate"))
    expected_signal = {
        "hold": ("Hold favoured", "hold"),
        "cut": ("Cut favoured", "cut"),
        "raise": ("Rise favoured", "raise"),
    }[vote.get("outcome")]
    expected_detail = (
        f"Latest MPC vote · {integer(vote.get('for'), 'policy.latestVote.for')}"
        f"–{integer(vote.get('against'), 'policy.latestVote.against')} "
        f"to {expected_signal[1]}"
    )
    if (
        vote_day > briefing_day
        or (briefing_day - vote_day).days > 100
        or policy.get("signalValue") != expected_signal[0]
        or policy.get("signalDetail") != expected_detail
    ):
        raise InsightViewValidationError(
            "INSIGHT View policy signal does not reconcile with the latest vote"
        )
    if (
        set(mortgage)
        != {
            "rate",
            "previousRate",
            "trend",
            "observationDate",
            "retrievedAt",
            "seriesId",
            "qualifier",
            "sourceId",
        }
        or not 0 < number(mortgage.get("rate"), "mortgage.rate") <= 25
        or (
            mortgage.get("previousRate") is not None
            and not 0 < number(mortgage.get("previousRate"), "mortgage.previousRate") <= 25
        )
        or mortgage.get("trend") not in ALLOWED_TRENDS
        or not iso_date(mortgage.get("observationDate"))
        or not iso_datetime(mortgage.get("retrievedAt"))
        or mortgage.get("seriesId") != "IUMBV34"
        or "75% LTV" not in clean(mortgage.get("qualifier"))
        or mortgage.get("sourceId") != MORTGAGE_SOURCE_ID
    ):
        raise InsightViewValidationError("INSIGHT View mortgage snapshot is invalid")
    mortgage_day = iso_date(mortgage.get("observationDate"))
    if (
        mortgage_day > briefing_day
        or (briefing_day - mortgage_day).days > 100
        or mortgage.get("trend")
        != mortgage_trend(
            number(mortgage.get("rate"), "mortgage.rate"),
            mortgage.get("previousRate"),
        )
    ):
        raise InsightViewValidationError(
            "INSIGHT View mortgage observation is stale or its trend is inconsistent"
        )
    if (
        set(market)
        != {
            "ukAveragePrice",
            "ukAnnualChange",
            "ukPreviousAnnualChange",
            "ukMonthlyChange",
            "surreyAveragePrice",
            "surreyAnnualChange",
            "surreyMonthlyChange",
            "londonAnnualChange",
            "londonPreviousAnnualChange",
            "observationMonth",
            "retrievedAt",
            "provisional",
            "sourceId",
        }
        or not 50_000 <= integer(market.get("ukAveragePrice"), "market.ukAveragePrice") <= 2_000_000
        or not re.fullmatch(r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", clean(market.get("observationMonth")))
        or not iso_datetime(market.get("retrievedAt"))
        or not isinstance(market.get("provisional"), bool)
        or market.get("sourceId") != MARKET_SOURCE_ID
    ):
        raise InsightViewValidationError("INSIGHT View market snapshot is invalid")
    market_month = date.fromisoformat(f"{market.get('observationMonth')}-01")
    briefing_month = briefing_day.replace(day=1)
    if (
        market_month > briefing_month
        or (briefing_day - market_month).days > 150
    ):
        raise InsightViewValidationError(
            "INSIGHT View UK HPI observation is stale or in the future"
        )
    for field in (
        "ukAnnualChange",
        "ukPreviousAnnualChange",
        "ukMonthlyChange",
        "surreyAnnualChange",
        "surreyMonthlyChange",
        "londonAnnualChange",
        "londonPreviousAnnualChange",
    ):
        if not -50 <= number(market.get(field), f"market.{field}") <= 50:
            raise InsightViewValidationError(f"INSIGHT View market.{field} is invalid")
    if not 100_000 <= integer(market.get("surreyAveragePrice"), "market.surreyAveragePrice") <= 5_000_000:
        raise InsightViewValidationError("INSIGHT View market.surreyAveragePrice is invalid")
    narrative = view.get("narrative")
    if (
        not isinstance(narrative, list)
        or len(narrative) != 3
        or any(not 80 <= len(clean(value)) <= 900 for value in narrative)
    ):
        raise InsightViewValidationError("INSIGHT View must contain three concise analysis paragraphs")
    sources = view.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        raise InsightViewValidationError("INSIGHT View must contain three governed sources")
    source_ids = []
    for source in sources:
        if (
            not isinstance(source, Mapping)
            or set(source) != {"id", "name", "url", "observedAt", "rights"}
            or not clean(source.get("url")).startswith("https://")
            or source.get("rights") != "Open Government Licence v3.0"
        ):
            raise InsightViewValidationError("INSIGHT View source provenance is invalid")
        source_ids.append(source.get("id"))
    if source_ids != [POLICY_SOURCE_ID, MORTGAGE_SOURCE_ID, MARKET_SOURCE_ID]:
        raise InsightViewValidationError("INSIGHT View sources are missing or out of order")
    if [
        clean(source.get("observedAt"))
        for source in sources
    ] != [
        clean(policy.get("bankRateObservedOn")),
        clean(mortgage.get("observationDate")),
        clean(market.get("observationMonth")),
    ]:
        raise InsightViewValidationError(
            "INSIGHT View source observation dates do not reconcile"
        )
    stale = view.get("staleSources")
    if (
        not isinstance(stale, list)
        or stale != sorted(set(stale))
        or any(value not in source_ids for value in stale)
    ):
        raise InsightViewValidationError("INSIGHT View staleSources is invalid")
    limitations = view.get("limitations")
    if not isinstance(limitations, list) or len(limitations) != 3 or any(not clean(value) for value in limitations):
        raise InsightViewValidationError("INSIGHT View limitations are required")
    fingerprint = clean(view.get("fingerprint")).lower()
    unsigned = dict(view)
    unsigned.pop("fingerprint", None)
    expected = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if fingerprint != expected:
        raise InsightViewValidationError("INSIGHT View fingerprint is invalid")
