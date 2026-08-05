# INSIGHT persistent property-record architecture

## Product outcome

Every qualifying Surrey property has one durable record and one glanceable,
evidence-backed story. A property that first appears in the monthly HM Land
Registry sweep is assigned a canonical identity during that same build, then
receives its sale timeline, source-coverage ledger, fact packet and initial
story automatically. Later sales and enrichments attach to that record rather
than creating a second card.

The right panel is therefore a view of a permanent property record, not a view
of whichever sale marker happened to be clicked.

The panel order is:

1. property/estate metric strip;
2. canonical address and primary property data;
3. the two-paragraph property story, ending with the estimated current value;
4. dated sales, planning and EPC evidence;
5. schools, station, airport and other contextual sections already supported
   by the property context feeds.

Internal matching diagnostics, such as postcode-centroid authority, remain in
the evidence/context layer for audit but are not promoted into the customer
metric strip.

## Implemented contract

`scripts/property_records.py` is the offline domain module. It exposes:

- `property_record_id(item)` — canonical identity;
- `build_property_records(...) -> (records, metadata)` — deterministic build;
- `read_property_records_js(path)` and `write_property_records_js(...)` —
  persistent JS snapshot I/O;
- `record_fingerprint(record)` and `dataset_fingerprint(records)` — change and
  publication integrity digests.

`scripts/build_property_records.py` is the monthly/offline command. The output
contains exactly these assignments:

```javascript
window.SURREY_PROPERTY_RECORDS = { /* propertyId -> record */ };
window.SURREY_PROPERTY_RECORDS_META = { /* build and integrity metadata */ };
```

`config/property-record.schema.json` is the version-1 record schema.
`scripts/validate_property_records.py` is the publication gate.

## Canonical identity

Version 1 identity is deliberately fail-closed:

```text
property:<FULL NORMALISED ADDRESS>|<NORMALISED POSTCODE>
```

The complete address is upper-cased, whitespace-normalised and punctuation is
collapsed; the postcode is upper-cased with spacing removed. The algorithm
does not truncate the address to its first components. It never uses a nearby
or unconfirmed UPRN as identity.

An empty address is rejected. If the postcode field is missing, the builder
first extracts a valid postcode at the end of the full address; if none exists,
it uses the explicit fail-closed `NOPOSTCODE` sentinel. This keeps the source
transaction visible without merging it into any postcode-known property. A
later postcode correction, redundant locality or reviewed property rename is
handled by the shared structured canonicaliser and evidence-reviewed alias
registry. Every retired full-address identity is published in the compact
`sourceAddressVariants` ledger so dependent evidence can migrate
deterministically. Ambiguous variants continue to fail closed as separate
properties.

Every Land Registry transaction ID must belong to exactly one property record.
That invariant is recomputed by the validator.

## Logical data model

The JS snapshot is the present persistence format, but the record is already
normalised into database-shaped collections:

| Logical table/collection | Key | Purpose |
| --- | --- | --- |
| `property_records` | `propertyId` | identity, address, lifecycle version, profile and latest metrics |
| `property_transactions` | `(propertyId, transactionId)` | exact membership of source Land Registry rows |
| `property_events` | `eventId` | oldest-first sale, planning-application and EPC timeline |
| `property_evidence` | `evidenceId` | immutable source payload supporting an event, fact or coverage assertion |
| `property_source_coverage` | `(propertyId, source)` | what was checked, archive bounds, status and limitations |
| `property_facts` | `factId` | conservative computed facts supplied to narrative generation |
| `property_stories` | `(propertyId, generatorVersion)` | rendered text plus evidence-linked claims and limitations |
| `property_valuations` | `(propertyId, modelVersion, asOf)` | auditable base value, comparable cohort, planning adjustment and final estimate |

These can move to SQLite/PostgreSQL without changing the card contract. Arrays
in the snapshot become child tables; `propertyId` remains the foreign key.

Each record contains:

- `schemaVersion`, `recordVersion`, `createdAt`, `updatedAt`;
- `canonicalAddress`, `postcode`, `profile`, `context`;
- `transactionIds`, chronological `events`, and an evidence ledger;
- `coverage.sales`, `coverage.planning`, `coverage.epc`;
- computed `metrics`, versioned `factPacket`, and persistent `story`;
- a content `fingerprint`.

Context preserves current public source observations such as schools. Nearby
radius planning is not part of the transaction or Property Record contract;
property-matched planning history remains in the separate planning feed and
event timeline. Airport fields are preserved when a source feed supplies them;
absence remains absence and must not be inferred.
Where the record has usable coordinates, the persistent narrative and live
renderer use the same bounded Surrey station and tracked-airport reference
sets, distance gates and wording. Transient flood alerts remain live context;
only mapped flood-zone/setting evidence is eligible for the permanent story.

Estate velocity and estate price metrics are aggregate facts, not property
facts. The panel joins the existing estate aggregate by `profile.estateId` at
read time. The aggregate's date window and sample size must remain visible in
its own data contract.

## Coverage is data, including negative results

Every governed source carries:

```text
status, complete, coverageMode, source, checkedAt,
coverageFrom, coverageTo, recordCount, evidenceIds, basis, limitations
```

Allowed states are `complete`, `partial`, `checked_none`, `not_checked`,
`unavailable` and `failed`.

`checked_none` is a strong assertion. Planning may use it only when metadata
proves a completed property-level `full-available-history` search and the
record has zero matched planning events. A missing overlay, a nearby planning
search, a failed authority or a partial archive can never become “no planning
ever”.

Planning decisions are classified before narrative or valuation use. Explicit
grants are substantive permissions; condition details, discharges, amendments,
reserved matters, Section 73 variations and lawful-development certificates
are follow-on/implementation signals rather than new schemes. An EPC area rise
after a permission and before resale supports a development-led story. The raw
limitations remain in the fact/evidence layer and the collapsed coverage view,
not as disclaimer copy in the glanceable story.

Legacy authority records without a recoverable date are retained with an empty
date, `datePrecision: unknown`, and sort after dated events. The system does not
invent a date merely to satisfy the timeline. Two- and four-digit years that
are encoded in normal authority references are normalised as year-precision
dates.

## Story generation and core-IP boundary

The production path is evidence first:

```text
source rows -> canonical events/evidence -> coverage ledger
            -> versioned fact packet -> governed narrative -> persistent story
```

The implemented version uses a deterministic narrative generator so every
property has a stable initial story without a network dependency or variable
model output. It records matched sales, nominal and CPIH-restated price
movement, property-level planning, dated EPC floor-area observations and the
single most material location signal. Verified private-estate membership ranks
ahead of postcode-level flood/landscape context, genuinely close rail or school
access, and airport access. Proximity never becomes a school-quality or
catchment claim. The story is exactly two paragraphs: house history/context
first, then the valuation basis and the closing line
`Estimated current value — £x`.

Every persisted story claim contains `evidenceIds` and confidence. The fact
packet also records limitations. Narrative inference is allowed when multiple
facts form a coherent sequence—for example, consent, a later larger EPC and a
subsequent higher sale—but no single cause is presented as the sole source of
the value movement, and an incomplete search never becomes an absolute
negative.

## Current-value model

`property-valuation-1` is a deterministic and auditable valuation layer. It is
not an LLM guess. It stores the selected comparable rows and every material
decision field in `story.valuation` and in a `derived_valuation` evidence
record.

The completed-house base is built from two independent anchors:

1. the latest matched sale, restated to the valuation date using the explicit,
   versioned annual CPIH table; and
2. one comparable channel selected from category-A sales of the same property
   type, using estate → town → market → Surrey-prime scope and 5 → 7 → 10 → 12
   year windows.

Where the target and at least three comparables have trustworthy EPC areas, the
comparable channel is price per square foot. EPC timing must be within one year
after or five years before the sale, area must be positive and within 0.5–2.0x
of the target, and a comparable is excluded from the area channel when an
approved substantive scheme falls between its EPC and sale. Otherwise the
model uses absolute prices with lower weight. Weighted medians, effective
sample size, geographic scope, the loaded cohort floor (now £2m), category-B
own sales and a greater-than-35% split between anchors all affect confidence
and weighting. When the absolute-price channel is the only comparable channel,
sales within £500,000 of the loaded floor receive the same conservative
near-floor weighting in the offline property-record builder and live renderer.
The blend is geometric and bounded around the CPIH-restated own sale so sparse
or structurally different comparables cannot dominate.

Planning is a separate option/implementation layer so it cannot be counted
twice. An additive planning adjustment is eligible only for a substantive
explicit approval dated later than both the latest sale and latest EPC. A
permission between an old EPC and a newer sale can invalidate the target-area
channel, but the later sale already captures its market value and therefore
receives no additional uplift. Follow-on condition/amendment records can raise
the implementation weight of a qualifying later scheme but never count as
another scheme.

An area-led planning adjustment is permitted only when structured before/after
fields or explicit incremental wording such as “adding 2,000 sq ft” supplies a
defensible delta. A stated total proposed area is never treated as added area.
The planning normaliser explicitly preserves the structured additional,
existing and proposed floor-area fields consumed by this rule.
Without an explicit delta the model uses a small category-based option signal;
all planning adjustments are rounded and capped. The base, adjustment and
final estimate remain separately visible in the stored record.

A later LLM renderer can replace the prose layer without changing this
architecture, but it must receive only the fact packet, return structured
claims referencing supplied fact/evidence IDs, pass the same validator and
write its model/prompt version into `story.generator`. Raw source payloads must
not be used to improvise unsupported claims.

## Persistence and rebuild behaviour

The logical record fingerprint excludes lifecycle timestamps and other fetch
timestamps. Therefore:

- an unchanged rerun keeps `createdAt`, `updatedAt` and `recordVersion`;
- a changed logical record keeps `createdAt`, advances `recordVersion`, updates
  `updatedAt`, and receives a new fingerprint;
- a new entrant starts at record version 1;
- the dataset fingerprint hashes sorted `(propertyId, record fingerprint)`
  pairs and protects the complete publication.

Output is written to a temporary sibling and atomically replaced. A failed
build cannot leave a half-written JS assignment.

## Monthly build order

The live-data workflow should run in this order:

1. collect and classify the Land Registry monthly transaction universe;
2. enrich property context (coordinates, schools, station, environment, EPC);
3. build/refresh complete sales history keyed by canonical property ID;
4. build/refresh licensed property-level planning history and its coverage
   metadata;
5. build property records using the existing output as `prior_records`;
6. validate schema, source references, transaction uniqueness, event order,
   coverage truthfulness, stories, counts and both fingerprints;
7. publish the transaction/history/property-record files as one release unit.

The installed `.github/workflows/monthly-property-refresh.yml` performs that
sequence and gates the commit on strict property-record validation. The sales
and planning history workflows also rebuild the same artifact in their commit,
so a newly arrived evidence overlay cannot leave the stored story stale.

Local reproducible example:

```bash
python3 scripts/build_property_records.py \
  --transactions outputs/surrey-transactions.js \
  --sales-history outputs/sales-history.js \
  --planning-history outputs/planning-history.js \
  --prior-records outputs/property-records.js \
  --output outputs/property-records.js \
  --as-of 2026-07-19

python3 scripts/validate_property_records.py \
  outputs/property-records.js \
  --transactions outputs/surrey-transactions.js
```

On the first run, omit `--prior-records`. On subsequent runs it may also be
omitted because the builder automatically reuses the existing output. Use
`--no-prior` only for a deliberate clean reconstruction.

## Publication and UI rules

- Data and story are read from the same record snapshot/fingerprint.
- A card must not combine new facts with an old story.
- Timeline events are ordered by `(date, type, eventId)` and are presentation
  neutral.
- The stored story is displayed from `record.story`. If a cached app receives a
  newer transaction, EPC or local planning overlay before the next monthly
  snapshot, seed reuse is rejected unless model/CPIH version, valuation date,
  latest sale date/price, target area/type, area-staleness state and transaction
  universe signature still match. The browser then runs the same governed
  fallback, and the next successful pipeline run persists it. When the
  governed live rendering is byte-for-byte identical to the stored two
  paragraphs, the panel uses the persistent story directly.
- Contextual nearby planning is labelled nearby and never merged with the
  property planning timeline.
- Missing airport/station/school data shows an unavailable/empty state; it does
  not fall back to an invented nearest place.
- Internal match quality remains available for audit and future confidence UI,
  even when simplified out of the customer panel.

## Audit acceptance gate

A release is acceptable only when the validator confirms:

- every input transaction is linked once and only once;
- every property has a non-empty evidence-backed story;
- every event and story claim references existing evidence;
- planning `checked_none` has explicit full-history proof;
- event order and all IDs are deterministic;
- metadata counts match recomputed counts;
- record and dataset fingerprints recompute exactly;
- no unsupported causal or absolute planning language appears;
- every story has exactly two paragraphs and ends with a positive estimated
  current value;
- comparable count equals the rows actually used by the chosen channel;
- category-B/unknown comparables and stale EPC denominators do not enter the
  price-per-square-foot channel;
- planning uplift references occur after both the latest sale and EPC, and
  total scheme area cannot be mistaken for incremental area.
- each record passes the checked-in draft-2020-12 JSON Schema using the
  dependency-free publication validator, in addition to the cross-record and
  valuation invariants above.

Source availability is not concealed by this gate. A record can exist and tell
the supported part of its story while a source is explicitly `unavailable`.
Strict publication policy may additionally reject `partial`, `not_checked` or
`failed` coverage until its upstream enrichment has completed.
