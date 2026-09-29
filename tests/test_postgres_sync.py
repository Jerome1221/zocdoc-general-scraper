from __future__ import annotations

import sys
from types import SimpleNamespace

from zocdoc_ortho.db import connect
from zocdoc_ortho.postgres_sync import publish_to_postgres
from zocdoc_ortho.utils import now_iso
from zocdoc_ortho.workspace import Workspace


class FakeCursor:
    def __init__(self):
        self.executions = []
        self.batches = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def executemany(self, sql, params):
        self.batches.append((sql, list(params)))


class FakePostgresConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True


def test_postgres_projection_publishes_canonical_rows_transactionally(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    stamp = now_iso()
    doctor_url = "https://www.zocdoc.com/doctor/alpha-doctor-md-1"
    listing_url = "https://www.zocdoc.com/cardiologists/test-10001pm"
    with connect(workspace) as conn:
        cursor = conn.execute(
            """
            INSERT INTO daily_sync_runs(
                run_uuid,started_at,completed_at,status,scope,locations_selected,
                locations_completed,providers_seen,providers_added
            ) VALUES ('run-test',?,?,'complete','limited',1,1,1,1)
            """,
            (stamp, stamp),
        )
        run_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO daily_sync_locations(
                run_id,specialty,specialty_slug,location,location_url,status,
                expected_pages,pages_fetched,providers_seen,providers_added,completed_at
            ) VALUES (?,?,?,?,?,'complete',1,1,1,1,?)
            """,
            (run_id, "Cardiologists", "cardiologists", "Test, NY", listing_url, stamp),
        )
        conn.execute(
            """
            INSERT INTO canonical_provider_records(
                doctor_url,doctor_name,is_active,first_seen_at,last_seen_at,
                profile_data_json,profile_diagnostics_json,last_seen_run_id
            ) VALUES (?, 'Alpha Doctor',1,?,?,'[]','{}',?)
            """,
            (doctor_url, stamp, stamp, run_id),
        )
        conn.execute(
            """
            INSERT INTO provider_location_memberships(
                specialty,specialty_slug,location,location_url,doctor_url,doctor_name,
                is_active,first_seen_at,last_seen_at,last_seen_run_id,listing_data_json
            ) VALUES ('Cardiologists','cardiologists','Test, NY',?,?,'Alpha Doctor',1,?,?,?,'[]')
            """,
            (listing_url, doctor_url, stamp, stamp, run_id),
        )
        conn.execute(
            """
            INSERT INTO provider_change_events(
                run_id,specialty_slug,location_url,doctor_url,change_type,created_at
            ) VALUES (?,'cardiologists',?,?,'added',?)
            """,
            (run_id, listing_url, doctor_url, stamp),
        )
        conn.commit()

    fake_connection = FakePostgresConnection()
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda database_url: fake_connection),
    )

    result = publish_to_postgres(workspace, "postgresql://test", run_id=run_id)

    assert result == {
        "run_id": run_id,
        "run_uuid": "run-test",
        "providers": 1,
        "memberships": 1,
        "events": 1,
    }
    assert fake_connection.committed is True
    assert "CREATE TABLE IF NOT EXISTS zocdoc_sync_runs" in fake_connection.cursor_instance.executions[0][0]
    assert [len(batch) for _, batch in fake_connection.cursor_instance.batches] == [1, 1, 1, 1]
