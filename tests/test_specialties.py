from zocdoc_ortho.db import connect
from zocdoc_ortho.specialties import (
    classify_specialty_entity,
    parse_specialty_catalog,
    refresh_specialties,
)
from zocdoc_ortho.workspace import Workspace


def _catalog_html(extra: str = "") -> str:
    return f"""
    <html><body>
      <header><a href="/dentists">Dentists in header</a></header>
      <main>
        <h1>Find a doctor by specialty</h1>
        <h2>Browse All Specialties</h2>
        <ul>
          <li><a href="/acupuncturists">Acupuncturists</a></li>
          <li><a href="/allergists">Allergists</a></li>
          <li><a href="/audiologists">Audiologists</a></li>
          <li><a href="/cardiologists">Cardiologists</a></li>
          <li><a href="/ct-scan-facilities">CT Scan Facilities</a></li>
          <li><a href="/dentists">Dentists</a></li>
          <li><a href="/dermatologists">Dermatologists</a></li>
          <li><a href="/doctors">Doctors</a></li>
          <li><a href="/mri-facilities">MRI Facilities</a></li>
          <li><a href="/orthopedic-surgeons">Orthopedic Surgeons</a></li>
          <li><a href="/urgent-care">Urgent care</a></li>
          {extra}
        </ul>
      </main>
      <footer><a href="/about-us">About us</a></footer>
    </body></html>
    """


def test_parse_specialty_catalog_is_bounded_and_classified():
    rows = parse_specialty_catalog(_catalog_html())
    by_name = {row["specialty_name"]: row for row in rows}

    assert len(rows) == 11
    assert "Dentists in header" not in by_name
    assert by_name["Orthopedic Surgeons"]["specialty_url"] == "https://www.zocdoc.com/orthopedic-surgeons"
    assert by_name["Orthopedic Surgeons"]["entity_type"] == "provider_specialty"
    assert by_name["CT Scan Facilities"]["entity_type"] == "facility_or_clinic"
    assert by_name["Urgent care"]["entity_type"] == "facility_or_clinic"
    assert classify_specialty_entity("Immediate Care Clinics") == "facility_or_clinic"


def test_refresh_specialties_reconciles_catalog(tmp_path):
    workspace = Workspace(tmp_path).ensure()
    first = workspace.specialty_dir / "first.html"
    first.write_text(_catalog_html(), encoding="utf-8")

    with connect(workspace) as conn:
        frame = refresh_specialties(conn, workspace, first)
        assert len(frame[frame["is_active"]]) == 11

    # Second complete catalog drops Allergists and adds Urologists.
    second_html = _catalog_html('<li><a href="/urologists">Urologists</a></li>').replace(
        '<li><a href="/allergists">Allergists</a></li>',
        "",
    )
    second = workspace.specialty_dir / "second.html"
    second.write_text(second_html, encoding="utf-8")

    with connect(workspace) as conn:
        frame = refresh_specialties(conn, workspace, second)

    statuses = dict(zip(frame["specialty_name"], frame["is_active"], strict=True))
    assert not bool(statuses["Allergists"])
    assert bool(statuses["Urologists"])
    assert (workspace.output_dir / "specialties.csv").exists()
