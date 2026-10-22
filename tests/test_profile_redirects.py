import csv
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from zocdoc_ortho.db import connect
from zocdoc_ortho.profile_redirects import repair_profile_redirects
from zocdoc_ortho.saver import MANIFEST_FIELDS, SaverRuntime, make_handler
from zocdoc_ortho.trace import find_profile_html
from zocdoc_ortho.urls import safe_slug
from zocdoc_ortho.workspace import Workspace

REQUESTED_URL = "https://www.zocdoc.com/doctor/mark-sonnenschine-do"
CANONICAL_URL = "https://www.zocdoc.com/doctor/mark-sonnenschine-do-860475"


@pytest.mark.parametrize("final_url,html", [
    ("https://www.zocdoc.com/404", "<html><body>Page unavailable</body></html>"),
    ("https://www.zocdoc.com/", "<html><body>Not Found. Redirecting to /</body></html>"),
])
def test_saver_accepts_confirmed_unavailable_redirect(tmp_path, final_url, html):
    from zocdoc_ortho.profile_availability import profile_unavailable_reason

    workspace = Workspace(tmp_path / "workspace").ensure()
    runtime = SaverRuntime(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/save",
        data=json.dumps({"url": final_url, "requested_url": REQUESTED_URL, "html": html}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.loads(response.read())["ok"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert profile_unavailable_reason((workspace.profile_dir / safe_slug(REQUESTED_URL)).read_text())


def _valid_profile_html() -> str:
    return (
        '<html><head><link rel="canonical" href="'
        + CANONICAL_URL
        + '"></head><body><h1 data-test="provider-name">Mark Sonnenschine, DO</h1></body></html>'
    )


def test_profile_lookup_reuses_newest_slug_with_same_provider_id(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    requested = "https://www.zocdoc.com/doctor/neil-patel-dnp-927540"
    old_url = "https://www.zocdoc.com/doctor/neil-patel-927540"
    current_url = "https://www.zocdoc.com/doctor/neil-patel-dnp-fnp-c-927540"
    old_path = workspace.profile_dir / safe_slug(old_url)
    current_path = workspace.profile_dir / safe_slug(current_url)
    old_path.write_text("old profile", encoding="utf-8")
    current_path.write_text("current profile", encoding="utf-8")
    os.utime(old_path, (100, 100))
    os.utime(current_path, (200, 200))

    assert find_profile_html(workspace, requested) == current_path


def test_saver_keeps_requested_filename_and_records_profile_redirect(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    runtime = SaverRuntime(workspace, runner_id="runner-test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = json.dumps(
        {
            "url": CANONICAL_URL,
            "requested_url": REQUESTED_URL,
            "title": "Mark Sonnenschine, DO",
            "html": _valid_profile_html(),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/save",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            saved = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert saved["requested_url"] == REQUESTED_URL
    assert saved["final_url"] == CANONICAL_URL
    assert saved["file"] == safe_slug(REQUESTED_URL)
    assert (workspace.profile_dir / safe_slug(REQUESTED_URL)).exists()
    assert not (workspace.profile_dir / safe_slug(CANONICAL_URL)).exists()
    with workspace.profile_redirects_csv.open(encoding="utf-8-sig", newline="") as handle:
        redirects = list(csv.DictReader(handle))
    assert redirects == [
        {
            "event_at": redirects[0]["event_at"],
            "requested_url": REQUESTED_URL,
            "canonical_url": CANONICAL_URL,
            "file": safe_slug(REQUESTED_URL),
            "status": "saved",
        }
    ]


def test_repair_profile_redirects_reuses_existing_canonical_capture(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    filename = safe_slug(CANONICAL_URL)
    (workspace.profile_dir / filename).write_text(_valid_profile_html(), encoding="utf-8")
    with (workspace.root / "manifest.runner-01.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "event_at": "2026-10-04T05:21:00Z",
                "status": "saved",
                "page_type": "profile",
                "url": CANONICAL_URL,
                "title": "Mark Sonnenschine, DO",
                "file": filename,
                "bytes": "100",
                "error": "",
            }
        )

    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO specialties (
                specialty_url,specialty_name,specialty_slug,entity_type,
                is_active,first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                "https://www.zocdoc.com/oncologists",
                "Oncologists",
                "oncologists",
                "provider_specialty",
                1,
                "2026-10-04T00:00:00Z",
                "2026-10-04T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO doctors (
                doctor_url,doctor_name,first_seen_page_url,first_seen_location_url,
                first_seen_at,last_seen_at,first_seen_specialty_url,first_seen_specialty_name
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                REQUESTED_URL,
                "Mark Sonnenschine, DO",
                "https://www.zocdoc.com/oncologists/example-1pm",
                "https://www.zocdoc.com/oncologists/example-1pm",
                "2026-10-04T00:00:00Z",
                "2026-10-04T00:00:00Z",
                "https://www.zocdoc.com/oncologists",
                "Oncologists",
            ),
        )
        conn.execute(
            """
            INSERT INTO doctor_profiles (
                doctor_url,doctor_name,status,discovered_at,error,last_error
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                REQUESTED_URL,
                "Mark Sonnenschine, DO",
                "timeout",
                "2026-10-04T00:00:00Z",
                "No saved HTML after 60s",
                "No saved HTML after 60s",
            ),
        )
        conn.commit()

    preview = repair_profile_redirects(
        workspace, specialties=("oncologists",), dry_run=True
    )
    assert preview["matched"] == 1
    assert preview["repaired"] == 0

    result = repair_profile_redirects(workspace, specialties=("oncologists",))
    assert result["matched"] == 1
    assert result["repaired"] == 1
    with connect(workspace) as conn:
        row = conn.execute(
            "SELECT status,file,error,last_error FROM doctor_profiles WHERE doctor_url=?",
            (REQUESTED_URL,),
        ).fetchone()
    assert row["status"] == "saved"
    assert row["file"] == filename
    assert row["error"] == ""
    assert row["last_error"] == ""
