# Daily Updater Guide

This is the operational path for Dev9.6. It is the same synchronization engine for a five-location test and a complete all-target run; `--limit` changes only how many locations are selected.

## 1. Install

Run in Anaconda Prompt:

```bat
cd /d C:\path\to\zocdoc-provider-collector-0.2.0.dev9.6.0-daily-sync
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[postgres]"
```

Use `python -m pip install -e .` if PostgreSQL publication is not needed.

## 2. Point to the existing workspace

Do not create a new workspace when testing the data already collected. Set `WS` to the directory containing `collector.db`, `captured`, and `output`:

```bat
set WS=C:\path\to\workspace_test
zocdoc-collector --workspace "%WS%" status
```

The database migration is additive. Existing Dev9 tables and data are preserved.

## 3. Bootstrap current data

```bat
zocdoc-collector --workspace "%WS%" updater bootstrap
zocdoc-collector --workspace "%WS%" updater status
```

Bootstrap reads the durable listing trace and seeds active canonical memberships. It makes no browser requests and never infers removals.

## 4. Start browser runners

If this workspace already has `.runners\runners.json`:

```bat
zocdoc-collector --workspace "%WS%" runners start
zocdoc-collector --workspace "%WS%" runners status
```

For a workspace with no runner configuration:

```bat
zocdoc-collector --workspace "%WS%" runners init --count 2
zocdoc-collector --workspace "%WS%" runners start
```

Every requested runner must report `server=true` and `controller=true`.

## 5. Controlled test

Test five locations from the previously successful snapshot set:

```bat
zocdoc-collector --workspace "%WS%" updater daily --live --from-snapshots --limit 5 --removal-confirmations 1
```

Inspect the result:

```bat
zocdoc-collector --workspace "%WS%" updater status
```

Expected behavior:

- every pagination page is freshly captured;
- a location is complete only after every page validates;
- provider additions and removals are recorded in `provider_change_events`;
- incomplete locations fail without applying removals;
- missing or stale profiles are captured and parsed into `canonical_provider_records`;
- temporary updater captures are deleted after use.

If profile collection was interrupted:

```bat
zocdoc-collector --workspace "%WS%" updater profiles
```

## 6. Full run

Use the default two-run removal confirmation and remove all testing limits:

```bat
zocdoc-collector --workspace "%WS%" updater daily --live
```

The target universe comes from `specialty_targets` and base rows in `pages`. Run the existing specialty catalog and target discovery workflow first when not all specialties and ZIP/location targets are present.

## 7. PostgreSQL publication

Install the PostgreSQL extra and set the URL:

```bat
set ZOCDOC_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
zocdoc-collector --workspace "%WS%" updater daily --live
```

The updater creates and upserts these PostgreSQL tables:

- `zocdoc_sync_runs`
- `zocdoc_sync_locations`
- `zocdoc_providers`
- `zocdoc_provider_locations`
- `zocdoc_provider_changes`

Publish an already completed local run:

```bat
zocdoc-collector --workspace "%WS%" updater publish --database-url "%ZOCDOC_DATABASE_URL%"
```

## 8. Daily scheduling

### Foreground daemon

```bat
zocdoc-collector --workspace "%WS%" updater daemon --live --interval-hours 24
```

### Windows Task Scheduler

Open PowerShell as the same Windows user that owns the Chrome profiles:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_daily_task.ps1 -Workspace "C:\path\to\workspace_test" -At "02:00"
```

The task runs only in an interactive logged-in session because managed Chrome must be visible. The task calls `scripts\run_daily_update.ps1`, which writes logs under `<workspace>\logs\daily-updater`.

To remove it:

```powershell
Unregister-ScheduledTask -TaskName "Zocdoc Provider Daily Sync" -Confirm:$false
```

## Safety properties

- One database writer is enforced with `daily_sync_lock`.
- A stale lock can be recovered after 24 hours.
- Listing pages are tokenized and staged before canonical apply.
- Provider deactivation occurs only after a complete successful location crawl.
- The default requires two consecutive complete absences.
- Every run, location result, provider set, and provider change is retained locally.
- PostgreSQL publication is transactional and idempotent.

