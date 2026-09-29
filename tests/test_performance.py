from __future__ import annotations

from pathlib import Path

from zocdoc_ortho.cli import build_parser
from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import process_saved_html
from zocdoc_ortho.performance import PerformanceRecorder, performance_report
from zocdoc_ortho.utils import now_iso
from zocdoc_ortho.workspace import Workspace


def _listing_html(names: list[str]) -> str:
    cards = []
    for idx, name in enumerate(names, start=1):
        cards.append(
            f'''
            <article data-test="search-result-item">
              <a data-test="doctor-card-info-name" href="/doctor/provider-{idx}">{name}</a>
            </article>
            '''
        )
    return "<html><body>" + "".join(cards) + "</body></html>"


def test_listing_provider_set_hash_changes_when_membership_changes(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/cardiologists/test-city-12345pm"
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO pages (
                url,base_location_url,state_group,location,page_no,source,status,discovered_at,
                specialty_url,specialty_name,specialty_slug
            ) VALUES (?,?,?,?,1,'seed','pending',?,?,?,?)
            """,
            (
                url,
                url,
                "Test State",
                "Test City",
                now_iso(),
                "https://www.zocdoc.com/cardiologists",
                "Cardiologists",
                "cardiologists",
            ),
        )
        conn.commit()

        first = process_saved_html(conn, workspace, url, "", _listing_html(["A", "B"]), "first.html", 10)
        first_hash = first["provider_set_hash"]
        row = conn.execute("SELECT provider_set_hash FROM pages WHERE url=?", (url,)).fetchone()
        assert row["provider_set_hash"] == first_hash

        second = process_saved_html(conn, workspace, url, "", _listing_html(["A", "B", "C"]), "second.html", 20)
        assert second["provider_set_hash"] != first_hash
        row = conn.execute(
            "SELECT provider_set_hash,last_provider_set_changed_at FROM pages WHERE url=?", (url,)
        ).fetchone()
        assert row["provider_set_hash"] == second["provider_set_hash"]
        assert row["last_provider_set_changed_at"]


def test_performance_recorder_and_report(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO pages (url,base_location_url,page_no,source,status,discovered_at)
            VALUES ('https://www.zocdoc.com/cardiologists/test-city-12345pm',
                    'https://www.zocdoc.com/cardiologists/test-city-12345pm',1,'seed','pending',?)
            """,
            (now_iso(),),
        )
        conn.commit()

    recorder = PerformanceRecorder(
        workspace,
        "collect-listings",
        {
            "concurrency": 4,
            "stagger_seconds": 0.1,
            "poll_seconds": 0.2,
            "page_check_ms": 250,
            "stable_checks": 2,
            "page_max_wait_ms": 8000,
            "controller_poll_ms": 200,
        },
    )
    recorder.record(
        page_type="listing",
        status="saved",
        url="https://www.zocdoc.com/cardiologists/test-city-12345pm",
        duration_seconds=2.0,
        doctor_count=40,
        pagination_count=3,
    )
    summary = recorder.finish()
    assert summary["processed"] == 1
    assert (workspace.output_dir / "capture_performance.csv").exists()
    assert (workspace.output_dir / "crawl_run_metrics.csv").exists()

    report = performance_report(workspace, command="collect-listings", last=5)
    assert report["runs"] == 1
    assert report["pending"] == 1
    assert report["pages_per_minute"] > 0


def test_optimized_listing_cli_defaults():
    args = build_parser().parse_args(["collect-listings"])
    assert args.concurrency == 4
    assert args.stagger == 0.1
    assert args.poll == 0.2
    assert args.page_check_ms == 250
    assert args.stable_checks == 2
    assert args.page_max_wait_ms == 8000
    assert args.controller_poll_ms == 200
