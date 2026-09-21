#!/usr/bin/env python3
"""Publication cadence for the monthly HM Land Registry UK HPI source."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Mapping
from zoneinfo import ZoneInfo


TIME_ZONE = "Europe/London"
HPI_RELEASE_TIME = time(9, 30)
HPI_CALENDAR_URL = (
    "https://www.gov.uk/government/publications/about-the-uk-house-price-index/"
    "about-the-uk-house-price-index"
)

# Official calendar published by HM Land Registry. Observation months are kept
# separate from publication dates because UK HPI is released with a time lag.
# Keep this horizon aligned with HPI_CALENDAR_URL.
HPI_RELEASE_DATES = (
    ("2026-07", "2026-09-16"),
    ("2026-08", "2026-10-21"),
    ("2026-09", "2026-11-18"),
    ("2026-10", "2026-12-16"),
    ("2026-11", "2027-01-20"),
    ("2026-12", "2027-02-17"),
    ("2027-01", "2027-03-24"),
    ("2027-02", "2027-04-21"),
    ("2027-03", "2027-05-19"),
    ("2027-04", "2027-06-16"),
    ("2027-05", "2027-07-21"),
    ("2027-06", "2027-08-18"),
    ("2027-07", "2027-09-15"),
    ("2027-08", "2027-10-20"),
    ("2027-09", "2027-11-17"),
    ("2027-10", "2027-12-15"),
    ("2027-11", "2028-01-19"),
)


def expected_hpi_observation_month(now: datetime) -> str | None:
    """Return the newest HPI month officially due at ``now``."""
    if not now.tzinfo:
        raise ValueError("HPI cadence reference time must include a timezone")
    local_now = now.astimezone(ZoneInfo(TIME_ZONE))
    expected = None
    for observation_month, publication_day in HPI_RELEASE_DATES:
        published_at = datetime.combine(
            datetime.fromisoformat(publication_day).date(),
            HPI_RELEASE_TIME,
            tzinfo=ZoneInfo(TIME_ZONE),
        )
        if published_at <= local_now:
            expected = observation_month
    return expected


def hpi_refresh_due(snapshot: Mapping[str, Any], now: datetime) -> bool:
    """Only request HPI when its official calendar says a newer month is due."""
    expected = expected_hpi_observation_month(now)
    observed = str(snapshot.get("market", {}).get("observationMonth", ""))
    return bool(expected and observed < expected)
