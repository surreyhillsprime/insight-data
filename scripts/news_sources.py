#!/usr/bin/env python3
"""Load and validate INSIGHT's fail-closed news source registry."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "news-sources.json"
DEFAULT_SCHEMA = ROOT / "config" / "news-sources.schema.json"

SCHEMA_VERSION = 2
ALLOWED_ADAPTERS = frozenset({
    "rss",
    "atom",
    "news-sitemap",
    "publisher-feed-required",
})
FETCHABLE_ADAPTERS = frozenset({"rss", "atom", "news-sitemap"})
PUBLICATION_MODES = frozenset({"live", "shadow", "disabled"})
APPROVED = "approved"


class NewsSourceValidationError(ValueError):
    """Raised when the news source registry is unsafe or malformed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _resolve_local_ref(root_schema: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise NewsSourceValidationError(f"Unsupported JSON Schema reference {reference!r}")
    node: Any = root_schema
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or token not in node:
            raise NewsSourceValidationError(f"JSON Schema reference {reference!r} does not resolve")
        node = node[token]
    if not isinstance(node, Mapping):
        raise NewsSourceValidationError(f"JSON Schema reference {reference!r} is not an object")
    return node


def _type_matches(value: Any, type_name: str) -> bool:
    checks = {
        "object": lambda candidate: isinstance(candidate, Mapping),
        "array": lambda candidate: isinstance(candidate, list),
        "string": lambda candidate: isinstance(candidate, str),
        "integer": lambda candidate: isinstance(candidate, int) and not isinstance(candidate, bool),
        "number": lambda candidate: isinstance(candidate, (int, float)) and not isinstance(candidate, bool),
        "boolean": lambda candidate: isinstance(candidate, bool),
        "null": lambda candidate: candidate is None,
    }
    check = checks.get(type_name)
    return bool(check and check(value))


def _validate_schema_value(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    path: str,
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        _validate_schema_value(value, _resolve_local_ref(root_schema, reference), root_schema, path)
        return

    if "const" in schema and value != schema["const"]:
        raise NewsSourceValidationError(f"{path}: value does not match the required constant")
    if "enum" in schema and value not in schema["enum"]:
        raise NewsSourceValidationError(f"{path}: value is outside the allowed enum")

    allowed_types = schema.get("type")
    if allowed_types:
        type_names = [allowed_types] if isinstance(allowed_types, str) else list(allowed_types)
        if not any(_type_matches(value, str(type_name)) for type_name in type_names):
            raise NewsSourceValidationError(f"{path}: value does not match type {type_names}")

    if isinstance(value, Mapping):
        required = schema.get("required") or []
        missing = sorted(str(field) for field in required if field not in value)
        if missing:
            raise NewsSourceValidationError(f"{path}: required fields are missing {missing}")
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None:
                if additional is False:
                    raise NewsSourceValidationError(f"{path}.{key}: additional property is not permitted")
                child_schema = additional if isinstance(additional, Mapping) else None
            if isinstance(child_schema, Mapping):
                _validate_schema_value(child, child_schema, root_schema, f"{path}.{key}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise NewsSourceValidationError(f"{path}: array has fewer than {minimum_items} items")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise NewsSourceValidationError(f"{path}: array has more than {maximum_items} items")
        if schema.get("uniqueItems") is True:
            serialised = [_canonical_json(item) for item in value]
            if len(serialised) != len(set(serialised)):
                raise NewsSourceValidationError(f"{path}: array items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, child in enumerate(value):
                _validate_schema_value(child, item_schema, root_schema, f"{path}[{index}]")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise NewsSourceValidationError(f"{path}: string is shorter than {minimum_length}")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise NewsSourceValidationError(f"{path}: string is longer than {maximum_length}")
        pattern = schema.get("pattern")
        if pattern:
            try:
                matches = re.search(str(pattern), value)
            except re.error as error:
                raise NewsSourceValidationError(f"{path}: schema contains an invalid pattern") from error
            if not matches:
                raise NewsSourceValidationError(f"{path}: string does not match the required pattern")
        if schema.get("format") == "date":
            try:
                parsed = date.fromisoformat(value)
            except ValueError as error:
                raise NewsSourceValidationError(f"{path}: value is not an ISO date") from error
            if parsed.isoformat() != value:
                raise NewsSourceValidationError(f"{path}: value is not a canonical ISO date")
        elif schema.get("format") == "uri":
            parts = urlsplit(value)
            if not parts.scheme or not parts.netloc:
                raise NewsSourceValidationError(f"{path}: value is not an absolute URI")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise NewsSourceValidationError(f"{path}: number is below {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise NewsSourceValidationError(f"{path}: number is above {maximum}")


def _validate_hostname(value: str, path: str) -> str:
    host = str(value or "").strip().lower()
    if not host or host != value or len(host) > 253:
        raise NewsSourceValidationError(f"{path}: host must be a canonical lowercase hostname")
    if host == "localhost" or host.endswith(".local") or host.endswith(".localhost"):
        raise NewsSourceValidationError(f"{path}: local hosts are not permitted")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise NewsSourceValidationError(f"{path}: IP-literal hosts are not permitted")
    labels = host.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise NewsSourceValidationError(f"{path}: host is not a valid public hostname")
    return host


def _validate_https_url(value: str, path: str) -> str:
    try:
        parts = urlsplit(str(value or ""))
        port = parts.port
    except ValueError as error:
        raise NewsSourceValidationError(f"{path}: URL is malformed") from error
    if parts.scheme != "https" or not parts.hostname:
        raise NewsSourceValidationError(f"{path}: URL must use HTTPS")
    if parts.username or parts.password:
        raise NewsSourceValidationError(f"{path}: URL credentials are not permitted")
    if port not in (None, 443):
        raise NewsSourceValidationError(f"{path}: non-standard URL ports are not permitted")
    if parts.fragment:
        raise NewsSourceValidationError(f"{path}: URL fragments are not permitted")
    _validate_hostname(parts.hostname.lower(), f"{path} host")
    return parts.hostname.lower()


def _validate_regexes(source: Mapping[str, Any], index: int) -> None:
    for field in ("requiredTitlePatterns", "blockedTitlePatterns"):
        for pattern_index, pattern in enumerate(source.get(field) or []):
            try:
                re.compile(str(pattern), re.IGNORECASE)
            except re.error as error:
                raise NewsSourceValidationError(
                    f"$.sources[{index}].{field}[{pattern_index}]: invalid regular expression: {error}"
                ) from error


def _validate_source_policy(source: Mapping[str, Any], index: int) -> None:
    path = f"$.sources[{index}]"
    adapter = str(source.get("adapter") or "")
    if adapter not in ALLOWED_ADAPTERS:
        raise NewsSourceValidationError(f"{path}.adapter: unsupported adapter {adapter!r}")

    _validate_https_url(str(source.get("url") or ""), f"{path}.url")
    for host_index, host in enumerate(source.get("articleHosts") or []):
        _validate_hostname(str(host), f"{path}.articleHosts[{host_index}]")

    rights = source.get("rights")
    if not isinstance(rights, Mapping):
        raise NewsSourceValidationError(f"{path}.rights: rights object is required")
    _validate_https_url(str(rights.get("referenceUrl") or ""), f"{path}.rights.referenceUrl")
    _validate_regexes(source, index)

    mode = str(source.get("publicationMode") or "")
    collection_status = str(rights.get("collectionStatus") or "")
    publication_status = str(rights.get("publicationStatus") or "")
    if mode not in PUBLICATION_MODES:
        raise NewsSourceValidationError(f"{path}.publicationMode: unsupported mode {mode!r}")
    if mode in {"live", "shadow"}:
        if adapter not in FETCHABLE_ADAPTERS:
            raise NewsSourceValidationError(
                f"{path}: {mode} source requires a fetchable, implemented adapter"
            )
        if collection_status != APPROVED:
            raise NewsSourceValidationError(
                f"{path}: {mode} source lacks approved automated collection rights"
            )
    if mode == "live" and publication_status != APPROVED:
        raise NewsSourceValidationError(
            f"{path}: live source lacks approved publication rights"
        )

    if source.get("authoritativeNational") is True:
        if (
            source.get("lane") != "official-market"
            or mode != "live"
            or collection_status != APPROVED
            or publication_status != APPROVED
            or not rights.get("licenceId")
            or not source.get("requiredTitlePatterns")
        ):
            raise NewsSourceValidationError(
                f"{path}: authoritativeNational is restricted to licensed, title-gated live official sources"
            )


def validate_registry(
    registry: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a registry against schema v2 and the live rights policy."""

    if not isinstance(registry, Mapping) or not isinstance(schema, Mapping):
        raise NewsSourceValidationError("News registry and schema must both be JSON objects")
    _validate_schema_value(registry, schema, schema, "$")
    if registry.get("schemaVersion") != SCHEMA_VERSION:
        raise NewsSourceValidationError(
            f"News registry schemaVersion must be {SCHEMA_VERSION}"
        )

    schema_adapters = set(
        schema.get("$defs", {})
        .get("source", {})
        .get("properties", {})
        .get("adapter", {})
        .get("enum", [])
    )
    if schema_adapters != set(ALLOWED_ADAPTERS):
        raise NewsSourceValidationError(
            "News source schema and runtime adapter allowlists do not match"
        )

    identifiers: set[str] = set()
    for index, source in enumerate(registry.get("sources") or []):
        identifier = str(source.get("id") or "")
        if identifier in identifiers:
            raise NewsSourceValidationError(
                f"$.sources[{index}].id: duplicate source id {identifier!r}"
            )
        identifiers.add(identifier)
        _validate_source_policy(source, index)
    return dict(registry)


def load_registry(
    path: str | Path = DEFAULT_REGISTRY,
    schema_path: str | Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    """Read and validate a registry, raising ``ValueError`` on every failure."""

    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise NewsSourceValidationError(f"Unable to read news source registry: {error}") from error
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise NewsSourceValidationError(f"Unable to read news source schema: {error}") from error
    return validate_registry(registry, schema)


def assert_source_collectable(source: Mapping[str, Any]) -> None:
    """Fail closed if a source is not currently authorised for collection."""

    identifier = str(source.get("id") or "unknown")
    adapter = str(source.get("adapter") or "")
    mode = str(source.get("publicationMode") or "")
    rights = source.get("rights") if isinstance(source.get("rights"), Mapping) else {}
    if mode not in {"live", "shadow"}:
        raise NewsSourceValidationError(f"{identifier}: disabled source cannot be fetched")
    if rights.get("collectionStatus") != APPROVED:
        raise NewsSourceValidationError(f"{identifier}: collection rights are not approved")
    if mode == "live" and rights.get("publicationStatus") != APPROVED:
        raise NewsSourceValidationError(f"{identifier}: publication rights are not approved")
    if rights.get("mode") != "link-only":
        raise NewsSourceValidationError(f"{identifier}: only link-only collection is supported")
    if adapter not in FETCHABLE_ADAPTERS:
        raise NewsSourceValidationError(f"{identifier}: adapter {adapter!r} is not fetchable")


def article_url_is_allowed(source: Mapping[str, Any], value: str) -> bool:
    """Return whether an article URL is HTTPS and on an exact registered host."""

    try:
        parts = urlsplit(str(value or ""))
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or port not in (None, 443)
    ):
        return False
    allowed = {str(host).lower() for host in source.get("articleHosts") or []}
    return parts.hostname.lower() in allowed


def title_is_allowed(source: Mapping[str, Any], title: str) -> bool:
    """Apply source-specific title gates, with block patterns taking precedence."""

    text = str(title or "")
    blocked = source.get("blockedTitlePatterns") or []
    if any(re.search(str(pattern), text, re.IGNORECASE) for pattern in blocked):
        return False
    required = source.get("requiredTitlePatterns") or []
    return not required or any(
        re.search(str(pattern), text, re.IGNORECASE) for pattern in required
    )
