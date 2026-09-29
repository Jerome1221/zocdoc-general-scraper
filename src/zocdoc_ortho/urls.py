from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

ZOCDOC_ORIGIN = "https://www.zocdoc.com"


def normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def absolute_zocdoc_url(value: str, base: str = ZOCDOC_ORIGIN) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return normalize_url(urljoin(base, value))


def safe_slug(url: str) -> str:
    url = normalize_url(url)
    parsed = urlsplit(url)
    raw = parsed.path.strip("/") or "home"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.replace("/", "__")).strip("_")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{slug}--{digest}.html"


def base_location_url(url: str) -> str:
    parts = urlsplit(normalize_url(url))
    path = re.sub(r"/\d+$", "", parts.path.rstrip("/"))
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def page_number(url: str) -> int:
    match = re.search(r"/(\d+)/?$", urlsplit(url).path)
    return int(match.group(1)) if match else 1


def profile_path(url: str) -> str:
    value = absolute_zocdoc_url(url)
    return urlsplit(value).path.rstrip("/") if value else ""


def is_specialty_index_url(url: str) -> bool:
    return urlsplit(normalize_url(url)).path == "/specialty"


def is_specialty_landing_url(url: str) -> bool:
    path = urlsplit(normalize_url(url)).path.rstrip("/")
    if not path or path in {"/specialty"} or is_profile_url(url):
        return False
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) == 1


def is_listing_url(url: str) -> bool:
    path = urlsplit(normalize_url(url)).path.rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) not in {2, 3}:
        return False
    if len(segments) == 3 and not segments[2].isdigit():
        return False
    location_segment = segments[1]
    match = re.search(r"-(\d+)pm$", location_segment)
    return bool(match)


def is_profile_url(url: str) -> bool:
    """Return True for Zocdoc provider profile URLs across provider types.

    Provider URLs are not guaranteed to end in a numeric Zocdoc id. Real
    listing cards can contain paths such as ``/dentist/amy-hartsfield-dmd``
    and ``/doctor/1114140902-timothy-nettles-dmd`` in addition to
    ``/dentist/name-dds-409823``. Treat the two-segment provider-card shape as
    a profile while explicitly excluding known non-profile route families and
    listing-location slugs that end in ``<id>pm``.
    """
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    if parts.netloc and parts.netloc.lower() not in {"www.zocdoc.com", "zocdoc.com"}:
        return False
    segments = [segment for segment in parts.path.rstrip("/").split("/") if segment]
    if len(segments) != 2:
        return False

    route, slug = segments
    if re.search(r"-[0-9]+pm$", slug, flags=re.IGNORECASE):
        return False

    non_profile_routes = {
        "about", "business", "patient-help", "createuser", "insurance",
        "specialty", "location", "condition", "procedure", "treatment",
        "hospital", "practice", "resources", "blog", "techblog",
    }
    if route.lower() in non_profile_routes:
        return False

    return bool(route and slug)
