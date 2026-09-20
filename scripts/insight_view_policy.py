"""Official MPC evidence shared by collection, polling and freshness checks.

The year/weekday and horizon rules adapt the app's governed mpc_calendar.py.
Future dates come from the official table, not a union with superseded dates.
"""

from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from typing import Any, Mapping


CALENDAR_URL = "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates"
MINIMUM_FUTURE_DECISIONS = 2
MINIMUM_HORIZON_DAYS = 120
MONTHS = {name.lower(): i for i, name in enumerate(
    ("", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December")) if name}
DATE_PATTERN = r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(20\d{2})"


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class _OfficialEvidence(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.year: int | None = None
        self.heading: list[str] | None = None
        self.cells: list[list[str]] | None = None
        self.cell: list[str] | None = None
        self.rows: list[tuple[int, list[str]]] = []
        self.published: list[str] = []
        self.publication: list[str] | None = None
        self.publication_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h2":
            self.heading = []
        if tag == "tr" and self.year is not None:
            self.cells = []
        if tag == "td" and self.cells is not None:
            self.cell = []
        if tag == "div":
            if self.publication is not None:
                self.publication_depth += 1
            elif "published-date" in (attributes.get("class") or "").split():
                self.publication = []
                self.publication_depth = 1

    def handle_data(self, data: str) -> None:
        for target in (self.heading, self.cell, self.publication):
            if target is not None:
                target.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self.heading is not None:
            match = re.fullmatch(r"(20\d{2})\s+(?:confirmed|provisional)\s+dates",
                                 _text(" ".join(self.heading)), re.I)
            self.year = int(match.group(1)) if match else None
            self.heading = None
        if tag == "td" and self.cell is not None:
            self.cells.append(_text(" ".join(self.cell)))
            self.cell = None
        if tag == "tr" and self.cells is not None:
            if self.cells:
                self.rows.append((self.year, self.cells))
            self.cells = None
        if tag == "div" and self.publication is not None:
            self.publication_depth -= 1
            if not self.publication_depth:
                self.published.append(_text(" ".join(self.publication)))
                self.publication = None


def official_evidence(payload: bytes | str) -> _OfficialEvidence:
    parser = _OfficialEvidence()
    parser.feed(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
    parser.close()
    return parser


def parse_official_dates(payload: bytes | str) -> list[date]:
    parsed = []
    for year, cells in official_evidence(payload).rows:
        if len(cells) < 2 or not re.search(r"(?:monetary policy|MPC) summary", cells[1], re.I):
            raise ValueError("Official MPC calendar row has no policy-summary evidence")
        match = re.fullmatch(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
                             r"\s+(\d{1,2})\s+(" + "|".join(MONTHS) + r")", cells[0], re.I)
        if not match:
            raise ValueError("Official MPC calendar row has an unsupported date")
        weekday, day, month = match.groups()
        value = date(year, MONTHS[month.lower()], int(day))
        if value.strftime("%A").casefold() != weekday.casefold():
            raise ValueError("Official MPC calendar weekday does not match its date")
        parsed.append(value)
    if not parsed or len(parsed) != len(set(parsed)):
        raise ValueError("Official MPC calendar must contain unique evidenced dates")
    if any(not 8 <= sum(value.year == year for value in parsed) <= 12
           for year in {value.year for value in parsed}):
        raise ValueError("Official MPC calendar must contain complete annual decision tables")
    return sorted(parsed)


def refresh_schedule(existing: list[str], payload: bytes | str, today: date) -> list[str]:
    official = parse_official_dates(payload)
    future = [value for value in official if value >= today]
    if (len(future) < MINIMUM_FUTURE_DECISIONS
            or (future[-1] - today).days < MINIMUM_HORIZON_DAYS
            or any(value.year > today.year + 3 for value in official)):
        raise ValueError("Official MPC calendar has insufficient or implausible future coverage")
    # Replace every supplied year, including a postponed date already past.
    # Retain completed history only for years no longer present on the page.
    official_years = {value.year for value in official}
    expected_years = {date.fromisoformat(value).year for value in existing
                      if date.fromisoformat(value) >= today}
    if not expected_years.issubset(official_years):
        raise ValueError("Official MPC calendar omitted a year with remaining decisions")
    history = {value for value in existing
               if date.fromisoformat(value) < today
               and date.fromisoformat(value).year not in official_years}
    return sorted(history | {value.isoformat() for value in official})


def announcement_rate(payload: bytes | str, expected_date: date, plain_text: str) -> float:
    publications = official_evidence(payload).published
    if len(publications) != 1:
        raise ValueError("MPC announcement must have one official publication date")
    match = re.fullmatch(r"Published on\s+" + DATE_PATTERN, publications[0], re.I)
    if not match:
        raise ValueError("MPC announcement publication date is unsupported")
    day, month, year = match.groups()
    if date(int(year), MONTHS[month.lower()], int(day)) != expected_date:
        raise ValueError("MPC announcement does not match the due decision")
    # Bind the rate to the majority/unanimous clause. Minority alternatives and
    # the previous day's meeting-ending date are not the announced policy rate.
    rate = re.search(
        r"voted (?:by a majority of\s+\d+\s*[–—-]\s*\d+|unanimously)\s+to\s+"
        r"(?:maintain|reduce|increase)\s+Bank Rate"
        r"(?:\s+by\s+\d+(?:\.\d+)?\s+percentage points)?\s*,?\s*(?:at|to)\s+"
        r"(\d+(?:\.\d+)?)\s*%", plain_text, re.I)
    if not rate or not 0 <= float(rate.group(1)) <= 25:
        raise ValueError("MPC announcement has no supported majority policy rate")
    return float(rate.group(1))


def validate_observation_date(value: date, previous: Any, today: date) -> None:
    if value > today or (previous and value < date.fromisoformat(str(previous))):
        raise ValueError("Official observation date is future or regressive")


def policy_is_pending(snapshot: Mapping[str, Any], completed: date) -> bool:
    policy = snapshot["policy"]
    return (policy["latestVote"]["announcementDate"] != completed.isoformat()
            or policy["observationDate"] < completed.isoformat())
