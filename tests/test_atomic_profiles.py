from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zocdoc_ortho.db import connect
from zocdoc_ortho.queue import _claim_pending_profile, recover_stale_profile_claims
from zocdoc_ortho.workspace import Workspace


def _seed_profiles(workspace: Workspace, count: int = 8) -> None:
    with connect(workspace) as conn:
        for index in range(count):
            conn.execute(
                """
                INSERT INTO doctor_profiles(doctor_url,doctor_name,status,discovered_at)
                VALUES (?,?, 'pending', ?)
                """,
                (
                    f"https://www.zocdoc.com/doctor/test-doctor-{index}",
                    f"Doctor {index:02d}",
                    datetime.now(UTC).isoformat(),
                ),
            )
        conn.commit()


def test_atomic_profile_claims_are_unique_across_runners(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_profiles(workspace, 8)

    runner_ids = [f"runner-{index:02d}" for index in range(1, 9)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda runner: _claim_pending_profile(workspace, runner), runner_ids))

    urls = [claim["doctor_url"] for claim in claims if claim is not None]
    assert len(urls) == 8
    assert len(set(urls)) == 8
    with connect(workspace) as conn:
        rows = conn.execute(
            "SELECT status,claimed_by,claimed_at,attempt_count FROM doctor_profiles"
        ).fetchall()
    assert all(row["status"] == "opening" for row in rows)
    assert all(row["claimed_by"] for row in rows)
    assert all(row["claimed_at"] for row in rows)
    assert all(row["attempt_count"] == 1 for row in rows)


def test_stale_profile_claim_recovery(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_profiles(workspace, 1)
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE doctor_profiles
            SET status='opening',claimed_by='dead-runner',claimed_at=?,opened_at=?,attempt_count=1
            """,
            (old, old),
        )
        conn.commit()

    assert recover_stale_profile_claims(workspace, stale_claim_minutes=10) == 1
    with connect(workspace) as conn:
        row = conn.execute("SELECT * FROM doctor_profiles").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None
    assert "Recovered stale profile claim" in row["last_error"]
