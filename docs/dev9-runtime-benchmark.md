# Dev9 specialty runtime benchmark

Use one specialty filter at a time. Timers are local only and do not add browser activity.

```bat
zocdoc-collector --workspace .\workspace_test discover-targets --collect --specialty dentists --limit 1
zocdoc-collector --workspace .\workspace_test multi-listings --specialty dentists --phase seed --limit 50
zocdoc-collector --workspace .\workspace_test specialty-runtime --specialty dentists
zocdoc-collector --workspace .\workspace_test multi-listings --specialty dentists --phase pagination --limit 100
zocdoc-collector --workspace .\workspace_test specialty-runtime --specialty dentists
zocdoc-collector --workspace .\workspace_test multi-profiles --specialty dentists --limit 100
zocdoc-collector --workspace .\workspace_test specialty-runtime --specialty dentists
```

`specialty-runtime` reports measured elapsed time, throughput, current workload size, and projected full runtime for target discovery, base listings, pagination, and net-new profiles.

`--specialty` on profile collection intentionally limits the benchmark to providers first discovered by that specialty. This prevents an inherited dev8 profile backlog from contaminating the runtime estimate.
