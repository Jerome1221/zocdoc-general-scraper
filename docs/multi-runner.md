# Multi-runner local collection

`0.2.0.dev5` adds N independent Chrome/controller sessions on one machine while keeping one canonical SQLite queue.

## Why

The live benchmark showed one Chrome session at concurrency 4 outperforming 6 and 8 concurrent tabs. Multi-runner mode tests horizontal browser scaling instead: for example, two independent Chrome profiles with four active listing tabs each.

## One-time setup

```bat
zocdoc-collector --workspace .\workspace runners init --count 2 --base-port 8765
```

This creates persistent profiles and logs under `workspace/.runners/`.

## Start automatically

```bat
zocdoc-collector --workspace .\workspace runners start
```

For two runners this launches:

- runner-01 saver/controller on `127.0.0.1:8765`;
- runner-02 saver/controller on `127.0.0.1:8766`;
- one persistent Chrome profile for each runner;
- the bundled extension and each runner's controller tab.

Verify:

```bat
zocdoc-collector --workspace .\workspace runners status
```

Both `server` and `controller` should be `true` before collection.

## Multi-run listing benchmark

```bat
zocdoc-collector --workspace .\workspace multi-listings ^
  --limit 400 ^
  --concurrency-per-runner 4
```

The total limit is split across configured runners. Pending listing pages are claimed atomically from the shared SQLite queue so two runners do not intentionally choose the same pending URL.

Runner logs:

```text
workspace/.runners/logs/runner-01.collect-listings.log
workspace/.runners/logs/runner-02.collect-listings.log
```

Performance files:

```text
workspace/output/crawl_run_metrics.runner-01.csv
workspace/output/crawl_run_metrics.runner-02.csv
workspace/output/capture_performance.runner-01.csv
workspace/output/capture_performance.runner-02.csv
```

Combined report:

```bat
zocdoc-collector --workspace .\workspace runners perf
```

## Scale beyond two browsers

Reinitialize before a benchmark if you want a different runner count:

```bat
zocdoc-collector --workspace .\workspace runners stop
zocdoc-collector --workspace .\workspace runners init --count 3 --base-port 8765
zocdoc-collector --workspace .\workspace runners start
```

Then run the same `multi-listings` command. Increase runner count only while combined pages/min improves and timeout/blocked rates remain acceptable.

## Stop

```bat
zocdoc-collector --workspace .\workspace runners stop
```

## Chrome executable

Chrome is auto-detected on common Windows/macOS/Linux paths. If needed:

```bat
zocdoc-collector --workspace .\workspace runners init ^
  --count 2 ^
  --chrome-path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

or pass `--chrome-path` to `runners start`.

## Extension fallback

The runner launcher passes Chrome the bundled unpacked extension automatically. If a local Chrome build blocks command-line extension loading, load the extension once in each persistent runner profile. The profiles are reused on future starts, so this is only a fallback setup step.

## Current scope

Multi-runner mode is enabled for the high-volume listing layer. Provider-profile collection remains single-runner in this milestone. PostgreSQL remains the production target for future daily reconciliation and multi-machine scaling.
