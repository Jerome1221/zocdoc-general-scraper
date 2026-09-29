from __future__ import annotations

from pathlib import Path

from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import process_saved_html, seed_locations
from zocdoc_ortho.trace import cleanup_parsed_listing_html, rebuild_listing_trace
from zocdoc_ortho.urls import safe_slug
from zocdoc_ortho.workspace import Workspace


def _listing_html(slug: str, name: str) -> str:
    return f"""
    <html><body>
      <article data-test="search-result-item">
        <a data-test="doctor-card-info-name" href="/doctor/{slug}">{name}</a>
        <div data-test="doctor-card-info-specialty">Cardiologist</div>
      </article>
    </body></html>
    """


def _seed_and_save(workspace: Workspace, tmp_path: Path, url: str, doctor_slug: str, doctor_name: str) -> None:
    source = tmp_path / f"{doctor_slug}.csv"
    source.write_text(
        "url,state_group,location,specialty_url,specialty_name,specialty_slug\n"
        f"{url},NY,Test,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n",
        encoding="utf-8",
    )
    html = _listing_html(doctor_slug, doctor_name)
    path = workspace.listing_dir / safe_slug(url)
    path.write_text(html, encoding="utf-8")
    with connect(workspace) as conn:
        seed_locations(source, conn)
        process_saved_html(conn, workspace, url, "", html, path.name, path.stat().st_size)


def test_incremental_trace_survives_raw_html_cleanup(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url1 = "https://www.zocdoc.com/cardiologists/a-10001pm"
    _seed_and_save(workspace, tmp_path, url1, "alpha-doctor-1", "Alpha Doctor")

    trace1, _, missing1 = rebuild_listing_trace(workspace)
    assert len(trace1) == 1
    assert missing1 == []
    with connect(workspace) as conn:
        page = conn.execute("SELECT trace_status,trace_occurrence_count FROM pages WHERE url=?", (url1,)).fetchone()
        assert page["trace_status"] == "parsed"
        assert page["trace_occurrence_count"] == 1

    cleaned = cleanup_parsed_listing_html(workspace)
    assert cleaned["deleted"] == 1
    assert not (workspace.listing_dir / safe_slug(url1)).exists()

    url2 = "https://www.zocdoc.com/cardiologists/b-10002pm"
    _seed_and_save(workspace, tmp_path, url2, "beta-doctor-2", "Beta Doctor")
    trace2, _, missing2 = rebuild_listing_trace(workspace)

    assert missing2 == []
    assert len(trace2) == 2
    assert set(trace2["doctor_name"]) == {"Alpha Doctor", "Beta Doctor"}
    with connect(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM listing_provider_trace").fetchone()["n"] == 2
        assert conn.execute("SELECT trace_status FROM pages WHERE url=?", (url1,)).fetchone()["trace_status"] == "parsed"


def test_new_capture_resets_trace_marker_for_reparse(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/cardiologists/a-10001pm"
    _seed_and_save(workspace, tmp_path, url, "alpha-doctor-1", "Alpha Doctor")
    rebuild_listing_trace(workspace)

    new_html = _listing_html("gamma-doctor-3", "Gamma Doctor")
    path = workspace.listing_dir / safe_slug(url)
    path.write_text(new_html, encoding="utf-8")
    with connect(workspace) as conn:
        process_saved_html(conn, workspace, url, "", new_html, path.name, path.stat().st_size)
        page = conn.execute("SELECT trace_status FROM pages WHERE url=?", (url,)).fetchone()
        assert page["trace_status"] == ""

    trace, _, _ = rebuild_listing_trace(workspace)
    assert list(trace["doctor_name"]) == ["Gamma Doctor"]


def test_trace_limit_processes_incrementally(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    urls = [
        "https://www.zocdoc.com/cardiologists/a-10001pm",
        "https://www.zocdoc.com/cardiologists/b-10002pm",
        "https://www.zocdoc.com/cardiologists/c-10003pm",
    ]
    doctors = [
        ("alpha-doctor-1", "Alpha Doctor"),
        ("beta-doctor-2", "Beta Doctor"),
        ("gamma-doctor-3", "Gamma Doctor"),
    ]
    for url, (slug, name) in zip(urls, doctors):
        _seed_and_save(workspace, tmp_path, url, slug, name)

    trace1, _, missing1 = rebuild_listing_trace(workspace, limit=2, batch_size=1)
    assert missing1 == []
    assert len(trace1) == 2
    with connect(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM pages WHERE trace_status='parsed'").fetchone()["n"] == 2

    trace2, _, missing2 = rebuild_listing_trace(workspace, limit=2, batch_size=1)
    assert missing2 == []
    assert len(trace2) == 3
    with connect(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM pages WHERE trace_status='parsed'").fetchone()["n"] == 3


def test_trace_progress_callback_reports_batches(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url1 = "https://www.zocdoc.com/cardiologists/a-10001pm"
    url2 = "https://www.zocdoc.com/cardiologists/b-10002pm"
    _seed_and_save(workspace, tmp_path, url1, "alpha-doctor-1", "Alpha Doctor")
    _seed_and_save(workspace, tmp_path, url2, "beta-doctor-2", "Beta Doctor")

    events = []
    rebuild_listing_trace(workspace, batch_size=1, progress_callback=events.append)

    assert events[0]["event"] == "start"
    batches = [event for event in events if event["event"] == "batch"]
    assert [event["processed"] for event in batches] == [1, 2]
    assert events[-1]["event"] == "done"
