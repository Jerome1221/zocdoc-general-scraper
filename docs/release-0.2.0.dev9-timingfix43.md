# Dev9.4.3 timing metrics compatibility fix

This hotfix preserves the Dev9.4.2 profile-capture changes and fixes timing summaries for existing workspaces.

## Fix

Dev9.4.2 added timing fields to `capture_performance*.csv` and `crawl_run_metrics*.csv`. If those files already existed from an older Dev9 build, new timing rows could be appended underneath the legacy header. Throughput still worked, but `multi-listings` / `multi-profiles` could not read the timing columns and therefore omitted `Avg stage timing/...` from the terminal summary.

Dev9.4.3 automatically upgrades legacy metrics CSV headers before appending. Existing legacy rows are retained; expanded Dev9.4.2 rows are recovered when possible.

The summary now also includes `server-save` when available.
