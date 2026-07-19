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

Current audited release: **v1.5.1 (build 17)**, completed 19 July 2026 for
internal investor demonstration. The release audit and machine-readable
manifest are in [RELEASE-v1.5.1.md](RELEASE-v1.5.1.md) and
[RELEASE-v1.5.1.json](RELEASE-v1.5.1.json).

The current macOS package is published at:

```text
https://raw.githubusercontent.com/surreyhillsprime/insight-data/main/downloads/INSIGHT-macOS.zip
```

SHA-256: `e1d640bfac647c8741c552619c4b17c1b017a5323ccd3d012465227bd63a5d0a`

This package is strictly ad-hoc signed for controlled internal use. It is not
Developer ID signed or Apple-notarized and must not be represented as an
external commercial distribution build. The remaining rights and repository
history reviews are recorded in [NOTICE.md](NOTICE.md).

The package contains the matching private-estate registry and app code. On
first launch, INSIGHT downloads the current base feed from this repository.
The installer and feed are released together whenever their required estate
registry version changes.

## V2 Workflow Schedule

INSIGHT now uses separate refresh jobs so high-change sources stay fresh without forcing every heavy source to run daily.

| Workflow | Runs | What it refreshes |
| --- | --- | --- |
| `daily-intelligence.yml` | Daily at 05:15 UTC | Recent planning applications near tracked properties, with explicit unknown coverage when the live source cannot prove a result |
| `weekly-context.yml` | Mondays at 07:15 UTC | Planning constraints, listed-building matches, conservation/heritage overlays, and schools if an approved GIAS CSV feed is supplied |
| `monthly-property-refresh.yml` | 1st of each month at 06:00 UTC | Land Registry, EPC floor areas, GBP/sq ft, and live flood-alert context |
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
The collector defaults to local-only output; the workflow must explicitly use
`--deployment-mode commercial`, and the publication validator rejects a local
feed. That is an engineering gate, not a substitute for checking that the
product remains a residential property price information service within the
HM Land Registry address-data permission recorded in [NOTICE.md](NOTICE.md).

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
- lookup progress is written into the private `work/epc-cache.json`
- the runner encrypts it with the existing EPC bearer secret before GitHub Actions persists the ciphertext outside the repository
- the next run decrypts it only on the runner and continues instead of starting again

The published row contains only the approved EPC-derived fields. Certificate
numbers, certificate addresses, match scores, search diagnostics and source
addresses are not part of the publication contract.

If the job stops because of repeated real API errors, it still fails.

## Optional Secrets

These are useful, but the workflows will still run if they are not supplied.

```text
SCHOOLS_CSV_URL
```

The approved DfE Get Information about Schools bulk CSV URL. If omitted, the
weekly job skips school enrichment and the app hides the school section unless
existing field-minimised school data is present. The current GIAS path does not
claim an Ofsted rating.

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

`work/epc-cache.json` and its encrypted form are deliberately excluded from
Git. The monthly workflow saves only ciphertext in the Actions cache and
decrypts it transiently on the runner. Rotating the EPC bearer token makes an
old cache unreadable; the workflow then starts a fresh cache.

`work/property-context-cache.json` is also operational-only request state and
is ignored by Git. Reviewed postcode-centroid results are published only in the
field-allowlisted transaction feed; Overpass payloads are not published.

`work/daily-intelligence-cache.json` is an off-repository Actions cache. It is
never a product artifact; only planning observations that pass the circular
distance, pagination and truthfulness gates reach the canonical feed.

`SCHOOLS_CSV_URL` must identify the approved DfE Get Information about Schools
bulk download. The raw CSV is transient and ignored by Git; only the
field-minimised nearby-school summaries allowed by the publication contract
are written to the public feed.

## Data Behaviour

The enrichment scripts are source-aware:

- If a source returns usable data, INSIGHT writes it into the property record.
- If a source is unavailable or no optional secret is supplied, the script skips that section.
- The app hides empty sections rather than showing blanks.
- Existing useful enrichment is not wiped just because a public API has a bad day.
- Planning Data's square API prefilter is reduced to the stated circular radius
  using measured point distance, and every declared result page is consumed
  before a latest observed application can be shown.
- The shared writer has a fail-closed top-level field allowlist. A new public
  field requires an explicit code review and contract update.

Source licences, required attributions, publication boundaries and unresolved
external-review items are recorded in [NOTICE.md](NOTICE.md).

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
