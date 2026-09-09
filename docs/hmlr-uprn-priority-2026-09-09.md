# HMLR UPRN priority and 36-property audit — 9 September 2026

HMLR is the primary transaction-to-UPRN source. Other sources remain backups where no HMLR relationship is available. This change joins official transaction UUIDs to existing canonical sale records and joins the supplied UPRNs to current OS Open UPRN coordinates. It does not use proximity to choose an alternative identifier or change canonical property identity.

## Result

- All 36 HMLR UPRNs resolve to the checked OS Open UPRN August 2026 archive.
- 24 new points; 11 existing points agree exactly; one conflicting identifier is replaced using HMLR.
- 11 Oakwood Road, Virginia Water moves 17.841 metres within its existing parcel. The predecessor is retained in the private app transition history.
- Combined point coverage: 3,266 → 3,290 of the production data repository’s 3,785 canonical properties. 495 remain without this point evidence.
- 48 transaction records belonging to these 36 properties receive primary UPRN context. Only `uprn` and `ordnanceSurvey` row fields change; the 4,739 sale records, canonical IDs, prices, dates and source transaction coordinates are preserved.
- 21 new parcel associations: 3,228 → 3,249. All 21 are uniquely contained more than two metres inside a parcel also named in the direct HMLR transaction-to-INSPIRE lookup. The other three remain unassociated.
- Existing app fallback evidence is preserved. The private merged feed contains 14 transitions: the original two plus 12 HMLR evidence upgrades. No underlying UPRNs or transition details enter the browser map projection.

## Parcel exceptions

| Property | Finding | Implemented outcome |
| --- | --- | --- |
| Flat 9, Kingsdown, Castle Hill, Farnham | HMLR supplies a leasehold UPRN but no direct freehold INSPIRE lookup. | Accept the address point; withhold a parcel association. |
| Woodlands Ryde, Chobham Park Lane | The lookup’s INSPIRE ID is absent from all 11 checked September Surrey source files; the OS point has no containing source polygon. | Accept the address point; retain the missing-parcel finding. |
| 15 Highlands Road, Reigate | The OS point is in a different INSPIRE polygon from the one HMLR directly links to the transaction. | Accept the HMLR UPRN point; withhold automatic parcel selection. |

## All 36 properties

| Property | UPRN point change | Parcel outcome |
| --- | --- | --- |
| 9, Pelhams Walk, Esher, Kt10 8Qa | Added | Added: HMLR lookup and containment agree |
| 2, Onslow Road, Hersham, Walton-On-Thames, Kt12 5Bb | Confirmed; unchanged coordinates | Existing association agrees |
| 24, Fairacres, Cobham, Kt11 2Jw | Confirmed; unchanged coordinates | Existing association agrees |
| Flat 9, Kingsdown, Castle Hill, Farnham, Gu9 0Ad | Added | Held: no HMLR freehold lookup |
| Woodlands Ryde, Chobham Park Lane, Chobham, Woking, Gu24 8Hg | Added | Held: missing or conflicting polygon |
| 9, Eaton Park Road, Cobham, Kt11 2Jj | Confirmed; unchanged coordinates | Existing association agrees |
| Charters, South Ridge, Weybridge, Kt13 0Nf | Confirmed; unchanged coordinates | Existing association agrees |
| Birchwood, Leigh Place, Cobham, Kt11 2Hl | Confirmed; unchanged coordinates | Existing association agrees |
| Ravens Point, Tor Lane, Weybridge, Kt13 0Ns | Added | Added: HMLR lookup and containment agree |
| 5, Eriswell Crescent, Hersham, Walton-On-Thames, Kt12 5Ds | Confirmed; unchanged coordinates | Existing association agrees |
| Wentworth Lodge, Portnall Rise, Virginia Water, Gu25 4Jz | Added | Added: HMLR lookup and containment agree |
| 15, Highlands Road, Reigate, Rh2 0La | Added | Held: missing or conflicting polygon |
| Foxwood, 15A, Longdown Road, Lower Bourne, Farnham, Gu10 3Ju | Added | Added: HMLR lookup and containment agree |
| 7, Pit Farm Road, Guildford, Gu1 2Jh | Added | Added: HMLR lookup and containment agree |
| Skerryvore, Uvedale Road, Oxted, Rh8 0En | Added | Added: HMLR lookup and containment agree |
| 12, Great Austins, Farnham, Gu9 8Jg | Added | Added: HMLR lookup and containment agree |
| Wiljoy, 32, Garratts Lane, Banstead, Sm7 2Eb | Added | Added: HMLR lookup and containment agree |
| 9, Albury Road, Hersham, Walton-On-Thames, Kt12 5Dy | Confirmed; unchanged coordinates | Existing association agrees |
| 17, Chargate Close, Hersham, Walton-On-Thames, Kt12 5Dw | Added | Added: HMLR lookup and containment agree |
| Old Westwick, Chantry View Road, Guildford, Gu1 3Xw | Confirmed; unchanged coordinates | Existing association agrees |
| Springacres, Spring Woods, Virginia Water, Gu25 4Pw | Added | Added: HMLR lookup and containment agree |
| Shardeloes, Ashtead Woods Road, Ashtead, Kt21 2Eq | Added | Added: HMLR lookup and containment agree |
| 20, West End Lane, Esher, Kt10 8La | Added | Added: HMLR lookup and containment agree |
| 5, Woodside Road, Cobham, Kt11 2Qr | Added | Added: HMLR lookup and containment agree |
| 11, Oakwood Road, Virginia Water, Gu25 4Rz | HMLR replaces prior link; moved 17.841 m | Existing association agrees |
| 19, Austen Road, Guildford, Gu1 3Nw | Added | Added: HMLR lookup and containment agree |
| Mulberry Lodge, Kent Hatch Road, Oxted, Rh8 0Sz | Added | Added: HMLR lookup and containment agree |
| 17, Littleworth Road, Esher, Kt10 9Pd | Added | Added: HMLR lookup and containment agree |
| 4, Ashcroft Park, Cobham, Kt11 2Dn | Added | Added: HMLR lookup and containment agree |
| 3, Clifford Manor Road, Guildford, Gu4 8Ag | Added | Added: HMLR lookup and containment agree |
| 5, Fox Wood, Walton-On-Thames, Kt12 4Bs | Confirmed; unchanged coordinates | Existing association agrees |
| Meadcroft, Grenville Road, Shackleford, Godalming, Gu8 6Ax | Added | Added: HMLR lookup and containment agree |
| Laurel Bank, Bourne Grove, Lower Bourne, Farnham, Gu10 3Qt | Added | Added: HMLR lookup and containment agree |
| Heatherling, Jumps Road, Churt, Farnham, Gu10 2Jy | Confirmed; unchanged coordinates | Existing association agrees |
| Holt House, 3, Oaksend Close, Oxshott, Leatherhead, Kt22 0Nx | Confirmed; unchanged coordinates | Existing association agrees |
| 9, St Johns Avenue, Leatherhead, Kt22 7Ht | Added | Added: HMLR lookup and containment agree |

## Source scope and reproducibility

The first lookup is July 2026 data, published 28 August 2026. It covers the new monthly publication, including corrections and late registrations, rather than a historical backfill. The downloaded national UPRN table has 94,112 transaction links to 92,750 distinct UPRNs. The INSPIRE table has 82,493 rows linking 77,051 transactions to 81,512 distinct parcels. In Surrey, across all price bands, 1,933 transactions link to 1,910 distinct UPRNs. The £2m+ residential monthly cohort contains 37 canonical properties; 36 have UPRNs. Single Oak, Golf Club Road has no UPRN in this release.

The coordinate archive is `osopenuprn_202608_csv.zip` (618,494,417 bytes), verified against the OS downloads API’s MD5 `2f023512afc378cd7b9351b24ccf1a34`. Source SHA-256 digests, acquisition timestamps, transaction UUIDs and official identifiers are recorded in the cumulative acquisition ledger `work/hmlr-identifier-state.json`. The 11 INSPIRE authority files are the September 6 release. Address points and index polygons are distinct types of evidence; neither establishes a surveyed entrance, building footprint or exact legal boundary.

## Ongoing behavior

`collect_hmlr_identifiers.py` discovers published lookup URLs instead of guessing the calendar filename. It checks that PPD and both identifier lookups belong to one publication, verifies every source digest and requires exact transaction UUID-to-sale joins. Pending records are retained until the canonical property ledger catches up. Earlier monthly links remain available; a later record without an identifier does not erase an older official relationship. Corrections update the same UUID. Multiple identifiers, missing OS coordinates, duplicate ownership, or withdrawal of an accepted link stop generation for investigation. Lower-priority sources cannot silently replace those relationships.

`uprn_priority.py` supplies the shared HMLR-first order used by OS enrichment and planning/context identifier resolution. App packaging validates the official feed alongside the exact staged canonical and parcel data, records replacements, and merges it into the private backup feed before the existing five-field map projection. Authoritative address points take precedence in map display; weaker backup points only refine approximate locations.

The candidate workflow checks after the 20th-working-day PPD publication and after a successful monthly property refresh. It has `contents: read` and uploads validated files for review. Publication is a separate reviewed step. The existing first-Sunday INSPIRE schedule is preserved.

## Consumer compatibility correction

Full app validation revealed that the earlier September lifecycle repair included two registry review fields beyond the native app’s exact public contract. The producer now projects only `decision`, `decisionBatch`, `reviewedAt` and `semantics`. Reviewer and report references remain in the registry. The Python validator and JSON Schema now enforce the same public shape. This fixes the client rejection without weakening the native validator.

## Validation and release status

All 277 data tests and 1,287 app source tests passed, with no failures or skips. Live official-source discovery returned the audited checksums. Repeating the complete collection, context and parcel build against freshly downloaded HMLR files preserved all five tracked artifact hashes. The app merge is also byte-identical on repetition and produces 3,290 points through the exact five-field public allowlist. Full app spatial validation accepts the 3,249 parcels, the 36-record HMLR feed and the merged private feed. Schema, base completeness, deterministic estate and existing public-package checks pass. Native packaging and installation remain separate release steps.

## Official sources

- [HMLR announcement and lack of historical backfill](https://www.gov.uk/government/news/hm-land-registry-to-provide-property-identifiers-for-price-paid-data-from-28-august)
- [HMLR UPRN dataset](https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-uprn-look-up-table-dataset)
- [HMLR UPRN technical specification](https://www.gov.uk/government/statistical-data-sets/technical-specification-transaction-unique-identifier-and-uprn-look-up-table-dataset)
- [HMLR INSPIRE lookup](https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-inspire-id-look-up-table-dataset)
- [OS Open UPRN](https://www.ordnancesurvey.co.uk/products/os-open-uprn)
