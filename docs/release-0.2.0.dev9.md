# Release 0.2.0.dev9

## Purpose
Reduce listing-storage growth and remove the separate trace backlog without changing browser-side collection behavior.

## Changes
- Successful listing captures are traced immediately during normal listing ingestion.
- `listing_provider_trace` remains the durable provider-to-listing provenance layer.
- Successful raw listing HTML is deleted after the trace commit by default.
- Failed trace, retry, and review HTML is retained for debugging/recovery.
- `--keep-listing-html` restores dev8-style retention for `collect-listings` and `multi-listings`.
- Existing `trace` and `cleanup-listing-html` commands remain available for legacy/backlog captures.
- The existing `advertised_total` (`X verified ...`) and `provider_set_hash` fields are documented as updater inputs; automatic count/hash-driven updating is not implemented in dev9.

## Browser behavior
No extra Zocdoc interaction is introduced. Dev9 does not add clicks, scrolling, additional requests, or stealth/evasion behavior. The optimization is local after the existing rendered-page capture.

## Still retained
Provider profile HTML is still retained because the current derive/export workflow uses it. Moving profiles to immediate structured DB upserts belongs to the later updater/database milestone.

## Runtime benchmark add-on
- Added `specialty-runtime --specialty <slug>`.
- Specialty-filtered listing/profile runs now record wall-clock step timing in `output/specialty_runtime.csv`.
- Partial batches can project full specialty runtime from observed throughput.
- Added `--specialty` support to `multi-listings` and `multi-profiles`.
- Specialty-filtered profile benchmarking collects only providers first discovered by that specialty, avoiding contamination from older pending profile queues.
