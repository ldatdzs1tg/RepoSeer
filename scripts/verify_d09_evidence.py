"""Cross-check D09 mapping candidates against PyPI and original GH Archive objects."""

import argparse
import base64
import gzip
import hashlib
import json
import re
import tempfile
import time
import zlib
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from scripts.audit_d09_archive import BUCKET_URL, OBJECT_NAME
from scripts.build_d09_reviewed import load_universe
from scripts.collect_d09_batch import (
    ACTIVITY_TYPES,
    CUTOFFS,
    WINDOW_START,
    archive_timestamp,
    chunked,
    parse_rows,
    query_rows,
    sql_list,
    sql_time,
)
from scripts.collect_d09_pilot import (
    provider_time,
    pypi_json,
    run_lock,
    source_links,
    stored_source_matches,
)
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collectors.package.repository_mapping import _root_url
from reposeer.contracts.identity import (
    add_calendar_months,
    checked_timestamp,
    format_utc_timestamp,
    normalize_pypi_name,
    package_id,
    parse_repo_id,
    repo_id,
)
from reposeer.exceptions import HTTPClientError

MAX_OBJECT_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
BLOCK_BYTES = 1024 * 1024
MAX_DOWNLOAD_SECONDS = 120


def load_candidates(batch_dir: Path, subjects: set[str]) -> list[dict]:
    universe = load_universe(batch_dir)
    seen, candidates = set(), []
    for path in sorted(batch_dir.glob("shard-*/review.jsonl")):
        for row in parse_rows(path.read_bytes()):
            key = (row["package_id"], row["cutoff_at"])
            if key not in universe or key in seen:
                raise ValueError(f"duplicate or out-of-cohort candidate: {key}")
            seen.add(key)
            if row["mapping_review"] != "single_id_observed" or (
                subjects and key[0] not in subjects
            ):
                continue
            ids, urls = row["observed_repo_ids"], row["explicit_source_urls"]
            if (
                len(ids) != 1
                or len(urls) != 1
                or not (
                    universe[key] <= checked_timestamp(row["distribution_uploaded_at"]) < key[1]
                )
            ):
                raise ValueError(f"invalid mapping candidate: {key}")
            if not re.fullmatch(r"[0-9a-f]{64}", row["distribution_sha256"]) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,199}", row["source_version"]
            ):
                raise ValueError(f"invalid PyPI revision: {key}")
            candidates.append(
                {
                    "package_id": key[0],
                    "cutoff_at": key[1],
                    "source_version": row["source_version"],
                    "distribution_uploaded_at": row["distribution_uploaded_at"],
                    "distribution_sha256": row["distribution_sha256"],
                    "repo_id": repo_id(parse_repo_id(ids[0])),
                    "repo_url": _root_url(urls[0]),
                }
            )
    if seen != set(universe):
        raise ValueError("batch is incomplete; resume collection before verifying evidence")
    return sorted(candidates, key=lambda row: (row["package_id"], row["cutoff_at"]))


def pypi_evidence(http: HTTPClient, directory: Path, row: dict) -> tuple[dict, datetime]:
    name = normalize_pypi_name(row["package_id"].removeprefix("pypi:"))
    version = row["source_version"]
    url = f"https://pypi.org/pypi/{name}/{quote(version, safe='')}/json"
    key = hashlib.sha256(url.encode()).hexdigest()
    receipt_path = directory / "pypi" / f"{key}.json"
    source = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
    if source.get("url") == url and stored_source_matches(directory, source):
        payload = json.loads((directory / source["path"]).read_bytes())
    else:
        payload, source = pypi_json(http, url, directory, f"pypi/{key}-release")
        write_json_atomic(receipt_path, source)
    info = payload.get("info")
    if (
        not isinstance(info, dict)
        or package_id(info.get("name")) != row["package_id"]
        or info.get("version") != version
    ):
        raise ValueError("pypi_identity_or_version_mismatch")
    if set(source_links(info)) != {row["repo_url"]}:
        raise ValueError("pypi_explicit_source_mismatch")
    files = payload.get("urls")
    if not isinstance(files, list):
        raise ValueError("pypi_release_files_missing")
    cutoff = provider_time(row["cutoff_at"])
    for artifact in files:
        digests = artifact.get("digests") if isinstance(artifact, dict) else None
        if not isinstance(digests, dict) or digests.get("sha256") != row["distribution_sha256"]:
            continue
        uploaded = provider_time(artifact.get("upload_time_iso_8601"))
        if format_utc_timestamp(uploaded) == row["distribution_uploaded_at"] and uploaded < cutoff:
            return {**source, "artifact_uploaded_at": artifact["upload_time_iso_8601"]}, uploaded
    raise ValueError("pypi_artifact_digest_or_upload_time_mismatch")


def locate_hours(http: HTTPClient, directory: Path, rows: list[dict]) -> dict:
    pairs = {(row["repo_id"], row["repo_url"].removeprefix("https://github.com/")) for row in rows}
    results = defaultdict(set)
    for index, batch in enumerate(chunked(sorted({identity for identity, _ in pairs}), 25)):
        names = {name for identity, name in pairs if identity in batch}
        query = f"""SELECT repo_id, lower(repo_name) AS name,
       toString(argMax(file_time, tuple(event_type = 'ReleaseEvent', created_at, file_time)), 'UTC') AS preferred_hour,
       toString(min(file_time), 'UTC') AS first_hour
FROM github.github_events
WHERE repo_id IN ({sql_list(map(parse_repo_id, batch))}) AND lower(repo_name) IN ({sql_list(names)})
  AND event_type IN ({sql_list(ACTIVITY_TYPES)})
  AND created_at >= {sql_time(WINDOW_START)} AND created_at <= {sql_time(CUTOFFS[-1])}
GROUP BY repo_id, name, toYYYYMM(file_time, 'UTC')
ORDER BY repo_id, name, preferred_hour
FORMAT JSONEachRow"""
        for item in query_rows(http, directory, f"archive-locations-{index:03d}", query):
            pair = (repo_id(item["repo_id"]), item["name"])
            if pair[0] not in batch or pair[1] not in names:
                raise ValueError("archive locator returned an out-of-scope row")
            if pair not in pairs:
                continue
            for field in ("preferred_hour", "first_hour"):
                hour = archive_timestamp(item[field])
                if WINDOW_START <= hour <= CUTOFFS[-1] and hour.endswith(":00:00.000Z"):
                    results[pair].add(hour)
    return results


def verified_revision(directory: Path, hour: str, revision: dict, listings: dict) -> dict:
    name = revision["name"]
    match = OBJECT_NAME.fullmatch(name)
    if (
        match is None
        or f"{match[1]}T{int(match[2]):02d}:00:00.000Z" != hour
        or not re.fullmatch(r"[1-9][0-9]*", revision["generation"])
        or not 0 < int(revision["size"]) <= MAX_OBJECT_BYTES
        or not revision.get("md5Hash")
        or provider_time(revision["timeCreated"]) < provider_time(hour)
    ):
        raise ValueError("invalid_or_oversized_archive_revision")
    relative = revision["inventory_receipt"]
    if not re.fullmatch(r"sources/[0-9]{4}-[0-9]{2}\.json", relative):
        raise ValueError("invalid_inventory_receipt_path")
    if relative not in listings:
        receipt = json.loads((directory / relative).read_bytes())
        if not receipt.get("url", "").startswith(f"{BUCKET_URL}?") or not stored_source_matches(
            directory, receipt
        ):
            raise ValueError("inventory_receipt_checksum_mismatch")
        body = json.loads((directory / receipt["path"]).read_bytes())
        listings[relative] = ({item["name"]: item for item in body["items"]}, receipt)
    items, receipt = listings[relative]
    if items.get(name) != {k: v for k, v in revision.items() if k != "inventory_receipt"}:
        raise ValueError("inventory_revision_does_not_match_original_listing")
    return receipt


def scan_archive(path: Path, revision: dict, names: set[str]) -> tuple[str, list[bytes]]:
    """Validate original compressed bytes, then keep the latest eligible event per name and ID."""
    md5, sha = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    archive_at = provider_time(revision["timeCreated"])
    with path.open("rb") as file:
        total = 0
        while block := file.read(BLOCK_BYTES):
            total += len(block)
            if total > int(revision["size"]):
                raise ValueError("archive_object_size_mismatch")
            md5.update(block)
            sha.update(block)
        if (
            total != int(revision["size"])
            or base64.b64encode(md5.digest()).decode() != revision["md5Hash"]
        ):
            raise ValueError("archive_object_size_or_md5_mismatch")
        file.seek(0)
        hits = {}
        expanded = 0
        with gzip.GzipFile(fileobj=file) as stream:
            while line := stream.readline(MAX_LINE_BYTES + 1):
                expanded += len(line)
                if expanded > MAX_EXPANDED_BYTES or len(line) > MAX_LINE_BYTES:
                    raise ValueError("archive_decoding_limit")
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("invalid_archive_event")
                repo = event.get("repo")
                if not isinstance(repo, dict):
                    continue
                name = str(repo.get("name", "")).casefold()
                if name not in names or event.get("type") not in ACTIVITY_TYPES:
                    continue
                key = (name, repo_id(str(repo.get("id"))))
                event_at = provider_time(event.get("created_at"))
                if event_at <= archive_at and (key not in hits or event_at > hits[key][0]):
                    hits[key] = (event_at, line)
        return sha.hexdigest(), [hits[key][1] for key in sorted(hits)]


def archive_evidence(
    client: httpx.Client,
    directory: Path,
    revision: dict,
    names: set[str],
    local: Path | None,
) -> tuple[dict, list[bytes]]:
    key = f"{revision['name']}.{revision['generation']}"
    path = directory / "archives" / f"{key}.json"
    source = json.loads(path.read_bytes()) if path.exists() else {}
    if (
        source.get("names") == sorted(names)
        and source.get("revision") == revision
        and stored_source_matches(directory, source)
    ):
        return source, (directory / source["path"]).read_bytes().splitlines(keepends=True)

    url = f"{BUCKET_URL}/{revision['name']}?" + urlencode(
        {"alt": "media", "generation": revision["generation"]}
    )
    if local is not None:
        archive_sha, events = scan_archive(local, revision, names)
    else:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory, suffix=".part", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
                with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                    response.raise_for_status()
                    if response.headers.get("x-goog-generation") != revision["generation"]:
                        raise ValueError("archive_response_generation_mismatch")
                    total = 0
                    for block in response.iter_raw():
                        if time.monotonic() > deadline:
                            raise ValueError("archive_download_time_limit")
                        total += len(block)
                        if total > int(revision["size"]):
                            raise ValueError("archive_object_size_mismatch")
                        temporary.write(block)
            archive_sha, events = scan_archive(temporary_path, revision, names)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    body = b"".join(line if line.endswith(b"\n") else line + b"\n" for line in events)
    digest = hashlib.sha256(body).hexdigest()
    raw_path = f"archives/{key}-{digest}.jsonl"
    atomic_bytes(directory / raw_path, body, immutable=False)
    source = {
        "url": url,
        "path": raw_path,
        "content_sha256": digest,
        "archive_sha256": archive_sha,
        "revision": revision,
        "names": sorted(names),
        "retrieved_at": format_utc_timestamp(datetime.now(UTC)),
    }
    write_json_atomic(path, source)
    return source, body.splitlines(keepends=True)


def rounded_up_time(value: datetime) -> str:
    remainder = value.microsecond % 1000
    return format_utc_timestamp(value + timedelta(microseconds=(1000 - remainder) % 1000))


def matching_original(row: dict, source: dict, events: list[bytes]) -> dict | None:
    slug = row["repo_url"].removeprefix("https://github.com/")
    cutoff = provider_time(row["cutoff_at"])
    start = provider_time(add_calendar_months(row["cutoff_at"], -12))
    archive_at = provider_time(source["revision"]["timeCreated"])
    matches = []
    for line in events:
        event = json.loads(line)
        if event["repo"]["name"].casefold() != slug:
            continue
        event_at = provider_time(event["created_at"])
        if start < event_at <= archive_at <= cutoff:
            identity = repo_id(str(event["repo"]["id"]))
            if identity != row["repo_id"]:
                raise ValueError("original_archive_repo_id_conflict")
            event_id = str(event.get("id"))
            if not re.fullmatch(r"[1-9][0-9]*", event_id):
                raise ValueError("invalid_original_event_id")
            matches.append((event_at, event_id, line))
    if matches:
        event_at, event_id, line = min(matches)
        return {
            "event_id": event_id,
            "event_type": json.loads(line)["type"],
            "event_at": format_utc_timestamp(event_at),
            "record_sha256": hashlib.sha256(line.rstrip(b"\r\n")).hexdigest(),
        }
    return None


def verify_batch(args, rows: list[dict], inventory: dict) -> tuple[list[dict], int]:
    directory = args.output_dir
    results, listings, archives = [], {}, {}
    unavailable_releases = {}
    downloads = 0
    names = {row["repo_url"].removeprefix("https://github.com/") for row in rows}
    with (
        HTTPClient(timeout=45, max_retries=2) as http,
        httpx.Client(timeout=httpx.Timeout(45, connect=15), follow_redirects=False) as client,
    ):
        locations = locate_hours(http, directory, rows)
        for row in rows:
            result = {**row, "official_outcome_review": "required"}
            results.append(result)
            release = (row["package_id"], row["source_version"])
            if release in unavailable_releases:
                result.update(
                    status="pypi_evidence_unverified", reason=unavailable_releases[release]
                )
                continue
            try:
                pypi_source, uploaded = pypi_evidence(http, directory, row)
            except (HTTPClientError, OSError, ValueError) as error:
                if isinstance(error, HTTPClientError):
                    unavailable_releases[release] = str(error)
                result.update(status="pypi_evidence_unverified", reason=str(error))
                continue
            result["pypi_source"] = pypi_source
            slug = row["repo_url"].removeprefix("https://github.com/")
            start = add_calendar_months(row["cutoff_at"], -12)
            cutoff = provider_time(row["cutoff_at"])
            hours = [
                hour
                for hour in locations.get((row["repo_id"], slug), ())
                if start < hour <= row["cutoff_at"]
                and hour in inventory
                and provider_time(inventory[hour]["timeCreated"]) <= cutoff
            ]
            # Prefer an hour already scanned, then one close to the release upload.
            hours.sort(
                key=lambda hour: (hour not in archives, abs(provider_time(hour) - uploaded), hour)
            )
            result["status"] = (
                "archive_evidence_unverified" if hours else "no_pre_cutoff_archive_revision"
            )
            for hour in hours[:3]:
                revision = inventory[hour]
                try:
                    listing = verified_revision(args.inventory_dir, hour, revision, listings)
                    if hour not in archives:
                        local = args.archive_dir / revision["name"] if args.archive_dir else None
                        local = local if local is not None and local.is_file() else None
                        checkpoint = (
                            directory
                            / "archives"
                            / f"{revision['name']}.{revision['generation']}.json"
                        )
                        saved = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else {}
                        reusable = (
                            saved.get("names") == sorted(names)
                            and saved.get("revision") == revision
                            and stored_source_matches(directory, saved)
                        )
                        if not reusable and local is None:
                            if downloads >= args.max_downloads:
                                result.update(status="download_budget_reached")
                                continue
                            downloads += 1
                            print(f"Verifying original {revision['name']}", flush=True)
                        try:
                            archives[hour] = archive_evidence(
                                client, directory, revision, names, local
                            )
                        except (
                            httpx.HTTPError,
                            OSError,
                            ValueError,
                            EOFError,
                            zlib.error,
                        ) as error:
                            archives[hour] = str(error)
                    observed = archives[hour]
                    if isinstance(observed, str):
                        result["reason"] = observed
                        continue
                    source, events = observed
                    match = matching_original(row, source, events)
                except (OSError, ValueError) as error:
                    result["reason"] = str(error)
                    if str(error) == "original_archive_repo_id_conflict":
                        result["status"] = "conflicting_original_repo_id"
                        break
                    continue
                if match is None:
                    result["reason"] = "original_archive_event_not_found"
                    continue
                result.pop("reason", None)
                result.update(
                    status="mapping_sources_matched",
                    scope_quality="repository_only",
                    repo_group_id=row["repo_id"],
                    evidence_available_at=rounded_up_time(
                        max(uploaded, provider_time(revision["timeCreated"]))
                    ),
                    archive_source=source,
                    archive_event=match,
                    inventory_listing_sha256=listing["content_sha256"],
                )
                break
    return results, downloads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--inventory-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--package", action="append", default=[], help="Limit to this PyPI package; repeatable"
    )
    parser.add_argument(
        "--archive-dir", type=Path, help="Optional directory of local GH Archive gzip files"
    )
    parser.add_argument(
        "--max-downloads", type=int, default=6, help="Maximum new archive downloads per invocation"
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        if args.max_downloads < 0 or (
            args.archive_dir is not None and not args.archive_dir.is_dir()
        ):
            raise ValueError(
                "expected nonnegative download budget and an existing archive directory"
            )
        subjects = {package_id(value.removeprefix("pypi:")) for value in args.package}
        rows = load_candidates(args.batch_dir, subjects)
        if not rows:
            raise ValueError("no single-ID mapping candidates for the requested packages")
        inventory_body = (args.inventory_dir / "inventory.json").read_bytes()
        inventory = json.loads(inventory_body)
        config = {
            "version": 1,
            "candidate_sha256": hashlib.sha256(
                json.dumps(rows, sort_keys=True).encode()
            ).hexdigest(),
            "inventory_sha256": hashlib.sha256(inventory_body).hexdigest(),
        }
        args.output_dir.mkdir(parents=True, exist_ok=args.resume)
        with run_lock(args.output_dir):
            config_path = args.output_dir / "config.json"
            if config_path.exists() and json.loads(config_path.read_bytes()) != config:
                raise ValueError("resume inputs differ from the original verification run")
            write_json_atomic(config_path, config)
            rows, downloads = verify_batch(args, rows, inventory)
            atomic_bytes(
                args.output_dir / "mapping_evidence.jsonl",
                b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\n" for row in rows),
                immutable=False,
            )
            write_json_atomic(
                args.output_dir / "summary.json",
                {
                    "candidate_rows": len(rows),
                    "archive_downloads_this_invocation": downloads,
                    "statuses": dict(Counter(row["status"] for row in rows)),
                    "official_outcomes_verified": 0,
                },
            )
    except (HTTPClientError, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"D09 evidence verification stopped: {error}\n")
    print(f"D09 source verification: {args.output_dir.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
