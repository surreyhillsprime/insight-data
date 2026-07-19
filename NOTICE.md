# INSIGHT data rights and publication notice

Last reviewed: 2026-07-19. This register records engineering controls and
source terms; it is not legal advice or a completed data-protection assessment.

## Public sources and boundaries

| Source | Current public use | Terms and boundary |
| --- | --- | --- |
| HM Land Registry Price Paid Data | Base transaction ledger and `sales-history.js` | [Official Price Paid Data terms](https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads) permit commercial or non-commercial reuse under OGL v3, but the embedded OS/Royal Mail address data is permitted only for personal/non-commercial use or display in a residential property price information service. Any broader product use requires a fresh rights review. Required attribution: Contains HM Land Registry data © Crown copyright and database right 2021. This data is licensed under the Open Government Licence v3.0. |
| MHCLG energy certificate data | EPC rating, registration date, floor area and derived GBP/sq ft in the base ledger | [Licensing restrictions](https://get-energy-performance-data.communities.gov.uk/guidance/licensing-restrictions) put non-address fields under OGL v3 but restrict EPC address/postcode data. [Data-protection guidance](https://get-energy-performance-data.communities.gov.uk/guidance/data-protection-requirements) says address-level EPC data is personal data. v1.5.1 excludes certificate numbers, EPC addresses, match scores and search diagnostics from Git and keeps the lookup cache private. |
| Planning Data | Recent applications, constraints and listed-building context | [Planning Data terms](https://www.planning.data.gov.uk/terms-and-conditions) say most content is OGL unless marked otherwise. Each dataset/provider exception must be checked before adding a field. |
| Environment Agency real-time flood API | Flood alert context | [API terms and attribution](https://environment.data.gov.uk/flood-monitoring/doc/reference) publish the API under OGL. Attribution required: this uses Environment Agency flood and river level data from the real-time data API (Beta). |
| OpenFreeMap / OpenMapTiles / OpenStreetMap vector basemap | Label-free roads, rail, water, land use and buildings for the internal app | OpenFreeMap permits commercial use and requires OpenMapTiles/OpenStreetMap attribution, which remains visible in the app. Its [terms](https://openfreemap.org/tos/) provide the public service as-is, without an SLA, and prohibit automated collection without permission. INSIGHT performs normal interactive requests only and does not prefetch or redistribute tiles. A contracted or self-hosted production basemap remains an external-release gate. |
| OpenStreetMap via Overpass | Optional local-only research context | v1.5.1 removes this field and its operational cache from the current release tree because it is unused by the app and its derived-database boundary has not been separately reviewed. Any future publication must comply with [OpenStreetMap attribution and ODbL requirements](https://www.openstreetmap.org/copyright). |
| OS Open UPRN | UPRN/coordinate linkage | [OS Open UPRN](https://www.ordnancesurvey.co.uk/products/os-open-uprn) and the [Open Identifiers policy](https://www.ordnancesurvey.co.uk/products/open-mastermap-programme/open-id-policy) make the open identifier product available under OGL. Preserve the current OS attribution for any published use. |
| Companies House API | Optional local-only research | v1.5.1 does not publish company, PSC or filing fields. Future property-level publication requires a documented necessity, privacy and product-rights review even though the [official API](https://developer.company-information.service.gov.uk/) exposes public company information. |
| DfE Get Information about Schools (GIAS) public download | Field-minimised nearby-school summaries and bundled map points | The base feed uses name, phase, URN, postcode and distance. The app's map asset additionally uses type/status, town, coordinates, pupil/capacity, age range, gender/admissions, website and inspectorate. GIAS permits bulk ingestion from its public downloads with the Open Government Licence where stated. The workflow must use that approved bulk download, not scrape the service UI. Raw full addresses, head names, trust/governance, religion, boarding, nursery, sixth-form, FSM and SEN fields are excluded from the current release. Attribution: Contains public sector information licensed under the Open Government Licence v3.0. |
| Private-estate research registry | Exact-road property classification | Evidence citations and review status live in `config/`. The classification is not a legal estate boundary, title plan or ownership statement. |
| Licensed planning-history path | Separate `planning-history.js` | Deliberately dormant until written commercial redistribution permission and provider coverage are approved. |

## Publication controls

- `scripts/insight_data_utils.py` owns a fail-closed allowlist for every
  top-level field in `outputs/surrey-transactions.js`.
- `scripts/check_data_completeness.py` and CI reject restricted or unreviewed
  public fields.
- `work/epc-cache.json` is ignored by Git. The runner encrypts it with the EPC
  bearer secret and saves only ciphertext in the off-repository Actions cache;
  raw API response/matching state exists only transiently on the runner.
- Raw GIAS downloads and postcode/Overpass request caches are ignored by Git;
  only field-minimised, allowlisted release data is committed.
- `deploymentMode: commercial` is an engineering gate, not legal approval.

## External review still required

Before describing the product as legally or commercially "watertight" for a
broad external launch, obtain and record:

1. a UK GDPR/DPA 2018 lawful-basis and proportionality review for linking
   EPC-derived values to address-level Price Paid records, even after direct EPC
   identifiers and addresses are removed;
2. approval for any future licensed planning-history source;
3. a privacy/necessity decision before publishing Companies House PSC or filing
   data at property level;
4. confirmation that future uses of HM Land Registry address fields stay within
   the residential property price information permission; and
5. a contracted or self-hosted production basemap decision covering SLA,
   privacy, caching, offline use and redistribution; and
6. a repository-history remediation decision: releases before v1.5.1 committed
   the EPC lookup cache/raw match fields, the raw GIAS CSV, and an operational
   postcode/Overpass cache. This release removes them from the current branch
   and prevents recurrence, but deliberately does not rewrite earlier Git
   history.
