from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .urls import safe_slug
from .workspace import Workspace

PROFILE_UNAVAILABLE_MARKERS = (
    ("not found. redirecting to /404", "Zocdoc profile returned /404"),
    ("not found. redirecting to/404", "Zocdoc profile returned /404"),
)


def profile_unavailable_reason(html: str, *, final_url: str = "") -> str | None:
    """Return a terminal reason for a recognized unavailable profile page."""
    final = urlsplit(final_url)
    if final.hostname in {"zocdoc.com", "www.zocdoc.com"} and final.path.rstrip("/") == "/404":
        return "Zocdoc profile returned /404"
    content = str(html or "")
    text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True) if "<" in content else content
    lowered = " ".join(text.lower().split())
    for marker, reason in PROFILE_UNAVAILABLE_MARKERS:
        if marker in lowered:
            return reason
    if re.search(r"not found\.?\s*redirecting to\s*/(?:\s|$|[.])", lowered):
        return "Zocdoc profile returned Not Found homepage redirect"
    return None


def archive_unavailable_profile(
    workspace: Workspace,
    source: Path,
    doctor_url: str,
) -> Path:
    """Move an unavailable response out of the valid profile directory."""
    destination = workspace.unavailable_profile_dir / safe_slug(doctor_url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return destination
    destination.unlink(missing_ok=True)
    source.replace(destination)
    return destination
