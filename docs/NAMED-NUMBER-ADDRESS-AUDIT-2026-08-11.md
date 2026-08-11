# Named and numbered property-address audit — 11 August 2026

## Outcome

The complete published Surrey £2m+ transaction universe was checked for
properties split between a house name and a street number.

- 4,704 published transactions resolve to 3,766 canonical properties.
- 37 historical name-plus-number versus number-only groups are already
  canonicalised to the named form.
- 25 evidence-reviewed alias groups cover other verified address changes.
- 201 source identities are collapsed without removing a transaction.
- No two active properties share the guarded numbered-delivery-point key.
- No HMLR sale-history transaction is attached to more than one active
  property.
- The public property-UPRN feed contains no duplicate UPRN ownership.

The 37 historical numbered aliases reconcile exactly to the feed metadata.
Their canonical names are: Ballyfin House, Beech House, Blenheim House, Blu
Lotus, Bourne View, Brompton Court, Cedar House, Dorset Cottage, Greystones,
Highclere House, Hillgrove House, Jasmine House, Kestrel Grange, La Perle,
Lynncroft House, Montrachet, Morningside, Oak House, Old Greenwich, Oloidien,
Onslow House, Osprey House, Otters Leap, Pine Acre, Pine House, Pines Glen,
Pipers Croft, Rythe Head House, Silkways, Somersby, Southborough House,
Spindlewood, The Lantern House, Torosa, Virginia House, Woodhayes and York
House.

## Wider structural review

A deliberately looser comparison paired name-only and number-only properties
on the same route and postcode. It produced 118 possible pairings across 26
route/postcode groups. Most were disproved by distinct property or planning
identifiers, or by an address that explicitly attached the name to a different
number.

Twenty-five pairings across five groups remain genuinely ambiguous and were
not merged:

| Route and postcode | Possible pairings | Decision |
|---|---:|---|
| Fort Road, GU1 3TE | 16 | Review only |
| Woodland Way, KT20 6NW | 5 | Review only |
| Heath Rise, GU25 4AX | 2 | Review only |
| Mellersh Hill Road, GU5 0QJ | 1 | Review only |
| Virginia Avenue, GU25 4RY | 1 | Review only |

Postcode centroids, price similarity, estate membership and fuzzy name
similarity are not accepted as property identity evidence.

## Coromandel

Coromandel was not duplicated. The source universe contained one property and
one sale under `33, FAIRMILE AVENUE, COBHAM, KT11 2JA`.

A bounded official HMLR SPARQL lookup for postcode KT11 2JA independently
confirmed the source transaction as PAON `33`, with no Coromandel occurrence.
The correction is therefore presentation enrichment, not the merger of two
HMLR identities.

The product owner confirmed that its current house name is Coromandel. A
separate owner-reviewed presentation registry therefore changes the canonical
display to:

`COROMANDEL, 33, FAIRMILE AVENUE, COBHAM, KT11 2JA`

The old numbered property ID is retained in `sourceAddressVariants`; every
transaction, sale-history alias, heritage decision and indicative parcel link
is migrated to the new canonical key. This rule applies only to Coromandel and
does not promote unreviewed names from private planning data.

## Fresh official-source check

The official 2026 HMLR annual archive was downloaded and all 252,062 national
rows were scanned with the production parser. It contained 33 Surrey
residential transactions at or above £2m through 19 June 2026. One is the
reviewed 100-times price input error at 42 Cherry Tree Lane and remains
intentionally excluded by `config/transaction-exclusions.json`. The remaining
32 rows match the 32 eligible 2026 rows in the feed exactly. They contain no
name-plus-number versus number-only split. The bounded source was HMLR's
`https://price-paid-data.publicdata.landregistry.gov.uk/pp-2026.csv` archive.

An all-history official SPARQL query timed out. Downloading every 1995–2026
annual archive would transfer approximately 5.117 GiB, so that second raw
reconstruction was not started. The full checked-in structured universe,
source-variant ledger, sales history and dependent identity feeds were still
audited in full; the fresh raw boundary above covers every 2026 row.

## Ongoing controls

The producer continues to:

1. merge only an exact name-plus-number versus number-only match with the same
   unit, complete numeric signature, route and postcode and compatible sale
   facts;
2. prefer the named PAON for display;
3. preserve retired IDs and source addresses;
4. fail validation if a guarded numbered delivery point survives under two
   active property IDs; and
5. require an explicit reviewed presentation entry for a name not supplied by
   HMLR.
