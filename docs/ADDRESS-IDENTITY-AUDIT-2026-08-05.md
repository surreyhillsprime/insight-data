# Address identity audit — 2026-08-05

## Outcome

The Surrey £2m+ base feed retains all 4,704 HM Land Registry transaction rows
and all 4,704 stable transaction IDs while consolidating 3,967 source address
identities to 3,766 canonical properties. The net reduction is 201 phantom
property identities; no sale row is deleted.

`Chimneys, Yaffle Road, Weybridge, KT13 0QF` is one property. Its 2004 source
row repeated `WEYBRIDGE` as both locality and town. The canonical base feed
now gives the 2004 and 2020 £2m+ rows the same address and
`propertyRecordId`; the complete sales-history record also includes its 2001
sale.

## Matching boundary

The migration uses three bounded identity rules:

1. exact structured `SAON + PAON + street-or-locality + postcode`;
2. a guarded complete numeric PAON signature, with source-classification and
   same-day sale-fact conflict checks;
3. 25 evidence-reviewed alias groups in
   `config/property-address-aliases.json`.

The complete number signature keeps examples such as `PLOT 1, 12` separate
from `12`. Distinct SAONs remain distinct. No UPRN, postcode centroid,
proximity, planning similarity or fuzzy name score controls identity.

## Published audit trail

The canonicalisation metadata records:

- 413 rows whose structured/display address changed;
- 345 redundant locality/town repetitions removed;
- 138 exact structured alias groups;
- 37 guarded numbered alias groups;
- 25 reviewed registry groups;
- 368 canonical properties with retired source identities;
- 374 exact legacy-to-canonical mappings;
- 201 net identities consolidated.

The 374 mappings include 173 one-for-one address cleanups as well as the 201
net duplicate reduction. They are the sole authority for downstream identity
migration. Stable transaction-ID correspondence is retained, and repeat writes
preserve the richest valid pre-rewrite ledger.

## Verified repeat-sales and velocity impact

The migration changes property grouping, not sale evidence. Side-by-side
runtime verification found all 4,704 transaction IDs, prices, dates and cohort
assignments unchanged. No transaction was removed, altered or moved across a
cohort, so the activity and velocity cohort counts and their cutoff remain
unchanged.

Canonical grouping raises the qualifying runtime repeat-sale pairs from 284 to
299: 15 genuine pairs that were previously hidden across duplicate property
identities. The overall bounded CPIH-adjusted annualised median consequently
moves from +1.203% to +1.016%, displayed as +1.2% to +1.0%.
The only raw retirement/replacement is a canonical-ID spelling normalisation
from `D'ABERNON` to `DABERNON`; its underlying sale evidence is unchanged.

| Market | Qualifying pairs | Annualised median |
|---|---:|---:|
| Elmbridge | 132 -> 142 | +0.6% -> +0.4% |
| Guildford | 27 -> 28 | +0.8% -> +0.7% |
| Reigate | 18 -> 20 | +1.3% (unchanged) |
| Runnymede | 14 -> 16 | +1.8% -> +1.0% |

| Town | Qualifying pairs | Annualised median |
|---|---:|---:|
| Cobham | 38 -> 42 | -0.3% -> -0.6% |
| Esher | 28 -> 31 | +1.7% (unchanged) |
| Guildford | 19 -> 20 | +1.4% -> +1.2% |
| Leatherhead | 32 -> 33 | +2.4% -> +2.3% |
| Tadworth | 11 -> 13 | +1.0% (unchanged) |
| Virginia Water | 10 -> 12 | +2.7% -> +1.4% |
| Walton-on-Thames | 18 -> 20 | +0.8% (unchanged) |

| Private estate | Qualifying pairs | Annualised median |
|---|---:|---:|
| Burwood Park | 4 -> 5 | +1.4% -> +2.4% |
| Crown Estate Oxshott | 8 -> 9 | +1.9% -> +1.4% |
| Wentworth | 8 -> 9 | +2.7% -> +2.2% |

## Dependent evidence

The complete public sales-history feed is re-keyed from its previously
validated evidence rather than re-scraped: 3,766 canonical records, 4,704
transaction aliases and 6,775 source sales are retained. Commercial validation
uses a 99% property-denominator floor, a 6,735-sale floor and a maximum of four
unavailable properties.

Historic England evidence is migrated only through the exact source-address
ledger. All superseded IDs remain in `retiredMappings`. The reconciled
property-grain result is 142 confirmed listed properties, 3,603
`no_direct_match` properties and 21 unknown properties. Tuscan House is the
only mixed-status consolidation: its earlier exhaustive no-direct review
outranks a later automated fail-closed unknown identity.

Public `outputs/planning-history.js` remains the exact zero-record blocked
placeholder until licensed redistribution is enabled. The licensed workflow's
coverage gate is now 79% of the current property denominator, so identity
changes cannot make a stale absolute threshold appear healthy.
