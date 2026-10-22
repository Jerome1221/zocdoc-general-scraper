"""Explicit bookkeeping for stopped runs; never declares data collection complete."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from .daily_sync import daily_sync_lock
from .db import connect
from .utils import now_iso
from .workspace import Workspace


def _report(conn, run_id: int) -> dict:
    run = conn.execute("SELECT * FROM daily_sync_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Run {run_id} does not exist")
    if run["status"] not in {"running", "interrupted", "failed", "partial"}:
        raise ValueError("Only unfinished runs can be reconciled")
    locations = conn.execute("""SELECT COUNT(*) total,
        COALESCE(SUM(status='complete'),0) completed,
        COALESCE(SUM(status='failed'),0) failed,
        COALESCE(SUM(pages_fetched),0) pages,
        COALESCE(SUM(providers_seen),0) providers
        FROM daily_sync_locations WHERE run_id=?""", (run_id,)).fetchone()
    jobs = dict(conn.execute("SELECT status,COUNT(*) FROM daily_profile_queue WHERE run_id=? GROUP BY status", (run_id,)))
    return {"run_id": run_id, "previous_status": run["status"], "status": "interrupted",
            "locations_selected": locations["total"], "locations_completed": locations["completed"],
            "locations_failed": locations["failed"], "pages_fetched": locations["pages"],
            "providers_seen": locations["providers"], "profiles_queued": sum(jobs.values()),
            "profiles_saved": jobs.get("saved", 0), "profiles_failed": jobs.get("failed", 0)}


def reconcile_interrupted_run(workspace: Workspace, *, run_id: int, apply: bool = False) -> dict:
    """Caller must first stop its worker; the updater lock prevents concurrent imports."""
    if not apply:
        with closing(sqlite3.connect(workspace.db.resolve().as_uri()+"?mode=ro", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            return {**_report(conn, run_id), "applied": False}
    with daily_sync_lock(workspace), connect(workspace) as conn:
        result = _report(conn, run_id)
        conn.execute("""UPDATE daily_sync_runs SET status='interrupted',completed_at=?,
                locations_selected=?,locations_completed=?,locations_failed=?,pages_fetched=?,providers_seen=?,
                profiles_queued=?,profiles_saved=?,profiles_failed=?,error=? WHERE id=?""",
            (now_iso(), result["locations_selected"], result["locations_completed"],
             result["locations_failed"], result["pages_fetched"], result["providers_seen"],
             result["profiles_queued"], result["profiles_saved"], result["profiles_failed"],
             "Stopped by operator; committed work retained. Completion was not asserted.", run_id))
        conn.commit()
        return {**result, "applied": True}
