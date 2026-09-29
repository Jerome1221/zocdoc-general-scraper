from __future__ import annotations

from pathlib import Path

from zocdoc_ortho import queue
from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import seed_locations
from zocdoc_ortho.urls import safe_slug
from zocdoc_ortho.workspace import Workspace


def test_listing_collector_rolling_pool_smoke(tmp_path: Path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    source = tmp_path / "locations.csv"
    source.write_text(
        "url,state_group,location,specialty_url,specialty_name,specialty_slug\n"
        "https://www.zocdoc.com/cardiologists/a-10001pm,NY,A,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n"
        "https://www.zocdoc.com/cardiologists/b-10002pm,NY,B,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n"
        "https://www.zocdoc.com/cardiologists/c-10003pm,NY,C,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n",
        encoding="utf-8",
    )
    with connect(workspace) as conn:
        seed_locations(source, conn)

    monkeypatch.setattr(queue, "_assert_saver_ready", lambda _workspace: None)
    monkeypatch.setattr(queue, "configure_capture", lambda *args, **kwargs: {"ok": True})

    def fake_enqueue(url: str):
        html = (
            '<html><body><article data-test="search-result-item">'
            '<a data-test="doctor-card-info-name" href="/doctor/test-doctor-1">Test Doctor</a>'
            "</article></body></html>"
        )
        (workspace.listing_dir / safe_slug(url)).write_text(html, encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(queue, "enqueue_url", fake_enqueue)

    completed = queue.collect_listing_batch(
        workspace,
        limit=3,
        concurrency=2,
        stagger_seconds=0,
        poll_seconds=0.01,
        timeout_seconds=2,
    )
    assert completed == 3
    with connect(workspace) as conn:
        statuses = [row["status"] for row in conn.execute("SELECT status FROM pages ORDER BY url")]
        hashes = [row["provider_set_hash"] for row in conn.execute("SELECT provider_set_hash FROM pages ORDER BY url")]
    assert statuses == ["saved", "saved", "saved"]
    assert all(hashes)
    assert (workspace.output_dir / "crawl_run_metrics.csv").exists()
