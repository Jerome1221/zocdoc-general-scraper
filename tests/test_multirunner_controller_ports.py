from __future__ import annotations

import json
from pathlib import Path

import zocdoc_ortho.runners as runners
from zocdoc_ortho.workspace import Workspace


class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid


def test_start_runners_launches_unique_controller_ports(tmp_path: Path, monkeypatch):
    workspace = Workspace.from_value(tmp_path / "workspace").ensure()
    runners.init_runners(workspace, count=3, base_port=8765)

    commands: list[tuple[list[str], bool]] = []
    next_pid = iter(range(1001, 1100))

    monkeypatch.setattr(runners, "find_chrome", lambda _=None: Path("/fake/chrome"))
    monkeypatch.setattr(runners, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        runners,
        "_terminate_owned_pid",
        lambda pid, **kwargs: {
            "pid": pid,
            "action": "not_running",
            "status": "not_running",
            "owned": False,
        },
    )

    def fake_popen(command, _log, *, detach_on_windows=True):
        commands.append((command, detach_on_windows))
        return _FakeProcess(next(next_pid))

    monkeypatch.setattr(runners, "_popen_background", fake_popen)
    monkeypatch.setattr(runners, "runner_status", lambda _workspace: {"runners": []})

    runners.start_runners(workspace, startup_wait_seconds=0)

    chrome_commands = [
        item for item in commands if item[0] and Path(item[0][0]) == Path("/fake/chrome")
    ]
    assert len(chrome_commands) == 3
    assert chrome_commands[0][0][-1] == "http://127.0.0.1:8765/controller?runner_id=runner-01&expected_port=8765"
    assert chrome_commands[1][0][-1] == "http://127.0.0.1:8766/controller?runner_id=runner-02&expected_port=8766"
    assert chrome_commands[2][0][-1] == "http://127.0.0.1:8767/controller?runner_id=runner-03&expected_port=8767"
    assert all(detach is False for _command, detach in chrome_commands)

    saver_commands = [
        item for item in commands if item[0] and Path(item[0][0]) != Path("/fake/chrome")
    ]
    assert saver_commands
    assert all(detach is True for _command, detach in saver_commands)

    state = json.loads((workspace.root / ".runners" / "state.json").read_text(encoding="utf-8"))
    assert [row["port"] for row in state["runners"]] == [8765, 8766, 8767]
    assert [row["launch_version"] for row in state["runners"]] == [3, 3, 3]


def test_old_launch_state_is_not_reused(tmp_path: Path, monkeypatch):
    workspace = Workspace.from_value(tmp_path / "workspace").ensure()
    config = runners.init_runners(workspace, count=2, base_port=8765)
    state_path = workspace.root / ".runners" / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "runners": [
                    {
                        **config["runners"][0],
                        "launch_version": 1,
                        "saver_pid": 111,
                        "chrome_pid": 222,
                    },
                    {
                        **config["runners"][1],
                        "launch_version": 1,
                        "saver_pid": 333,
                        "chrome_pid": 444,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    terminated: list[int] = []
    commands: list[list[str]] = []
    next_pid = iter(range(2001, 2100))

    monkeypatch.setattr(runners, "find_chrome", lambda _=None: Path("/fake/chrome"))
    monkeypatch.setattr(runners, "_pid_alive", lambda pid: bool(pid))

    def fake_terminate(pid, **kwargs):
        if pid:
            terminated.append(pid)
        return {"pid": pid, "action": "terminated", "status": "owned", "owned": True}

    monkeypatch.setattr(runners, "_terminate_owned_pid", fake_terminate)
    monkeypatch.setattr(
        runners,
        "_popen_background",
        lambda command, _log, **_kwargs: commands.append(command) or _FakeProcess(next(next_pid)),
    )
    monkeypatch.setattr(runners, "runner_status", lambda _workspace: {"runners": []})

    runners.start_runners(workspace, startup_wait_seconds=0)

    assert terminated == [111, 222, 333, 444]
    chrome_commands = [
        cmd for cmd in commands if cmd and Path(cmd[0]) == Path("/fake/chrome")
    ]
    assert len(chrome_commands) == 2
    assert ":8765/controller" in chrome_commands[0][-1]
    assert ":8766/controller" in chrome_commands[1][-1]


def test_safe_termination_skips_foreign_pid(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(runners, "_pid_alive", lambda pid: pid == 999)
    monkeypatch.setattr(
        runners,
        "_process_command_line",
        lambda _pid: 'python -m zocdoc_ortho --workspace C:/dev8/workspace saver --port 8765 --runner-id runner-01',
    )
    monkeypatch.setattr(runners, "_terminate_pid_unchecked", lambda pid: killed.append(pid))

    result = runners._terminate_owned_pid(
        999,
        expected_tokens=("C:/dev9/workspace", "saver", "--port", "8770", "runner-01"),
        label="runner-01:saver",
    )

    assert result["action"] == "skipped"
    assert result["status"] == "foreign"
    assert killed == []


def test_safe_termination_kills_owned_pid(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(runners, "_pid_alive", lambda pid: pid == 321)
    monkeypatch.setattr(
        runners,
        "_process_command_line",
        lambda _pid: (
            'python -m zocdoc_ortho --workspace C:/dev9/workspace saver '
            '--host 127.0.0.1 --port 8770 --runner-id runner-01'
        ),
    )
    monkeypatch.setattr(runners, "_terminate_pid_unchecked", lambda pid: killed.append(pid))

    result = runners._terminate_owned_pid(
        321,
        expected_tokens=("C:/dev9/workspace", "zocdoc_ortho", "saver", "--port", "8770", "runner-01"),
        label="runner-01:saver",
    )

    assert result["action"] == "terminated"
    assert result["status"] == "owned"
    assert killed == [321]
