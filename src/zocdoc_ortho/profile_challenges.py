from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from bs4 import BeautifulSoup

from .db import connect
from .outputs import export_profile_queue
from .utils import now_iso
from .workspace import Workspace

PROFILE_CHALLENGE_MARKERS = (
    ("verify you are human", "human-verification message"),
    ("are you a human", "human-verification message"),
    ("access is temporarily restricted", "temporary-access restriction"),
    ("automated (bot) activity", "automated-activity restriction"),
    ("unusual activity", "unusual-activity restriction"),
)

JS_ADBLOCK_PATTERN = re.compile(
    r"enable\s+(?:javascript|js)\s+and\s+disable\s+"
    r"(?:any\s+|your\s+|the\s+)?ad[\s-]*block(?:er|ers|ing)?", re.IGNORECASE
)


def js_adblock_challenge_reason(html: str) -> str | None:
    """Detect a rendered JS/ad-block wall, excluding inert fallback text."""
    lowered = html.lower()
    if "enable" not in lowered or not any(x in lowered for x in ("adblock", "ad blocker", "ad-block", "ad block")):
        return None
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style,noscript,template"):
        node.decompose()
    name = soup.select_one('[data-test="provider-name"]')
    if name and name.get_text(strip=True):
        return None
    if soup.select_one('a[data-test="doctor-card-info-name"][href]'):
        return None
    text = " ".join(soup.get_text(" ", strip=True).split())
    if JS_ADBLOCK_PATTERN.search(text):
        return "JavaScript/ad-block verification page"
    return None


class _ChallengeFrameParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.reason: str | None = None
        self.has_provider_content = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(name).lower(): str(value or "") for name, value in attrs}
        if values.get("data-test", "").lower() == "provider-name":
            self.has_provider_content = True
        if self.reason is not None or tag.lower() != "iframe":
            return
        src = values.get("src", "").lower()
        if "captcha-delivery.com" in src or "datadome" in src:
            self.reason = "DataDome CAPTCHA frame"


def profile_challenge_reason(html: str) -> str | None:
    """Return the recognized verification signal in a captured profile page."""
    parser = _ChallengeFrameParser()
    parser.feed(html)
    if parser.has_provider_content:
        return None
    js_reason = js_adblock_challenge_reason(html)
    if js_reason:
        return js_reason
    lowered = html.lower()
    for marker, reason in PROFILE_CHALLENGE_MARKERS:
        if marker in lowered:
            return reason
    return parser.reason


def profile_specialty_clause(specialties: tuple[str, ...]) -> tuple[str, list[str]]:
    values = [value.strip().lower() for value in specialties if value.strip()]
    if not values:
        return "", []
    placeholders = ",".join("?" for _ in values)
    clause = (
        " AND EXISTS ("
        "SELECT 1 FROM specialties s "
        "WHERE (lower(s.specialty_name)=lower(d.first_seen_specialty_name) "
        "OR lower(s.specialty_url)=lower(d.first_seen_specialty_url)) "
        f"AND (lower(s.specialty_slug) IN ({placeholders}) "
        f"OR lower(s.specialty_name) IN ({placeholders}) "
        f"OR lower(s.specialty_url) IN ({placeholders})))"
    )
    return clause, values + values + values


def _quarantine_destination(folder: Path, filename: str) -> Path:
    safe_name = Path(filename).name
    destination = folder / safe_name
    counter = 1
    while destination.exists():
        destination = folder / f"{Path(safe_name).stem}--{counter}{Path(safe_name).suffix}"
        counter += 1
    return destination


def repair_profile_challenges(
    workspace: Workspace,
    *,
    specialties: tuple[str, ...] = (),
    dry_run: bool = False,
) -> dict:
    """Audit saved profile HTML and reset recognized challenge captures safely."""
    workspace.ensure()
    clause, params = profile_specialty_clause(specialties)
    quarantine_dir = workspace.rejected_profile_dir
    result = {
        "scanned": 0,
        "challenged": 0,
        "reset": 0,
        "missing": 0,
        "dry_run": dry_run,
        "quarantine_dir": str(quarantine_dir),
    }

    with connect(workspace) as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT dp.doctor_url,dp.file
            FROM doctor_profiles dp
            JOIN doctors d ON d.doctor_url=dp.doctor_url
            WHERE dp.status='saved' AND COALESCE(dp.file,'')<>''{clause}
            ORDER BY dp.doctor_url
            """,
            params,
        ).fetchall()
        result["scanned"] = len(rows)

        for row in rows:
            filename = Path(str(row["file"] or "")).name
            path = workspace.profile_dir / filename
            if not path.exists():
                result["missing"] += 1
                continue
            reason = profile_challenge_reason(path.read_text(encoding="utf-8", errors="replace"))
            if reason is None:
                continue
            result["challenged"] += 1
            if dry_run:
                continue

            quarantine_dir.mkdir(parents=True, exist_ok=True)
            destination = _quarantine_destination(quarantine_dir, filename)
            path.replace(destination)
            message = f"Verification challenge quarantined: {reason}"
            conn.execute(
                """
                UPDATE doctor_profiles
                SET status='pending',file='',bytes=0,opened_at=NULL,saved_at=NULL,
                    error=?,last_error=?,last_seen_at=?,claimed_by=NULL,claimed_at=NULL
                WHERE doctor_url=? AND status='saved'
                """,
                (message, message, now_iso(), row["doctor_url"]),
            )
            conn.commit()
            result["reset"] += 1

        if not dry_run and result["reset"]:
            export_profile_queue(conn, workspace)

    return result
