import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from zocdoc_ortho.db import connect
from zocdoc_ortho.profile_challenges import profile_challenge_reason, repair_profile_challenges
from zocdoc_ortho.saver import SaverRuntime, make_handler
from zocdoc_ortho.workspace import Workspace

CHALLENGE_HTML = """
<html><body>
<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=test"></iframe>
</body></html>
"""

VALID_PROFILE_WITH_DATADOME_SCRIPT = """
<html><body>
<h1 data-test="provider-name">Valid Doctor</h1>
<script src="https://geo.captcha-delivery.com/captcha/script.js"></script>
</body></html>
"""

VALID_PROFILE_WITH_DATADOME_IFRAME = """
<html><body>
<h1 data-test="provider-name">Valid Doctor</h1>
<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=background"></iframe>
</body></html>
"""


def _seed_profile(workspace: Workspace, *, url: str, filename: str, html: str) -> None:
    path = workspace.profile_dir / filename
    path.write_text(html, encoding="utf-8")
    with connect(workspace) as conn:
        conn.execute(
            """
            INSERT INTO specialties (
                specialty_url,specialty_name,specialty_slug,entity_type,
                is_active,first_seen_at,last_seen_at
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(specialty_url) DO NOTHING
            """,
            (
                "https://www.zocdoc.com/radiologists",
                "Radiologists",
                "radiologists",
                "provider_specialty",
                1,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO doctors (
                doctor_url,doctor_name,first_seen_page_url,first_seen_location_url,
                first_seen_state_group,first_seen_location,first_seen_at,last_seen_at,
                first_seen_specialty_url,first_seen_specialty_name
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                url,
                "Example Doctor",
                "https://www.zocdoc.com/radiologists/example-1pm",
                "https://www.zocdoc.com/radiologists/example-1pm",
                "NY",
                "Example",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "https://www.zocdoc.com/radiologists",
                "Radiologists",
            ),
        )
        conn.execute(
            """
            INSERT INTO doctor_profiles (
                doctor_url,doctor_name,status,file,bytes,discovered_at,saved_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                url,
                "Example Doctor",
                "saved",
                filename,
                path.stat().st_size,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()


def test_profile_challenge_reason_detects_iframe_but_not_valid_profile():
    assert profile_challenge_reason(CHALLENGE_HTML) == "DataDome CAPTCHA frame"
    assert profile_challenge_reason('<h1 data-test="provider-name">Valid Doctor</h1>') is None
    assert profile_challenge_reason(VALID_PROFILE_WITH_DATADOME_SCRIPT) is None
    assert profile_challenge_reason(VALID_PROFILE_WITH_DATADOME_IFRAME) is None


def test_js_adblock_wall_is_not_a_saved_profile_or_listing():
    from zocdoc_ortho.listing import classify_listing_capture
    from zocdoc_ortho.profile_challenges import js_adblock_challenge_reason
    for message in ("Please enable JS and disable any ad blocker", "Please enable JavaScript and disable your adblocker",
                    "Please enable <b>JS</b> and disable the ad-blocker"):
        html = f"<html><body><p>{message}</p></body></html>"
        assert profile_challenge_reason(html) == "JavaScript/ad-block verification page"
        assert classify_listing_capture(html, {"doctors": [], "advertised_total": 0})[0] == "restriction_like"
        assert js_adblock_challenge_reason('<h1 data-test="provider-name">Doctor</h1>' + html) is None
        assert js_adblock_challenge_reason('<a data-test="doctor-card-info-name" href="/doctor/test-1">Doctor</a>' + html) is None
        for tag in ("script", "style", "noscript", "template"):
            assert js_adblock_challenge_reason(f"<{tag}>{message}</{tag}>") is None


def test_content_script_ignores_background_datadome_scripts():
    root = Path(__file__).resolve().parents[1]
    content = (root / "src" / "zocdoc_ortho" / "chrome_extension" / "content.js").read_text(
        encoding="utf-8"
    )
    assert 'script[src*="captcha-delivery.com"]' not in content
    assert 'script[src*="datadome"]' not in content
    assert "getBoundingClientRect" in content
    assert "blockingChallengeFrame" in content
    assert "profileContentReady" in content
    assert "profileUnavailableReason" in content
    assert 'reason: "profile_unavailable"' in content


def test_extension_has_idempotent_capture_watchdog():
    root = Path(__file__).resolve().parents[1]
    extension = root / "src" / "zocdoc_ortho" / "chrome_extension"
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    background = (extension / "background.js").read_text(encoding="utf-8")
    content = (extension / "content.js").read_text(encoding="utf-8")

    assert {"alarms", "scripting"}.issubset(manifest["permissions"])
    assert any(
        script.get("matches") == ["https://www.zocdoc.com/doctor/*"]
        and script.get("run_at") == "document_start"
        for script in manifest["content_scripts"]
    )
    assert "ensureCaptureScript" in background
    assert 'files: ["content.js"]' in background
    assert 'type: "FORCE_CAPTURE"' in background
    assert "__zocdocCaptureScriptLoaded" in content
    assert 'message?.type === "PING_CAPTURE_SCRIPT"' in content
    assert 'reason: "background_watchdog"' in content


def test_repair_profile_challenges_quarantines_and_resets(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/doctor/example-doctor-123"
    filename = "example-doctor-123.html"
    _seed_profile(workspace, url=url, filename=filename, html=CHALLENGE_HTML)

    preview = repair_profile_challenges(
        workspace, specialties=("radiologists",), dry_run=True
    )
    assert preview["challenged"] == 1
    assert preview["reset"] == 0
    assert (workspace.profile_dir / filename).exists()

    result = repair_profile_challenges(workspace, specialties=("radiologists",))
    assert result["challenged"] == 1
    assert result["reset"] == 1
    assert not (workspace.profile_dir / filename).exists()
    assert (workspace.rejected_profile_dir / filename).exists()
    with connect(workspace) as conn:
        row = conn.execute(
            "SELECT status,file,bytes,saved_at FROM doctor_profiles WHERE doctor_url=?",
            (url,),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["file"] == ""
    assert row["bytes"] == 0
    assert row["saved_at"] is None


def test_saver_rejects_profile_challenge_before_writing(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    runtime = SaverRuntime(workspace, runner_id="runner-test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_url = f"http://127.0.0.1:{server.server_port}"
    payload = json.dumps(
        {
            "url": "https://www.zocdoc.com/doctor/example-doctor-123",
            "title": "Verify",
            "html": CHALLENGE_HTML,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{server_url}/save",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        try:
            urllib.request.urlopen(request, timeout=3)
            assert False, "challenge save unexpectedly succeeded"
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert workspace.blocked_flag.exists()
    assert not any(workspace.profile_dir.iterdir())
