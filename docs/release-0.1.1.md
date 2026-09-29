# 0.1.1

Adds ZIP-input generation from collected canonical provider-location data.

- Adds `openpyxl` runtime dependency.
- Adds `zocdoc-ortho ... zip-input` for standalone regeneration.
- Default `export`/`derive` now write:
  - `zocdoc_scraped_zipcodes_by_state.xlsx`
  - `zocdoc_scraped_zipcodes_long.csv`
  - `zocdoc_scraped_zipcode_conflicts.csv`
- The Excel workbook uses the compatibility sheet name `UC Extended Zipcodes` and preserves leading-zero ZIPs as text.
- ZIPs come from canonical `state` + `postal_code`, not search provenance (`source_zip`).
