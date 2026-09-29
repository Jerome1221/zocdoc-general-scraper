from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
from bs4 import BeautifulSoup

from .db import connect
from .outputs import export_page_outputs
from .performance import PerformanceRecorder
from .queue import _assert_saver_ready
from .redux import extract_redux_state
from .saver import configure_capture, enqueue_url
from .urls import absolute_zocdoc_url, normalize_url, safe_slug
from .utils import now_iso
from .workspace import Workspace

SPECIALTY_PAGE_COLUMNS = [
    "specialty_name",
    "specialty_slug",
    "specialty_url",
    "status",
    "specialty_id",
    "procedure_id",
    "search_query",
    "location_count",
    "file",
    "bytes",
    "discovered_at",
    "opened_at",
    "saved_at",
    "error",
]

SPECIALTY_TARGET_COLUMNS = [
    "specialty_name",
    "specialty_slug",
    "specialty_url",
    "state_group",
    "location",
    "listing_url",
    "is_active",
    "first_seen_at",
    "last_seen_at",
]


def _slug_from_url(url: str) -> str:
    return urlsplit(normalize_url(url)).path.strip("/").split("/")[0]


def _direct_child(element, *, data_test: str | None = None, role: str | None = None):
    for child in element.find_all(recursive=False):
        if data_test is not None and child.get("data-test") != data_test:
            continue
        if role is not None and child.get("role") != role:
            continue
        return child
    return None


def parse_specialty_landing(html: str, page_url: str) -> dict:
    """Parse one rendered specialty landing page into identity + location targets."""
    soup = BeautifulSoup(html, "html.parser")
    redux = extract_redux_state(html) or {}
    params = ((redux.get("search") or {}).get("parameters") or {}) if isinstance(redux, dict) else {}

    canonical = soup.find("link", rel="canonical")
    canonical_url = absolute_zocdoc_url(canonical.get("href", "")) if canonical else ""
    specialty_url = normalize_url(str(params.get("url") or canonical_url or page_url))
    specialty_slug = _slug_from_url(specialty_url)

    if not specialty_slug or specialty_slug == "specialty":
        raise ValueError(f"Could not identify a specialty landing URL from {page_url}")
    if str(params.get("searchType") or "").lower() not in {"", "specialty"}:
        raise ValueError(f"Unexpected search type for specialty landing: {params.get('searchType')}")

    panel = soup.select_one('[data-test="tab-panel-location"]')
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    if panel is not None:
        for section in panel.select('div[data-test^="expandable-list-section-"]'):
            list_box = _direct_child(section, data_test="expandable-list-section-list")
            if list_box is None:
                continue
            heading = _direct_child(section, role="button")
            state_group = " ".join(heading.stripped_strings).strip() if heading else ""

            for anchor in list_box.select("a[href]"):
                href = str(anchor.get("href") or "").strip()
                target = absolute_zocdoc_url(href, base=specialty_url)
                target_parts = [segment for segment in urlsplit(target).path.split("/") if segment]
                if len(target_parts) != 2:
                    continue
                if target_parts[0] != specialty_slug or not target_parts[1].endswith("pm"):
                    continue
                suffix = target_parts[1].rsplit("-", 1)[-1]
                if not suffix[:-2].isdigit():
                    continue
                if target in seen:
                    continue
                seen.add(target)
                rows.append(
                    {
                        "state_group": state_group,
                        "location": " ".join(anchor.stripped_strings).strip(),
                        "listing_url": target,
                    }
                )

    # Defensive fallback if wrappers change but links remain in rendered HTML.
    if panel is None or not rows:
        for anchor in soup.select(f'a[href^="/{specialty_slug}/"]'):
            href = str(anchor.get("href") or "").strip()
            target = absolute_zocdoc_url(href, base=specialty_url)
            parts = [segment for segment in urlsplit(target).path.split("/") if segment]
            if len(parts) != 2 or parts[0] != specialty_slug or not parts[1].endswith("pm"):
                continue
            suffix = parts[1].rsplit("-", 1)[-1]
            if not suffix[:-2].isdigit() or target in seen:
                continue
            seen.add(target)
            rows.append(
                {
                    "state_group": "",
                    "location": " ".join(anchor.stripped_strings).strip(),
                    "listing_url": target,
                }
            )

    return {
        "specialty_url": specialty_url,
        "specialty_slug": specialty_slug,
        "specialty_id": str(params.get("specialtyId") or ""),
        "procedure_id": str(params.get("procedureId") or ""),
        "search_query": str(params.get("searchQuery") or ""),
        "locations": sorted(rows, key=lambda row: (row["state_group"], row["location"], row["listing_url"])),
    }


def sync_specialty_pages_from_catalog(conn: sqlite3.Connection) -> int:
    """Ensure every active catalog entry has a resumable landing-page job."""
    now = now_iso()
    rows = conn.execute(
        """
        SELECT specialty_url,specialty_name,specialty_slug
        FROM specialties
        WHERE is_active=1
        ORDER BY lower(specialty_name),specialty_url
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT INTO specialty_pages (
                specialty_url,specialty_name,specialty_slug,status,discovered_at
            ) VALUES (?,?,?,'pending',?)
            ON CONFLICT(specialty_url) DO UPDATE SET
                specialty_name=excluded.specialty_name,
                specialty_slug=excluded.specialty_slug
            """,
            (row["specialty_url"], row["specialty_name"], row["specialty_slug"], now),
        )
    conn.commit()
    return len(rows)


def _landing_prefix(url: str) -> str:
    return safe_slug(url).removesuffix(".html") + "--"


def latest_specialty_landing_html(workspace: Workspace, specialty_url: str) -> Path | None:
    prefix = _landing_prefix(specialty_url)
    candidates = sorted(
        workspace.specialty_landing_dir.glob(f"{prefix}*.html"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    return candidates[0] if candidates else None


def refresh_specialty_targets(
    conn: sqlite3.Connection,
    workspace: Workspace,
    specialty_url: str,
    html_path: Path,
) -> dict:
    """Parse one specialty landing capture and reconcile its location targets."""
    row = conn.execute(
        """
        SELECT specialty_url,specialty_name,specialty_slug
        FROM specialty_pages
        WHERE specialty_url=?
        """,
        (normalize_url(specialty_url),),
    ).fetchone()
    if row is None:
        raise KeyError(f"Specialty page is not registered: {specialty_url}")

    html = html_path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_specialty_landing(html, specialty_url)
    now = now_iso()
    specialty_name = row["specialty_name"] or parsed["search_query"] or row["specialty_slug"]
    specialty_slug = row["specialty_slug"] or parsed["specialty_slug"]

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE specialty_pages
            SET status='saved',file=?,bytes=?,specialty_id=?,procedure_id=?,search_query=?,
                location_count=?,saved_at=?,error=''
            WHERE specialty_url=?
            """,
            (
                html_path.name,
                html_path.stat().st_size,
                parsed["specialty_id"],
                parsed["procedure_id"],
                parsed["search_query"],
                len(parsed["locations"]),
                now,
                row["specialty_url"],
            ),
        )
        conn.execute(
            "UPDATE specialty_targets SET is_active=0 WHERE specialty_url=?",
            (row["specialty_url"],),
        )

        for target in parsed["locations"]:
            conn.execute(
                """
                INSERT INTO specialty_targets (
                    listing_url,specialty_url,specialty_name,specialty_slug,
                    state_group,location,is_active,first_seen_at,last_seen_at
                ) VALUES (?,?,?,?,?,?,1,?,?)
                ON CONFLICT(listing_url) DO UPDATE SET
                    specialty_url=excluded.specialty_url,
                    specialty_name=excluded.specialty_name,
                    specialty_slug=excluded.specialty_slug,
                    state_group=excluded.state_group,
                    location=excluded.location,
                    is_active=1,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    target["listing_url"],
                    row["specialty_url"],
                    specialty_name,
                    specialty_slug,
                    target["state_group"],
                    target["location"],
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO pages (
                    url,base_location_url,state_group,location,page_no,source,status,discovered_at,
                    specialty_url,specialty_name,specialty_slug
                ) VALUES (?,?,?,?,1,'seed','pending',?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    state_group=excluded.state_group,
                    location=excluded.location,
                    specialty_url=excluded.specialty_url,
                    specialty_name=excluded.specialty_name,
                    specialty_slug=excluded.specialty_slug
                """,
                (
                    target["listing_url"],
                    target["listing_url"],
                    target["state_group"],
                    target["location"],
                    now,
                    row["specialty_url"],
                    specialty_name,
                    specialty_slug,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    export_specialty_target_outputs(conn, workspace)
    export_page_outputs(conn, workspace)
    return {
        "specialty_name": specialty_name,
        "specialty_slug": specialty_slug,
        "specialty_url": row["specialty_url"],
        "location_count": len(parsed["locations"]),
        "specialty_id": parsed["specialty_id"],
        "procedure_id": parsed["procedure_id"],
        "search_query": parsed["search_query"],
    }


def export_specialty_target_outputs(conn: sqlite3.Connection, workspace: Workspace) -> None:
    workspace.ensure()
    pages = conn.execute(
        """
        SELECT specialty_name,specialty_slug,specialty_url,status,specialty_id,
               procedure_id,search_query,location_count,file,bytes,
               discovered_at,opened_at,saved_at,error
        FROM specialty_pages
        ORDER BY lower(specialty_name),specialty_url
        """
    ).fetchall()
    pd.DataFrame([dict(row) for row in pages], columns=SPECIALTY_PAGE_COLUMNS).to_csv(
        workspace.output_dir / "specialty_pages.csv", index=False, encoding="utf-8-sig"
    )

    targets = conn.execute(
        """
        SELECT specialty_name,specialty_slug,specialty_url,state_group,location,
               listing_url,is_active,first_seen_at,last_seen_at
        FROM specialty_targets
        ORDER BY lower(specialty_name),state_group,location,listing_url
        """
    ).fetchall()
    frame = pd.DataFrame([dict(row) for row in targets], columns=SPECIALTY_TARGET_COLUMNS)
    if not frame.empty:
        frame["is_active"] = frame["is_active"].astype(bool)
    frame.to_csv(workspace.output_dir / "specialty_targets.csv", index=False, encoding="utf-8-sig")

    active = frame.loc[frame["is_active"]] if not frame.empty else frame
    if not active.empty:
        summary = (
            active.groupby(["specialty_name", "specialty_slug", "specialty_url"], as_index=False)
            .agg(state_count=("state_group", "nunique"), location_count=("listing_url", "nunique"))
            .sort_values(["location_count", "specialty_name"], ascending=[False, True])
        )
    else:
        summary = pd.DataFrame(
            columns=["specialty_name", "specialty_slug", "specialty_url", "state_count", "location_count"]
        )
    summary.to_csv(workspace.output_dir / "specialty_target_summary.csv", index=False, encoding="utf-8-sig")


def _matches_specialty(row: sqlite3.Row, selectors: tuple[str, ...]) -> bool:
    if not selectors:
        return True
    wanted = {value.strip().lower() for value in selectors if value.strip()}
    return (
        str(row["specialty_slug"] or "").lower() in wanted
        or str(row["specialty_name"] or "").lower() in wanted
        or str(row["specialty_url"] or "").lower() in wanted
    )


def rebuild_specialty_targets_from_saved(
    workspace: Workspace,
    *,
    specialties: tuple[str, ...] = (),
) -> int:
    """Offline rebuild of specialty targets from the latest saved landing HTML."""
    count = 0
    with connect(workspace) as conn:
        sync_specialty_pages_from_catalog(conn)
        rows = conn.execute(
            """
            SELECT specialty_url,specialty_name,specialty_slug
            FROM specialty_pages
            ORDER BY lower(specialty_name),specialty_url
            """
        ).fetchall()
        for row in rows:
            if not _matches_specialty(row, specialties):
                continue
            path = latest_specialty_landing_html(workspace, row["specialty_url"])
            if path is None:
                continue
            refresh_specialty_targets(conn, workspace, row["specialty_url"], path)
            count += 1
        export_specialty_target_outputs(conn, workspace)
    return count


def collect_specialty_landing_batch(
    workspace: Workspace,
    *,
    limit: int = 25,
    concurrency: int = 4,
    timeout_seconds: int = 60,
    stagger_seconds: float = 0.15,
    poll_seconds: float = 0.25,
    page_check_ms: int = 350,
    stable_checks: int = 2,
    page_max_wait_ms: int = 10000,
    controller_poll_ms: int = 200,
    specialties: tuple[str, ...] = (),
    server_url: str | None = None,
    runner_id: str = "single",
) -> int:
    """Collect specialty landing pages with a rolling Chrome worker pool."""
    _assert_saver_ready(workspace, server_url=server_url)
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    configure_capture(
        "specialty_landing",
        check_ms=page_check_ms,
        stable_checks=stable_checks,
        max_wait_ms=page_max_wait_ms,
        controller_poll_ms=controller_poll_ms,
        server_url=server_url,
    )
    recorder = PerformanceRecorder(
        workspace,
        "discover-targets",
        {
            "concurrency": concurrency,
            "stagger_seconds": stagger_seconds,
            "poll_seconds": poll_seconds,
            "page_check_ms": page_check_ms,
            "stable_checks": stable_checks,
            "page_max_wait_ms": page_max_wait_ms,
            "controller_poll_ms": controller_poll_ms,
            "runner_id": runner_id,
        },
    )
    active: dict[str, dict] = {}
    scheduled = 0
    completed = 0
    last_enqueue = 0.0
    exhausted = False

    try:
        while completed < limit:
            if workspace.blocked_flag.exists():
                with connect(workspace) as conn:
                    conn.execute(
                        "UPDATE specialty_pages SET status='blocked',error=? WHERE status='opening'",
                        ("Restriction detected by Chrome saver",),
                    )
                    conn.commit()
                    export_specialty_target_outputs(conn, workspace)
                now = time.monotonic()
                for url, item in active.items():
                    recorder.record(
                        page_type="specialty_landing", status="blocked", url=url,
                        duration_seconds=now - item["started"], specialty_name=item["name"],
                    )
                completed += len(active)
                active.clear()
                print("STOP: restriction detected.")
                break

            now = time.monotonic()
            for url, item in list(active.items()):
                path = latest_specialty_landing_html(workspace, url)
                if path is not None:
                    with connect(workspace) as conn:
                        result = refresh_specialty_targets(conn, workspace, url, path)
                    duration = now - item["started"]
                    print(f"    SAVED | {duration:.2f}s | locations={result['location_count']} | {item['name']}")
                    recorder.record(
                        page_type="specialty_landing", status="saved", url=url,
                        duration_seconds=duration, specialty_name=item["name"],
                    )
                    active.pop(url, None)
                    completed += 1
                    continue
                if now - item["started"] > timeout_seconds:
                    with connect(workspace) as conn:
                        conn.execute(
                            "UPDATE specialty_pages SET status='timeout',error=? WHERE specialty_url=?",
                            (f"No saved HTML after {timeout_seconds}s", url),
                        )
                        conn.commit()
                        export_specialty_target_outputs(conn, workspace)
                    print(f"    TIMEOUT | {now - item['started']:.2f}s | {url}")
                    recorder.record(
                        page_type="specialty_landing", status="timeout", url=url,
                        duration_seconds=now - item["started"], specialty_name=item["name"],
                    )
                    active.pop(url, None)
                    completed += 1

            while len(active) < concurrency and scheduled < limit and not exhausted:
                with connect(workspace) as conn:
                    sync_specialty_pages_from_catalog(conn)
                    rows = conn.execute(
                        """
                        SELECT * FROM specialty_pages
                        WHERE status='pending'
                        ORDER BY lower(specialty_name),specialty_url
                        """
                    ).fetchall()
                    candidates = [row for row in rows if _matches_specialty(row, specialties)]
                    if not candidates:
                        exhausted = True
                        export_specialty_target_outputs(conn, workspace)
                        break
                    row = candidates[0]
                    url = row["specialty_url"]
                    existing = latest_specialty_landing_html(workspace, url)
                    if existing is not None:
                        result = refresh_specialty_targets(conn, workspace, url, existing)
                        scheduled += 1
                        completed += 1
                        print(f"INGEST existing | locations={result['location_count']} | {row['specialty_name']}")
                        recorder.record(
                            page_type="specialty_landing", status="existing", url=url,
                            duration_seconds=0.0, specialty_name=row["specialty_name"],
                        )
                        continue
                    conn.execute(
                        """
                        UPDATE specialty_pages
                        SET status='opening',opened_at=?,error=''
                        WHERE specialty_url=? AND status='pending'
                        """,
                        (now_iso(), url),
                    )
                    conn.commit()
                    export_specialty_target_outputs(conn, workspace)

                if stagger_seconds > 0 and last_enqueue:
                    wait = stagger_seconds - (time.monotonic() - last_enqueue)
                    if wait > 0:
                        time.sleep(wait)
                print(f"OPEN SPECIALTY [{scheduled + 1}/{limit}] {row['specialty_name']}")
                print(f"    {url}")
                enqueue_url(url, server_url=server_url)
                started = time.monotonic()
                last_enqueue = started
                active[url] = {"started": started, "name": row["specialty_name"]}
                scheduled += 1

            if not active and (exhausted or scheduled >= limit):
                break
            if active:
                time.sleep(poll_seconds)

        return completed
    finally:
        summary = recorder.finish()
        if summary:
            print(
                f"PERF | {summary['processed']} specialty pages | {summary['pages_per_minute']:.1f}/min | "
                f"elapsed={summary['elapsed_seconds'] / 60:.2f}m | p50={summary['p50_seconds']:.2f}s p95={summary['p95_seconds']:.2f}s"
            )
