from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from .listing import _clean_text, _container_location, _provider_profile_url
from .redux import extract_redux_state
from .schema import REQUIRED_COLUMNS
from .urls import normalize_url, profile_path
from .utils import first_nonempty, nonempty, row_get, tri_bool, walk_dicts


def profile_provider_from_redux(redux_state: dict | None, profile_url: str) -> dict:
    if not redux_state:
        return {}
    target = profile_path(profile_url)
    candidate_paths = [
        ("profile", "data", "provider"),
        ("profile", "provider"),
        ("provider",),
    ]
    for path in candidate_paths:
        node: Any = redux_state
        ok = True
        for key in path:
            if not isinstance(node, dict) or key not in node:
                ok = False
                break
            node = node[key]
        if ok and isinstance(node, dict):
            node_url = _provider_profile_url(node)
            if (not node_url or profile_path(node_url) == target) and any(
                key in node for key in ("id", "npi", "monolithId", "approvedFullName")
            ):
                return node

    scored: list[tuple[int, dict]] = []
    for obj in walk_dicts(redux_state):
        if profile_path(_provider_profile_url(obj)) != target:
            continue
        score = sum(
            key in obj
            for key in (
                "id",
                "monolithId",
                "npi",
                "approvedFullName",
                "firstName",
                "lastName",
                "averageRating",
                "reviewCount",
                "statement",
                "approvedLocations",
                "relevantSpecialty",
                "acceptsNewPatients",
            )
        )
        scored.append((score, obj))
    if not scored:
        return {}
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def profile_location_key_map(redux_state: dict | None, provider_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not redux_state or not provider_id:
        return out
    for obj in walk_dicts(redux_state):
        provider = obj.get("provider") if isinstance(obj, dict) else None
        if not isinstance(provider, dict) or str(provider.get("id") or "") != str(provider_id):
            continue
        composite = obj.get("id")
        if not isinstance(composite, str) or "|" not in composite:
            continue
        location = _container_location(obj)
        location_id = str(location.get("id") or "") if isinstance(location, dict) else ""
        if location_id:
            out[location_id] = composite
    return out


def normalize_photo_url(value: Any) -> str:
    text = str(value or "").strip()
    return "https:" + text if text.startswith("//") else text


def normalize_affiliation_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        value = [] if not nonempty(value) else [value]
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = first_nonempty(
                item.get("name"),
                item.get("displayName"),
                item.get("hospitalName"),
                item.get("title"),
            )
            if nonempty(name):
                out.append(str(name))
        elif nonempty(item):
            out.append(str(item))
    return list(dict.fromkeys(out))


def normalize_profile_location(location: dict) -> dict | None:
    if not isinstance(location, dict):
        return None
    practice = location.get("practice") if isinstance(location.get("practice"), dict) else {}
    normalized = {
        "location_id": str(first_nonempty(location.get("id"), location.get("locationId"), location.get("location_id")) or ""),
        "location_name": str(first_nonempty(location.get("name"), location.get("locationName"), location.get("location_name")) or ""),
        "address_line_1": str(first_nonempty(location.get("address1"), location.get("addressLine1"), location.get("streetAddress")) or ""),
        "address_line_2": str(first_nonempty(location.get("address2"), location.get("addressLine2")) or ""),
        "city": str(first_nonempty(location.get("city"), location.get("addressLocality")) or ""),
        "state": str(first_nonempty(location.get("state"), location.get("addressRegion")) or ""),
        "postal_code": str(first_nonempty(location.get("zipCode"), location.get("postalCode"), location.get("zip")) or ""),
        "phone": str(first_nonempty(location.get("phone"), location.get("telephone"), location.get("phoneNumber")) or ""),
        "is_virtual_location": tri_bool(first_nonempty(location.get("isVirtual"), location.get("isVirtualLocation"))),
        "can_have_appointments": tri_bool(first_nonempty(location.get("canHaveAppointments"), location.get("can_have_appointments"))),
        "has_new_patient_availability": tri_bool(first_nonempty(location.get("hasNewPatientAvailability"), location.get("has_new_patient_availability"))),
        "virtual_visit_type": str(first_nonempty(location.get("virtualVisitType"), location.get("virtual_visit_type")) or ""),
        "practice_name": str(first_nonempty(practice.get("name"), location.get("practiceName"), location.get("practice_name")) or ""),
    }
    if not any(
        normalized[key]
        for key in ("location_id", "location_name", "address_line_1", "city", "state", "postal_code")
    ):
        return None
    return normalized


def fallback_profile_location_from_dom(soup: BeautifulSoup) -> dict | None:
    street = _clean_text(
        soup.select_one(
            '[data-test="doctor-card-bottom-section"] [itemprop="streetAddress"], '
            '[itemprop="address"] [itemprop="streetAddress"]'
        )
    )
    city = _clean_text(soup.select_one('[itemprop="addressLocality"]'))
    state = _clean_text(soup.select_one('[itemprop="addressRegion"]'))
    postal = _clean_text(soup.select_one('[itemprop="postalCode"]'))

    if not all((city, state, postal)):
        body_text = " ".join(soup.stripped_strings)
        match = re.search(r"\b([A-Za-z][A-Za-z .'\-]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b", body_text)
        if match:
            city = city or match.group(1).strip()
            state = state or match.group(2)
            postal = postal or match.group(3)

    if not any((street, city, state, postal)):
        return None
    return {
        "location_id": "",
        "location_name": "",
        "address_line_1": street,
        "address_line_2": "",
        "city": city,
        "state": state,
        "postal_code": postal,
        "phone": "",
        "is_virtual_location": None,
        "can_have_appointments": None,
        "has_new_patient_availability": None,
        "virtual_visit_type": "",
        "practice_name": "",
    }


def split_provider_name(full_name: str) -> dict[str, str]:
    text = str(full_name or "").strip()
    prenominal = ""
    match = re.match(r"^(Dr\.|Mr\.|Ms\.|Mrs\.)\s+", text, flags=re.IGNORECASE)
    if match:
        prenominal = match.group(1)
        text = text[match.end() :]
    name_part, *credentials = text.split(",")
    tokens = name_part.strip().split()
    return {
        "first_name": tokens[0] if tokens else "",
        "last_name": tokens[-1] if len(tokens) > 1 else "",
        "prenominal": prenominal,
        "postnominal": ",".join(credentials).strip(),
    }


def _trace_booleans(trace_rows: list[dict], keys: tuple[str, ...]) -> list[bool]:
    out: list[bool] = []
    for trace_row in trace_rows or []:
        for key in keys:
            value = tri_bool(row_get(trace_row, key))
            if value is not None:
                out.append(value)
                break
    return out


def resolve_can_have_appointments(provider: dict, trace_rows: list[dict]) -> tuple[bool | None, dict]:
    listing_values = _trace_booleans(
        trace_rows,
        (
            "structured_can_have_appointments",
            "listing_can_have_appointments",
            "can_have_appointments",
            "canHaveAppointments",
        ),
    )
    unique_listing = set(listing_values)
    listing_conflict = len(unique_listing) > 1
    listing_value = next(iter(unique_listing)) if len(unique_listing) == 1 else None

    profile_is_bookable = tri_bool(provider.get("isBookable"))
    profile_is_preview = tri_bool(provider.get("isPreview"))
    marketplace_enabled = tri_bool(provider.get("isEnabledForMarketplace"))

    if listing_value is not None:
        value, source = listing_value, "listing.canHaveAppointments"
    elif profile_is_bookable is not None:
        value = profile_is_bookable
        source = "profile.isBookable_after_listing_conflict" if listing_conflict else "profile.isBookable"
    elif profile_is_preview is True:
        value, source = False, "profile.isPreview"
    elif marketplace_enabled is False:
        value, source = False, "profile.isEnabledForMarketplace"
    else:
        value, source = None, "unresolved"

    return value, {
        "value": value,
        "source": source,
        "listing_values": listing_values,
        "listing_conflict": listing_conflict,
        "profile_is_bookable": profile_is_bookable,
        "profile_is_preview": profile_is_preview,
        "marketplace_enabled": marketplace_enabled,
    }


def telemedicine_from_evidence(provider: dict, locations: list[dict], trace_rows: list[dict]) -> tuple[bool | None, str]:
    positive: list[str] = []
    negative: list[str] = []
    offers = tri_bool(provider.get("offersTelemedicine"))
    has_virtual = tri_bool(provider.get("hasVirtualLocations"))

    if offers is True:
        positive.append("profile.offersTelemedicine=true")
    elif offers is False:
        negative.append("profile.offersTelemedicine=false")

    if has_virtual is True:
        positive.append("profile.hasVirtualLocations=true")
    elif has_virtual is False:
        negative.append("profile.hasVirtualLocations=false")

    if any(location.get("is_virtual_location") is True for location in locations):
        positive.append("profile.location.isVirtual=true")
    for location in locations:
        visit_type = str(location.get("virtual_visit_type") or "").strip().lower()
        if visit_type and visit_type != "none":
            positive.append("profile.location.virtualVisitType")

    if any(tri_bool(row_get(row, "listing_offers_video_visits")) is True for row in trace_rows):
        positive.append("listing.Also offers video visits")
    if any(tri_bool(row_get(row, "structured_offers_telemedicine")) is True for row in trace_rows):
        positive.append("listing.provider.offersTelemedicine=true")
    if any(tri_bool(row_get(row, "structured_has_virtual_locations")) is True for row in trace_rows):
        positive.append("listing.provider.hasVirtualLocations=true")

    if positive:
        return True, "; ".join(dict.fromkeys(positive))
    if has_virtual is False or offers is False:
        return False, "; ".join(dict.fromkeys(negative))
    return None, ""


def accepts_new_patients_from_evidence(provider: dict, trace_rows: list[dict]) -> bool | None:
    direct = tri_bool(provider.get("acceptsNewPatients"))
    if direct is not None:
        return direct
    if any(tri_bool(row_get(row, "structured_accepts_new_patients")) is True for row in trace_rows):
        return True
    if any(tri_bool(row_get(row, "listing_accepts_new_patients")) is True for row in trace_rows):
        return True
    return None


def _trace_location_key_maps(trace_rows: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    by_location_id: dict[str, str] = {}
    by_zip: dict[str, str] = {}
    for trace in trace_rows:
        composite = str(row_get(trace, "provider_location_key") or "")
        if not composite:
            continue
        if "|" in composite:
            by_location_id[composite.split("|", 1)[1]] = composite
        postal = str(row_get(trace, "postal_code") or "")
        if postal:
            by_zip.setdefault(postal, composite)
    return by_location_id, by_zip


def _trace_new_availability(trace_rows: list[dict]) -> bool | None:
    values = _trace_booleans(trace_rows, ("structured_has_new_patient_availability",))
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 else None


def parse_profile_rows(
    profile_url: str,
    html: str,
    trace_rows: list[dict] | None = None,
    source_zip_mode: str = "office",
) -> tuple[list[dict], dict]:
    """Parse one saved profile HTML into the canonical provider-location rows."""
    trace_rows = trace_rows or []
    profile_url = normalize_url(profile_url)
    soup = BeautifulSoup(html, "html.parser")
    redux_state = extract_redux_state(html)
    provider = profile_provider_from_redux(redux_state, profile_url)

    full_name = str(
        first_nonempty(
            provider.get("approvedFullName"),
            provider.get("fullName"),
            provider.get("displayName"),
            _clean_text(soup.select_one('[data-test="provider-name"]')),
            _clean_text(soup.select_one("h1")),
        )
        or ""
    )
    split_name = split_provider_name(full_name)
    first_name = str(first_nonempty(provider.get("firstName"), split_name["first_name"]) or "")
    last_name = str(first_nonempty(provider.get("lastName"), split_name["last_name"]) or "")
    prenominal = str(first_nonempty(provider.get("prenominal"), split_name["prenominal"]) or "")
    postnominal = str(first_nonempty(provider.get("postnominal"), split_name["postnominal"]) or "")

    relevant_specialty = provider.get("relevantSpecialty") if isinstance(provider.get("relevantSpecialty"), dict) else {}
    specialty_name = str(
        first_nonempty(
            relevant_specialty.get("name"),
            provider.get("specialtyName"),
            _clean_text(soup.select_one('[data-test="provider-specialty"]')),
        )
        or ""
    )

    provider_id = str(provider.get("id") or "")
    monolith_id = str(provider.get("monolithId") or "")
    npi = str(provider.get("npi") or "")
    average_rating = first_nonempty(
        provider.get("averageRating"),
        _clean_text(soup.select_one('[data-test="doctor-card-info-rating-number"]')),
    )
    review_count = provider.get("reviewCount")
    if review_count is None:
        review_text = " ".join(soup.stripped_strings)
        match = re.search(r"(?:See all|Read)\s+([\d,]+)\s+reviews?", review_text, flags=re.IGNORECASE)
        review_count = match.group(1).replace(",", "") if match else ""

    accepts_new_patients = accepts_new_patients_from_evidence(provider, trace_rows)
    only_sees_children = tri_bool(provider.get("onlySeesChildren"))
    hospital_affiliations = normalize_affiliation_names(provider.get("hospitalAffiliations"))
    bio = str(first_nonempty(provider.get("statement"), provider.get("bio"), provider.get("biography")) or "")
    photo_url = normalize_photo_url(
        first_nonempty(
            provider.get("frontEndSquarePictureUrl"),
            provider.get("frontEndCirclePictureUrl"),
            provider.get("squarePictureUrl"),
            provider.get("circlePictureUrl"),
        )
    )
    if not photo_url:
        og = soup.select_one('meta[property="og:image"]')
        photo_url = normalize_photo_url(og.get("content", "") if og else "")

    raw_locations = provider.get("approvedLocations")
    raw_locations = raw_locations if isinstance(raw_locations, list) else []
    locations: list[dict] = []
    seen_locations: set[tuple] = set()
    for raw in raw_locations:
        location = normalize_profile_location(raw)
        if not location:
            continue
        key = (
            location["location_id"],
            location["address_line_1"].lower(),
            location["city"].lower(),
            location["state"].lower(),
            location["postal_code"],
        )
        if key in seen_locations:
            continue
        seen_locations.add(key)
        locations.append(location)

    if not locations:
        fallback = fallback_profile_location_from_dom(soup)
        if fallback:
            locations = [fallback]

    if not locations:
        fallback_state = next((str(row_get(row, "state") or "") for row in trace_rows if row_get(row, "state")), "")
        fallback_zip = next((str(row_get(row, "postal_code") or "") for row in trace_rows if row_get(row, "postal_code")), "")
        locations = [
            {
                "location_id": "",
                "location_name": "",
                "address_line_1": "",
                "address_line_2": "",
                "city": "",
                "state": fallback_state,
                "postal_code": fallback_zip,
                "phone": "",
                "is_virtual_location": None,
                "can_have_appointments": None,
                "has_new_patient_availability": None,
                "virtual_visit_type": "",
                "practice_name": "",
            }
        ]

    practice_obj = provider.get("practice") if isinstance(provider.get("practice"), dict) else {}
    provider_practice_name = str(
        first_nonempty(
            practice_obj.get("name"),
            provider.get("practiceName"),
            next((loc.get("practice_name") for loc in locations if loc.get("practice_name")), ""),
        )
        or ""
    )

    provider_new_avail = tri_bool(provider.get("hasNewPatientAvailability"))
    if provider_new_avail is None:
        provider_new_avail = _trace_new_availability(trace_rows)
    resolved_can_have, booking_diag = resolve_can_have_appointments(provider, trace_rows)
    profile_key_map = profile_location_key_map(redux_state, provider_id)
    trace_key_by_loc_id, trace_key_by_zip = _trace_location_key_maps(trace_rows)
    offers_telemedicine, telemedicine_evidence = telemedicine_from_evidence(provider, locations, trace_rows)

    serialized_locations: list[dict] = []
    for location in locations:
        location_id = location.get("location_id", "")
        postal_code = location.get("postal_code", "")
        provider_location_key = first_nonempty(
            profile_key_map.get(location_id, ""),
            trace_key_by_loc_id.get(location_id, ""),
            trace_key_by_zip.get(postal_code, ""),
        )
        if not provider_location_key and provider_id and location_id:
            provider_location_key = f"{provider_id}|{location_id}"
        copy = dict(location)
        copy["provider_location_key"] = provider_location_key
        serialized_locations.append(copy)

    locations_json = json.dumps(serialized_locations, ensure_ascii=False)
    rows: list[dict] = []
    for location in serialized_locations:
        can_have = location.get("can_have_appointments")
        if can_have is None:
            can_have = resolved_can_have
        new_avail = location.get("has_new_patient_availability")
        if new_avail is None:
            new_avail = provider_new_avail
        practice_name = first_nonempty(location.get("practice_name"), provider_practice_name)
        state = str(location.get("state") or "")
        postal_code = str(location.get("postal_code") or "")

        if source_zip_mode == "office":
            source_zip = postal_code
            source_zip_state = state
        elif source_zip_mode == "blank":
            source_zip = ""
            source_zip_state = ""
        else:
            raise ValueError("source_zip_mode must be 'office' or 'blank'")

        row = {
            "state": state,
            "full_name": full_name,
            "specialty_name": specialty_name,
            "practice_name": practice_name,
            "npi": npi,
            "provider_id": provider_id,
            "monolith_id": monolith_id,
            "first_name": first_name,
            "last_name": last_name,
            "prenominal": prenominal,
            "postnominal": postnominal,
            "average_rating": average_rating,
            "review_count": review_count,
            "profile_url": profile_url,
            "accepts_new_patients": accepts_new_patients,
            "offers_telemedicine": offers_telemedicine,
            "only_sees_children": only_sees_children,
            "hospital_affiliations": json.dumps(hospital_affiliations, ensure_ascii=False),
            "bio": bio,
            "num_locations": len(serialized_locations),
            "address_line_1": location.get("address_line_1", ""),
            "address_line_2": location.get("address_line_2", ""),
            "city": location.get("city", ""),
            "postal_code": postal_code,
            "phone": location.get("phone", ""),
            "location_id": location.get("location_id", ""),
            "location_name": location.get("location_name", ""),
            "can_have_appointments": can_have,
            "is_virtual_location": location.get("is_virtual_location"),
            "has_new_patient_availability": new_avail,
            "photo_url": photo_url,
            "provider_location_key": location.get("provider_location_key", ""),
            "locations": locations_json,
            "source_zip": source_zip,
            "source_zip_state": source_zip_state,
        }
        rows.append({column: row.get(column, "") for column in REQUIRED_COLUMNS})

    diagnostics = {
        "redux_found": redux_state is not None,
        "provider_found": bool(provider),
        "trace_occurrences": len(trace_rows),
        "telemedicine_evidence": telemedicine_evidence,
        "can_have_appointments": resolved_can_have,
        "can_have_appointments_source": booking_diag["source"],
        "listing_can_have_values": booking_diag["listing_values"],
        "listing_can_have_conflict": booking_diag["listing_conflict"],
        "profile_is_bookable": booking_diag["profile_is_bookable"],
        "profile_is_preview": booking_diag["profile_is_preview"],
        "marketplace_enabled": booking_diag["marketplace_enabled"],
    }
    return rows, diagnostics
