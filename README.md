# INSIGHT Data Feed

This repository publishes the shared Land Registry data feed for INSIGHT.

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

The sweep runs `scripts/sweep_land_registry.py`, updates `outputs/surrey-transactions.js`, and commits the new data back to this repository.

## Data Scope

- Surrey Land Registry sales
- GBP 3m+
- Residential property types
- From 2010-01-01
