from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from .db import connect, trace_table_exists
from .listing import extract_listing_occurrences, parse_page
from .outputs import export_profile_queue
from .schema import TRACE_COLUMNS
from .urls import is_profile_url, normalize_url, safe_slug
from .utils import now_iso
from .workspace import Workspace


def find_listing_html(workspace: Workspace, url: str) -> Path | None:
    path = workspace.listing_dir / safe_slug(normalize_url(url))
    return path if path.exists() else None


def find_profile_html(workspace: Workspace, url: str) -> Path | None:
    path = workspace.profile_dir / safe_slug(normalize_url(url))
    return path if path.exists() else None


def trace_rows_for_doctor(conn: sqlite3.Connection, doctor_url: str) -> list[dict]:
    if not trace_table_exists(conn):
        return []
    rows = conn.execute(
        """
        SELECT * FROM listing_provider_trace
        WHERE doctor_url=?
        ORDER BY listing_url,card_index
        """,
        (normalize_url(doctor_url),),
    ).fetchall()
    return [dict(row) for row in rows]


def sync_profiles_from_doctors(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    doctor_urls: Iterable[str] | None = None,
    write_output: bool = True,
) -> int:
    selected = sorted({normalize_url(url) for url in (doctor_urls or []) if url})
    if selected:
        placeholders = ",".join("?" for _ in selected)
        doctors = conn.execute(
            f"SELECT doctor_url,doctor_name FROM doctors WHERE doctor_url IN ({placeholders}) ORDER BY doctor_url",
            selected,
        ).fetchall()
    elif doctor_urls is not None:
        doctors = []
    else:
        doctors = conn.execute("SELECT doctor_url,doctor_name FROM doctors ORDER BY doctor_url").fetchall()
    count = 0
    for doctor in doctors:
        url = normalize_url(doctor["doctor_url"] or "")
        if not is_profile_url(url):
            continue
        conn.execute(
            """
            INSERT INTO doctor_profiles(doctor_url,doctor_name,status,discovered_at)
            VALUES (?,?,'pending',?)
            ON CONFLICT(doctor_url) DO UPDATE SET
                doctor_name=CASE
                    WHEN doctor_profiles.doctor_name='' THEN excluded.doctor_name
                    ELSE doctor_profiles.doctor_name END
            """,
            (url, doctor["doctor_name"] or "", now_iso()),
        )
        count += 1
    conn.commit()
    if write_output:
        export_profile_queue(conn, workspace)
    return count


def _normalize_record_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def persist_listing_trace_html(
    conn: sqlite3.Connection,
    *,
    page_row: sqlite3.Row | dict,
    html: str,
    source_file: str,
    commit: bool = True,
) -> int:
    """Persist one listing trace directly from an in-memory HTML capture.

    Dev9 uses this immediately after a normal browser capture is ingested, so the
    raw HTML can be deleted after the SQLite commit without requiring a later
    trace pass. No additional browser interaction is performed.
    """
    url = normalize_url(page_row["url"])
    occurrences = extract_listing_occurrences(
        url,
        html,
        {
            "state_group": page_row["state_group"],
            "location": page_row["location"],
            "page_no": page_row["page_no"],
            "specialty_url": page_row["specialty_url"],
            "specialty_name": page_row["specialty_name"],
            "specialty_slug": page_row["specialty_slug"],
        },
        source_file,
    )

    # A per-page replace makes re-parsing idempotent without ever dropping the
    # trace history for other pages whose raw HTML may already have been pruned.
    conn.execute("DELETE FROM listing_provider_trace WHERE listing_url=?", (url,))
    if occurrences:
        placeholders = ",".join("?" for _ in TRACE_COLUMNS)
        values = [
            tuple(_normalize_record_value(record.get(column)) for column in TRACE_COLUMNS)
            for record in occurrences
        ]
        conn.executemany(
            f"INSERT INTO listing_provider_trace ({','.join(TRACE_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    conn.execute(
        """
        UPDATE pages
        SET trace_status='parsed', trace_parsed_at=?, trace_occurrence_count=?,
            trace_source_file=?, trace_html_deleted_at=NULL
        WHERE url=?
        """,
        (now_iso(), len(occurrences), source_file, url),
    )
    if commit:
        conn.commit()
    return len(occurrences)


def _persist_listing_trace(
    conn: sqlite3.Connection,
    *,
    page_row: sqlite3.Row | dict,
    path: Path,
    commit: bool = True,
) -> int:
    """Replace the trace rows for one listing page and mark the page durably parsed."""
    html = path.read_text(encoding="utf-8", errors="replace")
    return persist_listing_trace_html(
        conn,
        page_row=page_row,
        html=html,
        source_file=path.name,
        commit=commit,
    )


def _migrate_legacy_trace_markers(conn: sqlite3.Connection, workspace: Workspace) -> int:
    """Preserve old trace history when its original raw HTML has already been removed.

    If raw HTML is still present, leave the page unparsed so the current parser can
    refresh that page's trace rows. This avoids treating a newly re-captured page as
    parsed merely because an older trace row exists for the same URL.
    """
    rows = conn.execute(
        """
        SELECT p.url,p.saved_at,p.discovered_at,p.file,
               COUNT(t.listing_url) AS occurrence_count
        FROM pages p
        JOIN listing_provider_trace t ON t.listing_url=p.url
        WHERE COALESCE(p.trace_status,'')<>'parsed'
        GROUP BY p.url
        """
    ).fetchall()
    migrated = 0
    for row in rows:
        if find_listing_html(workspace, row["url"]) is not None:
            continue
        conn.execute(
            """
            UPDATE pages
            SET trace_status='parsed',
                trace_parsed_at=COALESCE(trace_parsed_at,saved_at,discovered_at),
                trace_occurrence_count=?,
                trace_source_file=COALESCE(NULLIF(trace_source_file,''),file)
            WHERE url=?
            """,
            (int(row["occurrence_count"] or 0), normalize_url(row["url"])),
        )
        migrated += 1
    conn.commit()
    return migrated


def _trace_frame_from_db(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        f"SELECT {','.join(TRACE_COLUMNS)} FROM listing_provider_trace ORDER BY listing_url,card_index"
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=TRACE_COLUMNS)
    return pd.DataFrame([dict(row) for row in rows], columns=TRACE_COLUMNS)


def _export_trace_outputs(
    conn: sqlite3.Connection,
    workspace: Workspace,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Regenerate trace CSVs from durable SQLite state, not raw HTML files."""
    trace_df = _trace_frame_from_db(conn)
    trace_df.to_csv(
        workspace.output_dir / "listing_doctor_occurrences_nondedup.csv",
        index=False,
        encoding="utf-8-sig",
    )

    if len(trace_df):
        summary_df = (
            trace_df.groupby("doctor_url", as_index=False)
            .agg(
                doctor_name=("doctor_name", "first"),
                appearance_count=("doctor_url", "size"),
                listing_page_count=("listing_url", "nunique"),
                provider_id=("provider_id", _first_text),
                monolith_id=("monolith_id", _first_text),
                npi=("npi", _first_text),
            )
            .sort_values(["appearance_count", "doctor_name"], ascending=[False, True])
            .reset_index(drop=True)
        )
    else:
        summary_df = pd.DataFrame(
            columns=[
                "doctor_url",
                "doctor_name",
                "appearance_count",
                "listing_page_count",
                "provider_id",
                "monolith_id",
                "npi",
            ]
        )

    summary_df.to_csv(
        workspace.output_dir / "doctor_occurrence_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary_df.to_csv(
        workspace.output_dir / "unique_doctors_from_listings.csv",
        index=False,
        encoding="utf-8-sig",
    )

    if len(trace_df):
        specialty_observations = (
            trace_df.loc[trace_df["listing_specialty_url"].astype(str).str.strip() != ""]
            .groupby(
                [
                    "doctor_url",
                    "listing_specialty_url",
                    "listing_specialty_name",
                    "listing_specialty_slug",
                ],
                as_index=False,
                dropna=False,
            )
            .agg(
                doctor_name=("doctor_name", "first"),
                appearance_count=("listing_url", "size"),
                listing_page_count=("listing_url", "nunique"),
            )
            .sort_values(
                ["doctor_url", "listing_specialty_name", "listing_specialty_url"],
                kind="stable",
            )
        )
    else:
        specialty_observations = pd.DataFrame(
            columns=[
                "doctor_url",
                "doctor_name",
                "listing_specialty_url",
                "listing_specialty_name",
                "listing_specialty_slug",
                "appearance_count",
                "listing_page_count",
            ]
        )
    specialty_observations.to_csv(
        workspace.output_dir / "provider_specialty_observations.csv",
        index=False,
        encoding="utf-8-sig",
    )

    for _, doctor in summary_df.iterrows():
        url = normalize_url(str(doctor["doctor_url"]))
        if not is_profile_url(url):
            continue
        conn.execute(
            """
            INSERT INTO doctor_profiles(doctor_url,doctor_name,status,discovered_at)
            VALUES (?,?,'pending',?)
            ON CONFLICT(doctor_url) DO UPDATE SET
                doctor_name=CASE
                    WHEN doctor_profiles.doctor_name='' THEN excluded.doctor_name
                    ELSE doctor_profiles.doctor_name END
            """,
            (url, str(doctor["doctor_name"] or ""), now_iso()),
        )
    conn.commit()
    export_profile_queue(conn, workspace)
    return trace_df, summary_df



def repair_trace_partials(
    workspace: Workspace,
    *,
    limit: int | None = None,
    delete_html: bool = False,
) -> dict[str, int]:
    """Offline-repair retained listing pages whose validation/trace coverage disagreed.

    Only pages already quarantined as ``review`` + ``trace_partial`` are considered.
    Their retained HTML is reparsed locally; no browser or Zocdoc request is made.
    A page is restored to ``saved`` only when every provider URL seen by the
    validator is also present in the durable listing trace.
    """
    workspace.ensure()
    selected = 0
    repaired = 0
    still_partial = 0
    missing_html = 0
    deleted = 0
    occurrences = 0

    with connect(workspace) as conn:
        sql = """
            SELECT url,state_group,location,page_no,file,
                   specialty_url,specialty_name,specialty_slug
            FROM pages
            WHERE status='review' AND validation_status='trace_partial'
            ORDER BY specialty_name,state_group,location,page_no,url
        """
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        rows = conn.execute(sql, params).fetchall()
        selected = len(rows)

        for row in rows:
            url = normalize_url(row["url"])
            path = find_listing_html(workspace, url)
            if path is None:
                missing_html += 1
                continue

            html = path.read_text(encoding="utf-8", errors="replace")
            expected = {
                normalize_url(d.get("doctor_url", ""))
                for d in parse_page(html, url).get("doctors", [])
                if d.get("doctor_url")
            }
            count = persist_listing_trace_html(
                conn,
                page_row=row,
                html=html,
                source_file=path.name,
                commit=True,
            )
            traced = {
                normalize_url(r["doctor_url"])
                for r in conn.execute(
                    "SELECT DISTINCT doctor_url FROM listing_provider_trace WHERE listing_url=?",
                    (url,),
                ).fetchall()
                if r["doctor_url"]
            }
            missing = expected.difference(traced)
            if expected and not missing:
                reason = f"offline trace repair complete: {len(traced)} provider link(s) persisted"
                conn.execute(
                    """
                    UPDATE pages
                    SET status='saved',validation_status='valid_repaired',
                        validation_reason=?,error='',trace_status='parsed',
                        trace_occurrence_count=?
                    WHERE url=?
                    """,
                    (reason, count, url),
                )
                conn.commit()
                sync_profiles_from_doctors(
                    conn, workspace, doctor_urls=traced, write_output=False
                )
                repaired += 1
                occurrences += count
                if delete_html and path.exists():
                    path.unlink()
                    conn.execute(
                        "UPDATE pages SET trace_html_deleted_at=? WHERE url=?",
                        (now_iso(), url),
                    )
                    conn.commit()
                    deleted += 1
            else:
                reason = (
                    f"trace coverage incomplete after offline repair: {len(missing)} of "
                    f"{len(expected)} validated provider link(s) missing"
                )
                conn.execute(
                    """
                    UPDATE pages
                    SET status='review',validation_status='trace_partial',
                        validation_reason=?,error=?,trace_status='partial',
                        trace_occurrence_count=?
                    WHERE url=?
                    """,
                    (reason, reason, count, url),
                )
                conn.commit()
                still_partial += 1

        export_profile_queue(conn, workspace)

    return {
        "selected": selected,
        "repaired": repaired,
        "still_partial": still_partial,
        "missing_html": missing_html,
        "occurrences": occurrences,
        "deleted": deleted,
    }

def cleanup_parsed_listing_html(
    workspace: Workspace,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete raw listing HTML only when SQLite records a successful trace parse."""
    workspace.ensure()
    deleted = 0
    missing = 0
    bytes_freed = 0
    with connect(workspace) as conn:
        sql = """
            SELECT url,trace_source_file
            FROM pages
            WHERE trace_status='parsed'
              AND status IN ('saved','valid_empty')
            ORDER BY trace_parsed_at,url
        """
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        rows = conn.execute(sql, params).fetchall()
        for row in rows:
            path = find_listing_html(workspace, row["url"])
            if path is None:
                missing += 1
                continue
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
                conn.execute(
                    "UPDATE pages SET trace_html_deleted_at=? WHERE url=?",
                    (now_iso(), normalize_url(row["url"])),
                )
            deleted += 1
            bytes_freed += size
        if not dry_run:
            conn.commit()
    return {
        "eligible": len(rows),
        "deleted": deleted,
        "already_missing": missing,
        "bytes_freed": bytes_freed,
    }


def rebuild_listing_trace(
    workspace: Workspace,
    *,
    force: bool = False,
    delete_html: bool = False,
    limit: int | None = None,
    batch_size: int = 250,
    progress_callback=None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Incrementally parse listing HTML into durable SQLite trace state.

    By default only saved/valid-empty pages not already marked ``trace_status='parsed'``
    are read. Existing trace rows for other pages are preserved, so raw HTML for an
    already parsed page may be permanently removed without losing its trace data.

    ``limit`` caps the number of candidate listings examined in this invocation.
    ``batch_size`` controls how often SQLite is committed and progress is reported.
    A process interruption can therefore cause at most the current uncommitted batch
    to be reprocessed on the next run; already committed batches remain durable.

    ``force=True`` reparses every available saved listing page and replaces only that
    page's trace rows. ``delete_html=True`` removes raw listing HTML only *after* the
    batch containing its trace rows has committed successfully.
    """
    workspace.ensure()
    missing: list[str] = []
    batch_size = max(1, int(batch_size or 1))
    if limit is not None:
        limit = max(0, int(limit))

    def emit(event: dict) -> None:
        if progress_callback is not None:
            progress_callback(event)

    with connect(workspace) as conn:
        _migrate_legacy_trace_markers(conn, workspace)
        if force:
            where = "status IN ('saved','valid_empty')"
        else:
            where = "status IN ('saved','valid_empty') AND COALESCE(trace_status,'')<>'parsed'"

        candidate_total = int(
            conn.execute(f"SELECT COUNT(*) n FROM pages WHERE {where}").fetchone()["n"]
        )
        sql = f"""
            SELECT url,state_group,location,page_no,status,file,
                   specialty_url,specialty_name,specialty_slug,trace_status
            FROM pages
            WHERE {where}
            ORDER BY specialty_name,state_group,location,page_no,url
        """
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        page_rows = conn.execute(sql, params).fetchall()
        selected_total = len(page_rows)

        started = time.perf_counter()
        processed = 0
        parsed = 0
        occurrences_run = 0
        deleted_run = 0
        pending_deletes: list[tuple[str, Path]] = []

        emit({
            "event": "start",
            "candidate_total": candidate_total,
            "selected_total": selected_total,
            "limit": limit,
            "batch_size": batch_size,
            "force": force,
            "delete_html": delete_html,
        })

        def commit_batch() -> None:
            nonlocal deleted_run
            # First make all trace rows + parsed markers durable. Raw HTML is never
            # deleted before this commit succeeds.
            conn.commit()
            if delete_html and pending_deletes:
                for url, path in list(pending_deletes):
                    if not path.exists():
                        continue
                    path.unlink()
                    conn.execute(
                        "UPDATE pages SET trace_html_deleted_at=? WHERE url=?",
                        (now_iso(), url),
                    )
                    deleted_run += 1
                conn.commit()
                pending_deletes.clear()

            elapsed = max(time.perf_counter() - started, 1e-9)
            rate = processed / elapsed
            remaining = max(0, selected_total - processed)
            eta = (remaining / rate) if rate > 0 else None
            emit({
                "event": "batch",
                "processed": processed,
                "selected_total": selected_total,
                "candidate_total": candidate_total,
                "parsed": parsed,
                "missing": len(missing),
                "occurrences": occurrences_run,
                "deleted": deleted_run,
                "elapsed_seconds": elapsed,
                "rate_files_per_second": rate,
                "eta_seconds": eta,
            })

        for row in page_rows:
            url = normalize_url(row["url"])
            path = find_listing_html(workspace, url)
            processed += 1
            if path is None:
                missing.append(url)
            else:
                occurrences_run += _persist_listing_trace(
                    conn, page_row=row, path=path, commit=False
                )
                parsed += 1
                if delete_html:
                    pending_deletes.append((url, path))

            if processed % batch_size == 0:
                commit_batch()

        if processed % batch_size != 0:
            commit_batch()
        elif processed == 0:
            conn.commit()

        emit({
            "event": "export_start",
            "processed": processed,
            "parsed": parsed,
            "missing": len(missing),
        })
        trace_df, summary_df = _export_trace_outputs(conn, workspace)

        elapsed = max(time.perf_counter() - started, 1e-9)
        emit({
            "event": "done",
            "processed": processed,
            "selected_total": selected_total,
            "candidate_total": candidate_total,
            "parsed": parsed,
            "missing": len(missing),
            "occurrences": occurrences_run,
            "deleted": deleted_run,
            "elapsed_seconds": elapsed,
            "rate_files_per_second": (processed / elapsed) if processed else 0.0,
        })

    return trace_df, summary_df, missing


def _first_text(series: pd.Series) -> str:
    for value in series:
        if pd.notna(value) and str(value).strip():
            return str(value)
    return ""
