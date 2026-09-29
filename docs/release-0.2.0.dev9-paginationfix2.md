# Dev9.2 Pagination Trace Fix

## Fixes

- Durable listing trace now supplements rich card parsing with every rendered provider-name anchor, even when Zocdoc places provider links outside the expected result-card article wrapper.
- Inline ingestion verifies that every provider link accepted by listing validation is represented in durable `listing_provider_trace` before raw HTML is deleted.
- A trace coverage mismatch is marked `review` / `trace_partial` and raw HTML is retained for diagnosis.
- Profile queue synchronization now follows successfully persisted trace relationships rather than validator-only discoveries.
- Specialty runtime profile projections ignore untraced doctor rows, preventing inflated `New-to-run providers` counts.

## Recovery from Dev9.1 Dentist test

Pages previously saved with `trace_occurrence_count=0` should be reset to `pending` and recaptured because Dev9.1 may already have deleted their raw HTML.
