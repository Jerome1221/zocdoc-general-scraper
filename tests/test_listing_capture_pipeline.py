from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from zocdoc_ortho import queue
from zocdoc_ortho.workspace import Workspace


class _Recorder:
    def __init__(self, *_args, **_kwargs):
        self.events = []

    def record(self, **event):
        self.events.append(event)

    def finish(self):
        return {}


def test_listing_capture_slot_refills_before_trace_ingest(tmp_path: Path, monkeypatch):
    workspace = Workspace.from_value(tmp_path / "workspace").ensure()
    urls = ["https://example.test/location/2", "https://example.test/location/3"]
    rows = iter(
        [
            {
                "url": url,
                "specialty_name": "Test Specialty",
                "specialty_slug": "test-specialty",
                "location": "Test Location",
                "page_no": index + 2,
            }
            for index, url in enumerate(urls)
        ]
    )
    find_calls: dict[str, int] = {}
    events: list[str] = []

    monkeypatch.setattr(queue, "_assert_saver_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(queue, "activate_zero_link_retries", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(queue, "configure_capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(queue, "PerformanceRecorder", _Recorder)
    monkeypatch.setattr(queue.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(queue, "connect", lambda _workspace: nullcontext(object()))
    monkeypatch.setattr(
        queue,
        "_claim_pending_page",
        lambda *_args, **_kwargs: next(rows, None),
    )

    def fake_find(_workspace, url):
        count = find_calls.get(url, 0)
        find_calls[url] = count + 1
        return None if count == 0 else tmp_path / f"capture-{urls.index(url)}.html"

    def fake_enqueue(url, **_kwargs):
        events.append(f"enqueue:{url}")

    def fake_ingest(_conn, _workspace, url, _path, **_kwargs):
        events.append(f"ingest:{url}")
        return {
            "queue_status": "saved",
            "doctors": [],
            "pagination_urls": [],
            "provider_set_hash": "",
            "_timings": {},
        }

    monkeypatch.setattr(queue, "find_listing_html", fake_find)
    monkeypatch.setattr(queue, "enqueue_url", fake_enqueue)
    monkeypatch.setattr(queue, "get_capture_metrics", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(queue, "_ingest_listing", fake_ingest)

    processed = queue.collect_listing_batch(
        workspace,
        limit=2,
        concurrency=1,
        poll_seconds=0.01,
        phase="pagination",
        server_url="http://127.0.0.1:8765",
        runner_id="runner-test",
    )

    assert processed == 2
    assert events == [
        f"enqueue:{urls[0]}",
        f"enqueue:{urls[1]}",
        f"ingest:{urls[0]}",
        f"ingest:{urls[1]}",
    ]
