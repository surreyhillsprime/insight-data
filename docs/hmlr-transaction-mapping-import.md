# Monthly HMLR transaction mapping import

The producer now has a separate database-import candidate for the official
PPD, PPD-to-UPRN and PPD-to-INSPIRE release. It records every existing published
sale's mapping disposition and preserves the official lookup relationships for
new qualifying records. It does not write a database or admit an identity.

The website orchestrator owns the database integration and serving contract.
No raw UPRN or private address-reference ledger belongs in a browser DTO.

## Source limits

HMLR's [INSPIRE lookup specification](https://www.gov.uk/government/statistical-data-sets/technical-specification-transaction-unique-identifier-and-inspire-id-look-up-table-dataset)
says the lookup is not backdated and supplies an ID only for a freehold record
with an index polygon. The [UPRN lookup specification](https://www.gov.uk/government/statistical-data-sets/technical-specification-transaction-unique-identifier-and-uprn-look-up-table-dataset)
also excludes backdated records. A missing lookup row must remain an explicit
coverage result, not a fabricated match or a withdrawal of an earlier link.

One sale can have several INSPIRE IDs. Store a relation per supplied ID; never
truncate the list to one primary polygon. A property's current parcel
association does not prove the polygon extent at the date of an older sale.
Leasehold transactions can have an exact UPRN point without having a polygon
in the freehold INSPIRE lookup.

## Producer

`scripts/build_hmlr_mapping_packet.py --download-current` discovers the release
from HMLR's actual published links. It downloads the three source files and
requires matching publication dates. The parser verifies the SHA-256 of each
file and parses those same retained bytes; it does not reopen an unverified
source after hashing. Inputs and output receive content hashes.

The packet has two distinct collections:

| Collection | Meaning |
| --- | --- |
| `officialTransactionCandidates` | Official PPD UUID, complete raw 16-cell PPD record, row ordinal, A/C/D operation, every supplied UPRN and INSPIRE ID, selection reasons and missing-link disposition. |
| `legacyMappingCandidates` | One assessment per existing sale reference, its existing exact published property reference, current indicative parcel IDs or an unresolved/held disposition, source snapshot and limitations. No official UUID is assigned. |

For the private database, `--transaction-index-json` accepts the orchestrator's
exact current adapter export and requires its explicit SHA-256 pin. These rows
retain the separate `insight_internal_transaction_uuid` namespace and canonical
public property reference. An internal UUID's appearance never makes it an
official HMLR UUID. The source of truth for coverage is that current export,
not the older data-repository feed.

`--geometry-holds-json` accepts explicit property/INSPIRE/source-snapshot holds.
A held relation has no active `inspireIds`, preserves `heldPriorInspireIds`, and
is counted as `review_required_geometry_change`, separately from an unresolved
address match. The known November split at 35 Onslow Road is held in the private
handoff rather than relabelled as either half of its prior parcel.

Selection retains Surrey residential sales at or above GBP 2m, monthly records
whose exact address reference is already in the published property universe
(including lower-value history), and any previously observed official UUID.
The latter retains corrections/deletions even if their new cells fall outside
the current price or geographic cohort. An exact address-reference candidate
still requires canonical identity admission. It cannot authorize a property
merge or establish equivalence with an old synthetic transaction ID.

All source occurrences survive. Repeated UUIDs are flagged for operation-order
review rather than deduplicated or silently applied. A lookup UUID absent from
the accompanying PPD file, duplicate lookup pair, malformed identifier or
mixed source publication fails the candidate.

`idempotencyKey` is derived from packet version, release, all three official
source hashes and the exact transaction/parcel/prior-state input hashes. It
does not depend on an acquisition clock. The receipt separately hashes the
complete packet bytes.

The daily `HMLR Transaction Mapping Candidate` workflow captures new releases
without guessing working days. It needs no database credential, has read-only
repository permissions, and retains the raw source files, manifest, packet and
receipt as an artifact. The existing identifier workflow also builds this
artifact before OS-point or polygon validation: a geometry problem cannot
erase the transaction identifier handoff. These workflow changes remain
unpublished until commit/push approval.

Artifacts expire after 90 days. The database consumer must archive source bytes
and a successful import receipt durably; workflow artifact retention is not
the database or historical source archive.

## Database consumer contract

Use the existing `ingest` source/delivery/content/observation tables and the
transaction identity/revision framework. The database owner selects the exact
new relation migration after checking the installed schema.

1. Verify packet bytes against the receipt, validate its version and recompute
   its idempotency key. Bind all source and input hashes before admitting rows.
2. Persist the official PPD UUID in its own source namespace, raw source
   observations, source file and row ordinal. Preserve the raw action and
   transfer date independently of the release month/publication date.
3. Reconcile A/C/D through the existing importer. Changes append a revision;
   deletions withdraw the source transaction and affected active mapping
   eligibility. An unknown correction/deletion requires reconciliation, not
   an address-based delete. Missing rows in a later monthly file are not deletes.
4. Store official transaction-to-UPRN and transaction-to-INSPIRE edges as
   many-to-many source-backed relations. Resolve INSPIRE IDs to versioned
   `evidence.parcels`/`parcel_versions` only when their geometry exists.
   Otherwise retain `geometryResolution=not_checked/pending` and source evidence.
5. Store current property-to-parcel associations separately from official sale
   relations. Bind legacy `lr-*` references through the existing admitted
   crosswalk. Rebuild this collection against the actual private Pilot input,
   including its explicit Brook merge, before claiming Pilot-wide coverage.
6. Join an admitted UPRN to its exact OS Open UPRN coordinate for a point.
   Multiple, conflicting or missing identities remain explicit. Do not use a
   postcode centroid or the nearest point as an exact property location.
7. Commit source observations, relation revisions, coverage and the import
   receipt atomically. A repeated idempotency key must be a verified no-op;
   preserve an unknown commit outcome and reconcile it before retrying.
8. Publish mapping data only after a readback proves that every admitted sale
   has a disposition and that active links reference the intended source and
   geometry versions. Return minimized coordinates/quality/status to the map.

The consumer is not implemented or activated by this producer change. It is a
required orchestrator integration, not an optional later enhancement.
Its daily job must assess the current private database export after applying
source events; the public workflow's older baseline assessment cannot be used
as proof of current private Pilot coverage.

## Verified September source run

The official files discovered on 2 November contain September's release,
published on 28 October 2026. There are 98,103 PPD rows: 93,495 additions,
3,056 changes and 1,552 deletions. There are 80,105 INSPIRE pairs covering
74,750 source UUIDs and 90,941 UPRN pairs. The source selection produces 32
transaction candidates; all 32 have supplied official INSPIRE IDs.

The data-repository input assessed here has 4,738 sales over 3,784 properties.
Its existing September parcel publication provides current indicative
property associations for 4,108 sales, leaving 630 unresolved. This is not the
newer private Pilot's transaction count and is not an official historical
sale-to-polygon coverage claim. The orchestrator must supply that exact input
for its final database coverage check.

The subsequently supplied exact private adapter export is pinned to SHA-256
`d273116a6e1ecacc6f253d84650ce146b4329fec43361b85af6c0e0afdc99bee`:
4,774 internal sale UUIDs, 3,809 canonical public property references, with the
explicit Brook identity merge already applied. Before the Onslow geometry hold,
4,115 sale rows have an existing current-property parcel association and 659
are unresolved. These are indicative property mappings, not newly established
official transaction-to-polygon identities. The private receipt records the
final held/active/unresolved counts.

In that exact export, the two current sale rows named HIGH CROFT HOUSE, 35
ONSLOW ROAD already have no matching current parcel key and are unresolved.
The old public parcel key is the different bare 35 ONSLOW ROAD reference. The
producer does not infer their positive equivalence from a shared street number.
The separate geometry hold blocks the old key if an admitted database crosswalk
exposes it. Accordingly, the pinned private packet accounts for all 4,774 rows
as 4,115 current indicative associations plus 659 unresolved; the old-key hold
does not silently invent an additional match.

For unresolved historical records, retain already evidenced property links
and investigate exact address/UPRN identity. HMLR's licensed
[National Polygon Service](https://www.gov.uk/guidance/national-polygon-service)
provides the title-number/UPRN and polygon joins when that route is needed.
It requires separate entitlement/rights and still does not justify promising
an official freehold INSPIRE ID for every leasehold or historical sale.
