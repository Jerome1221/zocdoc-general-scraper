from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .schema import PROFILE_QUEUE_COLUMNS
from .workspace import Workspace


def export_page_outputs(conn: sqlite3.Connection, workspace: Workspace) -> None:
    workspace.ensure()

    page_fields = [
        "specialty_name",
        "specialty_slug",
        "specialty_url",
        "state_group",
        "location",
        "page_no",
        "status",
        "source",
        "advertised_total",
        "doctor_links_found",
        "validation_status",
        "validation_reason",
        "zero_link_retry_count",
        "last_rejected_file",
        "provider_set_hash",
        "last_provider_set_changed_at",
        "trace_status",
        "trace_parsed_at",
        "trace_occurrence_count",
        "trace_source_file",
        "trace_html_deleted_at",
        "url",
        "base_location_url",
        "file",
        "saved_at",
        "error",
    ]
    pages = conn.execute(
        """
        SELECT specialty_name,specialty_slug,specialty_url,
               state_group, location, page_no, status, source,
               advertised_total, doctor_links_found, validation_status,validation_reason,
               zero_link_retry_count,last_rejected_file,provider_set_hash,
               last_provider_set_changed_at,trace_status,trace_parsed_at,
               trace_occurrence_count,trace_source_file,trace_html_deleted_at,url,
               base_location_url, file, saved_at, error
        FROM pages
        ORDER BY specialty_name,state_group, location, base_location_url, page_no, url
        """
    ).fetchall()
    _write_rows(workspace.output_dir / "page_queue.csv", page_fields, pages)

    doctor_fields = [
        "doctor_name",
        "doctor_url",
        "first_seen_specialty_name",
        "first_seen_specialty_url",
        "first_seen_state_group",
        "first_seen_location",
        "first_seen_location_url",
        "first_seen_page_url",
        "seen_on_pages",
        "seen_in_specialties",
    ]
    doctors = conn.execute(
        """
        SELECT d.doctor_url, d.doctor_name,
               d.first_seen_specialty_name,d.first_seen_specialty_url,
               d.first_seen_state_group, d.first_seen_location,
               d.first_seen_location_url, d.first_seen_page_url,
               COUNT(ds.page_url) AS seen_on_pages,
               COUNT(DISTINCT CASE WHEN ds.specialty_url<>'' THEN ds.specialty_url END) AS seen_in_specialties
        FROM doctors d
        LEFT JOIN doctor_sources ds ON ds.doctor_url=d.doctor_url
        GROUP BY d.doctor_url
        ORDER BY d.doctor_name, d.doctor_url
        """
    ).fetchall()
    _write_rows(workspace.output_dir / "doctor_links.csv", doctor_fields, doctors)

    summary_fields = [
        "specialty_name",
        "specialty_slug",
        "specialty_url",
        "state_group",
        "location",
        "base_location_url",
        "advertised_total",
        "pages_saved",
        "pages_valid_empty",
        "pages_pending",
        "pages_retry_pending",
        "pages_review",
        "pages_blocked",
        "pages_timeout",
        "unique_profile_links",
    ]
    summaries = conn.execute(
        """
        SELECT p.specialty_name,p.specialty_slug,p.specialty_url,
               p.state_group, p.location, p.base_location_url,
               MAX(p.advertised_total) AS advertised_total,
               SUM(CASE WHEN p.status='saved' THEN 1 ELSE 0 END) AS pages_saved,
               SUM(CASE WHEN p.status='valid_empty' THEN 1 ELSE 0 END) AS pages_valid_empty,
               SUM(CASE WHEN p.status='pending' THEN 1 ELSE 0 END) AS pages_pending,
               SUM(CASE WHEN p.status='retry_pending' THEN 1 ELSE 0 END) AS pages_retry_pending,
               SUM(CASE WHEN p.status='review' THEN 1 ELSE 0 END) AS pages_review,
               SUM(CASE WHEN p.status='blocked' THEN 1 ELSE 0 END) AS pages_blocked,
               SUM(CASE WHEN p.status='timeout' THEN 1 ELSE 0 END) AS pages_timeout,
               COUNT(DISTINCT ds.doctor_url) AS unique_profile_links
        FROM pages p
        LEFT JOIN doctor_sources ds ON ds.location_url=p.base_location_url
        GROUP BY p.specialty_url,p.base_location_url
        ORDER BY p.specialty_name,p.state_group, p.location
        """
    ).fetchall()
    _write_rows(workspace.output_dir / "location_summary.csv", summary_fields, summaries)


def export_profile_queue(conn: sqlite3.Connection, workspace: Workspace) -> None:
    rows = conn.execute(
        """
        SELECT doctor_name,status,doctor_url,file,bytes,
               discovered_at,opened_at,saved_at,claimed_by,claimed_at,attempt_count,
               last_error,last_seen_at,error
        FROM doctor_profiles
        ORDER BY doctor_name,doctor_url
        """
    ).fetchall()
    _write_rows(workspace.output_dir / "profile_queue.csv", PROFILE_QUEUE_COLUMNS, rows)


def _write_rows(path: Path, fields: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: (row[key] if row[key] is not None else "") for key in fields})
