# Native sales dataset v1

`scripts/build_sales_dataset.py` produces `outputs/sales-dataset.json` offline
from the canonical transaction feed and the matching commercial sales history.
It does not fetch sources or use EPC/private context. Monthly property refresh
and sales-history refresh publish the dataset with their validated source feeds.

The outer JSON object has exactly these fields:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Integer `1`. |
| `publishedAt` | UTC publication timestamp, used to order replacement snapshots. |
| `sourceCheckedAt` | The base ledger's verified official acquisition time. Never the build clock or latest sale date. |
| `sourceRefreshFrom` | Lower date bound of the base ledger acquisition; rolling annual refresh normally checks the current and preceding year. Earlier cached history is not claimed to have been freshly retrieved. |
| `contentSha256` | Lowercase SHA-256 of the exact UTF-8 bytes of `payload`. |
| `payload` | A JSON **string**, not an embedded object. |

The decoded payload has exactly `schemaVersion`, `scope`, `metadata`,
`transactions`, `historyByProperty`, and `historyMetadata`. The scope is
`surrey-residential-2m-1995`. Export allowlists are defined in the producer and
contain only scalar canonical HMLR facts and reviewed property/estate identity.
Histories use canonical property keys; raw HMLR transaction identifiers,
provider URLs, UPRNs, EPC measurements, planning and context fields are omitted.
History transaction counts and latest-sale entries are recomputed after
projection. Each history retains its actual `updatedAt`, and history metadata
retains its actual `sourceCheckedAt` (oldest complete lookup) and `updatedAt`.
Those history timestamps are part of the content digest; outer clocks are not.

Transactions and histories form one replacement snapshot. A later publication
may decrease row counts or its latest sale date because HMLR corrections and
deletions must be accepted. A successful unchanged check can advance the outer
timestamps without changing the content digest. Consumers must not append new
sales to retained old sale events: that would resurrect withdrawn evidence.

The base sweeper records `sourceFetchStatus=verified`, `sourceCheckedAt`,
`sourceRefreshFrom`, and `sourceRefreshMode` only after official retrieval
succeeds. If both current SPARQL and rolling annual-archive retrieval fail, it
fails without replacing the publication. Explicit local/cache rebuilds retain
old check timestamps with `sourceFetchStatus=retained`; they cannot publish a
new native dataset. Both source checks must be no older than 45 days. The
existing history validator also enforces the reviewed coverage floor, identity,
rights, exclusions and per-property freshness.

The first release intentionally does not manufacture a dataset from the old
feed, which has no verified base-acquisition timestamp. The next successful
official acquisition supplies that provenance. Until a valid snapshot exists,
the native consumer must retain its signed bundled dataset.

Consumers retain signed private EPC/context evidence by exact canonical
property identity and replace only the HMLR facts. Native Ask and property
timelines must use that same active sales generation. Public property records
are not published upstream and must not overwrite private installed records.

Use `python3 scripts/build_sales_dataset.py --check` to verify that an existing
dataset still matches its exact source feeds and freshness requirements. The
daily completeness workflow uses `--validate` to validate the self-contained
snapshot independently after the first dataset exists.

The manual `sales-dataset-feed.yml` workflow performs an official HMLR sweep
and targeted HMLR history alignment in runner-temporary files, publishing only
the allowlisted JSON. It makes no EPC or context requests and does not replace
the legacy enriched public feeds. Changed exact canonical properties (including
corrections, removed sales, and old/new identities of address corrections) bypass
the fresh history seed and postcode cache. Unchanged properties keep their true
prior lookup timestamps. The monthly workflow also uses the prior dataset for
this invalidation. The separate history workflow skips native dataset generation
with an explicit notice if its legacy base has no verified acquisition metadata;
it continues its established history/Today publication without forging freshness.
