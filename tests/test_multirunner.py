from __future__ import annotations

from pathlib import Path

from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import seed_locations
from zocdoc_ortho.performance import PerformanceRecorder
from zocdoc_ortho.queue import _claim_pending_page
from zocdoc_ortho.runners import combined_performance_report, init_runners, load_runner_config
from zocdoc_ortho.saver import normalize_server_url
from zocdoc_ortho.workspace import Workspace


def test_runner_init_assigns_sequential_ports(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    config = init_runners(workspace, count=3, base_port=9100)

    assert [row["port"] for row in config["runners"]] == [9100, 9101, 9102]
    assert [row["runner_id"] for row in config["runners"]] == [
        "runner-01",
        "runner-02",
        "runner-03",
    ]
    loaded = load_runner_config(workspace)
    assert loaded["count"] == 3


def test_controller_url_must_be_localhost():
    assert normalize_server_url("http://127.0.0.1:9001/") == "http://127.0.0.1:9001"
    assert normalize_server_url("http://localhost:9002") == "http://localhost:9002"

    try:
        normalize_server_url("https://example.com")
    except ValueError as exc:
        assert "localhost" in str(exc)
    else:
        raise AssertionError("non-local controller URL should be rejected")


def test_atomic_listing_claims_do_not_duplicate(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    source = tmp_path / "locations.csv"
    source.write_text(
        "url,state_group,location,specialty_url,specialty_name,specialty_slug\n"
        "https://www.zocdoc.com/cardiologists/a-10001pm,NY,A,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n"
        "https://www.zocdoc.com/cardiologists/b-10002pm,NY,B,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n",
        encoding="utf-8",
    )
    with connect(workspace) as conn:
        seed_locations(source, conn)

    first = _claim_pending_page(workspace, "seed")
    second = _claim_pending_page(workspace, "seed")
    third = _claim_pending_page(workspace, "seed")

    assert first is not None
    assert second is not None
    assert first["url"] != second["url"]
    assert third is None
    with connect(workspace) as conn:
        opening = conn.execute("SELECT COUNT(*) n FROM pages WHERE status='opening'").fetchone()["n"]
    assert opening == 2


def test_runner_metrics_are_isolated_and_combined(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO pages(url,base_location_url,page_no,source,status,discovered_at)
            VALUES ('https://www.zocdoc.com/cardiologists/test-10001pm',
                    'https://www.zocdoc.com/cardiologists/test-10001pm',1,'seed','pending','2026-01-01T00:00:00Z')
            """
        )
        conn.commit()

    for runner_id in ("runner-01", "runner-02"):
        recorder = PerformanceRecorder(
            workspace,
            "collect-listings",
            {"runner_id": runner_id, "concurrency": 4},
        )
        recorder.record(
            page_type="listing",
            status="saved",
            url=f"https://www.zocdoc.com/cardiologists/{runner_id}-10001pm",
            duration_seconds=2.0,
        )
        recorder.finish()

    assert (workspace.output_dir / "crawl_run_metrics.runner-01.csv").exists()
    assert (workspace.output_dir / "crawl_run_metrics.runner-02.csv").exists()
    report = combined_performance_report(workspace)
    assert report["runner_count"] == 2
    assert report["combined_pages_per_minute"] == 60.0
    assert report["pending"] == 1
