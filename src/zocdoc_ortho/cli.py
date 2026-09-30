from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

from .daily_sync import (
    bootstrap_from_trace,
    daily_sync_lock,
    daily_sync_status,
    migrate_profiles_from_export,
    resume_daily_profiles,
    run_daily_daemon,
    run_daily_sync,
)
from .db import connect
from .export import derive_outputs, export_final
from .legacy import import_legacy
from .listing import seed_locations
from .outputs import export_page_outputs
from .performance import performance_report
from .postgres_sync import publish_to_postgres
from .profile import parse_profile_rows
from .queue import (
    audit_zero_link_listings,
    choose_listing_phase,
    collect_listing_batch,
    collect_one_profile,
    collect_profile_batch,
    print_status,
    reconcile_existing_files,
    reset_statuses,
)
from .runners import (
    combined_performance_report,
    init_runners,
    load_runner_config,
    run_multi_listings,
    run_multi_profiles,
    runner_status,
    start_runners,
    stop_runners,
)
from .saver import get_health, run_server
from .schema import REQUIRED_COLUMNS
from .specialties import (
    collect_specialty_catalog_html,
    latest_specialty_catalog_html,
    refresh_specialties,
)
from .specialty_runtime import append_specialty_runtime, resolve_specialty, specialty_runtime_report
from .specialty_targets import (
    collect_specialty_landing_batch,
    export_specialty_target_outputs,
    rebuild_specialty_targets_from_saved,
    sync_specialty_pages_from_catalog,
)
from .trace import cleanup_parsed_listing_html, rebuild_listing_trace, repair_trace_partials, trace_rows_for_doctor
from .updater import (
    check_location_snapshots,
    create_live_location_snapshots,
    create_location_snapshots,
    load_refresh_queue,
)
from .workspace import Workspace
from .zip_inputs import build_scraped_zip_inputs


def _add_daily_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="Compatibility flag; daily synchronization always uses fresh browser captures",
    )
    parser.add_argument("--limit", type=int, default=None, help="Test with only the first N locations")
    parser.add_argument(
        "--from-snapshots",
        action="store_true",
        help="Use only locations with an existing successful count snapshot",
    )
    parser.add_argument("--specialty", action="append", default=[], help="Specialty slug/name selector")
    parser.add_argument("--runner", action="append", default=[], help="Active runner id to use")
    parser.add_argument("--concurrency-per-runner", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--poll", type=float, default=0.2)
    parser.add_argument("--capture-retries", type=int, default=2)
    parser.add_argument(
        "--removal-confirmations",
        type=int,
        default=2,
        help="Complete consecutive crawls required before soft-deactivation",
    )
    parser.add_argument("--profile-refresh-days", type=int, default=30)
    parser.add_argument("--profile-limit", type=int, default=None)
    parser.add_argument("--skip-profiles", action="store_true")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional PostgreSQL URL; defaults to ZOCDOC_DATABASE_URL",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zocdoc-collector",
        description="Reusable Zocdoc provider collection pipeline.",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Mutable workspace directory. Default: $ZOCDOC_WORKSPACE or ./workspace",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create the workspace directories and database")
    sub.add_parser("extension-path", help="Print the unpacked Chrome extension path")
    saver = sub.add_parser("saver", help="Run a local HTML saver server")
    saver.add_argument("--host", default="127.0.0.1")
    saver.add_argument("--port", type=int, default=8765)
    saver.add_argument("--runner-id", default="single")
    health = sub.add_parser("health", help="Check saver/controller health")
    health.add_argument("--controller-url", default="http://127.0.0.1:8765")
    sub.add_parser("status", help="Show listing/profile queue progress")
    sub.add_parser("reconcile", help="Reconcile already-saved HTML into SQLite")
    sub.add_parser("show-blocked", help="Print BLOCKED.flag if one exists")

    updater = sub.add_parser(
        "updater",
        help="Maintain an existing provider database with incremental refresh checks",
    )
    updater_sub = updater.add_subparsers(dest="updater_command", required=True, title="Commands")
    updater_snapshot = updater_sub.add_parser(
        "snapshot", help="Create verified-count snapshots for current locations"
    )
    updater_snapshot.add_argument(
        "--live",
        action="store_true",
        help="Fetch fresh listing HTML through the configured Dev9 browser runners",
    )
    updater_snapshot.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum locations to select before browser collection",
    )
    updater_snapshot.add_argument(
        "--from-snapshots",
        action="store_true",
        help="Select only locations that already have a successful snapshot",
    )
    updater_sub.add_parser("check", help="Compare current counts against latest snapshots and queue refreshes")
    updater_queue = updater_sub.add_parser("queue", help="View pending updater refresh jobs")
    updater_queue.add_argument("--status", default=None, help="Optional queue status filter")
    updater_queue.add_argument("--limit", type=int, default=50, help="Maximum queue rows to print")
    updater_daily = updater_sub.add_parser(
        "daily",
        help="Run the complete listing, provider-diff, profile, and database update workflow",
    )
    _add_daily_arguments(updater_daily)
    updater_sub.add_parser(
        "bootstrap",
        help="Seed canonical provider memberships from already parsed listing trace data",
    )
    updater_migrate_profiles = updater_sub.add_parser(
        "migrate-profiles",
        help="Import existing unique_doctors.csv profiles without browser requests",
    )
    updater_migrate_profiles.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Profile CSV; defaults to workspace/output/unique_doctors.csv",
    )
    updater_status = updater_sub.add_parser("status", help="Show canonical updater status and recent runs")
    updater_status.add_argument("--limit", type=int, default=10)
    updater_profiles = updater_sub.add_parser(
        "profiles", help="Resume pending profile captures for a daily run"
    )
    updater_profiles.add_argument("--run-id", type=int, default=None)
    updater_profiles.add_argument("--runner", action="append", default=[])
    updater_profiles.add_argument("--limit", type=int, default=None)
    updater_profiles.add_argument("--concurrency-per-runner", type=int, default=3)
    updater_profiles.add_argument("--timeout", type=float, default=60.0)
    updater_profiles.add_argument("--poll", type=float, default=0.2)
    updater_profiles.add_argument("--capture-retries", type=int, default=2)
    updater_publish = updater_sub.add_parser(
        "publish", help="Publish the canonical provider database to PostgreSQL"
    )
    updater_publish.add_argument("--run-id", type=int, default=None)
    updater_publish.add_argument("--database-url", required=True)
    updater_daemon = updater_sub.add_parser(
        "daemon", help="Run the complete updater repeatedly at a fixed daily interval"
    )
    _add_daily_arguments(updater_daemon)
    updater_daemon.add_argument("--interval-hours", type=float, default=24.0)
    updater_daemon.add_argument("--no-run-immediately", action="store_true")

    specialties = sub.add_parser(
        "specialties",
        help="Build or refresh the specialty catalog from Zocdoc /specialty",
    )
    specialty_source = specialties.add_mutually_exclusive_group()
    specialty_source.add_argument(
        "--collect",
        action="store_true",
        help="Capture a fresh /specialty page through the Chrome saver before parsing",
    )
    specialty_source.add_argument(
        "--html",
        type=Path,
        default=None,
        help="Parse a specific previously saved /specialty HTML file",
    )
    specialties.add_argument("--timeout", type=int, default=60)
    specialties.add_argument("--controller-url", default="http://127.0.0.1:8765")

    discover_targets = sub.add_parser(
        "discover-targets",
        help="Capture specialty landing pages and build specialty/location listing targets",
    )
    discover_targets.add_argument(
        "--collect",
        action="store_true",
        help="Capture pending specialty landing pages through Chrome before parsing",
    )
    discover_targets.add_argument("--limit", type=int, default=25)
    discover_targets.add_argument("--concurrency", type=int, default=4)
    discover_targets.add_argument("--timeout", type=int, default=60)
    discover_targets.add_argument("--stagger", type=float, default=0.15)
    discover_targets.add_argument("--poll", type=float, default=0.25, help="CLI completion poll interval in seconds")
    discover_targets.add_argument("--page-check-ms", type=int, default=350, help="Rendered-page readiness check interval")
    discover_targets.add_argument("--stable-checks", type=int, default=2, help="Consecutive unchanged readiness checks before save")
    discover_targets.add_argument("--page-max-wait-ms", type=int, default=10000, help="Maximum rendered-page readiness wait")
    discover_targets.add_argument("--controller-poll-ms", type=int, default=200, help="Chrome controller queue poll interval")
    discover_targets.add_argument("--controller-url", default="http://127.0.0.1:8765")
    discover_targets.add_argument("--runner-id", default="single")
    discover_targets.add_argument(
        "--specialty",
        action="append",
        default=[],
        help="Optional repeatable specialty slug/name/URL filter",
    )

    seed = sub.add_parser("seed", help="Seed base listing pages from a location CSV")
    seed.add_argument("csv", type=Path, help="CSV with url; optional state_group/location columns")

    collect_listing = sub.add_parser("collect-listings", help="Collect listing pages, inline-trace providers, and prune successful raw HTML")
    collect_listing.add_argument("--limit", type=int, default=250)
    collect_listing.add_argument("--concurrency", type=int, default=4)
    collect_listing.add_argument("--timeout", type=int, default=60)
    collect_listing.add_argument("--stagger", type=float, default=0.1)
    collect_listing.add_argument("--poll", type=float, default=0.2, help="CLI completion poll interval in seconds")
    collect_listing.add_argument("--page-check-ms", type=int, default=250, help="Rendered-page readiness check interval")
    collect_listing.add_argument("--stable-checks", type=int, default=2, help="Consecutive unchanged readiness checks before save")
    collect_listing.add_argument("--page-max-wait-ms", type=int, default=8000, help="Maximum rendered-page readiness wait")
    collect_listing.add_argument("--controller-poll-ms", type=int, default=200, help="Chrome controller queue poll interval")
    collect_listing.add_argument("--controller-url", default="http://127.0.0.1:8765")
    collect_listing.add_argument("--runner-id", default="single")
    collect_listing.add_argument("--zero-link-max-retries", type=int, default=2)
    collect_listing.add_argument(
        "--keep-listing-html",
        action="store_true",
        help="Keep successful raw listing HTML (dev9 deletes it after inline trace by default)",
    )
    collect_listing.add_argument("--phase", choices=["auto", "seed", "pagination"], default="auto")
    collect_listing.add_argument(
        "--specialty",
        action="append",
        default=[],
        help="Optional repeatable specialty slug/name/URL filter",
    )

    trace = sub.add_parser("trace", help="Incrementally persist listing -> doctor trace into SQLite")
    trace.add_argument("--force", action="store_true", help="Reparse every available saved listing HTML")
    trace.add_argument("--limit", type=int, default=None, help="Maximum listing HTML candidates to examine this run")
    trace.add_argument(
        "--batch-size",
        type=int,
        default=250,
        help="Commit and log progress every N listing HTMLs (default: 250)",
    )
    trace.add_argument(
        "--delete-html",
        action="store_true",
        help="Permanently delete listing HTML only after its trace batch commits successfully",
    )

    repair_partials = sub.add_parser(
        "repair-trace-partials",
        help="Offline-repair retained review pages whose validated provider links were missing from durable trace",
    )
    repair_partials.add_argument("--limit", type=int, default=None)
    repair_partials.add_argument(
        "--delete-html",
        action="store_true",
        help="Delete retained HTML only after complete trace coverage is verified",
    )

    cleanup_trace = sub.add_parser(
        "cleanup-listing-html",
        help="Delete raw listing HTML only for pages already marked trace_status=parsed",
    )
    cleanup_trace.add_argument("--limit", type=int, default=None)
    cleanup_trace.add_argument("--dry-run", action="store_true")

    perf = sub.add_parser("perf-report", help="Show recent crawl throughput and ETA from recorded run metrics")
    perf.add_argument("--command", choices=["collect-listings", "collect-profiles", "discover-targets"], default="collect-listings")
    perf.add_argument("--last", type=int, default=5, help="Number of recent matching runs used for the median throughput")

    smoke = sub.add_parser("smoke", help="Save/parse exactly one doctor profile before bulk collection")
    smoke.add_argument("--doctor-url", default=None)
    smoke.add_argument("--timeout", type=int, default=60)
    smoke.add_argument("--controller-url", default="http://127.0.0.1:8765")

    collect_profiles = sub.add_parser("collect-profiles", help="Bulk-save unique doctor profile HTML")
    collect_profiles.add_argument("--limit", type=int, default=250)
    collect_profiles.add_argument("--concurrency", type=int, default=3)
    collect_profiles.add_argument("--timeout", type=int, default=60)
    collect_profiles.add_argument("--stagger", type=float, default=0.25)
    collect_profiles.add_argument("--poll", type=float, default=0.25, help="CLI completion poll interval in seconds")
    collect_profiles.add_argument("--page-check-ms", type=int, default=500, help="Rendered-page readiness check interval")
    collect_profiles.add_argument("--stable-checks", type=int, default=3, help="Consecutive unchanged readiness checks before save")
    collect_profiles.add_argument("--page-max-wait-ms", type=int, default=12000, help="Maximum rendered-page readiness wait")
    collect_profiles.add_argument("--controller-poll-ms", type=int, default=200, help="Chrome controller queue poll interval")
    collect_profiles.add_argument("--controller-url", default="http://127.0.0.1:8765")
    collect_profiles.add_argument("--runner-id", default="single")
    collect_profiles.add_argument(
        "--specialty",
        action="append",
        default=[],
        help="Optional specialty filter; profiles are limited to providers first discovered by that specialty",
    )
    collect_profiles.add_argument(
        "--stale-claim-minutes",
        type=float,
        default=10.0,
        help="Recover profile claims left opening longer than this many minutes",
    )

    runners = sub.add_parser("runners", help="Manage N isolated Chrome/controller runner sessions")
    runners_sub = runners.add_subparsers(dest="runner_command", required=True)
    runners_init = runners_sub.add_parser("init", help="Create persistent runner profiles and port assignments")
    runners_init.add_argument("--count", type=int, default=2)
    runners_init.add_argument("--base-port", type=int, default=8765)
    runners_init.add_argument("--chrome-path", default=None)
    runners_start = runners_sub.add_parser("start", help="Start saver servers and Chrome/controller sessions")
    runners_start.add_argument("--chrome-path", default=None)
    runners_start.add_argument("--startup-wait", type=float, default=2.0)
    runners_start.add_argument("--restart", action="store_true", help="Force relaunch all configured saver and Chrome sessions")
    runners_sub.add_parser("status", help="Show saver/browser/controller state for all configured runners")
    runners_stop = runners_sub.add_parser("stop", help="Stop saver servers and runner Chrome processes")
    runners_stop.add_argument("--keep-browsers", action="store_true")
    runners_sub.add_parser("perf", help="Show combined throughput from the latest runner listing runs")

    multi_listing = sub.add_parser("multi-listings", help="Collect and inline-trace listings across configured runners")
    multi_listing.add_argument("--limit", type=int, default=400, help="Total pages across selected runners")
    multi_listing.add_argument("--concurrency-per-runner", type=int, default=4)
    multi_listing.add_argument("--timeout", type=int, default=60)
    multi_listing.add_argument("--stagger", type=float, default=0.1)
    multi_listing.add_argument("--poll", type=float, default=0.2)
    multi_listing.add_argument("--page-check-ms", type=int, default=250)
    multi_listing.add_argument("--stable-checks", type=int, default=2)
    multi_listing.add_argument("--page-max-wait-ms", type=int, default=8000)
    multi_listing.add_argument("--controller-poll-ms", type=int, default=200)
    multi_listing.add_argument("--zero-link-max-retries", type=int, default=2)
    multi_listing.add_argument(
        "--keep-listing-html",
        action="store_true",
        help="Keep successful raw listing HTML on all selected runners",
    )
    multi_listing.add_argument("--phase", choices=["auto", "seed", "pagination"], default="auto")
    multi_listing.add_argument(
        "--specialty",
        action="append",
        default=[],
        help="Optional repeatable specialty slug/name/URL filter",
    )
    multi_listing.add_argument(
        "--runner",
        action="append",
        default=[],
        help="Optional repeatable runner id (for example --runner runner-01 --runner runner-02)",
    )

    multi_profiles = sub.add_parser("multi-profiles", help="Collect profiles across selected configured runners")
    multi_profiles.add_argument("--limit", type=int, default=500, help="Total profiles across selected runners")
    multi_profiles.add_argument("--concurrency-per-runner", type=int, default=3)
    multi_profiles.add_argument("--timeout", type=int, default=60)
    multi_profiles.add_argument("--stagger", type=float, default=0.25)
    multi_profiles.add_argument("--poll", type=float, default=0.25)
    multi_profiles.add_argument("--page-check-ms", type=int, default=500)
    multi_profiles.add_argument("--stable-checks", type=int, default=3)
    multi_profiles.add_argument("--page-max-wait-ms", type=int, default=12000)
    multi_profiles.add_argument("--controller-poll-ms", type=int, default=200)
    multi_profiles.add_argument("--stale-claim-minutes", type=float, default=10.0)
    multi_profiles.add_argument(
        "--specialty",
        action="append",
        default=[],
        help="Optional specialty filter; profiles are limited to providers first discovered by that specialty",
    )
    multi_profiles.add_argument(
        "--runner",
        action="append",
        default=[],
        help="Optional repeatable runner id; default is all configured runners",
    )

    specialty_runtime = sub.add_parser(
        "specialty-runtime",
        help="Show measured and projected runtime for one specialty",
    )
    specialty_runtime.add_argument("--specialty", required=True, help="Specialty slug, name, or URL")

    retry_zero = sub.add_parser(
        "retry-zero-listings",
        help="Audit saved zero-link listing pages and schedule suspicious captures for a later retry",
    )
    retry_zero.add_argument("--max-retries", type=int, default=2)

    retry = sub.add_parser("retry", help="Reset interrupted/timeout/review queue rows to pending")
    retry.add_argument("--target", choices=["listings", "profiles", "specialty-pages"], required=True)
    retry.add_argument(
        "--statuses",
        nargs="+",
        choices=["opening", "timeout", "blocked", "review", "retry_pending"],
        default=["opening", "timeout"],
    )

    export = sub.add_parser("export", help="Build canonical 35-column final outputs")
    export.add_argument("--allow-partial", action="store_true")
    export.add_argument("--source-zip-mode", choices=["office", "blank"], default="office")
    export.add_argument("--no-derived", action="store_true")

    sub.add_parser("derive", help="Rebuild derived CSVs and ZIP input workbook from final_providers.csv")

    zip_input = sub.add_parser(
        "zip-input",
        help="Build original-scraper-compatible ZIP-by-state inputs from collected provider locations",
    )
    zip_input.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Optional provider CSV. Default: workspace/output/final_providers.csv",
    )

    legacy = sub.add_parser("import-legacy", help="Import the earlier notebook-style collector workspace")
    legacy.add_argument("source", type=Path)
    legacy.add_argument("--force", action="store_true")

    return parser


def _format_runtime(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes / 60.0:.2f}h"


def _format_stage_timing_summary(timings: dict | None, *, profile: bool = False) -> str:
    timings = timings or {}
    fields = [
        ("load", "navigation_seconds"),
        ("stabilize", "stabilization_seconds"),
        ("serialize", "serialize_seconds"),
        ("disk", "disk_write_seconds"),
        ("server-save", "server_save_seconds"),
    ]
    if profile:
        fields.append(("profile-db", "profile_mark_db_seconds"))
    else:
        fields.extend(
            [
                ("validate", "validation_seconds"),
                ("listing-db", "listing_db_seconds"),
                ("trace", "trace_seconds"),
                ("profile-sync", "profile_sync_seconds"),
                ("cleanup", "cleanup_seconds"),
                ("output", "output_seconds"),
            ]
        )
    parts = []
    for label, key in fields:
        value = timings.get(key)
        if value in {None, ""}:
            continue
        try:
            parts.append(f"{label}={float(value):.3f}s")
        except (TypeError, ValueError):
            continue
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Workspace.from_value(args.workspace).ensure()

    try:
        command = args.command
        if command == "init":
            with connect(workspace):
                pass
            print(f"Workspace initialized: {workspace.root}")
            return 0

        if command == "extension-path":
            print(Path(__file__).resolve().parent / "chrome_extension")
            return 0

        if command == "saver":
            run_server(workspace, host=args.host, port=args.port, runner_id=args.runner_id)
            return 0

        if command == "health":
            health = get_health(server_url=args.controller_url)
            print(json.dumps(health, indent=2))
            return 0 if health.get("ok") else 1

        if command == "status":
            print_status(workspace)
            return 0

        if command == "show-blocked":
            if workspace.blocked_flag.exists():
                print(workspace.blocked_flag.read_text(encoding="utf-8", errors="replace"))
                return 2
            print("No BLOCKED.flag exists.")
            return 0

        if command == "updater":
            if args.updater_command in {"daily", "daemon"}:
                daily_options = {
                    "limit": args.limit,
                    "from_snapshots": args.from_snapshots,
                    "specialties": tuple(args.specialty),
                    "runner_ids": tuple(args.runner),
                    "concurrency_per_runner": args.concurrency_per_runner,
                    "timeout_seconds": args.timeout,
                    "poll_seconds": args.poll,
                    "capture_retries": args.capture_retries,
                    "removal_confirmations": args.removal_confirmations,
                    "profile_refresh_days": args.profile_refresh_days,
                    "profile_limit": args.profile_limit,
                    "skip_profiles": args.skip_profiles,
                    "database_url": args.database_url,
                }
                if args.updater_command == "daemon":
                    run_daily_daemon(
                        workspace,
                        interval_hours=args.interval_hours,
                        run_immediately=not args.no_run_immediately,
                        **daily_options,
                    )
                    return 0
                result = run_daily_sync(workspace, **daily_options)
                print("=" * 72)
                print("DAILY PROVIDER DATABASE UPDATE")
                print("=" * 72)
                print(f"Run id                 : {result['id']}")
                print(f"Status                 : {result['status']}")
                print(
                    f"Locations              : {result['locations_completed']:,} complete / "
                    f"{result['locations_failed']:,} failed / {result['locations_selected']:,} selected"
                )
                print(f"Listing pages fetched  : {result['pages_fetched']:,}")
                print(f"Provider memberships   : {result['providers_seen']:,} seen")
                print(f"Added / reactivated    : {result['providers_added']:,} / {result['providers_reactivated']:,}")
                print(f"Pending / removed      : {result['removals_pending']:,} / {result['providers_removed']:,}")
                print(
                    f"Profiles queued/saved  : {result['profiles_queued']:,} / "
                    f"{result['profiles_saved']:,}"
                )
                print(f"Profiles still pending : {result.get('profiles_pending', 0):,}")
                print(f"PostgreSQL published   : {bool(result['database_published'])}")
                if result.get("error"):
                    print(f"Error                  : {result['error']}")
                print("=" * 72)
                return 0 if result["status"] == "complete" else 2

            if args.updater_command == "bootstrap":
                result = bootstrap_from_trace(workspace)
                print("=" * 72)
                print("CANONICAL DATABASE BOOTSTRAP")
                print("=" * 72)
                print(f"Run id                 : {result['run_id']}")
                print(f"Locations imported     : {result['locations']:,}")
                print(f"Memberships imported   : {result['memberships']:,}")
                print(f"Unique providers       : {result['providers']:,}")
                print(f"Profiles imported      : {result['profiles_imported']:,}")
                print(f"Profiles queued        : {result['profiles_queued']:,}")
                print("No browser requests were made.")
                print("=" * 72)
                return 0

            if args.updater_command == "migrate-profiles":
                with daily_sync_lock(workspace):
                    result = migrate_profiles_from_export(workspace, input_path=args.input)
                print("=" * 72)
                print("CANONICAL PROFILE MIGRATION")
                print("=" * 72)
                print(f"Source                 : {result['source']}")
                print(f"Rows scanned           : {result['scanned']:,}")
                print(f"Profiles imported      : {result['imported']:,}")
                print(f"Already canonical      : {result['already_present']:,}")
                print(f"Invalid rows           : {result['invalid']:,}")
                print(f"Not in current baseline: {result['unmatched']:,}")
                print("No browser requests were made.")
                print("=" * 72)
                return 0

            if args.updater_command == "status":
                result = daily_sync_status(workspace, limit=args.limit)
                providers = result["providers"]
                memberships = result["memberships"]
                print("=" * 72)
                print("DAILY UPDATER STATUS")
                print("=" * 72)
                print(
                    f"Canonical providers    : {providers['total'] or 0:,} total | "
                    f"{providers['active'] or 0:,} active | {providers['inactive'] or 0:,} inactive"
                )
                print(
                    f"Location memberships   : {memberships['total'] or 0:,} total | "
                    f"{memberships['active'] or 0:,} active | {memberships['inactive'] or 0:,} inactive"
                )
                print(f"Pending profile jobs   : {result['pending_profiles']:,}")
                print("-")
                for row in result["runs"]:
                    print(
                        f"run={row['id']} status={row['status']} started={row['started_at']} "
                        f"locations={row['locations_completed']}/{row['locations_selected']} "
                        f"added={row['providers_added']} removed={row['providers_removed']}"
                    )
                if not result["runs"]:
                    print("No daily runs yet.")
                print("=" * 72)
                return 0

            if args.updater_command == "profiles":
                result = resume_daily_profiles(
                    workspace,
                    run_id=args.run_id,
                    runner_ids=tuple(args.runner),
                    limit=args.limit,
                    concurrency_per_runner=args.concurrency_per_runner,
                    timeout_seconds=args.timeout,
                    poll_seconds=args.poll,
                    capture_retries=args.capture_retries,
                )
                batch = result["profile_batch"]
                print(
                    f"Profile run {result['id']}: selected={batch['selected']:,} "
                    f"saved={batch['saved']:,} failed={batch['failed']:,} "
                    f"remaining={result.get('profiles_pending', 0):,}"
                )
                return 0 if not result.get("profiles_pending") and not batch["failed"] else 2

            if args.updater_command == "publish":
                result = publish_to_postgres(
                    workspace,
                    args.database_url,
                    run_id=args.run_id,
                )
                print(
                    f"Published run {result['run_id']} to PostgreSQL: "
                    f"providers={result['providers']:,} memberships={result['memberships']:,} "
                    f"events={result['events']:,}"
                )
                return 0

            if args.updater_command == "snapshot":
                if args.from_snapshots and not args.live:
                    raise ValueError("--from-snapshots requires --live")
                if args.limit is not None and not args.live:
                    raise ValueError("--limit requires --live")
                if args.live:
                    result = create_live_location_snapshots(
                        workspace,
                        limit=args.limit,
                        from_snapshots=args.from_snapshots,
                    )
                    print("=" * 36)
                    print("UPDATER LIVE SNAPSHOT")
                    print("=" * 36)
                    print(f"Locations selected: {result['locations_selected']:,}")
                    print(f"Browser fetched: {result['browser_fetched']:,}")
                    print(f"Snapshots created: {result['snapshots_created']:,}")
                    print(f"Failed: {result['failed']:,}")
                    print("=" * 36)
                    return 0

                result = create_location_snapshots(workspace)
                print("=" * 36)
                print("UPDATER SNAPSHOT")
                print("=" * 36)
                print(f"Locations processed: {result['locations_processed']:,}")
                print(f"Snapshots created: {result['snapshots_created']:,}")
                print(f"Failed: {result['failed']:,}")
                print("=" * 36)
                return 0
            if args.updater_command == "check":
                result = check_location_snapshots(workspace)
                print("=" * 36)
                print("UPDATER CHECK")
                print("=" * 36)
                print(f"Locations checked: {result['locations_checked']:,}")
                print(f"Unchanged: {result['unchanged']:,}")
                print(f"Changed: {result['changed']:,}")
                print(f"Refresh queue: {result['refresh_queue']:,}")
                if result.get("failed"):
                    print(f"Failed: {result['failed']:,}")
                print("=" * 36)
                return 0
            if args.updater_command == "queue":
                rows = load_refresh_queue(workspace, status=args.status, limit=args.limit)
                print("REFRESH QUEUE")
                if not rows:
                    print("(empty)")
                    return 0
                for row in rows:
                    label = row["specialty"] or row["specialty_slug"] or "(unknown specialty)"
                    location = row["location"] or row["location_url"]
                    print(f"{label} {location} reason={row['reason']} status={row['status']}")
                return 0
            raise RuntimeError(f"Unhandled updater command: {args.updater_command}")

        if command == "specialties":
            if args.html is not None:
                html_path = args.html.expanduser().resolve()
                if not html_path.exists():
                    raise FileNotFoundError(html_path)
            elif args.collect:
                html_path = collect_specialty_catalog_html(
                    workspace, timeout_seconds=args.timeout, server_url=args.controller_url
                )
            else:
                html_path = latest_specialty_catalog_html(workspace)
                if html_path is None:
                    raise FileNotFoundError(
                        "No specialty catalog capture exists. Start the saver/controller and run "
                        "'zocdoc-collector --workspace .\\workspace specialties --collect'."
                    )

            with connect(workspace) as conn:
                frame = refresh_specialties(conn, workspace, html_path)

            active = frame.loc[frame["is_active"]] if not frame.empty else frame
            facility_count = (active["entity_type"] == "facility_or_clinic").sum() if not active.empty else 0
            print("=" * 72)
            print("SPECIALTY CATALOG")
            print("=" * 72)
            print(f"Source HTML            : {html_path}")
            print(f"Active catalog entries : {len(active):,}")
            print(f"Provider specialties   : {len(active) - int(facility_count):,}")
            print(f"Facilities / clinics   : {int(facility_count):,}")
            print(f"Catalog CSV            : {workspace.output_dir / 'specialties.csv'}")
            print("=" * 72)
            return 0

        if command == "discover-targets":
            selectors = tuple(args.specialty)
            with connect(workspace) as conn:
                catalog_count = sync_specialty_pages_from_catalog(conn)
                export_specialty_target_outputs(conn, workspace)

            if catalog_count == 0:
                raise RuntimeError(
                    "No active specialties exist. Run 'specialties --collect' first."
                )

            step_started = time.monotonic()
            if args.collect:
                processed = collect_specialty_landing_batch(
                    workspace,
                    limit=args.limit,
                    concurrency=args.concurrency,
                    timeout_seconds=args.timeout,
                    stagger_seconds=args.stagger,
                    poll_seconds=args.poll,
                    page_check_ms=args.page_check_ms,
                    stable_checks=args.stable_checks,
                    page_max_wait_ms=args.page_max_wait_ms,
                    controller_poll_ms=args.controller_poll_ms,
                    specialties=selectors,
                    server_url=args.controller_url,
                    runner_id=args.runner_id,
                )
            else:
                processed = rebuild_specialty_targets_from_saved(
                    workspace, specialties=selectors
                )
            step_elapsed = time.monotonic() - step_started
            if args.collect and len(selectors) == 1 and args.runner_id == "single":
                append_specialty_runtime(
                    workspace,
                    specialty=resolve_specialty(workspace, selectors[0]),
                    step="target_discovery",
                    elapsed_seconds=step_elapsed,
                    processed=processed,
                    runner_mode="single",
                )

            with connect(workspace) as conn:
                active_targets = conn.execute(
                    "SELECT COUNT(*) n FROM specialty_targets WHERE is_active=1"
                ).fetchone()["n"]
                saved_pages = conn.execute(
                    "SELECT COUNT(*) n FROM specialty_pages WHERE status='saved'"
                ).fetchone()["n"]
                pending_pages = conn.execute(
                    "SELECT COUNT(*) n FROM specialty_pages WHERE status='pending'"
                ).fetchone()["n"]

            print("=" * 72)
            print("SPECIALTY / LOCATION TARGET DISCOVERY")
            print("=" * 72)
            print(f"Specialty pages processed: {processed:,}")
            print(f"Elapsed this step        : {_format_runtime(step_elapsed)}")
            print(f"Specialty pages saved    : {saved_pages:,}")
            print(f"Specialty pages pending  : {pending_pages:,}")
            print(f"Active listing targets   : {active_targets:,}")
            print(f"Targets CSV              : {workspace.output_dir / 'specialty_targets.csv'}")
            print(f"Summary CSV              : {workspace.output_dir / 'specialty_target_summary.csv'}")
            print("=" * 72)
            return 0

        if command == "seed":
            source = args.csv.expanduser().resolve()
            if not source.exists():
                raise FileNotFoundError(source)
            if source != workspace.locations_csv.resolve():
                shutil.copy2(source, workspace.locations_csv)
            with connect(workspace) as conn:
                count = seed_locations(workspace.locations_csv, conn)
                export_page_outputs(conn, workspace)
            print(f"Seeded/read {count:,} location rows from {workspace.locations_csv}")
            print_status(workspace)
            return 0

        if command == "reconcile":
            listings, profiles = reconcile_existing_files(workspace)
            print(f"Reconciled {listings:,} listing HTML file(s), {profiles:,} profile HTML file(s).")
            print_status(workspace)
            return 0

        if command == "collect-listings":
            selectors = tuple(args.specialty)
            resolved_phase = args.phase if args.phase != "auto" else choose_listing_phase(workspace, selectors)
            step_started = time.monotonic()
            count = collect_listing_batch(
                workspace,
                limit=args.limit,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout,
                stagger_seconds=args.stagger,
                poll_seconds=args.poll,
                page_check_ms=args.page_check_ms,
                stable_checks=args.stable_checks,
                page_max_wait_ms=args.page_max_wait_ms,
                controller_poll_ms=args.controller_poll_ms,
                phase=args.phase,
                specialties=selectors,
                server_url=args.controller_url,
                runner_id=args.runner_id,
                zero_link_max_retries=args.zero_link_max_retries,
                keep_listing_html=args.keep_listing_html,
            )
            step_elapsed = time.monotonic() - step_started
            if len(selectors) == 1 and args.runner_id == "single" and resolved_phase in {"seed", "pagination"}:
                append_specialty_runtime(
                    workspace,
                    specialty=resolve_specialty(workspace, selectors[0]),
                    step="base_listings" if resolved_phase == "seed" else "pagination",
                    phase=resolved_phase,
                    elapsed_seconds=step_elapsed,
                    processed=count,
                    runner_mode="single",
                )
            print(f"Processed this run: {count:,} listing page(s)")
            print(f"Elapsed this step : {_format_runtime(step_elapsed)}")
            print_status(workspace)
            return 0

        if command == "perf-report":
            report = performance_report(workspace, command=args.command, last=args.last)
            print("=" * 72)
            print("CRAWL PERFORMANCE REPORT")
            print("=" * 72)
            print(f"Command              : {report['command']}")
            print(f"Recent runs used     : {report['runs']}")
            print(f"Median throughput    : {report['pages_per_minute']:.2f} pages/min")
            print(f"Pending work         : {report['pending']:,}")
            if report['eta_minutes'] is None:
                print("ETA at recent rate   : unavailable (run a measured batch first)")
            else:
                hours = report['eta_minutes'] / 60
                print(f"ETA at recent rate   : {report['eta_minutes']:.1f} min ({hours:.2f} h)")
            print(f"Run metrics CSV      : {workspace.output_dir / 'crawl_run_metrics.csv'}")
            print(f"Page metrics CSV     : {workspace.output_dir / 'capture_performance.csv'}")
            print("=" * 72)
            return 0

        if command == "trace":
            with connect(workspace) as conn:
                before = conn.execute(
                    "SELECT COUNT(*) n FROM pages WHERE trace_status='parsed'"
                ).fetchone()["n"]
            log_path = workspace.output_dir / "trace_progress.log"

            def trace_log(event: dict) -> None:
                from datetime import datetime

                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                kind = event.get("event")
                if kind == "start":
                    message = (
                        f"START selected={event['selected_total']:,} "
                        f"available={event['candidate_total']:,} "
                        f"limit={event['limit'] if event['limit'] is not None else 'all'} "
                        f"batch_size={event['batch_size']:,} "
                        f"delete_html={event['delete_html']}"
                    )
                elif kind == "batch":
                    eta = event.get("eta_seconds")
                    eta_text = "n/a" if eta is None else f"{eta / 60:.1f}m"
                    message = (
                        f"PROGRESS {event['processed']:,}/{event['selected_total']:,} "
                        f"parsed={event['parsed']:,} missing={event['missing']:,} "
                        f"occurrences={event['occurrences']:,} deleted={event['deleted']:,} "
                        f"rate={event['rate_files_per_second']:.2f} files/s "
                        f"elapsed={event['elapsed_seconds'] / 60:.1f}m eta={eta_text}"
                    )
                elif kind == "export_start":
                    message = "EXPORT rebuilding CSV summaries from durable SQLite trace state"
                elif kind == "done":
                    message = (
                        f"DONE processed={event['processed']:,} parsed={event['parsed']:,} "
                        f"missing={event['missing']:,} occurrences={event['occurrences']:,} "
                        f"deleted={event['deleted']:,} elapsed={event['elapsed_seconds'] / 60:.1f}m "
                        f"rate={event['rate_files_per_second']:.2f} files/s"
                    )
                else:
                    message = str(event)
                line = f"[{stamp}] {message}"
                print(line, flush=True)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

            trace_df, _summary_df, missing = rebuild_listing_trace(
                workspace,
                force=args.force,
                delete_html=args.delete_html,
                limit=args.limit,
                batch_size=args.batch_size,
                progress_callback=trace_log,
            )
            with connect(workspace) as conn:
                after = conn.execute(
                    "SELECT COUNT(*) n FROM pages WHERE trace_status='parsed'"
                ).fetchone()["n"]
                html_deleted = conn.execute(
                    "SELECT COUNT(*) n FROM pages WHERE trace_html_deleted_at IS NOT NULL"
                ).fetchone()["n"]
            print("=" * 72)
            print("INCREMENTAL LISTING -> DOCTOR TRACE")
            print("=" * 72)
            print(f"Newly parsed listings  : {max(0, after - before):,}")
            print(f"Parsed listings total  : {after:,}")
            print(f"Raw doctor appearances : {len(trace_df):,}")
            print(f"Unique doctor URLs     : {trace_df['doctor_url'].nunique() if len(trace_df) else 0:,}")
            print(f"Missing unparsed HTML  : {len(missing):,}")
            print(f"HTML deleted after parse: {html_deleted:,}")
            print(f"Trace CSV              : {workspace.output_dir / 'listing_doctor_occurrences_nondedup.csv'}")
            print(f"Unique queue CSV       : {workspace.output_dir / 'unique_doctors_from_listings.csv'}")
            print(f"Progress log           : {log_path}")
            print("=" * 72)
            return 0

        if command == "repair-trace-partials":
            result = repair_trace_partials(
                workspace,
                limit=args.limit,
                delete_html=args.delete_html,
            )
            print("=" * 72)
            print("OFFLINE TRACE-PARTIAL REPAIR")
            print("=" * 72)
            print(f"Selected review pages : {result['selected']:,}")
            print(f"Successfully repaired : {result['repaired']:,}")
            print(f"Still partial         : {result['still_partial']:,}")
            print(f"Missing retained HTML : {result['missing_html']:,}")
            print(f"Trace occurrences     : {result['occurrences']:,}")
            print(f"HTML deleted          : {result['deleted']:,}")
            print("No browser/Zocdoc requests are made by this command.")
            print("=" * 72)
            return 0

        if command == "cleanup-listing-html":
            result = cleanup_parsed_listing_html(
                workspace, limit=args.limit, dry_run=args.dry_run
            )
            print("=" * 72)
            print("SAFE LISTING HTML CLEANUP")
            print("=" * 72)
            print(f"Eligible parsed files : {result['eligible']:,}")
            print(f"{'Would delete' if args.dry_run else 'Deleted'}          : {result['deleted']:,}")
            print(f"Already missing       : {result['already_missing']:,}")
            print(f"Bytes {'reclaimable' if args.dry_run else 'freed'}       : {result['bytes_freed']:,}")
            print("=" * 72)
            return 0

        if command == "smoke":
            with connect(workspace) as conn:
                trace_count = conn.execute("SELECT COUNT(*) n FROM listing_provider_trace").fetchone()["n"]
                unparsed_saved = conn.execute(
                    """SELECT COUNT(*) n FROM pages
                       WHERE status IN ('saved','valid_empty')
                         AND COALESCE(trace_status,'')<>'parsed'"""
                ).fetchone()["n"]
            if trace_count == 0 and unparsed_saved:
                print("No durable listing trace exists yet; parsing available saved listings first (offline only).")
                rebuild_listing_trace(workspace)

            url, path = collect_one_profile(
                workspace, args.doctor_url, timeout_seconds=args.timeout, server_url=args.controller_url
            )
            html = path.read_text(encoding="utf-8", errors="replace")
            with connect(workspace) as conn:
                trace_rows = trace_rows_for_doctor(conn, url)
            rows, diagnostics = parse_profile_rows(url, html, trace_rows=trace_rows)
            smoke_df = pd.DataFrame(rows, columns=REQUIRED_COLUMNS)
            smoke_path = workspace.output_dir / "smoke_profile.csv"
            smoke_df.to_csv(smoke_path, index=False, encoding="utf-8-sig")
            print("=" * 72)
            print("ONE-DOCTOR SMOKE TEST")
            print("=" * 72)
            print(f"Profile URL            : {url}")
            print(f"Profile HTML           : {path}")
            print(f"Listing appearances    : {len(trace_rows):,}")
            print(f"Parsed location rows   : {len(smoke_df):,}")
            print(f"Redux/provider found   : {diagnostics.get('redux_found')} / {diagnostics.get('provider_found')}")
            print(f"Telemedicine evidence  : {diagnostics.get('telemedicine_evidence','')}")
            print(f"Appointment source     : {diagnostics.get('can_have_appointments_source','')}")
            print(f"Smoke CSV              : {smoke_path}")
            print("=" * 72)
            if len(smoke_df):
                for column in REQUIRED_COLUMNS:
                    print(f"{column:32} {smoke_df.iloc[0][column]}")
            return 0

        if command == "collect-profiles":
            selectors = tuple(args.specialty)
            step_started = time.monotonic()
            count = collect_profile_batch(
                workspace,
                limit=args.limit,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout,
                stagger_seconds=args.stagger,
                poll_seconds=args.poll,
                page_check_ms=args.page_check_ms,
                stable_checks=args.stable_checks,
                page_max_wait_ms=args.page_max_wait_ms,
                controller_poll_ms=args.controller_poll_ms,
                server_url=args.controller_url,
                runner_id=args.runner_id,
                stale_claim_minutes=args.stale_claim_minutes,
                specialties=selectors,
            )
            step_elapsed = time.monotonic() - step_started
            if len(selectors) == 1 and args.runner_id == "single":
                append_specialty_runtime(
                    workspace,
                    specialty=resolve_specialty(workspace, selectors[0]),
                    step="profiles",
                    elapsed_seconds=step_elapsed,
                    processed=count,
                    runner_mode="single",
                )
            print(f"Processed this run: {count:,} unique doctor profile(s)")
            print(f"Elapsed this step : {_format_runtime(step_elapsed)}")
            print_status(workspace)
            return 0

        if command == "runners":
            if args.runner_command == "init":
                config = init_runners(
                    workspace, count=args.count, base_port=args.base_port, chrome_path=args.chrome_path
                )
                print(f"Configured {config['count']} runner(s) under {workspace.root / '.runners'}")
                for row in config["runners"]:
                    print(f"{row['runner_id']:12} {row['controller_url']}  {row['profile_dir']}")
                return 0
            if args.runner_command == "start":
                report = start_runners(
                    workspace,
                    chrome_path=args.chrome_path,
                    startup_wait_seconds=args.startup_wait,
                    force_restart=args.restart,
                )
                for row in report["runners"]:
                    print(
                        f"{row['runner_id']:12} server={str(row['server_ok']).lower():5} "
                        f"controller={str(row['controller_active']).lower():5} "
                        f"chrome={str(row['chrome_process_alive']).lower():5} "
                        f"port={row['port']}"
                    )
                    if not row["server_ok"]:
                        print(f"  saver log : {row['saver_log']}")
                    if not row["chrome_process_alive"] or not row["controller_active"]:
                        print(f"  chrome log: {row['chrome_log']}")
                for warning in report.get("safety_warnings", []):
                    print(
                        "SAFETY: skipped terminating "
                        f"PID {warning.get('pid')} ({warning.get('label')}); "
                        "process ownership did not match this workspace."
                    )
                return 0
            if args.runner_command == "status":
                report = runner_status(workspace)
                for row in report["runners"]:
                    print(
                        f"{row['runner_id']:12} server={str(row['server_ok']).lower():5} "
                        f"controller={str(row['controller_active']).lower():5} "
                        f"chrome={str(row['chrome_process_alive']).lower():5} "
                        f"port={row['port']} queue={row['queued_open_requests']}"
                    )
                    if not row["server_ok"]:
                        print(f"  saver log : {row['saver_log']}")
                    if not row["chrome_process_alive"] or not row["controller_active"]:
                        print(f"  chrome log: {row['chrome_log']}")
                return 0
            if args.runner_command == "stop":
                result = stop_runners(workspace, close_browsers=not args.keep_browsers)
                print(
                    f"Stopped {result['terminated_processes']} owned process(es) "
                    f"across {result['configured']} configured runner(s)."
                )
                for warning in result.get("safety_warnings", []):
                    print(
                        "SAFETY: left "
                        f"PID {warning.get('pid')} running ({warning.get('label')}); "
                        "it could not be verified as belonging to this workspace."
                    )
                return 0
            if args.runner_command == "perf":
                report = combined_performance_report(workspace)
                print("=" * 72)
                print("MULTI-RUNNER PERFORMANCE")
                print("=" * 72)
                print(f"Runners with metrics : {report['runner_count']}")
                print(f"Combined throughput  : {report['combined_pages_per_minute']:.2f} pages/min")
                print(f"Pending work         : {report['pending']:,}")
                if report["eta_minutes"] is not None:
                    print(
                        f"ETA at combined rate : {report['eta_minutes']:.1f} min "
                        f"({report['eta_minutes'] / 60:.2f} h)"
                    )
                return 0
            raise RuntimeError(f"Unhandled runner command: {args.runner_command}")

        if command == "multi-listings":
            load_runner_config(workspace)
            selectors = tuple(args.specialty)
            resolved_phase = args.phase if args.phase != "auto" else choose_listing_phase(workspace, selectors)
            result = run_multi_listings(
                workspace,
                limit=args.limit,
                concurrency_per_runner=args.concurrency_per_runner,
                timeout_seconds=args.timeout,
                stagger_seconds=args.stagger,
                poll_seconds=args.poll,
                page_check_ms=args.page_check_ms,
                stable_checks=args.stable_checks,
                page_max_wait_ms=args.page_max_wait_ms,
                controller_poll_ms=args.controller_poll_ms,
                phase=args.phase,
                zero_link_max_retries=args.zero_link_max_retries,
                keep_listing_html=args.keep_listing_html,
                specialties=selectors,
                runner_ids=tuple(args.runner),
            )
            combined = result["combined"]
            if len(args.specialty) == 1 and resolved_phase in {"seed", "pagination"}:
                append_specialty_runtime(
                    workspace,
                    specialty=resolve_specialty(workspace, args.specialty[0]),
                    step="base_listings" if resolved_phase == "seed" else "pagination",
                    phase=resolved_phase,
                    elapsed_seconds=result["elapsed_seconds"],
                    processed=result.get("latest_processed_sum", 0),
                    runner_mode=f"multi-{result['runner_count']}",
                )
            print("=" * 72)
            print("MULTI-RUNNER LISTING RUN")
            print("=" * 72)
            print(f"Runner count         : {result['runner_count']}")
            print(f"Requested page limit : {result['requested_limit']:,}")
            print(f"Combined throughput  : {combined['combined_pages_per_minute']:.2f} pages/min")
            print(f"Elapsed this step    : {_format_runtime(result['elapsed_seconds'])}")
            timing_summary = _format_stage_timing_summary(combined.get("avg_stage_timings"), profile=False)
            if timing_summary:
                print(f"Avg stage timing/page: {timing_summary}")
            print(f"Pending work         : {combined['pending']:,}")
            if result["failures"]:
                print(f"Runner failures      : {len(result['failures'])}")
                for failure in result["failures"]:
                    print(f"  {failure['runner_id']}: {failure['log']}")
                return 1
            print_status(workspace)
            return 0

        if command == "multi-profiles":
            load_runner_config(workspace)
            result = run_multi_profiles(
                workspace,
                limit=args.limit,
                concurrency_per_runner=args.concurrency_per_runner,
                timeout_seconds=args.timeout,
                stagger_seconds=args.stagger,
                poll_seconds=args.poll,
                page_check_ms=args.page_check_ms,
                stable_checks=args.stable_checks,
                page_max_wait_ms=args.page_max_wait_ms,
                controller_poll_ms=args.controller_poll_ms,
                stale_claim_minutes=args.stale_claim_minutes,
                specialties=tuple(args.specialty),
                runner_ids=tuple(args.runner),
            )
            combined = result["combined"]
            if len(args.specialty) == 1:
                append_specialty_runtime(
                    workspace,
                    specialty=resolve_specialty(workspace, args.specialty[0]),
                    step="profiles",
                    elapsed_seconds=result["elapsed_seconds"],
                    processed=result.get("latest_processed_sum", 0),
                    runner_mode=f"multi-{result['runner_count']}",
                )
            print("=" * 72)
            print("MULTI-RUNNER PROFILE RUN")
            print("=" * 72)
            print(f"Runner count          : {result['runner_count']}")
            print(f"Requested profile limit: {result['requested_limit']:,}")
            print(f"Combined throughput   : {combined['combined_pages_per_minute']:.2f} profiles/min")
            print(f"Elapsed this step     : {_format_runtime(result['elapsed_seconds'])}")
            timing_summary = _format_stage_timing_summary(combined.get("avg_stage_timings"), profile=True)
            if timing_summary:
                print(f"Avg stage timing/profile: {timing_summary}")
            print(f"Pending profiles      : {combined['pending']:,}")
            if result["failures"]:
                print(f"Runner failures       : {len(result['failures'])}")
                for failure in result["failures"]:
                    print(f"  {failure['runner_id']}: {failure['log']}")
                return 1
            print_status(workspace)
            return 0

        if command == "specialty-runtime":
            report = specialty_runtime_report(workspace, args.specialty)
            specialty = report["specialty"]
            print("=" * 72)
            print("SPECIALTY RUNTIME REPORT")
            print("=" * 72)
            print(f"Specialty             : {specialty['specialty_name']} ({specialty['specialty_slug']})")
            print(f"Location targets      : {report['targets']:,}")
            print(f"Unique providers seen : {report['providers']:,}")
            print(f"New-to-run providers  : {report['first_seen_providers']:,}")
            print("-")
            labels = {
                "target_discovery": "Target discovery",
                "base_listings": "Base listings",
                "pagination": "Pagination",
                "profiles": "Profiles",
            }
            for row in report["steps"]:
                projected = _format_runtime(row["projected_full_seconds"])
                measured = _format_runtime(row["measured_elapsed_seconds"])
                print(
                    f"{labels[row['step']]:18} measured={measured:>8} | "
                    f"processed={row['measured_processed']:,}/{row['expected_count']:,} | "
                    f"rate={row['rate_per_minute']:.2f}/min | projected={projected}"
                )
            print("-")
            print(f"Projected total       : {_format_runtime(report['projected_total_seconds'])}")
            print(f"Runtime CSV           : {report['runtime_csv']}")
            print("=" * 72)
            return 0

        if command == "retry-zero-listings":
            result = audit_zero_link_listings(workspace, max_retries=args.max_retries)
            print("=" * 72)
            print("ZERO-LINK LISTING AUDIT")
            print("=" * 72)
            print(f"Examined           : {result.get('examined', 0):,}")
            print(f"Valid empty        : {result.get('valid_empty', 0):,}")
            print(f"Retry next run     : {result.get('retry_pending', 0):,}")
            print(f"Needs review       : {result.get('review', 0):,}")
            print(f"Still valid/saved  : {result.get('saved', 0):,}")
            print(f"Quarantine folder  : {workspace.rejected_listing_dir}")
            print("=" * 72)
            print_status(workspace)
            return 0

        if command == "retry":
            count = reset_statuses(workspace, args.target, tuple(args.statuses))
            print(f"Reset {count:,} {args.target} row(s) to pending.")
            if workspace.blocked_flag.exists():
                print(f"Note: {workspace.blocked_flag} still exists. Review normal site access before collecting again.")
            return 0

        if command == "export":
            result = export_final(
                workspace,
                allow_partial=args.allow_partial,
                source_zip_mode=args.source_zip_mode,
                write_derived=not args.no_derived,
            )
            print("=" * 72)
            print("FINAL EXPORT")
            print("=" * 72)
            for key, value in result.items():
                print(f"{key:28} {value}")
            print("=" * 72)
            return 0

        if command == "derive":
            result = derive_outputs(workspace)
            for key, value in result.items():
                print(f"{key:28} {value}")
            return 0

        if command == "zip-input":
            input_path = args.input.expanduser().resolve() if args.input else None
            result = build_scraped_zip_inputs(workspace, input_path=input_path)
            print("=" * 72)
            print("SCRAPED ZIP INPUT EXPORT")
            print("=" * 72)
            for key, value in result.items():
                print(f"{key:28} {value}")
            print("=" * 72)
            return 0

        if command == "import-legacy":
            result = import_legacy(args.source, workspace, force=args.force)
            for key, value in result.items():
                print(f"{key:20} {value:,}")
            print(f"Imported into: {workspace.root}")
            return 0

        raise RuntimeError(f"Unhandled command: {command}")

    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to a clean exit code
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
