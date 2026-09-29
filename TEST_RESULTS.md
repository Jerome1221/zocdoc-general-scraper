# Dev9.6 daily updater test results

Test environment: Windows, Anaconda base Python, isolated editable install.

## Passed

- `zocdoc-collector updater --help` lists the complete and legacy updater commands.
- Full automated suite: `58 passed`.
- A two-page daily run captured both pages, committed two providers, and recorded two additions.
- A later complete one-page run retired the stale page and soft-deactivated the missing provider.
- A timed-out pagination page marked the location failed and left every prior provider membership active.
- A resumed profile job stored canonical profile JSON, a semantic data hash, and canonical profile HTML.
- Offline bootstrap seeded canonical memberships from existing durable trace rows without browser requests.
- A fake PostgreSQL connection verified schema creation, canonical upsert batches, event publication, and transaction commit.
- Saver HTTP tests verify tokenized temporary captures for both listing and profile URLs.
- Legacy snapshot/check/queue and all pre-existing collector tests continue to pass.
- SQLite schema initialization and additive migration completed successfully.
- The supplied source built into a valid wheel, installed in a clean package-local venv, and launched the new CLI.
- A 733 MB existing collector database was opened read-only, passed `PRAGMA quick_check`, and exposed the page/profile columns expected by the additive migration.

## Host limitation

The native managed-Chrome launch remains unavailable in this Codex execution host because the browser parent process is denied while starting its child process (`0xC0000022`) before the extension controller can poll. The saver and complete HTTP capture contract are verified, but real browser navigation must still be certified from a normal Anaconda Prompt:

```bat
zocdoc-collector --workspace TEST updater daily --live --from-snapshots --limit 5 --removal-confirmations 1
```

The automated tests exercise target selection, pagination discovery, retries, staging, validation, durable trace ingestion, provider diffs, guarded removals, profile refresh and parsing, run history, locking, saver endpoints, and cleanup. They do not substitute for the outstanding native Chrome verification.
