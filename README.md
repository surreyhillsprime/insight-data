# INSIGHT Data Feed

This repository publishes the shared Land Registry and EPC-enriched data feed for INSIGHT.

The Mac app should use this feed URL:

```text
https://raw.githubusercontent.com/YOUR-GITHUB-USERNAME/insight-data/main/outputs/surrey-transactions.js
```

Replace `YOUR-GITHUB-USERNAME` with the GitHub username that owns this repository.

## Monthly Sweep

The workflow in `.github/workflows/monthly-land-registry-sweep.yml` runs on the 1st of every month at 06:00 UTC.

It can also be run manually from:

```text
Actions -> Monthly Land Registry Sweep -> Run workflow
```

The sweep runs `scripts/sweep_land_registry.py`, then runs `scripts/enrich_epc_data.py` if an EPC API token has been added. It updates `outputs/surrey-transactions.js` and commits the new data back to this repository.

## EPC Enrichment

INSIGHT uses the official GOV.UK "Get energy performance of buildings data" API to match domestic EPC certificates and add:

- EPC floor area in sq m and sq ft
- achieved GBP/sq ft
- EPC rating
- EPC match score and certificate reference

To enable this, add a repository secret:

```text
Settings -> Secrets and variables -> Actions -> New repository secret
Name: EPC_BEARER_TOKEN
Value: your GOV.UK EPC API bearer token
```

You get the token by signing in to the GOV.UK EPC data service with GOV.UK One Login and copying the bearer token from your account page.

The script keeps a cache at `work/epc-cache.json` so monthly updates only need to look up new or previously unmatched properties.

## Data Scope

- Surrey Land Registry sales
- GBP 3m+
- Residential property types
- From 2010-01-01
- Domestic EPC floor area where a confident address match is found
