# Release 0.2.0.dev1 — Specialty-Aware Target Discovery

This development release completes the specialty-aware discovery layer while preserving the existing orthopedic workflow.

## Added

- Generic Zocdoc specialty landing-page support.
- `captured/specialty_landings/` evidence directory.
- `specialty_pages` resumable capture queue.
- `specialty_targets` state/location target table.
- `discover-targets` CLI command with controlled concurrency.
- Repeatable `--specialty` filters for target/listing collection.
- Generic listing URL recognition for specialty/location pages and pagination.
- Specialty provenance on seed and pagination pages.
- `provider_specialty_observations.csv` from the raw listing trace.
- `specialty_pages.csv`, `specialty_targets.csv`, and `specialty_target_summary.csv`.
- Retry support for `specialty-pages`.
- Regression tests for generic URL classification, specialty landing parsing, and queue seeding.

## Sample validation

The generalized specialty landing parser was validated against supplied rendered HTML for:

- Plastic Surgeons — 180 published location targets.
- Family Physicians — 136 targets.
- Travel Medicine Specialists — 126 targets.
- Acupuncturists — 75 targets.
- Rheumatologists — 34 targets.

The five samples produced 551 specialty/location crawl targets in the offline smoke workspace.

## Compatibility

`zocdoc-ortho` remains an alias for `zocdoc-collector`. The existing orthopedic seed/listing/trace/profile flow remains supported.

## Next

PostgreSQL persistence, sync-run lifecycle, hashes/change detection, safe removal semantics, and scheduled daily synchronization are intentionally deferred to the next milestone.
