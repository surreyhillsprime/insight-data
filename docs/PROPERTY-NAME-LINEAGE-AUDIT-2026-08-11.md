# Property-name lineage audit — 11 August 2026

## Outcome

The full 3,766-property planning-history corpus was screened for current and
historic house names that might represent one property under two canonical
IDs. The screen covered raw planning-address aliases, normalized
planning-UPRN groups, shared application fingerprints and exact planning map
points. Candidate pairs were then reviewed against HMLR chronology, EPC
evidence, current/historical local-gazetteer status, Open UPRNs and indicative
INSPIRE parcels.

The raw address screen produced 23 directional candidates representing 20
unordered pairs. The structural UPRN union added one further pair:

- four lineages are approved and merged;
- one remains held pending stronger title or independent address-history
  evidence; and
- 16 are rejected as neighbours, subdivisions or multi-property planning
  records.

No two accepted current properties share an Open UPRN or an accepted
indicative INSPIRE parcel.

## Approved current/former lineages

| Current canonical name | Reviewed former HMLR name | Evidence-led decision |
|---|---|---|
| Arasan Manor | Chilton House | Same Elmbridge base UPRN `100061347035`; Arasan is the approved LPI and Chilton the historical LPI. HMLR chronology changes from Chilton to Arasan. |
| Brooke House | Balmoral | Same Elmbridge base UPRN `10013119444`; Brooke is approved and Balmoral historical. The planning point lies inside Brooke parcel `34271765`. |
| Chapters | Fortune Manor | Same Elmbridge base UPRN `100061346504`, eleven shared planning references and a planning point inside Chapters parcel `55184440`. |
| Dunmore | Thirlstone | Same approved/historical planning BLPU `100061348214`; HMLR, planning, EPC and parcel evidence establish one site lineage. Planning records also establish demolition and replacement, so this is not described as the same physical building. |

All nine HMLR transactions remain published. The four historic property IDs
become retired aliases under the four current IDs, reducing the canonical
property count from 3,766 to 3,762 without removing a transaction.

Planning-only names do not acquire HMLR sales-identity authority. In
particular, `Wits End` and `Two Ways` are not members of the Chapters/Fortune
Manor alias group.

## Dunmore and Thirlstone

Dunmore and Thirlstone are the same operational property/site and indicative
parcel lineage, but the evidence does not support calling them the same
building.

- HMLR records Thirlstone at £3.625m on 12 July 2021 and Dunmore at £11.75m on
  31 May 2024.
- The EPC sequence changes from Thirlstone, 721 sqm and rating D in 2020, to
  Dunmore, 1,003 sqm and rating B in 2026.
- All 12 matched planning applications retain Thirlstone in the raw address
  history. Application 2021/2897 records a new detached house after demolition
  of the existing house.
- The repeated planning point lies inside Thirlstone parcel `33483728`, an
  indicative 5,705.56 sqm / 1.4099 acre polygon.
- Elmbridge marks Dunmore as the approved current LPI and Thirlstone as the
  historical LPI for planning BLPU `100061348214`.

The canonical property is therefore Dunmore, with Thirlstone retained as its
reviewed former name and retired transaction identity. The parcel is
indicative and is not a legal-title or ownership statement.

## Held pair

Woodside House / Hill Cottage, Cockcrow Hill, KT6 5HE remains unmerged. The
approved/historical LPI relationship and sales chronology favour Woodside as
the current name, but a planning description referring to land rear of Hill
Cottage and a 103 metre map-point separation leave a material subdivision
risk. A title or independent authoritative address-history source is required
before promotion.

## Rejected pairs

The following are not safe property-identity merges:

- Aurora / Kingswood House;
- Breton Hill / Hamilton House;
- Bridgeway House / Oaklands;
- Catkins / Pentlands;
- Chedworth / Yaffle Hill;
- Fairway House / Dana;
- Fairways West / Fairways;
- Leigh Hill Hall 15A / Leigh Hill House 15;
- Loxwood / Golden Oaks;
- Mountfield House / Woodcroft House;
- Oak Rise 7A / Hill Rise 7;
- Plot 1, 12 / Le Chene, 12;
- Regency House / Golden Oaks;
- Rosewarne / Tilehurst;
- Side Ley Lodge / Side Ley; and
- Bluebells / Heathridge House.

Their shared planning signals arise from adjacent sites, parent-site
redevelopment, additional dwellings, shared access or multi-property
applications. They remain separate canonical properties.

## Former-name publication contract

`reviewedAliasRegistryVersion` remains the installed-app compatibility epoch,
so existing supported builds continue to accept the corrected live feed. Exact
registry content is independently versioned and SHA-256 fingerprinted by
`reviewedAliasContentVersion` and `reviewedAliasContentFingerprint`; changing a
canonical identity group therefore cannot masquerade as unchanged content.

`sourceAddressVariants` remains the complete three-field retired identity
ledger used for sales and history migration. A separate reviewed classification
maps a current canonical ID to only those retired IDs that represent a former
house name.

The public metadata publishes a version, exact property/member counts, a
content fingerprint and `reviewedFormerNameMembers`. A displayed name is
derived from the matching source variant rather than copied as free text.
Validation fails if a reviewed former-name ID is not a retired member under
the same current property.

The property card displays `Former name` or `Former names` only when this
reviewed intersection is non-empty. Absence means no reviewed former name was
supplied; the interface renders no label, row or placeholder and does not
claim that the property has never had another name.

## Evidence and rights boundary

The audit used the private/local council-planning cache for candidate discovery
and review. Raw council proposal text and the private planning corpus are not
republished. The public feed contains only the minimal reviewed identity
decision supported by the later HMLR identity and current/historical address
status. Planning-only aliases remain private and scope-limited unless a
separately publishable source and explicit review permit promotion.
