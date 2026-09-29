# Migrating from the notebook package

The package can import the earlier folder layout without requiring the old notebooks.

```bat
zocdoc-ortho --workspace .\workspace import-legacy "C:\path\to\zocdoc-normal-browser-link-collector-v2"
```

The importer:

- copies the SQLite database through SQLite's backup API (including committed WAL state),
- copies `zocdoc_locations.csv`,
- searches the known old `saved_html` / `saved_profiles` locations,
- initializes any missing tables/columns,
- reconciles saved HTML back into queue state,
- regenerates queue CSVs.

Stop the old notebook/saver before migration. That avoids a moving source snapshot and makes the imported state reproducible.

After migration, run:

```bat
zocdoc-ortho --workspace .\workspace status
zocdoc-ortho --workspace .\workspace trace
```

If the old folder did not contain the saved HTML directories, the DB can still be imported, but profile/listing parsing obviously requires the actual HTML files. Copy them into:

```text
workspace/captured/listings/
workspace/captured/profiles/
```

and run `reconcile`.


## Upgrading from dev7 to dev8

The migration is additive. Opening the existing workspace with dev8 adds listing trace-state columns and profile-claim columns automatically. Existing `collector.db` data is retained.

After installing dev8, run:

```bat
zocdoc-collector --workspace .\workspace_test status
zocdoc-collector --workspace .\workspace_test trace
```

The first dev8 `trace` parses any saved listing HTML not yet marked as durable. If an older `listing_provider_trace` already contains observations for a page whose raw HTML is already gone, dev8 preserves that trace and marks the page parsed rather than deleting the old observation.

Once `trace_status=parsed`, the raw listing file may be safely removed with:

```bat
zocdoc-collector --workspace .\workspace_test cleanup-listing-html --dry-run
zocdoc-collector --workspace .\workspace_test cleanup-listing-html
```

Profile rows from dev7 receive default empty claim metadata. New dev8 profile workers use atomic claims and can run concurrently across different runner/controller sessions.
