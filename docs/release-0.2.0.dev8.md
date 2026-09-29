# 0.2.0.dev8

This release focuses on two scale bottlenecks: raw listing HTML growth and safe parallel profile collection.

## Incremental listing trace

- `listing_provider_trace` is now persistent and updated per listing page instead of being dropped and rebuilt.
- Each successfully traced page is marked in `pages` with `trace_status`, `trace_parsed_at`, `trace_occurrence_count`, `trace_source_file`, and `trace_html_deleted_at`.
- `trace` parses only new/unparsed saved listing pages by default.
- Trace CSVs are rebuilt from SQLite history, not from whatever HTML happens to remain on disk.
- `trace --delete-html` deletes a listing capture only after its trace commit succeeds.
- `cleanup-listing-html` safely removes already-parsed listing HTML; `--dry-run` previews disk reclamation.
- Re-capturing a listing invalidates its trace marker so new content cannot be skipped.

## Atomic profile workers

- Profile claims now use `BEGIN IMMEDIATE` and a conditional update.
- Added `claimed_by`, `claimed_at`, `attempt_count`, `last_error`, and `last_seen_at`.
- Added stale-claim recovery (`--stale-claim-minutes`, default 10).
- Added `multi-profiles` for multiple managed profile collectors.
- Shared profile queue CSV output is deferred to the parent process for multi-runner jobs to avoid concurrent file writers.
- `multi-listings` and `multi-profiles` accept repeatable `--runner` filters.

## Compatibility

Existing dev7 SQLite workspaces migrate additively on first connection. No destructive schema migration is required.

## Incremental trace batching and progress logging

`trace` can now be run in controlled batches:

```bat
zocdoc-collector --workspace .\workspace_test trace --limit 500 --batch-size 100
```

- `--limit` caps how many unparsed listing pages are examined in the current invocation.
- `--batch-size` controls SQLite commit frequency and progress logging.
- Progress, throughput, elapsed time, and ETA are printed to the terminal and appended to `output/trace_progress.log`.
- Batch commits reduce the overhead of committing once per listing while preserving resumability.
- With `--delete-html`, raw HTML is deleted only after the batch containing its durable trace has committed successfully.

For a full backlog, omit `--limit` and keep batch commits/logging:

```bat
zocdoc-collector --workspace .\workspace_test trace --batch-size 250
```

For manual iterative cleanup:

```bat
zocdoc-collector --workspace .\workspace_test trace --limit 500 --batch-size 100 --delete-html
```

Repeat the command until `selected=0`.
