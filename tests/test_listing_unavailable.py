import sqlite3

import pytest

from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import classify_listing_capture, ensure_page
from zocdoc_ortho.queue import _ingest_listing, activate_zero_link_retries, repair_unavailable_listings
from zocdoc_ortho.workspace import Workspace

NOT_FOUND = """<html><head><title>Well, this is unexpected. - 404 Error - Zocdoc</title></head>
<body><h1>Well, this is unexpected.</h1>
<p>Our apologies. It looks like this page may no longer be available.</p></body></html>"""


@pytest.mark.parametrize("html", [NOT_FOUND, "Not Found. Redirecting to /404"])
def test_explicit_404_overrides_unrelated_links_and_zero_totals(html):
    result = classify_listing_capture(html, {"doctors": [{"doctor_url": "footer-link"}], "advertised_total": 0})
    assert result[0] == "unavailable"


@pytest.mark.parametrize("html", [
    "<title>Search results</title><p>A provider at 404 Main Street</p>",
    "<p>No results found</p>",
    "<title>404 Error - Zocdoc</title><p>Verify you are human</p>",
    "<title>404 Error - Zocdoc</title><p>Transient incomplete response</p>",
])
def test_generic_empty_or_challenge_is_not_unavailable(html):
    assert classify_listing_capture(html, {"doctors": [], "advertised_total": None})[0] != "unavailable"


def test_ingest_404_preserves_trace_and_stops_automatic_retry(tmp_path):
    w = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/oncologists/test-city-1pm/6"
    with connect(w) as c:
        ensure_page(c, url)
        c.execute(
            """UPDATE pages SET status='saved',provider_set_hash='historical',
               doctor_links_found=7,trace_status='parsed',trace_occurrence_count=7 WHERE url=?""",
            (url,),
        )
        c.execute("INSERT INTO listing_provider_trace(listing_url,doctor_url) VALUES (?,?)", (url, "doctor"))
        c.commit()
        f = w.listing_dir / "404.html"
        f.write_text(NOT_FOUND, encoding="utf-8")
        parsed = _ingest_listing(c, w, url, f, write_outputs=False)
        r = c.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        assert parsed["queue_status"] == r["status"] == "unavailable"
        assert r["validation_status"] == "unavailable_404"
        assert r["zero_link_retry_count"] == 0
        assert r["provider_set_hash"] == "historical"
        assert r["doctor_links_found"] == r["trace_occurrence_count"] == 7
        assert r["trace_status"] == "parsed"
        assert c.execute("SELECT COUNT(*) FROM listing_provider_trace").fetchone()[0] == 1
        assert (w.root / r["last_rejected_file"]).read_text(encoding="utf-8") == NOT_FOUND
        assert not f.exists()
    assert activate_zero_link_retries(w) == 0


def test_repair_is_scoped_backed_up_idempotent_and_preserves_data(tmp_path):
    w = Workspace(tmp_path / "workspace").ensure()
    urls = []
    with connect(w) as c:
        for slug in ("oncologists", "neurosurgeons"):
            url = f"https://www.zocdoc.com/{slug}/test-city-1pm/6"
            urls.append(url)
            ensure_page(c, url)
            evidence = w.rejected_listing_dir / f"{slug}.html"
            evidence.write_text(NOT_FOUND, encoding="utf-8")
            c.execute(
                """UPDATE pages SET specialty_slug=?,status='retry_pending',
                   last_rejected_file=?,provider_set_hash='retained' WHERE url=?""",
                (slug, evidence.relative_to(w.root).as_posix(), url),
            )
        c.commit()
    preview = repair_unavailable_listings(w, specialties=("oncologists",), dry_run=True)
    assert len(preview["matches"]) == 1
    assert preview["repaired"] == 0
    assert preview["backup"] is None
    result = repair_unavailable_listings(w, specialties=("oncologists",))
    assert result["repaired"] == 1
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("SELECT status FROM pages WHERE url=?", (urls[0],)).fetchone()[0] == "retry_pending"
    with connect(w) as c:
        rows = c.execute("SELECT status,provider_set_hash FROM pages ORDER BY url").fetchall()
        assert sorted(r["status"] for r in rows) == ["retry_pending", "unavailable"]
        assert all(r["provider_set_hash"] == "retained" for r in rows)
    assert repair_unavailable_listings(w, specialties=("oncologists",))["repaired"] == 0
    assert repair_unavailable_listings(w)["repaired"] == 1


def test_repair_leaves_ambiguous_and_active_captures_untouched(tmp_path):
    w = Workspace(tmp_path / "workspace").ensure()
    with connect(w) as c:
        for number, status, html in [(2, "retry_pending", "<p>Loading</p>"), (3, "opening", NOT_FOUND)]:
            url = f"https://www.zocdoc.com/urologists/test-city-1pm/{number}"
            ensure_page(c, url)
            f = w.rejected_listing_dir / f"{number}.html"
            f.write_text(html, encoding="utf-8")
            c.execute("UPDATE pages SET status=?,last_rejected_file=? WHERE url=?",
                      (status, f.relative_to(w.root).as_posix(), url))
        c.commit()
    assert repair_unavailable_listings(w)["matches"] == []
