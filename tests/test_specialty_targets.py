from __future__ import annotations

from pathlib import Path

from zocdoc_ortho.db import connect
from zocdoc_ortho.specialty_targets import parse_specialty_landing, refresh_specialty_targets
from zocdoc_ortho.urls import is_listing_url, is_specialty_landing_url
from zocdoc_ortho.utils import now_iso
from zocdoc_ortho.workspace import Workspace


def _landing_html() -> str:
    return """
    <!doctype html>
    <html>
      <head>
        <link rel="canonical" href="https://www.zocdoc.com/cardiologists" />
      </head>
      <body>
        <div data-test="tab-panel-location">
          <div data-test="expandable-list-section-california">
            <div role="button" data-test="expandable-list-section-heading-california">California</div>
            <div data-test="expandable-list-section-list">
              <a href="/cardiologists/los-angeles-13122pm">Los Angeles</a>
              <a href="/cardiologists/san-diego-223791pm">San Diego</a>
            </div>
          </div>
          <div data-test="expandable-list-section-new-york">
            <div role="button" data-test="expandable-list-section-heading-new-york">New York</div>
            <div data-test="expandable-list-section-list">
              <a href="/cardiologists/new-york-46063pm">New York</a>
            </div>
          </div>
        </div>
      </body>
    </html>
    """


def test_generic_specialty_url_classification():
    assert is_specialty_landing_url("https://www.zocdoc.com/cardiologists")
    assert is_listing_url("https://www.zocdoc.com/cardiologists/los-angeles-13122pm")
    assert is_listing_url("https://www.zocdoc.com/cardiologists/los-angeles-13122pm/2")
    assert not is_listing_url("https://www.zocdoc.com/cardiologists/aetna-300m")
    assert not is_specialty_landing_url("https://www.zocdoc.com/specialty")


def test_parse_specialty_landing_groups_locations_by_state():
    parsed = parse_specialty_landing(_landing_html(), "https://www.zocdoc.com/cardiologists")
    assert parsed["specialty_slug"] == "cardiologists"
    assert len(parsed["locations"]) == 3
    assert parsed["locations"][0]["state_group"] == "California"
    assert parsed["locations"][0]["location"] == "Los Angeles"
    assert parsed["locations"][2]["state_group"] == "New York"


def test_refresh_specialty_targets_seeds_listing_queue(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    html_path = workspace.specialty_landing_dir / "cardiologists--sample.html"
    html_path.write_text(_landing_html(), encoding="utf-8")

    with connect(workspace) as conn:
        stamp = now_iso()
        conn.execute(
            """
            INSERT INTO specialties (
                specialty_url,specialty_name,specialty_slug,entity_type,
                is_active,first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,1,?,?)
            """,
            (
                "https://www.zocdoc.com/cardiologists",
                "Cardiologists",
                "cardiologists",
                "provider_specialty",
                stamp,
                stamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO specialty_pages (
                specialty_url,specialty_name,specialty_slug,status,discovered_at
            ) VALUES (?,?,?,'pending',?)
            """,
            (
                "https://www.zocdoc.com/cardiologists",
                "Cardiologists",
                "cardiologists",
                stamp,
            ),
        )
        conn.commit()

        result = refresh_specialty_targets(
            conn,
            workspace,
            "https://www.zocdoc.com/cardiologists",
            html_path,
        )
        assert result["location_count"] == 3

        page = conn.execute(
            """
            SELECT specialty_name,specialty_slug,state_group,location,status
            FROM pages
            WHERE url='https://www.zocdoc.com/cardiologists/los-angeles-13122pm'
            """
        ).fetchone()
        assert page["specialty_name"] == "Cardiologists"
        assert page["specialty_slug"] == "cardiologists"
        assert page["state_group"] == "California"
        assert page["location"] == "Los Angeles"
        assert page["status"] == "pending"
