#!/usr/bin/env python3
"""Fetch and normalise approved INSIGHT RSS, Atom and news-sitemap sources."""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, time as datetime_time, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

from news_sources import (
    FETCHABLE_ADAPTERS,
    NewsSourceValidationError,
    article_url_is_allowed,
    assert_source_collectable,
)


USER_AGENT = "INSIGHT Surrey property intelligence/2.0 (+link-only news metadata)"
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
MAX_FETCH_ATTEMPTS = 5


class NewsAdapterError(ValueError):
    """Raised when an approved feed cannot be fetched or safely parsed."""


class _RetryableFetchError(RuntimeError):
    pass


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _child_text(element: ET.Element, names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if _local_name(child.tag) in wanted:
            value = _text(child)
            if value:
                return value
    return ""


def _descendant_text(element: ET.Element, names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if child is not element and _local_name(child.tag) in wanted:
            value = _text(child)
            if value:
                return value
    return ""


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.combine(parsed.date(), datetime_time(), tzinfo=timezone.utc)
        else:
            return None
    return parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _entry_link(element: ET.Element, adapter: str) -> str:
    if adapter == "news-sitemap":
        return _child_text(element, ("loc",))
    direct = _child_text(element, ("link",))
    if direct:
        return direct
    alternate = ""
    fallback = ""
    for child in list(element):
        if _local_name(child.tag) != "link" or not child.attrib.get("href"):
            continue
        href = str(child.attrib["href"]).strip()
        if not fallback:
            fallback = href
        if child.attrib.get("rel", "alternate").lower() == "alternate":
            alternate = href
            break
    return alternate or fallback


def _canonical_article_url(source: Mapping[str, Any], raw_url: str) -> tuple[str, str]:
    if not raw_url:
        return "", "missing-link"
    absolute = urljoin(str(source.get("url") or ""), raw_url.strip())
    try:
        parts = urlsplit(absolute)
    except ValueError:
        return "", "invalid-link"
    if parts.scheme != "https":
        return "", "insecure-link"
    if not article_url_is_allowed(source, absolute):
        return "", "wrong-domain-link"
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    canonical = urlunsplit(("https", parts.netloc.lower(), path, parts.query, ""))
    return canonical, ""


def _feed_elements(root: ET.Element, adapter: str) -> list[ET.Element]:
    root_name = _local_name(root.tag)
    if adapter == "rss":
        if root_name not in {"rss", "rdf"}:
            raise NewsAdapterError(f"RSS adapter received an XML root named {root_name!r}")
        return [element for element in root.iter() if _local_name(element.tag) == "item"]
    if adapter == "atom":
        if root_name != "feed":
            raise NewsAdapterError(f"Atom adapter received an XML root named {root_name!r}")
        return [element for element in root.iter() if _local_name(element.tag) == "entry"]
    if adapter == "news-sitemap":
        if root_name != "urlset":
            raise NewsAdapterError(f"News sitemap adapter received an XML root named {root_name!r}")
        return [element for element in list(root) if _local_name(element.tag) == "url"]
    raise NewsAdapterError(f"Unsupported feed adapter {adapter!r}")


def _normalise_element(
    element: ET.Element,
    source: Mapping[str, Any],
    adapter: str,
) -> tuple[dict[str, Any] | None, str]:
    if adapter == "news-sitemap":
        raw_title = _descendant_text(element, ("title",))
        raw_date = _descendant_text(element, ("publication_date",))
        raw_description = _descendant_text(element, ("description",))
    else:
        raw_title = _child_text(element, ("title",))
        raw_date = _child_text(element, ("pubdate", "published", "updated", "date"))
        raw_description = _child_text(
            element,
            ("description", "summary", "content", "encoded"),
        )

    title = _clean_text(raw_title)
    if not title:
        return None, "missing-title"
    url, rejection = _canonical_article_url(source, _entry_link(element, adapter))
    if rejection:
        return None, rejection
    published = _parse_date(raw_date)
    if published is None:
        return None, "missing-or-invalid-date"

    rights = source.get("rights") if isinstance(source.get("rights"), Mapping) else {}
    return {
        "title": title[:300],
        "url": url,
        "publishedAt": _iso_z(published),
        "sourceId": str(source.get("id") or ""),
        "source": str(source.get("name") or ""),
        "sourceCategory": str(source.get("category") or "editorial"),
        "rightsMode": str(rights.get("mode") or "link-only"),
        "_description": _clean_text(raw_description)[:2000],
    }, ""


def parse_feed(
    payload: bytes | str,
    source: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse one approved source and return articles plus rejection diagnostics."""

    assert_source_collectable(source)
    adapter = str(source.get("adapter") or "")
    if adapter not in FETCHABLE_ADAPTERS:
        raise NewsAdapterError(f"Unsupported feed adapter {adapter!r}")
    if isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        payload_bytes = payload
    else:
        raise NewsAdapterError("Feed payload must be bytes or text")
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise NewsAdapterError("Feed payload exceeds 5 MB")
    lowered = payload_bytes[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise NewsAdapterError("Feed document type and entity declarations are prohibited")
    try:
        root = ET.fromstring(payload_bytes)
    except ET.ParseError as error:
        raise NewsAdapterError(f"Feed is not well-formed XML: {error}") from error

    elements = _feed_elements(root, adapter)
    articles: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    for element in elements:
        article, reason = _normalise_element(element, source, adapter)
        if article is None:
            rejections[reason or "invalid-entry"] += 1
        else:
            articles.append(article)

    diagnostics = {
        "sourceId": str(source.get("id") or ""),
        "adapter": adapter,
        "discovered": len(elements),
        "parsed": len(articles),
        "entriesSeen": len(elements),
        "articlesAccepted": len(articles),
        "articlesRejected": sum(rejections.values()),
        "rejectionReasons": dict(sorted(rejections.items())),
    }
    return articles, diagnostics


def _validate_final_feed_url(source: Mapping[str, Any], value: str) -> None:
    try:
        requested = urlsplit(str(source.get("url") or ""))
        final = urlsplit(value)
        final_port = final.port
    except ValueError as error:
        raise NewsAdapterError("Feed redirect URL is malformed") from error
    allowed_hosts = {
        str(requested.hostname or "").lower(),
        *(str(host).lower() for host in source.get("articleHosts") or []),
    }
    if (
        final.scheme != "https"
        or not final.hostname
        or final.hostname.lower() not in allowed_hosts
        or final.username
        or final.password
        or final_port not in (None, 443)
    ):
        raise NewsAdapterError("Feed redirected outside its registered HTTPS hosts")


def fetch_source(
    source: Mapping[str, Any],
    timeout: int = 20,
    attempts: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch an approved source with bounded retries and normalise its entries."""

    assert_source_collectable(source)
    if not isinstance(timeout, int) or timeout <= 0 or timeout > 120:
        raise NewsAdapterError("Fetch timeout must be an integer from 1 to 120 seconds")
    if not isinstance(attempts, int) or attempts < 1 or attempts > MAX_FETCH_ATTEMPTS:
        raise NewsAdapterError(
            f"Fetch attempts must be an integer from 1 to {MAX_FETCH_ATTEMPTS}"
        )

    adapter = str(source.get("adapter") or "")
    accepts = {
        "rss": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
        "atom": "application/atom+xml, application/xml;q=0.9, text/xml;q=0.8",
        "news-sitemap": "application/xml, text/xml;q=0.9",
    }
    if adapter not in accepts:
        raise NewsAdapterError(f"Unsupported fetch adapter {adapter!r}")

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            str(source["url"]),
            headers={"User-Agent": USER_AGENT, "Accept": accepts[adapter]},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise _RetryableFetchError(f"HTTP {status}")
                final_url = str(
                    response.geturl()
                    if hasattr(response, "geturl")
                    else source["url"]
                )
                _validate_final_feed_url(source, final_url)
                payload = response.read(MAX_PAYLOAD_BYTES + 1)
                if len(payload) > MAX_PAYLOAD_BYTES:
                    raise NewsAdapterError("Feed payload exceeds 5 MB")
            articles, diagnostics = parse_feed(payload, source)
            diagnostics["fetchAttempts"] = attempt
            diagnostics["fetchedUrl"] = final_url
            return articles, diagnostics
        except NewsSourceValidationError:
            raise
        except NewsAdapterError:
            raise
        except (
            _RetryableFetchError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(0.25 * (2 ** (attempt - 1)))

    identifier = str(source.get("id") or "unknown")
    raise NewsAdapterError(
        f"{identifier}: fetch failed after {attempts} attempts: {last_error}"
    ) from last_error
