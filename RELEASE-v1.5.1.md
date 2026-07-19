# INSIGHT v1.5.1 release audit

Audit completed: 19 July 2026

Bundle version: **1.5.1**

Build: **17**

Release identity: **v1.5.1**

## Decision

**PASS for a controlled internal investor demonstration.** This is the complete
v1.5.1 internal product baseline. It replaces v1.5.0 build 16; `v1.04` would
have been an incorrect version rollback.

**NOT approved for broad external or commercial distribution.** The archive is
strictly ad-hoc signed, not Developer ID signed or Apple-notarized. The legal,
privacy and repository-history items in this report remain open. This audit is
engineering evidence, not legal advice.

## Release artefact

| Item | Result |
| --- | --- |
| Package | `downloads/INSIGHT-macOS.zip` |
| SHA-256 | `e1d640bfac647c8741c552619c4b17c1b017a5323ccd3d012465227bd63a5d0a` |
| Size | 7,135,444 bytes |
| Bundle | `com.surreyhillsprime.insight` |
| Architectures | arm64 and x86_64 |
| Minimum macOS | 12.0 on both slices |
| Code signature | Strict verification passed; ad-hoc; no Team ID |
| Dataset fingerprint | `f85c31d13a3a6edeb22948262e1d6082df07b6b4ef6ce60f1ae487fed6183823` |
| Data source commit | `595b89041d892d7f51c2f552c75cbb76b8d8ab26` |

The exact ZIP was extracted and independently validated. Its 26 bundled web
assets match the audited source, and the installed `/Applications/INSIGHT.app`
matches v1.5.1 build 17 with a valid strict signature.

## Verification summary

| Gate | Evidence | Result |
| --- | --- | --- |
| Data unit and contract tests | 30/30 | PASS |
| App regression tests | 115/115 | PASS |
| Strict completeness metadata | All seven thresholds | PASS |
| Python, shell and JavaScript syntax | 30 Python, 3 shell, 5 release JS assets | PASS |
| Release ZIP | Version/build, extraction, universal binary, macOS 12, signature | PASS |
| Offline/static assets | 26 assets, local MapLibre worker/CSS/glyphs, source parity | PASS |
| Publication boundary | Fail-closed schema-v3 allowlist and restricted-field scan | PASS |
| Native product smoke | Map, panels, news, Ask INSIGHT, Escape, estate/property views | PASS |

The in-app Browser automation backend was unavailable during the audit. This
was superseded by a smoke test of the installed native macOS bundle itself.

## Native product smoke

The installed artefact was fully relaunched before the final test. The native
UI showed:

- the local map with bundled attribution and property markers;
- exact `Market News` wording and all six current link-only stories;
- populated Local Authorities, Private Estates and Towns panels;
- nine Fairmile Avenue matches through Ask INSIGHT, including correct
  `1 sale`/`falls` singular wording;
- a working Escape reset;
- Eaton Park filtering with its audited road list and 44 loaded sales; and
- the detailed record for 12 Fairmile Avenue, including transaction history,
  evidence-limited narrative, planning context, schools, rail and airports.

## Current data baseline

| Check | Found | Total | Coverage | Gate | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Postcodes | 1,543 | 1,544 | 99.9% | 99.0% | PASS |
| Coordinates | 1,542 | 1,544 | 99.9% | 99.0% | PASS |
| EPC matches | 1,506 | 1,544 | 97.5% | 75.0% | PASS |
| Fresh flood observations | 1,542 | 1,544 | 99.9% | 90.0% | PASS |
| UPRN matches | 58 | 1,544 | 3.8% | 3.0% | PASS |
| School lookups | 1,541 | 1,544 | 99.8% | 80.0% | PASS |
| Planning query responses | 1,542 | 1,544 | 99.9% | 95.0% | PASS |

The release contains 1,544 qualifying transactions from 1 January 1995 through
6 May 2026, 1,315 canonical properties, 3,553 evidence-linked events and 1,315
property stories. It also bundles 1,112 field-minimised schools and six current
news links generated 19 July 2026.

Planning coverage is intentionally fail-closed: 1,542 successful source
responses are labelled `unknown` because the source does not establish a
complete negative result; two failed responses are `unavailable`. The product
does not turn missing evidence into “nothing found”. Flood observations are
time-gated and polygon holes are preserved.

## Code and publication hardening completed

- Public feed publication is schema v3 and rejects unreviewed top-level fields.
- EPC certificate IDs, EPC addresses, match scores/search diagnostics,
  Companies House, OpenStreetMap and licensed planning-history payloads are not
  published in the base feed.
- EPC and operational context caches are ignored and kept off-repository;
  workflow use is transient/encrypted where applicable.
- Planning lookups consume all declared pages and enforce an exact 1.2 km
  circular distance after their square prefilter.
- Flood evaluation uses a four-call bulk snapshot, keeps interior polygon holes
  and refuses stale observations.
- The app bundles MapLibre, its CSP worker, CSS and Noto glyphs locally under a
  strict Content Security Policy, with visible notices/attribution.
- The school map asset is reduced to the 17 product fields it actually uses.
- Non-map panels render before the map-load callback, so a delayed map worker
  cannot blank the product UI.
- Packaging validates the staged app and an extracted pending ZIP before the
  release ZIP becomes authoritative.
- The stale v1.4.3 public installer and obsolete July staging tree were replaced
  or quarantined; neither is present in this release package.

## Rights and external-release boundary

HM Land Registry permits commercial and non-commercial reuse of Price Paid
Data under OGL v3, but its page separately restricts the embedded address data
to personal/non-commercial use or display for residential property-price
information unless broader rights are obtained. The required HMLR attribution
is retained. See the [official Price Paid Data conditions](https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads).

The current tree strips direct EPC identifiers and keeps its lookup cache
private, but address-level EPC derivation still needs a recorded UK GDPR/DPA
2018 lawful-basis and proportionality review. See the official
[EPC licensing restrictions](https://get-energy-performance-data.communities.gov.uk/guidance/licensing-restrictions)
and [data-protection requirements](https://get-energy-performance-data.communities.gov.uk/guidance/data-protection-requirements).

Planning Data is treated as source-specific and fail-closed; future licensed
planning-history redistribution stays dormant without written permission. See
[Planning Data terms](https://www.planning.data.gov.uk/terms-and-conditions).

Earlier Git history still contains superseded private caches/raw fields. The
current branch deletes and ignores them and tests against recurrence, but this
release does not rewrite historical Git objects. That decision needs legal and
operational sign-off before broad external distribution.

Apple states that direct distribution should use a Developer ID signature and
notarization. No valid signing identity is installed on this Mac, so v1.5.1 is
correctly marked as an internal build. See [Apple Developer ID](https://developer.apple.com/support/developer-id/)
and [Apple notarization guidance](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).

## Provenance qualification

The data pipeline is Git-versioned and this release is marked by tag `v1.5.1`.
The app source is an audited local snapshot rather than a dedicated Git
repository. The exact distributed ZIP, native binary, core HTML/JavaScript and
data files are therefore pinned by SHA-256 in `RELEASE-v1.5.1.json`. Moving the
app source into its own repository is recommended before the next external
release.

## Release statement

The product may be described as **INSIGHT v1.5.1 build 17, complete for a
controlled internal investor demonstration as audited on 19 July 2026**.

It must not yet be described as legally watertight, Apple-notarized, or ready
for unrestricted customer distribution.
