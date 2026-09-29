from __future__ import annotations

from zocdoc_ortho.daily_sync import bootstrap_from_trace, resume_daily_profiles, run_daily_sync
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


def test_incomplete_daily_crawl_never_removes_provider(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    listing_url = _seed_target(workspace)
    _mock_runners(monkeypatch)
    fail_page_two = {"value": False}

    def fake_enqueue(url, *, server_url, capture_token, timeout=3.0):
        if page_number(url) == 2 and fail_page_two["value"]:
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
        capture_retries=0,
        poll_seconds=0.01,
    )
    assert failed["status"] == "failed"
    assert failed["locations_failed"] == 1
    assert failed["providers_removed"] == 0
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
    with connect(workspace) as conn:
        assert conn.execute(
            "SELECT COUNT(*) n FROM provider_location_memberships WHERE is_active=1"
        ).fetchone()["n"] == 1
