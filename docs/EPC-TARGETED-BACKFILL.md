# Targeted EPC recovery for Build 117

The corrected retained-cache review verifies 2,931 of the installed cohort's
4,738 sales. The remaining 1,807 sales represent 1,486 properties. This candidate
job retrieves additional official-register evidence using the repository's
existing `EPC_BEARER_TOKEN`, with the exact-property identity guard retained.

The monthly workflow has an explicit `epc_backfill_only` dispatch mode. This mode
skips the normal monthly publication chain and runs with read-only repository
permissions. It does not run the Land Registry sweeper, change public feeds,
commit a cache or update an application. The default monthly path is preserved.

## Frozen inputs and results

- Source app commit: `fc9565fd6abdde6ce3b74b888f3173fac2b66ea4`.
- Input asset SHA-256: `30774d097d8578cc8025208d8bcf909c2bc4926134681d825f3211067ca6e082`.
- Retained cache SHA-256: `099b1a55936663987dc1a3d8eca61f8e01774f548804547e3f63e2f33860eb82`.
- Runner input: this repository's tracked feed, SHA-256
  `e8d692733241e411ca9d403125cc0a70efd000ac42cff7e5bb64e92f97c77990`,
  minus the one explicitly excluded transaction. Its 17 identity/sales fields
  match the app's exact ordered cohort, fingerprint
  `1dcc423f2b0fef79de4c7db9dc6b3581b5142ca1f973918e3fb9193ae9fdc06d`.
  The runner does not need access to the private app repository.
- All 4,738 IDs, ordering and non-EPC facts must remain unchanged. The separately
  excluded £112.5m transaction cannot enter this candidate.
- Existing verified evidence is retained. Fresh searches target unresolved
  properties, reuse postcode results and verify full certificate identity.
- Complete pagination is required. Conflicting identities, incomplete responses,
  failed authentication and exhausted request/time budgets remain unresolved.
- The latest exact certificate is used only when its measurement evidence is
  usable and consistent. An unusable newer record cannot silently select an older
  certificate. A completed search without a usable exact match is distinct from
  an unperformed or failed search; neither promises that no certificate exists.

The job creates three files: `result.enc`, `result.key` and `receipt.json`.
The first two contain an encrypted candidate and wrapped encryption key. The
receipt contains aggregate counts, input/source hashes and authenticated artifact
hashes. No raw certificate addresses, identifiers, UPRNs or provider credentials
are uploaded in plaintext. Artifact retention is three days.

Generate an RSA public/private key pair of at least 3,072 bits in a private local
directory. Keep the private key on the release owner's Mac. Supply only its PEM
public key as `epc_result_public_key`, and explicitly set `epc_backfill_only` to
true when dispatching `monthly-property-refresh.yml` on the approved repair
branch. Dispatching without that flag invokes the existing monthly workflow.
Commit/push approval must precede dispatch of the reviewed source revision.

After the run, bind the artifact to its exact GitHub run and producer commit,
then call `epc_candidate_result.open_result(artifact_directory, private_key_path)`
locally. Public-key encryption protects the data and detects tampering; GitHub
run/source verification establishes which producer created the artifact.

## Subsequent application rebuild

The decrypted result contains EPC-only patches, EPC metadata, a private resumable
cache, the initial identity decisions, minimized fetched certificate/search
evidence and the aggregate report. Use
`backfill_epc_candidate.apply_candidate_to_frozen_app(candidate, input_path)` on
the Mac to produce private candidate rows. It verifies the original app asset,
the full ordered ID cohort and every patch against the exact certificate cache.
It changes only the seven EPC-derived fields and preserves the app's non-EPC hash.
This avoids replacing private app planning, school, UPRN or estate context with
the data repository's different projections. Review recovered coverage and
remaining identity problems before staging application assets.

Retained records preserve their original provider-check dates. Fresh records
carry their own `searchedAt`. The candidate has a separate
`targetedSearchCompletedAt`; its blanket `updatedAt` stays at the retained source
timestamp so the mixed dataset cannot imply every property was just checked.
The application rebuild must use those per-record dates when projecting evidence.

Regenerate transactions and their summaries, property records, valuation seeds,
runtime projections and Today as one generation. Validate the result and package
before installation and the fresh 15-minute installed workflow audit. This
candidate job alone is not release sign-off or a claim of complete EPC coverage.

API contracts checked against the official [domestic search documentation](https://get-energy-performance-data.communities.gov.uk/api-technical-documentation/search-certificates/domestic)
and [full-certificate documentation](https://get-energy-performance-data.communities.gov.uk/api-technical-documentation/fetch-certificate-data).
Some documented full-certificate schemas omit a repeated certificate number;
those responses remain bound to the exact request, complete address identity,
registration date and agreeing UPRN when both responses provide it.
