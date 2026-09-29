# v0.2.0.dev7 — Multi-runner controller-port hotfix

This release fixes multi-browser startup when runner state from an earlier configuration is still present.

## Changes

- Each Chrome runner launch URL is now constructed directly from that runner's assigned host/port.
- Controller pages carry `runner_id` and `expected_port` so the extension can self-correct a mismatched local controller port.
- Runner state now has a launch-version marker; older dev6 state is not silently reused.
- Added `runners start --restart` to force-close/relaunch all configured saver and Chrome sessions.
- Added tests verifying 8765/8766/8767 launch independently and stale state is replaced.
- Retains dev6 zero-link validation/retry behavior and dev5 multi-runner queue/performance features.
