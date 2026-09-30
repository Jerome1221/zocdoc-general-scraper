# Zocdoc Provider Collector

## Dev9.6.1 complete daily updater

Dev9.6 turns the Dev9.5 count checker into a complete daily database synchronization path. It reuses the existing managed Chrome runners and parsers, but stages fresh listing captures separately until every pagination page for a location has passed validation.

For each selected specialty/location, a daily run now:

1. captures the base listing and follows all pagination;
2. rejects incomplete, zero-link, restricted, or timed-out crawls;
3. commits validated pages through the existing durable listing trace;
4. stores an auditable provider-set snapshot;
5. adds new memberships and reactivates returning providers;
6. soft-deactivates missing providers only after complete consecutive crawls;
7. parses cached profiles and captures missing, reactivated, or stale profiles;
8. stores canonical profile rows as JSON with a content hash;
9. optionally publishes providers, memberships, runs, and change events to PostgreSQL.

An incomplete location never applies removals. The default requires two complete daily absences before deactivation; use `--removal-confirmations 1` only for a controlled test.

### Install in Anaconda Prompt with a package-local venv

```bat
cd /d C:\path\to\zocdoc-provider-collector-0.2.0.dev9.6.0-daily-sync
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[postgres]"
```

The `postgres` extra is optional. Use `python -m pip install -e .` when PostgreSQL publication is not needed.

### First test on data already collected

Set the existing workspace once for the current Anaconda Prompt:

```bat
set WS=C:\path\to\your\workspace_test
```

Seed the canonical membership tables from the listing trace already in that workspace. This is offline and never removes a provider:

```bat
zocdoc-collector --workspace "%WS%" updater bootstrap
zocdoc-collector --workspace "%WS%" updater status
```

Bootstrap also imports validated records from `output\unique_doctors.csv` when that file exists. For a workspace that was already bootstrapped with Dev9.6.0, migrate the existing profile export once without repeating bootstrap:

```bat
zocdoc-collector --workspace "%WS%" updater migrate-profiles
```

The migration never opens browser tabs and never overwrites a newer canonical profile. Daily profile selection uses canonical profile data even when the old raw HTML is no longer retained.

Start the existing managed runners:

```bat
zocdoc-collector --workspace "%WS%" runners start
zocdoc-collector --workspace "%WS%" runners status
```

Run the real production workflow against five of the locations that already have successful snapshots:

```bat
zocdoc-collector --workspace "%WS%" updater daily --live --from-snapshots --limit 5 --removal-confirmations 1
zocdoc-collector --workspace "%WS%" updater status
```

`--limit 5` limits only the location selection. Each selected location still uses full pagination, exact provider diffing, profile updates, history, retries, and locking.

To test more of the currently successful set:

```bat
zocdoc-collector --workspace "%WS%" updater daily --live --from-snapshots --limit 32
```

### Full daily run

After all specialties and ZIP/location targets have been discovered by the normal collector, omit both test filters:

```bat
zocdoc-collector --workspace "%WS%" updater daily --live
```

This processes every active known specialty/location target. New specialty/location discovery remains the existing `specialties` plus `discover-targets` workflow; the daily updater maintains the resulting target universe.

Run continuously in the foreground every 24 hours:

```bat
zocdoc-collector --workspace "%WS%" updater daemon --live --interval-hours 24
```

For a durable Windows schedule that survives closing Anaconda Prompt, use `scripts\install_daily_task.ps1` as described in `DAILY_UPDATER_GUIDE.md`.

### PostgreSQL

Set the connection string without placing it in command history, then run normally:

```bat
set ZOCDOC_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
zocdoc-collector --workspace "%WS%" updater daily --live
```

You can republish the latest completed local run independently:

```bat
zocdoc-collector --workspace "%WS%" updater publish --database-url "%ZOCDOC_DATABASE_URL%"
```

SQLite remains the browser-side durable queue and audit database. PostgreSQL is a transactional canonical projection with current provider/profile data, active and inactive location memberships, run history, and provider change events.

### Recovery and inspection

```bat
zocdoc-collector --workspace "%WS%" updater status
zocdoc-collector --workspace "%WS%" updater profiles
zocdoc-collector --workspace "%WS%" updater profiles --run-id 12 --limit 100
```

The updater lock prevents two daily writers from running at once. Failed profile captures remain resumable. The legacy `updater snapshot`, `updater check`, and `updater queue` commands are retained for count-only diagnostics, but they are not the complete synchronization workflow.

## Dev9.4.1 profile identity hotfix

Dev9.4.1 accepts provider-card profile URLs that do not end in a numeric id, including `/dentist/name-dmd` and `/doctor/<npi>-name-dmd`, while still excluding listing-location URLs ending in `pm`. This keeps validation, durable trace, and profile queueing aligned.


## Dev9.4 provider-profile path fix

Zocdoc provider profile URLs are not limited to `/doctor/...`; e.g. Dentist profile links use `/dentist/...`. Dev9.4 recognizes provider profile URLs by the stable trailing numeric profile ID, uses `doctor-card-info-name` anchors for listing readiness, traces those profiles durably, and allows profile collection for specialty-specific paths such as `/dentist/...`.


> **Dev9.2 pagination trace fix:** durable trace now recovers provider links outside expected card wrappers and raw listing HTML is deleted only after trace coverage matches validation. Runtime profile projections are trace-backed.


> **Safe-runner patch (`0.2.0.dev9+saferunner1`)**: managed runner restarts now verify process ownership before terminating a PID, and Chrome is launched as a visible Windows GUI process. This is the recommended Dev9 package when Dev8 is also running.


A reusable, CLI-first pipeline for collecting and parsing Zocdoc provider data. Version `0.2.0.dev9` keeps the dev8 multi-runner/atomic profile workflow and adds **inline listing trace + automatic raw-listing cleanup**. Successful listing captures are parsed locally immediately after the normal Chrome capture, committed to SQLite, and their raw listing HTML is deleted by default. Failed/review captures are retained for diagnosis.

The package deliberately separates **browser capture**, **queue state**, **offline parsing**, and **exports** so collection can be resumed, tested, audited, and run incrementally without repeating completed work.

## What it does

The full pipeline is:

```text
location URLs
  -> normal rendered Chrome capture
  -> immediate local listing parse + durable SQLite trace
  -> successful raw listing HTML deleted automatically
  -> discovered pagination URLs
  -> deduplicated doctor profile queue
  -> saved doctor profile HTML
  -> structured profile parser
  -> canonical 35-column provider-location CSV
  -> unique-doctor / virtual-location / physical-location outputs
  -> ZIP-by-state workbook for the original ZIP scraper
```

Listing collection and doctor-profile collection are intentionally separate. You do **not** need to finish every listing page before beginning profile collection.

The package uses a normal Chrome session plus a small local extension to capture rendered HTML. In dev9, successful **listing** HTML is only transient: after local parsing and a successful trace commit, it is deleted automatically. Profile HTML is still retained for the current profile-derive workflow.

It contains **no proxy rotation, CAPTCHA solving, browser fingerprint spoofing, or stealth/evasion logic**. If the browser encounters a restriction or verification page, the saver creates `BLOCKED.flag` and collection stops for review.

---

## Repository layout

```text
src/zocdoc_ortho/      Python package
  chrome_extension/    unpacked Chrome extension bundled with the package

data/                  location seed data
examples/              small example inputs
docs/                  workflow, migration, and data-model documentation
tests/                 parser and output tests

workspace/             runtime data; created locally and gitignored
```

All mutable collection state lives inside a workspace:

```text
workspace/
  collector.db
  zocdoc_locations.csv

  captured/
    specialties/
    specialty_landings/
    listings/
    profiles/

  output/

  manifest.csv
  BLOCKED.flag          # created only if a restriction page is detected
```

You may use any directory as the workspace by passing it to `--workspace`.

---

## Installation

Windows example:

```bat
py -m venv .venv
.venv\Scripts\activate

python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Initialize a workspace:

```bat
zocdoc-ortho --workspace .\workspace init
```

---

## Chrome saver setup

Start the local saver in one terminal:

```bat
zocdoc-ortho --workspace .\workspace saver
```

Find the bundled Chrome extension directory:

```bat
zocdoc-ortho extension-path
```

Then:

1. Open `chrome://extensions/`.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select the extension directory printed by `zocdoc-ortho extension-path`.
4. Open `http://127.0.0.1:8765/controller` in the same Chrome profile where the extension is loaded.
5. Keep the controller tab open while collecting.

`127.0.0.1` is the standard local loopback address. It always refers to the same computer running the saver; it is not a public IP address.

Verify the connection:

```bat
zocdoc-ortho --workspace .\workspace health
```

`controller_active` should be `true`, and the saver workspace should match the CLI workspace.

---

## All-specialties discovery workflow (v0.2 development)

The live Zocdoc `/specialty` page is treated as the source of truth for the specialty/category catalog. The collector keeps every catalog entry, including facility/clinic categories, and assigns an `entity_type` so downstream users can filter them without losing source coverage.

The package installs both CLI names:

```bat
zocdoc-collector --help
zocdoc-ortho --help
```

`zocdoc-ortho` remains a compatibility alias. New all-specialties documentation uses `zocdoc-collector`.

### 1. Capture the specialty catalog

With the saver and Chrome controller running:

```bat
zocdoc-collector --workspace .\workspace specialties --collect
```

This writes timestamped source HTML under `workspace/captured/specialties/` and reconciles `workspace/output/specialties.csv`.

You can rebuild the catalog offline from the latest saved capture with:

```bat
zocdoc-collector --workspace .\workspace specialties
```

### 2. Discover specialty/location crawl targets

Each specialty landing page contains Zocdoc's published location directory for that specialty. Capture several specialty landing pages concurrently with:

```bat
zocdoc-collector --workspace .\workspace discover-targets --collect --limit 25 --concurrency 4
```

Repeat until the specialty-page queue is complete. The command writes:

```text
workspace/output/specialty_pages.csv
workspace/output/specialty_targets.csv
workspace/output/specialty_target_summary.csv
```

Each discovered target is also seeded into the existing listing queue with its specialty provenance. You can work on selected specialties only:

```bat
zocdoc-collector --workspace .\workspace discover-targets --collect --specialty cardiologists --specialty dermatologists --concurrency 2
```

Rebuild target files offline from already captured specialty landing HTML with:

```bat
zocdoc-collector --workspace .\workspace discover-targets
```

### 3. Collect specialty listing and pagination pages

The existing listing engine is now specialty-aware and retains controlled parallel collection:

```bat
zocdoc-collector --workspace .\workspace collect-listings --limit 250 --concurrency 4
```

To focus on one or more specialties:

```bat
zocdoc-collector --workspace .\workspace collect-listings --specialty cardiologists --specialty dermatologists --limit 250 --concurrency 4
```

The queue preserves `specialty_name`, `specialty_slug`, and `specialty_url` on seed and pagination pages. Provider profiles remain globally deduplicated even when the same provider appears under multiple specialties.

### 4. Build the provider trace incrementally

```bat
zocdoc-collector --workspace .\workspace trace
```

`trace` now persists each successfully parsed listing page in SQLite (`pages.trace_status='parsed'`) and preserves all historical listing-provider observations in `listing_provider_trace`. Later runs parse only new/unparsed listing HTML while regenerating the CSV summaries from the full SQLite trace history. This means already parsed raw listing HTML can be removed without losing the trace.

To parse and immediately remove each successfully committed listing HTML:

```bat
zocdoc-collector --workspace .\workspace trace --delete-html
```

Or preview / perform cleanup later:

```bat
zocdoc-collector --workspace .\workspace cleanup-listing-html --dry-run
zocdoc-collector --workspace .\workspace cleanup-listing-html
```

A newly re-captured listing automatically clears its trace marker, so it must be parsed again before cleanup.

In addition to the existing trace files, this writes:

```text
workspace/output/provider_specialty_observations.csv
```

This file preserves provider-to-listing-specialty relationships separately from the provider card's displayed specialty.

### Parallelism and performance tuning (v0.2.0.dev4)

Parallelism is controlled inside one collector process with `--concurrency`; it does not require multiple independent processes writing to the same SQLite database. Milestone 2.5 replaces fixed-size batches with a **rolling worker pool**: whenever one capture finishes, that slot can immediately take the next pending target instead of waiting for the slowest page in the previous batch.

Listing defaults are tuned for the repetitive listing-page structure while preserving readiness checks:

```bat
zocdoc-collector --workspace .\workspace collect-listings ^
  --limit 250 ^
  --concurrency 4 ^
  --stagger 0.1 ^
  --poll 0.2 ^
  --page-check-ms 250 ^
  --stable-checks 2 ^
  --page-max-wait-ms 8000 ^
  --controller-poll-ms 200
```

The timing flags mean:

- `--stagger`: minimum delay between newly queued tabs.
- `--poll`: how often the Python collector checks for completed HTML.
- `--page-check-ms`: how often the Chrome content script checks whether the rendered result is stable.
- `--stable-checks`: consecutive identical meaningful readiness signatures required before saving.
- `--page-max-wait-ms`: fallback maximum readiness wait before capture.
- `--controller-poll-ms`: how often the controller asks the local saver for another URL.

Profiles intentionally keep more conservative defaults than listing pages. Do not reduce readiness settings solely for speed without checking completeness.

Every measured collection run appends:

```text
workspace/output/capture_performance.csv
workspace/output/crawl_run_metrics.csv
```

Use the recent measured throughput to estimate remaining work:

```bat
zocdoc-collector --workspace .\workspace perf-report --command collect-listings --last 5
```

The listing queue also stores a deterministic `provider_set_hash` for each saved page. This is groundwork for the later daily-change detector; it does not yet deactivate providers or implement the PostgreSQL daily sync.

**After upgrading from an earlier package, restart the saver and reload the unpacked extension in `chrome://extensions/`** so Chrome uses the new runtime-configurable capture logic.

### Status and retry

```bat
zocdoc-collector --workspace .\workspace status
```

Status includes specialty landing-page progress, active specialty/location targets, listing pages, pagination, and profile counts. Retry interrupted specialty landing captures with:

```bat
zocdoc-collector --workspace .\workspace retry --target specialty-pages --statuses opening timeout
```

See [`docs/all-specialties-architecture.md`](docs/all-specialties-architecture.md) for the target database and daily-sync design. PostgreSQL synchronization and daily change tracking are intentionally the next milestone after specialty-aware collection is validated.

---

# Existing orthopedic production workflow

## 1. Seed location pages

```bat
zocdoc-ortho --workspace .\workspace seed .\data\zocdoc_locations.csv
```

The input CSV requires a `url` column.

`state_group` and `location` are optional but recommended because they improve provenance and reporting.

---

## 2. Collect location and pagination pages

```bat
zocdoc-ortho --workspace .\workspace collect-listings --limit 250
```

Run this command repeatedly as needed.

`collect-listings` only works on the listing-page queue. It **never automatically starts doctor-profile collection**.

Check progress at any time:

```bat
zocdoc-ortho --workspace .\workspace status
```

If listing pages are left in `opening` or `timeout`:

```bat
zocdoc-ortho --workspace .\workspace retry --target listings --statuses opening timeout
```

---

## 3. Build or refresh the doctor trace

You can run `trace` whenever saved listing HTML is available:

```bat
zocdoc-ortho --workspace .\workspace trace
```

This is an **offline** operation. It does not open Zocdoc pages.

It parses only saved listing HTML that has not already been durably traced, then creates/refreshes:

- `listing_doctor_occurrences_nondedup.csv`  
  Every observed doctor-card occurrence. Duplicate appearances are intentionally preserved.

- `doctor_occurrence_summary.csv`  
  One row per unique doctor profile URL with occurrence counts.

- `unique_doctors_from_listings.csv`  
  The deduplicated set of doctor profile URLs discovered so far.

The same raw listing-to-provider trace is also stored in SQLite as `listing_provider_trace`.

Running `trace` again after collecting more listing pages parses only the newly saved/unparsed pages, preserves older SQLite trace rows even if their raw HTML was deleted, and adds newly discovered doctors to the profile workflow.

---

# Flexible / incremental collection

The pipeline does **not** need to be run as one strict sequence.

For example, you may collect some listing pages:

```bat
zocdoc-ortho --workspace .\workspace collect-listings --limit 250
```

Build the doctor trace:

```bat
zocdoc-ortho --workspace .\workspace trace
```

Then immediately collect the doctor profiles discovered so far:

```bat
zocdoc-ortho --workspace .\workspace collect-profiles --limit 250
```

Later, you can continue listing collection:

```bat
zocdoc-ortho --workspace .\workspace collect-listings --limit 250
```

Refresh the trace:

```bat
zocdoc-ortho --workspace .\workspace trace
```

Then collect only the newly discovered or still-pending profiles:

```bat
zocdoc-ortho --workspace .\workspace collect-profiles --limit 250
```

Conceptually:

```text
collect listings
  -> trace
  -> collect discovered profiles
  -> collect more listings
  -> trace again
  -> collect newly discovered profiles
```

Existing saved profiles remain saved. The doctor profile queue is deduplicated by normalized profile URL, so previously completed doctor profiles do not need to be collected again.

### Important

A partially completed listing queue produces a **partial doctor universe**.

It is safe to collect and inspect profiles before listing discovery is complete, but the nationwide dataset should only be considered complete after:

1. All intended location and pagination pages have been processed.
2. `trace` has been run against the completed listing collection.
3. All newly discovered doctor profiles have been added to the queue.
4. All resulting doctor profiles have been collected or reviewed.
5. The final export has been generated.

---

## 4. One-doctor smoke test

Before a large profile run, test the workflow with one doctor:

```bat
zocdoc-ortho --workspace .\workspace smoke
```

Or choose a specific doctor already present in the queue:

```bat
zocdoc-ortho --workspace .\workspace smoke --doctor-url "https://www.zocdoc.com/doctor/..."
```

The smoke test:

- saves or reuses exactly one doctor profile HTML;
- parses the structured profile data;
- maps the result to the canonical schema;
- writes `output/smoke_profile.csv`;
- prints parser diagnostics.

Inspect this output before beginning a large profile run.

---

## 5. Collect doctor profile HTML

```bat
zocdoc-ortho --workspace .\workspace collect-profiles --limit 250
```

The listing trace may contain the same doctor many times across different locations and pagination pages.

The profile queue is intentionally deduplicated by normalized doctor profile URL, so each unique doctor profile HTML only needs to be saved once.

Check progress with:

```bat
zocdoc-ortho --workspace .\workspace status
```

Retry profile rows if necessary:

```bat
zocdoc-ortho --workspace .\workspace retry --target profiles --statuses opening timeout
```

---

## 6. Export

When profile collection is complete:

```bat
zocdoc-ortho --workspace .\workspace export
```

For a deliberate partial export during development or QA:

```bat
zocdoc-ortho --workspace .\workspace export --allow-partial
```

A partial export is useful for testing, but should not be interpreted as a complete nationwide provider dataset.

By default, `export` also produces ZIP inputs for the original state/ZIP scraper from the `state` + `postal_code` values observed in the canonical provider-location data. Repeated ZIPs are deduplicated within each state.

You can rebuild only those ZIP inputs at any time after `final_providers.csv` exists:

```bat
zocdoc-ortho --workspace .\workspace zip-input
```

Or build them from another canonical-style provider CSV:

```bat
zocdoc-ortho --workspace .\workspace zip-input --input .\path\to\final_providers.csv
```

---

# Main outputs

All generated datasets are written under:

```text
workspace/output/
```

## `final_providers.csv`

The canonical provider-location dataset using the 35 repo fields.

Its grain is:

```text
one provider × one Zocdoc location object
```

Because Zocdoc can represent physical and virtual locations as separate location objects, the same provider and address may appear in more than one canonical row.

The raw `locations` JSON is retained so the original location structure is not lost.

---

## `unique_doctors.csv`

One row per unique provider.

Top-level location-specific fields are removed, while the complete `locations` JSON remains available.

This is useful when the desired unit of analysis is the **doctor/provider**, rather than the provider-location combination.

---

## `doctor_locations_virtual.csv`

Contains canonical provider-location rows where:

```text
is_virtual_location == True
```

---

## `doctor_locations_physical.csv`

Contains canonical provider-location rows where:

```text
is_virtual_location == False
```

Rows where the virtual/physical status is unknown are deliberately not forced into either derived dataset.

---

## `zocdoc_scraped_zipcodes_by_state.xlsx`

An Excel workbook containing the unique ZIP codes observed in collected provider locations, arranged as one column per two-letter state/area code.

The primary sheet is named:

```text
UC Extended Zipcodes
```

This keeps the workbook compatible with the original state/ZIP scraper input convention. A second `ZIP Counts` sheet provides QA counts by state/area. ZIP values are written as text so leading zeroes are preserved.

## `zocdoc_scraped_zipcodes_long.csv`

A long-format QA/version-control-friendly companion file with:

```text
state,zip
PA,19462
NY,10001
...
```

The ZIP input uses the actual canonical provider-location fields `state` and `postal_code`. It does **not** use `source_zip`, so the workbook represents ZIPs observed in collected Zocdoc provider locations rather than the ZIPs originally used for discovery.

## `zocdoc_scraped_zipcode_conflicts.csv`

A QA file listing any normalized ZIP that appears under more than one state/area code in the collected data. It is normally empty; conflicts are preserved for review rather than silently reassigned.

See [`docs/data-model.md`](docs/data-model.md) for canonical field definitions and normalization rules.

---

# Canonical output schema

`final_providers.csv` contains these 35 fields:

```text
state
full_name
specialty_name
practice_name
npi
provider_id
monolith_id
first_name
last_name
prenominal
postnominal
average_rating
review_count
profile_url
accepts_new_patients
offers_telemedicine
only_sees_children
hospital_affiliations
bio
num_locations
address_line_1
address_line_2
city
postal_code
phone
location_id
location_name
can_have_appointments
is_virtual_location
has_new_patient_availability
photo_url
provider_location_key
locations
source_zip
source_zip_state
```

The package distinguishes direct Zocdoc fields from normalized/inferred fields. See `docs/data-model.md` for the exact source and fallback hierarchy used for each field.

---

# Resume and reconcile

Saved HTML is the durable evidence layer.

If the CLI stops after Chrome has already saved a page, run:

```bat
zocdoc-ortho --workspace .\workspace reconcile
```

This scans the local capture directories and updates SQLite for listing/profile HTML that exists on disk but has not yet been reflected in queue state.

This allows collection to resume without unnecessarily repeating completed browser work.

---

# Importing the legacy notebook collector

The package includes a migration command for the earlier collector directory layout:

```bat
zocdoc-ortho --workspace .\workspace import-legacy "C:\path\to\zocdoc-normal-browser-link-collector-v2"
```

The migration process:

- imports the existing SQLite database using SQLite's backup API;
- copies recognized legacy listing/profile HTML;
- runs required schema migrations;
- reconciles copied files with queue state.

Stop old collector processes before migration so the imported database represents a stable snapshot.

After migration, verify with:

```bat
zocdoc-ortho --workspace .\workspace status
```

and:

```bat
zocdoc-ortho --workspace .\workspace reconcile
```

---

# Restriction handling

If the saver observes a restriction or verification page, it creates:

```text
workspace/BLOCKED.flag
```

Collection commands refuse to continue while this file exists.

Inspect it with:

```bat
zocdoc-ortho --workspace .\workspace show-blocked
```

Resume only after normal site access has been restored and the restriction has been reviewed.

The package does not provide restriction-bypass tooling.

---

# Data safety and Git

Runtime data is intended to remain outside version control.

The following types of files should remain gitignored:

```text
workspace/
collector.db
collector.db-wal
collector.db-shm
captured/
output/
manifest.csv
BLOCKED.flag
```

The repository should contain the reusable application code, tests, documentation, Chrome extension, and appropriate seed/example inputs — not large generated HTML collections or runtime databases.

---

# Development checks

Run the test suite:

```bat
pytest
```

Run linting:

```bat
ruff check src tests
```

The included tests cover:

- URL normalization;
- embedded Redux extraction;
- listing occurrence extraction;
- profile parsing;
- bookability rules;
- telemedicine normalization;
- virtual/physical location handling;
- derived output grain;
- ZIP normalization and ZIP-by-state workbook generation.

---

# Typical command sequence

A full sequential run can be:

```bat
zocdoc-ortho --workspace .\workspace init

zocdoc-ortho --workspace .\workspace seed .\data\zocdoc_locations.csv

zocdoc-ortho --workspace .\workspace saver

zocdoc-ortho --workspace .\workspace health

zocdoc-ortho --workspace .\workspace collect-listings --limit 250

zocdoc-ortho --workspace .\workspace trace

zocdoc-ortho --workspace .\workspace smoke

zocdoc-ortho --workspace .\workspace collect-profiles --limit 250

zocdoc-ortho --workspace .\workspace export
```

For long-running collection, the recommended pattern is instead to treat listing and profile collection as resumable queues and alternate between them as needed.

---

# Multi-browser runners (0.2.0.dev6)

Milestone 2.5B can run multiple isolated Chrome/controller sessions against one canonical local listing queue. This is intended to test whether several moderate-concurrency browser sessions outperform one saturated browser session.

Initialize two persistent runner profiles:

```bat
zocdoc-collector --workspace .\workspace runners init --count 2 --base-port 8765
```

Start both saver servers and Chrome/controller sessions automatically:

```bat
zocdoc-collector --workspace .\workspace runners start
```

Check readiness:

```bat
zocdoc-collector --workspace .\workspace runners status
```

A healthy two-runner setup should show both local servers and controllers active on sequential ports such as `8765` and `8766`.

Run a combined listing benchmark. `--limit` is the total page budget across all configured runners, while `--concurrency-per-runner` is the number of active listing tabs inside each Chrome session:

```bat
zocdoc-collector --workspace .\workspace multi-listings ^
  --limit 400 ^
  --concurrency-per-runner 4
```

The workers share the same SQLite queue and claim pending listing URLs atomically. Per-runner performance files are written separately:

```text
workspace/output/crawl_run_metrics.runner-01.csv
workspace/output/crawl_run_metrics.runner-02.csv
workspace/output/capture_performance.runner-01.csv
workspace/output/capture_performance.runner-02.csv
```

Show the latest combined throughput and ETA:

```bat
zocdoc-collector --workspace .\workspace runners perf
```

Stop the local savers and runner browsers:

```bat
zocdoc-collector --workspace .\workspace runners stop
```

Runner browser profiles and logs are stored under:

```text
workspace/.runners/
```

The number of runners is configurable rather than hard-coded to two:

```bat
zocdoc-collector --workspace .\workspace runners init --count 3 --base-port 8765
```

which assigns ports `8765`, `8766`, and `8767`.

The single-runner commands remain supported. For example:

```bat
zocdoc-collector --workspace .\workspace saver
zocdoc-collector --workspace .\workspace collect-listings --limit 200 --concurrency 4
```

This milestone still uses SQLite locally. PostgreSQL remains the planned production shared queue for daily synchronization and broader horizontal scaling.

## Zero-provider listing validation and retry (dev6)

A listing capture is no longer considered complete only because an HTML file exists.
If a rendered listing contains provider links it remains `saved`. If it explicitly reports
zero results it is marked `valid_empty`. A zero-link capture without an explicit empty-state
marker is quarantined under `captured/listing_retries/` and marked `retry_pending`.
At the start of the next listing collection run, those rows are returned to `pending` and
are fetched again. After the default two scheduled retries, an unresolved page is marked
`review` instead of looping forever.

Audit and requeue zero-link pages that were already saved by an older package:

```bat
zocdoc-collector --workspace .\workspace_test retry-zero-listings
```

Then run listings normally. Retry candidates from the audit are activated automatically:

```bat
zocdoc-collector --workspace .\workspace_test collect-listings --limit 200 --concurrency 4
```

The same behavior applies to `multi-listings`. The retry cap can be changed with
`--zero-link-max-retries N` for collection or `--max-retries N` for the legacy audit.
`page_queue.csv` now includes `validation_status`, `validation_reason`,
`zero_link_retry_count`, and `last_rejected_file`.

## v0.2.0.dev7 multi-runner hotfix

If upgrading from dev6, reinitialize the runner config and force a clean browser/server relaunch:

```bat
zocdoc-collector --workspace "C:\path\to\workspace_test" runners init --count 2 --base-port 8765
zocdoc-collector --workspace "C:\path\to\workspace_test" runners start --restart --startup-wait 5
zocdoc-collector --workspace "C:\path\to\workspace_test" runners status
```

Expected controller assignments are `runner-01 -> 8765`, `runner-02 -> 8766`, and so on.


## v0.2.0.dev8 incremental trace + atomic profiles

### Safe incremental listing parsing and disk cleanup

The listing trace is now durable in SQLite. Each successfully parsed listing gets:

```text
trace_status = parsed
trace_parsed_at
trace_occurrence_count
trace_source_file
trace_html_deleted_at
```

Normal incremental parse:

```bat
zocdoc-collector --workspace .\workspace_test trace
```

Parse new/unparsed listing HTML and delete each raw file only after its SQLite trace transaction succeeds:

```bat
zocdoc-collector --workspace .\workspace_test trace --delete-html
```

Preview safe cleanup of already-parsed listing HTML:

```bat
zocdoc-collector --workspace .\workspace_test cleanup-listing-html --dry-run
```

Then delete:

```bat
zocdoc-collector --workspace .\workspace_test cleanup-listing-html
```

`listing_provider_trace` is no longer dropped/rebuilt from the files currently on disk. CSV trace outputs are regenerated from SQLite, so deleting an already-parsed listing HTML does not erase its historical provider observations.

### Atomic profile claims

Profile workers now claim rows using an atomic SQLite transaction. `doctor_profiles` includes `claimed_by`, `claimed_at`, `attempt_count`, `last_error`, and `last_seen_at`. A crashed worker's claim is recoverable after the configurable stale interval:

```bat
zocdoc-collector --workspace .\workspace_test collect-profiles ^
  --limit 500 ^
  --concurrency 3 ^
  --stale-claim-minutes 10 ^
  --controller-url http://127.0.0.1:8767 ^
  --runner-id runner-03
```

Multiple profile collectors can now safely share the same queue. With managed runners, use:

```bat
zocdoc-collector --workspace .\workspace_test multi-profiles ^
  --runner runner-03 ^
  --runner runner-04 ^
  --limit 500 ^
  --concurrency-per-runner 3
```

Runner selection is also available for listings, so runners can be partitioned cleanly:

```bat
zocdoc-collector --workspace .\workspace_test multi-listings ^
  --runner runner-01 ^
  --runner runner-02 ^
  --limit 500 ^
  --concurrency-per-runner 4
```

### Batch / resumable trace processing

For large listing backlogs, `trace` supports bounded iterative runs and durable batch commits:

```bat
zocdoc-collector --workspace .\workspace_test trace --limit 500 --batch-size 100
```

This parses at most 500 currently unparsed listing captures, commits every 100 files, and writes progress to both the terminal and:

```text
workspace_test\output\trace_progress.log
```

Example log lines include processed count, parsed count, missing HTML, doctor occurrences, files/second, elapsed time, and ETA. Re-running the same command continues with the next unparsed listings because completed batches are marked in SQLite.

To process the entire backlog while still committing/logging in batches:

```bat
zocdoc-collector --workspace .\workspace_test trace --batch-size 250
```

To parse and reclaim disk space iteratively:

```bat
zocdoc-collector --workspace .\workspace_test trace --limit 500 --batch-size 100 --delete-html
```

With `--delete-html`, a raw listing HTML is removed only after its trace batch has committed successfully.


## v0.2.0.dev9 inline trace + transient listing HTML

Dev9 changes **local post-capture handling only**. It does not add scrolling, clicks, extra Zocdoc requests, or longer page-readiness waits. Chrome captures the page the same way as dev8 and can close after the local saver accepts the capture.

For each successful listing capture, the collector now:

```text
normal Chrome capture
  -> local listing ingest
  -> persist listing_provider_trace immediately
  -> add newly discovered unique doctors to doctor_profiles
  -> commit SQLite
  -> delete successful listing HTML
```

Therefore new dev9 listing runs normally keep `Saved listings awaiting trace` at zero without a separate `trace` command. If inline trace fails, the HTML is kept so the legacy `trace` command can recover it later. Retry/review captures are also retained.

To debug while retaining successful listing HTML, add:

```bat
zocdoc-collector --workspace .\workspace_test collect-listings --keep-listing-html
```

The same option is available for `multi-listings`.

### Existing dev8 HTML

Existing successfully traced listing HTML can still be cleaned safely:

```bat
zocdoc-collector --workspace .\workspace_test cleanup-listing-html --dry-run
zocdoc-collector --workspace .\workspace_test cleanup-listing-html
```

### Updater signals and canonical synchronization

The listing parser extracts the visible `X verified ... in ...` value into `pages.advertised_total` and stores a per-page `provider_set_hash`. The legacy count checker still uses the advertised total as a diagnostic signal. Dev9.6's complete updater independently captures all pagination, computes the exact location-level provider set, records that snapshot, and applies canonical membership changes only after the entire location succeeds.

## Specialty runtime benchmarking (dev9)

Dev9 records wall-clock timing for a specialty when you use a single `--specialty` filter. The timing is local bookkeeping only; it does not add browser interactions or Zocdoc requests.

Measured stages:

- target discovery
- base listings
- pagination
- net-new profiles first discovered by that specialty

Example benchmark for Dentists:

```bat
zocdoc-collector --workspace .\workspace_test discover-targets --collect --specialty dentists --limit 1
zocdoc-collector --workspace .\workspace_test multi-listings --specialty dentists --phase seed --limit 50
zocdoc-collector --workspace .\workspace_test specialty-runtime --specialty dentists
```

After the base queue is complete, benchmark pagination:

```bat
zocdoc-collector --workspace .\workspace_test multi-listings --specialty dentists --phase pagination --limit 100
zocdoc-collector --workspace .\workspace_test specialty-runtime --specialty dentists
```

Then benchmark only the net-new Dentist provider profiles, without mixing in the older dev8 profile backlog:

```bat
zocdoc-collector --workspace .\workspace_test multi-profiles --specialty dentists --limit 100
zocdoc-collector --workspace .\workspace_test specialty-runtime --specialty dentists
```

The report uses partial measured throughput to project the full specialty runtime. Raw measurements are appended to:

```text
workspace_test\output\specialty_runtime.csv
```

For clean attribution, benchmark one specialty at a time.

## Dev9.3 trace alignment / offline repair

If a previous Dev9 run retained listing HTML with `status=review` and `validation_status=trace_partial`, repair those pages locally without opening Chrome or making any Zocdoc request:

```bat
zocdoc-collector --workspace C:\z9test repair-trace-partials
```

After reviewing the repaired counts, optionally remove only successfully repaired HTML:

```bat
zocdoc-collector --workspace C:\z9test repair-trace-partials --delete-html
```

## Dev9.4.2 profile capture and timing

Dev9.4.2 aligns the Chrome extension profile recognizer with the Python profile recognizer, so valid no-ID profile URLs and `Claim your profile` pages save and close normally instead of waiting for profile timeout. It also prints stage timing for browser/capture, listing ingest/trace, profile queue updates, and final export parsing. See `docs/release-0.2.0.dev9-profilecapture42.md`.
