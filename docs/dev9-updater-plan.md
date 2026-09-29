# Dev9 -> Daily Updater Plan

This release prepares the collection layer; it does not yet schedule or perform daily change reconciliation.

## Already available
- `pages.advertised_total`: parsed from the visible `X verified ... in ...` listing header.
- `pages.provider_set_hash`: deterministic hash of provider URLs found on that listing page.
- `listing_provider_trace`: durable listing-page -> provider provenance.
- `doctors` / `doctor_profiles`: globally deduplicated provider discovery and profile queue.
- `first_seen` / `last_seen` style fields already exist in several queue/catalog tables.

## Dev9 storage behavior
Successful listing captures are traced immediately and the raw listing HTML is deleted after the SQLite commit. Failed/review captures stay on disk.

## Next updater milestone
1. Persist run IDs / sync-run completeness.
2. Use the base listing's advertised provider count as a cheap change signal.
3. Add page-count / first-page provider fingerprint if useful.
4. Trigger full pagination when the signal changes.
5. Force periodic full reconciliation even when the signal is unchanged.
6. Profile-fetch only newly discovered or stale/changed providers.
7. Add safe missing/inactive logic that ignores blocked/partial runs.
8. Move canonical current/history state to PostgreSQL.
9. Add one `daily-sync` command and scheduler documentation.

The advertised count is a sentinel, not proof that membership is unchanged: one provider can leave while another joins and keep the count constant.
