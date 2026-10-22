from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from zocdoc_ortho import runners
from zocdoc_ortho.cli import build_parser
from zocdoc_ortho.db import connect
from zocdoc_ortho.workspace import Workspace


@pytest.mark.parametrize("specialty", ["dentists", " Dentists ", "https://example.test/dentists"])
def test_profile_monitor_caches_membership_but_reads_fresh_status(tmp_path, monkeypatch, specialty):
    workspace = Workspace(tmp_path / "workspace").ensure()
    with connect(workspace) as conn:
        conn.executemany(
            "INSERT INTO doctor_profiles(doctor_url,status,discovered_at) VALUES (?,?,?)",
            [("shared", "pending", "now"), ("other", "saved", "now")],
        )
        conn.executemany(
            "INSERT INTO listing_provider_trace(doctor_url,listing_specialty_slug,"
            "listing_specialty_name,listing_specialty_url) VALUES (?,?,?,?)",
            [("shared", "dentists", "Dentists", "https://example.test/dentists")] * 2
            + [("other", "cardiologists", "Cardiologists", "https://example.test/cardio")],
        )
        conn.commit()

    cohort = runners._profile_progress_cohort(workspace, specialties=(specialty,))
    assert cohort == frozenset({"shared"})
    assert runners._profile_progress_snapshot(
        workspace, specialties=(specialty,), provider_urls=cohort,
    ) == {"pending": 1}
    with connect(workspace) as conn:
        conn.execute("UPDATE doctor_profiles SET status='saved' WHERE doctor_url='shared'")
        conn.commit()

    def unexpected(*args, **kwargs):
        raise AssertionError("Heartbeat must not resolve membership or initialize the DB")

    monkeypatch.setattr(runners, "connect", unexpected)
    monkeypatch.setattr(runners, "_profile_progress_cohort", unexpected)
    assert runners._profile_progress_snapshot(
        workspace, specialties=(specialty,), provider_urls=cohort,
    ) == {"saved": 1}


def test_profile_monitor_all_and_empty_specialty_filters(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    with connect(workspace) as conn:
        conn.execute(
            "INSERT INTO doctor_profiles(doctor_url,status,discovered_at) VALUES ('test','saved','now')"
        )
        conn.commit()
    assert runners._profile_progress_snapshot(workspace, specialties=()) == {"saved": 1}
    assert runners._profile_progress_snapshot(workspace, specialties=(" ",)) == {}
    assert runners._profile_progress_snapshot(workspace, specialties=("unknown",)) == {}


def test_multi_profiles_launches_unbuffered_workers_and_reuses_cohort(tmp_path, monkeypatch, capsys):
    workspace = Workspace(tmp_path / "workspace").ensure()
    with connect(workspace):
        pass
    logs = workspace.root / ".runners" / "logs"
    logs.mkdir(parents=True)
    runner = {"runner_id": "runner-01", "controller_url": "http://localhost:8765/controller"}
    monkeypatch.setattr(runners, "load_runner_config", lambda w: {})
    monkeypatch.setattr(runners, "runner_status", lambda w: {})
    monkeypatch.setattr(runners, "_select_runners", lambda *a: [runner])
    calls = []

    def cohort(*args, **kwargs):
        assert "preparing specialty membership" in capsys.readouterr().out
        calls.append(1)
        return frozenset({"shared"})

    monkeypatch.setattr(runners, "_profile_progress_cohort", cohort)
    commands = []
    monkeypatch.setattr(runners.subprocess, "Popen", lambda command, **kw: commands.append(command) or object())

    def wait(processes, **kwargs):
        assert kwargs["snapshot"]() == {}
        assert kwargs["snapshot"]() == {}
        return [{"returncode": 0}]

    monkeypatch.setattr(runners, "_wait_for_runner_processes", wait)
    monkeypatch.setattr(runners, "export_profile_queue", lambda *a: None)
    monkeypatch.setattr(runners, "latest_runner_metrics", lambda *a, **kw: [])
    monkeypatch.setattr(runners, "combined_performance_report", lambda *a, **kw: {})
    runners.run_multi_profiles(workspace, limit=1, specialties=("dentists",))
    assert calls == [1]
    assert commands[0][:4] == [sys.executable, "-u", "-m", "zocdoc_ortho"]


def test_listing_progress_snapshot_is_scoped_to_phase_and_specialty(tmp_path: Path):
    workspace = Workspace.from_value(tmp_path / "workspace").ensure()
    with connect(workspace) as conn:
        conn.executemany(
            """
            INSERT INTO pages (
                url,base_location_url,source,status,discovered_at,
                specialty_url,specialty_name,specialty_slug
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    "https://example.test/gastro/1",
                    "https://example.test/gastro",
                    "pagination",
                    "saved",
                    "2026-01-01T00:00:00Z",
                    "https://example.test/specialty/gastroenterologists",
                    "Gastroenterologists",
                    "gastroenterologists",
                ),
                (
                    "https://example.test/gastro/2",
                    "https://example.test/gastro",
                    "pagination",
                    "pending",
                    "2026-01-01T00:00:00Z",
                    "https://example.test/specialty/gastroenterologists",
                    "Gastroenterologists",
                    "gastroenterologists",
                ),
                (
                    "https://example.test/cardio/1",
                    "https://example.test/cardio",
                    "pagination",
                    "saved",
                    "2026-01-01T00:00:00Z",
                    "https://example.test/specialty/cardiologists",
                    "Cardiologists",
                    "cardiologists",
                ),
                (
                    "https://example.test/gastro",
                    "https://example.test/gastro",
                    "seed",
                    "saved",
                    "2026-01-01T00:00:00Z",
                    "https://example.test/specialty/gastroenterologists",
                    "Gastroenterologists",
                    "gastroenterologists",
                ),
            ],
        )
        conn.commit()

    assert runners._listing_progress_snapshot(
        workspace,
        phase="pagination",
        specialties=("gastroenterologists",),
    ) == {"pending": 1, "saved": 1}


def test_live_progress_prints_monitoring_fields(capsys):
    runners._print_live_progress(
        kind="listings",
        scope="phase=pagination specialty=gastroenterologists",
        counts={"saved": 25, "pending": 70, "opening": 5, "timeout": 1},
        baseline={"saved": 20, "pending": 81},
        elapsed_seconds=30,
        active_runners=3,
        runner_count=3,
    )

    output = capsys.readouterr().out
    assert "LIVE LISTINGS" in output
    assert "this-run=6" in output
    assert "rate=12.00/min" in output
    assert "saved=25" in output
    assert "pending=70" in output
    assert "opening=5" in output


def test_multi_runner_commands_accept_progress_interval():
    parser = build_parser()
    listing_args = parser.parse_args(["multi-listings", "--progress-interval", "5"])
    profile_args = parser.parse_args(["multi-profiles", "--progress-interval", "7.5"])

    assert listing_args.progress_interval == 5
    assert profile_args.progress_interval == 7.5


def test_runner_wait_loop_prints_heartbeat_and_exit(tmp_path: Path, capsys):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.08)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()

    results = runners._wait_for_runner_processes(
        [({"runner_id": "runner-01"}, process, tmp_path / "runner.log")],
        kind="listings",
        scope="phase=pagination specialty=test",
        started=started,
        progress_interval_seconds=0.01,
        baseline={"pending": 2},
        snapshot=lambda: {"saved": 1, "pending": 1},
    )

    output = capsys.readouterr().out
    assert results[0]["returncode"] == 0
    assert "LIVE MONITOR" in output
    assert "LIVE LISTINGS" in output
    assert "RUNNER EXIT | runner-01 | code=0" in output
