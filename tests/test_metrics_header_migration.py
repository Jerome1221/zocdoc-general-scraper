from __future__ import annotations

import csv
from pathlib import Path

from zocdoc_ortho.performance import PerformanceRecorder, RUN_FIELDS
from zocdoc_ortho.runners import combined_performance_report
from zocdoc_ortho.workspace import Workspace


def test_legacy_runner_metrics_header_is_upgraded_and_timings_are_visible(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    path = workspace.output_dir / "crawl_run_metrics.runner-01.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    legacy_fields = [field for field in RUN_FIELDS if not field.startswith("avg_")]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=legacy_fields)
        writer.writeheader()
        writer.writerow({
            "run_id": "legacy",
            "runner_id": "runner-01",
            "command": "collect-profiles",
            "processed": 1,
            "saved": 1,
            "pages_per_minute": 10,
        })

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
            "server_save_seconds": 0.08,
            "profile_mark_db_seconds": 0.02,
        },
    )
    recorder.finish()

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == RUN_FIELDS
        rows = list(reader)
    assert len(rows) == 2
    assert rows[-1]["avg_navigation_seconds"] == "0.8"
    assert rows[-1]["avg_server_save_seconds"] == "0.08"

    report = combined_performance_report(workspace, command="collect-profiles")
    assert report["avg_stage_timings"]["navigation_seconds"] == 0.8
    assert report["avg_stage_timings"]["server_save_seconds"] == 0.08
