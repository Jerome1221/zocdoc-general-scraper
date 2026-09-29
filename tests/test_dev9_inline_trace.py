from pathlib import Path

from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import seed_locations
from zocdoc_ortho.queue import _ingest_listing
from zocdoc_ortho.urls import safe_slug
from zocdoc_ortho.workspace import Workspace


def _html() -> str:
    return """
    <html><body>
      <div data-test="selection-criteria-header">1 verified Cardiologist in Test</div>
      <article data-test="search-result-item">
        <a data-test="doctor-card-info-name" href="/doctor/alpha-doctor-md-123">
          <span data-test="doctor-card-info-name-full">Alpha Doctor</span>
        </a>
        <div data-test="doctor-card-info-specialty">Cardiologist</div>
      </article>
    </body></html>
    """


def _seed(workspace: Workspace, tmp_path: Path, url: str) -> Path:
    source = tmp_path / "seed.csv"
    source.write_text(
        "url,state_group,location,specialty_url,specialty_name,specialty_slug\n"
        f"{url},NY,Test,https://www.zocdoc.com/cardiologists,Cardiologists,cardiologists\n",
        encoding="utf-8",
    )
    with connect(workspace) as conn:
        seed_locations(source, conn)
    path = workspace.listing_dir / safe_slug(url)
    path.write_text(_html(), encoding="utf-8")
    return path


def test_dev9_ingest_traces_and_deletes_successful_listing_html(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/cardiologists/test-10001pm"
    path = _seed(workspace, tmp_path, url)

    with connect(workspace) as conn:
        parsed = _ingest_listing(conn, workspace, url, path, write_outputs=False)

    assert parsed["queue_status"] == "saved"
    assert parsed["listing_html_deleted"] is True
    assert parsed["trace_occurrence_count"] == 1
    assert not path.exists()

    with connect(workspace) as conn:
        page = conn.execute(
            "SELECT trace_status,trace_occurrence_count,trace_source_file,trace_html_deleted_at,advertised_total "
            "FROM pages WHERE url=?",
            (url,),
        ).fetchone()
        assert page["trace_status"] == "parsed"
        assert page["trace_occurrence_count"] == 1
        assert page["trace_source_file"] == safe_slug(url)
        assert page["trace_html_deleted_at"]
        assert page["advertised_total"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM listing_provider_trace").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM doctor_profiles").fetchone()["n"] == 1


def test_dev9_keep_listing_html_debug_flag(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/cardiologists/test-10001pm"
    path = _seed(workspace, tmp_path, url)

    with connect(workspace) as conn:
        parsed = _ingest_listing(
            conn,
            workspace,
            url,
            path,
            write_outputs=False,
            keep_listing_html=True,
        )

    assert parsed["listing_html_deleted"] is False
    assert path.exists()
    with connect(workspace) as conn:
        assert conn.execute("SELECT trace_status FROM pages WHERE url=?", (url,)).fetchone()["trace_status"] == "parsed"


def test_dev9_keeps_html_when_trace_coverage_is_partial(tmp_path: Path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/cardiologists/test-10001pm"
    path = _seed(workspace, tmp_path, url)

    monkeypatch.setattr("zocdoc_ortho.queue.persist_listing_trace_html", lambda *args, **kwargs: 0)

    with connect(workspace) as conn:
        parsed = _ingest_listing(conn, workspace, url, path, write_outputs=False)

    assert parsed["queue_status"] == "review"
    assert parsed["validation_status"] == "trace_partial"
    assert parsed["listing_html_deleted"] is False
    assert path.exists()
    with connect(workspace) as conn:
        page = conn.execute(
            "SELECT status,validation_status,trace_status FROM pages WHERE url=?", (url,)
        ).fetchone()
        assert page["status"] == "review"
        assert page["validation_status"] == "trace_partial"
        assert page["trace_status"] == "partial"
        assert conn.execute("SELECT COUNT(*) n FROM doctor_profiles").fetchone()["n"] == 0


def test_dev93_repairs_trace_partial_offline(tmp_path: Path, monkeypatch):
    from zocdoc_ortho.trace import repair_trace_partials

    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/cardiologists/test-10001pm"
    path = _seed(workspace, tmp_path, url)

    # Simulate an older build that validated the page but failed to persist its trace.
    with monkeypatch.context() as m:
        m.setattr("zocdoc_ortho.queue.persist_listing_trace_html", lambda *args, **kwargs: 0)
        with connect(workspace) as conn:
            parsed = _ingest_listing(conn, workspace, url, path, write_outputs=False)
        assert parsed["validation_status"] == "trace_partial"
        assert path.exists()

    result = repair_trace_partials(workspace)
    assert result["selected"] == 1
    assert result["repaired"] == 1
    assert result["still_partial"] == 0
    assert result["occurrences"] == 1
    assert path.exists()  # retained unless --delete-html is explicitly requested

    with connect(workspace) as conn:
        page = conn.execute(
            "SELECT status,validation_status,trace_status,trace_occurrence_count FROM pages WHERE url=?",
            (url,),
        ).fetchone()
        assert page["status"] == "saved"
        assert page["validation_status"] == "valid_repaired"
        assert page["trace_status"] == "parsed"
        assert page["trace_occurrence_count"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM listing_provider_trace").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM doctor_profiles").fetchone()["n"] == 1
