# All-Specialties Architecture

## Scope decision

The all-specialties collector treats the live Zocdoc `/specialty` index as the source of truth for the catalog. Every entry exposed in that index is retained, including facility/clinic categories such as imaging facilities and urgent-care clinics. Downstream users can filter these entries by `entity_type` instead of losing them during discovery.

The package keeps the existing normal-Chrome capture model: Chrome renders Zocdoc pages, the local saver stores the HTML as durable evidence, and Python performs parsing and reconciliation offline.

## Discovery hierarchy

The all-specialties crawl is intentionally hierarchical rather than a literal specialty x ZIP Cartesian product:

```text
/specialty
  -> specialty catalog
  -> specialty landing pages
  -> published state/location targets
  -> listing + pagination pages
  -> provider observations
  -> globally deduplicated profile queue
```

Specialty landing pages expose Zocdoc's own location directory. Those URLs become the listing queue, preserving both specialty and geographic provenance.

## Canonical entities

The target production model has these entities:

- **specialties** — one row per Zocdoc specialty/category URL.
- **specialty_pages** — capture/retry state for each specialty landing page.
- **specialty_targets** — discovered specialty/location listing URLs.
- **providers** — one canonical row per provider identity.
- **provider_specialties** — many-to-many provider/specialty relationships.
- **provider_locations** — provider location objects with independent lifecycle state.
- **listing_observations** — raw appearances of a provider on a specialty/location listing page.
- **sync_runs** — one auditable record per initial or daily synchronization run.
- **change_events** — additions, changes, removals/deactivations, and relationship changes.

PostgreSQL is the intended production database because the core relationships are relational and require reliable uniqueness, upserts, reconciliation, and concurrent workers. Raw Zocdoc structures can still be retained in JSONB fields.

## Identity and grain

A provider is globally deduplicated even when the same profile appears under multiple specialties or locations. Provider-specialty and provider-location membership are stored separately rather than duplicating provider records.

The listing trace distinguishes:

- `listing_specialty_*` — the specialty whose search/listing produced the observation.
- `specialty_name` — the specialty displayed on the provider card itself.

These values can legitimately differ.

Removal is relationship-aware: disappearing from one specialty does not automatically mean the provider has left Zocdoc, and losing one location does not deactivate the provider globally.

## Controlled parallelism

The collector uses one orchestrator process with controlled parallel Chrome tabs. The CLI exposes `--concurrency` for specialty landing discovery and listing collection. This avoids requiring multiple independent processes to write to the same SQLite database during the initial development stage.

PostgreSQL will later allow stronger atomic job claiming and multi-process workers for production daily synchronization.

## Milestone 1 — catalog discovery

Version `0.2.0.dev0` added:

1. `captured/specialties/` timestamped `/specialty` snapshots.
2. `specialties` SQLite catalog table.
3. `output/specialties.csv`.
4. `zocdoc-collector ... specialties --collect`.
5. `zocdoc-ortho` compatibility alias.

## Milestone 2 — specialty-aware target discovery

Version `0.2.0.dev1` adds:

1. `captured/specialty_landings/` for timestamped specialty landing pages.
2. `specialty_pages` resumable capture queue.
3. `specialty_targets` specialty/location target table.
4. `discover-targets --collect --concurrency N`.
5. Generic listing URL support across specialty slugs.
6. Specialty provenance propagated through seed and pagination pages.
7. `provider_specialty_observations.csv` from the non-deduplicated trace.
8. Specialty filters on `collect-listings`.
9. Retry support for specialty landing-page jobs.

## Next milestone

The next milestone is PostgreSQL persistence plus daily synchronization semantics:

- sync run lifecycle,
- provider/location/specialty upserts,
- content hashes,
- added/changed/removed events,
- incomplete-run safeguards,
- scheduled daily execution,
- rolling profile refresh rather than blindly revisiting every profile every day.
