# Release 0.2.0.dev3 — Milestone 2.5 Hotfix

Hotfix for the Milestone 2.5 specialty landing-page collector.

- Removed a stale pre-rolling-pool polling block from `collect_specialty_landing_batch`.
- Fixes Ruff `F821` errors for undefined `remaining` and `processed`.
- No intended behavior change beyond removing unreachable/dead legacy code.
