from __future__ import annotations

from pathlib import Path

from zocdoc_ortho.db import connect
from zocdoc_ortho.queue import _claim_pending_profile
from zocdoc_ortho.runners import _profile_progress_snapshot
from zocdoc_ortho.specialty_runtime import append_specialty_runtime, specialty_runtime_report
from zocdoc_ortho.utils import now_iso
from zocdoc_ortho.workspace import Workspace


def _seed_specialty(workspace: Workspace) -> None:
    stamp = now_iso()
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO specialties (
                specialty_url,specialty_name,specialty_slug,entity_type,
                is_active,first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,1,?,?)
            """,
            (
                "https://www.zocdoc.com/dentists",
                "Dentists",
                "dentists",
                "provider_specialty",
                stamp,
                stamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO specialty_targets (
                listing_url,specialty_url,specialty_name,specialty_slug,
                state_group,location,is_active,first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,?,?,1,?,?)
            """,
            (
                "https://www.zocdoc.com/dentists/test-city-1pm",
                "https://www.zocdoc.com/dentists",
                "Dentists",
                "dentists",
                "Test State",
                "Test City",
                stamp,
                stamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO pages (
                url,base_location_url,page_no,source,status,discovered_at,
                specialty_url,specialty_name,specialty_slug
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                "https://www.zocdoc.com/dentists/test-city-1pm",
                "https://www.zocdoc.com/dentists/test-city-1pm",
                1,
                "seed",
                "saved",
                stamp,
                "https://www.zocdoc.com/dentists",
                "Dentists",
                "dentists",
            ),
        )
        doctor = "https://www.zocdoc.com/doctor/test-dentist-123"
        conn.execute(
            """
            INSERT INTO doctors (
                doctor_url,doctor_name,first_seen_page_url,first_seen_location_url,
                first_seen_specialty_url,first_seen_specialty_name,
                first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                doctor,
                "Test Dentist",
                "https://www.zocdoc.com/dentists/test-city-1pm",
                "https://www.zocdoc.com/dentists/test-city-1pm",
                "https://www.zocdoc.com/dentists",
                "Dentists",
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO doctor_profiles(doctor_url,doctor_name,status,discovered_at) VALUES (?,?,?,?)",
            (doctor, "Test Dentist", "pending", stamp),
        )
        conn.execute(
            """
            INSERT INTO listing_provider_trace (
                listing_url,listing_specialty_url,listing_specialty_name,
                listing_specialty_slug,doctor_url,doctor_name
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                "https://www.zocdoc.com/dentists/test-city-1pm",
                "https://www.zocdoc.com/dentists",
                "Dentists",
                "dentists",
                doctor,
                "Test Dentist",
            ),
        )
        conn.commit()


def test_specialty_runtime_report_projects_partial_run(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_specialty(workspace)
    specialty = {
        "specialty_name": "Dentists",
        "specialty_slug": "dentists",
        "specialty_url": "https://www.zocdoc.com/dentists",
    }
    append_specialty_runtime(
        workspace,
        specialty=specialty,
        step="base_listings",
        elapsed_seconds=30,
        processed=1,
        phase="seed",
    )
    report = specialty_runtime_report(workspace, "dentists")
    assert report["targets"] == 1
    assert report["providers"] == 1
    assert report["first_seen_providers"] == 1
    assert report["previously_known_providers"] == 0
    assert report["all_profile_counts"] == {"pending": 1}
    assert report["listing_complete"] is True
    assert report["profiles_complete"] is False
    base = next(row for row in report["steps"] if row["step"] == "base_listings")
    assert base["rate_per_minute"] == 2.0
    assert base["projected_full_seconds"] == 30.0


def test_profile_specialty_filter_uses_listing_membership_not_first_seen_specialty(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_specialty(workspace)
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE doctors
            SET first_seen_specialty_name='Cardiologists',
                first_seen_specialty_url='https://www.zocdoc.com/cardiologists'
            """
        )
        conn.commit()

    claimed = _claim_pending_profile(workspace, "runner-01", ("dentists",))
    assert claimed is not None
    assert claimed["doctor_url"].endswith("test-dentist-123")

    with connect(workspace) as conn:
        conn.execute(
            "UPDATE doctor_profiles SET status='pending',claimed_by=NULL,claimed_at=NULL WHERE doctor_url=?",
            (claimed["doctor_url"],),
        )
        conn.commit()
    assert _claim_pending_profile(workspace, "runner-01", ("cardiologists",)) is None


def test_profile_progress_uses_listing_membership_not_first_seen_specialty(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_specialty(workspace)
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE doctors
            SET first_seen_specialty_name='Cardiologists',
                first_seen_specialty_url='https://www.zocdoc.com/cardiologists'
            """
        )
        conn.commit()

    assert _profile_progress_snapshot(workspace, specialties=("dentists",)) == {
        "pending": 1
    }
    assert _profile_progress_snapshot(workspace, specialties=("cardiologists",)) == {}


def test_runtime_reports_unavailable_as_resolved_without_all_profiles_saved(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_specialty(workspace)
    with connect(workspace) as conn:
        conn.execute("UPDATE doctor_profiles SET status='unavailable'")
        conn.commit()
    report = specialty_runtime_report(workspace, "dentists")
    assert report["collection_resolved"]
    assert not report["profiles_complete"]


def test_runtime_ignores_untraced_doctors_for_profile_projection(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_specialty(workspace)
    stamp = now_iso()
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO doctors (
                doctor_url,doctor_name,first_seen_page_url,first_seen_location_url,
                first_seen_specialty_url,first_seen_specialty_name,
                first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "https://www.zocdoc.com/doctor/untraced-dentist-999",
                "Untraced Dentist",
                "https://www.zocdoc.com/dentists/test-city-1pm/2",
                "https://www.zocdoc.com/dentists/test-city-1pm",
                "https://www.zocdoc.com/dentists",
                "Dentists",
                stamp,
                stamp,
            ),
        )
        conn.commit()

    report = specialty_runtime_report(workspace, "dentists")
    assert report["first_seen_providers"] == 1


def test_listing_404_is_resolved_but_not_fully_saved(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_specialty(workspace)
    with connect(workspace) as conn:
        conn.execute("UPDATE doctor_profiles SET status='saved'")
        conn.execute(
            """INSERT INTO pages(url,base_location_url,page_no,source,status,
               discovered_at,specialty_slug,specialty_name,specialty_url)
               SELECT url||'/2',base_location_url,2,'pagination','unavailable',
               discovered_at,specialty_slug,specialty_name,specialty_url FROM pages"""
        )
        conn.commit()
    report = specialty_runtime_report(workspace, "dentists")
    assert report["listing_resolved"]
    assert report["listing_unavailable"] == 1
    assert not report["listing_complete"]
    assert report["collection_resolved"]


def test_retired_pagination_does_not_block_listing_resolution(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    _seed_specialty(workspace)
    with connect(workspace) as conn:
        conn.execute(
            """INSERT INTO pages(url,base_location_url,page_no,source,status,
               discovered_at,specialty_slug,specialty_name,specialty_url)
               SELECT url||'/2',base_location_url,2,'pagination','retired',
               discovered_at,specialty_slug,specialty_name,specialty_url FROM pages"""
        )
        conn.commit()
    report = specialty_runtime_report(workspace, "dentists")
    assert report["listing_complete"]
    assert report["listing_resolved"]
