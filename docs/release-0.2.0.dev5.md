# Release 0.2.0.dev5

Milestone 2.5B: multi-browser / N-runner collection.

## Added

- configurable saver host/port and controller URL;
- dynamic Chrome-extension binding to whichever local controller origin is open;
- persistent Chrome profile per runner;
- automated `runners init/start/status/stop` lifecycle;
- automatic Chrome/controller launch with `--load-extension`;
- N-runner support using sequential local ports (`8765`, `8766`, ...);
- shared SQLite queue with atomic listing-page claims so runners do not intentionally duplicate the same pending URL;
- per-runner manifests and performance CSVs;
- `multi-listings` command for parallel listing collection across active runners;
- combined runner throughput/ETA report via `runners perf`;
- listing ingest now syncs only providers seen on the current page instead of re-scanning the full doctor table after every page;
- shared CSV queue exports are deferred until the multi-runner batch ends to avoid concurrent file rewrites.

## Example

```bat
zocdoc-collector --workspace .\workspace runners init --count 2 --base-port 8765
zocdoc-collector --workspace .\workspace runners start
zocdoc-collector --workspace .\workspace runners status

zocdoc-collector --workspace .\workspace multi-listings ^
  --limit 400 ^
  --concurrency-per-runner 4

zocdoc-collector --workspace .\workspace runners perf
zocdoc-collector --workspace .\workspace runners stop
```

Runner Chrome profiles and logs live under `workspace/.runners/` and are runtime artifacts.

## Architecture

All runners share one canonical SQLite queue during this local-development milestone. Listing jobs are claimed with a short `BEGIN IMMEDIATE` transaction before being opened. Each browser session has its own saver server, local port, Chrome user-data directory, controller tab, manifest, and performance file.

This is intended for local throughput benchmarking before the shared queue is migrated to PostgreSQL. PostgreSQL remains the production target for multi-machine/distributed runners and daily reconciliation.

## Caveats

- `runners start` attempts to load the bundled unpacked extension automatically. If a Chrome build disallows command-line extension loading, load the extension once in each persistent runner profile; subsequent launches reuse that profile.
- Do not point multiple manual `collect-listings` processes at the same controller. Use `multi-listings`, which assigns a distinct controller URL and runner ID to each child process.
- Multi-runner profile collection is not exposed yet; this milestone focuses on the high-volume listing layer.
