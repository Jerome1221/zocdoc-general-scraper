from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
from bs4 import BeautifulSoup

from .queue import _assert_saver_ready
from .saver import enqueue_url
from .specialty_targets import sync_specialty_pages_from_catalog
from .urls import ZOCDOC_ORIGIN, absolute_zocdoc_url, normalize_url
from .utils import now_iso
from .workspace import Workspace

SPECIALTY_INDEX_URL = f"{ZOCDOC_ORIGIN}/specialty"
SPECIALTY_OUTPUT_COLUMNS = [
    "specialty_name",
    "specialty_slug",
    "specialty_url",
    "entity_type",
    "is_active",
    "first_seen_at",
    "last_seen_at",
]

_FACILITY_MARKERS = (
    "facility",
    "facilities",
    "clinic",
    "clinics",
    "urgent care",
)


def classify_specialty_entity(name: str) -> str:
    """Classify a Zocdoc /specialty entry without dropping any catalog entry."""
    lowered = " ".join((name or "").lower().split())
    if any(marker in lowered for marker in _FACILITY_MARKERS):
        return "facility_or_clinic"
    return "provider_specialty"


def _specialty_slug(url: str) -> str:
    return urlsplit(normalize_url(url)).path.strip("/")


def _heading_matches(tag) -> bool:
    if getattr(tag, "name", None) not in {"h1", "h2", "h3", "h4"}:
        return False
    text = " ".join(tag.stripped_strings).strip().lower()
    return text == "browse all specialties"


def _is_catalog_href(url: str) -> bool:
    parts = urlsplit(normalize_url(url))
    if parts.scheme not in {"http", "https"} or parts.netloc.lower() not in {
        "zocdoc.com",
        "www.zocdoc.com",
    }:
        return False
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 1:
        return False
    return segments[0].lower() != "specialty"


def parse_specialty_catalog(html: str, page_url: str = SPECIALTY_INDEX_URL) -> list[dict[str, str]]:
    """Parse the A-Z specialty catalog from a rendered Zocdoc /specialty page."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(_heading_matches)
    if heading is None:
        raise ValueError("Could not find the 'Browse All Specialties' section")

    anchors = []

    # The live page currently renders the catalog as a list after the heading.
    # Prefer that bounded list so header/footer navigation does not leak in.
    list_container = heading.find_next(["ul", "ol"])
    if list_container is not None:
        anchors = list_container.select("a[href]")

    # Defensive fallback for wrapper changes: walk forward from the catalog
    # heading until footer content begins.
    if len(anchors) < 10:
        anchors = []
        for node in heading.find_all_next():
            if getattr(node, "name", None) == "footer":
                break
            if getattr(node, "name", None) == "a" and node.get("href"):
                anchors.append(node)

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in anchors:
        name = " ".join(anchor.stripped_strings).strip()
        href = str(anchor.get("href") or "").strip()
        if not name or not href:
            continue
        url = absolute_zocdoc_url(href, base=page_url)
        if not _is_catalog_href(url) or url in seen:
            continue
        seen.add(url)
        rows.append(
            {
                "specialty_name": name,
                "specialty_slug": _specialty_slug(url),
                "specialty_url": url,
                "entity_type": classify_specialty_entity(name),
            }
        )

    if len(rows) < 10:
        raise ValueError(
            f"Specialty catalog parse produced only {len(rows)} row(s); refusing to treat it as a complete catalog"
        )

    return sorted(rows, key=lambda row: (row["specialty_name"].lower(), row["specialty_url"]))


def latest_specialty_catalog_html(workspace: Workspace) -> Path | None:
    files = sorted(
        workspace.specialty_dir.glob("*.html"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    return files[0] if files else None


def collect_specialty_catalog_html(
    workspace: Workspace,
    *,
    url: str = SPECIALTY_INDEX_URL,
    timeout_seconds: int = 60,
    server_url: str | None = None,
) -> Path:
    """Capture a fresh /specialty page through the existing Chrome saver."""
    _assert_saver_ready(workspace, server_url=server_url)
    before = {path.resolve() for path in workspace.specialty_dir.glob("*.html")}
    enqueue_url(url, server_url=server_url)
    started = time.time()

    while time.time() - started <= timeout_seconds:
        if workspace.blocked_flag.exists():
            raise RuntimeError("Restriction detected while collecting the specialty catalog")
        candidates = [
            path
            for path in workspace.specialty_dir.glob("*.html")
            if path.resolve() not in before
        ]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
        time.sleep(0.5)

    raise TimeoutError(f"No saved specialty catalog HTML after {timeout_seconds}s: {url}")


def refresh_specialties(
    conn: sqlite3.Connection,
    workspace: Workspace,
    html_path: Path,
    *,
    page_url: str = SPECIALTY_INDEX_URL,
) -> pd.DataFrame:
    """Parse a catalog capture, reconcile SQLite, and write output/specialties.csv."""
    html = html_path.read_text(encoding="utf-8", errors="replace")
    rows = parse_specialty_catalog(html, page_url=page_url)
    now = now_iso()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE specialties SET is_active=0")
        for row in rows:
            conn.execute(
                """
                INSERT INTO specialties (
                    specialty_url, specialty_name, specialty_slug, entity_type,
                    is_active, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(specialty_url) DO UPDATE SET
                    specialty_name=excluded.specialty_name,
                    specialty_slug=excluded.specialty_slug,
                    entity_type=excluded.entity_type,
                    is_active=1,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    row["specialty_url"],
                    row["specialty_name"],
                    row["specialty_slug"],
                    row["entity_type"],
                    now,
                    now,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    sync_specialty_pages_from_catalog(conn)

    db_rows = conn.execute(
        """
        SELECT specialty_name,specialty_slug,specialty_url,entity_type,
               is_active,first_seen_at,last_seen_at
        FROM specialties
        ORDER BY lower(specialty_name),specialty_url
        """
    ).fetchall()
    frame = pd.DataFrame([dict(row) for row in db_rows], columns=SPECIALTY_OUTPUT_COLUMNS)
    if not frame.empty:
        frame["is_active"] = frame["is_active"].astype(bool)

    output = workspace.output_dir / "specialties.csv"
    frame.to_csv(output, index=False, encoding="utf-8-sig")
    return frame
