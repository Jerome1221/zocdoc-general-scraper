# Dev9 Safe Runner Patch

Package version: `0.2.0.dev9+saferunner1`

This patch keeps the Dev9 inline-trace/runtime features and hardens managed runners for side-by-side Dev8/Dev9 use.

## Changes

- Before terminating a saved PID, the runner inspects the live process command line and verifies it belongs to the current workspace/runner/port.
- A live PID that cannot be verified is left running and reported as a safety warning.
- Windows Chrome processes are launched without `DETACHED_PROCESS` so the GUI window remains visible/reliable. Saver processes remain detached.
- `runners start` now prints Chrome state in addition to saver/controller state.
- Unhealthy runner output points directly to saver/Chrome logs.
- `runners stop` uses the same ownership checks and will not blindly kill a stale/reused PID.

## Side-by-side recommendation

Keep workspaces and ports separate, for example:

- Dev8: existing workspace, ports 8765-8767
- Dev9: separate workspace, ports 8770-8772

Do not share `collector.db`, `.runners`, or Chrome profile folders while validating Dev9.
