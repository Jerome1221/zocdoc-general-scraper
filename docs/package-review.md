# Package review: notebook workflow -> reusable package

This repository is a refactor of the working collector, not a rewrite of the data contract.

## Preserved behavior

- SQLite checkpoint/resume state.
- Separate base-location and pagination queues.
- Normal Chrome + local extension HTML capture.
- Restriction detection and stop flag.
- Offline reconciliation of already-saved HTML.
- Raw non-deduplicated listing/provider occurrence trace.
- One-profile-per-unique-doctor queue.
- One-doctor smoke test before bulk profile collection.
- Structured `window.__REDUX_STATE__` parsing with DOM fallbacks.
- Canonical 35-field `final_providers.csv`.
- `can_have_appointments` resolution from listing `canHaveAppointments`, profile `isBookable`, and explicit preview/marketplace evidence.
- Telemedicine normalization from provider, location, and doctor-card evidence.
- Derived unique-doctor, virtual-only, and physical-only datasets.

## Engineering changes

- Notebook state was moved into importable modules with a console CLI.
- Runtime files are isolated under a gitignored workspace.
- Chrome extension files are bundled as Python package data and discoverable with `zocdoc-ortho extension-path`.
- The saver verifies that the CLI and server are using the same workspace.
- Legacy SQLite migration uses SQLite's backup API so committed WAL state is preserved.
- Queue commands never automatically cross from listing collection into profile collection.
- Export is complete-by-default; partial output requires an explicit flag.
- Missing booleans remain unknown instead of silently becoming `False`.
- Tests cover the high-risk parsing and output-grain rules.
- GitHub Actions CI is included for pytest + Ruff.

## Local validation performed for this release

- Python compilation completed successfully.
- 5/5 included pytest tests passed.
- The package imported the supplied legacy collector DB and reported:
  - 460 base locations saved,
  - 1,306 pagination pages saved,
  - 2,282 unique doctor profiles queued,
  - 75,417 raw listing appearances,
  - 2,282 unique doctors in the raw trace.
- The derived-output code was run against the supplied `final_providers.csv` and produced:
  - 5,699 canonical provider-location rows,
  - 2,281 unique doctors,
  - 277 virtual-location rows,
  - 5,369 physical-location rows,
  - 53 rows with unknown virtual/physical status.
- A wheel was built and installed into a clean target directory; the bundled Chrome extension was present and discoverable.
