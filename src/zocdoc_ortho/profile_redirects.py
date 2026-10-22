from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from .db import connect
from .outputs import export_profile_queue
from .profile_challenges import profile_challenge_reason, profile_specialty_clause
from .queue import mark_profile_saved
from .urls import is_profile_url, normalize_url
from .workspace import Workspace

REPAIRABLE_PROFILE_STATUSES = ("pending", "opening", "timeout", "blocked")


def _is_canonical_redirect(requested_url: str, canonical_url: str) -> bool:
    requested = urlsplit(normalize_url(requested_url))
    canonical = urlsplit(normalize_url(canonical_url))
    requested_parts = [part for part in requested.path.split("/") if part]
    canonical_parts = [part for part in canonical.path.split("/") if part]
    if len(requested_parts) != 2 or len(canonical_parts) != 2:
        return False
    if requested_parts[0].lower() != canonical_parts[0].lower():
        return False
    return bool(
        re.fullmatch(
            re.escape(requested_parts[1]) + r"-\d+",
            canonical_parts[1],
            flags=re.IGNORECASE,
        )
    )


def _manifest_profile_captures(workspace: Workspace) -> dict[str, set[str]]:
    captures: dict[str, set[str]] = defaultdict(set)
    for manifest in sorted(workspace.root.glob("manifest*.csv")):
        try:
            with manifest.open("r", newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    if row.get("page_type") != "profile" or row.get("status") not in {"saved", "existing"}:
                        continue
                    url = normalize_url(str(row.get("url") or ""))
                    filename = Path(str(row.get("file") or "")).name
                    if is_profile_url(url) and filename:
                        captures[url].add(filename)
        except (OSError, csv.Error, UnicodeError):
            continue
    return captures


def repair_profile_redirects(
    workspace: Workspace,
    *,
    specialties: tuple[str, ...] = (),
    dry_run: bool = False,
) -> dict:
    """Reconcile redirected profile captures that older savers stored under the final URL."""
    workspace.ensure()
    clause, params = profile_specialty_clause(specialties)
    captures = _manifest_profile_captures(workspace)
    result = {
        "scanned": 0,
        "matched": 0,
        "repaired": 0,
        "ambiguous": 0,
        "missing": 0,
        "challenged": 0,
        "dry_run": dry_run,
    }

    with connect(workspace) as conn:
        placeholders = ",".join("?" for _ in REPAIRABLE_PROFILE_STATUSES)
        rows = conn.execute(
            f"""
            SELECT DISTINCT dp.doctor_url,dp.status
            FROM doctor_profiles dp
            JOIN doctors d ON d.doctor_url=dp.doctor_url
            WHERE dp.status IN ({placeholders}){clause}
            ORDER BY dp.doctor_url
            """,
            [*REPAIRABLE_PROFILE_STATUSES, *params],
        ).fetchall()
        result["scanned"] = len(rows)

        for row in rows:
            requested_url = normalize_url(str(row["doctor_url"] or ""))
            matches = [url for url in captures if _is_canonical_redirect(requested_url, url)]
            if not matches:
                continue
            if len(matches) != 1:
                result["ambiguous"] += 1
                continue

            canonical_url = matches[0]
            files = [
                workspace.profile_dir / filename
                for filename in captures[canonical_url]
                if (workspace.profile_dir / filename).exists()
            ]
            if not files:
                result["missing"] += 1
                continue
            if len(files) != 1:
                result["ambiguous"] += 1
                continue

            path = files[0]
            html = path.read_text(encoding="utf-8", errors="replace")
            if profile_challenge_reason(html):
                result["challenged"] += 1
                continue

            result["matched"] += 1
            if dry_run:
                continue
            mark_profile_saved(conn, workspace, requested_url, path, write_output=False)
            result["repaired"] += 1

        if not dry_run and result["repaired"]:
            export_profile_queue(conn, workspace)

    return result
