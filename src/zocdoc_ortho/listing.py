from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .outputs import export_page_outputs
from .redux import extract_redux_state
from .urls import (
    absolute_zocdoc_url,
    base_location_url,
    normalize_url,
    page_number,
    profile_path,
    is_profile_url,
)
from .utils import now_iso, tri_bool, walk_dicts
from .workspace import Workspace


ZERO_LINK_MAX_RETRIES = 2

_EMPTY_RESULT_PATTERNS = (
    "no providers found",
    "no results found",
    "we couldn't find any",
    "we could not find any",
    "there are no providers",
    "0 verified providers",
)

_RESTRICTION_PATTERNS = (
    "verify you are human",
    "captcha",
    "access denied",
    "unusual traffic",
    "security check",
    "just a moment",
)


def classify_listing_capture(html: str, parsed: dict) -> tuple[str, str]:
    """Classify a rendered listing capture before declaring it complete.

    Returns (classification, reason), where classification is one of:
    valid, valid_empty, retry_zero, restriction_like.
    """
    if parsed.get("doctors"):
        return "valid", "provider links found"

    if parsed.get("advertised_total") == 0:
        return "valid_empty", "advertised provider total is explicitly zero"

    text = " ".join(BeautifulSoup(html, "html.parser").stripped_strings).lower()
    text = " ".join(text.split())
    for marker in _EMPTY_RESULT_PATTERNS:
        if marker in text:
            return "valid_empty", f"explicit empty-results marker: {marker}"
    for marker in _RESTRICTION_PATTERNS:
        if marker in text:
            return "restriction_like", f"restriction-like marker: {marker}"
    return "retry_zero", "capture contains zero provider links without an explicit empty-results marker"


class ZocdocPageParser(HTMLParser):
    """Lightweight listing parser used while pages are being ingested."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.base_url = base_location_url(page_url)
        self.doctors: list[dict[str, str]] = []
        self.pagination_urls: set[str] = set()
        self.advertised_total: int | None = None
        self._doctor_href: str | None = None
        self._doctor_text: list[str] = []
        self._in_pagination = 0
        self._criteria_depth = 0
        self._criteria_text: list[str] = []

    @staticmethod
    def _attrs(attrs):
        return {k: (v or "") for k, v in attrs}

    def handle_starttag(self, tag, attrs):
        attrs = self._attrs(attrs)

        if attrs.get("data-test") == "selection-criteria-header":
            self._criteria_depth = 1
            self._criteria_text = []
        elif self._criteria_depth:
            self._criteria_depth += 1

        if tag == "nav" and attrs.get("data-test") == "search-results-pagination":
            self._in_pagination = 1
        elif self._in_pagination:
            self._in_pagination += 1

        if tag == "a" and attrs.get("data-test") == "doctor-card-info-name":
            href = attrs.get("href", "").strip()
            if href:
                self._doctor_href = normalize_url(urljoin(self.page_url, href))
                self._doctor_text = []

        if self._in_pagination and tag == "a":
            href = attrs.get("href", "").strip()
            if href:
                absolute = normalize_url(urljoin(self.page_url, href))
                if absolute.startswith(self.base_url):
                    self.pagination_urls.add(absolute)

    def handle_endtag(self, tag):
        if tag == "a" and self._doctor_href:
            name = " ".join(" ".join(self._doctor_text).split())
            self.doctors.append({"doctor_url": self._doctor_href, "doctor_name": name})
            self._doctor_href = None
            self._doctor_text = []

        if self._criteria_depth:
            self._criteria_depth -= 1
            if self._criteria_depth == 0:
                text = " ".join(" ".join(self._criteria_text).split())
                match = re.search(r"\b([0-9][0-9,]*)\s+verified\b", text, flags=re.IGNORECASE)
                if match:
                    self.advertised_total = int(match.group(1).replace(",", ""))

        if self._in_pagination:
            self._in_pagination -= 1

    def handle_data(self, data):
        if self._doctor_href:
            self._doctor_text.append(data)
        if self._criteria_depth:
            self._criteria_text.append(data)


def parse_page(html: str, page_url: str) -> dict:
    parser = ZocdocPageParser(page_url)
    parser.feed(html)

    # Supplement the lightweight parser with DOM selectors. This makes the
    # queue resilient to small wrapper/depth changes in the rendered page.
    soup = BeautifulSoup(html, "html.parser")
    discovered_doctors = list(parser.doctors)
    for anchor in soup.select('a[data-test="doctor-card-info-name"][href]'):
        href = anchor.get("href", "")
        if not href:
            continue
        discovered_doctors.append(
            {
                "doctor_url": normalize_url(urljoin(page_url, href)),
                "doctor_name": _clean_text(anchor),
            }
        )

    seen: set[str] = set()
    doctors: list[dict[str, str]] = []
    for doctor in discovered_doctors:
        if doctor["doctor_url"] in seen:
            continue
        seen.add(doctor["doctor_url"])
        doctors.append(doctor)

    pagination_urls = set(parser.pagination_urls)
    for anchor in soup.select('nav[data-test="search-results-pagination"] a[href]'):
        absolute = normalize_url(urljoin(page_url, anchor.get("href", "")))
        if absolute and base_location_url(absolute) == parser.base_url:
            pagination_urls.add(absolute)

    pages = sorted(
        {u for u in pagination_urls if base_location_url(u) == parser.base_url},
        key=lambda u: (page_number(u), u),
    )

    advertised_total = parser.advertised_total
    if advertised_total is None:
        criteria = soup.select_one('[data-test="selection-criteria-header"]')
        if criteria is not None:
            match = re.search(r"\b([0-9][0-9,]*)\s+verified\b", _clean_text(criteria), flags=re.IGNORECASE)
            if match:
                advertised_total = int(match.group(1).replace(",", ""))

    return {
        "doctors": doctors,
        "pagination_urls": pages,
        "advertised_total": advertised_total,
    }


def seed_locations(csv_path: Path, conn: sqlite3.Connection) -> int:
    now = now_iso()
    count = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        if not rows.fieldnames or "url" not in rows.fieldnames:
            raise ValueError("Location CSV must contain a 'url' column")
        for row in rows:
            url = normalize_url((row.get("url") or "").strip())
            if not url:
                continue
            conn.execute(
                """
                INSERT INTO pages (
                    url, base_location_url, state_group, location,
                    page_no, source, status, discovered_at,
                    specialty_url, specialty_name, specialty_slug
                ) VALUES (?, ?, ?, ?, ?, 'seed', 'pending', ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    state_group = CASE WHEN excluded.state_group<>'' THEN excluded.state_group ELSE pages.state_group END,
                    location = CASE WHEN excluded.location<>'' THEN excluded.location ELSE pages.location END,
                    specialty_url = CASE WHEN excluded.specialty_url<>'' THEN excluded.specialty_url ELSE pages.specialty_url END,
                    specialty_name = CASE WHEN excluded.specialty_name<>'' THEN excluded.specialty_name ELSE pages.specialty_name END,
                    specialty_slug = CASE WHEN excluded.specialty_slug<>'' THEN excluded.specialty_slug ELSE pages.specialty_slug END
                """,
                (
                    url,
                    base_location_url(url),
                    (row.get("state_group") or "").strip(),
                    (row.get("location") or "").strip(),
                    page_number(url),
                    now,
                    (row.get("specialty_url") or "").strip(),
                    (row.get("specialty_name") or "").strip(),
                    (row.get("specialty_slug") or "").strip(),
                ),
            )
            count += 1
    conn.commit()
    return count


def ensure_page(conn: sqlite3.Connection, url: str, source: str = "captured"):
    url = normalize_url(url)
    base = base_location_url(url)
    parent = conn.execute(
        """
        SELECT state_group,location,specialty_url,specialty_name,specialty_slug
        FROM pages WHERE base_location_url=? ORDER BY page_no LIMIT 1
        """,
        (base,),
    ).fetchone()
    state_group = parent["state_group"] if parent else ""
    location = parent["location"] if parent else ""
    specialty_url = parent["specialty_url"] if parent else ""
    specialty_name = parent["specialty_name"] if parent else ""
    specialty_slug = parent["specialty_slug"] if parent else ""
    conn.execute(
        """
        INSERT INTO pages (
            url,base_location_url,state_group,location,page_no,source,status,discovered_at,
            specialty_url,specialty_name,specialty_slug
        ) VALUES (?,?,?,?,?,?, 'pending', ?,?,?,?)
        ON CONFLICT(url) DO NOTHING
        """,
        (
            url, base, state_group, location, page_number(url), source, now_iso(),
            specialty_url, specialty_name, specialty_slug,
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()


def process_saved_html(
    conn: sqlite3.Connection,
    workspace: Workspace,
    page_url: str,
    title: str,
    html: str,
    file_name: str,
    byte_count: int,
    *,
    write_outputs: bool = True,
    zero_link_max_retries: int = ZERO_LINK_MAX_RETRIES,
) -> dict:
    timing_started = time.monotonic()
    page_url = normalize_url(page_url)
    row = ensure_page(conn, page_url)
    parse_started = time.monotonic()
    parsed = parse_page(html, page_url)
    classification, validation_reason = classify_listing_capture(html, parsed)
    current_retry_count = int(row["zero_link_retry_count"] or 0)
    retry_count = current_retry_count
    if classification == "valid":
        queue_status = "saved"
        validation_status = "valid"
        retry_count = 0
    elif classification == "valid_empty":
        queue_status = "valid_empty"
        validation_status = "valid_empty"
    else:
        if current_retry_count < max(0, int(zero_link_max_retries)):
            retry_count = current_retry_count + 1
            queue_status = "retry_pending"
            validation_status = classification
            validation_reason = (
                f"{validation_reason}; retry {retry_count}/{max(0, int(zero_link_max_retries))} scheduled for next run"
            )
        else:
            queue_status = "review"
            validation_status = "retry_exhausted"
            validation_reason = (
                f"{validation_reason}; retry limit {max(0, int(zero_link_max_retries))} exhausted"
            )

    parsed["queue_status"] = queue_status
    parsed["validation_status"] = validation_status
    parsed["validation_reason"] = validation_reason
    parsed["zero_link_retry_count"] = retry_count
    provider_urls = sorted({doctor["doctor_url"] for doctor in parsed["doctors"] if doctor.get("doctor_url")})
    provider_set_hash = hashlib.sha256("\n".join(provider_urls).encode("utf-8")).hexdigest()
    parsed["provider_set_hash"] = provider_set_hash
    previous_provider_set_hash = row["provider_set_hash"] or ""
    provider_set_changed = previous_provider_set_hash != provider_set_hash
    now = now_iso()
    state_group = row["state_group"] or ""
    location = row["location"] or ""
    location_url = row["base_location_url"]
    listing_specialty_url = row["specialty_url"] or ""
    listing_specialty_name = row["specialty_name"] or ""
    listing_specialty_slug = row["specialty_slug"] or ""

    parse_validate_seconds = max(0.0, time.monotonic() - parse_started)
    db_started = time.monotonic()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE pages SET status=?,title=?,file=?,bytes=?,
                advertised_total=COALESCE(?,advertised_total),
                doctor_links_found=?,validation_status=?,validation_reason=?,
                zero_link_retry_count=?,provider_set_hash=?,
                last_provider_set_changed_at=CASE WHEN ? THEN ? ELSE last_provider_set_changed_at END,
                saved_at=?,error=?,
                trace_status='',trace_parsed_at=NULL,trace_occurrence_count=0,
                trace_source_file='',trace_html_deleted_at=NULL
            WHERE url=?
            """,
            (
                queue_status,
                title,
                file_name,
                byte_count,
                parsed["advertised_total"],
                len(parsed["doctors"]),
                validation_status,
                validation_reason,
                retry_count,
                provider_set_hash,
                int(provider_set_changed),
                now,
                now,
                "" if queue_status in {"saved", "valid_empty"} else validation_reason,
                page_url,
            ),
        )

        for doctor in parsed["doctors"]:
            conn.execute(
                """
                INSERT INTO doctors (
                    doctor_url,doctor_name,first_seen_page_url,first_seen_location_url,
                    first_seen_state_group,first_seen_location,first_seen_at,last_seen_at,
                    first_seen_specialty_url,first_seen_specialty_name
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(doctor_url) DO UPDATE SET
                    doctor_name=CASE
                        WHEN doctors.doctor_name='' AND excluded.doctor_name<>'' THEN excluded.doctor_name
                        ELSE doctors.doctor_name END,
                    first_seen_specialty_url=CASE
                        WHEN doctors.first_seen_specialty_url='' THEN excluded.first_seen_specialty_url
                        ELSE doctors.first_seen_specialty_url END,
                    first_seen_specialty_name=CASE
                        WHEN doctors.first_seen_specialty_name='' THEN excluded.first_seen_specialty_name
                        ELSE doctors.first_seen_specialty_name END,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    doctor["doctor_url"],
                    doctor["doctor_name"],
                    page_url,
                    location_url,
                    state_group,
                    location,
                    now,
                    now,
                    listing_specialty_url,
                    listing_specialty_name,
                ),
            )
            conn.execute(
                """
                INSERT INTO doctor_sources(
                    doctor_url,page_url,location_url,seen_at,specialty_url,specialty_name
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(doctor_url,page_url) DO UPDATE SET
                    location_url=excluded.location_url,
                    seen_at=excluded.seen_at,
                    specialty_url=CASE WHEN excluded.specialty_url<>'' THEN excluded.specialty_url ELSE doctor_sources.specialty_url END,
                    specialty_name=CASE WHEN excluded.specialty_name<>'' THEN excluded.specialty_name ELSE doctor_sources.specialty_name END
                """,
                (
                    doctor["doctor_url"], page_url, location_url, now,
                    listing_specialty_url, listing_specialty_name,
                ),
            )

        for discovered in parsed["pagination_urls"]:
            if discovered == page_url:
                continue
            conn.execute(
                """
                INSERT INTO pages (
                    url,base_location_url,state_group,location,page_no,source,status,discovered_at,
                    specialty_url,specialty_name,specialty_slug
                ) VALUES (?,?,?,?,?,'pagination','pending',?,?,?,?)
                ON CONFLICT(url) DO NOTHING
                """,
                (
                    discovered,
                    location_url,
                    state_group,
                    location,
                    page_number(discovered),
                    now,
                    listing_specialty_url,
                    listing_specialty_name,
                    listing_specialty_slug,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    db_seconds = max(0.0, time.monotonic() - db_started)
    output_started = time.monotonic()
    if write_outputs:
        export_page_outputs(conn, workspace)
    output_seconds = max(0.0, time.monotonic() - output_started)
    parsed.setdefault("_timings", {}).update(
        {
            "validation_seconds": round(parse_validate_seconds, 4),
            "listing_db_seconds": round(db_seconds, 4),
            "listing_output_seconds": round(output_seconds, 4),
            "process_saved_html_seconds": round(max(0.0, time.monotonic() - timing_started), 4),
        }
    )
    return parsed


def _clean_text(element) -> str:
    if element is None:
        return ""
    return " ".join(element.get_text(" ", strip=True).split())


def _provider_profile_url(provider: dict) -> str:
    return (
        provider.get("profileUrl")
        or provider.get("profile_url")
        or provider.get("url")
        or ""
    ) if isinstance(provider, dict) else ""


def _container_location(container: dict) -> dict:
    if not isinstance(container, dict):
        return {}
    for key in ("location", "approvedLocation", "providerLocation"):
        value = container.get(key)
        if isinstance(value, dict):
            return value
    return {}


def find_structured_provider(redux_state: dict | None, doctor_url: str, postal_code: str = ""):
    if not redux_state:
        return {}, {}
    target = profile_path(doctor_url)
    candidates = []
    for obj in walk_dicts(redux_state):
        if profile_path(_provider_profile_url(obj)) == target:
            richness = sum(
                key in obj
                for key in (
                    "id",
                    "monolithId",
                    "npi",
                    "approvedFullName",
                    "averageRating",
                    "reviewCount",
                    "approvedLocations",
                    "offersTelemedicine",
                )
            )
            candidates.append((10 + richness, obj, {}))

        provider = obj.get("provider") if isinstance(obj, dict) else None
        if isinstance(provider, dict) and profile_path(_provider_profile_url(provider)) == target:
            score = 20
            location = _container_location(obj)
            loc_zip = str(
                location.get("zipCode")
                or location.get("postalCode")
                or location.get("zip")
                or ""
            )
            if postal_code and loc_zip == str(postal_code):
                score += 10
            if isinstance(obj.get("id"), str) and "|" in obj.get("id", ""):
                score += 2
            candidates.append((score, provider, obj))

    if not candidates:
        return {}, {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, provider, container = candidates[0]
    return provider, container


def _fallback_occurrence_from_anchor(
    page_url: str,
    page_meta: dict,
    source_file: str,
    redux_state: dict | None,
    anchor,
    card_index: int,
) -> dict | None:
    doctor_url = absolute_zocdoc_url(anchor.get("href", ""))
    if not doctor_url or not is_profile_url(doctor_url):
        return None
    provider, provider_location = find_structured_provider(redux_state, doctor_url)
    raw_id = provider_location.get("id") if isinstance(provider_location, dict) else None
    return {
        "listing_url": normalize_url(page_url),
        "listing_state_group": page_meta.get("state_group", "") or "",
        "listing_location": page_meta.get("location", "") or "",
        "listing_page_no": page_meta.get("page_no", "") or "",
        "listing_file": source_file,
        "listing_specialty_url": page_meta.get("specialty_url", "") or "",
        "listing_specialty_name": page_meta.get("specialty_name", "") or "",
        "listing_specialty_slug": page_meta.get("specialty_slug", "") or "",
        "card_index": card_index,
        "doctor_url": doctor_url,
        "doctor_name": _clean_text(anchor),
        "specialty_name": "",
        "provider_id": str(provider.get("id") or "") if provider else "",
        "monolith_id": str(provider.get("monolithId") or "") if provider else "",
        "npi": str(provider.get("npi") or "") if provider else "",
        "provider_location_key": raw_id if isinstance(raw_id, str) and "|" in raw_id else "",
        "average_rating": provider.get("averageRating") if provider else "",
        "review_count": provider.get("reviewCount") if provider else "",
        "address_line_1": "",
        "city": "",
        "state": "",
        "postal_code": "",
        "listing_offers_video_visits": False,
        "listing_accepts_new_patients": False,
        "structured_accepts_new_patients": tri_bool(provider.get("acceptsNewPatients")) if provider else None,
        "structured_offers_telemedicine": tri_bool(provider.get("offersTelemedicine")) if provider else None,
        "structured_has_virtual_locations": tri_bool(provider.get("hasVirtualLocations")) if provider else None,
        "structured_can_have_appointments": tri_bool(provider.get("canHaveAppointments")) if provider else None,
        "structured_has_new_patient_availability": tri_bool(provider.get("hasNewPatientAvailability")) if provider else None,
    }


def extract_listing_occurrences(page_url: str, html: str, page_meta: dict, source_file: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    redux_state = extract_redux_state(html)
    cards = soup.select('article[data-test="search-result-item"]')
    if not cards:
        cards = soup.select('[data-test="search-result-card"] article')

    rows: list[dict] = []
    for card_index, card in enumerate(cards, start=1):
        anchor = card.select_one('a[data-test="doctor-card-info-name"][href]')
        if anchor is None:
            for candidate in card.select('a[href]'):
                if is_profile_url(absolute_zocdoc_url(candidate.get("href", ""))):
                    anchor = candidate
                    break
        if anchor is None:
            continue

        doctor_url = absolute_zocdoc_url(anchor.get("href", ""))
        if not doctor_url or not is_profile_url(doctor_url):
            continue

        prefix = _clean_text(card.select_one('[data-test="doctor-card-info-name-prefix"]'))
        core_name = _clean_text(card.select_one('[data-test="doctor-card-info-name-full"]'))
        suffix = _clean_text(card.select_one('[data-test="doctor-card-info-name-suffix"]'))
        if core_name:
            doctor_name = " ".join(x for x in (prefix, core_name) if x).strip()
            if suffix:
                doctor_name += suffix
        else:
            doctor_name = _clean_text(anchor)

        specialty = _clean_text(card.select_one('[data-test="doctor-card-info-specialty"]'))
        rating = _clean_text(card.select_one('[data-test="doctor-card-info-rating-number"]'))
        reviews = _clean_text(card.select_one('[data-test="total-review-count"]'))
        address = card.select_one('[data-test="doctor-card-info-location"]')
        street = _clean_text(address.select_one('[itemprop="streetAddress"]')) if address else ""
        city = _clean_text(address.select_one('[itemprop="addressLocality"]')) if address else ""
        state = _clean_text(address.select_one('[itemprop="addressRegion"]')) if address else ""
        postal = _clean_text(address.select_one('[itemprop="postalCode"]')) if address else ""
        video_marker = card.select_one('[data-test="video-visits-text"]') is not None
        accepts_marker = card.select_one('[data-test="accept-new-patient"]') is not None

        provider, provider_location = find_structured_provider(redux_state, doctor_url, postal)
        provider_location_key = ""
        raw_id = provider_location.get("id") if isinstance(provider_location, dict) else None
        if isinstance(raw_id, str) and "|" in raw_id:
            provider_location_key = raw_id

        rows.append(
            {
                "listing_url": normalize_url(page_url),
                "listing_state_group": page_meta.get("state_group", "") or "",
                "listing_location": page_meta.get("location", "") or "",
                "listing_page_no": page_meta.get("page_no", "") or "",
                "listing_file": source_file,
                "listing_specialty_url": page_meta.get("specialty_url", "") or "",
                "listing_specialty_name": page_meta.get("specialty_name", "") or "",
                "listing_specialty_slug": page_meta.get("specialty_slug", "") or "",
                "card_index": card_index,
                "doctor_url": doctor_url,
                "doctor_name": doctor_name,
                "specialty_name": specialty,
                "provider_id": str(provider.get("id") or "") if provider else "",
                "monolith_id": str(provider.get("monolithId") or "") if provider else "",
                "npi": str(provider.get("npi") or "") if provider else "",
                "provider_location_key": provider_location_key,
                "average_rating": provider.get("averageRating") if provider.get("averageRating") is not None else rating,
                "review_count": provider.get("reviewCount") if provider.get("reviewCount") is not None else reviews,
                "address_line_1": street.strip(" ,"),
                "city": city,
                "state": state,
                "postal_code": postal,
                "listing_offers_video_visits": bool(video_marker),
                "listing_accepts_new_patients": bool(accepts_marker),
                "structured_accepts_new_patients": tri_bool(provider.get("acceptsNewPatients")) if provider else None,
                "structured_offers_telemedicine": tri_bool(provider.get("offersTelemedicine")) if provider else None,
                "structured_has_virtual_locations": tri_bool(provider.get("hasVirtualLocations")) if provider else None,
                "structured_can_have_appointments": tri_bool(provider.get("canHaveAppointments")) if provider else None,
                "structured_has_new_patient_availability": tri_bool(provider.get("hasNewPatientAvailability")) if provider else None,
            }
        )

    # Supplement rich-card rows from the *same validated doctor list* used by
    # parse_page(). This is deliberately stronger than relying on a second
    # BeautifulSoup selector pass: some rendered pagination captures are
    # malformed enough that the lightweight HTMLParser sees provider-name
    # anchors while BeautifulSoup does not expose them through the expected
    # wrapper structure. Using parse_page() here keeps validation and durable
    # trace coverage aligned by construction.
    seen_urls = {row.get("doctor_url", "") for row in rows if row.get("doctor_url")}
    validated_doctors = parse_page(html, page_url).get("doctors", [])
    next_index = len(rows) + 1
    for doctor in validated_doctors:
        doctor_url = normalize_url(doctor.get("doctor_url", ""))
        if not doctor_url or not is_profile_url(doctor_url) or doctor_url in seen_urls:
            continue
        provider, provider_location = find_structured_provider(redux_state, doctor_url)
        raw_id = provider_location.get("id") if isinstance(provider_location, dict) else None
        rows.append(
            {
                "listing_url": normalize_url(page_url),
                "listing_state_group": page_meta.get("state_group", "") or "",
                "listing_location": page_meta.get("location", "") or "",
                "listing_page_no": page_meta.get("page_no", "") or "",
                "listing_file": source_file,
                "listing_specialty_url": page_meta.get("specialty_url", "") or "",
                "listing_specialty_name": page_meta.get("specialty_name", "") or "",
                "listing_specialty_slug": page_meta.get("specialty_slug", "") or "",
                "card_index": next_index,
                "doctor_url": doctor_url,
                "doctor_name": doctor.get("doctor_name", "") or "",
                "specialty_name": "",
                "provider_id": str(provider.get("id") or "") if provider else "",
                "monolith_id": str(provider.get("monolithId") or "") if provider else "",
                "npi": str(provider.get("npi") or "") if provider else "",
                "provider_location_key": raw_id if isinstance(raw_id, str) and "|" in raw_id else "",
                "average_rating": provider.get("averageRating") if provider else "",
                "review_count": provider.get("reviewCount") if provider else "",
                "address_line_1": "",
                "city": "",
                "state": "",
                "postal_code": "",
                "listing_offers_video_visits": False,
                "listing_accepts_new_patients": False,
                "structured_accepts_new_patients": tri_bool(provider.get("acceptsNewPatients")) if provider else None,
                "structured_offers_telemedicine": tri_bool(provider.get("offersTelemedicine")) if provider else None,
                "structured_has_virtual_locations": tri_bool(provider.get("hasVirtualLocations")) if provider else None,
                "structured_can_have_appointments": tri_bool(provider.get("canHaveAppointments")) if provider else None,
                "structured_has_new_patient_availability": tri_bool(provider.get("hasNewPatientAvailability")) if provider else None,
            }
        )
        seen_urls.add(doctor_url)
        next_index += 1
    return rows
