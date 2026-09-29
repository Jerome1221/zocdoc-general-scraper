# Dev9.4.2 — profile capture + timing

This hotfix keeps the Dev9.4.1 listing/trace/profile identity behavior and adds two focused changes.

## Profile capture alignment

The Chrome extension now uses the same broad two-segment provider-profile rule as Python. This includes provider URLs such as:

- `/dentist/benjamin-crunk-dds`
- `/dentist/christopher-swicord-dmd-788006`
- `/doctor/1114140902-timothy-nettles-dmd`
- `/professional/...`

Known non-profile routes and listing slugs ending in `<id>pm` remain excluded.

Profile pages that display **Claim your profile** count as a meaningful ready state. They are captured and the tab closes after the local saver confirms a successful save. They are still parsed/QA'd normally at export time; saving them does not force them into the canonical output if parsing fails.

## Timing instrumentation

Collection now records and prints stage timing where available:

- navigation/load to content-script readiness
- stabilization wait
- HTML serialization
- disk write
- listing validation/parser
- listing DB update
- durable trace write
- profile-queue synchronization
- cleanup/output work
- profile queue DB marking
- end-to-end page/profile duration

`multi-listings` and `multi-profiles` print average stage timing in their final summary. Per-page detail remains in each runner collection log and saver log.

`export` additionally reports average/p95 profile parse time, HTML-read time, trace-query time, parser time, output-write time, derived-output time, and total export time.

No change is made to profile HTML retention. Profile HTML remains available for later export/re-export and can be cleaned in a future explicit cleanup step.
