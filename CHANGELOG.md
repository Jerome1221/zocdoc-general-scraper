# Changelog

## 0.2.0.dev9.6.0-daily-sync

- Added complete fresh listing recrawls across every discovered pagination page for each selected specialty/location.
- Added staged capture validation so incomplete, restricted, invalid, or timed-out locations cannot apply provider removals.
- Added canonical provider and provider/location membership tables with add, reactivate, missing-streak, and soft-deactivate behavior.
- Added configurable removal confirmation, defaulting to two complete consecutive absences.
- Added per-run location results, exact provider-set snapshots, provider change events, run locking, and resumable profile jobs.
- Added canonical profile parsing, semantic data hashes, profile change events, cached-profile reuse, and configurable stale-profile refresh.
- Extended tokenized saver captures to profiles so refreshes cannot overwrite canonical profile HTML before validation.
- Added `updater daily`, `bootstrap`, `status`, `profiles`, `publish`, and `daemon` commands.
- Added optional transactional PostgreSQL projection for providers, memberships, runs, locations, and change events.
- Added Windows Task Scheduler scripts and an Anaconda/venv operations guide.
- Added end-to-end tests covering full pagination, add/remove application, incomplete-crawl removal protection, profile resume, and tokenized profile capture.

## 0.2.0.dev9.5.1-live-updater

- Added `updater snapshot --live` to capture fresh listing HTML through the existing Dev9 runner, Chrome profile, controller, and saver system.
- Added `--limit` with pre-dispatch selection so unselected locations never open browser tabs.
- Added `--from-snapshots` to retest only locations that already have successful snapshots; it can be combined with `--limit`.
- Added tokenized temporary updater captures so live checks do not overwrite the collector's normal saved listing HTML.
- Added the additive `snapshot_runs` table for run mode, timing, selected/processed totals, and success/failure counts.
- Added clear active-runner validation and startup guidance for live mode.
- Added live snapshot comparison and `count_changed` queue creation while leaving queue consumption, provider diffing, removal detection, profile refresh, and discovery out of scope.
- Added focused tests for limit selection, snapshot-only filtering, run history, immediate unchanged checks, and temporary saver captures.

## 0.2.0.dev9.5.0-updater

- Added additive SQLite migrations for `location_snapshots` and `refresh_queue`.
- Added reusable verified-count extraction for saved listing HTML headers such as `130 verified acupuncturists in Alexandria, VA`.
- Added `zocdoc-collector updater` command group with `snapshot`, `check`, and `queue`.
- Added baseline snapshot creation from the existing specialty/location database.
- Added daily count comparison that enqueues `reason=count_changed` refresh jobs only when the latest count differs from the latest snapshot.
- Documented the updater workflow, daily update process, snapshot logic, and refresh queue handoff.
- Added tests for count extraction and the snapshot/check/queue flow.

Dev9.5.0 does not run the queued refresh jobs. Queue consumption, provider diffing, new/removed provider handling, and profile refresh remain future work.
