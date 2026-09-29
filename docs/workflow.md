# Workflow and checkpoints

The collector is intentionally staged.

## Discovery layer

1. Seed base location URLs.
2. Save rendered base listing HTML.
3. Discover pagination links from saved HTML.
4. Save all pagination HTML.
5. Rebuild the raw, non-deduplicated listing-provider trace offline.

The raw trace preserves provenance even when the same doctor appears on many listing pages.

## Profile layer

The doctor profile queue is deduplicated by normalized profile URL. Run one smoke-test profile before bulk collection, then save missing unique profile HTML in small concurrent windows.

## Parsing/export layer

Profile HTML is parsed offline, primarily from `window.__REDUX_STATE__`. DOM fallbacks are only used when structured state is unavailable. Exports can be regenerated without browser calls.

## Recovery model

SQLite queue state is not the sole source of truth. If HTML was saved before a kernel/process stopped, `reconcile` discovers it and updates queue state. This avoids unnecessary re-collection.

## Rebuilding ZIP inputs

After `final_providers.csv` exists, ZIP inputs for the original state/ZIP scraper can be rebuilt independently:

```bat
zocdoc-ortho --workspace .\workspace zip-input
```

The normal `export` and `derive` commands also generate these files automatically unless derived outputs are explicitly disabled during export.

## All-specialties discovery (0.2 development)

After the specialty catalog is captured, build specialty/location targets from Zocdoc's published location directories:

```bat
zocdoc-collector --workspace .\workspace specialties --collect
zocdoc-collector --workspace .\workspace discover-targets --collect --limit 25 --concurrency 4
```

Repeat `discover-targets --collect` until `status` shows no pending specialty pages. Then use the existing listing engine:

```bat
zocdoc-collector --workspace .\workspace collect-listings --limit 250 --concurrency 4
zocdoc-collector --workspace .\workspace trace
zocdoc-collector --workspace .\workspace collect-profiles --limit 250 --concurrency 3
```

A selected specialty can be processed independently with repeatable `--specialty` filters. The provider profile queue remains globally deduplicated across specialties.
