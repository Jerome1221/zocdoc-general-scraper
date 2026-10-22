"""Read-only specialty delivery export using the orthopedic submission schema."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

from zocdoc_ortho.profile import parse_profile_rows
from zocdoc_ortho.profile_availability import profile_unavailable_reason
from zocdoc_ortho.schema import LOCATION_LEVEL_COLUMNS, REQUIRED_COLUMNS
from zocdoc_ortho.trace import trace_rows_for_doctor
from zocdoc_ortho.urls import safe_slug
from zocdoc_ortho.utils import tri_bool
from zocdoc_ortho.workspace import Workspace
from zocdoc_ortho.zip_inputs import normalize_state, normalize_zip

csv.field_size_limit(64 * 1024 * 1024)

DEFAULT_SPECIALTIES = [
    "cardiologists", "radiologists", "oncologists", "neurosurgeons",
    "urologists", "gastroenterologists", "physical-therapists",
    "ear-nose-throat-doctors",
]
UNIQUE_COLUMNS = [c for c in REQUIRED_COLUMNS if c not in LOCATION_LEVEL_COLUMNS]
TERMINAL = {"saved", "valid_empty", "unavailable", "retired"}


def clean(value):
    return "" if value is None else str(value).strip()


def nested(value):
    return json.loads(value) if isinstance(value, str) and value.strip() else value


def expand_profiles(payload, doctor_url):
    profiles = json.loads(payload)
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Missing canonical profile rows")
    result = []
    for profile in profiles:
        if not isinstance(profile, dict) or not clean(profile.get("full_name")):
            raise ValueError("Invalid or unnamed canonical profile")
        locations = nested(profile.get("locations", []))
        if not isinstance(locations, list) or any(not isinstance(x, dict) for x in locations):
            raise ValueError("Invalid nested locations")
        # Migrated unique-doctor CSVs contain only provider fields. Expand their offices.
        flattened = any(k in profile for k in ("location_id", "address_line_1", "is_virtual_location"))
        if not flattened and not locations:
            raise ValueError("Provider-only record has no location details")
        for location in ([None] if flattened else locations):
            row = {c: profile.get(c, "") for c in REQUIRED_COLUMNS}
            if location is not None:
                for c in LOCATION_LEVEL_COLUMNS:
                    if c not in ("source_zip", "source_zip_state"):
                        row[c] = location.get(c, "")
                row["practice_name"] = location.get("practice_name") or profile.get("practice_name", "")
                row["source_zip"] = row["postal_code"]
                row["source_zip_state"] = row["state"]
                row["num_locations"] = len(locations)
            row["profile_url"] = profile.get("profile_url") or doctor_url
            row["locations"] = locations
            affiliations = nested(profile.get("hospital_affiliations", []))
            if not isinstance(affiliations, list):
                raise TypeError("Invalid hospital affiliations")
            row["hospital_affiliations"] = affiliations
            for c in ("npi", "provider_id", "monolith_id", "postal_code", "source_zip", "phone", "location_id"):
                row[c] = clean(row[c])
            for c in ("accepts_new_patients", "offers_telemedicine", "only_sees_children",
                      "can_have_appointments", "is_virtual_location", "has_new_patient_availability"):
                row[c] = tri_bool(row[c])
            result.append(row)
    return result


def identity(row):
    return clean(row.get("provider_id")) or clean(row.get("npi")) or clean(row.get("profile_url"))


def location_key(row):
    provider = identity(row)
    key = clean(row.get("provider_location_key"))
    if key:
        return (provider, key, clean(row.get("location_id")),
                clean(row.get("address_line_1")).lower(), clean(row.get("address_line_2")).lower(),
                clean(row.get("city")).lower(), clean(row.get("state")).upper(),
                clean(row.get("postal_code")), row.get("is_virtual_location"))
    return (provider, clean(row.get("location_id")), clean(row.get("address_line_1")).lower(),
            clean(row.get("address_line_2")).lower(), clean(row.get("city")).lower(),
            clean(row.get("state")).upper(), clean(row.get("postal_code")), row.get("is_virtual_location"))


def csv_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return "" if value is None else value


def write_pair(directory, name, columns, rows):
    with (directory / (name + ".csv")).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows({c: csv_value(r.get(c)) for c in columns} for r in rows)
    with (directory / (name + ".json")).open("w", encoding="utf-8") as f:
        f.write("[\n")
        for index, row in enumerate(rows):
            f.write("  " + json.dumps({c: row.get(c) for c in columns}, ensure_ascii=False, allow_nan=False))
            f.write(",\n" if index < len(rows) - 1 else "\n")
        f.write("]\n")
    # Confirm both deliveries have the same records after CSV type serialization.
    with (directory / (name + ".csv")).open(encoding="utf-8-sig", newline="") as f:
        actual = csv.DictReader(f)
        for row in rows:
            expected = {c: str(csv_value(row.get(c))) for c in columns}
            if next(actual, None) != expected:
                raise ValueError(f"CSV round-trip mismatch: {name}")
        assert next(actual, None) is None
    with (directory / (name + ".json")).open(encoding="utf-8") as f:
        assert next(f).strip() == "["
        for row in rows:
            assert json.loads(next(f).strip().removesuffix(",")) == {c: row.get(c) for c in columns}
        assert next(f).strip() == "]"
        assert not f.read()


def file_sha256(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def reviewed_resolutions(conn, mhtml):
    """Apply the three user-reviewed delivery decisions without database writes."""
    resolutions = {}
    john = "https://www.zocdoc.com/doctor/john-heiss-md-bethesda"
    resolutions[john] = {"status": "unavailable", "reason":
        "User-verified screenshot: Not Found. Redirecting to https://www.zocdoc.com",
        "source": "codex-clipboard-e7561f89-4fa8-4b51-b46c-8261cb11eba3.png"}
    david = conn.execute("""SELECT c.doctor_url,c.profile_data_json,d.saved_at
        FROM canonical_provider_records c JOIN doctor_profiles d ON d.doctor_url=c.doctor_url
        WHERE c.doctor_url IN (?,?) AND d.status='saved'""", (
        "https://www.zocdoc.com/doctor/david-epstein-md-863404",
        "https://www.zocdoc.com/doctor/david-epstein-md-888823")).fetchall()
    if not david or any(not r["saved_at"] for r in david):
        raise ValueError("Cannot establish David's capture chronology")
    latest = max(david, key=lambda r: datetime.fromisoformat(r["saved_at"]))
    latest_rows = expand_profiles(latest["profile_data_json"], latest["doctor_url"])
    if {r["npi"] for r in latest_rows} != {"1598262511"}:
        raise ValueError("David identity mismatch")
    for r in david:
        resolutions[r["doctor_url"]] = {"status": "profile", "rows": latest_rows,
            "source": latest["doctor_url"], "captured_at": latest["saved_at"],
            "reason": "User requested the latest valid captured profile for conflicting details"}
    message = BytesParser(policy=policy.default).parsebytes(mhtml.read_bytes())
    mark_url = "https://www.zocdoc.com/doctor/mark-sonnenschine-do-860475"
    if message.get("Snapshot-Content-Location") != mark_url:
        raise ValueError("Unexpected MHTML source URL")
    part = next(p for p in message.walk() if p.get_content_type() == "text/html" and p.get("Content-Location") == mark_url)
    from bs4 import BeautifulSoup
    text = BeautifulSoup(part.get_content(), "html.parser").get_text(" ", strip=True)
    for evidence in ("Dr. Mark Sonnenschine, DO", "Oncologist", "1922291939",
                     "Adore Hematology and Oncology PLLC", "6201 15th Ave", "11219"):
        if evidence not in text:
            raise ValueError(f"Missing Mark evidence: {evidence}")
    old_mark = "https://www.zocdoc.com/doctor/mark-sonnenschine-do"
    trace_ids = conn.execute("SELECT DISTINCT provider_id,npi FROM listing_provider_trace WHERE doctor_url=?", (old_mark,)).fetchall()
    if len(trace_ids) != 1 or trace_ids[0]["npi"] != "1922291939":
        raise ValueError("Mark listing identity mismatch")
    location = {"address_line_1": "6201 15th Ave", "address_line_2": "#306",
        "city": "Brooklyn", "state": "NY", "postal_code": "11219", "phone": "",
        "location_id": "", "location_name": "", "is_virtual_location": False,
        "can_have_appointments": True, "has_new_patient_availability": True,
        "practice_name": "Adore Hematology and Oncology PLLC", "provider_location_key": ""}
    # Visible-only fields are reviewed explicitly; unobserved values stay unknown.
    mark = {c: "" for c in REQUIRED_COLUMNS}
    mark.update(location)
    mark.update(full_name="Dr. Mark Sonnenschine, DO", specialty_name="Oncologist",
        npi="1922291939", provider_id=trace_ids[0]["provider_id"], first_name="Mark",
        last_name="Sonnenschine", postnominal="DO", profile_url=mark_url,
        accepts_new_patients=True, offers_telemedicine=None, only_sees_children=None,
        review_count=0, hospital_affiliations=[], num_locations=1, locations=[location],
        source_zip="11219", source_zip_state="NY")
    if "first to leave a review" not in text.lower():
        raise ValueError("Cannot establish Mark's zero-review status")
    resolutions[old_mark] = {"status": "profile", "rows": [mark], "source": mhtml.name,
        "source_sha256": hashlib.sha256(mhtml.read_bytes()).hexdigest(),
        "captured_at": message.get("Date"), "reason":
        "Reviewed visible MHTML fields; NPI matched listing trace. Unobserved values remain blank/null."}
    return resolutions


def latest_alias_resolutions(conn, review_path):
    """Prefer a newer valid capture only for reviewed, identity-matched aliases."""
    result = {}
    for group in json.loads(review_path.read_text(encoding="utf-8")):
        urls = tuple(r["doctor_url"] for r in group["versions"])
        placeholders = ','.join('?' for _ in urls)
        versions = conn.execute(f"""SELECT c.doctor_url,c.profile_data_json,d.saved_at,d.status
            FROM canonical_provider_records c JOIN doctor_profiles d ON d.doctor_url=c.doctor_url
            WHERE c.doctor_url IN ({placeholders})""", urls).fetchall()
        if len(versions) != len(urls) or any(r['status'] != 'saved' or not r['saved_at'] for r in versions):
            raise ValueError("Alias capture state changed; review again")
        latest = max(versions, key=lambda r: datetime.fromisoformat(r['saved_at']))
        parsed = {r['doctor_url']: expand_profiles(r['profile_data_json'], r['doctor_url']) for r in versions}
        all_rows = [row for rows in parsed.values() for row in rows]
        if {r['provider_id'] for r in all_rows} != {group['provider_id']}:
            raise ValueError("Alias provider identity mismatch")
        npis = {r['npi'] for r in all_rows}
        if len(npis) != 1 or not next(iter(npis)):
            raise ValueError("Alias NPI identity mismatch")
        latest_rows = parsed[latest['doctor_url']]
        if any({location_key(r) for r in rows} != {location_key(r) for r in latest_rows} for rows in parsed.values()):
            raise ValueError("Alias office sets differ; manual review required")
        for url in urls:
            result[url] = {'status': 'profile', 'rows': latest_rows, 'source': latest['doctor_url'],
                'captured_at': latest['saved_at'], 'reason':
                'Newest valid captured profile chosen for reviewed aliases with matching provider ID, NPI and offices'}
    return result


def export_specialty(conn, slug, root, cohort, workspace, resolutions=None):
    status = dict(conn.execute("SELECT status,COUNT(*) FROM pages WHERE specialty_slug=? GROUP BY status", (slug,)))
    profiles = []
    for url in sorted(cohort):
        details = conn.execute("""SELECT c.profile_data_json,d.status,d.error
            FROM (SELECT ? doctor_url) t
            LEFT JOIN canonical_provider_records c ON c.doctor_url=t.doctor_url
            LEFT JOIN doctor_profiles d ON d.doctor_url=t.doctor_url""", (url,)).fetchone()
        profiles.append((url, *details))
    issues, records = [], []
    local_parsed = 0
    applied = []
    for url, payload, capture_status, error in profiles:
        resolution = (resolutions or {}).get(url)
        if resolution:
            applied.append({"doctor_url": url, **{k: v for k, v in resolution.items() if k != "rows"}})
            if resolution["status"] == "unavailable":
                issues.append({"doctor_url": url, "status": "unavailable", "reason": resolution["reason"]})
            else:
                records.extend(resolution["rows"])
            continue
        if capture_status == "unavailable":
            issues.append({"doctor_url": url, "status": "unavailable", "reason": error or "Profile unavailable"})
            continue
        try:
            records.extend(expand_profiles(payload or "[]", url))
        except (ValueError, TypeError) as exc:
            paths = [workspace.profile_dir / safe_slug(url), workspace.unavailable_profile_dir / safe_slug(url)]
            path = next((p for p in paths if p.is_file()), None)
            if path:
                try:
                    html = path.read_text(encoding="utf-8", errors="replace")
                    reason = profile_unavailable_reason(html)
                    if reason:
                        issues.append({"doctor_url": url, "status": "unavailable", "reason": reason})
                        continue
                    parsed, diagnostics = parse_profile_rows(url, html, trace_rows=trace_rows_for_doctor(conn, url), source_zip_mode="office")
                    if not diagnostics.get("provider_found") or not parsed:
                        raise ValueError("Local HTML did not contain a recognized provider")
                    records.extend(expand_profiles(json.dumps(parsed), url))
                    local_parsed += 1
                    continue
                except Exception as local_exc:  # noqa: BLE001 - report this profile as an export exception
                    exc = ValueError(f"{exc}; local HTML: {local_exc}")
            issues.append({"doctor_url": url, "status": "needs_review", "reason": str(exc)})
    unique_rows = {}
    conflicting = 0
    for row in records:
        key = location_key(row)
        if key in unique_rows and row != unique_rows[key]:
            conflicting += 1
        unique_rows.setdefault(key, row)
    records = sorted(unique_rows.values(), key=lambda r: (clean(r["full_name"]), identity(r), repr(location_key(r))))
    providers = {}
    for row in records:
        providers.setdefault(identity(row), {c: row[c] for c in UNIQUE_COLUMNS})
    physical = [r for r in records if r["is_virtual_location"] is False]
    virtual = [r for r in records if r["is_virtual_location"] is True]
    unknown = [r for r in records if r["is_virtual_location"] is None]
    zip_pairs = sorted({(normalize_state(r["state"]), normalize_zip(r["postal_code"]))
                        for r in records if normalize_state(r["state"]) and normalize_zip(r["postal_code"])})
    by_state = defaultdict(list)
    for state, zipcode in zip_pairs:
        by_state[state].append(zipcode)
    wide = [{state: values[i] if i < len(values) else "" for state, values in by_state.items()}
            for i in range(max(map(len, by_state.values()), default=0))]
    unresolved = sum(n for s, n in status.items() if s not in TERMINAL)
    ready = bool(profiles) and not unresolved and not conflicting and not any(i["status"] == "needs_review" for i in issues)
    directory = root / ("ready" if ready else "needs-review") / slug
    directory.mkdir(parents=True, exist_ok=False)
    datasets = {
        "final_providers": (REQUIRED_COLUMNS, records),
        "unique_doctors": (UNIQUE_COLUMNS, list(providers.values())),
        "doctor_locations_physical": (REQUIRED_COLUMNS, physical),
        "doctor_locations_virtual": (REQUIRED_COLUMNS, virtual),
        "zocdoc_scraped_zipcodes_long": (["state", "zip"], [{"state": s, "zip": z} for s, z in zip_pairs]),
        "zocdoc_scraped_zipcodes_by_state": (list(by_state), wide),
        "profile_exceptions": (["doctor_url", "status", "reason"], issues),
    }
    if unknown:
        datasets["doctor_locations_unknown"] = (REQUIRED_COLUMNS, unknown)
    for name, (columns, rows) in datasets.items():
        write_pair(directory, name, columns, rows)
    qa = [{"column": c, "non_empty": sum(r[c] not in (None, "", []) for r in records),
           "total_rows": len(records)} for c in REQUIRED_COLUMNS]
    write_pair(directory, "final_providers_qa", ["column", "non_empty", "total_rows"], qa)
    unavailable_pages = [{"url": u, "reason": reason} for u, reason in conn.execute(
        "SELECT url,validation_reason FROM pages WHERE specialty_slug=? AND status='unavailable' ORDER BY url", (slug,))]
    write_pair(directory, "listing_exceptions", ["url", "reason"], unavailable_pages)
    if applied:
        (directory / "reviewed_resolutions.json").write_text(json.dumps(applied, indent=2), encoding="utf-8")
    summary = {"specialty": slug, "delivery_status": "ready" if ready else "needs_review",
               "listing_statuses": status, "listing_providers": len(profiles),
               "exported_unique_providers": len(providers), "final_location_rows": len(records),
               "physical_rows": len(physical), "virtual_rows": len(virtual), "unknown_location_rows": len(unknown),
               "profile_exceptions": dict(Counter(i["status"] for i in issues)),
               "profiles_parsed_from_local_html": local_parsed,
               "conflicting_duplicate_location_rows": conflicting,
               "files": {p.name: file_sha256(p) for p in directory.iterdir()}}
    (directory / "manifest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--specialty", action="append")
    parser.add_argument("--reviewed-mark-mhtml", type=Path,
                        help="Apply user-reviewed Mark, John and David delivery resolutions")
    parser.add_argument("--latest-alias-review", type=Path)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    database = (args.workspace / "collector.db").resolve()
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    summaries = []
    try:
        slugs = args.specialty or DEFAULT_SPECIALTIES
        resolutions = reviewed_resolutions(conn, args.reviewed_mark_mhtml) if args.reviewed_mark_mhtml else {}
        if args.latest_alias_review:
            resolutions.update(latest_alias_resolutions(conn, args.latest_alias_review))
        cohorts = defaultdict(set)
        placeholders = ','.join('?' for _ in slugs)
        print("Reading specialty provenance...", flush=True)
        for slug, url in conn.execute(f"SELECT DISTINCT listing_specialty_slug,doctor_url FROM listing_provider_trace WHERE listing_specialty_slug IN ({placeholders})", slugs):
            cohorts[slug].add(url)
        for slug in args.specialty or DEFAULT_SPECIALTIES:
            summary = export_specialty(conn, slug, args.output, cohorts[slug], Workspace(args.workspace), resolutions)
            summaries.append(summary)
            print(json.dumps({k: v for k, v in summary.items() if k != "files"}), flush=True)
    finally:
        conn.rollback()
        conn.close()
    (args.output / "summary.json").write_text(json.dumps({"exported_at_utc": datetime.now(UTC).isoformat(),
        "specialties": summaries}, indent=2), encoding="utf-8")
    (args.output / "README.txt").write_text(
        "Each specialty includes matching CSV and JSON datasets.\n"
        "Provider/location files match the orthopedic 35-column schema; unique_doctors has 21 columns.\n"
        "JSON locations and hospital_affiliations are arrays; CSV stores them as JSON strings.\n"
        "Selection uses specialty listing provenance, not a strict clinical credential filter.\n"
        "A provider can appear in multiple specialty packages. Profile specialty_name is retained.\n"
        "ZIP lists describe observed provider offices, not proof of nationwide ZIP coverage.\n"
        "Unavailable profiles/pages are reported separately; unknown virtual flags are not guessed.\n"
        "needs-review folders are drafts: missing canonical details or unresolved listings must be addressed.\n"
        "Canonical JSON supplies the records. Missing details may be parsed from existing local HTML only.\n"
        "Database records and capture files were not changed. No new website requests were made.\n",
        encoding="utf-8")
    with zipfile.ZipFile(args.output.with_suffix(".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
        for path in args.output.rglob("*"):
            if path.is_file() and "needs-review" not in path.relative_to(args.output).parts:
                archive.write(path, path.relative_to(args.output))
    print("DELIVERY ZIP:", args.output.with_suffix(".zip"), flush=True)
    return 0 if all(s["delivery_status"] == "ready" for s in summaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
