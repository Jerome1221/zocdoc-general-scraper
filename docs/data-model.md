# Data model

## Canonical provider-location schema

`final_providers.csv` contains 35 fields, in this order:

1. `state`
2. `full_name`
3. `specialty_name`
4. `practice_name`
5. `npi`
6. `provider_id`
7. `monolith_id`
8. `first_name`
9. `last_name`
10. `prenominal`
11. `postnominal`
12. `average_rating`
13. `review_count`
14. `profile_url`
15. `accepts_new_patients`
16. `offers_telemedicine`
17. `only_sees_children`
18. `hospital_affiliations`
19. `bio`
20. `num_locations`
21. `address_line_1`
22. `address_line_2`
23. `city`
24. `postal_code`
25. `phone`
26. `location_id`
27. `location_name`
28. `can_have_appointments`
29. `is_virtual_location`
30. `has_new_patient_availability`
31. `photo_url`
32. `provider_location_key`
33. `locations`
34. `source_zip`
35. `source_zip_state`

## Important field semantics

### `accepts_new_patients`

Priority:

1. Direct profile field `provider.acceptsNewPatients`.
2. Structured listing field when available.
3. Positive provider-card marker (`accept-new-patient`) as a positive fallback.
4. Otherwise unknown (`None`).

Current appointment slots are **not** used to infer this field.

### `only_sees_children`

Direct profile field `provider.onlySeesChildren`. Missing stays unknown.

### `offers_telemedicine`

Normalized from provider-specific evidence. Positive evidence includes:

- `provider.offersTelemedicine == true`
- `provider.hasVirtualLocations == true`
- an approved location with `isVirtual == true`
- a non-`None` `virtualVisitType`
- the doctor card marker `Also offers video visits`

An online-booking FAQ is not treated as telemedicine evidence.

### `can_have_appointments`

This means the provider can participate in appointment booking on Zocdoc; it is not the same as current slot availability.

Priority:

1. Direct location `canHaveAppointments` when exposed.
2. Listing-provider `canHaveAppointments` when listing snapshots agree.
3. Profile `isBookable`.
4. Explicit preview/non-marketplace evidence can resolve to `False`.
5. Otherwise unknown.

`has_new_patient_availability` and `accepts_new_patients` are not substitutes for this field.

### `is_virtual_location`

Direct approved-location `isVirtual` / `isVirtualLocation` flag.

A provider can have a physical and a virtual Zocdoc location object with the same displayed street address. `final_providers.csv` keeps those separate because its grain is provider x Zocdoc location object.

### `locations`

JSON list containing all normalized approved location objects for the provider. It preserves:

- location ID/name
- address and ZIP
- phone
- virtual flag and visit type
- location practice name
- provider-location composite key

### `source_zip` / `source_zip_state`

Default package behavior matches the current pipeline: these fields mirror the actual provider-office `postal_code` / `state` for each output row. Pass `--source-zip-mode blank` if you do not want that convention.

## Derived datasets

### `unique_doctors.csv`

One row per provider identity, preferring `provider_id`, then NPI, then profile URL. Location-level top-level columns are dropped; `locations` is retained.

### `doctor_locations_virtual.csv`

Canonical rows with explicit `is_virtual_location == True`.

### `doctor_locations_physical.csv`

Canonical rows with explicit `is_virtual_location == False`.

## ZIP input derivation

The default export also derives inputs for the original ZIP/state scraper from the canonical provider-location fields `state` and `postal_code`.

- `zocdoc_scraped_zipcodes_by_state.xlsx` uses one state/area column per code on the `UC Extended Zipcodes` sheet.
- `zocdoc_scraped_zipcodes_long.csv` stores the same unique state/ZIP pairs in long format.
- `zocdoc_scraped_zipcode_conflicts.csv` records ZIPs that appear under more than one state/area code for QA.

The derivation does not use `source_zip`; it represents office/location ZIPs observed in collected provider records. ZIPs are normalized to five-digit strings and leading zeroes are preserved.
