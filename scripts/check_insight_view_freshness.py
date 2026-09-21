#!/usr/bin/env python3
"""Choose automatic refresh work and verify the published View against its sources."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from collect_insight_view import latest_completed_decision, next_decision
from insight_view import POLICY_SOURCE_ID, TIME_ZONE, iso_datetime, load_snapshot
from insight_view_policy import MINIMUM_FUTURE_DECISIONS, MINIMUM_HORIZON_DAYS, policy_is_pending
from insight_view_hpi_policy import hpi_refresh_due
from validate_insight_view import read_insight_view, validate


ROOT = Path(__file__).resolve().parents[1]
FAST_SCHEDULE = "*/5 11-14 * * *"
HPI_SCHEDULE = "35 9 * * *"
DAILY_SCHEDULES = {"7 0 * * *", "0 6 * * *"}


def refresh_mode(snapshot: Mapping[str, Any], view: Mapping[str, Any] | None,
                 now: datetime, event_name: str, event_schedule: str) -> str:
    local = now.astimezone(ZoneInfo(TIME_ZONE))
    today = local.date().isoformat()
    policy = snapshot["policy"]
    stale = snapshot["collectionStatus"]["staleSources"]
    hpi_due = hpi_refresh_due(snapshot, now)
    if event_name == "workflow_dispatch" or hpi_due:
        return "all"
    if "hm-land-registry-uk-hpi" in stale:
        return "all"
    if (event_name != "schedule" or event_schedule in DAILY_SCHEDULES
            or not view or view.get("briefingDate") != today):
        return "routine"
    due = latest_completed_decision(policy["schedule"], now, policy["nextDecisionTime"])
    pending = policy_is_pending(snapshot, due)
    if event_schedule == FAST_SCHEDULE:
        if (due.isoformat() == today and local.time().replace(tzinfo=None) >=
                time.fromisoformat(policy["nextDecisionTime"]) and
                (pending or POLICY_SOURCE_ID in stale)):
            return "mpc"
        return "skip"
    if event_schedule == HPI_SCHEDULE:
        return "skip"
    collected = iso_datetime(snapshot["collectedAt"])
    if stale or pending or now - collected >= timedelta(hours=6) or collected > now:
        return "routine"
    return "skip"


def freshness_issues(snapshot: Mapping[str, Any], view: Mapping[str, Any],
                     now: datetime) -> list[str]:
    local = now.astimezone(ZoneInfo(TIME_ZONE))
    today = local.date()
    issues = []
    if view["briefingDate"] != today.isoformat():
        issues.append("published briefing is not dated today in Europe/London")
    collected = iso_datetime(snapshot["collectedAt"])
    if collected > now + timedelta(minutes=5) or now - collected > timedelta(hours=8):
        issues.append("official source collection is future dated or more than eight hours old")
    if iso_datetime(view["generatedAt"]) > now + timedelta(minutes=5):
        issues.append("published briefing generation is in the future")
    stale = snapshot["collectionStatus"]["staleSources"]
    if stale:
        issues.append("official sources remain incomplete: " + ", ".join(stale))
    if hpi_refresh_due(snapshot, now):
        issues.append("latest scheduled HPI observation has not been collected")
    if set(view["staleSources"]) != set(stale):
        issues.append("published source status differs from the collected snapshot")
    policy = snapshot["policy"]
    for field in ("bankRate", "observationDate", "nextDecisionDate", "nextDecisionTime"):
        public_field = "bankRateObservedOn" if field == "observationDate" else field
        if view["policy"][public_field] != policy[field]:
            issues.append("published policy differs from the collected snapshot")
            break
    vote = {key: value for key, value in policy["latestVote"].items() if key != "sourceUrl"}
    if view["policy"]["latestVote"] != vote:
        issues.append("published MPC result differs from the collected snapshot")
    due = latest_completed_decision(policy["schedule"], now, policy["nextDecisionTime"])
    if policy_is_pending(snapshot, due):
        issues.append("latest scheduled MPC decision has not been coherently observed")
    try:
        expected = next_decision(policy["schedule"], today,
                                 datetime.fromisoformat(policy["latestVote"]["announcementDate"]).date())
        if policy["nextDecisionDate"] != expected.isoformat():
            issues.append("next MPC decision does not reconcile with the observed result")
    except ValueError:
        issues.append("official MPC calendar has no next decision")
    future = [datetime.fromisoformat(value).date() for value in policy["schedule"]
              if value >= today.isoformat()]
    if (len(future) < MINIMUM_FUTURE_DECISIONS or
            (future[-1] - today).days < MINIMUM_HORIZON_DAYS):
        issues.append("official MPC calendar needs a longer verified horizon")
    for section, fields in (("mortgage", ("rate", "previousRate", "observationDate")),
                            ("market", ("observationMonth", "ukAveragePrice", "ukAnnualChange",
                                        "surreyAveragePrice", "surreyAnnualChange"))):
        if any(view[section][field] != snapshot[section][field] for field in fields):
            issues.append(f"published {section} differs from the collected snapshot")
    for observed in (policy["observationDate"], policy["latestVote"]["announcementDate"],
                     snapshot["mortgage"]["observationDate"]):
        if observed > today.isoformat():
            issues.append("official observation is future dated")
            break
    if snapshot["market"]["observationMonth"] > today.strftime("%Y-%m"):
        issues.append("official HPI observation is future dated")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "config/insight-view-snapshot.json")
    parser.add_argument("--feed", type=Path, default=ROOT / "outputs/insight-view.js")
    parser.add_argument("--now", help="Explicit UTC reference time for reproducible tests")
    parser.add_argument("--cadence", action="store_true")
    parser.add_argument("--event-name", default="schedule")
    parser.add_argument("--event-schedule", default="17 * * * *")
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Reference time must include a timezone")
    snapshot = load_snapshot(args.snapshot)
    if args.cadence:
        try:
            view, _ = read_insight_view(args.feed)
        except (OSError, ValueError):
            view = None
        print(refresh_mode(snapshot, view, now, args.event_name, args.event_schedule))
        return 0
    view, _ = validate(args.feed)
    issues = freshness_issues(snapshot, view, now)
    print(json.dumps({"status": "incomplete" if issues else "healthy", "issues": issues,
                      "checkedAt": now.isoformat(), "sourceCollectedAt": snapshot["collectedAt"],
                      "briefingDate": view["briefingDate"]}, sort_keys=True))
    return 1 if issues else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"INSIGHT View freshness check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
