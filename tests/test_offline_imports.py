import json

import pytest

from zocdoc_ortho import daily_sync, local_profiles
from zocdoc_ortho.cli import build_parser
from zocdoc_ortho.db import connect
from zocdoc_ortho.run_reconciliation import reconcile_interrupted_run
from zocdoc_ortho.urls import safe_slug
from zocdoc_ortho.workspace import Workspace


def seed(workspace, slug, suffix):
    base = f"https://www.zocdoc.com/{slug}/{suffix}"
    doctor = f"https://www.zocdoc.com/doctor/test-{suffix}"
    with connect(workspace) as c:
        c.execute("""INSERT INTO pages(url,base_location_url,specialty_slug,specialty_name,
            location,source,status,discovered_at) VALUES (?,?,?,?,?,'seed','saved','now')""",
            (base, base, slug, slug.title(), suffix))
        c.execute("""INSERT INTO listing_provider_trace(listing_url,doctor_url,doctor_name,
            listing_specialty_slug) VALUES (?,?,?,?)""", (base, doctor, suffix, slug))
        c.commit()
    return doctor


def test_scoped_bootstrap_skips_other_specialties_and_is_idempotent(tmp_path, capsys):
    w = Workspace(tmp_path / "w").ensure()
    seed(w, "cardiologists", "a")
    seed(w, "psychiatrists", "b")
    result = daily_sync.bootstrap_from_trace(w, specialties=("CARDIOLOGISTS",), skip_profiles=True, min_free_gb=0)
    assert result["locations"] == result["memberships"] == result["providers"] == 1
    assert "committed locations=1/1" in capsys.readouterr().out
    daily_sync.bootstrap_from_trace(w, specialties=("cardiologists",), skip_profiles=True, min_free_gb=0)
    with connect(w) as c:
        assert c.execute("SELECT COUNT(*) FROM provider_location_memberships").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM daily_profile_queue").fetchone()[0] == 0


def test_bootstrap_interrupt_keeps_checkpoint_and_resume_skips_committed_location(tmp_path, monkeypatch):
    w = Workspace(tmp_path / "w").ensure()
    seed(w, "cardiologists", "a")
    seed(w, "cardiologists", "b")
    seed(w, "psychiatrists", "c")
    original = daily_sync.shutil.disk_usage
    checks = []

    def interrupt(path):
        checks.append(1)
        if len(checks) == 3:
            raise KeyboardInterrupt()
        return original(path)

    monkeypatch.setattr(daily_sync.shutil, "disk_usage", interrupt)
    with pytest.raises(KeyboardInterrupt):
        daily_sync.bootstrap_from_trace(w, specialties=("cardiologists",), skip_profiles=True, min_free_gb=0)
    with connect(w) as c:
        run = c.execute("SELECT * FROM daily_sync_runs").fetchone()
        assert run["status"] == "interrupted" and run["locations_completed"] == 1
        first_stamp = c.execute("SELECT last_seen_at FROM provider_location_memberships").fetchone()[0]
    monkeypatch.setattr(daily_sync.shutil, "disk_usage", original)
    result = daily_sync.bootstrap_from_trace(w, resume_run_id=run["id"], skip_profiles=True, min_free_gb=0)
    assert result["locations"] == result["memberships"] == result["providers"] == 2
    with connect(w) as c:
        assert c.execute("SELECT COUNT(*) FROM daily_sync_runs").fetchone()[0] == 1
        assert c.execute("SELECT status FROM daily_sync_runs").fetchone()[0] == "complete"
        assert c.execute("SELECT last_seen_at FROM provider_location_memberships WHERE doctor_url LIKE '%test-a'").fetchone()[0] == first_stamp


def prepare_profiles(w):
    for suffix in ("a", "b", "c", "missing", "unavailable"):
        seed(w, "psychiatrists", suffix)
    run = daily_sync.bootstrap_from_trace(w, specialties=("psychiatrists",), skip_profiles=True, min_free_gb=0)["run_id"]
    with connect(w) as c:
        for suffix in ("a", "b", "c", "missing", "unavailable"):
            url = f"https://www.zocdoc.com/doctor/test-{suffix}"
            c.execute("INSERT INTO doctor_profiles(doctor_url,status,discovered_at) VALUES (?,?,?)",
                      (url, "unavailable" if suffix == "unavailable" else "saved", "now"))
            if suffix not in ("missing", "unavailable"):
                (w.profile_dir / safe_slug(url)).write_text('<h1 data-test="provider-name">Test Doctor</h1>')
        c.commit()
    return run


def fake_store(c, *, doctor_url, **kwargs):
    c.execute("UPDATE canonical_provider_records SET profile_sha256='hash',profile_data_json=? WHERE doctor_url=?",
              (json.dumps([{"full_name": "Test Doctor"}]), doctor_url))
    return True, ""


def test_local_import_dry_run_limit_resume_and_preserved_states(tmp_path, monkeypatch):
    w = Workspace(tmp_path / "w").ensure()
    run = prepare_profiles(w)
    monkeypatch.setattr(local_profiles, "_store_profile_html", fake_store)
    result = local_profiles.import_local_profiles(w, run_id=run, specialties=("psychiatrists",), dry_run=True)
    assert result["candidates"] == 3 and result["imported"] == 0
    assert result["missing_html"] == result["unavailable_skipped"] == 1
    result = local_profiles.import_local_profiles(w, run_id=run, limit=1, min_free_gb=0)
    assert result["imported"] == 1
    result = local_profiles.import_local_profiles(w, run_id=run, batch_size=2, min_free_gb=0)
    assert result["imported"] == 2
    result = local_profiles.import_local_profiles(w, run_id=run, min_free_gb=0)
    assert result["selected"] == result["imported"] == 0
    with connect(w) as c:
        assert dict(c.execute("SELECT status,COUNT(*) FROM doctor_profiles GROUP BY status")) == {"saved": 4, "unavailable": 1}
        assert c.execute("SELECT COUNT(*) FROM daily_profile_queue").fetchone()[0] == 0


@pytest.mark.parametrize("batch_size,committed", [(1, 1), (2, 0)])
def test_local_import_rolls_back_only_uncommitted_batch(tmp_path, monkeypatch, batch_size, committed):
    w = Workspace(tmp_path / "w").ensure()
    run = prepare_profiles(w)

    def store(c, **kwargs):
        fake_store(c, **kwargs)
        if kwargs["doctor_url"].endswith("test-b"):
            raise KeyboardInterrupt()
        return True, ""

    monkeypatch.setattr(local_profiles, "_store_profile_html", store)
    with pytest.raises(KeyboardInterrupt):
        local_profiles.import_local_profiles(w, run_id=run, batch_size=batch_size, min_free_gb=0)
    with connect(w) as c:
        assert c.execute("SELECT COUNT(*) FROM canonical_provider_records WHERE profile_sha256='hash'").fetchone()[0] == committed


def test_run_reconciliation_is_dry_by_default_and_never_asserts_completion(tmp_path):
    w = Workspace(tmp_path / "w").ensure()
    seed(w, "psychiatrists", "a")
    run = daily_sync.bootstrap_from_trace(w, skip_profiles=True, min_free_gb=0)["run_id"]
    with connect(w) as c:
        c.execute("UPDATE daily_sync_runs SET status='running',locations_completed=0 WHERE id=?", (run,))
        c.commit()
    result = reconcile_interrupted_run(w, run_id=run)
    assert not result["applied"] and result["locations_completed"] == 1
    with connect(w) as c:
        assert c.execute("SELECT status FROM daily_sync_runs WHERE id=?", (run,)).fetchone()[0] == "running"
    result = reconcile_interrupted_run(w, run_id=run, apply=True)
    assert result["status"] == "interrupted" and result["applied"]
    with connect(w) as c:
        assert c.execute("SELECT COUNT(*) FROM provider_location_memberships").fetchone()[0] == 1


def test_new_cli_options():
    p = build_parser()
    a = p.parse_args(["updater", "bootstrap", "--specialty", "psychiatrists", "--skip-profiles"])
    assert a.specialty == ["psychiatrists"] and a.skip_profiles
    assert p.parse_args(["updater", "import-local-profiles", "--run-id", "16", "--dry-run"]).dry_run
    assert not p.parse_args(["updater", "reconcile-run", "--run-id", "16"]).apply
    assert p.parse_args(["export-delivery", "--specialty", "psychiatrists", "--output", "delivery"]).output.name == "delivery"


def test_finalizer_does_not_resolve_pending_locations(tmp_path):
    w = Workspace(tmp_path / "w").ensure()
    run = daily_sync._start_run(w, [daily_sync.UpdateTarget(
        specialty="Psychiatrists", specialty_slug="psychiatrists", location="Test",
        location_url="https://www.zocdoc.com/psychiatrists/test", verified_provider_count=None,
    )], scope="bootstrap")
    result = daily_sync._finalize_run(w, run, profile_collection_requested=False)
    assert result["status"] == "partial" and not result["collection_resolved"]


def test_resume_refuses_scope_changes_and_superseded_runs(tmp_path):
    w = Workspace(tmp_path / "w").ensure()
    seed(w, "psychiatrists", "a")
    first = daily_sync.bootstrap_from_trace(w, skip_profiles=True, min_free_gb=0)["run_id"]
    with connect(w) as c:
        c.execute("UPDATE daily_sync_runs SET status='interrupted' WHERE id=?", (first,))
        c.commit()
    with pytest.raises(ValueError, match="original run scope"):
        daily_sync.bootstrap_from_trace(w, resume_run_id=first, specialties=("psychiatrists",), min_free_gb=0)
    daily_sync._start_run(w, [], scope="limited")
    with pytest.raises(ValueError, match="newer updater"):
        daily_sync.bootstrap_from_trace(w, resume_run_id=first, min_free_gb=0)


def test_packaged_export_is_read_only_and_gates_incomplete_data(tmp_path):
    from zocdoc_ortho.delivery import main
    w = Workspace(tmp_path / "w").ensure()
    doctor = seed(w, "psychiatrists", "a")
    daily_sync.bootstrap_from_trace(w, skip_profiles=True, min_free_gb=0)
    with connect(w) as c:
        c.execute("INSERT INTO doctor_profiles(doctor_url,status,discovered_at) VALUES (?,'saved','now')", (doctor,))
        payload = json.dumps([{"full_name": "Test Doctor", "provider_id": "test", "state": "MA",
            "postal_code": "01234", "source_zip": "01234", "is_virtual_location": False,
            "locations": [], "hospital_affiliations": []}])
        c.execute("UPDATE canonical_provider_records SET profile_sha256='hash',profile_data_json=?", (payload,))
        c.commit()
    before = w.db.stat().st_mtime_ns
    output = tmp_path / "delivery"
    assert main(["--workspace", str(w.root), "--output", str(output), "--specialty", "psychiatrists"]) == 0
    assert w.db.stat().st_mtime_ns == before
    assert (output / "ready" / "psychiatrists" / "final_providers.csv").exists()
    assert output.with_suffix(".zip").exists()
    with connect(w) as c:
        c.execute("UPDATE doctor_profiles SET status='pending'")
        c.execute("UPDATE canonical_provider_records SET profile_sha256='',profile_data_json='[]'")
        c.commit()
    output = tmp_path / "draft"
    assert main(["--workspace", str(w.root), "--output", str(output), "--specialty", "psychiatrists"]) == 2
    assert (output / "needs-review" / "psychiatrists").exists()
