# Dev9.3 trace alignment

- Durable listing trace now supplements from the exact validated provider list returned by `parse_page()`.
- Fixes pagination captures where validation found provider links but the trace writer persisted zero rows.
- Adds `repair-trace-partials` to repair retained `review / trace_partial` HTML completely offline.
- A repaired page returns to `saved / valid_repaired` only after every validated provider URL is present in durable trace.
- Raw HTML is retained by default during repair; pass `--delete-html` only when you want repaired HTML removed after verification.
