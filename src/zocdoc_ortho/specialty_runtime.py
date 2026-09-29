from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from .db import connect
from .workspace import Workspace

RUNTIME_FIELDS = [
    "specialty_name",
    "specialty_slug",
    "specialty_url",
    "step",
    "phase",
    "runner_mode",
    "started_at",
    "ended_at",
    "elapsed_seconds",
    "processed",
    "rate_per_minute",
]


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def resolve_specialty(workspace: Workspace, selector: str) -> dict:
    wanted = _normalize(selector)
    with connect(workspace) as conn:
        rows = conn.execute(
            """
            SELECT specialty_name,specialty_slug,specialty_url
            FROM specialties
            WHERE is_active=1
            ORDER BY lower(specialty_name),specialty_url
            """
        ).fetchall()
    exact = [
        dict(row)
        for row in rows
        if wanted in {
            _normalize(row["specialty_name"]),
            _normalize(row["specialty_slug"]),
            _normalize(row["specialty_url"]),
        }
    ]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        partial = [
            dict(row)
            for row in rows
            if wanted and (
                wanted in _normalize(row["specialty_name"])
                or wanted in _normalize(row["specialty_slug"])
                or wanted in _normalize(row["specialty_url"])
            )
        ]
        if len(partial) == 1:
            return partial[0]
    raise ValueError(f"Could not resolve one specialty from selector: {selector!r}")


def append_specialty_runtime(
    workspace: Workspace,
    *,
    specialty: dict,
    step: str,
    elapsed_seconds: float,
    processed: int,
    phase: str = "",
    runner_mode: str = "single",
    started_at: str = "",
) -> None:
    path = workspace.output_dir / "specialty_runtime.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    elapsed = max(float(elapsed_seconds), 0.0)
    rate = (processed * 60.0 / elapsed) if elapsed > 0 else 0.0
    row = {
        "specialty_name": specialty["specialty_name"],
        "specialty_slug": specialty["specialty_slug"],
        "specialty_url": specialty["specialty_url"],
        "step": step,
        "phase": phase,
        "runner_mode": runner_mode,
        "started_at": started_at or "",
        "ended_at": _iso_now(),
        "elapsed_seconds": round(elapsed, 3),
        "processed": int(processed),
        "rate_per_minute": round(rate, 3),
    }
    with path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUNTIME_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _specialty_clause(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"(lower({prefix}specialty_slug)=? OR lower({prefix}specialty_name)=? "
        f"OR lower({prefix}specialty_url)=?)"
    )


def _trace_specialty_clause(alias: str = "listing_provider_trace") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"(lower({prefix}listing_specialty_slug)=? OR lower({prefix}listing_specialty_name)=? "
        f"OR lower({prefix}listing_specialty_url)=?)"
    )


def _read_runtime_rows(path: Path, specialty: dict) -> list[dict]:
    if not path.exists():
        return []
    wanted = _normalize(specialty["specialty_slug"])
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            row
            for row in csv.DictReader(handle)
            if _normalize(row.get("specialty_slug", "")) == wanted
        ]


def specialty_runtime_report(workspace: Workspace, selector: str) -> dict:
    specialty = resolve_specialty(workspace, selector)
    params = [
        _normalize(specialty["specialty_slug"]),
        _normalize(specialty["specialty_name"]),
        _normalize(specialty["specialty_url"]),
    ]
    with connect(workspace) as conn:
        targets = conn.execute(
            f"SELECT COUNT(*) n FROM specialty_targets WHERE is_active=1 AND {_specialty_clause()}",
            params,
        ).fetchone()["n"]
        page_rows = conn.execute(
            f"""
            SELECT source,status,COUNT(*) n
            FROM pages
            WHERE {_specialty_clause()}
            GROUP BY source,status
            """,
            params,
        ).fetchall()
        providers = conn.execute(
            f"""
            SELECT COUNT(DISTINCT doctor_url) n
            FROM listing_provider_trace
            WHERE {_trace_specialty_clause('listing_provider_trace')}
            """,
            params,
        ).fetchone()["n"]
        first_seen_providers = conn.execute(
            f"""
            SELECT COUNT(DISTINCT d.doctor_url) n
            FROM doctors d
            WHERE (lower(d.first_seen_specialty_name)=? OR lower(d.first_seen_specialty_url)=?)
              AND EXISTS (
                  SELECT 1 FROM listing_provider_trace t
                  WHERE t.doctor_url=d.doctor_url
                    AND {_trace_specialty_clause('t')}
              )
            """,
            [
                _normalize(specialty["specialty_name"]),
                _normalize(specialty["specialty_url"]),
                *params,
            ],
        ).fetchone()["n"]
        profile_rows = conn.execute(
            f"""
            SELECT dp.status,COUNT(DISTINCT dp.doctor_url) n
            FROM doctor_profiles dp
            JOIN doctors d ON d.doctor_url=dp.doctor_url
            WHERE (lower(d.first_seen_specialty_name)=? OR lower(d.first_seen_specialty_url)=?)
              AND EXISTS (
                  SELECT 1 FROM listing_provider_trace t
                  WHERE t.doctor_url=dp.doctor_url
                    AND {_trace_specialty_clause('t')}
              )
            GROUP BY dp.status
            """,
            [
                _normalize(specialty["specialty_name"]),
                _normalize(specialty["specialty_url"]),
                *params,
            ],
        ).fetchall()

    page_counts: dict[str, dict[str, int]] = {"seed": {}, "pagination": {}}
    for row in page_rows:
        page_counts.setdefault(row["source"], {})[row["status"]] = int(row["n"])
    profile_counts = {row["status"]: int(row["n"]) for row in profile_rows}

    rows = _read_runtime_rows(workspace.output_dir / "specialty_runtime.csv", specialty)
    step_rows: dict[str, list[dict]] = {}
    for row in rows:
        step_rows.setdefault(row.get("step", ""), []).append(row)

    expected = {
        "target_discovery": 1,
        "base_listings": int(targets),
        "pagination": sum(page_counts.get("pagination", {}).values()),
        "profiles": int(first_seen_providers),
    }

    steps = []
    for step in ("target_discovery", "base_listings", "pagination", "profiles"):
        measured = step_rows.get(step, [])
        elapsed = sum(float(row.get("elapsed_seconds") or 0) for row in measured)
        processed = sum(int(float(row.get("processed") or 0)) for row in measured)
        rate = (processed * 60.0 / elapsed) if elapsed > 0 and processed > 0 else 0.0
        expected_count = int(expected.get(step, 0))
        projected_seconds = (expected_count / rate * 60.0) if rate > 0 and expected_count > 0 else None
        steps.append(
            {
                "step": step,
                "measured_runs": len(measured),
                "measured_processed": processed,
                "measured_elapsed_seconds": round(elapsed, 3),
                "rate_per_minute": round(rate, 3),
                "expected_count": expected_count,
                "projected_full_seconds": round(projected_seconds, 3) if projected_seconds is not None else None,
            }
        )

    projected = [row["projected_full_seconds"] for row in steps if row["projected_full_seconds"] is not None]
    return {
        "specialty": specialty,
        "targets": int(targets),
        "page_counts": page_counts,
        "providers": int(providers),
        "first_seen_providers": int(first_seen_providers),
        "profile_counts": profile_counts,
        "steps": steps,
        "projected_total_seconds": round(sum(projected), 3) if projected else None,
        "runtime_csv": str(workspace.output_dir / "specialty_runtime.csv"),
    }
