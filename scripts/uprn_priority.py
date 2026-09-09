"""One source order for server-side property identifier consumers."""

from functools import lru_cache
from pathlib import Path

from validate_property_uprn_links import parse_feed, validation_failures

PRIMARY_FEED = Path(__file__).resolve().parents[1] / "outputs/property-uprn-links.js"


@lru_cache(maxsize=8)
def _load(path: str, modified_ns: int, size: int) -> dict:
    feed = parse_feed(Path(path))
    failures = validation_failures(feed, set(feed.get("linksByProperty", {})))
    if failures:
        raise ValueError("Invalid primary UPRN feed: " + "; ".join(failures))
    return {key: link for key, link in feed["linksByProperty"].items()
            if str(link["sourceId"]).startswith("hmlr_ppd_uprn_")
            and link["matchStatus"] == "confirmed_address_match"
            and link["evidenceTier"] == "authoritative_address_source"}


def primary_links(path: Path = PRIMARY_FEED) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    stat = path.stat()
    return _load(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def preferred_property_uprn(item: dict, primary: dict | None = None) -> str:
    links = primary_links() if primary is None else primary
    link = links.get(item.get("propertyRecordId"))
    if link:
        return link["uprn"]
    return str(item.get("uprn") or (item.get("ordnanceSurvey") or {}).get("uprn") or "").strip()
