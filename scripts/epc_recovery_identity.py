"""Explicit, source-pinned EPC recovery without changing canonical identity.

The manifest is private and trusted only through an externally supplied digest.
Neither a cached match method nor an indicative mapping UPRN grants admission.
"""

import copy
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType


IDENTITY_FIELDS = ("id", "address", "saon", "paon", "street", "locality", "town",
                   "district", "postcode", "price", "date", "propertyType", "category",
                   "market", "estateId", "propertyRecordId", "county")
SNAPSHOT_FIELDS = frozenset({
    "addressLine1", "addressLine2", "addressLine3", "addressLine4",
    "address_line_1", "address_line_2", "address_line_3", "address_line_4",
    "address1", "address2", "address3", "address4", "postTown", "post_town",
    "postcode", "POSTCODE", "uprn", "UPRN", "property_uprn", "propertyUprn",
    "uprn_source", "certificateNumber", "certificate_number", "certificate-number",
    "lmkKey", "lmk-key", "LMK_KEY", "registrationDate", "registration_date",
    "lodgementDate", "lodgement_date", "lodgement-datetime",
    "local_authority", "local_authority_code", "localAuthority", "localAuthorityCode",
    "council", "council_code", "town", "locality",
})
UPRN_FIELDS = ("uprn", "UPRN", "property_uprn", "propertyUprn")
COUNCIL_FIELDS = ("local_authority", "local_authority_code", "localAuthority",
                  "localAuthorityCode", "council", "council_code")
PARTIAL_EXTENT_WORDS = frozenset({
    "ANNEX", "ANNEXE", "FLAT", "UNIT", "PLOT", "ROOM", "SUITE", "MAISONETTE",
    "FLOOR", "GROUND", "FIRST", "SECOND", "THIRD", "FOURTH", "LOWER", "UPPER",
    "BASEMENT", "REAR", "PENTHOUSE", "OUTBUILDING", "OUTBUILDINGS", "GARAGE",
    "GARAGES", "WING",
})
_TRUST_TOKEN = object()


def context_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def cohort_digest(rows):
    return context_digest([{key: row.get(key) for key in IDENTITY_FIELDS} for row in rows])


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _uprn(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,12}", text) or int(text) == 0:
        return None
    return str(int(text))


def _canonical_text(value):
    text = re.sub(r"['\u2018\u2019\u02bc]", "", str(value or "").upper())
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def _alias_parts(member):
    if not isinstance(member, str) or not member.startswith("property:"):
        raise ValueError("Invalid reviewed EPC alias member")
    address, separator, postcode = member[9:].rpartition("|")
    if (not separator or not address or address != _canonical_text(address)
            or not re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}", postcode)):
        raise ValueError("Invalid reviewed EPC alias member")
    return address, postcode


@dataclass(frozen=True)
class TrustedRecoveryContext:
    payload_sha256: str
    cohort_identity_sha256: str
    properties: object
    rows_by_id: object
    rows_by_property: object
    address_owners: object
    _token: object


def load_recovery_context(payload, transactions, *, expected_sha256):
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("An external EPC recovery context SHA-256 is required")
    if not isinstance(payload, dict) or context_digest(payload) != expected_sha256:
        raise ValueError("EPC recovery context does not match its external SHA-256")
    if set(payload) != {"schemaVersion", "cohortIdentitySha256", "frozenAppCommit", "sourceHashes", "properties"}:
        raise ValueError("Unexpected EPC recovery context fields")
    if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
        raise ValueError("Unsupported EPC recovery context version")
    if (not isinstance(transactions, (list, tuple)) or not transactions
            or any(not isinstance(row, dict) for row in transactions)):
        raise ValueError("EPC recovery requires a complete transaction cohort")
    if payload["cohortIdentitySha256"] != cohort_digest(transactions):
        raise ValueError("EPC recovery context does not match the full identity cohort")
    if not isinstance(payload["frozenAppCommit"], str) or not re.fullmatch(r"[0-9a-f]{40}", payload["frozenAppCommit"]):
        raise ValueError("EPC recovery requires its frozen app commit")
    sources = payload["sourceHashes"]
    if (not isinstance(sources, dict) or not set(sources) <= {"hmlrLinks", "reviewedAliases", "councils", "uprnDiscovery"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in sources.values())):
        raise ValueError("Invalid EPC recovery source hashes")
    rows_by_id, rows_by_property = {}, defaultdict(list)
    address_owners = defaultdict(set)
    import enrich_epc_data as epc
    for row in transactions:
        row_id, property_id = row.get("id"), row.get("propertyRecordId")
        if (not isinstance(row_id, str) or not row_id or row_id in rows_by_id
                or not isinstance(property_id, str) or not property_id.startswith("property:")):
            raise ValueError("EPC recovery cohort has ambiguous row/property membership")
        identity = {key: copy.deepcopy(row.get(key)) for key in IDENTITY_FIELDS}
        rows_by_id[row_id] = identity
        rows_by_property[property_id].append(identity)
        address_owners[_canonical_text(epc.POSTCODE_PATTERN.sub(" ", str(row.get("address") or "")))].add(property_id)
    properties = payload["properties"]
    if not isinstance(properties, dict) or not set(properties) <= set(rows_by_property):
        raise ValueError("EPC recovery targets must belong to the pinned cohort")
    uprn_owners, alias_owners, group_owners = {}, {}, {}
    for property_id, entry in properties.items():
        if not isinstance(entry, dict) or not set(entry) <= {"authoritativeUprn", "reviewedAliases", "councilCode", "discoveryUprns"}:
            raise ValueError("Unexpected EPC recovery property fields")
        if "discoveryUprns" in entry:
            values = entry["discoveryUprns"]
            if ("uprnDiscovery" not in sources or not isinstance(values, list)
                    or not 1 <= len(values) <= 4
                    or any(not isinstance(value, str) or _uprn(value) is None for value in values)
                    or len({_uprn(value) for value in values}) != len(values)):
                raise ValueError("EPC discovery UPRNs require bounded, pinned query inputs")
        if "authoritativeUprn" in entry:
            claim = entry["authoritativeUprn"]
            if (not isinstance(claim, dict) or set(claim) != {"uprn", "sourceId", "sourceSnapshot"}
                    or "hmlrLinks" not in sources or _uprn(claim.get("uprn")) is None
                    or not isinstance(claim.get("sourceId"), str)
                    or not re.fullmatch(r"hmlr_ppd_uprn_\d{6}_os_\d{6}_[a-z0-9]+", claim["sourceId"])
                    or not isinstance(claim.get("sourceSnapshot"), str) or not claim["sourceSnapshot"]):
                raise ValueError("EPC recovery UPRN lacks pinned official HMLR provenance")
            uprn = _uprn(claim["uprn"])
            if uprn in uprn_owners and uprn_owners[uprn] != property_id:
                raise ValueError("EPC recovery UPRN is shared by multiple canonical properties")
            uprn_owners[uprn] = property_id
        if "reviewedAliases" in entry:
            groups = entry["reviewedAliases"]
            if "reviewedAliases" not in sources or not isinstance(groups, list) or not groups:
                raise ValueError("EPC recovery aliases require their pinned reviewed registry")
            for group in groups:
                if (not isinstance(group, dict) or set(group) != {"groupId", "members"}
                        or not isinstance(group["groupId"], str)
                        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", group["groupId"])
                        or not isinstance(group["members"], list) or len(group["members"]) < 2
                        or any(not isinstance(member, str) for member in group["members"])
                        or len(set(group["members"])) != len(group["members"])
                        or property_id not in group["members"]):
                    raise ValueError("EPC recovery alias group is not bound to its canonical property")
                if group["groupId"] in group_owners:
                    raise ValueError("Duplicate EPC recovery alias group")
                group_owners[group["groupId"]] = property_id
                for member in group["members"]:
                    _alias_parts(member)
                    if ((member in rows_by_property and member != property_id)
                            or (member in alias_owners and alias_owners[member] != property_id)):
                        raise ValueError("EPC recovery alias belongs to another canonical property")
                    alias_owners[member] = property_id
        if "councilCode" in entry:
            if ("councils" not in sources or not isinstance(entry["councilCode"], str)
                    or not re.fullmatch(r"[EW]\d{8}", entry["councilCode"])):
                raise ValueError("EPC recovery council filter lacks pinned source evidence")
    return TrustedRecoveryContext(expected_sha256, payload["cohortIdentitySha256"],
                                  _freeze(copy.deepcopy(properties)), _freeze(rows_by_id),
                                  _freeze(dict(rows_by_property)),
                                  MappingProxyType({k: frozenset(v) for k, v in address_owners.items()}),
                                  _TRUST_TOKEN)


def _entry(transaction, context):
    if not isinstance(context, TrustedRecoveryContext) or context._token is not _TRUST_TOKEN:
        raise ValueError("EPC recovery requires an explicitly verified context object")
    row_id = transaction.get("id")
    original = context.rows_by_id.get(row_id)
    if original is None or any(transaction.get(key) != original.get(key) for key in IDENTITY_FIELDS):
        raise ValueError("EPC recovery transaction is outside its bound identity cohort")
    return context.properties.get(transaction.get("propertyRecordId"))


def certificate_identity_snapshot(record):
    if not isinstance(record, dict):
        raise ValueError("EPC certificate identity must be an object")
    result = {}
    for key in SNAPSHOT_FIELDS:
        if key not in record:
            continue
        value = record[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))
                                  or len(str(value)) > 1200):
            raise ValueError("EPC certificate identity contains an invalid scalar")
        result[key] = value
    return result


def discovery_queries(transaction, context):
    entry = _entry(transaction, context)
    if entry is None:
        return ()
    import enrich_epc_data as epc
    queries = []
    postcode = epc.normalise_postcode(transaction.get("postcode"))
    if postcode:
        queries.append({"postcode": postcode})
    if entry.get("authoritativeUprn"):
        queries.append({"uprn": _uprn(entry["authoritativeUprn"]["uprn"])})
    # These are lookup hints only. Admission deliberately never reads them.
    queries.extend({"uprn": _uprn(value)} for value in entry.get("discoveryUprns", ()))
    addresses = [epc.POSTCODE_PATTERN.sub(" ", str(transaction.get("address") or ""))]
    if not postcode:
        # The register searches contiguous address text. Keep both a complete
        # delivery prefix and a road/town alternative when postcode is unknown.
        paon, road, town = (epc.clean(transaction.get(key)) for key in ("paon", "street", "town"))
        if paon and road:
            prefix = " ".join(filter(None, [epc.clean(transaction.get("saon")), paon, road]))
            addresses.append(" ".join(prefix.replace(",", " ").split()))
        if road and town:
            addresses.append(" ".join((road + " " + town).replace(",", " ").split()))
    for group in entry.get("reviewedAliases", ()):
        for member in group["members"]:
            address, alias_postcode = _alias_parts(member)
            queries.append({"postcode": alias_postcode})
            addresses.append(epc.POSTCODE_PATTERN.sub(" ", address))
    for address in addresses:
        address = " ".join(address.upper().split()).strip(" ,")
        if not 3 <= len(address) <= 240:
            continue
        # Council codes are identity context; the API's optional council[]
        # filter expects council names, which this manifest does not supply.
        queries.append({"address": address})
    unique = {context_digest(query): query for query in queries}
    return tuple(unique[key] for key in sorted(unique))


def _certificate_uprn(record):
    values = [_uprn(record[key]) for key in UPRN_FIELDS if record.get(key) not in (None, "")]
    if not values:
        return None
    if any(value is None for value in values) or len(set(values)) != 1:
        raise ValueError("Conflicting EPC certificate UPRN fields")
    return values[0]


def _address_parts(transaction, certificate):
    """Enumerate exact known road boundaries and fully attributed suffixes."""
    import enrich_epc_data as epc
    road = epc.identity_text(transaction.get("street") or transaction.get("locality"), street=True).split()
    if not road:
        return []
    alternatives = {road[-1]}
    alternatives.update(key for key, value in epc.ADDRESS_ABBREVIATIONS.items() if value == road[-1])
    tokens = epc.identity_text(epc.POSTCODE_PATTERN.sub(" ", epc.candidate_address(certificate))).split()
    results = []
    for index in range(len(tokens) - len(road) + 1):
        chunk = tokens[index:index + len(road)]
        if chunk[:-1] != road[:-1] or chunk[-1] not in alternatives:
            continue
        prefix, suffix = " ".join(tokens[:index]), tokens[index + len(road):]
        if (not prefix or any(token in epc.UNACCOUNTED_EXTENT_TOKENS for token in suffix)
                or any(re.fullmatch(r"\d+[A-Z]?", token) for token in suffix)
                or not epc.attributed_address_suffix(transaction, suffix)):
            continue
        results.append((prefix, " ".join(road), " ".join(suffix)))
    return results


def _pair_agrees(transaction, certificate, summary):
    import enrich_epc_data as epc
    if not isinstance(summary, dict):
        return False
    full_date, summary_date = epc.extract_registration_date(certificate), epc.extract_registration_date(summary)
    if not full_date or full_date != summary_date:
        return False
    full_number, summary_number = epc.extract_certificate_number(certificate), epc.extract_certificate_number(summary)
    if not summary_number or (full_number and full_number != summary_number):
        return False
    postcode = epc.certificate_postcode(certificate)
    if not postcode or postcode != epc.certificate_postcode(summary):
        return False
    try:
        full_uprn, summary_uprn = _certificate_uprn(certificate), _certificate_uprn(summary)
    except ValueError:
        return False
    if full_uprn and summary_uprn and full_uprn != summary_uprn:
        return False
    # Complete literal addresses and the original exact guard also support
    # source pairs when the ledger has no separately structured road field.
    if (_canonical_text(epc.candidate_address(certificate)) == _canonical_text(epc.candidate_address(summary))
            or (epc.delivery_identity(transaction, certificate)[0]
                and epc.delivery_identity(transaction, summary)[0])):
        return True
    full_parts = {(prefix, road) for prefix, road, _suffix in _address_parts(transaction, certificate)}
    summary_parts = {(prefix, road) for prefix, road, _suffix in _address_parts(transaction, summary)}
    return bool(full_parts & summary_parts)


def resolve_identity(transaction, certificate, context, summary=None):
    import enrich_epc_data as epc
    entry = _entry(transaction, context)
    exact = epc.delivery_identity(transaction, certificate)
    if exact[0] or entry is None:
        return exact
    if not isinstance(certificate, dict):
        return False, "recovery_certificate_identity_missing"
    # Reviewed aliases are literal, complete registry members, never edits or
    # inferred spelling/number/postcode changes.
    certificate_postcode = epc.certificate_postcode(certificate)
    certificate_address = _canonical_text(epc.POSTCODE_PATTERN.sub(" ", epc.candidate_address(certificate)))
    if certificate_postcode:
        for group in entry.get("reviewedAliases", ()):
            for member in group["members"]:
                alias_address, alias_postcode = _alias_parts(member)
                alias_address = _canonical_text(epc.POSTCODE_PATTERN.sub(" ", alias_address))
                if certificate_postcode == alias_postcode and certificate_address == alias_address:
                    return True, "reviewed_full_address_alias"
    parts = _address_parts(transaction, certificate)
    expected = epc.identity_text(" ".join(filter(None, [epc.clean(transaction.get("saon")), epc.clean(transaction.get("paon"))])))
    if not expected or not parts:
        return False, "recovery_delivery_identity_missing_or_conflicting"
    try:
        certificate_uprn = _certificate_uprn(certificate)
        summary_uprn = _certificate_uprn(summary) if isinstance(summary, dict) else None
    except ValueError:
        return False, "recovery_certificate_uprn_conflict"
    if certificate_uprn and summary_uprn and certificate_uprn != summary_uprn:
        return False, "recovery_full_summary_uprn_conflict"
    if not certificate_uprn and summary_uprn and _pair_agrees(transaction, certificate, summary):
        certificate_uprn = summary_uprn
        uprn_method = "hmlr_uprn_with_crossvalidated_summary"
    else:
        uprn_method = "hmlr_uprn_with_delivery_extent"
    authoritative = entry.get("authoritativeUprn")
    known_postcode = epc.normalise_postcode(transaction.get("postcode"))
    if authoritative and certificate_uprn == _uprn(authoritative["uprn"]):
        if known_postcode and known_postcode != certificate_postcode:
            return False, "recovery_known_postcode_conflict"
        # A UPRN may corroborate a renamed whole house, never a newly added or
        # omitted partial-building qualifier. Cottage/lodge/barn remain names.
        has_extent = lambda value: any(token in PARTIAL_EXTENT_WORDS for token in value.split())
        if any(prefix == expected or (not has_extent(prefix) and not has_extent(expected)
               and epc.delivery_conflict_reason(expected, prefix) == "unresolved_named_identity_or_alias")
               for prefix, _road, _suffix in parts):
            return True, uprn_method
        return False, "recovery_delivery_extent_conflict"
    if authoritative and certificate_uprn:
        return False, "recovery_hmlr_uprn_conflict"
    if known_postcode:
        return False, "recovery_no_independent_identity_corroboration"
    # A certificate postcode is not copied into the ledger to make the old
    # guard pass. Match the complete original delivery prefix and all available
    # independent town/locality context instead.
    town = epc.identity_text(transaction.get("town"))
    if not town or not certificate_postcode:
        return False, "recovery_independent_locality_missing"
    independent = {town}
    locality = epc.identity_text(transaction.get("locality"))
    road = epc.identity_text(transaction.get("street") or transaction.get("locality"), street=True)
    if locality and locality != road:
        independent.add(locality)
    for key in ("postTown", "post_town", "town"):
        if certificate.get(key) and epc.identity_text(certificate[key]) != town:
            return False, "recovery_town_conflict"
    if locality and certificate.get("locality") and epc.identity_text(certificate["locality"]) != locality:
        return False, "recovery_locality_conflict"
    council = entry.get("councilCode")
    codes = {str(certificate[key]).strip().upper() for key in COUNCIL_FIELDS
             if certificate.get(key) and re.fullmatch(r"[EW]\d{8}", str(certificate[key]).strip().upper())}
    if len(codes) > 1 or (council and codes and codes != {council}):
        return False, "recovery_council_conflict"
    address_key = _canonical_text(epc.POSTCODE_PATTERN.sub(" ", str(transaction.get("address") or "")))
    if context.address_owners.get(address_key, frozenset()) != {transaction["propertyRecordId"]}:
        return False, "recovery_shared_full_address"
    # Canonical full strings can differ by benign county/place suffixes while
    # the same source delivery still belongs to another existing property.
    for other_property, other_rows in context.rows_by_property.items():
        if other_property == transaction["propertyRecordId"]:
            continue
        if any(epc.normalise_postcode(other.get("postcode")) == certificate_postcode
               and epc.delivery_identity(other, certificate)[0] for other in other_rows):
            return False, "recovery_other_canonical_delivery_match"
    for prefix, _road, suffix in parts:
        if prefix == expected and all((" " + phrase + " ") in (" " + suffix + " ") for phrase in independent):
            return True, "exact_full_delivery_with_independent_locality"
    return False, "recovery_full_address_context_incomplete"


def replay_cached_identity(transaction, record, context):
    """Re-run source snapshots; the saved method/context claims grant no trust."""
    import enrich_epc_data as epc
    _entry(transaction, context)
    retained = record.get("epc")
    full = record.get("certificateIdentity")
    summary = record.get("summaryIdentity")
    fetch = record.get("certificateFetch")
    if not isinstance(retained, dict) or not isinstance(full, dict) or not isinstance(fetch, dict):
        return False, "recovery_original_identity_evidence_missing"
    try:
        if certificate_identity_snapshot(full) != full or (summary is not None and certificate_identity_snapshot(summary) != summary):
            return False, "recovery_invalid_identity_snapshot"
    except ValueError:
        return False, "recovery_invalid_identity_snapshot"
    number = retained.get("epcCertificateNumber")
    if not number or fetch.get("requestedNumber") != number:
        return False, "recovery_request_certificate_conflict"
    returned = epc.extract_certificate_number(full)
    if (returned and returned != number) or fetch.get("returnedNumber", "") != returned:
        return False, "recovery_returned_certificate_conflict"
    if fetch.get("uprn") not in (None, ""):
        try:
            claimed_uprn, full_uprn = _uprn(fetch["uprn"]), _certificate_uprn(full)
        except ValueError:
            return False, "recovery_fetch_uprn_conflict"
        if not claimed_uprn or claimed_uprn != full_uprn:
            return False, "recovery_fetch_uprn_conflict"
    if (epc.identity_text(epc.candidate_address(full)) != epc.identity_text(retained.get("epcAddress"))
            or epc.extract_registration_date(full) != retained.get("epcRegistrationDate")):
        return False, "recovery_retained_certificate_identity_conflict"
    if summary is not None and (epc.extract_certificate_number(summary) != number
                                or not _pair_agrees(transaction, full, summary)):
        return False, "recovery_full_summary_identity_conflict"
    return resolve_identity(transaction, full, context, summary=summary)
