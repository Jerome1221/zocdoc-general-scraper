from __future__ import annotations

from pathlib import Path

from zocdoc_ortho.performance import PerformanceRecorder
from zocdoc_ortho.workspace import Workspace


def test_chrome_extension_accepts_general_profile_paths_and_claim_state():
    root = Path(__file__).resolve().parents[1]
    content = (root / "src" / "zocdoc_ortho" / "chrome_extension" / "content.js").read_text(encoding="utf-8")
    assert "function isProfilePath" in content
    assert "NON_PROFILE_ROUTES" in content
    assert "claim your profile" in content.lower()
    assert "/-\\d+pm$/i.test(slug)" in content
    # Regression check: old profile recognition required /doctor/ or a trailing numeric ID.
    assert 'path.startsWith("/doctor/") || /^\\/[^/]+\\/[^/]+-\\d+$/.test(path)' not in content


def test_performance_recorder_persists_stage_timings(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    recorder = PerformanceRecorder(
        workspace,
        "collect-profiles",
        {"runner_id": "runner-01", "concurrency": 1},
    )
    recorder.record(
        page_type="profile",
        status="saved",
        url="https://www.zocdoc.com/dentist/benjamin-crunk-dds",
        duration_seconds=2.5,
        timings={
            "navigation_seconds": 0.8,
            "stabilization_seconds": 1.2,
            "serialize_seconds": 0.03,
            "disk_write_seconds": 0.01,
            "profile_mark_db_seconds": 0.02,
        },
    )
    summary = recorder.finish()
    assert summary["avg_navigation_seconds"] == 0.8
    assert summary["avg_stabilization_seconds"] == 1.2
    assert summary["avg_profile_mark_db_seconds"] == 0.02
    assert (workspace.output_dir / "capture_performance.runner-01.csv").exists()
    assert (workspace.output_dir / "crawl_run_metrics.runner-01.csv").exists()
