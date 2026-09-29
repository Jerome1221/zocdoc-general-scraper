import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from zocdoc_ortho.db import connect
from zocdoc_ortho.saver import SaverRuntime, enqueue_url, make_handler
from zocdoc_ortho.updater import (
    check_location_snapshots,
    create_live_location_snapshots,
    create_location_snapshots,
    extract_verified_provider_count,
    load_refresh_queue,
)
from zocdoc_ortho.utils import now_iso
from zocdoc_ortho.workspace import Workspace


def _seed_target(workspace, *, index, count):
    now = now_iso()
    listing_url = f"https://www.zocdoc.com/dentists/test-city-{index}-1000{index}pm"
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO specialty_targets (
                listing_url, specialty_url, specialty_name, specialty_slug,
                state_group, location, is_active, first_seen_at, last_seen_at
            ) VALUES (?, ?, 'dentists', 'dentists', 'Test', ?, 1, ?, ?)
            """,
            (listing_url, "https://www.zocdoc.com/dentists", f"Test City {index}, TX", now, now),
        )
        conn.execute(
            """
            INSERT INTO pages (
                url, base_location_url, state_group, location, page_no, source, status,
                advertised_total, discovered_at, specialty_url, specialty_name, specialty_slug
            ) VALUES (?, ?, 'Test', ?, 1, 'seed', 'saved', ?, ?, ?, 'dentists', 'dentists')
            """,
            (
                listing_url,
                listing_url,
                f"Test City {index}, TX",
                count,
                now,
                "https://www.zocdoc.com/dentists",
            ),
        )
        conn.commit()
    return listing_url


def test_extract_verified_provider_count_is_specialty_agnostic():
    html = """
    <html><body>
      <div data-test="selection-criteria-header">
        <h1>1,305 verified orthopedic surgeons in Miami, FL</h1>
      </div>
    </body></html>
    """
    parsed = extract_verified_provider_count(html)
    assert parsed == {
        "specialty": "orthopedic surgeons",
        "location": "Miami, FL",
        "verified_provider_count": 1305,
    }


def test_updater_snapshot_check_and_queue(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    now = now_iso()
    listing_url = "https://www.zocdoc.com/dentists/austin-tx-245218pm"

    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO specialty_targets (
                listing_url, specialty_url, specialty_name, specialty_slug,
                state_group, location, is_active, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                listing_url,
                "https://www.zocdoc.com/dentists",
                "dentists",
                "dentists",
                "Texas",
                "Austin TX",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO pages (
                url, base_location_url, state_group, location, page_no, source, status,
                advertised_total, discovered_at, specialty_url, specialty_name, specialty_slug
            ) VALUES (?, ?, ?, ?, 1, 'seed', 'saved', ?, ?, ?, ?, ?)
            """,
            (
                listing_url,
                listing_url,
                "Texas",
                "Austin TX",
                245,
                now,
                "https://www.zocdoc.com/dentists",
                "dentists",
                "dentists",
            ),
        )
        conn.commit()

    snapshot = create_location_snapshots(workspace)
    assert snapshot == {"locations_processed": 1, "snapshots_created": 1, "failed": 0}

    immediate = check_location_snapshots(workspace)
    assert immediate["locations_checked"] == 1
    assert immediate["unchanged"] == 1
    assert immediate["changed"] == 0
    assert immediate["refresh_queue"] == 0

    with connect(workspace) as conn:
        latest_id = conn.execute("SELECT MAX(id) AS id FROM location_snapshots").fetchone()["id"]
        conn.execute(
            "UPDATE location_snapshots SET verified_provider_count=250 WHERE id=?",
            (latest_id,),
        )
        conn.commit()

    changed = check_location_snapshots(workspace)
    assert changed["locations_checked"] == 1
    assert changed["unchanged"] == 0
    assert changed["changed"] == 1
    assert changed["refresh_queue"] == 1

    rows = load_refresh_queue(workspace)
    assert len(rows) == 1
    assert rows[0]["specialty_slug"] == "dentists"
    assert rows[0]["location"] == "Austin TX"
    assert rows[0]["reason"] == "count_changed"
    assert rows[0]["status"] == "pending"


def test_live_snapshot_applies_limit_before_runner_enqueue(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    urls = [_seed_target(workspace, index=index, count=100 + index) for index in range(1, 4)]
    enqueued = []

    monkeypatch.setattr(
        "zocdoc_ortho.updater._active_live_runners",
        lambda _workspace: [{"runner_id": "runner-01", "controller_url": "http://127.0.0.1:9101"}],
    )
    monkeypatch.setattr("zocdoc_ortho.updater.configure_capture", lambda *args, **kwargs: {"ok": True})

    def fake_enqueue(url, *, server_url, capture_token, timeout=3.0):
        enqueued.append(url)
        index = urls.index(url) + 1
        html = f"<h1>{200 + index} verified dentists in Test City {index}, TX</h1>"
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(html, encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.updater.enqueue_url", fake_enqueue)

    result = create_live_location_snapshots(workspace, limit=2, poll_seconds=0.01)

    assert result["locations_selected"] == 2
    assert result["browser_fetched"] == 2
    assert result["snapshots_created"] == 2
    assert result["failed"] == 0
    assert enqueued == urls[:2]
    with connect(workspace) as conn:
        run = conn.execute("SELECT * FROM snapshot_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run["mode"] == "live"
        assert run["locations_selected"] == 2
        assert run["locations_processed"] == 2
        assert run["success_count"] == 2
        assert run["failed_count"] == 0

    immediate = check_location_snapshots(workspace)
    assert immediate["changed"] == 0
    assert immediate["refresh_queue"] == 0


def test_live_snapshot_from_snapshots_filters_before_limit(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    urls = [_seed_target(workspace, index=index, count=100 + index) for index in range(1, 4)]
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO location_snapshots (
                specialty, specialty_slug, location, location_url,
                verified_provider_count, checked_at, created_at
            ) VALUES ('dentists', 'dentists', 'Test City 2, TX', ?, 102, ?, ?)
            """,
            (urls[1], now_iso(), now_iso()),
        )
        conn.commit()

    enqueued = []
    monkeypatch.setattr(
        "zocdoc_ortho.updater._active_live_runners",
        lambda _workspace: [{"runner_id": "runner-01", "controller_url": "http://127.0.0.1:9101"}],
    )
    monkeypatch.setattr("zocdoc_ortho.updater.configure_capture", lambda *args, **kwargs: {"ok": True})

    def fake_enqueue(url, *, server_url, capture_token, timeout=3.0):
        enqueued.append(url)
        (workspace.updater_snapshot_dir / f"{capture_token}.html").write_text(
            "<h1>103 verified dentists in Test City 2, TX</h1>",
            encoding="utf-8",
        )
        return {"ok": True}

    monkeypatch.setattr("zocdoc_ortho.updater.enqueue_url", fake_enqueue)

    result = create_live_location_snapshots(
        workspace,
        from_snapshots=True,
        limit=5,
        poll_seconds=0.01,
    )

    assert result["locations_selected"] == 1
    assert result["snapshots_created"] == 1
    assert result["changed"] == 1
    assert result["refresh_queue"] == 1
    assert enqueued == [urls[1]]
    assert load_refresh_queue(workspace)[0]["location_url"] == urls[1]

    immediate = check_location_snapshots(workspace)
    assert immediate["changed"] == 0
    assert immediate["refresh_queue"] == 0


def test_saver_token_writes_temporary_updater_capture(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    runtime = SaverRuntime(workspace, runner_id="runner-test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_url = f"http://127.0.0.1:{server.server_port}"
    listing_url = "https://www.zocdoc.com/dentists/austin-tx-245218pm"
    token = "snapshot-1-test"
    try:
        enqueue_url(listing_url, server_url=server_url, capture_token=token)
        payload = json.dumps(
            {
                "url": listing_url,
                "title": "Test",
                "html": "<h1>245 verified dentists in Austin, TX</h1>",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{server_url}/save",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            saved = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert saved["ok"] is True
    assert (workspace.updater_snapshot_dir / f"{token}.html").exists()
    assert not any(workspace.listing_dir.iterdir())


def test_saver_token_also_isolates_profile_refresh_capture(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    runtime = SaverRuntime(workspace, runner_id="runner-test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_url = f"http://127.0.0.1:{server.server_port}"
    profile_url = "https://www.zocdoc.com/doctor/alpha-doctor-md-123"
    token = "daily-profile-1-test"
    try:
        enqueue_url(profile_url, server_url=server_url, capture_token=token)
        payload = json.dumps(
            {
                "url": profile_url,
                "title": "Alpha Doctor",
                "html": '<h1 data-test="provider-name">Alpha Doctor, MD</h1>',
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{server_url}/save",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            saved = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert saved["ok"] is True
    assert (workspace.updater_snapshot_dir / f"{token}.html").exists()
    assert not any(workspace.profile_dir.iterdir())
