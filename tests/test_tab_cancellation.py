from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from zocdoc_ortho import queue
from zocdoc_ortho.db import connect
from zocdoc_ortho.listing import seed_locations
from zocdoc_ortho.saver import SaverRuntime, enqueue_url, make_handler, request_tab_close
from zocdoc_ortho.workspace import Workspace


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def test_cancel_removes_pending_open_and_queues_one_tab_close(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    runtime = SaverRuntime(workspace, runner_id="runner-test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_url = f"http://127.0.0.1:{server.server_port}"
    url = "https://www.zocdoc.com/gastroenterologists/test-city-10001pm/2"
    try:
        enqueue_url(url, server_url=server_url)
        request_tab_close(url, server_url=server_url)
        request_tab_close(url, server_url=server_url)

        assert _get_json(f"{server_url}/next-open")["url"] is None
        assert _get_json(f"{server_url}/next-close")["url"] == url
        assert _get_json(f"{server_url}/next-close")["url"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_listing_timeout_requests_physical_tab_close(tmp_path: Path, monkeypatch):
    workspace = Workspace(tmp_path / "workspace").ensure()
    url = "https://www.zocdoc.com/gastroenterologists/test-city-10001pm"
    source = tmp_path / "locations.csv"
    source.write_text(
        "url,state_group,location,specialty_url,specialty_name,specialty_slug\n"
        f"{url},NY,Test City,https://www.zocdoc.com/gastroenterologists,"
        "Gastroenterologists,gastroenterologists\n",
        encoding="utf-8",
    )
    with connect(workspace) as conn:
        seed_locations(source, conn)

    closed: list[tuple[str, str | None]] = []
    monkeypatch.setattr(queue, "_assert_saver_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(queue, "configure_capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(queue, "enqueue_url", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(
        queue,
        "_queue_tab_close",
        lambda target, server_url: closed.append((target, server_url)),
    )

    completed = queue.collect_listing_batch(
        workspace,
        limit=1,
        concurrency=1,
        timeout_seconds=0,
        stagger_seconds=0,
        poll_seconds=0.001,
        server_url="http://127.0.0.1:8765",
        runner_id="runner-test",
    )

    assert completed == 1
    assert closed == [(url, "http://127.0.0.1:8765")]
    with connect(workspace) as conn:
        assert conn.execute("SELECT status FROM pages WHERE url=?", (url,)).fetchone()["status"] == "timeout"
