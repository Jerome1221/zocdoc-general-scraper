from __future__ import annotations

import csv
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .urls import (
    is_listing_url,
    is_profile_url,
    is_specialty_index_url,
    is_specialty_landing_url,
    normalize_url,
    safe_slug,
)
from .utils import now_iso
from .workspace import Workspace

HOST = "127.0.0.1"
PORT = 8765
DEFAULT_SERVER_URL = f"http://{HOST}:{PORT}"
MAX_BODY = 80 * 1024 * 1024
SAVER_MODE = "zocdoc-provider-collector-multirunner-v4"
MANIFEST_FIELDS = ["event_at", "status", "page_type", "url", "title", "file", "bytes", "error"]

DEFAULT_CAPTURE_CONFIG = {
    "listing": {"check_ms": 250, "stable_checks": 2, "max_wait_ms": 8000},
    "profile": {"check_ms": 500, "stable_checks": 3, "max_wait_ms": 12000},
    "specialty_landing": {"check_ms": 350, "stable_checks": 2, "max_wait_ms": 10000},
    "specialty_index": {"check_ms": 500, "stable_checks": 3, "max_wait_ms": 12000},
    "controller_poll_ms": 200,
}




def _runner_token(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    return cleaned or "runner"


def page_type(url: str) -> str:
    if is_specialty_index_url(url):
        return "specialty_index"
    if is_profile_url(url):
        return "profile"
    if is_specialty_landing_url(url):
        return "specialty_landing"
    if is_listing_url(url):
        return "listing"
    return "unsupported"


def normalize_server_url(value: str | None = None) -> str:
    raw = (value or DEFAULT_SERVER_URL).strip().rstrip("/")
    if not raw.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("controller URL must point to localhost/127.0.0.1")
    return raw


def health_url(server_url: str | None = None) -> str:
    return f"{normalize_server_url(server_url)}/health"


def enqueue_url(
    url: str,
    timeout: float = 3.0,
    *,
    server_url: str | None = None,
    capture_token: str | None = None,
) -> dict:
    payload_data = {"url": normalize_url(url)}
    if capture_token:
        payload_data["capture_token"] = capture_token
    payload = json.dumps(payload_data).encode("utf-8")
    request = urllib.request.Request(
        f"{normalize_server_url(server_url)}/enqueue",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_health(timeout: float = 2.0, *, server_url: str | None = None) -> dict:
    try:
        with urllib.request.urlopen(health_url(server_url), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def configure_capture(
    page_type_name: str,
    *,
    check_ms: int,
    stable_checks: int,
    max_wait_ms: int,
    controller_poll_ms: int = 200,
    timeout: float = 3.0,
    server_url: str | None = None,
) -> dict:
    payload = json.dumps(
        {
            "page_type": page_type_name,
            "check_ms": int(check_ms),
            "stable_checks": int(stable_checks),
            "max_wait_ms": int(max_wait_ms),
            "controller_poll_ms": int(controller_poll_ms),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{normalize_server_url(server_url)}/capture-config",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_capture_config(timeout: float = 2.0, *, server_url: str | None = None) -> dict:
    try:
        with urllib.request.urlopen(f"{normalize_server_url(server_url)}/capture-config", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def get_capture_metrics(
    url: str,
    *,
    server_url: str | None = None,
    timeout: float = 1.0,
    attempts: int = 4,
    retry_sleep: float = 0.05,
) -> dict:
    """Return the most recent browser/save timing payload for one captured URL."""
    target = normalize_url(url)
    endpoint = (
        f"{normalize_server_url(server_url)}/capture-metrics?"
        + urllib.parse.urlencode({"url": target})
    )
    last_error = ""
    for attempt in range(max(1, int(attempts))):
        try:
            with urllib.request.urlopen(endpoint, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("ok") and data.get("found"):
                return data.get("metrics") or {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = str(exc)
        if attempt + 1 < max(1, int(attempts)) and retry_sleep > 0:
            time.sleep(retry_sleep)
    return {"error": last_error} if last_error else {}


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class SaverRuntime:
    def __init__(self, workspace: Workspace, *, runner_id: str = "single", host: str = HOST, port: int = PORT):
        self.workspace = workspace.ensure()
        self.runner_id = runner_id
        self.host = host
        self.port = int(port)
        self.lock = threading.Lock()
        self.open_queue: deque[str] = deque()
        self.open_set: set[str] = set()
        self.last_controller_poll = 0.0
        self.capture_config = json.loads(json.dumps(DEFAULT_CAPTURE_CONFIG))
        self.capture_metrics: dict[str, dict] = {}
        self.updater_capture_tokens: dict[str, deque[str]] = {}

    def controller_active(self) -> bool:
        return (time.time() - self.last_controller_poll) < 3.0

    def append_manifest(self, **row) -> None:
        path = self.workspace.manifest if self.runner_id == "single" else self.workspace.root / f"manifest.{_runner_token(self.runner_id)}.csv"
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in MANIFEST_FIELDS})


def controller_html() -> str:
    return """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Zocdoc Collector Controller</title>
<style>
body { font-family: Arial, sans-serif; margin: 40px; max-width: 760px; }
.ok { color: #0a7a28; font-weight: 700; }
code { background: #f3f3f3; padding: 2px 5px; }
</style>
</head>
<body>
<h1>Zocdoc Collector Controller</h1>
<p class="ok">Keep this tab open while collecting.</p>
<p>The extension polls the local queue and opens requested Zocdoc pages in background tabs. Rendered HTML is saved locally and the tab closes after a successful save.</p>
<p>If Zocdoc shows a restriction or verification page, the collector records a <code>BLOCKED.flag</code> and stops future batches until you review access normally.</p>
</body>
</html>"""


def make_handler(runtime: SaverRuntime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ZocdocProviderSaver/2.0"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _json(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _html(self, code: int, html: str) -> None:
            data = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.end_headers()

        def do_GET(self):
            parsed_request = urllib.parse.urlsplit(self.path)
            request_path = parsed_request.path

            if request_path == "/health":
                self._json(
                    200,
                    {
                        "ok": True,
                        "mode": SAVER_MODE,
                        "workspace": str(runtime.workspace.root),
                        "runner_id": runtime.runner_id,
                        "host": runtime.host,
                        "port": runtime.port,
                        "listing_save_dir": str(runtime.workspace.listing_dir),
                        "profile_save_dir": str(runtime.workspace.profile_dir),
                        "specialty_save_dir": str(runtime.workspace.specialty_dir),
                        "specialty_landing_save_dir": str(runtime.workspace.specialty_landing_dir),
                        "manifest": str(runtime.workspace.manifest if runtime.runner_id == "single" else runtime.workspace.root / f"manifest.{_runner_token(runtime.runner_id)}.csv"),
                        "queued_open_requests": len(runtime.open_queue),
                        "controller_active": runtime.controller_active(),
                        "capture_config": runtime.capture_config,
                    },
                )
                return

            if request_path == "/capture-config":
                self._json(200, {"ok": True, "config": runtime.capture_config})
                return

            if request_path == "/capture-metrics":
                query = urllib.parse.parse_qs(parsed_request.query)
                url = normalize_url(str((query.get("url") or [""])[0]))
                with runtime.lock:
                    metrics = dict(runtime.capture_metrics.get(url) or {})
                self._json(200, {"ok": True, "found": bool(metrics), "url": url, "metrics": metrics})
                return

            if request_path == "/controller":
                self._html(200, controller_html())
                return

            if request_path == "/next-open":
                runtime.last_controller_poll = time.time()
                with runtime.lock:
                    if runtime.open_queue:
                        url = runtime.open_queue.popleft()
                        runtime.open_set.discard(url)
                    else:
                        url = None
                self._json(200, {"ok": True, "url": url})
                return

            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            request_started = time.monotonic()
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0

            if length <= 0 or length > MAX_BODY:
                self._json(413, {"ok": False, "error": "invalid or too-large request"})
                return

            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._json(400, {"ok": False, "error": f"bad JSON: {exc}"})
                return

            if self.path == "/capture-config":
                page_type_name = str(payload.get("page_type") or "")
                if page_type_name not in {"listing", "profile", "specialty_landing", "specialty_index"}:
                    self._json(400, {"ok": False, "error": "unsupported page_type"})
                    return
                try:
                    check_ms = max(50, min(5000, int(payload.get("check_ms", 500))))
                    stable_checks = max(1, min(10, int(payload.get("stable_checks", 3))))
                    max_wait_ms = max(1000, min(60000, int(payload.get("max_wait_ms", 12000))))
                    controller_poll_ms = max(50, min(5000, int(payload.get("controller_poll_ms", 200))))
                except (TypeError, ValueError):
                    self._json(400, {"ok": False, "error": "capture config values must be integers"})
                    return
                with runtime.lock:
                    runtime.capture_config[page_type_name] = {
                        "check_ms": check_ms,
                        "stable_checks": stable_checks,
                        "max_wait_ms": max_wait_ms,
                    }
                    runtime.capture_config["controller_poll_ms"] = controller_poll_ms
                self._json(200, {"ok": True, "config": runtime.capture_config})
                return

            if self.path == "/enqueue":
                url = normalize_url(str(payload.get("url") or ""))
                if page_type(url) == "unsupported":
                    self._json(400, {"ok": False, "error": "unsupported Zocdoc URL"})
                    return
                capture_token = str(payload.get("capture_token") or "").strip()
                if capture_token and not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", capture_token):
                    self._json(400, {"ok": False, "error": "invalid capture_token"})
                    return
                with runtime.lock:
                    if capture_token:
                        runtime.updater_capture_tokens.setdefault(url, deque()).append(capture_token)
                    if url not in runtime.open_set:
                        runtime.open_queue.append(url)
                        runtime.open_set.add(url)
                self._json(200, {"ok": True, "queued": True, "url": url, "queue_size": len(runtime.open_queue)})
                return

            url = normalize_url(str(payload.get("url") or ""))
            title = str(payload.get("title") or "")
            ptype = page_type(url)
            if ptype == "unsupported":
                self._json(400, {"ok": False, "error": "unsupported Zocdoc URL"})
                return

            if self.path == "/blocked":
                text = str(payload.get("text") or "")[:3000]
                event_at = now_iso()
                with runtime.lock:
                    runtime.workspace.blocked_flag.write_text(
                        f"blocked_at={event_at}\nurl={url}\ntitle={title}\n\n{text}\n",
                        encoding="utf-8",
                    )
                    runtime.append_manifest(
                        event_at=event_at,
                        status="blocked",
                        page_type=ptype,
                        url=url,
                        title=title,
                        file="",
                        bytes="0",
                        error=text,
                    )
                print(f"BLOCKED [{ptype}]: {url}")
                self._json(200, {"ok": True, "blocked": True, "page_type": ptype})
                return

            if self.path != "/save":
                self._json(404, {"ok": False, "error": "not found"})
                return

            html = str(payload.get("html") or "")
            if not html:
                self._json(400, {"ok": False, "error": "empty HTML"})
                return
            browser_metrics = payload.get("capture_metrics") if isinstance(payload.get("capture_metrics"), dict) else {}

            capture_token = ""
            with runtime.lock:
                tokens = runtime.updater_capture_tokens.get(url)
                if tokens:
                    capture_token = tokens.popleft()
                    if not tokens:
                        runtime.updater_capture_tokens.pop(url, None)

            if capture_token:
                folder = runtime.workspace.updater_snapshot_dir
                filename = f"{capture_token}.html"
            elif ptype == "listing":
                folder = runtime.workspace.listing_dir
                filename = safe_slug(url)
            elif ptype == "profile":
                folder = runtime.workspace.profile_dir
                filename = safe_slug(url)
            elif ptype == "specialty_landing":
                folder = runtime.workspace.specialty_landing_dir
                base_name = safe_slug(url).removesuffix(".html")
                timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                filename = f"{base_name}--{timestamp}.html"
            else:
                folder = runtime.workspace.specialty_dir
                base_name = safe_slug(url).removesuffix(".html")
                timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                filename = f"{base_name}--{timestamp}.html"
            path = folder / filename
            encoded = html.encode("utf-8")
            event_at = now_iso()

            disk_started = time.monotonic()
            with runtime.lock:
                existed = path.exists()
                if capture_token:
                    temporary_path = path.with_suffix(".tmp")
                    temporary_path.write_bytes(encoded)
                    temporary_path.replace(path)
                elif not existed:
                    path.write_bytes(encoded)
                disk_finished = time.monotonic()
                runtime.append_manifest(
                    event_at=event_at,
                    status="existing" if existed else "saved",
                    page_type=ptype,
                    url=url,
                    title=title,
                    file=filename,
                    bytes=str(path.stat().st_size),
                    error="",
                )
                server_total = max(0.0, time.monotonic() - request_started)
                metrics = {
                    "navigation_seconds": round(float(browser_metrics.get("navigation_to_content_script_ms") or 0) / 1000.0, 4),
                    "dom_content_loaded_seconds": round(float(browser_metrics.get("dom_content_loaded_ms") or 0) / 1000.0, 4),
                    "load_event_seconds": round(float(browser_metrics.get("load_event_end_ms") or 0) / 1000.0, 4),
                    "stabilization_seconds": round(float(browser_metrics.get("stabilization_wait_ms") or 0) / 1000.0, 4),
                    "serialize_seconds": round(float(browser_metrics.get("html_serialize_ms") or 0) / 1000.0, 4),
                    "disk_write_seconds": round(max(0.0, disk_finished - disk_started), 4),
                    "server_save_seconds": round(server_total, 4),
                    "capture_reason": str(browser_metrics.get("capture_reason") or ""),
                    "claim_profile_visible": bool(browser_metrics.get("claim_profile_visible")),
                    "readiness_signature": str(browser_metrics.get("readiness_signature") or ""),
                }
                runtime.capture_metrics[url] = metrics

            status = "existing" if existed else "saved"
            timing_text = (
                f"load={metrics['navigation_seconds']:.2f}s "
                f"stabilize={metrics['stabilization_seconds']:.2f}s "
                f"serialize={metrics['serialize_seconds']:.3f}s "
                f"disk={metrics['disk_write_seconds']:.3f}s "
                f"server={metrics['server_save_seconds']:.3f}s"
            )
            claim_text = " claim-profile" if metrics.get("claim_profile_visible") else ""
            print(
                f"{status.upper()} [{ptype}] {path.stat().st_size:,} bytes: {filename} | "
                f"{timing_text} reason={metrics.get('capture_reason') or '-'}{claim_text}"
            )
            self._json(
                200,
                {
                    "ok": True,
                    "status": status,
                    "page_type": ptype,
                    "file": filename,
                    "bytes": path.stat().st_size,
                },
            )

    return Handler


def run_server(workspace: Workspace, *, host: str = HOST, port: int = PORT, runner_id: str = "single") -> None:
    workspace.ensure()
    if workspace.blocked_flag.exists():
        print(f"Note: {workspace.blocked_flag} exists from a previous restriction.")
        print("Review it before starting another batch.")

    runtime = SaverRuntime(workspace, runner_id=runner_id, host=host, port=port)
    handler = make_handler(runtime)
    print("Zocdoc Provider Collector - Chrome HTML saver")
    print(f"Workspace  : {workspace.root}")
    print(f"Listings   : {workspace.listing_dir}")
    print(f"Profiles   : {workspace.profile_dir}")
    print(f"Specialties: {workspace.specialty_landing_dir}")
    manifest_path = workspace.manifest if runner_id == "single" else workspace.root / f"manifest.{_runner_token(runner_id)}.csv"
    print(f"Runner     : {runner_id}")
    print(f"Manifest   : {manifest_path}")
    print(f"Controller : http://{host}:{port}/controller")
    print(f"Listening  : http://{host}:{port}")
    print("Keep the server and controller tab open. Ctrl+C stops the saver.")
    _ReusableThreadingHTTPServer((host, int(port)), handler).serve_forever()
