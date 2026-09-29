from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from .db import connect
from .outputs import export_page_outputs, export_profile_queue
from .queue import reconcile_existing_files
from .workspace import Workspace


def import_legacy(source: Path, workspace: Workspace, *, force: bool = False) -> dict[str, int]:
    """Import an earlier notebook-style collector workspace into the packaged layout.

    SQLite's backup API is used so committed WAL transactions are copied safely.
    Stop active collector processes first so the imported snapshot is easy to reason about.
    """
    source = source.expanduser().resolve()
    workspace.ensure()
    if not source.exists():
        raise FileNotFoundError(source)

    copied = {"database": 0, "locations_csv": 0, "listing_html": 0, "profile_html": 0}

    src_db = source / "collector.db"
    if src_db.exists():
        if workspace.db.exists() and not force:
            raise FileExistsError(f"Destination DB exists: {workspace.db}. Pass --force to replace it.")
        if workspace.db.exists():
            workspace.db.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(workspace.db) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        with sqlite3.connect(src_db) as src_conn, sqlite3.connect(workspace.db) as dst_conn:
            src_conn.backup(dst_conn)
        copied["database"] = 1

    src_locations = source / "zocdoc_locations.csv"
    if src_locations.exists() and (force or not workspace.locations_csv.exists()):
        shutil.copy2(src_locations, workspace.locations_csv)
        copied["locations_csv"] = 1

    listing_dirs = [
        source / "saved_html",
        source / "zocdoc-chrome-html-saver" / "saved_html",
        source / "captured" / "listings",
    ]
    profile_dirs = [
        source / "saved_profiles",
        source / "zocdoc-chrome-html-saver" / "saved_profiles",
        source / "captured" / "profiles",
    ]

    copied["listing_html"] = _copy_html(listing_dirs, workspace.listing_dir)
    copied["profile_html"] = _copy_html(profile_dirs, workspace.profile_dir)

    # Run migrations and regenerate queue exports from the imported DB.
    with connect(workspace) as conn:
        export_page_outputs(conn, workspace)
        export_profile_queue(conn, workspace)
    reconcile_existing_files(workspace)
    return copied


def _copy_html(candidates: list[Path], destination: Path) -> int:
    seen: set[Path] = set()
    count = 0
    destination.mkdir(parents=True, exist_ok=True)
    for folder in candidates:
        if not folder.exists() or folder in seen:
            continue
        seen.add(folder)
        for src in folder.glob("*.html"):
            dst = destination / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
                count += 1
    return count
