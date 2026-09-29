# Seed data

`zocdoc_locations.csv` is the location-page seed catalog carried forward from the existing collector package. It is version-controlled input data, not runtime output.

The collector only requires a `url` column; `state_group` and `location` are used for reporting and queue ordering.

Run:

```bat
zocdoc-ortho --workspace .\workspace seed .\data\zocdoc_locations.csv
```
