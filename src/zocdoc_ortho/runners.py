from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import connect
from .outputs import export_page_outputs, export_profile_queue
from .performance import STAGE_TIMING_FIELDS
from .saver import get_health
from .trace import sync_profiles_from_doctors
from .workspace import Workspace

RUNNER_DIR = ".runners"
CONFIG_NAME = "config.json"
STATE_NAME = "state.json"
RUNNER_LAUNCH_VERSION = 3


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _runner_root(workspace: Workspace) -> Path:
    return workspace.root / RUNNER_DIR


def _config_path(workspace: Workspace) -> Path:
    return _runner_root(workspace) / CONFIG_NAME


def _state_path(workspace: Workspace) -> Path:
    return _runner_root(workspace) / STATE_NAME


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def extension_path() -> Path:
    return Path(__file__).resolve().parent / "chrome_extension"


def find_chrome(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.exists():
            return path
        raise FileNotFoundError(f"Chrome executable not found: {path}")

    candidates: list[Path] = []
    if os.name == "nt":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
        candidates.extend(
            [
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
                Path.home() / "AppData/Local/Chromium/Application/chrome.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]
        )
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil_which(name)
            if found:
                candidates.append(Path(found))

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("Google Chrome/Chromium was not found. Pass --chrome-path explicitly.")


def shutil_which(name: str) -> str | None:
    # Local tiny replacement keeps this module dependency-free and easy to mock.
    paths = os.environ.get("PATH", "").split(os.pathsep)
    extensions = [""]
    if os.name == "nt":
        extensions += os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(";")
    for folder in paths:
        for extension in extensions:
            candidate = Path(folder) / f"{name}{extension}"
            if candidate.is_file():
                return str(candidate)
    return None


def init_runners(
    workspace: Workspace,
    *,
    count: int = 2,
    base_port: int = 8765,
    chrome_path: str | Path | None = None,
) -> dict:
    if count < 1:
        raise ValueError("runner count must be >= 1")
    if not 1024 <= int(base_port) <= 65535:
        raise ValueError("base port must be between 1024 and 65535")
    if int(base_port) + count - 1 > 65535:
        raise ValueError("runner ports would exceed 65535")

    workspace.ensure()
    root = _runner_root(workspace)
    profiles = root / "chrome_profiles"
    logs = root / "logs"
    profiles.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    resolved_chrome = str(find_chrome(chrome_path)) if chrome_path else ""
    runners = []
    for index in range(count):
        runner_id = f"runner-{index + 1:02d}"
        port = int(base_port) + index
        profile = profiles / runner_id
        profile.mkdir(parents=True, exist_ok=True)
        runners.append(
            {
                "runner_id": runner_id,
                "host": "127.0.0.1",
                "port": port,
                "controller_url": f"http://127.0.0.1:{port}",
                "profile_dir": str(profile.resolve()),
            }
        )

    config = {
        "version": 1,
        "created_at": _now_iso(),
        "workspace": str(workspace.root),
        "count": count,
        "base_port": int(base_port),
        "chrome_path": resolved_chrome,
        "runners": runners,
    }
    _write_json(_config_path(workspace), config)
    return config


def load_runner_config(workspace: Workspace) -> dict:
    config = _load_json(_config_path(workspace), None)
    if not config:
        raise FileNotFoundError(
            f"Runner config not found: {_config_path(workspace)}. Run 'runners init' first."
        )
    return config


def _popen_background(
    command: list[str],
    log_path: Path,
    *,
    detach_on_windows: bool = True,
) -> subprocess.Popen:
    """Start a managed background process.

    Saver processes can be fully detached. Chrome is intentionally *not* started
    with DETACHED_PROCESS on Windows so its GUI window remains visible/reliable.
    Both still receive their own process group.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab", buffering=0)
    kwargs: dict[str, Any] = {
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if detach_on_windows:
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    handle.close()
    return process


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, SystemError, ValueError):
        return False


def _process_command_line(pid: int | None) -> str | None:
    """Best-effort command-line inspection used to prevent cross-workspace kills."""
    if not _pid_alive(pid):
        return None
    pid = int(pid)
    try:
        if os.name == "nt":
            ps = (
                f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; '
                'if ($null -ne $p -and $null -ne $p.CommandLine) { [Console]::Out.Write($p.CommandLine) }'
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            value = (result.stdout or "").strip()
            return value or None

        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            raw = proc_cmdline.read_bytes().replace(b"\x00", b" ")
            value = raw.decode("utf-8", errors="replace").strip()
            if value:
                return value

        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        value = (result.stdout or "").strip()
        return value or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _normalize_process_text(value: str) -> str:
    return value.replace("\\", "/").replace('"', "").strip().lower()


def _ownership_check(pid: int | None, expected_tokens: tuple[str, ...]) -> dict:
    if not _pid_alive(pid):
        return {"pid": pid, "owned": False, "status": "not_running", "missing_tokens": []}
    command_line = _process_command_line(pid)
    if not command_line:
        return {
            "pid": int(pid),
            "owned": False,
            "status": "unverified",
            "missing_tokens": list(expected_tokens),
        }
    normalized = _normalize_process_text(command_line)
    missing = [token for token in expected_tokens if _normalize_process_text(str(token)) not in normalized]
    return {
        "pid": int(pid),
        "owned": not missing,
        "status": "owned" if not missing else "foreign",
        "missing_tokens": missing,
    }


def _saver_identity_tokens(workspace: Workspace, runner: dict) -> tuple[str, ...]:
    return (
        str(workspace.root),
        "zocdoc_ortho",
        "saver",
        "--runner-id",
        str(runner.get("runner_id") or ""),
        "--port",
        str(runner.get("port") or ""),
    )


def _chrome_identity_tokens(runner: dict) -> tuple[str, ...]:
    return (
        f"--user-data-dir={runner.get('profile_dir') or ''}",
        f"runner_id={runner.get('runner_id') or ''}",
        f"expected_port={runner.get('port') or ''}",
    )


def _terminate_pid_unchecked(pid: int | None) -> None:
    if not _pid_alive(pid):
        return
    pid = int(pid)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _terminate_owned_pid(
    pid: int | None,
    *,
    expected_tokens: tuple[str, ...],
    label: str,
) -> dict:
    check = _ownership_check(pid, expected_tokens)
    if check["status"] == "not_running":
        return {**check, "label": label, "action": "not_running"}
    if not check["owned"]:
        # Safety rule: never kill a live PID merely because an old state file names it.
        # Windows can reuse PIDs, so a stale Dev9 PID may now belong to Dev8 or another app.
        return {**check, "label": label, "action": "skipped"}
    _terminate_pid_unchecked(pid)
    return {**check, "label": label, "action": "terminated"}


def start_runners(
    workspace: Workspace,
    *,
    chrome_path: str | Path | None = None,
    startup_wait_seconds: float = 2.0,
    force_restart: bool = False,
) -> dict:
    config = load_runner_config(workspace)
    resolved_chrome = find_chrome(chrome_path or config.get("chrome_path") or None)
    ext = extension_path().resolve()
    logs_dir = _runner_root(workspace) / "logs"
    state = _load_json(_state_path(workspace), {"runners": []})
    old_by_id = {row.get("runner_id"): row for row in state.get("runners", [])}
    state_rows = []
    termination_events: list[dict] = []

    for runner in config["runners"]:
        runner_id = runner["runner_id"]
        old = old_by_id.get(runner_id, {})
        old_matches = (
            int(old.get("launch_version") or 0) == RUNNER_LAUNCH_VERSION
            and int(old.get("port") or -1) == int(runner["port"])
            and str(old.get("host") or "") == str(runner["host"])
            and str(old.get("profile_dir") or "") == str(runner["profile_dir"])
            and str(old.get("controller_url") or "") == str(runner["controller_url"])
        )
        if force_restart or not old_matches:
            if old:
                termination_events.append(
                    _terminate_owned_pid(
                        old.get("saver_pid"),
                        expected_tokens=_saver_identity_tokens(workspace, old or runner),
                        label=f"{runner_id}:saver",
                    )
                )
                termination_events.append(
                    _terminate_owned_pid(
                        old.get("chrome_pid"),
                        expected_tokens=_chrome_identity_tokens(old or runner),
                        label=f"{runner_id}:chrome",
                    )
                )
            old = {}

        saver_pid = old.get("saver_pid") if _pid_alive(old.get("saver_pid")) else None
        chrome_pid = old.get("chrome_pid") if _pid_alive(old.get("chrome_pid")) else None

        if saver_pid is None:
            saver_cmd = [
                sys.executable,
                "-m",
                "zocdoc_ortho",
                "--workspace",
                str(workspace.root),
                "saver",
                "--host",
                runner["host"],
                "--port",
                str(runner["port"]),
                "--runner-id",
                runner_id,
            ]
            saver_process = _popen_background(
                saver_cmd,
                logs_dir / f"{runner_id}.saver.log",
                detach_on_windows=True,
            )
            saver_pid = saver_process.pid

        controller_page_url = (
            f"http://{runner['host']}:{int(runner['port'])}/controller"
            f"?runner_id={runner_id}&expected_port={int(runner['port'])}"
        )
        if chrome_pid is None:
            chrome_cmd = [
                str(resolved_chrome),
                f"--user-data-dir={runner['profile_dir']}",
                f"--load-extension={ext}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                controller_page_url,
            ]
            chrome_process = _popen_background(
                chrome_cmd,
                logs_dir / f"{runner_id}.chrome.log",
                detach_on_windows=False,
            )
            chrome_pid = chrome_process.pid

        state_rows.append(
            {
                **runner,
                "controller_page_url": controller_page_url,
                "launch_version": RUNNER_LAUNCH_VERSION,
                "saver_pid": saver_pid,
                "chrome_pid": chrome_pid,
                "chrome_path": str(resolved_chrome),
                "saver_log": str((logs_dir / f"{runner_id}.saver.log").resolve()),
                "chrome_log": str((logs_dir / f"{runner_id}.chrome.log").resolve()),
                "started_at": old.get("started_at") or _now_iso(),
            }
        )

    state = {
        "version": 1,
        "workspace": str(workspace.root),
        "chrome_path": str(resolved_chrome),
        "updated_at": _now_iso(),
        "runners": state_rows,
    }
    _write_json(_state_path(workspace), state)
    if startup_wait_seconds > 0:
        time.sleep(startup_wait_seconds)
    report = runner_status(workspace)
    report["termination_events"] = termination_events
    report["safety_warnings"] = [
        event for event in termination_events if event.get("action") == "skipped"
    ]
    return report


def runner_status(workspace: Workspace) -> dict:
    config = load_runner_config(workspace)
    state = _load_json(_state_path(workspace), {"runners": []})
    state_by_id = {row.get("runner_id"): row for row in state.get("runners", [])}
    rows = []
    for runner in config["runners"]:
        saved = state_by_id.get(runner["runner_id"], {})
        health = get_health(server_url=runner["controller_url"], timeout=0.75)
        rows.append(
            {
                **runner,
                "saver_pid": saved.get("saver_pid"),
                "chrome_pid": saved.get("chrome_pid"),
                "saver_process_alive": _pid_alive(saved.get("saver_pid")),
                "chrome_process_alive": _pid_alive(saved.get("chrome_pid")),
                "server_ok": bool(health.get("ok")),
                "controller_active": bool(health.get("controller_active")),
                "queued_open_requests": int(health.get("queued_open_requests") or 0),
                "saver_log": saved.get("saver_log") or str((_runner_root(workspace) / "logs" / f"{runner['runner_id']}.saver.log").resolve()),
                "chrome_log": saved.get("chrome_log") or str((_runner_root(workspace) / "logs" / f"{runner['runner_id']}.chrome.log").resolve()),
                "chrome_path": saved.get("chrome_path") or state.get("chrome_path"),
            }
        )
    return {"workspace": str(workspace.root), "runners": rows}


def stop_runners(workspace: Workspace, *, close_browsers: bool = True) -> dict:
    state = _load_json(_state_path(workspace), {"runners": []})
    events: list[dict] = []
    for row in state.get("runners", []):
        events.append(
            _terminate_owned_pid(
                row.get("saver_pid"),
                expected_tokens=_saver_identity_tokens(workspace, row),
                label=f"{row.get('runner_id')}:saver",
            )
        )
        if close_browsers:
            events.append(
                _terminate_owned_pid(
                    row.get("chrome_pid"),
                    expected_tokens=_chrome_identity_tokens(row),
                    label=f"{row.get('runner_id')}:chrome",
                )
            )

    skipped = [event for event in events if event.get("action") == "skipped"]
    # Keep state when a live PID could not be proven to belong to this workspace.
    # This gives the operator a chance to inspect it rather than silently forgetting it.
    if _state_path(workspace).exists() and not skipped:
        _state_path(workspace).unlink()
    return {
        "configured": len(state.get("runners", [])),
        "terminated_processes": sum(event.get("action") == "terminated" for event in events),
        "close_browsers": close_browsers,
        "events": events,
        "safety_warnings": skipped,
    }


def _split_limit(total: int, count: int) -> list[int]:
    if total < 1:
        return [0] * count
    base, remainder = divmod(total, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]



def _select_runners(config: dict, status: dict, runner_ids: tuple[str, ...] = ()) -> list[dict]:
    wanted = {value.strip() for value in runner_ids if value.strip()}
    configured = {row["runner_id"]: row for row in config["runners"]}
    if wanted:
        unknown = sorted(wanted - set(configured))
        if unknown:
            raise ValueError("Unknown runner id(s): " + ", ".join(unknown))
        selected = [row for row in config["runners"] if row["runner_id"] in wanted]
    else:
        selected = list(config["runners"])

    status_by_id = {row["runner_id"]: row for row in status["runners"]}
    unavailable = [
        row["runner_id"]
        for row in selected
        if not status_by_id[row["runner_id"]]["server_ok"]
        or not status_by_id[row["runner_id"]]["controller_active"]
    ]
    if unavailable:
        raise RuntimeError(
            "Runner(s) are not ready: " + ", ".join(unavailable)
            + ". Run 'runners start' and check 'runners status'."
        )
    return selected


def run_multi_listings(
    workspace: Workspace,
    *,
    limit: int,
    concurrency_per_runner: int = 4,
    timeout_seconds: int = 60,
    stagger_seconds: float = 0.1,
    poll_seconds: float = 0.2,
    page_check_ms: int = 250,
    stable_checks: int = 2,
    page_max_wait_ms: int = 8000,
    controller_poll_ms: int = 200,
    phase: str = "auto",
    zero_link_max_retries: int = 2,
    keep_listing_html: bool = False,
    specialties: tuple[str, ...] = (),
    runner_ids: tuple[str, ...] = (),
) -> dict:
    config = load_runner_config(workspace)
    status = runner_status(workspace)
    selected_runners = _select_runners(config, status, runner_ids)

    allocations = _split_limit(limit, len(selected_runners))
    logs_dir = _runner_root(workspace) / "logs"
    processes: list[tuple[dict, subprocess.Popen, Path]] = []
    started = time.monotonic()

    for runner, runner_limit in zip(selected_runners, allocations, strict=True):
        if runner_limit <= 0:
            continue
        log_path = logs_dir / f"{runner['runner_id']}.collect-listings.log"
        handle = log_path.open("wb")
        command = [
            sys.executable,
            "-m",
            "zocdoc_ortho",
            "--workspace",
            str(workspace.root),
            "collect-listings",
            "--limit",
            str(runner_limit),
            "--concurrency",
            str(concurrency_per_runner),
            "--timeout",
            str(timeout_seconds),
            "--stagger",
            str(stagger_seconds),
            "--poll",
            str(poll_seconds),
            "--page-check-ms",
            str(page_check_ms),
            "--stable-checks",
            str(stable_checks),
            "--page-max-wait-ms",
            str(page_max_wait_ms),
            "--controller-poll-ms",
            str(controller_poll_ms),
            "--phase",
            phase,
            "--zero-link-max-retries",
            str(zero_link_max_retries),
        ]
        for specialty in specialties:
            command.extend(["--specialty", specialty])
        if keep_listing_html:
            command.append("--keep-listing-html")
        command.extend([
            "--controller-url",
            runner["controller_url"],
            "--runner-id",
            runner["runner_id"],
        ])
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
        handle.close()
        processes.append((runner, process, log_path))

    results = []
    for runner, process, log_path in processes:
        returncode = process.wait()
        results.append(
            {
                "runner_id": runner["runner_id"],
                "returncode": returncode,
                "log": str(log_path),
            }
        )

    with connect(workspace) as conn:
        sync_profiles_from_doctors(conn, workspace, write_output=False)
        export_page_outputs(conn, workspace)
        export_profile_queue(conn, workspace)

    elapsed = max(time.monotonic() - started, 0.001)
    processed = 0
    for row in latest_runner_metrics(workspace, command="collect-listings"):
        processed += int(float(row.get("processed") or 0))

    failures = [row for row in results if row["returncode"] != 0]
    return {
        "elapsed_seconds": round(elapsed, 3),
        "requested_limit": limit,
        "runner_count": len(processes),
        "results": results,
        "failures": failures,
        "combined": combined_performance_report(workspace, command="collect-listings"),
        "latest_processed_sum": processed,
    }



def run_multi_profiles(
    workspace: Workspace,
    *,
    limit: int,
    concurrency_per_runner: int = 3,
    timeout_seconds: int = 60,
    stagger_seconds: float = 0.25,
    poll_seconds: float = 0.25,
    page_check_ms: int = 500,
    stable_checks: int = 3,
    page_max_wait_ms: int = 12000,
    controller_poll_ms: int = 200,
    stale_claim_minutes: float = 10.0,
    specialties: tuple[str, ...] = (),
    runner_ids: tuple[str, ...] = (),
) -> dict:
    """Run profile collectors concurrently across selected managed runners.

    The profile queue itself performs atomic SQLite claims, so each doctor URL can
    be owned by only one collector at a time.
    """
    config = load_runner_config(workspace)
    status = runner_status(workspace)
    selected_runners = _select_runners(config, status, runner_ids)
    allocations = _split_limit(limit, len(selected_runners))
    logs_dir = _runner_root(workspace) / "logs"
    processes: list[tuple[dict, subprocess.Popen, Path]] = []
    started = time.monotonic()

    for runner, runner_limit in zip(selected_runners, allocations, strict=True):
        if runner_limit <= 0:
            continue
        log_path = logs_dir / f"{runner['runner_id']}.collect-profiles.log"
        handle = log_path.open("wb")
        command = [
            sys.executable,
            "-m",
            "zocdoc_ortho",
            "--workspace",
            str(workspace.root),
            "collect-profiles",
            "--limit",
            str(runner_limit),
            "--concurrency",
            str(concurrency_per_runner),
            "--timeout",
            str(timeout_seconds),
            "--stagger",
            str(stagger_seconds),
            "--poll",
            str(poll_seconds),
            "--page-check-ms",
            str(page_check_ms),
            "--stable-checks",
            str(stable_checks),
            "--page-max-wait-ms",
            str(page_max_wait_ms),
            "--controller-poll-ms",
            str(controller_poll_ms),
            "--stale-claim-minutes",
            str(stale_claim_minutes),
        ]
        for specialty in specialties:
            command.extend(["--specialty", specialty])
        command.extend([
            "--controller-url",
            runner["controller_url"],
            "--runner-id",
            runner["runner_id"],
        ])
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
        handle.close()
        processes.append((runner, process, log_path))

    results = []
    for runner, process, log_path in processes:
        returncode = process.wait()
        results.append(
            {
                "runner_id": runner["runner_id"],
                "returncode": returncode,
                "log": str(log_path),
            }
        )

    with connect(workspace) as conn:
        export_profile_queue(conn, workspace)

    elapsed = max(time.monotonic() - started, 0.001)
    processed = 0
    for row in latest_runner_metrics(workspace, command="collect-profiles"):
        processed += int(float(row.get("processed") or 0))
    failures = [row for row in results if row["returncode"] != 0]
    return {
        "elapsed_seconds": round(elapsed, 3),
        "requested_limit": limit,
        "runner_count": len(processes),
        "results": results,
        "failures": failures,
        "combined": combined_performance_report(workspace, command="collect-profiles"),
        "latest_processed_sum": processed,
    }


def latest_runner_metrics(workspace: Workspace, *, command: str = "collect-listings") -> list[dict]:
    rows = []
    for path in sorted(workspace.output_dir.glob("crawl_run_metrics.runner-*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            matches = [row for row in csv.DictReader(handle) if row.get("command") == command]
        if matches:
            rows.append(matches[-1])
    return rows


def combined_performance_report(workspace: Workspace, *, command: str = "collect-listings") -> dict:
    rows = latest_runner_metrics(workspace, command=command)
    combined_ppm = sum(float(row.get("pages_per_minute") or 0.0) for row in rows)
    processed = sum(int(float(row.get("processed") or 0)) for row in rows)
    with connect(workspace) as conn:
        if command == "collect-listings":
            pending = conn.execute("SELECT COUNT(*) n FROM pages WHERE status IN ('pending','retry_pending')").fetchone()["n"]
        elif command == "collect-profiles":
            pending = conn.execute("SELECT COUNT(*) n FROM doctor_profiles WHERE status='pending'").fetchone()["n"]
        else:
            pending = 0
    eta_minutes = pending / combined_ppm if combined_ppm > 0 else None
    avg_stage_timings: dict[str, float] = {}
    for field in STAGE_TIMING_FIELDS:
        weighted_total = 0.0
        weight_total = 0
        key = f"avg_{field}"
        for row in rows:
            raw = row.get(key)
            if raw in {None, ""}:
                continue
            try:
                value = float(raw)
                weight = max(1, int(float(row.get("processed") or 0)))
            except (TypeError, ValueError):
                continue
            weighted_total += value * weight
            weight_total += weight
        if weight_total:
            avg_stage_timings[field] = round(weighted_total / weight_total, 4)
    return {
        "command": command,
        "runner_count": len(rows),
        "processed": processed,
        "combined_pages_per_minute": round(combined_ppm, 3),
        "pending": pending,
        "eta_minutes": round(eta_minutes, 2) if eta_minutes is not None else None,
        "avg_stage_timings": avg_stage_timings,
        "runners": rows,
    }
