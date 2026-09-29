from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .db import connect
from .runners import load_runner_config, runner_status
from .saver import configure_capture, enqueue_url
from .urls import base_location_url, normalize_url
from .utils import now_iso
from .workspace import Workspace

COUNT_CHANGED = "count_changed"
NEW_LOCATION = "new_location"
NEW_SPECIALTY = "new_specialty"
MANUAL_REFRESH = "manual_refresh"

PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"

LIVE_CHECK_MS = 250
LIVE_STABLE_CHECKS = 2
LIVE_MAX_WAIT_MS = 8000
LIVE_CONTROLLER_POLL_MS = 200
LIVE_CONCURRENCY_PER_RUNNER = 4
LIVE_TIMEOUT_SECONDS = 60
LIVE_POLL_SECONDS = 0.2

_STRICT_COUNT_RE = re.compile(
    r"\b(?P<count>[0-9][0-9,]*)\s+verified\s+(?P<specialty>.+?)\s+in\s+(?P<location>.+?)\s*$",
    flags=re.IGNORECASE,
)
_CITY_STATE_COUNT_RE = re.compile(
    r"\b(?P<count>[0-9][0-9,]*)\s+verified\s+(?P<specialty>.+?)\s+in\s+"
    r"(?P<location>[A-Za-z0-9 .'\-]+,\s*[A-Z]{2})\b",
    flags=re.IGNORECASE,
)
_LOOSE_COUNT_RE = re.compile(
    r"\b(?P<count>[0-9][0-9,]*)\s+verified\s+(?P<specialty>.+?)\s+in\s+(?P<location>.+?)"
    r"(?=\s+(?:Sort by|Zocdoc|Book|Find|Search|Filter|Availability|Patients|All filters)\b|$)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class VerifiedCount:
    specialty: str
    location: str
    verified_provider_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "specialty": self.specialty,
            "location": self.location,
            "verified_provider_count": self.verified_provider_count,
        }


@dataclass(frozen=True)
class UpdateTarget:
    specialty: str
    specialty_slug: str
    location: str
    location_url: str
    verified_provider_count: int | None


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_specialty(value: str) -> str:
    return _clean_text(value).strip(" .,;:").lower()


def _normalize_location(value: str) -> str:
    return _clean_text(value).strip(" .,;:")


def _candidate_texts(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()

    candidates: list[str] = []
    for selector in (
        '[data-test="selection-criteria-header"]',
        "h1",
        "h2",
        "title",
    ):
        for element in soup.select(selector):
            text = _clean_text(element.get_text(" ", strip=True))
            if text:
                candidates.append(text)

    for text in soup.stripped_strings:
        cleaned = _clean_text(text)
        lower = cleaned.lower()
        if "verified" in lower and " in " in lower:
            candidates.append(cleaned)

    page_text = _clean_text(soup.get_text(" ", strip=True))
    if page_text:
        candidates.append(page_text)

    seen: set[str] = set()
    ordered: list[str] = []
    for text in candidates:
        if text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _count_from_text(text: str) -> VerifiedCount | None:
    for pattern in (_STRICT_COUNT_RE, _CITY_STATE_COUNT_RE, _LOOSE_COUNT_RE):
        match = pattern.search(text)
        if not match:
            continue
        specialty = _normalize_specialty(match.group("specialty"))
        location = _normalize_location(match.group("location"))
        if not specialty or not location:
            continue
        return VerifiedCount(
            specialty=specialty,
            location=location,
            verified_provider_count=int(match.group("count").replace(",", "")),
        )
    return None


def extract_verified_provider_count(html: str) -> dict[str, Any] | None:
    """Extract the visible listing header count from saved Zocdoc listing HTML."""
    parsed = parse_verified_provider_count(html)
    return parsed.as_dict() if parsed else None


def parse_verified_provider_count(html: str) -> VerifiedCount | None:
    """Parse `N verified <specialty> in <location>` without hard-coding specialties."""
    for text in _candidate_texts(html):
        parsed = _count_from_text(text)
        if parsed is not None:
            return parsed
    return None


def _listing_file_path(workspace: Workspace, file_value: str) -> Path | None:
    file_value = (file_value or "").strip()
    if not file_value:
        return None
    path = Path(file_value)
    candidates = [path] if path.is_absolute() else [workspace.listing_dir / path, workspace.root / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_count_from_html(workspace: Workspace, row: sqlite3.Row | None) -> VerifiedCount | None:
    if row is None:
        return None
    html_path = _listing_file_path(workspace, row["file"] or "")
    if html_path is None:
        return None
    html = html_path.read_text(encoding="utf-8", errors="replace")
    return parse_verified_provider_count(html)


def _base_page_for_location(conn: sqlite3.Connection, location_url: str) -> sqlite3.Row | None:
    normalized = normalize_url(location_url)
    base = base_location_url(normalized)
    return conn.execute(
        """
        SELECT * FROM pages
        WHERE url=? OR (base_location_url=? AND page_no=1)
        ORDER BY CASE WHEN url=? THEN 0 ELSE 1 END, saved_at DESC, discovered_at DESC
        LIMIT 1
        """,
        (normalized, base, normalized),
    ).fetchone()


def _current_count(
    conn: sqlite3.Connection,
    workspace: Workspace,
    location_url: str,
) -> tuple[int | None, VerifiedCount | None]:
    row = _base_page_for_location(conn, location_url)
    if row is not None and row["advertised_total"] is not None:
        return int(row["advertised_total"]), None
    parsed = _read_count_from_html(workspace, row)
    if parsed is None:
        return None, None
    return parsed.verified_provider_count, parsed


def load_update_targets(conn: sqlite3.Connection, workspace: Workspace) -> list[UpdateTarget]:
    raw_targets: dict[tuple[str, str], dict[str, str]] = {}

    for row in conn.execute(
        """
        SELECT specialty_name, specialty_slug, location, listing_url
        FROM specialty_targets
        WHERE is_active=1
        ORDER BY specialty_slug, location, listing_url
        """
    ).fetchall():
        location_url = base_location_url(row["listing_url"])
        key = ((row["specialty_slug"] or ""), location_url)
        raw_targets[key] = {
            "specialty": row["specialty_name"] or row["specialty_slug"] or "",
            "specialty_slug": row["specialty_slug"] or "",
            "location": row["location"] or "",
            "location_url": location_url,
        }

    for row in conn.execute(
        """
        SELECT specialty_name, specialty_slug, location, base_location_url, url
        FROM pages
        WHERE page_no=1
        ORDER BY specialty_slug, location, base_location_url
        """
    ).fetchall():
        location_url = base_location_url(row["base_location_url"] or row["url"])
        key = ((row["specialty_slug"] or ""), location_url)
        raw_targets.setdefault(
            key,
            {
                "specialty": row["specialty_name"] or row["specialty_slug"] or "",
                "specialty_slug": row["specialty_slug"] or "",
                "location": row["location"] or "",
                "location_url": location_url,
            },
        )

    targets: list[UpdateTarget] = []
    for item in raw_targets.values():
        count, parsed = _current_count(conn, workspace, item["location_url"])
        specialty = item["specialty"] or (parsed.specialty if parsed else "")
        location = item["location"] or (parsed.location if parsed else "")
        targets.append(
            UpdateTarget(
                specialty=specialty,
                specialty_slug=item["specialty_slug"],
                location=location,
                location_url=item["location_url"],
                verified_provider_count=count,
            )
        )
    return targets


def _insert_snapshot(conn: sqlite3.Connection, target: UpdateTarget, checked_at: str) -> None:
    if target.verified_provider_count is None:
        raise ValueError("Cannot snapshot a target without a verified provider count")
    conn.execute(
        """
        INSERT INTO location_snapshots (
            specialty, specialty_slug, location, location_url,
            verified_provider_count, checked_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target.specialty,
            target.specialty_slug,
            target.location,
            target.location_url,
            int(target.verified_provider_count),
            checked_at,
            now_iso(),
        ),
    )


def _start_snapshot_run(conn: sqlite3.Connection, *, mode: str, locations_selected: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO snapshot_runs (started_at, mode, locations_selected)
        VALUES (?, ?, ?)
        """,
        (now_iso(), mode, int(locations_selected)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _finish_snapshot_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    locations_processed: int,
    success_count: int,
    failed_count: int,
) -> None:
    conn.execute(
        """
        UPDATE snapshot_runs
        SET completed_at=?, locations_processed=?, success_count=?, failed_count=?
        WHERE id=?
        """,
        (
            now_iso(),
            int(locations_processed),
            int(success_count),
            int(failed_count),
            int(run_id),
        ),
    )
    conn.commit()


def create_location_snapshots(workspace: Workspace) -> dict[str, int]:
    with connect(workspace) as conn:
        targets = load_update_targets(conn, workspace)
        run_id = _start_snapshot_run(conn, mode="existing_html", locations_selected=len(targets))
        checked_at = now_iso()
        created = 0
        failed = 0
        for target in targets:
            if target.verified_provider_count is None:
                failed += 1
                continue
            _insert_snapshot(conn, target, checked_at)
            created += 1
        conn.commit()
        _finish_snapshot_run(
            conn,
            run_id,
            locations_processed=len(targets),
            success_count=created,
            failed_count=failed,
        )
    return {
        "locations_processed": len(targets),
        "snapshots_created": created,
        "failed": failed,
    }


def _latest_snapshot(conn: sqlite3.Connection, target: UpdateTarget) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM location_snapshots
        WHERE specialty_slug=? AND location_url=?
        ORDER BY checked_at DESC, id DESC
        LIMIT 1
        """,
        (target.specialty_slug, target.location_url),
    ).fetchone()


def _queue_count_change(conn: sqlite3.Connection, target: UpdateTarget, created_at: str) -> int:
    existing = conn.execute(
        """
        SELECT 1
        FROM refresh_queue
        WHERE specialty_slug=? AND location_url=? AND reason=? AND status IN ('pending','processing')
        LIMIT 1
        """,
        (target.specialty_slug, target.location_url, COUNT_CHANGED),
    ).fetchone()
    if existing is not None:
        return 0
    conn.execute(
        """
        INSERT INTO refresh_queue (
            specialty, specialty_slug, location, location_url, reason, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            target.specialty,
            target.specialty_slug,
            target.location,
            target.location_url,
            COUNT_CHANGED,
            created_at,
        ),
    )
    return 1


def _live_targets(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    from_snapshots: bool,
    limit: int | None,
) -> list[UpdateTarget]:
    targets = load_update_targets(conn, workspace)
    if from_snapshots:
        snapshot_keys = {
            (row["specialty_slug"] or "", normalize_url(row["location_url"]))
            for row in conn.execute(
                "SELECT DISTINCT specialty_slug, location_url FROM location_snapshots"
            ).fetchall()
        }
        targets = [
            target
            for target in targets
            if (target.specialty_slug, normalize_url(target.location_url)) in snapshot_keys
        ]

    unique: list[UpdateTarget] = []
    seen_urls: set[str] = set()
    for target in targets:
        normalized = normalize_url(target.location_url)
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        unique.append(target)
    if limit is not None:
        unique = unique[:limit]
    return unique


def _active_live_runners(workspace: Workspace) -> list[dict[str, Any]]:
    start_command = f'zocdoc-collector --workspace "{workspace.root}" runners start'
    error = f"Live updater requires active runners. Run:\n{start_command}"
    try:
        config = load_runner_config(workspace)
        status = runner_status(workspace)
    except (FileNotFoundError, OSError, ValueError):
        raise RuntimeError(error) from None

    status_by_id = {row["runner_id"]: row for row in status.get("runners", [])}
    active = []
    for runner in config.get("runners", []):
        state = status_by_id.get(runner["runner_id"], {})
        if state.get("server_ok") and state.get("controller_active"):
            active.append(runner)
    if not active:
        raise RuntimeError(error)
    return active


def _update_live_page_count(
    conn: sqlite3.Connection,
    target: UpdateTarget,
    verified_provider_count: int,
    checked_at: str,
) -> None:
    conn.execute(
        """
        UPDATE pages
        SET advertised_total=?, saved_at=?
        WHERE page_no=1 AND (url=? OR base_location_url=?)
        """,
        (
            int(verified_provider_count),
            checked_at,
            normalize_url(target.location_url),
            base_location_url(target.location_url),
        ),
    )


def create_live_location_snapshots(
    workspace: Workspace,
    *,
    limit: int | None = None,
    from_snapshots: bool = False,
    timeout_seconds: float = LIVE_TIMEOUT_SECONDS,
    poll_seconds: float = LIVE_POLL_SECONDS,
    concurrency_per_runner: int = LIVE_CONCURRENCY_PER_RUNNER,
) -> dict[str, int]:
    """Capture fresh listing HTML through active Dev9 runners and snapshot its count."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    if concurrency_per_runner < 1:
        raise ValueError("concurrency_per_runner must be at least 1")

    with connect(workspace) as conn:
        targets = _live_targets(
            conn,
            workspace,
            from_snapshots=from_snapshots,
            limit=limit,
        )

    runners = _active_live_runners(workspace)
    mode = "live_from_snapshots" if from_snapshots else "live"
    with connect(workspace) as conn:
        run_id = _start_snapshot_run(conn, mode=mode, locations_selected=len(targets))

    selected = len(targets)
    processed = 0
    fetched = 0
    created = 0
    failed = 0
    changed = 0
    queued = 0
    pending = deque(enumerate(targets, start=1))
    active: dict[str, dict[str, Any]] = {}
    active_by_runner = {runner["runner_id"]: 0 for runner in runners}

    try:
        configured = []
        for runner in runners:
            try:
                configure_capture(
                    "listing",
                    check_ms=LIVE_CHECK_MS,
                    stable_checks=LIVE_STABLE_CHECKS,
                    max_wait_ms=LIVE_MAX_WAIT_MS,
                    controller_poll_ms=LIVE_CONTROLLER_POLL_MS,
                    server_url=runner["controller_url"],
                )
            except (OSError, ValueError):
                continue
            configured.append(runner)
        runners = configured
        if not runners:
            raise RuntimeError(
                "Live updater requires active runners. Run:\n"
                f'zocdoc-collector --workspace "{workspace.root}" runners start'
            )

        while pending or active:
            for token, item in list(active.items()):
                path: Path = item["path"]
                elapsed = time.monotonic() - item["started"]
                if not path.exists() and elapsed < timeout_seconds:
                    continue

                active.pop(token)
                active_by_runner[item["runner_id"]] -= 1
                processed += 1
                if not path.exists():
                    failed += 1
                    continue

                fetched += 1
                try:
                    html = path.read_text(encoding="utf-8", errors="replace")
                    parsed = parse_verified_provider_count(html)
                    if parsed is None:
                        failed += 1
                        continue

                    target: UpdateTarget = item["target"]
                    snapshot_target = UpdateTarget(
                        specialty=target.specialty or parsed.specialty,
                        specialty_slug=target.specialty_slug,
                        location=target.location or parsed.location,
                        location_url=target.location_url,
                        verified_provider_count=parsed.verified_provider_count,
                    )
                    checked_at = now_iso()
                    with connect(workspace) as conn:
                        previous = _latest_snapshot(conn, snapshot_target)
                        if (
                            previous is not None
                            and int(previous["verified_provider_count"])
                            != parsed.verified_provider_count
                        ):
                            changed += 1
                            queued += _queue_count_change(conn, snapshot_target, checked_at)
                        _insert_snapshot(conn, snapshot_target, checked_at)
                        _update_live_page_count(
                            conn,
                            snapshot_target,
                            parsed.verified_provider_count,
                            checked_at,
                        )
                        conn.commit()
                    created += 1
                except (OSError, sqlite3.Error, ValueError):
                    failed += 1
                finally:
                    try:
                        path.unlink(missing_ok=True)
                    except (OSError, ValueError):
                        pass

            for runner in runners:
                runner_id = runner["runner_id"]
                while active_by_runner[runner_id] < concurrency_per_runner and pending:
                    sequence, target = pending.popleft()
                    token = f"snapshot-{run_id}-{sequence}-{uuid.uuid4().hex[:10]}"
                    path = workspace.updater_snapshot_dir / f"{token}.html"
                    try:
                        enqueue_url(
                            target.location_url,
                            server_url=runner["controller_url"],
                            capture_token=token,
                        )
                    except OSError:
                        processed += 1
                        failed += 1
                        continue
                    active[token] = {
                        "target": target,
                        "path": path,
                        "runner_id": runner_id,
                        "started": time.monotonic(),
                    }
                    active_by_runner[runner_id] += 1

            if active:
                time.sleep(max(0.01, poll_seconds))
    finally:
        failed += len(pending)
        processed += len(pending)
        pending.clear()
        failed += len(active)
        processed += len(active)
        active.clear()
        with connect(workspace) as conn:
            _finish_snapshot_run(
                conn,
                run_id,
                locations_processed=processed,
                success_count=created,
                failed_count=failed,
            )

    return {
        "locations_selected": selected,
        "browser_fetched": fetched,
        "snapshots_created": created,
        "failed": failed,
        "changed": changed,
        "refresh_queue": queued,
    }


def check_location_snapshots(workspace: Workspace) -> dict[str, int]:
    with connect(workspace) as conn:
        targets = load_update_targets(conn, workspace)
        checked_at = now_iso()
        checked = 0
        unchanged = 0
        changed = 0
        failed = 0
        queued = 0

        for target in targets:
            if target.verified_provider_count is None:
                failed += 1
                continue
            checked += 1
            previous = _latest_snapshot(conn, target)
            if previous is None:
                _insert_snapshot(conn, target, checked_at)
                unchanged += 1
                continue
            if int(previous["verified_provider_count"]) == int(target.verified_provider_count):
                _insert_snapshot(conn, target, checked_at)
                unchanged += 1
                continue
            changed += 1
            queued += _queue_count_change(conn, target, checked_at)
            _insert_snapshot(conn, target, checked_at)

        conn.commit()
    return {
        "locations_checked": checked,
        "unchanged": unchanged,
        "changed": changed,
        "refresh_queue": queued,
        "failed": failed,
    }


def load_refresh_queue(
    workspace: Workspace,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with connect(workspace) as conn:
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status=?"
            params.append(status)
        params.append(int(limit))
        rows = conn.execute(
            f"""
            SELECT id, specialty, specialty_slug, location, location_url, reason, status, created_at, processed_at
            FROM refresh_queue
            {where}
            ORDER BY id
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]
