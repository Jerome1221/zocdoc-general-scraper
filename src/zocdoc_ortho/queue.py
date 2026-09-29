from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from collections import Counter
from pathlib import Path

from .db import connect, trace_table_exists
from .listing import ZERO_LINK_MAX_RETRIES, process_saved_html
from .outputs import export_page_outputs, export_profile_queue
from .performance import PerformanceRecorder
from .saver import configure_capture, enqueue_url, get_capture_metrics, get_health
from .trace import (
    find_listing_html,
    find_profile_html,
    persist_listing_trace_html,
    sync_profiles_from_doctors,
)
from .urls import normalize_url
from .utils import now_iso
from .workspace import Workspace

ATTENTION_STATUSES = ("opening", "timeout", "blocked", "review")


def _timing_line(timings: dict, *, include_listing: bool = True) -> str:
    """Compact human-readable stage timing for collector logs."""
    parts = []
    mapping = [
        ("load", "navigation_seconds"),
        ("stabilize", "stabilization_seconds"),
        ("serialize", "serialize_seconds"),
        ("disk", "disk_write_seconds"),
    ]
    if include_listing:
        mapping.extend(
            [
                ("validate", "validation_seconds"),
                ("listing-db", "listing_db_seconds"),
                ("trace", "trace_seconds"),
                ("profile-sync", "profile_sync_seconds"),
                ("cleanup", "cleanup_seconds"),
                ("output", "output_seconds"),
            ]
        )
    else:
        mapping.append(("profile-db", "profile_mark_db_seconds"))
    for label, field in mapping:
        value = timings.get(field)
        if value not in {None, ""}:
            try:
                parts.append(f"{label}={float(value):.3f}s")
            except (TypeError, ValueError):
                pass
    return " ".join(parts)


def mark_page_opening(conn: sqlite3.Connection, url: str) -> None:
    conn.execute(
        "UPDATE pages SET status='opening',opened_at=?,error='' WHERE url=?",
        (now_iso(), normalize_url(url)),
    )
    conn.commit()


def mark_page_timeout(conn: sqlite3.Connection, url: str, error: str) -> None:
    conn.execute(
        "UPDATE pages SET status='timeout',error=? WHERE url=?",
        (error, normalize_url(url)),
    )
    conn.commit()


def mark_profile_saved(
    conn: sqlite3.Connection,
    workspace: Workspace,
    url: str,
    path: Path,
    *,
    write_output: bool = True,
) -> None:
    conn.execute(
        """
        UPDATE doctor_profiles
        SET status='saved',file=?,bytes=?,saved_at=?,error='',last_error='',
            last_seen_at=?,claimed_by=NULL,claimed_at=NULL
        WHERE doctor_url=?
        """,
        (path.name, path.stat().st_size, now_iso(), now_iso(), normalize_url(url)),
    )
    conn.commit()
    if write_output:
        export_profile_queue(conn, workspace)


def _quarantine_listing_capture(
    conn: sqlite3.Connection, workspace: Workspace, url: str, path: Path, parsed: dict
) -> Path | None:
    if parsed.get("queue_status") not in {"retry_pending", "review"}:
        return None
    if not path.exists():
        return None
    workspace.rejected_listing_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    retry_count = int(parsed.get("zero_link_retry_count") or 0)
    destination = workspace.rejected_listing_dir / (
        f"{path.stem}--zero{retry_count:02d}-{stamp}{path.suffix}"
    )
    counter = 1
    while destination.exists():
        destination = workspace.rejected_listing_dir / (
            f"{path.stem}--zero{retry_count:02d}-{stamp}-{counter}{path.suffix}"
        )
        counter += 1
    path.replace(destination)
    relative = destination.relative_to(workspace.root).as_posix()
    conn.execute(
        """
        UPDATE pages
        SET file='',bytes=0,last_rejected_file=?
        WHERE url=?
        """,
        (relative, normalize_url(url)),
    )
    conn.commit()
    return destination


def _ingest_listing(
    conn: sqlite3.Connection,
    workspace: Workspace,
    url: str,
    path: Path,
    *,
    write_outputs: bool = True,
    zero_link_max_retries: int = ZERO_LINK_MAX_RETRIES,
    keep_listing_html: bool = False,
) -> dict:
    """Ingest a listing capture, persist its trace immediately, then prune raw HTML.

    The browser capture behavior is unchanged. Chrome saves the rendered page exactly
    as before and can close immediately. Dev9 then parses the already-captured HTML
    locally, commits the durable listing->provider trace, and deletes successful raw
    listing HTML by default. Failed/review captures remain available for diagnosis.
    """
    ingest_started = time.monotonic()
    read_started = time.monotonic()
    html = path.read_text(encoding="utf-8", errors="replace")
    html_read_seconds = max(0.0, time.monotonic() - read_started)
    source_file = path.name
    byte_count = path.stat().st_size
    parsed = process_saved_html(
        conn,
        workspace,
        normalize_url(url),
        "",
        html,
        source_file,
        byte_count,
        write_outputs=False,
        zero_link_max_retries=zero_link_max_retries,
    )
    quarantine_started = time.monotonic()
    _quarantine_listing_capture(conn, workspace, url, path, parsed)
    quarantine_seconds = max(0.0, time.monotonic() - quarantine_started)

    trace_succeeded = False
    trace_started = time.monotonic()
    traced_doctor_urls: list[str] = []
    if parsed.get("queue_status") in {"saved", "valid_empty"}:
        page_row = conn.execute(
            """
            SELECT url,state_group,location,page_no,specialty_url,specialty_name,specialty_slug
            FROM pages WHERE url=?
            """,
            (normalize_url(url),),
        ).fetchone()
        if page_row is not None:
            try:
                parsed["trace_occurrence_count"] = persist_listing_trace_html(
                    conn,
                    page_row=page_row,
                    html=html,
                    source_file=source_file,
                    commit=True,
                )
                traced_doctor_urls = [
                    row["doctor_url"]
                    for row in conn.execute(
                        "SELECT DISTINCT doctor_url FROM listing_provider_trace WHERE listing_url=?",
                        (normalize_url(url),),
                    ).fetchall()
                    if row["doctor_url"]
                ]
                expected_doctor_urls = {
                    normalize_url(doctor.get("doctor_url", ""))
                    for doctor in parsed.get("doctors", [])
                    if doctor.get("doctor_url")
                }
                missing = expected_doctor_urls.difference(traced_doctor_urls)
                if missing:
                    reason = (
                        f"trace coverage incomplete: {len(missing)} of "
                        f"{len(expected_doctor_urls)} validated provider link(s) missing"
                    )
                    conn.execute(
                        """
                        UPDATE pages
                        SET status='review',validation_status='trace_partial',
                            validation_reason=?,error=?,trace_status='partial'
                        WHERE url=?
                        """,
                        (reason, reason, normalize_url(url)),
                    )
                    conn.commit()
                    parsed["queue_status"] = "review"
                    parsed["validation_status"] = "trace_partial"
                    parsed["validation_reason"] = reason
                    parsed["trace_error"] = reason
                    print(f"    TRACE PARTIAL | keeping raw HTML | {url} | {reason}")
                else:
                    trace_succeeded = True
            except Exception as exc:
                conn.rollback()
                parsed["trace_error"] = str(exc)
                print(f"    TRACE DEFERRED | keeping raw HTML | {url} | {exc}")

    trace_seconds = max(0.0, time.monotonic() - trace_started)
    sync_started = time.monotonic()
    sync_profiles_from_doctors(
        conn,
        workspace,
        doctor_urls=traced_doctor_urls if trace_succeeded else (),
        write_output=False,
    )
    profile_sync_seconds = max(0.0, time.monotonic() - sync_started)

    cleanup_started = time.monotonic()
    if trace_succeeded and not keep_listing_html and path.exists():
        path.unlink()
        conn.execute(
            "UPDATE pages SET trace_html_deleted_at=? WHERE url=?",
            (now_iso(), normalize_url(url)),
        )
        conn.commit()
        parsed["listing_html_deleted"] = True
    else:
        parsed["listing_html_deleted"] = False

    cleanup_seconds = max(0.0, time.monotonic() - cleanup_started) + quarantine_seconds
    output_started = time.monotonic()
    if write_outputs:
        export_page_outputs(conn, workspace)
        export_profile_queue(conn, workspace)
    output_seconds = max(0.0, time.monotonic() - output_started)
    timings = dict(parsed.get("_timings") or {})
    timings.update(
        {
            "html_read_seconds": round(html_read_seconds, 4),
            "trace_seconds": round(trace_seconds, 4),
            "profile_sync_seconds": round(profile_sync_seconds, 4),
            "cleanup_seconds": round(cleanup_seconds, 4),
            "output_seconds": round(output_seconds, 4),
            "local_ingest_seconds": round(max(0.0, time.monotonic() - ingest_started), 4),
        }
    )
    parsed["_timings"] = timings
    return parsed


def activate_zero_link_retries(
    workspace: Workspace,
    *,
    phase: str = "auto",
    specialties: tuple[str, ...] = (),
) -> int:
    """Make retries created by an earlier run claimable in this run."""
    clause, selector_params = _specialty_filter_sql(specialties)
    source_sql = ""
    params: list[str] = []
    if phase in {"seed", "pagination"}:
        source_sql = " AND source=?"
        params.append(phase)
    with connect(workspace) as conn:
        cursor = conn.execute(
            f"UPDATE pages SET status='pending' WHERE status='retry_pending'{source_sql}{clause}",
            [*params, *selector_params],
        )
        conn.commit()
        return cursor.rowcount


def audit_zero_link_listings(
    workspace: Workspace,
    *,
    max_retries: int = ZERO_LINK_MAX_RETRIES,
) -> dict[str, int]:
    """Validate legacy saved listing pages with zero doctor links and requeue suspicious captures."""
    counts = Counter()
    with connect(workspace) as conn:
        rows = conn.execute(
            """
            SELECT url,zero_link_retry_count FROM pages
            WHERE status='saved' AND COALESCE(doctor_links_found,0)=0
            ORDER BY specialty_name,state_group,location,page_no,url
            """
        ).fetchall()
        for row in rows:
            url = normalize_url(row["url"])
            path = find_listing_html(workspace, url)
            if path is None:
                retry_count = int(row["zero_link_retry_count"] or 0)
                if retry_count < max(0, int(max_retries)):
                    retry_count += 1
                    status = "retry_pending"
                    validation = "missing_capture"
                    reason = f"saved zero-link row has no listing HTML; retry {retry_count}/{max_retries} scheduled"
                else:
                    status = "review"
                    validation = "retry_exhausted"
                    reason = f"saved zero-link row has no listing HTML; retry limit {max_retries} exhausted"
                conn.execute(
                    """UPDATE pages SET status=?,validation_status=?,validation_reason=?,
                       zero_link_retry_count=?,file='',bytes=0,error=? WHERE url=?""",
                    (status, validation, reason, retry_count, reason, url),
                )
                conn.commit()
                counts[status] += 1
                continue

            parsed = _ingest_listing(
                conn, workspace, url, path, write_outputs=False,
                zero_link_max_retries=max_retries, keep_listing_html=True
            )
            counts[str(parsed.get("queue_status") or "unknown")] += 1

        export_page_outputs(conn, workspace)
    counts["examined"] = len(rows)
    return dict(counts)


def reconcile_existing_files(workspace: Workspace) -> tuple[int, int]:
    workspace.ensure()
    listing_count = 0
    profile_count = 0
    with connect(workspace) as conn:
        for row in conn.execute("SELECT url,status FROM pages").fetchall():
            if row["status"] in {"saved", "valid_empty"}:
                continue
            path = find_listing_html(workspace, row["url"])
            if path:
                _ingest_listing(conn, workspace, row["url"], path)
                listing_count += 1

        sync_profiles_from_doctors(conn, workspace)
        for row in conn.execute("SELECT doctor_url,status FROM doctor_profiles").fetchall():
            if row["status"] == "saved":
                continue
            path = find_profile_html(workspace, row["doctor_url"])
            if path:
                mark_profile_saved(conn, workspace, row["doctor_url"], path)
                profile_count += 1

        export_page_outputs(conn, workspace)
        export_profile_queue(conn, workspace)
    return listing_count, profile_count


def status_snapshot(workspace: Workspace) -> dict:
    with connect(workspace) as conn:
        pages = conn.execute("SELECT source,status,COUNT(*) n FROM pages GROUP BY source,status").fetchall()
        profiles = conn.execute("SELECT status,COUNT(*) n FROM doctor_profiles GROUP BY status").fetchall()
        specialty_pages = conn.execute(
            "SELECT status,COUNT(*) n FROM specialty_pages GROUP BY status"
        ).fetchall()
        specialty_targets = conn.execute(
            "SELECT COUNT(*) n FROM specialty_targets WHERE is_active=1"
        ).fetchone()["n"]
        raw_occurrences = 0
        trace_unique = 0
        trace_parsed_pages = conn.execute(
            "SELECT COUNT(*) n FROM pages WHERE trace_status='parsed'"
        ).fetchone()["n"]
        trace_unparsed_saved = conn.execute(
            """SELECT COUNT(*) n FROM pages
               WHERE status IN ('saved','valid_empty')
                 AND COALESCE(trace_status,'')<>'parsed'"""
        ).fetchone()["n"]
        if trace_table_exists(conn):
            raw_occurrences = conn.execute("SELECT COUNT(*) n FROM listing_provider_trace").fetchone()["n"]
            trace_unique = conn.execute("SELECT COUNT(DISTINCT doctor_url) n FROM listing_provider_trace").fetchone()["n"]

    page_counts: dict[str, Counter] = {"seed": Counter(), "pagination": Counter(), "other": Counter()}
    for row in pages:
        source = row["source"] if row["source"] in {"seed", "pagination"} else "other"
        page_counts[source][row["status"]] += row["n"]
    profile_counts = Counter({row["status"]: row["n"] for row in profiles})
    specialty_page_counts = Counter({row["status"]: row["n"] for row in specialty_pages})
    return {
        "pages": page_counts,
        "profiles": profile_counts,
        "specialty_pages": specialty_page_counts,
        "specialty_targets": specialty_targets,
        "raw_occurrences": raw_occurrences,
        "trace_unique": trace_unique,
        "trace_parsed_pages": trace_parsed_pages,
        "trace_unparsed_saved": trace_unparsed_saved,
    }


def print_status(workspace: Workspace) -> None:
    snap = status_snapshot(workspace)
    seed = snap["pages"]["seed"]
    pagination = snap["pages"]["pagination"]
    profiles = snap["profiles"]
    specialty_pages = snap["specialty_pages"]
    print("=" * 72)
    print(
        f"Specialty pages: {sum(specialty_pages.values()):,} | saved={specialty_pages.get('saved',0):,} "
        f"pending={specialty_pages.get('pending',0):,} timeout={specialty_pages.get('timeout',0):,} "
        f"blocked={specialty_pages.get('blocked',0):,}"
    )
    print(f"Active specialty/location targets: {snap['specialty_targets']:,}")
    print(
        f"Base locations : {sum(seed.values()):,} | saved={seed.get('saved',0):,} "
        f"empty={seed.get('valid_empty',0):,} pending={seed.get('pending',0):,} "
        f"retry={seed.get('retry_pending',0):,} review={seed.get('review',0):,} "
        f"timeout={seed.get('timeout',0):,} blocked={seed.get('blocked',0):,}"
    )
    print(
        f"Pagination     : {sum(pagination.values()):,} | saved={pagination.get('saved',0):,} "
        f"empty={pagination.get('valid_empty',0):,} pending={pagination.get('pending',0):,} "
        f"retry={pagination.get('retry_pending',0):,} review={pagination.get('review',0):,} "
        f"timeout={pagination.get('timeout',0):,} blocked={pagination.get('blocked',0):,}"
    )
    print(
        f"Doctor profiles: {sum(profiles.values()):,} | saved={profiles.get('saved',0):,} "
        f"pending={profiles.get('pending',0):,} timeout={profiles.get('timeout',0):,} blocked={profiles.get('blocked',0):,}"
    )
    print(f"Trace parsed listing pages      : {snap['trace_parsed_pages']:,}")
    print(f"Saved listings awaiting trace   : {snap['trace_unparsed_saved']:,}")
    print(f"Raw doctor appearances in trace: {snap['raw_occurrences']:,}")
    print(f"Unique doctors in trace         : {snap['trace_unique']:,}")
    print("=" * 72)


def _assert_saver_ready(workspace: Workspace, *, server_url: str | None = None) -> None:
    if workspace.blocked_flag.exists():
        raise RuntimeError(
            f"Collector is stopped because {workspace.blocked_flag} exists. Review the restriction before continuing."
        )
    health = get_health(server_url=server_url)
    if not health.get("ok"):
        raise RuntimeError(f"Local saver is not reachable: {health.get('error','unknown error')}")
    if not health.get("controller_active"):
        target = server_url or "http://127.0.0.1:8765"
        raise RuntimeError(f"Saver is running, but the Chrome controller tab is not active. Open {target}/controller")
    server_workspace = Path(str(health.get("workspace") or "")).resolve()
    if server_workspace != workspace.root.resolve():
        raise RuntimeError(
            f"Saver workspace mismatch. Server is writing to {server_workspace}; CLI is using {workspace.root}."
        )


def _specialty_filter_sql(specialties: tuple[str, ...]) -> tuple[str, list[str]]:
    values = [value.strip().lower() for value in specialties if value.strip()]
    if not values:
        return "", []
    placeholders = ",".join("?" for _ in values)
    clause = (
        f" AND (lower(specialty_slug) IN ({placeholders}) "
        f"OR lower(specialty_name) IN ({placeholders}) "
        f"OR lower(specialty_url) IN ({placeholders}))"
    )
    return clause, values + values + values


def _pending_pages(
    conn: sqlite3.Connection,
    phase: str,
    limit: int,
    specialties: tuple[str, ...] = (),
) -> list[sqlite3.Row]:
    if phase not in {"seed", "pagination"}:
        raise ValueError("phase must be seed or pagination")
    clause, selector_params = _specialty_filter_sql(specialties)
    return conn.execute(
        f"""
        SELECT * FROM pages
        WHERE source=? AND status='pending'{clause}
        ORDER BY specialty_name,state_group,location,base_location_url,page_no,url
        LIMIT ?
        """,
        [phase, *selector_params, limit],
    ).fetchall()


def _claim_pending_page(
    workspace: Workspace,
    phase: str,
    specialties: tuple[str, ...] = (),
) -> dict | None:
    """Atomically claim one pending listing page for multi-process runners."""
    if phase not in {"seed", "pagination"}:
        raise ValueError("phase must be seed or pagination")
    clause, selector_params = _specialty_filter_sql(specialties)
    with connect(workspace) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                f"""
                SELECT * FROM pages
                WHERE source=? AND status='pending'{clause}
                ORDER BY specialty_name,state_group,location,base_location_url,page_no,url
                LIMIT 1
                """,
                [phase, *selector_params],
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            url = normalize_url(row["url"])
            updated = conn.execute(
                """
                UPDATE pages
                SET status='opening',opened_at=?,error=''
                WHERE url=? AND status='pending'
                """,
                (now_iso(), url),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            claimed = dict(row)
            claimed["url"] = url
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise


def _phase_status_counts(
    workspace: Workspace,
    phase: str,
    specialties: tuple[str, ...] = (),
) -> Counter:
    clause, selector_params = _specialty_filter_sql(specialties)
    with connect(workspace) as conn:
        rows = conn.execute(
            f"SELECT status,COUNT(*) n FROM pages WHERE source=?{clause} GROUP BY status",
            [phase, *selector_params],
        ).fetchall()
    return Counter({row["status"]: row["n"] for row in rows})


def choose_listing_phase(workspace: Workspace, specialties: tuple[str, ...] = ()) -> str:
    seed = _phase_status_counts(workspace, "seed", specialties)
    pagination = _phase_status_counts(workspace, "pagination", specialties)
    if seed.get("pending", 0):
        return "seed"
    if any(seed.get(status, 0) for status in ATTENTION_STATUSES):
        return "seed_attention"
    if pagination.get("pending", 0):
        return "pagination"
    if any(pagination.get(status, 0) for status in ATTENTION_STATUSES):
        return "pagination_attention"
    return "complete"


def collect_listing_batch(
    workspace: Workspace,
    limit: int = 250,
    concurrency: int = 4,
    timeout_seconds: int = 60,
    stagger_seconds: float = 0.1,
    poll_seconds: float = 0.2,
    page_check_ms: int = 250,
    stable_checks: int = 2,
    page_max_wait_ms: int = 8000,
    controller_poll_ms: int = 200,
    phase: str = "auto",
    specialties: tuple[str, ...] = (),
    server_url: str | None = None,
    runner_id: str = "single",
    zero_link_max_retries: int = ZERO_LINK_MAX_RETRIES,
    keep_listing_html: bool = False,
) -> int:
    """Collect listing HTML with a rolling worker pool.

    Unlike the older fixed-size batches, a finished slot is refilled immediately,
    so one slow page does not idle the other workers.
    """
    if server_url is None:
        _assert_saver_ready(workspace)
    else:
        _assert_saver_ready(workspace, server_url=server_url)
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if limit < 1:
        return 0
    if stagger_seconds < 0 or poll_seconds <= 0:
        raise ValueError("stagger must be >= 0 and poll must be > 0")

    activate_zero_link_retries(workspace, phase=phase, specialties=specialties)

    configure_capture(
        "listing",
        check_ms=page_check_ms,
        stable_checks=stable_checks,
        max_wait_ms=page_max_wait_ms,
        controller_poll_ms=controller_poll_ms,
        server_url=server_url,
    )

    if phase == "auto":
        phase = choose_listing_phase(workspace, specialties)
    if phase.endswith("_attention"):
        raise RuntimeError(f"{phase}: review/reset timeout/opening/blocked rows before collecting more pages")
    if phase == "complete":
        print("No pending listing pages remain.")
        return 0
    if phase not in {"seed", "pagination"}:
        raise ValueError("phase must be auto, seed, or pagination")

    recorder = PerformanceRecorder(
        workspace,
        "collect-listings",
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
    write_shared_outputs = runner_id == "single"
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
                        "UPDATE pages SET status='blocked',error=? WHERE status='opening'",
                        ("Restriction detected by Chrome saver",),
                    )
                    conn.commit()
                    if write_shared_outputs:
                        export_page_outputs(conn, workspace)
                now = time.monotonic()
                for url, item in active.items():
                    recorder.record(
                        page_type="listing", status="blocked", url=url,
                        duration_seconds=now - item["started"],
                        specialty_name=item["specialty_name"], location=item["location"],
                    )
                completed += len(active)
                active.clear()
                print("STOP: restriction detected.")
                break

            # Reap finished/timeout tabs first so their slots can be reused immediately.
            now = time.monotonic()
            for url, item in list(active.items()):
                path = find_listing_html(workspace, url)
                if path:
                    with connect(workspace) as conn:
                        parsed = _ingest_listing(
                            conn, workspace, url, path,
                            write_outputs=write_shared_outputs,
                            zero_link_max_retries=zero_link_max_retries,
                            keep_listing_html=keep_listing_html,
                        )
                    duration = now - item["started"]
                    capture_timings = get_capture_metrics(url, server_url=server_url)
                    timings = {**capture_timings, **dict(parsed.get("_timings") or {})}
                    print(
                        f"    {parsed.get('queue_status','saved').upper()} | {duration:.2f}s | "
                        f"doctors={len(parsed['doctors'])} pagination={len(parsed['pagination_urls'])}"
                    )
                    timing_text = _timing_line(timings, include_listing=True)
                    if timing_text:
                        print(f"    TIMING | {timing_text} | total={duration:.3f}s")
                    recorder.record(
                        page_type="listing", status="saved", url=url, duration_seconds=duration,
                        specialty_name=item["specialty_name"], location=item["location"],
                        doctor_count=len(parsed["doctors"]),
                        pagination_count=len(parsed["pagination_urls"]),
                        provider_set_hash=parsed.get("provider_set_hash", ""),
                        timings=timings,
                    )
                    active.pop(url, None)
                    completed += 1
                    continue
                if now - item["started"] > timeout_seconds:
                    with connect(workspace) as conn:
                        mark_page_timeout(conn, url, f"No saved HTML after {timeout_seconds}s")
                        if write_shared_outputs:
                            export_page_outputs(conn, workspace)
                    print(f"    TIMEOUT | {now - item['started']:.2f}s | {url}")
                    recorder.record(
                        page_type="listing", status="timeout", url=url,
                        duration_seconds=now - item["started"],
                        specialty_name=item["specialty_name"], location=item["location"],
                    )
                    active.pop(url, None)
                    completed += 1

            # Continuously refill free slots instead of waiting for the slowest page in a batch.
            while len(active) < concurrency and scheduled < limit and not exhausted:
                row = _claim_pending_page(workspace, phase, specialties)
                if row is None:
                    exhausted = True
                    break
                url = normalize_url(row["url"])
                existing = find_listing_html(workspace, url)
                if existing:
                    with connect(workspace) as conn:
                        parsed = _ingest_listing(
                            conn, workspace, url, existing,
                            write_outputs=write_shared_outputs,
                            zero_link_max_retries=zero_link_max_retries,
                            keep_listing_html=keep_listing_html,
                        )
                    scheduled += 1
                    completed += 1
                    print(
                        f"INGEST existing | status={parsed.get('queue_status','saved')} | "
                        f"doctors={len(parsed['doctors'])} | {url}"
                    )
                    existing_timings = dict(parsed.get("_timings") or {})
                    timing_text = _timing_line(existing_timings, include_listing=True)
                    if timing_text:
                        print(f"    TIMING | {timing_text} | existing-html")
                    recorder.record(
                        page_type="listing", status="existing", url=url, duration_seconds=0.0,
                        specialty_name=row["specialty_name"] or row["specialty_slug"] or "",
                        location=row["location"] or "", doctor_count=len(parsed["doctors"]),
                        pagination_count=len(parsed["pagination_urls"]),
                        provider_set_hash=parsed.get("provider_set_hash", ""),
                        timings=existing_timings,
                    )
                    continue

                if stagger_seconds > 0 and last_enqueue:
                    wait = stagger_seconds - (time.monotonic() - last_enqueue)
                    if wait > 0:
                        time.sleep(wait)
                specialty_label = row["specialty_name"] or row["specialty_slug"] or "listing"
                print(
                    f"OPEN [{scheduled + 1}/{limit}] {specialty_label} | "
                    f"{row['location']} | page {row['page_no']}"
                )
                print(f"    {url}")
                if server_url is None:
                    enqueue_url(url)
                else:
                    enqueue_url(url, server_url=server_url)
                started = time.monotonic()
                last_enqueue = started
                active[url] = {
                    "started": started,
                    "specialty_name": specialty_label,
                    "location": row["location"] or "",
                }
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
                f"PERF | {summary['processed']} pages | {summary['pages_per_minute']:.1f}/min | "
                f"elapsed={summary['elapsed_seconds'] / 60:.2f}m | p50={summary['p50_seconds']:.2f}s p95={summary['p95_seconds']:.2f}s"
            )




def recover_stale_profile_claims(
    workspace: Workspace,
    *,
    stale_claim_minutes: float = 10.0,
) -> int:
    """Release profile claims left behind by crashed/interrupted workers."""
    if stale_claim_minutes <= 0:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(minutes=float(stale_claim_minutes))).isoformat()
    message = f"Recovered stale profile claim older than {stale_claim_minutes:g} minute(s)"
    with connect(workspace) as conn:
        cursor = conn.execute(
            """
            UPDATE doctor_profiles
            SET status='pending',claimed_by=NULL,claimed_at=NULL,opened_at=NULL,
                error=?,last_error=?
            WHERE status='opening'
              AND claimed_at IS NOT NULL
              AND claimed_at < ?
            """,
            (message, message, cutoff),
        )
        conn.commit()
        return cursor.rowcount


def _claim_pending_profile(
    workspace: Workspace, runner_id: str, specialties: tuple[str, ...] = ()
) -> dict | None:
    """Atomically claim one pending profile so multiple collectors cannot duplicate work."""
    with connect(workspace) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if specialties:
                values = [value.strip().lower() for value in specialties if value.strip()]
                placeholders = ",".join("?" for _ in values)
                specialty_clause = (
                    f" AND EXISTS ("
                    f"SELECT 1 FROM doctors d JOIN specialties s ON "
                    f"(lower(s.specialty_name)=lower(d.first_seen_specialty_name) "
                    f"OR lower(s.specialty_url)=lower(d.first_seen_specialty_url)) "
                    f"WHERE d.doctor_url=dp.doctor_url AND ("
                    f"lower(s.specialty_slug) IN ({placeholders}) OR "
                    f"lower(s.specialty_name) IN ({placeholders}) OR "
                    f"lower(s.specialty_url) IN ({placeholders})))"
                )
                params = values + values + values
            else:
                specialty_clause = ""
                params = []
            row = conn.execute(
                f"""
                SELECT dp.* FROM doctor_profiles dp
                WHERE dp.status='pending'{specialty_clause}
                ORDER BY dp.doctor_name,dp.doctor_url
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            url = normalize_url(row["doctor_url"])
            stamp = now_iso()
            updated = conn.execute(
                """
                UPDATE doctor_profiles
                SET status='opening',opened_at=?,claimed_by=?,claimed_at=?,
                    attempt_count=COALESCE(attempt_count,0)+1,error='',last_error=''
                WHERE doctor_url=? AND status='pending'
                """,
                (stamp, runner_id, stamp, url),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            claimed = dict(row)
            claimed["doctor_url"] = url
            claimed["claimed_by"] = runner_id
            claimed["claimed_at"] = stamp
            claimed["attempt_count"] = int(row["attempt_count"] or 0) + 1
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise


def collect_profile_batch(
    workspace: Workspace,
    limit: int = 250,
    concurrency: int = 3,
    timeout_seconds: int = 60,
    stagger_seconds: float = 0.25,
    poll_seconds: float = 0.25,
    page_check_ms: int = 500,
    stable_checks: int = 3,
    page_max_wait_ms: int = 12000,
    controller_poll_ms: int = 200,
    server_url: str | None = None,
    runner_id: str = "single",
    stale_claim_minutes: float = 10.0,
    specialties: tuple[str, ...] = (),
) -> int:
    if server_url is None:
        _assert_saver_ready(workspace)
    else:
        _assert_saver_ready(workspace, server_url=server_url)
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    configure_capture(
        "profile",
        check_ms=page_check_ms,
        stable_checks=stable_checks,
        max_wait_ms=page_max_wait_ms,
        controller_poll_ms=controller_poll_ms,
        server_url=server_url,
    )
    recorder = PerformanceRecorder(
        workspace,
        "collect-profiles",
        {
            "concurrency": concurrency,
            "stagger_seconds": stagger_seconds,
            "poll_seconds": poll_seconds,
            "page_check_ms": page_check_ms,
            "stable_checks": stable_checks,
            "page_max_wait_ms": page_max_wait_ms,
            "controller_poll_ms": controller_poll_ms,
            "runner_id": runner_id,
            "stale_claim_minutes": stale_claim_minutes,
            "specialty_filter": ";".join(specialties),
        },
    )
    write_shared_outputs = runner_id == "single"
    recovered = recover_stale_profile_claims(workspace, stale_claim_minutes=stale_claim_minutes)
    if recovered:
        print(f"Recovered {recovered:,} stale profile claim(s).")
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
                        """UPDATE doctor_profiles
                           SET status='blocked',error=?,last_error=?,last_seen_at=?,
                               claimed_by=NULL,claimed_at=NULL
                           WHERE status='opening'""",
                        ("Restriction detected by Chrome saver",
                         "Restriction detected by Chrome saver", now_iso()),
                    )
                    conn.commit()
                    if write_shared_outputs:
                        export_profile_queue(conn, workspace)
                now = time.monotonic()
                for url, item in active.items():
                    recorder.record(
                        page_type="profile", status="blocked", url=url,
                        duration_seconds=now - item["started"],
                    )
                completed += len(active)
                active.clear()
                print("STOP: restriction detected.")
                break

            now = time.monotonic()
            for url, item in list(active.items()):
                path = find_profile_html(workspace, url)
                if path:
                    capture_timings = get_capture_metrics(url, server_url=server_url)
                    db_started = time.monotonic()
                    with connect(workspace) as conn:
                        mark_profile_saved(conn, workspace, url, path, write_output=write_shared_outputs)
                    profile_db_seconds = max(0.0, time.monotonic() - db_started)
                    duration = now - item["started"]
                    timings = {**capture_timings, "profile_mark_db_seconds": round(profile_db_seconds, 4)}
                    print(f"    SAVED | {duration:.2f}s | {path.name}")
                    timing_text = _timing_line(timings, include_listing=False)
                    if timing_text:
                        claim_note = " | claim-profile" if capture_timings.get("claim_profile_visible") else ""
                        print(f"    TIMING | {timing_text} | total={duration:.3f}s{claim_note}")
                    recorder.record(
                        page_type="profile", status="saved", url=url, duration_seconds=duration, timings=timings
                    )
                    active.pop(url, None)
                    completed += 1
                    continue
                if now - item["started"] > timeout_seconds:
                    with connect(workspace) as conn:
                        message = f"No saved HTML after {timeout_seconds}s"
                        conn.execute(
                            """UPDATE doctor_profiles
                               SET status='timeout',error=?,last_error=?,last_seen_at=?,
                                   claimed_by=NULL,claimed_at=NULL
                               WHERE doctor_url=? AND claimed_by=?""",
                            (message, message, now_iso(), url, runner_id),
                        )
                        conn.commit()
                        if write_shared_outputs:
                            export_profile_queue(conn, workspace)
                    print(f"    TIMEOUT | {now - item['started']:.2f}s | {url}")
                    recorder.record(
                        page_type="profile", status="timeout", url=url,
                        duration_seconds=now - item["started"],
                    )
                    active.pop(url, None)
                    completed += 1

            while len(active) < concurrency and scheduled < limit and not exhausted:
                row = _claim_pending_profile(workspace, runner_id, specialties)
                if row is None:
                    exhausted = True
                    break
                url = normalize_url(row["doctor_url"])
                existing = find_profile_html(workspace, url)
                if existing:
                    db_started = time.monotonic()
                    with connect(workspace) as conn:
                        mark_profile_saved(
                            conn, workspace, url, existing, write_output=write_shared_outputs
                        )
                    profile_db_seconds = max(0.0, time.monotonic() - db_started)
                    scheduled += 1
                    completed += 1
                    print(f"INGEST existing profile: {url}")
                    print(f"    TIMING | profile-db={profile_db_seconds:.3f}s | existing-html")
                    recorder.record(
                        page_type="profile", status="existing", url=url, duration_seconds=0.0,
                        timings={"profile_mark_db_seconds": profile_db_seconds},
                    )
                    continue
                if write_shared_outputs:
                    with connect(workspace) as conn:
                        export_profile_queue(conn, workspace)

                if stagger_seconds > 0 and last_enqueue:
                    wait = stagger_seconds - (time.monotonic() - last_enqueue)
                    if wait > 0:
                        time.sleep(wait)
                print(f"OPEN PROFILE [{scheduled + 1}/{limit}] {row['doctor_name']}")
                print(f"    {url}")
                if server_url is None:
                    enqueue_url(url)
                else:
                    enqueue_url(url, server_url=server_url)
                started = time.monotonic()
                last_enqueue = started
                active[url] = {"started": started}
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
                f"PERF | {summary['processed']} profiles | {summary['pages_per_minute']:.1f}/min | "
                f"elapsed={summary['elapsed_seconds'] / 60:.2f}m | p50={summary['p50_seconds']:.2f}s p95={summary['p95_seconds']:.2f}s"
            )



def reset_statuses(workspace: Workspace, target: str, statuses: tuple[str, ...]) -> int:
    allowed = {"opening", "timeout", "blocked", "review", "retry_pending"}
    invalid = set(statuses) - allowed
    if invalid:
        raise ValueError(f"Unsupported reset statuses: {sorted(invalid)}")
    table_map = {
        "listings": "pages",
        "profiles": "doctor_profiles",
        "specialty-pages": "specialty_pages",
    }
    if target not in table_map:
        raise ValueError(f"Unsupported reset target: {target}")
    table = table_map[target]
    placeholders = ",".join("?" for _ in statuses)
    with connect(workspace) as conn:
        if target == "profiles":
            cursor = conn.execute(
                f"""UPDATE {table}
                   SET status='pending',error='',last_error='',claimed_by=NULL,claimed_at=NULL,opened_at=NULL
                   WHERE status IN ({placeholders})""",
                tuple(statuses),
            )
        else:
            cursor = conn.execute(
                f"UPDATE {table} SET status='pending',error='' WHERE status IN ({placeholders})",
                tuple(statuses),
            )
        conn.commit()
        if target == "listings":
            export_page_outputs(conn, workspace)
        elif target == "profiles":
            export_profile_queue(conn, workspace)
        else:
            from .specialty_targets import export_specialty_target_outputs

            export_specialty_target_outputs(conn, workspace)
        return cursor.rowcount


def collect_one_profile(
    workspace: Workspace,
    doctor_url: str | None = None,
    *,
    timeout_seconds: int = 60,
    server_url: str | None = None,
) -> tuple[str, Path]:
    """Save exactly one profile HTML with the same atomic claim semantics as bulk collection."""
    if server_url is None:
        _assert_saver_ready(workspace)
    else:
        _assert_saver_ready(workspace, server_url=server_url)

    smoke_runner = "smoke"
    if doctor_url:
        url = normalize_url(doctor_url)
        with connect(workspace) as conn:
            row = conn.execute(
                "SELECT * FROM doctor_profiles WHERE doctor_url=?",
                (url,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Doctor is not in the profile queue: {url}")
            existing = find_profile_html(workspace, url)
            if existing:
                mark_profile_saved(conn, workspace, url, existing)
                return url, existing
            if row["status"] == "opening" and row["claimed_by"] not in (None, "", smoke_runner):
                raise RuntimeError(f"Profile is already claimed by {row['claimed_by']}: {url}")
            stamp = now_iso()
            updated = conn.execute(
                """
                UPDATE doctor_profiles
                SET status='opening',opened_at=?,claimed_by=?,claimed_at=?,
                    attempt_count=COALESCE(attempt_count,0)+1,error='',last_error=''
                WHERE doctor_url=? AND status<>'saved'
                  AND (claimed_by IS NULL OR claimed_by='' OR claimed_by=?)
                """,
                (stamp, smoke_runner, stamp, url, smoke_runner),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"Could not claim profile for smoke test: {url}")
            conn.commit()
            export_profile_queue(conn, workspace)
    else:
        row = _claim_pending_profile(workspace, smoke_runner)
        if row is None:
            with connect(workspace) as conn:
                saved = conn.execute(
                    "SELECT doctor_url FROM doctor_profiles WHERE status='saved' ORDER BY doctor_name,doctor_url LIMIT 1"
                ).fetchone()
            if saved is None:
                raise RuntimeError("No doctor profiles are available for the smoke test")
            url = normalize_url(saved["doctor_url"])
            existing = find_profile_html(workspace, url)
            if existing is None:
                raise RuntimeError("No pending profiles and the saved smoke profile HTML is missing")
            return url, existing
        url = normalize_url(row["doctor_url"])
        existing = find_profile_html(workspace, url)
        if existing:
            with connect(workspace) as conn:
                mark_profile_saved(conn, workspace, url, existing)
            return url, existing

    print(f"QUEUE ONE PROFILE: {url}")
    if server_url is None:
        enqueue_url(url)
    else:
        enqueue_url(url, server_url=server_url)
    started = time.time()
    while time.time() - started <= timeout_seconds:
        if workspace.blocked_flag.exists():
            with connect(workspace) as conn:
                message = "Restriction detected by Chrome saver"
                conn.execute(
                    """
                    UPDATE doctor_profiles
                    SET status='blocked',error=?,last_error=?,last_seen_at=?,
                        claimed_by=NULL,claimed_at=NULL
                    WHERE doctor_url=? AND claimed_by=?
                    """,
                    (message, message, now_iso(), url, smoke_runner),
                )
                conn.commit()
                export_profile_queue(conn, workspace)
            raise RuntimeError("Restriction detected. Smoke test stopped.")
        path = find_profile_html(workspace, url)
        if path:
            with connect(workspace) as conn:
                mark_profile_saved(conn, workspace, url, path)
            return url, path
        time.sleep(0.5)

    with connect(workspace) as conn:
        message = f"No saved HTML after {timeout_seconds}s"
        conn.execute(
            """
            UPDATE doctor_profiles
            SET status='timeout',error=?,last_error=?,last_seen_at=?,
                claimed_by=NULL,claimed_at=NULL
            WHERE doctor_url=? AND claimed_by=?
            """,
            (message, message, now_iso(), url, smoke_runner),
        )
        conn.commit()
        export_profile_queue(conn, workspace)
    raise TimeoutError(f"No saved profile HTML after {timeout_seconds}s: {url}")
