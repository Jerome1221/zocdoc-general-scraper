from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import connect
from .listing import classify_listing_capture, parse_page
from .profile import parse_profile_rows
from .queue import _ingest_listing, mark_profile_saved
from .saver import configure_capture, enqueue_url
from .trace import find_profile_html, trace_rows_for_doctor
from .updater import (
    LIVE_CHECK_MS,
    LIVE_CONTROLLER_POLL_MS,
    LIVE_MAX_WAIT_MS,
    LIVE_POLL_SECONDS,
    LIVE_STABLE_CHECKS,
    LIVE_TIMEOUT_SECONDS,
    UpdateTarget,
    _active_live_runners,
    load_update_targets,
)
from .urls import base_location_url, normalize_url, page_number, safe_slug
from .utils import now_iso
from .workspace import Workspace

MAX_PAGES_PER_LOCATION = 500
DEFAULT_REMOVAL_CONFIRMATIONS = 2
DEFAULT_PROFILE_REFRESH_DAYS = 30
DEFAULT_CAPTURE_RETRIES = 2
LOCK_NAME = "daily-provider-sync"


@dataclass
class ListingCapture:
    url: str
    path: Path
    html: str
    parsed: dict[str, Any]


@dataclass
class LocationState:
    target: UpdateTarget
    pending: deque[str] = field(default_factory=deque)
    queued: set[str] = field(default_factory=set)
    active_urls: set[str] = field(default_factory=set)
    captures: dict[str, ListingCapture] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    max_page: int = 1
    started_at: str = field(default_factory=now_iso)
    error: str = ""
    finished: bool = False

    def add_page(self, url: str) -> None:
        normalized = normalize_url(url)
        if base_location_url(normalized) != base_location_url(self.target.location_url):
            return
        number = page_number(normalized)
        if number > MAX_PAGES_PER_LOCATION:
            self.error = f"pagination exceeded safety limit ({MAX_PAGES_PER_LOCATION} pages)"
            return
        self.max_page = max(self.max_page, number)
        if normalized in self.queued or normalized in self.active_urls or normalized in self.captures:
            return
        self.pending.append(normalized)
        self.queued.add(normalized)

    def add_contiguous_pages(self) -> None:
        base = base_location_url(self.target.location_url)
        for number in range(1, self.max_page + 1):
            self.add_page(base if number == 1 else f"{base}/{number}")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@contextmanager
def daily_sync_lock(
    workspace: Workspace,
    *,
    stale_after_hours: float = 24.0,
) -> Iterator[str]:
    """Hold the single-writer daily updater lock for the duration of a run."""
    token = uuid.uuid4().hex
    acquired_at = now_iso()
    with connect(workspace) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT lock_token,acquired_at,owner_pid FROM daily_sync_lock WHERE lock_name=?",
            (LOCK_NAME,),
        ).fetchone()
        if row is not None:
            previous = _parse_timestamp(row["acquired_at"])
            cutoff = datetime.now(UTC) - timedelta(hours=max(0.1, stale_after_hours))
            if previous is None or previous >= cutoff:
                conn.rollback()
                raise RuntimeError(
                    "A daily updater is already running "
                    f"(PID {row['owner_pid']}, acquired {row['acquired_at']})."
                )
        conn.execute(
            """
            INSERT INTO daily_sync_lock(lock_name,lock_token,acquired_at,owner_pid)
            VALUES (?,?,?,?)
            ON CONFLICT(lock_name) DO UPDATE SET
                lock_token=excluded.lock_token,
                acquired_at=excluded.acquired_at,
                owner_pid=excluded.owner_pid
            """,
            (LOCK_NAME, token, acquired_at, os.getpid()),
        )
        conn.commit()
    try:
        yield token
    finally:
        with connect(workspace) as conn:
            conn.execute(
                "DELETE FROM daily_sync_lock WHERE lock_name=? AND lock_token=?",
                (LOCK_NAME, token),
            )
            conn.commit()


def select_daily_targets(
    workspace: Workspace,
    *,
    limit: int | None = None,
    from_snapshots: bool = False,
    specialties: tuple[str, ...] = (),
) -> list[UpdateTarget]:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1")
    with connect(workspace) as conn:
        targets = load_update_targets(conn, workspace)
        snapshot_keys: set[tuple[str, str]] = set()
        if from_snapshots:
            snapshot_keys = {
                (row["specialty_slug"] or "", normalize_url(row["location_url"]))
                for row in conn.execute(
                    "SELECT DISTINCT specialty_slug,location_url FROM location_snapshots"
                ).fetchall()
            }

    selectors = {value.strip().lower() for value in specialties if value.strip()}
    selected: list[UpdateTarget] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        key = (target.specialty_slug, normalize_url(target.location_url))
        if key in seen:
            continue
        if from_snapshots and key not in snapshot_keys:
            continue
        if selectors and target.specialty_slug.lower() not in selectors and target.specialty.lower() not in selectors:
            continue
        seen.add(key)
        selected.append(target)
    selected.sort(key=lambda item: (item.specialty_slug, item.location, item.location_url))
    return selected[:limit] if limit is not None else selected


def _start_run(
    workspace: Workspace,
    targets: list[UpdateTarget],
    *,
    scope: str,
) -> int:
    with connect(workspace) as conn:
        cursor = conn.execute(
            """
            INSERT INTO daily_sync_runs(run_uuid,started_at,status,scope,locations_selected)
            VALUES (?,?,'running',?,?)
            """,
            (uuid.uuid4().hex, now_iso(), scope, len(targets)),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO daily_sync_locations(
                run_id,specialty,specialty_slug,location,location_url,status
            ) VALUES (?,?,?,?,?,'pending')
            """,
            [
                (
                    run_id,
                    target.specialty,
                    target.specialty_slug,
                    target.location,
                    normalize_url(target.location_url),
                )
                for target in targets
            ],
        )
        conn.commit()
        return run_id


def _mark_location_running(workspace: Workspace, run_id: int, target: UpdateTarget) -> None:
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE daily_sync_locations
            SET status='running',started_at=?
            WHERE run_id=? AND specialty_slug=? AND location_url=?
            """,
            (now_iso(), run_id, target.specialty_slug, normalize_url(target.location_url)),
        )
        conn.commit()


def _mark_location_failed(
    workspace: Workspace,
    run_id: int,
    state: LocationState,
    error: str,
) -> None:
    state.error = error
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE daily_sync_locations
            SET status='failed',pages_fetched=?,expected_pages=?,completed_at=?,error=?
            WHERE run_id=? AND specialty_slug=? AND location_url=?
            """,
            (
                len(state.captures),
                state.max_page,
                now_iso(),
                error,
                run_id,
                state.target.specialty_slug,
                normalize_url(state.target.location_url),
            ),
        )
        conn.commit()


def _event(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    doctor_url: str,
    change_type: str,
    specialty_slug: str = "",
    location_url: str = "",
    old_value: str = "",
    new_value: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO provider_change_events(
            run_id,specialty_slug,location_url,doctor_url,change_type,
            old_value,new_value,created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            specialty_slug,
            normalize_url(location_url) if location_url else "",
            normalize_url(doctor_url),
            change_type,
            old_value,
            new_value,
            now_iso(),
        ),
    )


def _validate_complete_location(state: LocationState) -> None:
    if state.error:
        raise RuntimeError(state.error)
    if not state.captures:
        raise RuntimeError("no listing pages were captured")
    expected = set(range(1, state.max_page + 1))
    actual = {page_number(url) for url in state.captures}
    missing = sorted(expected.difference(actual))
    if missing:
        raise RuntimeError(f"incomplete pagination; missing page(s): {missing}")
    for capture in state.captures.values():
        classification, reason = classify_listing_capture(capture.html, capture.parsed)
        if classification not in {"valid", "valid_empty"}:
            raise RuntimeError(f"invalid capture for {capture.url}: {reason}")
        if page_number(capture.url) < state.max_page and not capture.parsed["doctors"]:
            raise RuntimeError(f"non-final page contains no providers: {capture.url}")


def _ensure_target_pages(conn: sqlite3.Connection, state: LocationState) -> None:
    stamp = now_iso()
    base = base_location_url(state.target.location_url)
    for url in sorted(state.captures, key=lambda value: (page_number(value), value)):
        number = page_number(url)
        conn.execute(
            """
            INSERT INTO pages(
                url,base_location_url,state_group,location,page_no,source,status,
                discovered_at,specialty_name,specialty_slug
            ) VALUES (?,?,?,?,?,?, 'pending', ?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                base_location_url=excluded.base_location_url,
                location=CASE WHEN excluded.location<>'' THEN excluded.location ELSE pages.location END,
                page_no=excluded.page_no,
                source=excluded.source,
                specialty_name=CASE
                    WHEN excluded.specialty_name<>'' THEN excluded.specialty_name ELSE pages.specialty_name END,
                specialty_slug=CASE
                    WHEN excluded.specialty_slug<>'' THEN excluded.specialty_slug ELSE pages.specialty_slug END
            """,
            (
                normalize_url(url),
                base,
                "",
                state.target.location,
                number,
                "seed" if number == 1 else "pagination",
                stamp,
                state.target.specialty,
                state.target.specialty_slug,
            ),
        )
    conn.commit()


def _trace_payloads(
    conn: sqlite3.Connection,
    current_pages: set[str],
) -> dict[str, str]:
    if not current_pages:
        return {}
    placeholders = ",".join("?" for _ in current_pages)
    rows = conn.execute(
        f"""
        SELECT * FROM listing_provider_trace
        WHERE listing_url IN ({placeholders})
        ORDER BY doctor_url,listing_page_no,card_index
        """,
        sorted(current_pages),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[normalize_url(row["doctor_url"])].append(dict(row))
    return {
        doctor_url: json.dumps(values, ensure_ascii=False, default=str)
        for doctor_url, values in grouped.items()
    }


def _commit_location(
    workspace: Workspace,
    run_id: int,
    state: LocationState,
    *,
    removal_confirmations: int,
) -> dict[str, int]:
    _validate_complete_location(state)
    target = state.target
    base = base_location_url(target.location_url)
    current_pages = set(state.captures)
    with connect(workspace) as conn:
        previous_pages = {
            normalize_url(row["url"])
            for row in conn.execute(
                "SELECT url FROM pages WHERE base_location_url=?",
                (base,),
            ).fetchall()
        }
        _ensure_target_pages(conn, state)

        for url in sorted(current_pages, key=lambda value: (page_number(value), value)):
            capture = state.captures[url]
            parsed = _ingest_listing(
                conn,
                workspace,
                url,
                capture.path,
                write_outputs=False,
                zero_link_max_retries=0,
                keep_listing_html=False,
            )
            if parsed.get("queue_status") not in {"saved", "valid_empty"}:
                raise RuntimeError(
                    f"canonical ingest rejected {url}: {parsed.get('validation_reason') or parsed.get('queue_status')}"
                )

        retired_pages = previous_pages.difference(current_pages)
        if retired_pages:
            placeholders = ",".join("?" for _ in retired_pages)
            conn.execute(
                f"UPDATE pages SET status='retired',error='' WHERE url IN ({placeholders})",
                sorted(retired_pages),
            )
            conn.execute(
                f"DELETE FROM listing_provider_trace WHERE listing_url IN ({placeholders})",
                sorted(retired_pages),
            )

        now = now_iso()
        provider_pages: dict[str, tuple[str, int, str]] = {}
        for url, capture in state.captures.items():
            for doctor in capture.parsed["doctors"]:
                doctor_url = normalize_url(doctor["doctor_url"])
                old = provider_pages.get(doctor_url)
                candidate = (url, page_number(url), doctor.get("doctor_name", ""))
                if old is None or candidate[1] < old[1]:
                    provider_pages[doctor_url] = candidate

        for doctor_url, (listing_url, listing_page_no, doctor_name) in provider_pages.items():
            conn.execute(
                """
                INSERT INTO daily_provider_snapshots(
                    run_id,specialty_slug,location_url,doctor_url,doctor_name,
                    listing_url,listing_page_no,seen_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    target.specialty_slug,
                    base,
                    doctor_url,
                    doctor_name,
                    listing_url,
                    listing_page_no,
                    now,
                ),
            )

        existing = {
            normalize_url(row["doctor_url"]): row
            for row in conn.execute(
                """
                SELECT * FROM provider_location_memberships
                WHERE specialty_slug=? AND location_url=?
                """,
                (target.specialty_slug, base),
            ).fetchall()
        }
        payloads = _trace_payloads(conn, current_pages)
        added = 0
        reactivated = 0
        removal_pending = 0
        removed = 0

        for doctor_url, (_, _, doctor_name) in provider_pages.items():
            prior = existing.get(doctor_url)
            if prior is None:
                added += 1
                change_type = "added"
                conn.execute(
                    """
                    INSERT INTO provider_location_memberships(
                        specialty,specialty_slug,location,location_url,doctor_url,
                        doctor_name,is_active,first_seen_at,last_seen_at,inactive_at,
                        missing_streak,last_seen_run_id,listing_data_json
                    ) VALUES (?,?,?,?,?,?,1,?,?,NULL,0,?,?)
                    """,
                    (
                        target.specialty,
                        target.specialty_slug,
                        target.location,
                        base,
                        doctor_url,
                        doctor_name,
                        now,
                        now,
                        run_id,
                        payloads.get(doctor_url, "[]"),
                    ),
                )
            elif not int(prior["is_active"]):
                reactivated += 1
                change_type = "reactivated"
                conn.execute(
                    """
                    UPDATE provider_location_memberships
                    SET specialty=?,location=?,doctor_name=CASE WHEN ?<>'' THEN ? ELSE doctor_name END,
                        is_active=1,last_seen_at=?,inactive_at=NULL,missing_streak=0,
                        last_seen_run_id=?,listing_data_json=?
                    WHERE specialty_slug=? AND location_url=? AND doctor_url=?
                    """,
                    (
                        target.specialty,
                        target.location,
                        doctor_name,
                        doctor_name,
                        now,
                        run_id,
                        payloads.get(doctor_url, "[]"),
                        target.specialty_slug,
                        base,
                        doctor_url,
                    ),
                )
            else:
                change_type = ""
                conn.execute(
                    """
                    UPDATE provider_location_memberships
                    SET specialty=?,location=?,doctor_name=CASE WHEN ?<>'' THEN ? ELSE doctor_name END,
                        last_seen_at=?,missing_streak=0,last_seen_run_id=?,listing_data_json=?
                    WHERE specialty_slug=? AND location_url=? AND doctor_url=?
                    """,
                    (
                        target.specialty,
                        target.location,
                        doctor_name,
                        doctor_name,
                        now,
                        run_id,
                        payloads.get(doctor_url, "[]"),
                        target.specialty_slug,
                        base,
                        doctor_url,
                    ),
                )
            if change_type:
                _event(
                    conn,
                    run_id=run_id,
                    specialty_slug=target.specialty_slug,
                    location_url=base,
                    doctor_url=doctor_url,
                    change_type=change_type,
                    old_value="inactive" if change_type == "reactivated" else "absent",
                    new_value="active",
                )

            conn.execute(
                """
                INSERT INTO canonical_provider_records(
                    doctor_url,doctor_name,is_active,first_seen_at,last_seen_at,last_seen_run_id
                ) VALUES (?,?,1,?,?,?)
                ON CONFLICT(doctor_url) DO UPDATE SET
                    doctor_name=CASE WHEN excluded.doctor_name<>'' THEN excluded.doctor_name
                        ELSE canonical_provider_records.doctor_name END,
                    is_active=1,last_seen_at=excluded.last_seen_at,inactive_at=NULL,
                    last_seen_run_id=excluded.last_seen_run_id
                """,
                (doctor_url, doctor_name, now, now, run_id),
            )

        missing_urls = set(existing).difference(provider_pages)
        for doctor_url in sorted(missing_urls):
            prior = existing[doctor_url]
            if not int(prior["is_active"]):
                continue
            streak = int(prior["missing_streak"] or 0) + 1
            if streak >= removal_confirmations:
                removed += 1
                conn.execute(
                    """
                    UPDATE provider_location_memberships
                    SET is_active=0,inactive_at=?,missing_streak=?
                    WHERE specialty_slug=? AND location_url=? AND doctor_url=?
                    """,
                    (now, streak, target.specialty_slug, base, doctor_url),
                )
                _event(
                    conn,
                    run_id=run_id,
                    specialty_slug=target.specialty_slug,
                    location_url=base,
                    doctor_url=doctor_url,
                    change_type="removed",
                    old_value="active",
                    new_value="inactive",
                )
            else:
                removal_pending += 1
                conn.execute(
                    """
                    UPDATE provider_location_memberships SET missing_streak=?
                    WHERE specialty_slug=? AND location_url=? AND doctor_url=?
                    """,
                    (streak, target.specialty_slug, base, doctor_url),
                )
                _event(
                    conn,
                    run_id=run_id,
                    specialty_slug=target.specialty_slug,
                    location_url=base,
                    doctor_url=doctor_url,
                    change_type="removal_pending",
                    old_value=str(streak - 1),
                    new_value=str(streak),
                )

        for doctor_url in missing_urls:
            active_elsewhere = conn.execute(
                """
                SELECT 1 FROM provider_location_memberships
                WHERE doctor_url=? AND is_active=1 LIMIT 1
                """,
                (doctor_url,),
            ).fetchone()
            if active_elsewhere is None:
                conn.execute(
                    """
                    UPDATE canonical_provider_records
                    SET is_active=0,inactive_at=? WHERE doctor_url=?
                    """,
                    (now, doctor_url),
                )

        provider_hash = hashlib.sha256(
            "\n".join(sorted(provider_pages)).encode("utf-8")
        ).hexdigest()
        advertised_values = [
            capture.parsed.get("advertised_total")
            for capture in state.captures.values()
            if capture.parsed.get("advertised_total") is not None
        ]
        advertised_total = advertised_values[0] if advertised_values else None
        conn.execute(
            """
            UPDATE daily_sync_locations
            SET status='complete',expected_pages=?,pages_fetched=?,advertised_total=?,
                providers_seen=?,provider_set_hash=?,providers_added=?,
                providers_reactivated=?,removals_pending=?,providers_removed=?,
                completed_at=?,error=''
            WHERE run_id=? AND specialty_slug=? AND location_url=?
            """,
            (
                state.max_page,
                len(state.captures),
                advertised_total,
                len(provider_pages),
                provider_hash,
                added,
                reactivated,
                removal_pending,
                removed,
                now_iso(),
                run_id,
                target.specialty_slug,
                base,
            ),
        )
        conn.commit()
    return {
        "providers_seen": len(provider_pages),
        "providers_added": added,
        "providers_reactivated": reactivated,
        "removals_pending": removal_pending,
        "providers_removed": removed,
    }


def _retry_or_fail_listing(
    state: LocationState,
    url: str,
    *,
    capture_retries: int,
    reason: str,
) -> None:
    if state.attempts[url] <= capture_retries:
        state.pending.append(url)
    else:
        state.error = f"{url}: {reason} after {state.attempts[url]} attempt(s)"
        state.pending.clear()


def _cleanup_state_files(state: LocationState) -> None:
    for capture in state.captures.values():
        try:
            capture.path.unlink(missing_ok=True)
        except OSError:
            pass


def collect_and_apply_locations(
    workspace: Workspace,
    run_id: int,
    targets: list[UpdateTarget],
    *,
    runner_ids: tuple[str, ...] = (),
    concurrency_per_runner: int = 4,
    timeout_seconds: float = LIVE_TIMEOUT_SECONDS,
    poll_seconds: float = LIVE_POLL_SECONDS,
    capture_retries: int = DEFAULT_CAPTURE_RETRIES,
    removal_confirmations: int = DEFAULT_REMOVAL_CONFIRMATIONS,
) -> None:
    if concurrency_per_runner < 1:
        raise ValueError("concurrency_per_runner must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    if capture_retries < 0:
        raise ValueError("capture_retries cannot be negative")
    if removal_confirmations < 1:
        raise ValueError("removal_confirmations must be at least 1")

    runners = _active_live_runners(workspace)
    requested = {value.strip() for value in runner_ids if value.strip()}
    if requested:
        runners = [runner for runner in runners if runner["runner_id"] in requested]
        missing = requested.difference({runner["runner_id"] for runner in runners})
        if missing:
            raise RuntimeError(f"Requested runner(s) are not active: {', '.join(sorted(missing))}")
    configured: list[dict[str, Any]] = []
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
    if not configured:
        raise RuntimeError("No active runner could be configured for listing capture.")
    runners = configured

    states = [LocationState(target=target) for target in targets]
    for state in states:
        state.add_page(state.target.location_url)
        _mark_location_running(workspace, run_id, state.target)

    active: dict[str, dict[str, Any]] = {}
    active_by_runner = {runner["runner_id"]: 0 for runner in runners}
    schedule_cursor = 0

    while any(not state.finished for state in states):
        if workspace.blocked_flag.exists():
            for state in states:
                if not state.finished and not state.error:
                    state.error = "restriction detected by Chrome saver"
                    state.pending.clear()
        now = time.monotonic()
        for token, item in list(active.items()):
            state: LocationState = item["state"]
            path: Path = item["path"]
            url: str = item["url"]
            elapsed = now - item["started"]
            if not path.exists() and elapsed < timeout_seconds:
                continue

            active.pop(token)
            active_by_runner[item["runner_id"]] -= 1
            state.active_urls.discard(url)
            if state.error:
                path.unlink(missing_ok=True)
                continue
            if not path.exists():
                _retry_or_fail_listing(
                    state,
                    url,
                    capture_retries=capture_retries,
                    reason=f"capture timed out after {timeout_seconds:g}s",
                )
                continue
            try:
                html = path.read_text(encoding="utf-8", errors="replace")
                parsed = parse_page(html, url)
                classification, reason = classify_listing_capture(html, parsed)
                if classification not in {"valid", "valid_empty"}:
                    path.unlink(missing_ok=True)
                    _retry_or_fail_listing(
                        state,
                        url,
                        capture_retries=capture_retries,
                        reason=reason,
                    )
                    continue
                state.captures[url] = ListingCapture(url=url, path=path, html=html, parsed=parsed)
                for discovered in parsed["pagination_urls"]:
                    state.add_page(discovered)
                state.add_contiguous_pages()
            except (OSError, ValueError) as exc:
                path.unlink(missing_ok=True)
                _retry_or_fail_listing(
                    state,
                    url,
                    capture_retries=capture_retries,
                    reason=str(exc),
                )

        for state in states:
            if state.finished or state.active_urls or state.pending:
                continue
            try:
                if state.error:
                    raise RuntimeError(state.error)
                result = _commit_location(
                    workspace,
                    run_id,
                    state,
                    removal_confirmations=removal_confirmations,
                )
                print(
                    f"SYNCED | {state.target.specialty or state.target.specialty_slug} | "
                    f"{state.target.location} | pages={len(state.captures)} "
                    f"providers={result['providers_seen']} added={result['providers_added']} "
                    f"removed={result['providers_removed']}"
                )
            except Exception as exc:  # keep other locations moving after one bad crawl
                _mark_location_failed(workspace, run_id, state, str(exc))
                print(f"FAILED | {state.target.location_url} | {exc}")
            finally:
                _cleanup_state_files(state)
                state.finished = True

        made_schedule = True
        while made_schedule:
            made_schedule = False
            for runner in runners:
                runner_id = runner["runner_id"]
                if active_by_runner[runner_id] >= concurrency_per_runner:
                    continue
                candidate: LocationState | None = None
                for offset in range(len(states)):
                    index = (schedule_cursor + offset) % len(states) if states else 0
                    possible = states[index]
                    if not possible.finished and not possible.error and possible.pending:
                        candidate = possible
                        schedule_cursor = (index + 1) % len(states)
                        break
                if candidate is None:
                    continue
                url = candidate.pending.popleft()
                candidate.attempts[url] += 1
                token = f"daily-{run_id}-{uuid.uuid4().hex[:16]}"
                path = workspace.updater_snapshot_dir / f"{token}.html"
                try:
                    enqueue_url(
                        url,
                        server_url=runner["controller_url"],
                        capture_token=token,
                    )
                except (OSError, ValueError) as exc:
                    _retry_or_fail_listing(
                        candidate,
                        url,
                        capture_retries=capture_retries,
                        reason=f"enqueue failed: {exc}",
                    )
                    continue
                candidate.active_urls.add(url)
                active[token] = {
                    "state": candidate,
                    "url": url,
                    "path": path,
                    "runner_id": runner_id,
                    "started": time.monotonic(),
                }
                active_by_runner[runner_id] += 1
                made_schedule = True

        if active:
            time.sleep(max(0.01, poll_seconds))
        elif any(not state.finished for state in states):
            time.sleep(0.01)


def _profile_file_age_days(path: Path) -> float:
    return max(0.0, (time.time() - path.stat().st_mtime) / 86400.0)


def _profile_age_days(path: Path | None, updated_at: str | None) -> float:
    updated = _parse_timestamp(updated_at)
    if updated is not None:
        return max(0.0, (datetime.now(UTC) - updated).total_seconds() / 86400.0)
    if path is not None:
        return _profile_file_age_days(path)
    return float("inf")


def _profile_export_row(row: dict[str, str], doctor_url: str) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        key: value for key, value in row.items() if key and value is not None
    }
    normalized["profile_url"] = doctor_url
    for key in ("hospital_affiliations", "locations"):
        value = normalized.get(key, "")
        try:
            normalized[key] = json.loads(value) if value else []
        except (TypeError, json.JSONDecodeError):
            normalized[key] = []
    for key in (
        "accepts_new_patients",
        "offers_telemedicine",
        "only_sees_children",
    ):
        value = str(normalized.get(key, "")).strip().lower()
        if value in {"true", "1", "yes"}:
            normalized[key] = True
        elif value in {"false", "0", "no"}:
            normalized[key] = False
        else:
            normalized[key] = None
    value = str(normalized.get("num_locations", "")).strip()
    normalized["num_locations"] = int(value) if value.isdigit() else len(normalized["locations"])
    return normalized


def migrate_profiles_from_export(
    workspace: Workspace,
    *,
    input_path: Path | None = None,
) -> dict[str, Any]:
    """Import existing structured profiles without reopening provider pages."""
    source = (input_path or (workspace.output_dir / "unique_doctors.csv")).resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    with connect(workspace) as conn:
        canonical = {
            normalize_url(row["doctor_url"]): bool(row["profile_sha256"])
            for row in conn.execute(
                "SELECT doctor_url,profile_sha256 FROM canonical_provider_records"
            ).fetchall()
        }

    source_updated_at = datetime.fromtimestamp(source.stat().st_mtime, tz=UTC).isoformat()
    diagnostics_json = json.dumps(
        {"migrated_from_export": True, "source": source.name},
        ensure_ascii=False,
        sort_keys=True,
    )
    scanned = 0
    imported = 0
    already_present = 0
    invalid = 0
    unmatched = 0
    seen: set[str] = set()
    batch: list[tuple[Any, ...]] = []

    def flush() -> None:
        nonlocal imported
        if not batch:
            return
        with connect(workspace) as conn:
            conn.executemany(
                """
                UPDATE canonical_provider_records
                SET doctor_name=CASE WHEN ?<>'' THEN ? ELSE doctor_name END,
                    profile_file=?,profile_sha256=?,profile_data_json=?,
                    profile_diagnostics_json=?,profile_updated_at=?
                WHERE doctor_url=? AND COALESCE(profile_sha256,'')=''
                """,
                batch,
            )
            conn.commit()
        imported += len(batch)
        batch.clear()

    with source.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"profile_url", "full_name"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(
                f"{source.name} must contain the columns: {', '.join(sorted(required))}"
            )
        for row in reader:
            scanned += 1
            doctor_url = normalize_url(row.get("profile_url") or "")
            if not doctor_url or doctor_url in seen:
                invalid += 1
                continue
            seen.add(doctor_url)
            if doctor_url not in canonical:
                unmatched += 1
                continue
            if canonical[doctor_url]:
                already_present += 1
                continue
            doctor_name = " ".join((row.get("full_name") or "").split())
            if not doctor_name:
                invalid += 1
                continue
            profile = _profile_export_row(row, doctor_url)
            profile_json = json.dumps(
                [profile],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            digest = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
            batch.append(
                (
                    doctor_name,
                    doctor_name,
                    source.name,
                    digest,
                    profile_json,
                    diagnostics_json,
                    source_updated_at,
                    doctor_url,
                )
            )
            canonical[doctor_url] = True
            if len(batch) >= 500:
                flush()
        flush()

    return {
        "source": str(source),
        "scanned": scanned,
        "imported": imported,
        "already_present": already_present,
        "invalid": invalid,
        "unmatched": unmatched,
    }


def _store_profile_html(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    doctor_url: str,
    path: Path,
    html: str,
) -> tuple[bool, str]:
    trace_rows = trace_rows_for_doctor(conn, doctor_url)
    rows, diagnostics = parse_profile_rows(doctor_url, html, trace_rows=trace_rows)
    if not rows or not any(str(row.get("full_name") or "").strip() for row in rows):
        return False, "profile parser found no provider name"
    text = " ".join(html.lower().split())
    if any(marker in text for marker in ("verify you are human", "access denied", "unusual traffic")):
        return False, "restriction-like profile capture"
    profile_json = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
    prior = conn.execute(
        "SELECT profile_sha256 FROM canonical_provider_records WHERE doctor_url=?",
        (doctor_url,),
    ).fetchone()
    previous_digest = prior["profile_sha256"] if prior else ""
    full_name = str(rows[0].get("full_name") or "")
    stamp = now_iso()
    conn.execute(
        """
        INSERT INTO canonical_provider_records(
            doctor_url,doctor_name,is_active,first_seen_at,last_seen_at,
            profile_file,profile_sha256,profile_data_json,profile_diagnostics_json,
            profile_updated_at,last_seen_run_id
        ) VALUES (?,?,1,?,?,?,?,?,?,?,?)
        ON CONFLICT(doctor_url) DO UPDATE SET
            doctor_name=CASE WHEN excluded.doctor_name<>'' THEN excluded.doctor_name
                ELSE canonical_provider_records.doctor_name END,
            profile_file=excluded.profile_file,
            profile_sha256=excluded.profile_sha256,
            profile_data_json=excluded.profile_data_json,
            profile_diagnostics_json=excluded.profile_diagnostics_json,
            profile_updated_at=excluded.profile_updated_at,
            last_seen_run_id=excluded.last_seen_run_id
        """,
        (
            doctor_url,
            full_name,
            stamp,
            stamp,
            path.name,
            digest,
            profile_json,
            json.dumps(diagnostics, ensure_ascii=False, default=str),
            stamp,
            run_id,
        ),
    )
    if previous_digest and previous_digest != digest:
        _event(
            conn,
            run_id=run_id,
            doctor_url=doctor_url,
            change_type="profile_changed",
            old_value=previous_digest,
            new_value=digest,
        )
    elif not previous_digest:
        _event(
            conn,
            run_id=run_id,
            doctor_url=doctor_url,
            change_type="profile_added",
            new_value=digest,
        )
    return True, ""


def prepare_profile_queue(
    workspace: Workspace,
    run_id: int,
    *,
    refresh_days: int = DEFAULT_PROFILE_REFRESH_DAYS,
) -> int:
    if refresh_days < 0:
        raise ValueError("profile refresh days cannot be negative")
    queued = 0
    with connect(workspace) as conn:
        providers = conn.execute(
            """
            SELECT s.doctor_url,MAX(s.doctor_name) doctor_name,
                   MAX(CASE WHEN e.change_type='reactivated' THEN 1 ELSE 0 END) reactivated
            FROM daily_provider_snapshots s
            LEFT JOIN provider_change_events e
              ON e.run_id=s.run_id AND e.doctor_url=s.doctor_url
            WHERE s.run_id=?
            GROUP BY s.doctor_url
            ORDER BY s.doctor_url
            """,
            (run_id,),
        ).fetchall()
        run = conn.execute(
            "SELECT scope FROM daily_sync_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if not providers and run is not None and run["scope"] == "bootstrap":
            providers = conn.execute(
                """
                SELECT doctor_url,MAX(doctor_name) doctor_name,0 reactivated
                FROM provider_location_memberships
                WHERE last_seen_run_id=? AND is_active=1
                GROUP BY doctor_url
                ORDER BY doctor_url
                """,
                (run_id,),
            ).fetchall()
        for provider in providers:
            doctor_url = normalize_url(provider["doctor_url"])
            path = find_profile_html(workspace, doctor_url)
            canonical = conn.execute(
                """
                SELECT profile_sha256,profile_updated_at
                FROM canonical_provider_records WHERE doctor_url=?
                """,
                (doctor_url,),
            ).fetchone()
            if path is not None and (canonical is None or not canonical["profile_sha256"]):
                html = path.read_text(encoding="utf-8", errors="replace")
                valid, _ = _store_profile_html(
                    conn,
                    run_id=run_id,
                    doctor_url=doctor_url,
                    path=path,
                    html=html,
                )
                if valid:
                    canonical = conn.execute(
                        """
                        SELECT profile_sha256,profile_updated_at
                        FROM canonical_provider_records WHERE doctor_url=?
                        """,
                        (doctor_url,),
                    ).fetchone()

            reason = ""
            if canonical is None or not canonical["profile_sha256"]:
                if path is None:
                    reason = "missing_profile"
                else:
                    reason = "unparsed_profile"
            elif int(provider["reactivated"] or 0):
                reason = "reactivated"
            elif refresh_days == 0 or _profile_age_days(
                path, canonical["profile_updated_at"]
            ) >= refresh_days:
                reason = "stale_profile"
            if not reason:
                continue
            conn.execute(
                """
                INSERT INTO daily_profile_queue(
                    run_id,doctor_url,doctor_name,reason,status,created_at
                ) VALUES (?,?,?,?, 'pending', ?)
                ON CONFLICT(run_id,doctor_url) DO UPDATE SET
                    doctor_name=excluded.doctor_name,reason=excluded.reason,
                    status=CASE WHEN daily_profile_queue.status='saved' THEN 'saved' ELSE 'pending' END,
                    error=''
                """,
                (run_id, doctor_url, provider["doctor_name"] or "", reason, now_iso()),
            )
            conn.execute(
                """
                INSERT INTO doctor_profiles(doctor_url,doctor_name,status,discovered_at)
                VALUES (?,?,'pending',?)
                ON CONFLICT(doctor_url) DO UPDATE SET
                    doctor_name=CASE WHEN excluded.doctor_name<>'' THEN excluded.doctor_name
                        ELSE doctor_profiles.doctor_name END,
                    status='pending',error='',last_error=''
                """,
                (doctor_url, provider["doctor_name"] or "", now_iso()),
            )
            queued += 1
        conn.commit()
    return queued


def _finish_profile_attempt(
    workspace: Workspace,
    run_id: int,
    doctor_url: str,
    token_path: Path,
) -> tuple[bool, str]:
    try:
        html = token_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, str(exc)
    destination = workspace.profile_dir / safe_slug(doctor_url)
    with connect(workspace) as conn:
        valid, reason = _store_profile_html(
            conn,
            run_id=run_id,
            doctor_url=doctor_url,
            path=destination,
            html=html,
        )
        if not valid:
            conn.rollback()
            return False, reason
        temporary = destination.with_suffix(".daily.tmp")
        try:
            temporary.write_bytes(token_path.read_bytes())
            temporary.replace(destination)
            token_path.unlink(missing_ok=True)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            conn.rollback()
            return False, f"could not install profile capture: {exc}"
        mark_profile_saved(conn, workspace, doctor_url, destination, write_output=False)
        conn.execute(
            """
            UPDATE daily_profile_queue
            SET status='saved',completed_at=?,error=''
            WHERE run_id=? AND doctor_url=?
            """,
            (now_iso(), run_id, doctor_url),
        )
        conn.commit()
    return True, ""


def collect_daily_profiles(
    workspace: Workspace,
    run_id: int,
    *,
    runner_ids: tuple[str, ...] = (),
    limit: int | None = None,
    concurrency_per_runner: int = 3,
    timeout_seconds: float = LIVE_TIMEOUT_SECONDS,
    poll_seconds: float = LIVE_POLL_SECONDS,
    capture_retries: int = DEFAULT_CAPTURE_RETRIES,
) -> dict[str, int]:
    if limit is not None and limit < 1:
        raise ValueError("profile limit must be at least 1")
    if capture_retries < 0:
        raise ValueError("capture_retries cannot be negative")
    with connect(workspace) as conn:
        sql = """
            SELECT * FROM daily_profile_queue
            WHERE run_id=? AND status IN ('pending','retry_pending')
            ORDER BY doctor_name,doctor_url
        """
        params: list[Any] = [run_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if not rows:
        return {"selected": 0, "saved": 0, "failed": 0}

    runners = _active_live_runners(workspace)
    requested = {value.strip() for value in runner_ids if value.strip()}
    if requested:
        runners = [runner for runner in runners if runner["runner_id"] in requested]
    if not runners:
        raise RuntimeError("No requested active runner is available for profile capture.")
    for runner in runners:
        configure_capture(
            "profile",
            check_ms=500,
            stable_checks=3,
            max_wait_ms=12000,
            controller_poll_ms=LIVE_CONTROLLER_POLL_MS,
            server_url=runner["controller_url"],
        )

    pending = deque(rows)
    active: dict[str, dict[str, Any]] = {}
    active_by_runner = {runner["runner_id"]: 0 for runner in runners}
    saved = 0
    failed = 0

    while pending or active:
        if workspace.blocked_flag.exists():
            message = "restriction detected by Chrome saver"
            with connect(workspace) as conn:
                conn.execute(
                    """
                    UPDATE daily_profile_queue
                    SET status='failed',error=?,completed_at=?
                    WHERE run_id=? AND status IN ('pending','retry_pending','opening')
                    """,
                    (message, now_iso(), run_id),
                )
                conn.commit()
            failed += len(pending) + len(active)
            for item in active.values():
                item["path"].unlink(missing_ok=True)
            pending.clear()
            active.clear()
            break
        now = time.monotonic()
        for token, item in list(active.items()):
            path: Path = item["path"]
            elapsed = now - item["started"]
            if not path.exists() and elapsed < timeout_seconds:
                continue
            active.pop(token)
            active_by_runner[item["runner_id"]] -= 1
            row = item["row"]
            doctor_url = row["doctor_url"]
            success = False
            reason = f"capture timed out after {timeout_seconds:g}s"
            if path.exists():
                success, reason = _finish_profile_attempt(
                    workspace, run_id, doctor_url, path
                )
            if success:
                saved += 1
                print(f"PROFILE SAVED | {doctor_url}")
                continue
            path.unlink(missing_ok=True)
            attempts = int(row.get("attempt_count") or 0) + 1
            row["attempt_count"] = attempts
            if attempts <= capture_retries:
                pending.append(row)
                status = "retry_pending"
            else:
                failed += 1
                status = "failed"
            with connect(workspace) as conn:
                conn.execute(
                    """
                    UPDATE daily_profile_queue
                    SET status=?,attempt_count=?,error=?,completed_at=CASE WHEN ?='failed' THEN ? ELSE NULL END
                    WHERE run_id=? AND doctor_url=?
                    """,
                    (status, attempts, reason, status, now_iso(), run_id, doctor_url),
                )
                conn.execute(
                    """
                    UPDATE doctor_profiles SET status=?,error=?,last_error=?,last_seen_at=?
                    WHERE doctor_url=?
                    """,
                    ("timeout" if status == "failed" else "pending", reason, reason, now_iso(), doctor_url),
                )
                conn.commit()

        for runner in runners:
            runner_id = runner["runner_id"]
            while active_by_runner[runner_id] < concurrency_per_runner and pending:
                row = pending.popleft()
                doctor_url = normalize_url(row["doctor_url"])
                token = f"daily-profile-{run_id}-{uuid.uuid4().hex[:12]}"
                path = workspace.updater_snapshot_dir / f"{token}.html"
                try:
                    enqueue_url(
                        doctor_url,
                        server_url=runner["controller_url"],
                        capture_token=token,
                    )
                except (OSError, ValueError) as exc:
                    row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
                    if row["attempt_count"] <= capture_retries:
                        pending.append(row)
                    else:
                        failed += 1
                    with connect(workspace) as conn:
                        conn.execute(
                            """
                            UPDATE daily_profile_queue
                            SET attempt_count=?,status=?,error=?
                            WHERE run_id=? AND doctor_url=?
                            """,
                            (
                                row["attempt_count"],
                                "retry_pending" if row["attempt_count"] <= capture_retries else "failed",
                                f"enqueue failed: {exc}",
                                run_id,
                                doctor_url,
                            ),
                        )
                        conn.commit()
                    continue
                with connect(workspace) as conn:
                    conn.execute(
                        """
                        UPDATE daily_profile_queue
                        SET status='opening',started_at=?,attempt_count=?
                        WHERE run_id=? AND doctor_url=?
                        """,
                        (now_iso(), int(row.get("attempt_count") or 0) + 1, run_id, doctor_url),
                    )
                    conn.commit()
                active[token] = {
                    "row": row,
                    "path": path,
                    "runner_id": runner_id,
                    "started": time.monotonic(),
                }
                active_by_runner[runner_id] += 1
        if active:
            time.sleep(max(0.01, poll_seconds))

    return {"selected": len(rows), "saved": saved, "failed": failed}


def _finalize_run(
    workspace: Workspace,
    run_id: int,
    *,
    error: str = "",
    database_published: bool = False,
    profile_collection_requested: bool = True,
) -> dict[str, Any]:
    with connect(workspace) as conn:
        if error:
            conn.execute(
                """
                UPDATE daily_sync_locations
                SET status='failed',completed_at=?,error=CASE WHEN error='' THEN ? ELSE error END
                WHERE run_id=? AND status IN ('pending','running')
                """,
                (now_iso(), error, run_id),
            )
            conn.commit()
        location = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
                COALESCE(SUM(pages_fetched),0) pages_fetched,
                COALESCE(SUM(providers_seen),0) providers_seen,
                COALESCE(SUM(providers_added),0) providers_added,
                COALESCE(SUM(providers_reactivated),0) providers_reactivated,
                COALESCE(SUM(removals_pending),0) removals_pending,
                COALESCE(SUM(providers_removed),0) providers_removed
            FROM daily_sync_locations WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        profile_counts = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT status,COUNT(*) n FROM daily_profile_queue WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        }
        completed = int(location["completed"] or 0)
        failed = int(location["failed"] or 0)
        profiles_saved = profile_counts.get("saved", 0)
        profiles_failed = profile_counts.get("failed", 0)
        profiles_pending = sum(
            count for status, count in profile_counts.items() if status not in {"saved", "failed"}
        )
        if completed == 0 and (failed or error):
            status = "failed"
        elif error or failed or profiles_failed or (profile_collection_requested and profiles_pending):
            status = "partial"
        else:
            status = "complete"
        conn.execute(
            """
            UPDATE daily_sync_runs
            SET completed_at=?,status=?,locations_completed=?,locations_failed=?,
                pages_fetched=?,providers_seen=?,providers_added=?,providers_reactivated=?,
                removals_pending=?,providers_removed=?,profiles_queued=?,profiles_saved=?,
                profiles_failed=?,database_published=?,error=?
            WHERE id=?
            """,
            (
                now_iso(),
                status,
                completed,
                failed,
                int(location["pages_fetched"] or 0),
                int(location["providers_seen"] or 0),
                int(location["providers_added"] or 0),
                int(location["providers_reactivated"] or 0),
                int(location["removals_pending"] or 0),
                int(location["providers_removed"] or 0),
                sum(profile_counts.values()),
                profiles_saved,
                profiles_failed,
                int(database_published),
                error,
                run_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM daily_sync_runs WHERE id=?", (run_id,)).fetchone()
        result = dict(row)
        result["profiles_pending"] = profiles_pending
        return result


def run_daily_sync(
    workspace: Workspace,
    *,
    limit: int | None = None,
    from_snapshots: bool = False,
    specialties: tuple[str, ...] = (),
    runner_ids: tuple[str, ...] = (),
    concurrency_per_runner: int = 4,
    timeout_seconds: float = LIVE_TIMEOUT_SECONDS,
    poll_seconds: float = LIVE_POLL_SECONDS,
    capture_retries: int = DEFAULT_CAPTURE_RETRIES,
    removal_confirmations: int = DEFAULT_REMOVAL_CONFIRMATIONS,
    profile_refresh_days: int = DEFAULT_PROFILE_REFRESH_DAYS,
    profile_limit: int | None = None,
    skip_profiles: bool = False,
    database_url: str | None = None,
) -> dict[str, Any]:
    targets = select_daily_targets(
        workspace,
        limit=limit,
        from_snapshots=from_snapshots,
        specialties=specialties,
    )
    if not targets:
        raise RuntimeError("No locations matched the daily updater selection.")
    scope = "limited" if limit is not None else ("snapshots" if from_snapshots else "all")
    run_id = 0
    with daily_sync_lock(workspace):
        run_id = _start_run(workspace, targets, scope=scope)
        try:
            collect_and_apply_locations(
                workspace,
                run_id,
                targets,
                runner_ids=runner_ids,
                concurrency_per_runner=concurrency_per_runner,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
                capture_retries=capture_retries,
                removal_confirmations=removal_confirmations,
            )
            prepare_profile_queue(
                workspace,
                run_id,
                refresh_days=profile_refresh_days,
            )
            if not skip_profiles:
                collect_daily_profiles(
                    workspace,
                    run_id,
                    runner_ids=runner_ids,
                    limit=profile_limit,
                    concurrency_per_runner=max(1, min(3, concurrency_per_runner)),
                    timeout_seconds=timeout_seconds,
                    poll_seconds=poll_seconds,
                    capture_retries=capture_retries,
                )
            result = _finalize_run(
                workspace,
                run_id,
                profile_collection_requested=not skip_profiles,
            )
            publish_url = database_url or os.environ.get("ZOCDOC_DATABASE_URL", "").strip()
            if publish_url:
                from .postgres_sync import publish_to_postgres

                try:
                    publish_to_postgres(workspace, publish_url, run_id=run_id)
                    result = _finalize_run(
                        workspace,
                        run_id,
                        database_published=True,
                        profile_collection_requested=not skip_profiles,
                    )
                except Exception as exc:
                    result = _finalize_run(
                        workspace,
                        run_id,
                        error=f"PostgreSQL publish failed: {exc}",
                        profile_collection_requested=not skip_profiles,
                    )
            return result
        except Exception as exc:
            if run_id:
                _finalize_run(
                    workspace,
                    run_id,
                    error=str(exc),
                    profile_collection_requested=not skip_profiles,
                )
            raise


def bootstrap_from_trace(workspace: Workspace) -> dict[str, int]:
    """Seed canonical memberships from existing trace rows without removing anything."""
    with daily_sync_lock(workspace):
        with connect(workspace) as conn:
            groups = conn.execute(
                """
                SELECT p.specialty_name,p.specialty_slug,p.location,p.base_location_url,
                       COUNT(DISTINCT t.doctor_url) providers
                FROM pages p
                JOIN listing_provider_trace t ON t.listing_url=p.url
                WHERE p.status IN ('saved','valid_empty')
                GROUP BY p.specialty_slug,p.base_location_url
                ORDER BY p.specialty_slug,p.base_location_url
                """
            ).fetchall()
            targets = [
                UpdateTarget(
                    specialty=row["specialty_name"] or row["specialty_slug"] or "",
                    specialty_slug=row["specialty_slug"] or "",
                    location=row["location"] or "",
                    location_url=row["base_location_url"],
                    verified_provider_count=None,
                )
                for row in groups
            ]
        run_id = _start_run(workspace, targets, scope="bootstrap")
        memberships = 0
        providers: set[str] = set()
        with connect(workspace) as conn:
            stamp = now_iso()
            for target in targets:
                base = base_location_url(target.location_url)
                rows = conn.execute(
                    """
                    SELECT t.*,p.page_no FROM listing_provider_trace t
                    JOIN pages p INDEXED BY idx_pages_base_location_url
                      ON p.url=t.listing_url
                    WHERE p.base_location_url=?
                    ORDER BY t.doctor_url,p.page_no,t.card_index
                    """,
                    (base,),
                ).fetchall()
                grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for row in rows:
                    grouped[normalize_url(row["doctor_url"])].append(dict(row))
                for doctor_url, traces in grouped.items():
                    doctor_name = next(
                        (str(value.get("doctor_name") or "") for value in traces if value.get("doctor_name")),
                        "",
                    )
                    conn.execute(
                        """
                        INSERT INTO provider_location_memberships(
                            specialty,specialty_slug,location,location_url,doctor_url,
                            doctor_name,is_active,first_seen_at,last_seen_at,missing_streak,
                            last_seen_run_id,listing_data_json
                        ) VALUES (?,?,?,?,?,?,1,?,?,0,?,?)
                        ON CONFLICT(specialty_slug,location_url,doctor_url) DO UPDATE SET
                            is_active=1,last_seen_at=excluded.last_seen_at,inactive_at=NULL,
                            missing_streak=0,last_seen_run_id=excluded.last_seen_run_id,
                            listing_data_json=excluded.listing_data_json
                        """,
                        (
                            target.specialty,
                            target.specialty_slug,
                            target.location,
                            base,
                            doctor_url,
                            doctor_name,
                            stamp,
                            stamp,
                            run_id,
                            json.dumps(traces, ensure_ascii=False, default=str),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO canonical_provider_records(
                            doctor_url,doctor_name,is_active,first_seen_at,last_seen_at,last_seen_run_id
                        ) VALUES (?,?,1,?,?,?)
                        ON CONFLICT(doctor_url) DO UPDATE SET
                            is_active=1,last_seen_at=excluded.last_seen_at,inactive_at=NULL,
                            last_seen_run_id=excluded.last_seen_run_id
                        """,
                        (doctor_url, doctor_name, stamp, stamp, run_id),
                    )
                    providers.add(doctor_url)
                    memberships += 1
                digest = hashlib.sha256("\n".join(sorted(grouped)).encode("utf-8")).hexdigest()
                conn.execute(
                    """
                    UPDATE daily_sync_locations
                    SET status='complete',providers_seen=?,provider_set_hash=?,
                        pages_fetched=(SELECT COUNT(*) FROM pages WHERE base_location_url=?
                            AND status IN ('saved','valid_empty')),
                        expected_pages=(SELECT COALESCE(MAX(page_no),1) FROM pages
                            WHERE base_location_url=? AND status IN ('saved','valid_empty')),
                        started_at=?,completed_at=?
                    WHERE run_id=? AND specialty_slug=? AND location_url=?
                    """,
                    (len(grouped), digest, base, base, stamp, stamp, run_id, target.specialty_slug, base),
                )
            conn.commit()
        profile_source = workspace.output_dir / "unique_doctors.csv"
        profile_migration = (
            migrate_profiles_from_export(workspace, input_path=profile_source)
            if profile_source.exists()
            else {
                "source": str(profile_source),
                "scanned": 0,
                "imported": 0,
                "already_present": 0,
                "invalid": 0,
                "unmatched": 0,
            }
        )
        prepare_profile_queue(workspace, run_id, refresh_days=DEFAULT_PROFILE_REFRESH_DAYS)
        result = _finalize_run(
            workspace,
            run_id,
            profile_collection_requested=False,
        )
        return {
            "run_id": run_id,
            "locations": int(result["locations_completed"]),
            "memberships": memberships,
            "providers": len(providers),
            "profiles_imported": int(profile_migration["imported"]),
            "profiles_queued": int(result["profiles_queued"]),
        }


def resume_daily_profiles(
    workspace: Workspace,
    *,
    run_id: int | None = None,
    runner_ids: tuple[str, ...] = (),
    limit: int | None = None,
    concurrency_per_runner: int = 3,
    timeout_seconds: float = LIVE_TIMEOUT_SECONDS,
    poll_seconds: float = LIVE_POLL_SECONDS,
    capture_retries: int = DEFAULT_CAPTURE_RETRIES,
) -> dict[str, Any]:
    with daily_sync_lock(workspace):
        with connect(workspace) as conn:
            if run_id is None:
                row = conn.execute(
                    """
                    SELECT r.id FROM daily_sync_runs r
                    WHERE EXISTS (
                        SELECT 1 FROM daily_profile_queue q
                        WHERE q.run_id=r.id AND q.status IN ('pending','retry_pending','opening')
                    )
                    ORDER BY r.id DESC LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    raise RuntimeError("No daily run has pending profiles.")
                run_id = int(row["id"])
            exists = conn.execute(
                "SELECT 1 FROM daily_sync_runs WHERE id=?", (run_id,)
            ).fetchone()
            if exists is None:
                raise RuntimeError(f"Daily sync run {run_id} does not exist.")
        collected = collect_daily_profiles(
            workspace,
            run_id,
            runner_ids=runner_ids,
            limit=limit,
            concurrency_per_runner=concurrency_per_runner,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            capture_retries=capture_retries,
        )
        summary = _finalize_run(workspace, run_id, profile_collection_requested=True)
        return {**summary, "profile_batch": collected}


def daily_sync_status(workspace: Workspace, *, limit: int = 10) -> dict[str, Any]:
    with connect(workspace) as conn:
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM daily_sync_runs ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        ]
        membership = conn.execute(
            """
            SELECT COUNT(*) total,
                   SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) active,
                   SUM(CASE WHEN is_active=0 THEN 1 ELSE 0 END) inactive
            FROM provider_location_memberships
            """
        ).fetchone()
        providers = conn.execute(
            """
            SELECT COUNT(*) total,
                   SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) active,
                   SUM(CASE WHEN is_active=0 THEN 1 ELSE 0 END) inactive
            FROM canonical_provider_records
            """
        ).fetchone()
        pending_profiles = conn.execute(
            "SELECT COUNT(*) n FROM daily_profile_queue WHERE status IN ('pending','retry_pending','opening')"
        ).fetchone()["n"]
    return {
        "runs": runs,
        "memberships": dict(membership),
        "providers": dict(providers),
        "pending_profiles": int(pending_profiles),
    }


def run_daily_daemon(
    workspace: Workspace,
    *,
    interval_hours: float = 24.0,
    run_immediately: bool = True,
    **daily_options: Any,
) -> None:
    if interval_hours <= 0:
        raise ValueError("interval hours must be greater than 0")
    if not run_immediately:
        time.sleep(interval_hours * 3600)
    while True:
        started = datetime.now(UTC)
        try:
            result = run_daily_sync(workspace, **daily_options)
            print(
                f"DAILY RUN {result['id']} {result['status']} | "
                f"locations={result['locations_completed']}/{result['locations_selected']} "
                f"added={result['providers_added']} removed={result['providers_removed']}"
            )
        except Exception as exc:
            print(f"DAILY RUN FAILED | {exc}")
        next_run = started + timedelta(hours=interval_hours)
        sleep_seconds = max(1.0, (next_run - datetime.now(UTC)).total_seconds())
        print(f"Next daily run at {next_run.isoformat()}")
        time.sleep(sleep_seconds)
