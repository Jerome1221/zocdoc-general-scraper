from __future__ import annotations

import csv
import sqlite3
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .workspace import Workspace


STAGE_TIMING_FIELDS = [
    "navigation_seconds",
    "stabilization_seconds",
    "serialize_seconds",
    "disk_write_seconds",
    "server_save_seconds",
    "validation_seconds",
    "listing_db_seconds",
    "trace_seconds",
    "profile_sync_seconds",
    "cleanup_seconds",
    "output_seconds",
    "profile_mark_db_seconds",
]

EVENT_FIELDS = [
    "run_id",
    "runner_id",
    "command",
    "page_type",
    "status",
    "specialty_name",
    "location",
    "url",
    "duration_seconds",
    "doctor_count",
    "pagination_count",
    "provider_set_hash",
    *STAGE_TIMING_FIELDS,
    "recorded_at",
]

RUN_FIELDS = [
    "run_id",
    "runner_id",
    "command",
    "started_at",
    "ended_at",
    "elapsed_seconds",
    "processed",
    "saved",
    "existing",
    "timeout",
    "blocked",
    "average_seconds",
    "p50_seconds",
    "p95_seconds",
    "pages_per_minute",
    "concurrency",
    "stagger_seconds",
    "poll_seconds",
    "page_check_ms",
    "stable_checks",
    "page_max_wait_ms",
    "controller_poll_ms",
    *[f"avg_{field}" for field in STAGE_TIMING_FIELDS],
]


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _upgrade_csv_header(path: Path, fields: list[str]) -> None:
    """Upgrade an existing metrics CSV to the current schema without losing old rows.

    Dev9.4.2 added stage-timing columns to metrics files. Existing workspaces can
    therefore have an older header while newer rows already contain the expanded
    field set. Normalize both cases before appending so DictReader can address the
    timing columns by name.
    """
    if not path.exists() or path.stat().st_size == 0:
        return

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        raw_rows = list(csv.reader(handle))
    if not raw_rows:
        return

    old_header = raw_rows[0]
    if old_header == fields:
        return

    normalized: list[dict] = []
    for raw in raw_rows[1:]:
        if not raw:
            continue
        # A 9.4.2 row may already have been written with the new field order
        # beneath a legacy header. When lengths match, recover it directly.
        if len(raw) == len(fields):
            normalized.append(dict(zip(fields, raw)))
            continue
        # Genuine legacy rows use the legacy header; new fields stay blank.
        legacy = dict(zip(old_header, raw[: len(old_header)]))
        normalized.append({key: legacy.get(key, "") for key in fields})

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in normalized:
            writer.writerow({key: row.get(key, "") for key in fields})
    tmp.replace(path)


def _append_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _upgrade_csv_header(path, fields)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass
class PerformanceRecorder:
    workspace: Workspace
    command: str
    settings: dict[str, int | float | str]
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_wall: str = field(default_factory=_iso_now)
    started_mono: float = field(default_factory=time.monotonic)
    events: list[dict] = field(default_factory=list)
    _finished: bool = False

    def record(
        self,
        *,
        page_type: str,
        status: str,
        url: str,
        duration_seconds: float,
        specialty_name: str = "",
        location: str = "",
        doctor_count: int | str = "",
        pagination_count: int | str = "",
        provider_set_hash: str = "",
        timings: dict[str, float | int | str] | None = None,
    ) -> None:
        timing_values = timings or {}
        self.events.append(
            {
                "run_id": self.run_id,
                "runner_id": str(self.settings.get("runner_id") or "single"),
                "command": self.command,
                "page_type": page_type,
                "status": status,
                "specialty_name": specialty_name,
                "location": location,
                "url": url,
                "duration_seconds": round(max(0.0, duration_seconds), 3),
                "doctor_count": doctor_count,
                "pagination_count": pagination_count,
                "provider_set_hash": provider_set_hash,
                **{
                    field: (round(float(timing_values[field]), 4) if field in timing_values and timing_values[field] not in {None, ""} else "")
                    for field in STAGE_TIMING_FIELDS
                },
                "recorded_at": _iso_now(),
            }
        )

    def finish(self) -> dict:
        if self._finished:
            return {}
        self._finished = True
        elapsed = max(0.0, time.monotonic() - self.started_mono)
        durations = [float(row["duration_seconds"]) for row in self.events if row["status"] != "existing"]
        counts = {status: sum(row["status"] == status for row in self.events) for status in ("saved", "existing", "timeout", "blocked")}
        processed = len(self.events)
        # Very short synthetic/unit-test runs can complete within a single
        # monotonic-clock tick on some platforms (notably Windows). Use at
        # least the longest recorded page duration as the throughput window.
        # In real collection runs wall-clock elapsed is naturally longer than
        # any individual page duration, so this preserves real throughput.
        throughput_elapsed = max(elapsed, max(durations, default=0.0))
        stage_averages: dict[str, float | str] = {}
        for field in STAGE_TIMING_FIELDS:
            values = [
                float(row[field])
                for row in self.events
                if row.get(field) not in {None, ""}
            ]
            stage_averages[f"avg_{field}"] = round(statistics.fmean(values), 4) if values else ""

        summary = {
            "run_id": self.run_id,
            "runner_id": str(self.settings.get("runner_id") or "single"),
            "command": self.command,
            "started_at": self.started_wall,
            "ended_at": _iso_now(),
            "elapsed_seconds": round(elapsed, 3),
            "processed": processed,
            "saved": counts["saved"],
            "existing": counts["existing"],
            "timeout": counts["timeout"],
            "blocked": counts["blocked"],
            "average_seconds": round(statistics.fmean(durations), 3) if durations else 0.0,
            "p50_seconds": round(_percentile(durations, 0.50), 3),
            "p95_seconds": round(_percentile(durations, 0.95), 3),
            "pages_per_minute": round(
                (
                    sum(row["status"] != "existing" for row in self.events)
                    * 60.0
                    / throughput_elapsed
                ),
                3,
            ) if throughput_elapsed > 0 else 0.0,
            **stage_averages,
            **{key: self.settings.get(key, "") for key in RUN_FIELDS if key not in {
                "run_id", "runner_id", "command", "started_at", "ended_at", "elapsed_seconds", "processed",
                "saved", "existing", "timeout", "blocked", "average_seconds", "p50_seconds",
                "p95_seconds", "pages_per_minute", *stage_averages.keys()
            }},
        }
        runner_id = str(self.settings.get("runner_id") or "single")
        if runner_id == "single":
            event_path = self.workspace.output_dir / "capture_performance.csv"
            run_path = self.workspace.output_dir / "crawl_run_metrics.csv"
        else:
            safe_runner = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in runner_id)
            event_path = self.workspace.output_dir / f"capture_performance.{safe_runner}.csv"
            run_path = self.workspace.output_dir / f"crawl_run_metrics.{safe_runner}.csv"
        _append_rows(event_path, EVENT_FIELDS, self.events)
        _append_rows(run_path, RUN_FIELDS, [summary])
        return summary


def performance_report(workspace: Workspace, *, command: str = "collect-listings", last: int = 5) -> dict:
    path = workspace.output_dir / "crawl_run_metrics.csv"
    if not path.exists():
        return {"runs": 0, "command": command, "pending": 0, "pages_per_minute": 0.0, "eta_minutes": None}

    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("command") == command]
    rows = rows[-max(1, last):]
    throughputs = [float(row.get("pages_per_minute") or 0) for row in rows if float(row.get("pages_per_minute") or 0) > 0]
    pages_per_minute = statistics.median(throughputs) if throughputs else 0.0

    pending = 0
    if command == "collect-listings":
        conn = sqlite3.connect(workspace.db)
        try:
            pending = conn.execute("SELECT COUNT(*) FROM pages WHERE status IN ('pending','retry_pending')").fetchone()[0]
        finally:
            conn.close()
    elif command == "collect-profiles":
        conn = sqlite3.connect(workspace.db)
        try:
            pending = conn.execute("SELECT COUNT(*) FROM doctor_profiles WHERE status='pending'").fetchone()[0]
        finally:
            conn.close()
    elif command == "discover-targets":
        conn = sqlite3.connect(workspace.db)
        try:
            pending = conn.execute("SELECT COUNT(*) FROM specialty_pages WHERE status='pending'").fetchone()[0]
        finally:
            conn.close()

    eta_minutes = (pending / pages_per_minute) if pages_per_minute > 0 else None
    return {
        "runs": len(rows),
        "command": command,
        "pending": pending,
        "pages_per_minute": round(pages_per_minute, 3),
        "eta_minutes": round(eta_minutes, 2) if eta_minutes is not None else None,
        "recent": rows,
    }
