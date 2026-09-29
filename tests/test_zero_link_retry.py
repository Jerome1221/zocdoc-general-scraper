from pathlib import Path

from zocdoc_ortho import queue
from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import parse_page, seed_locations
from zocdoc_ortho.urls import safe_slug
from zocdoc_ortho.workspace import Workspace


def _seed_one(workspace: Workspace, tmp_path: Path) -> str:
    url = "https://www.zocdoc.com/cardiologists/test-city-10001pm"
    source = tmp_path / "locations.csv"
    source.write_text(
        "url,state_group,location,specialty_url,specialty_name,specialty_slug\n"
        f"{url},NY,Test City,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n",
        encoding="utf-8",
    )
    with connect(workspace) as conn:
        seed_locations(source, conn)
    return url


def _mark_legacy_saved_zero(workspace: Workspace, url: str, html: str) -> Path:
    path = workspace.listing_dir / safe_slug(url)
    path.write_text(html, encoding="utf-8")
    with connect(workspace) as conn:
        conn.execute(
            """
            UPDATE pages
            SET status='saved',file=?,bytes=?,doctor_links_found=0,saved_at='2026-09-20T00:00:00Z'
            WHERE url=?
            """,
            (path.name, path.stat().st_size, url),
        )
        conn.commit()
    return path


def test_legacy_saved_zero_is_quarantined_and_retried_next_run(tmp_path: Path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = _seed_one(workspace, tmp_path)
    original = _mark_legacy_saved_zero(
        workspace,
        url,
        "<html><body><main>Rendered search shell without provider cards.</main></body></html>",
    )

    result = queue.audit_zero_link_listings(workspace, max_retries=2)
    assert result["examined"] == 1
    assert result["retry_pending"] == 1
    assert not original.exists()

    with connect(workspace) as conn:
        row = conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        assert row["status"] == "retry_pending"
        assert row["zero_link_retry_count"] == 1
        assert row["last_rejected_file"]

    monkeypatch.setattr(queue, "_assert_saver_ready", lambda _workspace: None)
    monkeypatch.setattr(queue, "configure_capture", lambda *args, **kwargs: {"ok": True})

    def fake_enqueue(enqueued_url: str):
        html = (
            '<html><body><article data-test="search-result-item">'
            '<a data-test="doctor-card-info-name" href="/doctor/retry-doctor-123">Retry Doctor</a>'
            "</article></body></html>"
        )
        (workspace.listing_dir / safe_slug(enqueued_url)).write_text(html, encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(queue, "enqueue_url", fake_enqueue)

    completed = queue.collect_listing_batch(
        workspace,
        limit=1,
        concurrency=1,
        stagger_seconds=0,
        poll_seconds=0.01,
        timeout_seconds=2,
    )
    assert completed == 1
    with connect(workspace) as conn:
        row = conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        assert row["status"] == "saved"
        assert row["doctor_links_found"] == 1
        assert row["validation_status"] == "valid"
        assert row["zero_link_retry_count"] == 0


def test_explicit_empty_page_becomes_valid_empty(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = _seed_one(workspace, tmp_path)
    original = _mark_legacy_saved_zero(
        workspace,
        url,
        "<html><body><h1>No providers found</h1><p>Try another location.</p></body></html>",
    )

    result = queue.audit_zero_link_listings(workspace, max_retries=2)
    assert result["valid_empty"] == 1
    assert original.exists()

    with connect(workspace) as conn:
        row = conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        assert row["status"] == "valid_empty"
        assert row["doctor_links_found"] == 0
        assert row["validation_status"] == "valid_empty"
        assert row["zero_link_retry_count"] == 0


def test_parse_zero_page_does_not_invent_provider_links():
    parsed = parse_page("<html><body><main>No provider cards here.</main></body></html>", "https://www.zocdoc.com/cardiologists/test-10001pm")
    assert parsed["doctors"] == []
