# September 2026 INSPIRE recovery

HMLR's September release removes parcel `64286934`, previously associated with
`property:54 POTTERS LANE SEND WOKING GU23 7AL|GU237AL`. The collector correctly
blocked publication until an explicit reviewed transition was supplied. The
repair records indicative successor `64871044` and rebuilds the September feed
using the existing validation and publication contracts.

## Incident and publication dates

- Investigated production revision: `d9a0cecce755a477ef255dc01d97c20cfbf7148d`.
- First failed run: [6 September](https://github.com/surreyhillsprime/insight-data/actions/runs/34063320462).
- Repeated failures: [7 September](https://github.com/surreyhillsprime/insight-data/actions/runs/34168338113)
  and [8 September](https://github.com/surreyhillsprime/insight-data/actions/runs/34287984380).
- Failing step: `Download, audit and build Surrey parcel feed`.
- Error: `ValueError: 1 registered parcels are absent; first missing: 64286934`.
- The 8 September diagnostic identifies only this association and records all
  eleven authority source timestamps on `2026-09-06`.
- The live [HMLR download page](https://use-land-property-data.service.gov.uk/datasets/inspire/download),
  retrieved 9 September, says this dataset was published on 6 September 2026 and
  retains the first-Sunday monthly publication schedule. No scheduling change
  is needed to resolve this incident.
- Last good parcel release: `inspire-parcels-2026-08-02-a5b1752e184a`.

## Review evidence

Reviewed at `2026-09-09T05:45:56Z` by Codex. This review establishes an indicative
spatial successor; HMLR's polygon files do not establish legal title succession.

The full unmodified collector was run against fresh downloads of all eleven
Surrey authorities and reproduced the missing-parcel failure. All source-count,
structural and cross-authority checks preceding that failure passed.

The September Guildford archive has SHA-256
`7378d38afc4b3e5073b36f1f93a7be158bc38a19726aa273c191638ec8d9e84c`.
The replacement's `VALIDFROM` and `BEGINLIFESPANVERSION` are both
`2026-08-27T08:56:41.594Z`. Its canonical source-ring digest is
`f804e485e800ba71cabcdbd87729006a9c282179ad543bead1d6806b298ebb6d`.
It is not already associated with another INSIGHT property.

The old published EPSG:4326 polygon was converted back into EPSG:27700 using
the collector's pinned OSTN15 transformation. September polygons were compared
in their original EPSG:27700 coordinates using Shapely 2.1.2. The old geometry
has eight-decimal display rounding, so the comparisons below are approximate.

| Comparison | Result |
| --- | ---: |
| Old published source area | 2,509.8 square metres |
| Old area reconstructed from display geometry | 2,509.8030 square metres |
| Replacement source area | 2,497.7032 square metres |
| Old footprint covered by replacement | 99.5173% |
| Replacement footprint inside old footprint | 99.9994% |
| Measured Hausdorff distance | 0.4398 metres |
| Old area more than 0.5 m inside its boundary left uncovered | 0 square metres |
| Previously recorded linked-point boundary margin | 13.0185 metres |
| Rechecked existing linked-point boundary margin in current raw source | 13.0173 metres |

Every Guildford polygon intersecting or lying within 0.5 m of the old footprint
was examined. The next-largest overlap belongs to adjoining parcel `62063111`
and covers only 12.1044 square metres (0.4823%) of the old footprint. That
parcel's geometry version changed on 27 August at `08:55:57.702Z`, shortly before
the replacement was created. Other adjoining overlaps are less than 0.09
square metres each. These observations support a small boundary adjustment
with a new indicative parcel ID, rather than reassignment to a different site.

The existing transaction-linked property point was retrieved from the app's
private coordinate feed `property-uprn-links-2026-08-11-de1e00799c6f` (SHA-256
`9ca141b210e4d3a8303dc3e7d88eca2ce79210c3413abaf202c842412b77dd65`). Its provenance
is UBDC's PPD transaction linkage and the August OS Open UPRN snapshot. The
coordinate evidence is historical, not a newly confirmed address lookup.

The collector's spatial audit was rerun against all eleven current authority
files using this point. Exactly one polygon contains it: `64871044`, with a
13.0173 m boundary margin measured in the raw source. The association records
that updated measurement, `reviewed_indicative` status and evidence provenance.
No UPRN or private coordinate-feed content is added to the public repair.

## Other changes in the September source

Two retained parcel IDs also have changed source geometry. Their existing
property points were rechecked across all eleven authorities and remain
uniquely inside the same parcels, with clear boundary margins. The unchanged
property-to-parcel links remain valid as indicative associations.

| Property | Parcel ID | August area | September area | Current point margin |
| --- | --- | ---: | ---: | ---: |
| 18 Church Street, Cobham | `33823363` | 1,420.71 sqm | 1,419.15 sqm | 9.9908 m |
| Deer Park House, Hill Close, Wonersh | `32838326` | 10,387.17 sqm | 4,478.42 sqm | 20.8719 m |

Deer Park House's polygon shrinks substantially (about 56.9%); the revised
extent comes from HMLR's geometry version dated `2026-08-19T12:39:50Z`. Its
existing property point remains inside. The feed must display the current
indicative area without implying a proven legal disposal, subdivision or
change of ownership. Church Street's version is dated
`2026-08-04T11:24:09.390Z` and changes the area by about 1.56 sqm.

## Repair boundaries

The reviewed ledger appends a `replace` transition tied to the exact prior
release and snapshot. The prior transition history and all other property to
parcel links are retained. The original approval baseline remains provenance.
The property identity and transaction feed are unchanged. Title, exact-UPRN and
legal-boundary confirmation flags remain false.

Raw HMLR ZIP/GML downloads are held outside Git. No collector, validator,
schedule, dependency or public schema change is required for this correction.
Any later missing or changed association must still pass the existing explicit
transition review.

Three existing tests assumed August source counts, cohort totals or publication
dates. The repaired tests use configured source floors and per-authority count
reconciliation, check the current automatic/reviewed counts, and use the tested
feed's publication timestamp. They continue checking the historical approval
floor and exact parent-correction evidence, and retain the failure cases for
unknown associations, invalid geometry contracts and schema defects.

## Validation

The final candidate is `inspire-parcels-2026-09-06-bb925552632b`, generated at
`2026-09-09T05:48:38Z`. It contains 3,228 associations across 3,785 canonical
properties (85.2840%): 2,869 automatic and 359 reviewed indicative associations.
All 3,227 other associations are unchanged except for the new source snapshot.
All four prior ledger entries are preserved exactly.

The audit covered all eleven September authority files: 525,751 source feature
occurrences, 524,129 distinct INSPIRE IDs and 1,622 duplicates. The existing
unselected Waverley source quarantine remains unchanged.

Passed:

- Full repository suite: 264 tests.
- Parcel, UPRN-link and review-queue validators.
- All five INSPIRE/UPRN JSON Schema checks.
- Canonical public-feed completeness (`--base-only`).
- Deterministic private-estate rebuild with no output difference.
- Existing tracked public release ZIP validation. This checks the repository's
  existing package only; no native application was rebuilt or installed.
- A second full source rebuild produced identical bytes and publication times
  for both candidate feeds.
- `git diff --check`.

Final runtime SHA-256 values:

- `outputs/inspire-parcels.js`:
  `6ec87dd85c271b77090c016d26468bcbfdd501d0f56556a1ab9e503e1f99d4e5`.
- `outputs/inspire-parcel-review-queue.js`:
  `1736b337e73cdc33a98fc46da856e1ad75bb0bf951faf541e78e4ce5c5707e3e`.

The original app and data checkouts are untouched. This candidate is ready for
commit/push review on `codex/inspire-september-recovery`; it is not yet published
to production. The intended commit contains this report, the two configuration
files, the two generated feeds and `tests/test_inspire_parcels.py`.
