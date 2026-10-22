from __future__ import annotations

import csv
import json

import pytest

from zocdoc_ortho.daily_sync import (
    _finish_profile_attempt,
    bootstrap_from_trace,
    collect_daily_profiles,
    migrate_profiles_from_export,
    prepare_profile_queue,
    resume_daily_profiles,
    run_daily_sync,
)
from zocdoc_ortho.db import connect
from zocdoc_ortho.queue import _ingest_listing
from zocdoc_ortho.urls import page_number, safe_slug
from zocdoc_ortho.utils import now_iso
from zocdoc_ortho.workspace import Workspace


def _seed_target(workspace: Workspace) -> str:
    listing_url = "https://www.zocdoc.com/cardiologists/test-10001pm"
    stamp = now_iso()
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO specialty_targets(
                listing_url,specialty_url,specialty_name,specialty_slug,
                state_group,location,is_active,first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,?,?,1,?,?)
            """,
            (
                listing_url,
                "https://www.zocdoc.com/cardiologists",
                "Cardiologists",
                "cardiologists",
                "NY",
                "Test, NY",
                stamp,
                stamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO pages(
                url,base_location_url,state_group,location,page_no,source,status,
                discovered_at,specialty_url,specialty_name,specialty_slug
            ) VALUES (?,?,?,?,1,'seed','saved',?,?,?,?)
            """,
            (
                listing_url,
                listing_url,
                "NY",
                "Test, NY",
                stamp,
                "https://www.zocdoc.com/cardiologists",
                "Cardiologists",
                "cardiologists",
            ),
        )
        conn.commit()
    return listing_url


def _listing_html(
    listing_url: str,
    doctors: list[tuple[str, str]],
    *,
    advertised_total: int,
    pagination: list[int] | None = None,
) -> str:
    cards = "".join(
        f"""
        <article data-test="search-result-item">
          <a data-test="doctor-card-info-name" href="/doctor/{slug}">{name}</a>
          <div data-test="doctor-card-info-specialty">Cardiologist</div>
        </article>
        """
        for slug, name in doctors
    )
    links = "".join(
        f'<a href="{listing_url if number == 1 else f"{listing_url}/{number}"}">{number}</a>'
        for number in (pagination or [])
    )
    return f"""
    <html><body>
      <div data-test="selection-criteria-header">
        {advertised_total} verified Cardiologists in Test, NY
      </div>
      {cards}
      <nav data-test="search-results-pagination">{links}</nav>
    </body></html>
    """


def _mock_runners(monkeypatch) -> None:
    monkeypatch.setattr(
        "zocdoc_ortho.daily_sync._active_live_runners",
        lambda _workspace: [
            {"runner_id": "runner-01", "controller_url": "http://127.0.0.1:9101"}
        ],
    )
    monkeypatch.setattr(
        "zocdoc_ortho.daily_sync.configure_capture",
        lambda *args, **kwargs: {"ok": True},
    )


@pytest.mark.parametrize("historical", [False, True])
def test_unavailable_survives_repeated_runs_and_old_pending_jobs(tmp_path, monkeypatch, historical):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing = _seed_target(workspace)
    doctor = "https://www.zocdoc.com/doctor/alpha-doctor-md-1"
    path = workspace.listing_dir / "fixture.html"
    path.write_text(_listing_html(listing, [("alpha-doctor-md-1", "Alpha Doctor")], advertised_total=1))
    with connect(workspace) as conn:
        _ingest_listing(conn, workspace, listing, path, write_outputs=False)
    first = bootstrap_from_trace(workspace)
    if historical:
        with connect(workspace) as conn:
            conn.execute("UPDATE canonical_provider_records SET profile_sha256='history',profile_data_json='[]' WHERE doctor_url=?", (doctor,))
            conn.commit()
    capture = workspace.updater_snapshot_dir / "unavailable.html"
    capture.write_text("Not Found. Redirecting to /")
    assert _finish_profile_attempt(workspace, first["run_id"], doctor, capture)[0] == "unavailable"
    monkeypatch.setattr("zocdoc_ortho.daily_sync._active_live_runners", lambda _: pytest.fail("must not capture"))
    assert prepare_profile_queue(workspace, first["run_id"], refresh_days=0) == 0
    for _ in range(2):
        assert bootstrap_from_trace(workspace)["profiles_queued"] == 0
    _mock_runners(monkeypatch)
    def listing_capture(url, *, capture_token, **kwargs):
        assert url == listing
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            _listing_html(listing, [("alpha-doctor-md-1", "Alpha Doctor")], advertised_total=1)
        )
        return {"ok": True}
    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", listing_capture)
    for _ in range(2):
        assert run_daily_sync(workspace, profile_refresh_days=0, poll_seconds=0.01)["profiles_queued"] == 0
    monkeypatch.setattr("zocdoc_ortho.daily_sync._active_live_runners", lambda _: pytest.fail("must not capture"))
    with connect(workspace) as conn:
        conn.execute("UPDATE daily_profile_queue SET status='pending' WHERE run_id=?", (first["run_id"],))
        conn.commit()
    assert collect_daily_profiles(workspace, first["run_id"], retry_failed=True)["selected"] == 0
    with connect(workspace) as conn:
        assert conn.execute("SELECT status FROM doctor_profiles").fetchone()[0] == "unavailable"
        assert conn.execute("SELECT status FROM daily_profile_queue WHERE run_id=?", (first["run_id"],)).fetchone()[0] == "unavailable"
        assert conn.execute("SELECT is_active FROM provider_location_memberships").fetchone()[0] == 1
        assert conn.execute("SELECT profile_sha256 FROM canonical_provider_records").fetchone()[0] == ("history" if historical else "")


def test_explicit_unavailable_recheck_is_bounded_and_scoped(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing = _seed_target(workspace)
    path = workspace.listing_dir / "fixture.html"
    path.write_text(_listing_html(listing, [("alpha-md-1", "Alpha"), ("beta-md-2", "Beta"), ("gamma-md-3", "Gamma")], advertised_total=3))
    with connect(workspace) as conn:
        _ingest_listing(conn, workspace, listing, path, write_outputs=False)
    run = bootstrap_from_trace(workspace)["run_id"]
    for slug in ["alpha-md-1", "beta-md-2", "gamma-md-3"]:
        token = workspace.updater_snapshot_dir / "unavailable.html"
        token.write_text("Not Found. Redirecting to /404")
        _finish_profile_attempt(workspace, run, f"https://www.zocdoc.com/doctor/{slug}", token)
    with connect(workspace) as conn:
        conn.execute("UPDATE canonical_provider_records SET profile_sha256='historical'")
        conn.commit()
    later = bootstrap_from_trace(workspace)["run_id"]
    _mock_runners(monkeypatch)
    launched = []
    def capture(url, *, capture_token, **kwargs):
        launched.append(url)
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text('<h1 data-test="provider-name">Recovered Doctor</h1>')
        return {"ok": True}
    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", capture)
    with pytest.raises(ValueError, match="explicit profile limit"):
        collect_daily_profiles(workspace, run, retry_unavailable=True)
    # Alpha is outside the requested specialty.
    with connect(workspace) as conn:
        conn.execute("UPDATE provider_location_memberships SET specialty='Dentists',specialty_slug='dentists' WHERE doctor_url LIKE '%alpha-md-1'")
        conn.commit()
    result = resume_daily_profiles(workspace, run_id=run, retry_unavailable=True, specialties=("cardiologists",), limit=1, poll_seconds=0.01)
    assert result["profile_batch"]["saved"] == 1
    assert launched == ["https://www.zocdoc.com/doctor/beta-md-2"]
    with connect(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_profile_queue WHERE run_id=?", (later,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM doctor_profiles WHERE status='unavailable'").fetchone()[0] == 2
    # A failed explicit recheck must keep the confirmed unavailable state.
    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", lambda *args, **kwargs: {"ok": True})
    failed = resume_daily_profiles(workspace, run_id=run, retry_unavailable=True,
        specialties=("cardiologists",), limit=1, timeout_seconds=0, capture_retries=0, poll_seconds=0.01)
    assert failed["profile_batch"]["failed"] == 1
    with connect(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM doctor_profiles WHERE status='unavailable'").fetchone()[0] == 2


def test_daily_sync_crawls_pagination_and_soft_deactivates_removed_provider(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    _mock_runners(monkeypatch)
    generation = {"value": 1}

    def fake_enqueue(url, *, server_url, capture_token, timeout=3.0):
        if generation["value"] == 1 and page_number(url) == 1:
            html = _listing_html(
                listing_url,
                [("alpha-doctor-md-1", "Alpha Doctor")],
                advertised_total=2,
                pagination=[1, 2],
            )
        elif generation["value"] == 1:
            html = _listing_html(
                listing_url,
                [("beta-doctor-md-2", "Beta Doctor")],
                advertised_total=2,
                pagination=[1, 2],
            )
        else:
            html = _listing_html(
                listing_url,
                [("alpha-doctor-md-1", "Alpha Doctor")],
                advertised_total=1,
            )
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_enqueue)
    first = run_daily_sync(
        workspace,
        skip_profiles=True,
        removal_confirmations=1,
        poll_seconds=0.01,
    )
    assert first["status"] == "complete"
    assert first["pages_fetched"] == 2
    assert first["providers_added"] == 2

    generation["value"] = 2
    second = run_daily_sync(
        workspace,
        skip_profiles=True,
        removal_confirmations=1,
        poll_seconds=0.01,
    )
    assert second["status"] == "complete"
    assert second["pages_fetched"] == 1
    assert second["providers_removed"] == 1
    with connect(workspace) as conn:
        memberships = conn.execute(
            """
            SELECT doctor_url,is_active,missing_streak
            FROM provider_location_memberships ORDER BY doctor_url
            """
        ).fetchall()
        assert [(row["doctor_url"].split("/")[-1], row["is_active"]) for row in memberships] == [
            ("alpha-doctor-md-1", 1),
            ("beta-doctor-md-2", 0),
        ]
        assert conn.execute(
            "SELECT status FROM pages WHERE url=?", (f"{listing_url}/2",)
        ).fetchone()["status"] == "retired"


@pytest.mark.parametrize("failure_mode", ["timeout", "404"])
def test_incomplete_daily_crawl_never_removes_provider(tmp_path, monkeypatch, failure_mode):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    _mock_runners(monkeypatch)
    fail_page_two = {"value": False}
    failed_attempts = []

    def fake_enqueue(url, *, server_url, capture_token, timeout=3.0):
        if page_number(url) == 2 and fail_page_two["value"]:
            failed_attempts.append(url)
            if failure_mode == "404":
                html = """<title>404 Error - Zocdoc</title><h1>Well, this is unexpected.</h1>
                <p>It looks like this page may no longer be available.</p>"""
                (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(html, encoding="utf-8")
            return {"ok": True}
        doctors = (
            [("alpha-doctor-md-1", "Alpha Doctor")]
            if page_number(url) == 1
            else [("beta-doctor-md-2", "Beta Doctor")]
        )
        html = _listing_html(listing_url, doctors, advertised_total=2, pagination=[1, 2])
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_enqueue)
    baseline = run_daily_sync(
        workspace,
        skip_profiles=True,
        removal_confirmations=1,
        poll_seconds=0.01,
    )
    assert baseline["providers_added"] == 2

    fail_page_two["value"] = True
    failed = run_daily_sync(
        workspace,
        skip_profiles=True,
        removal_confirmations=1,
        timeout_seconds=0.02,
        capture_retries=2 if failure_mode == "404" else 0,
        poll_seconds=0.01,
    )
    assert failed["status"] == "failed"
    assert failed["locations_failed"] == 1
    assert failed["providers_removed"] == 0
    assert len(failed_attempts) == 1
    if failure_mode == "404":
        assert len(list(workspace.rejected_listing_dir.glob("updater404-*.html"))) == 1
    with connect(workspace) as conn:
        assert conn.execute(
            "SELECT COUNT(*) n FROM provider_location_memberships WHERE is_active=1"
        ).fetchone()["n"] == 2


def test_daily_profile_resume_writes_canonical_profile_data(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    _mock_runners(monkeypatch)

    def fake_listing_enqueue(url, *, server_url, capture_token, timeout=3.0):
        html = _listing_html(
            listing_url,
            [("alpha-doctor-md-1", "Alpha Doctor")],
            advertised_total=1,
        )
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_listing_enqueue)
    first = run_daily_sync(workspace, skip_profiles=True, poll_seconds=0.01)
    assert first["profiles_queued"] == 1

    def fake_profile_enqueue(url, *, server_url, capture_token, timeout=3.0):
        html = """
        <html><head><meta property="og:image" content="https://img.test/alpha.jpg"></head>
        <body><h1 data-test="provider-name">Alpha Doctor, MD</h1></body></html>
        """
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_profile_enqueue)
    resumed = resume_daily_profiles(workspace, run_id=first["id"], poll_seconds=0.01)
    assert resumed["profile_batch"]["saved"] == 1
    assert resumed["profiles_pending"] == 0
    doctor_url = "https://www.zocdoc.com/doctor/alpha-doctor-md-1"
    assert (workspace.profile_dir / safe_slug(doctor_url)).exists()
    with connect(workspace) as conn:
        record = conn.execute(
            "SELECT profile_sha256,profile_data_json FROM canonical_provider_records WHERE doctor_url=?",
            (doctor_url,),
        ).fetchone()
        assert record["profile_sha256"]
        assert "Alpha Doctor" in record["profile_data_json"]


def test_daily_profile_404_is_terminal_and_preserves_membership(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    _mock_runners(monkeypatch)

    def fake_listing_enqueue(url, *, server_url, capture_token, timeout=3.0):
        html = _listing_html(
            listing_url,
            [("missing-doctor-md-404", "Missing Doctor")],
            advertised_total=1,
        )
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_listing_enqueue)
    run = run_daily_sync(workspace, skip_profiles=True, poll_seconds=0.01)
    doctor_url = "https://www.zocdoc.com/doctor/missing-doctor-md-404"
    token_path = workspace.updater_snapshot_dir / "profile-404.html"
    token_path.write_text(
        "<html><body>Not Found. Redirecting to /404</body></html>",
        encoding="utf-8",
    )

    outcome, reason = _finish_profile_attempt(
        workspace, run["id"], doctor_url, token_path
    )

    assert outcome == "unavailable"
    assert reason == "Zocdoc profile returned /404"
    assert not token_path.exists()
    assert (workspace.unavailable_profile_dir / safe_slug(doctor_url)).exists()
    with connect(workspace) as conn:
        queue_status = conn.execute(
            "SELECT status FROM daily_profile_queue WHERE run_id=? AND doctor_url=?",
            (run["id"], doctor_url),
        ).fetchone()["status"]
        profile_status = conn.execute(
            "SELECT status FROM doctor_profiles WHERE doctor_url=?",
            (doctor_url,),
        ).fetchone()["status"]
        membership_active = conn.execute(
            "SELECT is_active FROM provider_location_memberships WHERE doctor_url=?",
            (doctor_url,),
        ).fetchone()["is_active"]
    assert queue_status == "unavailable"
    assert profile_status == "unavailable"
    assert membership_active == 1


def test_daily_profile_resume_filters_by_active_membership_specialty(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    _mock_runners(monkeypatch)

    def fake_listing_enqueue(url, *, server_url, capture_token, timeout=3.0):
        html = _listing_html(
            listing_url,
            [
                ("alpha-doctor-md-1", "Alpha Doctor"),
                ("beta-doctor-md-2", "Beta Doctor"),
                ("gamma-doctor-md-3", "Gamma Doctor"),
            ],
            advertised_total=3,
        )
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_listing_enqueue)
    first = run_daily_sync(workspace, skip_profiles=True, poll_seconds=0.01)
    assert first["profiles_queued"] == 3
    beta_url = "https://www.zocdoc.com/doctor/beta-doctor-md-2"
    gamma_url = "https://www.zocdoc.com/doctor/gamma-doctor-md-3"
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE provider_location_memberships
            SET specialty='Dermatologists',specialty_slug='dermatologists'
            WHERE doctor_url=?
            """,
            (beta_url,),
        )
        conn.execute(
            """
            UPDATE provider_location_memberships
            SET specialty='Allergists',specialty_slug='allergists'
            WHERE doctor_url=?
            """,
            (gamma_url,),
        )
        conn.commit()

    captured: list[str] = []

    def fake_profile_enqueue(url, *, server_url, capture_token, timeout=3.0):
        captured.append(url)
        html = f'<html><body><h1 data-test="provider-name">{url}</h1></body></html>'
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_profile_enqueue)
    resumed = resume_daily_profiles(
        workspace,
        run_id=first["id"],
        specialties=("cardiologists",),
        poll_seconds=0.01,
    )

    assert resumed["profile_batch"]["selected"] == 1
    assert captured == ["https://www.zocdoc.com/doctor/alpha-doctor-md-1"]
    with connect(workspace) as conn:
        pending = conn.execute(
            """
            SELECT doctor_url FROM daily_profile_queue
            WHERE run_id=? AND status='pending'
            """,
            (first["id"],),
        ).fetchall()
    assert [row["doctor_url"] for row in pending] == [beta_url, gamma_url]

    def fake_blocked_enqueue(url, *, server_url, capture_token, timeout=3.0):
        workspace.blocked_flag.write_text("test restriction", encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_blocked_enqueue)
    blocked = resume_daily_profiles(
        workspace,
        run_id=first["id"],
        specialties=("dermatologists",),
        poll_seconds=0.01,
    )
    workspace.blocked_flag.unlink()
    assert blocked["profile_batch"]["failed"] == 1
    with connect(workspace) as conn:
        statuses = {
            row["doctor_url"]: row["status"]
            for row in conn.execute(
                "SELECT doctor_url,status FROM daily_profile_queue WHERE run_id=?",
                (first["id"],),
            )
        }
    assert statuses[beta_url] == "failed"
    assert statuses[gamma_url] == "pending"

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_profile_enqueue)
    retried = resume_daily_profiles(
        workspace,
        run_id=first["id"],
        specialties=("dermatologists",),
        retry_failed=True,
        poll_seconds=0.01,
    )
    assert retried["profile_batch"]["selected"] == 1
    assert retried["profile_batch"]["saved"] == 1
    assert captured == [
        "https://www.zocdoc.com/doctor/alpha-doctor-md-1",
        beta_url,
    ]
    with connect(workspace) as conn:
        gamma_status = conn.execute(
            "SELECT status FROM daily_profile_queue WHERE run_id=? AND doctor_url=?",
            (first["id"], gamma_url),
        ).fetchone()["status"]
    assert gamma_status == "pending"


def test_bootstrap_seeds_existing_trace_without_browser_requests(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    html = _listing_html(
        listing_url,
        [("alpha-doctor-md-1", "Alpha Doctor")],
        advertised_total=1,
    )
    path = workspace.listing_dir / "bootstrap.html"
    path.write_text(html, encoding="utf-8")
    with connect(workspace) as conn:
        _ingest_listing(conn, workspace, listing_url, path, write_outputs=False)

    result = bootstrap_from_trace(workspace)

    assert result["locations"] == 1
    assert result["memberships"] == 1
    assert result["providers"] == 1
    assert result["profiles_imported"] == 0
    assert result["profiles_queued"] == 1
    with connect(workspace) as conn:
        assert conn.execute(
            "SELECT COUNT(*) n FROM provider_location_memberships WHERE is_active=1"
        ).fetchone()["n"] == 1


def test_bootstrap_migrates_export_and_does_not_reopen_canonical_profile(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    doctor_url = "https://www.zocdoc.com/doctor/alpha-doctor-md-1"
    html = _listing_html(
        listing_url,
        [("alpha-doctor-md-1", "Alpha Doctor")],
        advertised_total=1,
    )
    path = workspace.listing_dir / "bootstrap-export.html"
    path.write_text(html, encoding="utf-8")
    with connect(workspace) as conn:
        _ingest_listing(conn, workspace, listing_url, path, write_outputs=False)

    export_path = workspace.output_dir / "unique_doctors.csv"
    with export_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "full_name",
                "profile_url",
                "accepts_new_patients",
                "hospital_affiliations",
                "num_locations",
                "locations",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "full_name": "Alpha Doctor, MD",
                "profile_url": doctor_url,
                "accepts_new_patients": "True",
                "hospital_affiliations": "[]",
                "num_locations": "1",
                "locations": json.dumps([{"city": "New York", "state": "NY"}]),
            }
        )

    result = bootstrap_from_trace(workspace)

    assert result["profiles_imported"] == 1
    assert result["profiles_queued"] == 0
    assert not (workspace.profile_dir / safe_slug(doctor_url)).exists()
    with connect(workspace) as conn:
        record = conn.execute(
            """
            SELECT profile_sha256,profile_data_json,profile_updated_at
            FROM canonical_provider_records WHERE doctor_url=?
            """,
            (doctor_url,),
        ).fetchone()
        assert record["profile_sha256"]
        assert record["profile_updated_at"]
        assert "Alpha Doctor" in record["profile_data_json"]

    _mock_runners(monkeypatch)

    def fake_enqueue(url, *, server_url, capture_token, timeout=3.0):
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            html, encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.daily_sync.enqueue_url", fake_enqueue)
    daily = run_daily_sync(workspace, skip_profiles=True, poll_seconds=0.01)
    assert daily["profiles_queued"] == 0


def test_profile_export_migration_preserves_newer_canonical_profile(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    doctor_url = "https://www.zocdoc.com/doctor/alpha-doctor-md-1"
    html = _listing_html(
        listing_url,
        [("alpha-doctor-md-1", "Alpha Doctor")],
        advertised_total=1,
    )
    path = workspace.listing_dir / "migration-preserve.html"
    path.write_text(html, encoding="utf-8")
    with connect(workspace) as conn:
        _ingest_listing(conn, workspace, listing_url, path, write_outputs=False)
    bootstrap_from_trace(workspace)
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE canonical_provider_records
            SET profile_sha256='newer-live-profile',profile_data_json='[{"source":"live"}]',
                profile_updated_at=?
            WHERE doctor_url=?
            """,
            (now_iso(), doctor_url),
        )
        conn.commit()

    export_path = workspace.output_dir / "unique_doctors.csv"
    with export_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["full_name", "profile_url"])
        writer.writeheader()
        writer.writerow({"full_name": "Old Export", "profile_url": doctor_url})

    result = migrate_profiles_from_export(workspace)
    assert result["imported"] == 0
    assert result["already_present"] == 1
    with connect(workspace) as conn:
        record = conn.execute(
            "SELECT profile_sha256,profile_data_json FROM canonical_provider_records WHERE doctor_url=?",
            (doctor_url,),
        ).fetchone()
        assert record["profile_sha256"] == "newer-live-profile"
        assert '"live"' in record["profile_data_json"]
