# Command Cheatsheet

Use Anaconda Prompt (CMD). Run from the current package folder with its virtual environment activated. Set `WS` to the existing workspace that contains `collector.db`; an older workspace directory name does not mean its data is obsolete.

```bat
call .venv\Scripts\activate.bat
set "WS=C:\path\to\existing\workspace_test"
python -m zocdoc_ortho --version
```

## Completed Scrape To Canonical Data

For a newly completed specialty, not a workspace already imported successfully:

```bat
python -u -m zocdoc_ortho --workspace "%WS%" updater bootstrap --specialty psychiatrists
```

This is offline. It imports memberships, migrates local captures, then prepares jobs for missing or stale profiles. It does not browse. Existing unavailable profiles remain excluded from automatic capture. An unfiltered bootstrap still reprocesses every specialty.

To import memberships now and profiles separately:

```bat
python -u -m zocdoc_ortho --workspace "%WS%" updater bootstrap --specialty psychiatrists --skip-profiles
```

Use the printed run ID below. Do not type the placeholder literally:

```bat
set "RUN_ID=replace_with_printed_run_id"
python -u -m zocdoc_ortho --workspace "%WS%" updater import-local-profiles --run-id %RUN_ID% --specialty psychiatrists --dry-run
python -u -m zocdoc_ortho --workspace "%WS%" updater import-local-profiles --run-id %RUN_ID% --specialty psychiatrists --limit 25
python -u -m zocdoc_ortho --workspace "%WS%" updater import-local-profiles --run-id %RUN_ID% --specialty psychiatrists
```

The importer selects active memberships last seen in that run, with a saved collector status but missing canonical details. Profiles shared with another later run may therefore belong to the later run instead. It never resets collector statuses or creates browser jobs. Rejections and missing files must be reviewed, not blindly marked unavailable. Exit code 2 reports those exceptions. Already imported profiles are skipped when rerun.

## Interrupted Imports

Press Ctrl+C once and wait for the prompt. Committed work remains. Resume only after confirming the old process stopped:

```bat
python -u -m zocdoc_ortho --workspace "%WS%" updater bootstrap --resume-run-id %RUN_ID%
```

Resume uses the original specialty scope; do not add `--specialty`. It skips locations already committed and profiles already imported. If any newer updater run exists, the older bootstrap cannot be resumed because its baseline may be superseded. For a stopped historical run that should remain interrupted:

```bat
python -m zocdoc_ortho --workspace "%WS%" updater reconcile-run --run-id %RUN_ID%
python -m zocdoc_ortho --workspace "%WS%" updater reconcile-run --run-id %RUN_ID% --apply
```

Reconciliation changes run bookkeeping only. It does not reset work, remove providers, or claim all profile data is complete. Export readiness and run history are separate checks.

## Formatted CSV And JSON Delivery

Choose a fresh output folder:

```bat
python -u -m zocdoc_ortho --workspace "%WS%" export-delivery --specialty psychiatrists --output "C:\deliveries\psychiatrists_YYYYMMDD"
```

The matching datasets use the orthopedic 35-column provider/location schema and 21-column unique-provider schema. JSON has native nested arrays; CSV stores arrays as JSON strings. Provider identities are deduplicated, so exported provider counts may be lower than listing URL counts. Listing provenance selects providers, not a clinical credential filter. Unknown virtual flags stay unknown. Exceptions and checksums accompany the files. Draft specialties are placed under `needs-review` and omitted from the ZIP.

## Runners And Daily Updates

```bat
python -m zocdoc_ortho --workspace "%WS%" runners start --startup-wait 15
python -m zocdoc_ortho --workspace "%WS%" runners status
```

Resolve browser verification manually before clearing the restriction flag. Stop any collection first:

```bat
if exist "%WS%\BLOCKED.flag" del "%WS%\BLOCKED.flag"
python -m zocdoc_ortho --workspace "%WS%" retry --target profiles --statuses blocked timeout
```

The retry command above affects all specialties, not only the current one. Saved and unavailable profiles are not reset.

Test a daily update on a bounded selection before scheduling:

```bat
python -u -m zocdoc_ortho --workspace "%WS%" updater daily --live --specialty psychiatrists --limit 3 --removal-confirmations 2
python -m zocdoc_ortho --workspace "%WS%" updater status --limit 3
```

For Windows Task Scheduler use the absolute venv Python path, package folder as Start in, and an absolute workspace path. Schedule one non-overlapping daily invocation with the chosen specialty filters. The interactive Chrome controller must be available in the logged-in session; this is not an unattended headless service. Do not use `--limit 3` for production coverage. A full daily run recrawls selected listing locations and pagination, so measure runtime before assuming all specialties fit within 24 hours. No task is installed automatically by this package.

## Git And Storage

Review diffs and explicitly stage code, tests and documentation. Never push collector databases, captures, Chrome profiles, logs, credentials, or generated deliveries. `.gitignore` excludes common runtime files. Git does not remove already tracked sensitive files automatically.

Do not delete captures just because they are marked saved. Verify canonical coverage, validated delivery output and a recoverable backup first. Do not delete SQLite WAL/SHM files manually.
