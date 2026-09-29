from __future__ import annotations

from typing import Any

from .db import connect
from .workspace import Workspace

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS zocdoc_sync_runs (
    run_uuid TEXT PRIMARY KEY,
    local_run_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    scope TEXT NOT NULL,
    locations_selected INTEGER NOT NULL,
    locations_completed INTEGER NOT NULL,
    locations_failed INTEGER NOT NULL,
    pages_fetched INTEGER NOT NULL,
    providers_seen INTEGER NOT NULL,
    providers_added INTEGER NOT NULL,
    providers_reactivated INTEGER NOT NULL,
    removals_pending INTEGER NOT NULL,
    providers_removed INTEGER NOT NULL,
    profiles_queued INTEGER NOT NULL,
    profiles_saved INTEGER NOT NULL,
    profiles_failed INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS zocdoc_sync_locations (
    run_uuid TEXT NOT NULL REFERENCES zocdoc_sync_runs(run_uuid) ON DELETE CASCADE,
    specialty TEXT NOT NULL DEFAULT '',
    specialty_slug TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    location_url TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_pages INTEGER NOT NULL,
    pages_fetched INTEGER NOT NULL,
    advertised_total INTEGER,
    providers_seen INTEGER NOT NULL,
    provider_set_hash TEXT NOT NULL DEFAULT '',
    providers_added INTEGER NOT NULL,
    providers_reactivated INTEGER NOT NULL,
    removals_pending INTEGER NOT NULL,
    providers_removed INTEGER NOT NULL,
    completed_at TIMESTAMPTZ,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_uuid, specialty_slug, location_url)
);

CREATE TABLE IF NOT EXISTS zocdoc_providers (
    doctor_url TEXT PRIMARY KEY,
    doctor_name TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    inactive_at TIMESTAMPTZ,
    profile_sha256 TEXT NOT NULL DEFAULT '',
    profile_data JSONB NOT NULL DEFAULT '[]'::jsonb,
    profile_diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    profile_updated_at TIMESTAMPTZ,
    source_run_uuid TEXT
);

CREATE TABLE IF NOT EXISTS zocdoc_provider_locations (
    specialty TEXT NOT NULL DEFAULT '',
    specialty_slug TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    location_url TEXT NOT NULL,
    doctor_url TEXT NOT NULL REFERENCES zocdoc_providers(doctor_url),
    doctor_name TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    inactive_at TIMESTAMPTZ,
    missing_streak INTEGER NOT NULL DEFAULT 0,
    listing_data JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_run_uuid TEXT,
    PRIMARY KEY (specialty_slug, location_url, doctor_url)
);

CREATE TABLE IF NOT EXISTS zocdoc_provider_changes (
    event_key TEXT PRIMARY KEY,
    run_uuid TEXT NOT NULL REFERENCES zocdoc_sync_runs(run_uuid) ON DELETE CASCADE,
    specialty_slug TEXT NOT NULL DEFAULT '',
    location_url TEXT NOT NULL DEFAULT '',
    doctor_url TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_zocdoc_providers_active
    ON zocdoc_providers(is_active);
CREATE INDEX IF NOT EXISTS idx_zocdoc_provider_locations_active
    ON zocdoc_provider_locations(is_active, specialty_slug, location_url);
CREATE INDEX IF NOT EXISTS idx_zocdoc_changes_run
    ON zocdoc_provider_changes(run_uuid, change_type);
"""


def _load_payload(workspace: Workspace, run_id: int | None) -> dict[str, Any]:
    with connect(workspace) as conn:
        if run_id is None:
            run = conn.execute("SELECT * FROM daily_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        else:
            run = conn.execute("SELECT * FROM daily_sync_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise RuntimeError("No daily sync run is available to publish.")
        selected_run_id = int(run["id"])
        locations = conn.execute(
            "SELECT * FROM daily_sync_locations WHERE run_id=? ORDER BY specialty_slug,location_url",
            (selected_run_id,),
        ).fetchall()
        providers = conn.execute(
            "SELECT * FROM canonical_provider_records ORDER BY doctor_url"
        ).fetchall()
        memberships = conn.execute(
            "SELECT * FROM provider_location_memberships ORDER BY specialty_slug,location_url,doctor_url"
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM provider_change_events WHERE run_id=? ORDER BY id",
            (selected_run_id,),
        ).fetchall()
    return {
        "run": dict(run),
        "locations": [dict(row) for row in locations],
        "providers": [dict(row) for row in providers],
        "memberships": [dict(row) for row in memberships],
        "events": [dict(row) for row in events],
    }


def publish_to_postgres(
    workspace: Workspace,
    database_url: str,
    *,
    run_id: int | None = None,
) -> dict[str, int | str]:
    """Transactionally project the local canonical database into PostgreSQL."""
    if not database_url.strip():
        raise ValueError("A PostgreSQL database URL is required.")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support is not installed. Run: pip install -e \".[postgres]\""
        ) from exc

    payload = _load_payload(workspace, run_id)
    run = payload["run"]
    run_uuid = run["run_uuid"]
    with psycopg.connect(database_url) as pg:
        with pg.cursor() as cursor:
            cursor.execute(POSTGRES_SCHEMA)
            cursor.execute(
                """
                INSERT INTO zocdoc_sync_runs(
                    run_uuid,local_run_id,started_at,completed_at,status,scope,
                    locations_selected,locations_completed,locations_failed,pages_fetched,
                    providers_seen,providers_added,providers_reactivated,removals_pending,
                    providers_removed,profiles_queued,profiles_saved,profiles_failed,error
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT(run_uuid) DO UPDATE SET
                    completed_at=excluded.completed_at,status=excluded.status,scope=excluded.scope,
                    locations_selected=excluded.locations_selected,
                    locations_completed=excluded.locations_completed,
                    locations_failed=excluded.locations_failed,pages_fetched=excluded.pages_fetched,
                    providers_seen=excluded.providers_seen,providers_added=excluded.providers_added,
                    providers_reactivated=excluded.providers_reactivated,
                    removals_pending=excluded.removals_pending,
                    providers_removed=excluded.providers_removed,
                    profiles_queued=excluded.profiles_queued,profiles_saved=excluded.profiles_saved,
                    profiles_failed=excluded.profiles_failed,error=excluded.error
                """,
                (
                    run_uuid,
                    run["id"],
                    run["started_at"],
                    run["completed_at"],
                    run["status"],
                    run["scope"],
                    run["locations_selected"],
                    run["locations_completed"],
                    run["locations_failed"],
                    run["pages_fetched"],
                    run["providers_seen"],
                    run["providers_added"],
                    run["providers_reactivated"],
                    run["removals_pending"],
                    run["providers_removed"],
                    run["profiles_queued"],
                    run["profiles_saved"],
                    run["profiles_failed"],
                    run["error"] or "",
                ),
            )
            cursor.executemany(
                """
                INSERT INTO zocdoc_providers(
                    doctor_url,doctor_name,is_active,first_seen_at,last_seen_at,inactive_at,
                    profile_sha256,profile_data,profile_diagnostics,profile_updated_at,source_run_uuid
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                ON CONFLICT(doctor_url) DO UPDATE SET
                    doctor_name=excluded.doctor_name,is_active=excluded.is_active,
                    first_seen_at=LEAST(zocdoc_providers.first_seen_at,excluded.first_seen_at),
                    last_seen_at=excluded.last_seen_at,inactive_at=excluded.inactive_at,
                    profile_sha256=excluded.profile_sha256,profile_data=excluded.profile_data,
                    profile_diagnostics=excluded.profile_diagnostics,
                    profile_updated_at=excluded.profile_updated_at,source_run_uuid=excluded.source_run_uuid
                """,
                [
                    (
                        row["doctor_url"],
                        row["doctor_name"] or "",
                        bool(row["is_active"]),
                        row["first_seen_at"],
                        row["last_seen_at"],
                        row["inactive_at"],
                        row["profile_sha256"] or "",
                        row["profile_data_json"] or "[]",
                        row["profile_diagnostics_json"] or "{}",
                        row["profile_updated_at"],
                        run_uuid,
                    )
                    for row in payload["providers"]
                ],
            )
            cursor.executemany(
                """
                INSERT INTO zocdoc_provider_locations(
                    specialty,specialty_slug,location,location_url,doctor_url,doctor_name,
                    is_active,first_seen_at,last_seen_at,inactive_at,missing_streak,
                    listing_data,source_run_uuid
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT(specialty_slug,location_url,doctor_url) DO UPDATE SET
                    specialty=excluded.specialty,location=excluded.location,
                    doctor_name=excluded.doctor_name,is_active=excluded.is_active,
                    first_seen_at=LEAST(zocdoc_provider_locations.first_seen_at,excluded.first_seen_at),
                    last_seen_at=excluded.last_seen_at,inactive_at=excluded.inactive_at,
                    missing_streak=excluded.missing_streak,listing_data=excluded.listing_data,
                    source_run_uuid=excluded.source_run_uuid
                """,
                [
                    (
                        row["specialty"] or "",
                        row["specialty_slug"] or "",
                        row["location"] or "",
                        row["location_url"],
                        row["doctor_url"],
                        row["doctor_name"] or "",
                        bool(row["is_active"]),
                        row["first_seen_at"],
                        row["last_seen_at"],
                        row["inactive_at"],
                        row["missing_streak"],
                        row["listing_data_json"] or "[]",
                        run_uuid,
                    )
                    for row in payload["memberships"]
                ],
            )
            cursor.executemany(
                """
                INSERT INTO zocdoc_sync_locations(
                    run_uuid,specialty,specialty_slug,location,location_url,status,
                    expected_pages,pages_fetched,advertised_total,providers_seen,
                    provider_set_hash,providers_added,providers_reactivated,
                    removals_pending,providers_removed,completed_at,error
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(run_uuid,specialty_slug,location_url) DO UPDATE SET
                    status=excluded.status,expected_pages=excluded.expected_pages,
                    pages_fetched=excluded.pages_fetched,advertised_total=excluded.advertised_total,
                    providers_seen=excluded.providers_seen,provider_set_hash=excluded.provider_set_hash,
                    providers_added=excluded.providers_added,
                    providers_reactivated=excluded.providers_reactivated,
                    removals_pending=excluded.removals_pending,
                    providers_removed=excluded.providers_removed,
                    completed_at=excluded.completed_at,error=excluded.error
                """,
                [
                    (
                        run_uuid,
                        row["specialty"] or "",
                        row["specialty_slug"] or "",
                        row["location"] or "",
                        row["location_url"],
                        row["status"],
                        row["expected_pages"],
                        row["pages_fetched"],
                        row["advertised_total"],
                        row["providers_seen"],
                        row["provider_set_hash"] or "",
                        row["providers_added"],
                        row["providers_reactivated"],
                        row["removals_pending"],
                        row["providers_removed"],
                        row["completed_at"],
                        row["error"] or "",
                    )
                    for row in payload["locations"]
                ],
            )
            cursor.executemany(
                """
                INSERT INTO zocdoc_provider_changes(
                    event_key,run_uuid,specialty_slug,location_url,doctor_url,
                    change_type,old_value,new_value,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(event_key) DO NOTHING
                """,
                [
                    (
                        f"{run_uuid}:{row['id']}",
                        run_uuid,
                        row["specialty_slug"] or "",
                        row["location_url"] or "",
                        row["doctor_url"],
                        row["change_type"],
                        row["old_value"] or "",
                        row["new_value"] or "",
                        row["created_at"],
                    )
                    for row in payload["events"]
                ],
            )
        pg.commit()
    return {
        "run_id": int(run["id"]),
        "run_uuid": run_uuid,
        "providers": len(payload["providers"]),
        "memberships": len(payload["memberships"]),
        "events": len(payload["events"]),
    }
