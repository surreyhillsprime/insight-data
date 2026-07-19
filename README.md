# INSIGHT Data Feed

This repository publishes the shared data feed for INSIGHT.

The Mac app uses this canonical base-feed URL:

```text
https://raw.githubusercontent.com/surreyhillsprime/insight-data/main/outputs/surrey-transactions.js
```

The feed is one transaction ledger from 1995 onwards. Pre-2010 and 2010+
transactions are not separate product datasets. Each row retains the structured
HM Land Registry address fields used by the exact, fail-closed private-estate
classifier.

## Install INSIGHT

The current macOS package is published at:

```text
https://raw.githubusercontent.com/surreyhillsprime/insight-data/main/downloads/INSIGHT-macOS.zip
```

The package contains the matching private-estate registry and app code. On
first launch, INSIGHT downloads the current base feed from this repository.
The installer and feed are released together whenever their required estate
registry version changes.

## V2 Workflow Schedule

INSIGHT now uses separate refresh jobs so high-change sources stay fresh without forcing every heavy source to run daily.

| Workflow | Runs | What it refreshes |
| --- | --- | --- |
| `daily-intelligence.yml` | Daily at 05:15 UTC | Recent planning applications near tracked properties, plus Companies House where a company number already exists |
| `weekly-context.yml` | Mondays at 05:35 UTC | Planning constraints, listed-building matches, conservation/heritage overlays, and schools if a school CSV feed is supplied |
| `monthly-property-refresh.yml` | 1st of each month at 06:00 UTC | Land Registry, EPC floor areas, GBP/sq ft, live flood-alert context, and OSM amenities |
| `sales-history-feed.yml` | 2nd of each month at 06:30 UTC | Complete HM Land Registry Price Paid history for properties in the base feed |
| `six-week-os-refresh.yml` | Guarded Sunday schedule | OS Open UPRN matching and geometry/linkage improvement when an OS CSV is supplied |
| `data-completeness.yml` | Daily at 11:00 UTC | Validates historic coverage, source-level minimums and enrichment metadata |

`monthly-land-registry-sweep.yml` has been left as a manual legacy fallback only. The scheduled monthly job is now `monthly-property-refresh.yml`.

Every active producer workflow runs `scripts/check_data_completeness.py` before
committing its result. A separate daily audit runs the stricter metadata check
so a stale percentage or a major source-coverage regression is visible even
when no producer workflow is being run manually.

The monthly workflow now carries the Land Registry expansion through the full
dependency chain. After the base/EPC/property job commits, a second job aligns
planning constraints, heritage, schools, recent planning intelligence and OS
UPRN data with every newly added transaction before committing the shared feed.
The base sweep preserves existing enrichment fields on unchanged transactions.

Planning uses two deliberately separate time horizons. The daily intelligence
job remains a rolling 45-day alert search. The dormant licensed planning-history
feed imports each provider's complete available archive, records the earliest
and latest application years supplied, and searches each distinct property only
once even when it has several Land Registry transactions. EPC, constraints,
flood, school and OS enrichment is applied across every property in the expanded
feed using each source's full available or current coverage; those snapshot
sources are not artificially backdated to 1995.

`sales-history-feed.yml` publishes the separate complete Price Paid history
feed each month. Its postcode cache is resumable and its output is
`outputs/sales-history.js`. This is Price Paid transaction history from 1995
onwards, not the legal title register, ownership, deeds, or charges.

The audited private-estate registry is published as
`outputs/private-estates.js`, with its evidence and exact road rules under
`config/`. Every matching transaction carries `estateId` plus the display
estate name. Every row, including unmatched rows, carries the registry version
that evaluated it. This is a property classification layer; INSIGHT does not
draw or claim legal estate perimeters.

`outputs/planning-history.js` remains a separate licensed-provider publication
path. It must not be populated from a source whose terms do not permit product
redistribution. Recent public planning context in the base feed is unaffected.

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
work/land-reg-surrey-3m-1995.csv
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
work/land-reg-surrey-3m-1995.csv
work/land-reg-surrey-3m-1995-2009.csv
work/land-reg-surrey-3m-2010.csv
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
- GBP 3m+
- Residential property types
- From 1995-01-01 (the beginning of HM Land Registry Price Paid Data)
- One unified 1995+ transaction database, with stable transaction IDs
- Audited exact-road private-estate classification replayed across every row
- Estate registry version recorded on every matched and unmatched transaction
- Domestic EPC floor area where a confident address match is found
- GBP/sq ft calculated from Land Registry sold price divided by matched EPC floor area
- Optional public context where sources return usable data
