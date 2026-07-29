# INSIGHT news sources and cadence

INSIGHT publishes link-only article metadata: title, publisher, publication
date, canonical URL, deterministic relevance fields and operational
diagnostics. Article summaries, bodies and images are not published.

The source registry is fail-closed. A source can enter the live feed only when
both automated collection and link-only publication are explicitly approved.

## Live sources

| Source | Coverage | Access basis |
|---|---|---|
| Epsom & Ewell Times planning | Surrey planning and development | Publisher-operated category RSS; link metadata only |
| Epsom & Ewell Times housing | Surrey housing | Publisher-operated category RSS; link metadata only |
| Guildford Dragon | Guildford housing, planning and infrastructure | Publisher-operated RSS; link metadata only |
| Woking News & Mail planning | Woking and Guildford planning decisions | Publisher-operated category RSS; link metadata only |
| Farnham Herald | Farnham and western Surrey property stories | Publisher-operated RSS; property classifier; link metadata only |
| Haslemere Herald | Haslemere and Surrey Hills property stories | Publisher-operated RSS; property classifier; link metadata only |
| HM Land Registry | UK House Price Index, Price Paid and transaction releases | Open Government Licence v3.0 |
| Ministry of Housing, Communities and Local Government | Material national housing and planning policy | Open Government Licence v3.0 |
| Office for National Statistics | House prices, rents, affordability and housebuilding | Open Government Licence |

Official national feeds use strict title allowlists before their authority
override is applied. First-party planning and housing category feeds are
treated as curated property lanes; sitewide local feeds still use strict
property title gates before the normal INSIGHT geography, entity, materiality
and freshness scoring.

## Registered but disabled

PrimeResi, Estate Agent Today, Property Industry Eye, BBC Surrey, BBC Business,
SurreyLive, Country Life and Surrey County Council remain registered but are
not fetched or published. Their current rights or stable-feed status requires
permission or further verification. Changing one of these sources to `live`
will fail registry validation unless its collection and publication approvals
are also recorded.

## Cadence and publication

The dedicated news workflow is scheduled at minutes 17 and 47 of every hour,
has an independent concurrency group, supports manual runs and accepts the
`news-refresh` repository dispatch event. Each run:

1. validates the rights registry and adapter contracts;
2. fetches live sources concurrently with bounded retry;
3. publishes per-source diagnostics and safe last-known-good fallback;
4. validates pipeline freshness separately from editorial freshness;
5. leaves the independently generated Today feed untouched; and
6. commits the Market News feed only.

GitHub-hosted schedules are best effort. The repository dispatch hook exists
for an independent scheduler or watchdog where a contractual cadence is
required.
