# INSIGHT public data notices

This repository publishes an evidence-led Surrey property-price and context
feed. It is not a legal title register and does not provide legal, surveying,
planning, environmental, mortgage or valuation advice.

## HM Land Registry

Price Paid Data and UK House Price Index material are reused under the Open
Government Licence v3.0. Price Paid Data records completed transactions rather
than live asking prices, ownership, deeds, charges or title boundaries.

- Source: https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads
- Licence: https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/

HM Land Registry address fields are published only for the permitted purpose of
displaying residential property-price information.

### INSPIRE index polygons

The separate `outputs/inspire-parcels.js` feed uses HM Land Registry INSPIRE
Index Polygons under the Open Government Licence v3.0. It publishes only the
small approved INSIGHT association cohort, not the raw Surrey downloads.

- Source: https://use-land-property-data.service.gov.uk/datasets/inspire/download
- Dataset conditions: https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions
- Licence: https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/
- Coordinate conversion grid: https://cdn.proj.org/uk_os_OSTN15_NTv2_OSGBtoETRS.tif
- Pinned grid SHA-256: `5d6ed64d2119952c4c559fa1fccbc594b6520fc3ec3ef2fc10be13202c4384fa`

Required attribution is carried in the generated feed metadata:

> This information is subject to Crown copyright and database rights
> [year of supply or date of publication] and is reproduced with the permission
> of HM Land Registry.

> The polygons (including the associated geometry, namely x, y co-ordinates) are
> subject to Crown copyright and database rights
> [year of supply or date of publication] Ordnance Survey AC0000851063.

The generated feed replaces that template placeholder with the actual year
derived from its HMLR source snapshot; its validator checks both attribution
statements against that snapshot on every publication.

These are indicative HMLR INSPIRE index polygons. An association is not proof
of title, ownership, an exact UPRN, a title-plan extent or a legal boundary.
Derived polygon areas are not measured site surveys. The source snapshot,
authority files, hashes, CRS, OSTN15 transform and caveat travel with each
publication.

HMLR INSPIRE is a freehold index-polygon dataset; it does not publish leasehold
extents. The 17 current flat/maisonette associations are labelled only as
indicative superior/freehold parcel context and must not be read as the extent
of an individual lease.

Part of the conservative automatic association cohort uses:

- Urban Big Data Centre (2023), *Price paid data to UPRN lookup*, University of Glasgow
- DOI/citation: https://doi.org/10.20394/agu7hprj
- Coverage: January 1995 to January 2022
- Catalogue status: UBDC Open Dataset
- Licence: Open Government Licence v3.0

That UBDC linkage evidence is reused under its own OGL v3.0 basis, separately
from the HMLR INSPIRE OGL. Neither linkage source converts an indicative parcel
association into title, ownership, exact-UPRN or legal-boundary confirmation.

The future `outputs/property-uprn-links.js` contract is kept separate. It is
currently empty; a future licensed address source must be documented here
before records are published. Each source must state its snapshot, coordinate
basis, entitlement and redistribution classification. Ambiguous onboarding
outcomes are published separately in `outputs/inspire-parcel-review-queue.js`;
they are not silently promoted into the parcel feed. UPRN evidence never
controls the canonical property identity.

## Context sources

The public transaction feed can include field-minimised EPC, Environment
Agency, GIAS school, Planning Data, Ordnance Survey and Historic England NHLE
context. Source-specific status, freshness and evidence boundaries travel with
the feed.

EPC source addresses, certificate identifiers and raw match diagnostics are
not public fields. EPC material remains subject to the EPC register terms,
data-protection responsibilities and Royal Mail intellectual-property notice.

Historic England NHLE material is reused under the Open Government Licence
v3.0 with the attribution carried in publication metadata. A `no_direct_match`
state is not proof that a property is unlisted or outside listed-building
curtilage.

- NHLE source: https://historicengland.org.uk/listing/the-list/data-downloads
- NHLE terms: https://historicengland.org.uk/terms/website-terms-conditions/open-data-hub/

## Publication boundaries

The public planning-history asset is an explicit zero-record unavailable
placeholder unless a separately licensed commercial source is configured. The
private council-portal cache and its derived property narratives are not
published in this repository or the public macOS installer.

News records are link-only metadata. Third-party article text is not
republished. OpenStreetMap amenity and Companies House payload publication
remains disabled.
