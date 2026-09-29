from __future__ import annotations

import sqlite3

from .workspace import Workspace


def connect(workspace: Workspace) -> sqlite3.Connection:
    workspace.ensure()
    conn = sqlite3.connect(workspace.db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY,
            base_location_url TEXT NOT NULL,
            state_group TEXT DEFAULT '',
            location TEXT DEFAULT '',
            page_no INTEGER NOT NULL DEFAULT 1,
            source TEXT DEFAULT 'seed',
            status TEXT NOT NULL DEFAULT 'pending',
            title TEXT DEFAULT '',
            file TEXT DEFAULT '',
            bytes INTEGER DEFAULT 0,
            advertised_total INTEGER,
            doctor_links_found INTEGER DEFAULT 0,
            validation_status TEXT DEFAULT '',
            validation_reason TEXT DEFAULT '',
            zero_link_retry_count INTEGER NOT NULL DEFAULT 0,
            last_rejected_file TEXT DEFAULT '',
            provider_set_hash TEXT DEFAULT '',
            last_provider_set_changed_at TEXT,
            discovered_at TEXT NOT NULL,
            opened_at TEXT,
            saved_at TEXT,
            error TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS doctors (
            doctor_url TEXT PRIMARY KEY,
            doctor_name TEXT DEFAULT '',
            first_seen_page_url TEXT NOT NULL,
            first_seen_location_url TEXT NOT NULL,
            first_seen_state_group TEXT DEFAULT '',
            first_seen_location TEXT DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS doctor_sources (
            doctor_url TEXT NOT NULL,
            page_url TEXT NOT NULL,
            location_url TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (doctor_url, page_url)
        );

        CREATE TABLE IF NOT EXISTS doctor_profiles (
            doctor_url TEXT PRIMARY KEY,
            doctor_name TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            file TEXT DEFAULT '',
            bytes INTEGER DEFAULT 0,
            discovered_at TEXT NOT NULL,
            opened_at TEXT,
            saved_at TEXT,
            error TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS specialties (
            specialty_url TEXT PRIMARY KEY,
            specialty_name TEXT NOT NULL,
            specialty_slug TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'provider_specialty',
            is_active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS specialty_pages (
            specialty_url TEXT PRIMARY KEY,
            specialty_name TEXT NOT NULL,
            specialty_slug TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            specialty_id TEXT DEFAULT '',
            procedure_id TEXT DEFAULT '',
            search_query TEXT DEFAULT '',
            location_count INTEGER DEFAULT 0,
            file TEXT DEFAULT '',
            bytes INTEGER DEFAULT 0,
            discovered_at TEXT NOT NULL,
            opened_at TEXT,
            saved_at TEXT,
            error TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS specialty_targets (
            listing_url TEXT PRIMARY KEY,
            specialty_url TEXT NOT NULL,
            specialty_name TEXT NOT NULL,
            specialty_slug TEXT NOT NULL,
            state_group TEXT DEFAULT '',
            location TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_specialties_active
            ON specialties(is_active);
        CREATE INDEX IF NOT EXISTS idx_specialty_pages_status
            ON specialty_pages(status);
        CREATE INDEX IF NOT EXISTS idx_specialty_targets_specialty
            ON specialty_targets(specialty_url,is_active);

        CREATE TABLE IF NOT EXISTS listing_provider_trace (
            listing_url TEXT,
            listing_state_group TEXT,
            listing_location TEXT,
            listing_page_no INTEGER,
            listing_file TEXT,
            listing_specialty_url TEXT,
            listing_specialty_name TEXT,
            listing_specialty_slug TEXT,
            card_index INTEGER,
            doctor_url TEXT,
            doctor_name TEXT,
            specialty_name TEXT,
            provider_id TEXT,
            monolith_id TEXT,
            npi TEXT,
            provider_location_key TEXT,
            average_rating TEXT,
            review_count TEXT,
            address_line_1 TEXT,
            city TEXT,
            state TEXT,
            postal_code TEXT,
            listing_offers_video_visits INTEGER,
            listing_accepts_new_patients INTEGER,
            structured_accepts_new_patients INTEGER,
            structured_offers_telemedicine INTEGER,
            structured_has_virtual_locations INTEGER,
            structured_can_have_appointments INTEGER,
            structured_has_new_patient_availability INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_listing_provider_trace_listing
            ON listing_provider_trace(listing_url);
        CREATE INDEX IF NOT EXISTS idx_listing_provider_trace_doctor
            ON listing_provider_trace(doctor_url);

        CREATE TABLE IF NOT EXISTS location_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            specialty TEXT DEFAULT '',
            specialty_slug TEXT DEFAULT '',
            location TEXT DEFAULT '',
            location_url TEXT NOT NULL,
            verified_provider_count INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS refresh_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            specialty TEXT DEFAULT '',
            specialty_slug TEXT DEFAULT '',
            location TEXT DEFAULT '',
            location_url TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshot_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            mode TEXT NOT NULL,
            locations_selected INTEGER NOT NULL DEFAULT 0,
            locations_processed INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_location_snapshots_target
            ON location_snapshots(specialty_slug, location_url, checked_at);
        CREATE INDEX IF NOT EXISTS idx_refresh_queue_status
            ON refresh_queue(status, reason);
        CREATE INDEX IF NOT EXISTS idx_refresh_queue_target
            ON refresh_queue(specialty_slug, location_url, status);
        CREATE INDEX IF NOT EXISTS idx_snapshot_runs_started
            ON snapshot_runs(started_at);

        CREATE TABLE IF NOT EXISTS daily_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_uuid TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            scope TEXT NOT NULL DEFAULT 'all',
            locations_selected INTEGER NOT NULL DEFAULT 0,
            locations_completed INTEGER NOT NULL DEFAULT 0,
            locations_failed INTEGER NOT NULL DEFAULT 0,
            pages_fetched INTEGER NOT NULL DEFAULT 0,
            providers_seen INTEGER NOT NULL DEFAULT 0,
            providers_added INTEGER NOT NULL DEFAULT 0,
            providers_reactivated INTEGER NOT NULL DEFAULT 0,
            removals_pending INTEGER NOT NULL DEFAULT 0,
            providers_removed INTEGER NOT NULL DEFAULT 0,
            profiles_queued INTEGER NOT NULL DEFAULT 0,
            profiles_saved INTEGER NOT NULL DEFAULT 0,
            profiles_failed INTEGER NOT NULL DEFAULT 0,
            database_published INTEGER NOT NULL DEFAULT 0,
            error TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS daily_sync_locations (
            run_id INTEGER NOT NULL,
            specialty TEXT DEFAULT '',
            specialty_slug TEXT NOT NULL DEFAULT '',
            location TEXT DEFAULT '',
            location_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            expected_pages INTEGER NOT NULL DEFAULT 0,
            pages_fetched INTEGER NOT NULL DEFAULT 0,
            advertised_total INTEGER,
            providers_seen INTEGER NOT NULL DEFAULT 0,
            provider_set_hash TEXT DEFAULT '',
            providers_added INTEGER NOT NULL DEFAULT 0,
            providers_reactivated INTEGER NOT NULL DEFAULT 0,
            removals_pending INTEGER NOT NULL DEFAULT 0,
            providers_removed INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            error TEXT DEFAULT '',
            PRIMARY KEY (run_id, specialty_slug, location_url),
            FOREIGN KEY (run_id) REFERENCES daily_sync_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS daily_provider_snapshots (
            run_id INTEGER NOT NULL,
            specialty_slug TEXT NOT NULL DEFAULT '',
            location_url TEXT NOT NULL,
            doctor_url TEXT NOT NULL,
            doctor_name TEXT DEFAULT '',
            listing_url TEXT NOT NULL,
            listing_page_no INTEGER NOT NULL DEFAULT 1,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (run_id, specialty_slug, location_url, doctor_url, listing_url),
            FOREIGN KEY (run_id) REFERENCES daily_sync_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS provider_location_memberships (
            specialty TEXT DEFAULT '',
            specialty_slug TEXT NOT NULL DEFAULT '',
            location TEXT DEFAULT '',
            location_url TEXT NOT NULL,
            doctor_url TEXT NOT NULL,
            doctor_name TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            inactive_at TEXT,
            missing_streak INTEGER NOT NULL DEFAULT 0,
            last_seen_run_id INTEGER,
            listing_data_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (specialty_slug, location_url, doctor_url)
        );

        CREATE TABLE IF NOT EXISTS canonical_provider_records (
            doctor_url TEXT PRIMARY KEY,
            doctor_name TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            inactive_at TEXT,
            profile_file TEXT DEFAULT '',
            profile_sha256 TEXT DEFAULT '',
            profile_data_json TEXT NOT NULL DEFAULT '[]',
            profile_diagnostics_json TEXT NOT NULL DEFAULT '{}',
            profile_updated_at TEXT,
            last_seen_run_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS provider_change_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            specialty_slug TEXT DEFAULT '',
            location_url TEXT DEFAULT '',
            doctor_url TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_value TEXT DEFAULT '',
            new_value TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES daily_sync_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS daily_profile_queue (
            run_id INTEGER NOT NULL,
            doctor_url TEXT NOT NULL,
            doctor_name TEXT DEFAULT '',
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error TEXT DEFAULT '',
            PRIMARY KEY (run_id, doctor_url),
            FOREIGN KEY (run_id) REFERENCES daily_sync_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS daily_sync_lock (
            lock_name TEXT PRIMARY KEY,
            lock_token TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            owner_pid INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_daily_sync_runs_started
            ON daily_sync_runs(started_at);
        CREATE INDEX IF NOT EXISTS idx_daily_sync_locations_status
            ON daily_sync_locations(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_daily_provider_snapshots_target
            ON daily_provider_snapshots(specialty_slug, location_url, run_id);
        CREATE INDEX IF NOT EXISTS idx_provider_memberships_active
            ON provider_location_memberships(is_active, specialty_slug, location_url);
        CREATE INDEX IF NOT EXISTS idx_provider_memberships_doctor
            ON provider_location_memberships(doctor_url, is_active);
        CREATE INDEX IF NOT EXISTS idx_canonical_providers_active
            ON canonical_provider_records(is_active);
        CREATE INDEX IF NOT EXISTS idx_provider_change_events_run
            ON provider_change_events(run_id, change_type);
        CREATE INDEX IF NOT EXISTS idx_daily_profile_queue_status
            ON daily_profile_queue(run_id, status);
        """
    )

    # Additive compatibility with earlier collector.db files.
    _ensure_column(conn, "pages", "opened_at", "TEXT")
    _ensure_column(conn, "pages", "saved_at", "TEXT")
    _ensure_column(conn, "pages", "error", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "specialty_url", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "specialty_name", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "specialty_slug", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "provider_set_hash", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "last_provider_set_changed_at", "TEXT")
    _ensure_column(conn, "pages", "validation_status", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "validation_reason", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "zero_link_retry_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "pages", "last_rejected_file", "TEXT DEFAULT ''")
    _ensure_column(conn, "doctors", "first_seen_specialty_url", "TEXT DEFAULT ''")
    _ensure_column(conn, "doctors", "first_seen_specialty_name", "TEXT DEFAULT ''")
    _ensure_column(conn, "doctor_sources", "specialty_url", "TEXT DEFAULT ''")
    _ensure_column(conn, "doctor_sources", "specialty_name", "TEXT DEFAULT ''")

    _ensure_column(conn, "pages", "trace_status", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "trace_parsed_at", "TEXT")
    _ensure_column(conn, "pages", "trace_occurrence_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "pages", "trace_source_file", "TEXT DEFAULT ''")
    _ensure_column(conn, "pages", "trace_html_deleted_at", "TEXT")
    _ensure_column(conn, "doctor_profiles", "claimed_by", "TEXT")
    _ensure_column(conn, "doctor_profiles", "claimed_at", "TEXT")
    _ensure_column(conn, "doctor_profiles", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "doctor_profiles", "last_error", "TEXT DEFAULT ''")
    _ensure_column(conn, "doctor_profiles", "last_seen_at", "TEXT")
    conn.commit()


def recreate_trace_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS listing_provider_trace")
    conn.execute(
        """
        CREATE TABLE listing_provider_trace (
            listing_url TEXT,
            listing_state_group TEXT,
            listing_location TEXT,
            listing_page_no INTEGER,
            listing_file TEXT,
            listing_specialty_url TEXT,
            listing_specialty_name TEXT,
            listing_specialty_slug TEXT,
            card_index INTEGER,
            doctor_url TEXT,
            doctor_name TEXT,
            specialty_name TEXT,
            provider_id TEXT,
            monolith_id TEXT,
            npi TEXT,
            provider_location_key TEXT,
            average_rating TEXT,
            review_count TEXT,
            address_line_1 TEXT,
            city TEXT,
            state TEXT,
            postal_code TEXT,
            listing_offers_video_visits INTEGER,
            listing_accepts_new_patients INTEGER,
            structured_accepts_new_patients INTEGER,
            structured_offers_telemedicine INTEGER,
            structured_has_virtual_locations INTEGER,
            structured_can_have_appointments INTEGER,
            structured_has_new_patient_availability INTEGER
        )
        """
    )
    conn.commit()


def trace_table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_provider_trace'"
        ).fetchone()
        is not None
    )
