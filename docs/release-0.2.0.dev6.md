# 0.2.0.dev6 - zero-link listing validation

This release builds on dev5 multi-runner support and hardens listing completion semantics.

- `saved + doctor_links_found=0` is validated instead of blindly accepted.
- Explicit zero-result pages become `valid_empty`.
- Suspicious zero-link captures become `retry_pending` and are quarantined.
- Retry candidates are activated automatically at the start of the next listing run.
- The default retry cap is two; exhausted pages become `review`.
- Added `retry-zero-listings` to audit/requeue legacy zero-link saved pages.
- Added validation/retry fields to `page_queue.csv`.
- Works in both single-browser and N-runner `multi-listings` flows.
