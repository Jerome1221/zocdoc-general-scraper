"""Resumable, offline canonical profile imports from retained captures."""
from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import closing

from .daily_sync import _store_profile_html, daily_sync_lock
from .db import connect
from .profile_availability import profile_unavailable_reason
from .profile_challenges import profile_challenge_reason
from .trace import find_profile_html
from .workspace import Workspace


def _selectors(specialties: tuple[str, ...]) -> tuple[str, list[str]]:
    values = [v.strip().lower() for v in specialties if v.strip()]
    if specialties and not values:
        raise ValueError("Specialty filters cannot be blank")
    if not values:
        return "", []
    marks = ",".join("?" for _ in values)
    return f" AND (lower(specialty_slug) IN ({marks}) OR lower(specialty) IN ({marks}))", values * 2


def _pending_local_profiles(conn, *, run_id: int, specialties: tuple[str, ...]):
    run = conn.execute("SELECT id FROM daily_sync_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Run {run_id} does not exist")
    clause, params = _selectors(specialties)
    return conn.execute(
        f"""SELECT m.doctor_url,d.status FROM
        (SELECT DISTINCT doctor_url FROM provider_location_memberships
         WHERE is_active=1 AND last_seen_run_id=?{clause}) m
        LEFT JOIN doctor_profiles d ON d.doctor_url=m.doctor_url
        LEFT JOIN canonical_provider_records c ON c.doctor_url=m.doctor_url
        WHERE COALESCE(c.profile_sha256,'')=''
           OR COALESCE(c.profile_data_json,'[]') IN ('','[]')
        ORDER BY m.doctor_url""", [run_id, *params],
    ).fetchall()


def _import_local_profiles(
    conn, workspace: Workspace, *, run_id: int, specialties: tuple[str, ...] = (),
    limit: int | None = None, batch_size: int = 10, min_free_gb: float = 3,
    dry_run: bool = False,
) -> dict:
    if limit is not None and limit < 1:
        raise ValueError("Profile limit must be positive; omit it for all")
    if batch_size < 1 or min_free_gb < 0:
        raise ValueError("Batch size must be positive and minimum free space nonnegative")
    pending = _pending_local_profiles(conn, run_id=run_id, specialties=specialties)
    result = {"candidates": 0, "selected": 0, "imported": 0, "rejected": 0, "missing_html": 0,
              "unavailable_skipped": 0, "not_saved_skipped": 0}
    selected = []
    print(f"LOCAL IMPORT | inspecting {len(pending):,} records without canonical details", flush=True)
    for row in pending:
        if row["status"] == "unavailable":
            result["unavailable_skipped"] += 1
            continue
        if row["status"] != "saved":
            result["not_saved_skipped"] += 1
            continue
        path = find_profile_html(workspace, row["doctor_url"])
        if path is None:
            result["missing_html"] += 1
            print(f"MISSING HTML | {row['doctor_url']}", flush=True)
            continue
        result["candidates"] += 1
        if limit is None or len(selected) < limit:
            selected.append((row["doctor_url"], path))
    result["selected"] = len(selected)
    if dry_run:
        return result
    started = time.monotonic()
    uncommitted = 0
    try:
        for index, (url, path) in enumerate(selected, 1):
            if shutil.disk_usage(workspace.root).free < min_free_gb * 1024**3:
                raise RuntimeError("Insufficient disk space; previously committed batches remain saved")
            html = path.read_text(encoding="utf-8", errors="replace")
            reason = profile_unavailable_reason(html) or profile_challenge_reason(html)
            valid = False
            if not reason:
                if not conn.in_transaction:
                    conn.execute("BEGIN")
                conn.execute("SAVEPOINT local_profile")
                valid, reason = _store_profile_html(conn, run_id=run_id, doctor_url=url, path=path, html=html)
                if not valid:
                    conn.execute("ROLLBACK TO local_profile")
                conn.execute("RELEASE local_profile")
            if valid:
                uncommitted += 1
            else:
                result["rejected"] += 1
                print(f"LOCAL REJECTED | {url} | {reason}", flush=True)
            if index % batch_size == 0 or index == len(selected):
                conn.commit()
                result["imported"] += uncommitted
                uncommitted = 0
                print(f"LOCAL IMPORT | {index:,}/{len(selected):,} | committed={result['imported']:,} "
                      f"rejected={result['rejected']:,} | elapsed={(time.monotonic()-started)/60:.1f}m", flush=True)
    except BaseException:
        conn.rollback()
        print(f"LOCAL IMPORT STOPPED | committed={result['imported']:,}; rerun to resume", flush=True)
        raise
    return result


def import_local_profiles(
    workspace: Workspace, *, run_id: int, specialties: tuple[str, ...] = (),
    limit: int | None = None, batch_size: int = 10, min_free_gb: float = 3,
    dry_run: bool = False,
) -> dict:
    """Preserve memberships, collector states and history; never browse or queue work."""
    kwargs = {"run_id": run_id, "specialties": specialties, "limit": limit,
              "batch_size": batch_size, "min_free_gb": min_free_gb, "dry_run": dry_run}
    if dry_run:
        with closing(sqlite3.connect(workspace.db.resolve().as_uri()+"?mode=ro", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            return _import_local_profiles(conn, workspace, **kwargs)
    with daily_sync_lock(workspace), closing(connect(workspace)) as conn:
        return _import_local_profiles(conn, workspace, **kwargs)
