from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pandas as pd

from .db import connect
from .profile import parse_profile_rows
from .schema import LOCATION_LEVEL_COLUMNS, REQUIRED_COLUMNS
from .trace import find_profile_html, trace_rows_for_doctor
from .urls import normalize_url
from .utils import tri_bool
from .workspace import Workspace
from .zip_inputs import build_scraped_zip_inputs


def export_final(
    workspace: Workspace,
    *,
    allow_partial: bool = False,
    source_zip_mode: str = "office",
    write_derived: bool = True,
) -> dict[str, Path | int]:
    export_started = time.monotonic()
    workspace.ensure()
    with connect(workspace) as conn:
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status,COUNT(*) n FROM doctor_profiles GROUP BY status"
            ).fetchall()
        }
        unfinished = sum(counts.get(status, 0) for status in ("pending", "opening", "timeout", "blocked"))
        if unfinished and not allow_partial:
            raise RuntimeError(
                f"Profile collection is not complete. Unfinished rows: {unfinished:,}. "
                "Finish/review the queue or pass --allow-partial for a deliberate partial export."
            )
        saved_profiles = conn.execute(
            """
            SELECT doctor_url,doctor_name,file,status
            FROM doctor_profiles
            WHERE status='saved'
            ORDER BY doctor_name,doctor_url
            """
        ).fetchall()

    records: list[dict] = []
    parse_errors: list[dict] = []
    provider_trace_rows: list[dict] = []
    profile_parse_durations: list[float] = []
    html_read_durations: list[float] = []
    trace_query_durations: list[float] = []
    parser_durations: list[float] = []

    for saved in saved_profiles:
        url = normalize_url(saved["doctor_url"])
        path = find_profile_html(workspace, url)
        if not path:
            parse_errors.append(
                {
                    "doctor_url": url,
                    "doctor_name": saved["doctor_name"],
                    "error": "status=saved but profile HTML file not found",
                }
            )
            continue

        try:
            profile_started = time.monotonic()
            html_started = time.monotonic()
            html = path.read_text(encoding="utf-8", errors="replace")
            html_read_durations.append(max(0.0, time.monotonic() - html_started))
            trace_started = time.monotonic()
            with connect(workspace) as conn:
                trace_rows = trace_rows_for_doctor(conn, url)
            trace_query_durations.append(max(0.0, time.monotonic() - trace_started))
            parser_started = time.monotonic()
            parsed_rows, diagnostics = parse_profile_rows(
                url,
                html,
                trace_rows=trace_rows,
                source_zip_mode=source_zip_mode,
            )
            parser_durations.append(max(0.0, time.monotonic() - parser_started))
            profile_parse_durations.append(max(0.0, time.monotonic() - profile_started))
            if not parsed_rows:
                raise ValueError("Parser returned zero rows")
            records.extend(parsed_rows)
            provider_trace_rows.append(
                {
                    "doctor_url": url,
                    "doctor_name": saved["doctor_name"],
                    "profile_file": path.name,
                    "listing_occurrence_count": len(trace_rows),
                    "listing_page_count": len({row.get("listing_url") for row in trace_rows if row.get("listing_url")}),
                    "redux_found": diagnostics.get("redux_found"),
                    "provider_found": diagnostics.get("provider_found"),
                    "telemedicine_evidence": diagnostics.get("telemedicine_evidence", ""),
                    "parsed_location_rows": len(parsed_rows),
                    "can_have_appointments": diagnostics.get("can_have_appointments"),
                    "can_have_appointments_source": diagnostics.get("can_have_appointments_source"),
                    "listing_can_have_values": json.dumps(diagnostics.get("listing_can_have_values", [])),
                    "listing_can_have_conflict": diagnostics.get("listing_can_have_conflict"),
                    "profile_is_bookable": diagnostics.get("profile_is_bookable"),
                    "profile_is_preview": diagnostics.get("profile_is_preview"),
                    "marketplace_enabled": diagnostics.get("marketplace_enabled"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - isolate malformed profile records in batch export
            parse_errors.append(
                {
                    "doctor_url": url,
                    "doctor_name": saved["doctor_name"],
                    "error": repr(exc),
                }
            )

    final_df = pd.DataFrame(records, columns=REQUIRED_COLUMNS)
    if len(final_df):
        final_df["_dedupe_key"] = final_df.apply(_provider_location_dedupe_key, axis=1)
        final_df = (
            final_df.drop_duplicates(subset=["_dedupe_key"], keep="first")
            .drop(columns=["_dedupe_key"])
            .reset_index(drop=True)
        )

    final_path = workspace.output_dir / "final_providers.csv"
    repo_final_path = workspace.output_dir / "zocdoc_orthopedic_surgeons_all_states.csv"
    qa_path = workspace.output_dir / "final_providers_qa.csv"
    trace_path = workspace.output_dir / "provider_trace.csv"
    errors_path = workspace.output_dir / "profile_parse_errors.csv"

    output_write_started = time.monotonic()
    final_df.to_csv(final_path, index=False, encoding="utf-8-sig")
    final_df.to_csv(repo_final_path, index=False, encoding="utf-8-sig")

    qa_df = _qa(final_df)
    qa_df.to_csv(qa_path, index=False, encoding="utf-8-sig")
    provider_trace_df = pd.DataFrame(provider_trace_rows)
    provider_trace_df.to_csv(trace_path, index=False, encoding="utf-8-sig")
    parse_errors_df = pd.DataFrame(parse_errors, columns=["doctor_url", "doctor_name", "error"])
    parse_errors_df.to_csv(errors_path, index=False, encoding="utf-8-sig")
    output_write_seconds = max(0.0, time.monotonic() - output_write_started)

    def _mean(values: list[float]) -> float:
        return round(statistics.fmean(values), 4) if values else 0.0

    def _p95(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95))))
        return round(ordered[index], 4)

    result: dict[str, Path | int | float] = {
        "saved_profiles": len(saved_profiles),
        "final_rows": len(final_df),
        "parse_errors": len(parse_errors_df),
        "final": final_path,
        "repo_final": repo_final_path,
        "qa": qa_path,
        "trace": trace_path,
        "errors": errors_path,
        "avg_profile_parse_seconds": _mean(profile_parse_durations),
        "p95_profile_parse_seconds": _p95(profile_parse_durations),
        "avg_profile_html_read_seconds": _mean(html_read_durations),
        "avg_trace_query_seconds": _mean(trace_query_durations),
        "avg_parser_seconds": _mean(parser_durations),
        "output_write_seconds": round(output_write_seconds, 4),
    }
    derived_started = time.monotonic()
    if write_derived:
        result.update(derive_outputs(workspace, final_df=final_df))
    result["derived_write_seconds"] = round(max(0.0, time.monotonic() - derived_started), 4) if write_derived else 0.0
    result["total_export_seconds"] = round(max(0.0, time.monotonic() - export_started), 4)
    return result


def derive_outputs(workspace: Workspace, final_df: pd.DataFrame | None = None) -> dict[str, Path | int]:
    workspace.ensure()
    final_path = workspace.output_dir / "final_providers.csv"
    if final_df is None:
        if not final_path.exists():
            raise FileNotFoundError(final_path)
        final_df = pd.read_csv(final_path, dtype=str, keep_default_na=False)
    else:
        final_df = final_df.copy()

    if "provider_id" not in final_df.columns:
        raise ValueError("final_providers.csv does not have the canonical provider columns")

    working = final_df.copy()
    working["_provider_key"] = working.apply(_provider_identity, axis=1)
    working["_locations_rank"] = working.get("locations", "").astype(str).str.strip().ne("").astype(int)

    unique_doctors = (
        working.sort_values(["_provider_key", "_locations_rank"], ascending=[True, False])
        .drop_duplicates(subset=["_provider_key"], keep="first")
        .drop(
            columns=["_provider_key", "_locations_rank"]
            + [column for column in LOCATION_LEVEL_COLUMNS if column in working.columns]
        )
        .reset_index(drop=True)
    )

    virtual_mask = working["is_virtual_location"].map(tri_bool).eq(True)
    physical_mask = working["is_virtual_location"].map(tri_bool).eq(False)
    virtual_df = working.loc[virtual_mask].drop(columns=["_provider_key", "_locations_rank"], errors="ignore").reset_index(drop=True)
    physical_df = working.loc[physical_mask].drop(columns=["_provider_key", "_locations_rank"], errors="ignore").reset_index(drop=True)
    unknown_count = int(working["is_virtual_location"].map(tri_bool).isna().sum())

    unique_path = workspace.output_dir / "unique_doctors.csv"
    virtual_path = workspace.output_dir / "doctor_locations_virtual.csv"
    physical_path = workspace.output_dir / "doctor_locations_physical.csv"
    unique_doctors.to_csv(unique_path, index=False, encoding="utf-8-sig")
    virtual_df.to_csv(virtual_path, index=False, encoding="utf-8-sig")
    physical_df.to_csv(physical_path, index=False, encoding="utf-8-sig")

    result: dict[str, Path | int] = {
        "unique_doctors": unique_path,
        "unique_doctor_rows": len(unique_doctors),
        "virtual_locations": virtual_path,
        "virtual_rows": len(virtual_df),
        "physical_locations": physical_path,
        "physical_rows": len(physical_df),
        "unknown_virtual_flag_rows": unknown_count,
    }
    result.update(build_scraped_zip_inputs(workspace, final_df=final_df))
    return result


def _provider_location_dedupe_key(row: pd.Series) -> str:
    provider_location_key = _clean(row.get("provider_location_key"))
    if provider_location_key:
        return f"plk::{provider_location_key}"
    return "fallback::" + "::".join(
        [
            _clean(row.get("provider_id")),
            _clean(row.get("npi")),
            _clean(row.get("address_line_1")).lower(),
            _clean(row.get("city")).lower(),
            _clean(row.get("state")).upper(),
            _clean(row.get("postal_code")),
        ]
    )


def _provider_identity(row: pd.Series) -> str:
    provider_id = _clean(row.get("provider_id"))
    npi = _clean(row.get("npi"))
    profile_url = _clean(row.get("profile_url"))
    if provider_id:
        return "provider::" + provider_id
    if npi:
        return "npi::" + npi
    return "url::" + profile_url


def _clean(value) -> str:
    if value is None:
        return ""
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return ""
    return str(value).strip()


def _qa(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for column in REQUIRED_COLUMNS:
        if len(frame):
            series = frame[column]
            non_empty = int(
                (
                    series.notna()
                    & series.astype(str).str.strip().ne("")
                    & series.astype(str).str.lower().ne("nan")
                ).sum()
            )
        else:
            non_empty = 0
        rows.append(
            {
                "column": column,
                "non_empty": non_empty,
                "total_rows": len(frame),
                "pct_non_empty": round(100 * non_empty / len(frame), 1) if len(frame) else 0,
            }
        )
    return pd.DataFrame(rows)
