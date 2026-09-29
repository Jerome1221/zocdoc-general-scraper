# Release 0.2.0.dev4

Milestone 2.5 performance hotfix.

- Fix `PerformanceRecorder.finish()` on very short runs where the monotonic wall-clock interval can round to zero on Windows.
- Throughput now uses at least the longest recorded non-existing page duration as its timing window.
- Real collection runs continue to use actual wall-clock elapsed time whenever it is longer.
- Keeps the rolling worker pool, tunable capture timings, performance reports, and provider-set hashing from dev2/dev3.
