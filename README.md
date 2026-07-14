# INSIGHT Data Feed

This repository publishes the shared data feed for INSIGHT.

The Mac app should use this feed URL:

```text
https://raw.githubusercontent.com/YOUR-GITHUB-USERNAME/insight-data/main/outputs/surrey-transactions.js
```

Replace `YOUR-GITHUB-USERNAME` with the GitHub username that owns this repository.

## V2 Workflow Schedule

INSIGHT now uses separate refresh jobs so high-change sources stay fresh without forcing every heavy source to run daily.

| Workflow | Runs | What it refreshes |
| --- | --- | --- |
| `daily-intelligence.yml` | Daily at 05:15 UTC | Recent planning applications near tracked properties, plus Companies House where a company number already exists |
| `weekly-context.yml` | Mondays at 05:35 UTC | Planning constraints, listed-building matches, conservation/heritage overlays, and schools if a school CSV feed is supplied |
| `monthly-property-refresh.yml` | 1st of each month at 06:00 UTC | Land Registry, EPC floor areas, GBP/sq ft, live flood-alert context, and OSM amenities |
| `six-week-os-refresh.yml` | Guarded Sunday schedule | OS Open UPRN matching and geometry/linkage improvement when an OS CSV is supplied |

`monthly-land-registry-sweep.yml` has been left as a manual legacy fallback only. The scheduled monthly job is now `monthly-property-refresh.yml`.

The monthly workflow now carries the Land Registry expansion through the full
dependency chain. After the base/EPC/property job commits, a second job aligns
planning constraints, heritage, schools, recent planning intelligence and OS
UPRN data with every newly added transaction before committing the shared feed.
The base sweep preserves existing enrichment fields on unchanged transactions.

`sales-history-feed.yml` is a deliberately dormant commercial publication path
for the separate complete Price Paid history feed. It has no schedule and its
job cannot run until the `SALES_HISTORY_COMMERCIAL_ENABLED` repository variable
is explicitly set to `true`. Its postcode cache is resumable; its published
output is `outputs/sales-history.js`. This is Price Paid transaction history
from 1995 onwards, not the legal title register, ownership, deeds, or charges.

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
work/land-reg-surrey-3m-1995.csv
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
- Domestic EPC floor area where a confident address match is found
- GBP/sq ft calculated from Land Registry sold price divided by matched EPC floor area
- Optional public context where sources return usable data
