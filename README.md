# INSIGHT Data Feed

This repository publishes the shared data feed for INSIGHT.

The Mac app uses this canonical base-feed URL:

```text
https://raw.githubusercontent.com/surreyhillsprime/insight-data/main/outputs/surrey-transactions.js
```

The feed is the canonical Surrey £2m+ property and transaction universe from
1995 onwards. Pre-2010 and 2010+
transactions are not separate product datasets. Each row retains the structured
HM Land Registry address fields used by the exact, fail-closed private-estate
classifier.

The transaction feed uses schema 3. Every row carries a canonical
`propertyRecordId` derived from its full normalised address and postcode; an
unavailable postcode remains the explicit `NOPOSTCODE` identity and approximate
UPRNs never control property identity. Publication is fail closed against a
reviewed field allowlist. EPC certificate identifiers and matched addresses,
match diagnostics, OpenStreetMap payloads, Companies House payloads, and
property-level planning history remain outside the public transaction ledger.

Address identity is canonicalised across the complete transaction snapshot
before publication. Exact structured HMLR delivery points, guarded complete
house-number signatures and the small reviewed registry in
`config/property-address-aliases.json` may consolidate source variants; flats,
redevelopment plots and ambiguous nearby titles remain separate. Metadata
retains every superseded source identity in
`addressCanonicalisation.sourceAddressVariants`, allowing downstream history
and reviewed evidence to migrate deterministically without fuzzy matching.
See `docs/ADDRESS-IDENTITY-AUDIT-2026-08-05.md`.

## Install INSIGHT

The current public-safe INSIGHT v2.0.0 (build 37) macOS package is published at:

```text
https://raw.githubusercontent.com/surreyhillsprime/insight-data/main/downloads/INSIGHT-macOS.zip
```

The package contains the matching private-estate registry and app code, but no
private council-portal planning history. Planning remains explicitly
unavailable until a separately licensed commercial publication is configured.
On first launch, INSIGHT downloads the current base feed from this repository.
The installer and feed are released together whenever their required estate
registry version changes.

## V2 Workflow Schedule

INSIGHT now uses separate refresh jobs so high-change sources stay fresh without forcing every heavy source to run daily.

| Workflow | Runs | What it refreshes |
| --- | --- | --- |
| `daily-intelligence.yml` | Daily at 05:15 UTC | Recent planning applications near tracked properties; Companies House publication remains disabled |
| `heritage-listed-buildings.yml` | Daily at 08:15 UTC | Official Historic England NHLE listed-building evidence, reviewed property mappings and explicit coverage states |
| `weekly-context.yml` | Mondays at 07:15 UTC | Planning constraints, conservation/heritage overlays, and schools if a school CSV feed is supplied |
| `planning-history-feed.yml` | Mondays at 06:45 UTC when licensed-source publication is enabled | Complete property-level planning histories from the approved redistribution source |
| `monthly-property-refresh.yml` | 1st of each month at 06:00 UTC | Land Registry, EPC floor areas, GBP/sq ft, and live flood-alert context; OSM amenity publication remains disabled |
| `sales-history-feed.yml` | 2nd of each month at 06:30 UTC | Complete HM Land Registry Price Paid history for properties in the base feed |
| `six-week-os-refresh.yml` | Guarded Sunday schedule | OS Open UPRN matching and geometry/linkage improvement when an OS CSV is supplied |
| `data-completeness.yml` | Daily at 11:00 UTC | Validates historic coverage, source-level minimums and enrichment metadata |

`monthly-land-registry-sweep.yml` has been left as a manual legacy fallback only. The scheduled monthly job is now `monthly-property-refresh.yml`.

OpenStreetMap amenity payloads and Companies House payloads are deliberately
disabled in public producer workflows pending an approved redistribution basis.
This rights boundary does not reduce the £2m+ property universe processed by
the active Land Registry, EPC, flood, planning, school, UPRN and estate streams.

Every active producer workflow runs `scripts/check_data_completeness.py` before
committing its result. A separate daily audit runs the stricter metadata check
so a stale percentage or a major source-coverage regression is visible even
when no producer workflow is being run manually.

The monthly workflow now carries the Land Registry expansion through the full
dependency chain. After the base/EPC/property job commits, a second job aligns
planning constraints, schools, recent planning intelligence, OS UPRN data and
the dedicated Historic England designation layer with every newly added
transaction before committing the shared feed.
The base sweep preserves existing enrichment fields on unchanged transactions.

## Listed-building evidence

`scripts/enrich_listed_buildings.py` downloads the official National Heritage
List for England point layer and the much smaller set of genuine positive-area
building outlines from Historic England's ArcGIS FeatureServer. It validates
the pinned service item, required fields, pagination, grades, List Entry
Numbers, WGS84 Surrey search envelope and source counts before considering a
publication. ArcGIS can return multipoint or polygon features whose overall
geometry envelope touches the query while the relevant designation point is
outside it. Raw pages must still reconcile to the server's declared counts;
the producer then clips point members to the exact approved envelope and drops
boundary polygons without retained point evidence under strict count,
percentage and retained-cohort gates.

Matching is performed once per canonical full-address `propertyRecordId`.
Every transaction for that property receives the same compact
`historicEngland` object:

```json
{
  "status": "confirmed_listed",
  "entries": [
    {
      "listEntryNumber": "1234567",
      "grade": "II*",
      "name": "EXAMPLE HOUSE",
      "url": "https://historicengland.org.uk/listing/the-list/list-entry/1234567",
      "matchMethod": "reviewed_override",
      "matchConfidence": "confirmed"
    }
  ],
  "source": "Historic England NHLE",
  "checkedAt": "2026-07-26T12:00:00Z",
  "sourceUpdatedAt": "2026-07-25T23:00:00Z",
  "sourceSnapshot": "nhle-2026-07-25-0123456789ab"
}
```

The public contract retains four coverage states: `confirmed_listed`,
`candidate_review`, `no_direct_match` and `unknown`. The current production
ledger does not publish centroid-generated candidates. Every current property
was screened against the official address corpus first; a missing future ledger
decision remains `unknown`. `no_direct_match` means only that the completed
screen found no supported direct NHLE property identity. It is not a legal
assertion that the property is unlisted or outside listed-building curtilage.
Automatic confirmation outside the ledger is limited to a genuine Historic
England positive-area outline containing a trusted property-level coordinate.

For a confirmed reviewed mapping with exactly one List Entry Number and one
official NHLE point, the sync may replace a Postcodes.io postcode-centroid map
position with that designation point. The original centroid is preserved as
`geocode.postcodeCentroidLatitude` / `postcodeCentroidLongitude`, and every
later match is made from that preserved base view so the display refinement
cannot confirm itself. Removing the mapping, or encountering multiple entries
or multiple official points, restores the original coordinate and provenance.
Trusted address/rooftop/property points and polygon-derived confirmations are
never moved. `heritageSync.confirmedLocationsApplied` records the exact number
of properties refined under
`single-reviewed-entry-single-nhle-point`.

The complete address-screened decisions live in
`config/heritage-listing-overrides.json`: one mapping for each of the 3,947
current canonical properties. Each mapping is keyed by the full-address
Property Record identity and records reviewer/date evidence. Confirmed mappings
retain every applicable seven-digit List Entry Number; the remaining mappings
carry the explicit `no_direct_match` interpretation above. Review decisions
must pass `config/heritage-listing-overrides.schema.json` and the stricter
runtime checks before publication.

`config/heritage-listing-address-audit.json` pins the address-corpus and
official-document evidence, source hashes, criteria, 143 confirmed properties,
140 unique NHLE entries and the canonical decision digests. The original
47-property phase remains preserved in
`config/heritage-listing-initial-audit.json`. Run
`scripts/build_heritage_address_ledger.py --check` to prove that the expanded
production ledger still matches the compact final audit and the audited
property universe.

The writer is atomic and capped below the installed app's feed limit. A source
failure cannot replace the feed: a complete validated prior publication is
retained unchanged, while the workflow exits non-zero so maintainers are
alerted immediately. A first run with no last-known-good heritage state also
fails closed. Successful unchanged snapshots are left untouched to avoid
timestamp-only Git commits.

Planning uses two deliberately separate time horizons. The daily intelligence
job remains a rolling 45-day alert search. The licensed planning-history feed
imports each provider's complete available archive, records the earliest and
latest application years supplied, and searches each distinct property only
once even when it has several Land Registry transactions. The workflow is
scheduled but deliberately fails closed unless an approved redistribution
source and its public licence evidence are configured. EPC, constraints, flood,
school and OS enrichment is applied across every property in the expanded feed
using each source's full available or current coverage; those snapshot sources
are not artificially backdated to 1995.

`sales-history-feed.yml` publishes the separate complete Price Paid history
feed each month. Its postcode cache is resumable and its output is
`outputs/sales-history.js`. This is Price Paid transaction history from 1995
onwards, not the legal title register, ownership, deeds, or charges. The
published file is accepted only when it contains every canonical property and
every transaction lookup alias in the exact base feed, including the same
transaction-to-property pairings. Counts, record content and the base identity
universe are protected by SHA-256 fingerprints. The workflow rejects files
over 50 MiB, snapshots older than 45 days, fewer than 3,942 properties with
history, fewer than 6,735 matched Price Paid transactions, or more than the
reviewed four unavailable properties. Every canonical property is explicitly
accounted as complete or unavailable. The 28-day cache threshold ensures each
monthly run refreshes its postcode evidence before the 45-day publication
freshness window can expire.

Price Paid Data is reused under the Open Government Licence v3.0 for the
permitted purpose of displaying residential property price information. The
required attribution and the HM Land Registry address-data conditions are
recorded in `DATA-NOTICES.md` and in each publication's metadata.

The audited private-estate registry is published as
`outputs/private-estates.js`, with its evidence and exact road rules under
`config/`. Every matching transaction carries `estateId` plus the display
estate name. Every row, including unmatched rows, carries the registry version
that evaluated it. This is a property classification layer; INSIGHT does not
draw or claim legal estate perimeters.

`outputs/planning-history.js` remains a separate licensed-provider publication
path. It must not be populated from a source whose terms do not permit product
redistribution. A publishable snapshot must account for every canonical
property and transaction alias, reconcile every application array to metadata,
remain below 50 MiB, be no more than eight days old at workflow publication,
and retain the reviewed non-regression floors of 3,204 properties with history
and 21,180 applications. The 1,763 figure in the private collection audit is
the number of distinct council-portal/cache queries; it is not an application
count. Recent public planning context in the base feed is unaffected.

Until the licensed source is configured, the checked-in planning artifact
declares `blocked-missing-licensed-source`. INSIGHT v2 public-profile builds
accept only that exact zero-record placeholder or a complete licensed
commercial publication. The private 21,180-application council-cache snapshot
is not copied into this public repository or public installer.

The daily completeness workflow independently revalidates both standalone
history files against `outputs/surrey-transactions.js`. It applies the same
identity, count, size, rights, content-fingerprint, non-regression and freshness
gates used by their producer workflows. A missing or placeholder planning file,
or a stale/partial sales file, therefore leaves the production completeness
check red rather than silently passing the base-feed audit.

The scheduled market-enrichment workflows update:

```text
outputs/surrey-transactions.js
```

That is the file every installed INSIGHT app reads.

## Required Secret

Add this repository secret for EPC matching:

```text
Settings -> Secrets and variables -> Actions -> New repository secret
Name: EPC_BEARER_TOKEN
Value: your GOV.UK EPC API bearer token
```

The all-user planning-history workflow additionally requires:

```text
Repository variable: PLANNING_COMMERCIAL_ENABLED=true
Repository variable: PLANNING_SOURCE_NAME=<approved provider name>
Repository variable: PLANNING_SOURCE_LICENCE_URL=https://<public redistribution terms>
Repository secret:   PLANNING_DATA_SOURCE=<licensed CSV/JSON URL or source>
```

All four settings are mandatory. The workflow does not publish a local
council-portal cache when any setting is absent.

The monthly job will fail if this is missing, because EPC floor area is required for GBP/sq ft.

## EPC Rate Limits

The EPC API can rate-limit long first runs. The EPC script now treats a useful time-limit stop as a checkpoint:

- matched records are written into `outputs/surrey-transactions.js`
- lookup progress is written into `work/epc-cache.json`
- the workflow can still commit those files
- the next run continues from the cache instead of starting again

If the job stops because of repeated real API errors, it still fails.

## Optional Secrets

These are useful, but the workflows will still run if they are not supplied.

```text
COMPANIES_HOUSE_API_KEY
```

Used by the daily job, but only when an INSIGHT record already contains a company number from another source.

```text
SCHOOLS_CSV_URL
```

A direct CSV URL for school/location/rating data. If omitted, the weekly job skips school enrichment and the app hides the school section unless existing school data is present.

```text
OS_OPEN_UPRN_CSV_URL
```

A direct CSV URL for a Surrey-cut OS Open UPRN file. If omitted, the six-week OS job skips UPRN matching.

## Upload Checklist

### Existing `insight-data` Repository

If this repository is already live and has run EPC/Land Registry workflows, upload or replace only:

```text
.github/workflows/
scripts/
config/
outputs/private-estates.js
downloads/INSIGHT-macOS.zip
README.md
.nojekyll
```

Do not overwrite these live data files unless you deliberately want to reset the feed:

```text
outputs/surrey-transactions.js
work/epc-cache.json
work/property-context-cache.json
work/land-reg-surrey-2m-1995.csv
```

The new workflows will update the live data files themselves.

### Fresh Repository Seed

If this is a brand new empty repository, upload these folders/files:

```text
.github/workflows/
scripts/
outputs/surrey-transactions.js
outputs/private-estates.js
outputs/sales-history.js
config/
downloads/INSIGHT-macOS.zip
work/land-reg-surrey-2m-1995.csv
work/land-reg-surrey-2m-1995-2009.csv
work/land-reg-surrey-2m-2010.csv
.nojekyll
README.md
```

Do not delete cache files already created by a running workflow unless you deliberately want to force a full re-lookup.

## Data Behaviour

The enrichment scripts are source-aware:

- If a source returns usable data, INSIGHT writes it into the property record.
- If a source is unavailable or no optional secret is supplied, the script skips that section.
- The app hides empty sections rather than showing blanks.
- Existing useful enrichment is not wiped just because a public API has a bad day.

## Current Data Scope

Contains HM Land Registry data © Crown copyright and database right 2021.
This data is licensed under the Open Government Licence v3.0.

- Surrey Land Registry sales
- GBP 2m+
- Residential property types
- From 1995-01-01 (the beginning of HM Land Registry Price Paid Data)
- One unified 1995+ property and transaction database, with stable transaction and canonical property IDs
- Velocity maturity metadata derived from the latest observed Land Registry sale month
- Audited exact-road private-estate classification replayed across every row
- Estate registry version recorded on every matched and unmatched transaction
- Domestic EPC floor area where a confident address match is found
- GBP/sq ft calculated from Land Registry sold price divided by matched EPC floor area
- Optional public context where sources return usable data
- Official Historic England NHLE listed-building grades where address/document
  identity is confirmed, with no-direct-match and unknown coverage kept explicit

See `DATA-NOTICES.md` for attribution, licence and interpretation limits.
