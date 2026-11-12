"""Cross-check D09 mapping candidates against PyPI and original GH Archive objects."""

import argparse
import base64
import gzip
import hashlib
import json
import re
import time
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from scripts.audit_d09_archive import BUCKET_URL, OBJECT_NAME
from scripts.build_d09_reviewed import load_universe
from scripts.collect_d09_batch import (
    ACTIVITY_TYPES,
    CUTOFFS,
    IDENTITY_EVENT_TYPES,
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
from scripts.d09_archive_pool import covers_names, local_proof, names_digest, saved_proofs
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
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 120
DEFAULT_ARCHIVE_WORKERS = 4
MAX_ARCHIVE_WORKERS = 16


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
            if row.get("source_link_policy") is not None:
                if row["source_link_policy"] != "github-homepage-candidate-v1":
                    raise ValueError("unsupported historical source-link policy")
                candidates[-1]["source_link_policy"] = row["source_link_policy"]
            if row.get("archive_window_start_at") is not None:
                start = checked_timestamp(row["archive_window_start_at"])
                if start != "2018-01-01T00:00:00.000Z":
                    raise ValueError("unsupported extended archive window")
                candidates[-1]["archive_window_start_at"] = start
            if row.get("identity_event_policy") is not None:
                if row["identity_event_policy"] != "all-public-v1":
                    raise ValueError("unsupported identity event policy")
                candidates[-1]["identity_event_policy"] = row["identity_event_policy"]
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
        payload, source = pypi_json(http, url, directory, "pypi/release")
        write_json_atomic(receipt_path, source)
    info = payload.get("info")
    if (
        not isinstance(info, dict)
        or package_id(info.get("name")) != row["package_id"]
        or info.get("version") != version
    ):
        raise ValueError("pypi_identity_or_version_mismatch")
    declared_sources = source_links(info)
    if not declared_sources and row.get("source_link_policy") == "github-homepage-candidate-v1":
        try:
            declared_sources = {_root_url(info.get("home_page")): set()}
        except (TypeError, ValueError):
            pass
    if set(declared_sources) != {row["repo_url"]}:
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
    types = (
        IDENTITY_EVENT_TYPES
        if any(row.get("identity_event_policy") == "all-public-v1" for row in rows)
        else ACTIVITY_TYPES
    )
    window_start = min(
        (row.get("archive_window_start_at", WINDOW_START) for row in rows), default=WINDOW_START
    )
    for index, batch in enumerate(chunked(sorted({identity for identity, _ in pairs}), 25)):
        names = {name for identity, name in pairs if identity in batch}
        query = f"""SELECT repo_id, lower(repo_name) AS name,
       toString(argMax(file_time, tuple(event_type = 'ReleaseEvent', created_at, file_time)), 'UTC') AS preferred_hour,
       toString(min(file_time), 'UTC') AS first_hour
FROM github.github_events
WHERE repo_id IN ({sql_list(map(parse_repo_id, batch))}) AND lower(repo_name) IN ({sql_list(names)})
  AND event_type IN ({sql_list(types)})
  AND created_at >= {sql_time(window_start)} AND created_at <= {sql_time(CUTOFFS[-1])}
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
                if window_start <= hour <= CUTOFFS[-1] and hour.endswith(":00:00.000Z"):
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
                if name not in names or event.get("type") not in IDENTITY_EVENT_TYPES:
                    continue
                key = (name, repo_id(str(repo.get("id"))))
                event_at = provider_time(event.get("created_at"))
                if event_at <= archive_at and (key not in hits or event_at > hits[key][0]):
                    hits[key] = (event_at, line)
        return sha.hexdigest(), [hits[key][1] for key in sorted(hits)]


def archive_key(revision: dict) -> str:
    return f"{revision['name']}.{revision['generation']}"


def download_archive(client: httpx.Client, url: str, path: Path, revision: dict,
                     max_seconds: float = MAX_DOWNLOAD_SECONDS) -> None:
    expected_size = int(revision["size"])
    offset = path.stat().st_size if path.exists() else 0
    if offset > expected_size:
        path.unlink()
        offset = 0
    if offset == expected_size:
        return
    deadline = time.monotonic() + max_seconds
    while offset < expected_size:
        if time.monotonic() > deadline:
            raise ValueError("archive_download_time_limit")
        end = min(expected_size, offset + DOWNLOAD_CHUNK_BYTES) - 1
        headers = {"Accept-Encoding": "identity", "Range": f"bytes={offset}-{end}"}
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            if response.headers.get("x-goog-generation") != revision["generation"]:
                raise ValueError("archive_response_generation_mismatch")
            if response.status_code == 206:
                if response.headers.get("Content-Range") != f"bytes {offset}-{end}/{expected_size}":
                    raise ValueError("archive_response_range_mismatch")
            elif not (response.status_code == 200 and offset == 0 and end == expected_size - 1):
                raise ValueError("archive_response_range_mismatch")
            with path.open("ab") as temporary:
                total = offset
                for block in response.iter_raw():
                    if time.monotonic() > deadline:
                        raise ValueError("archive_download_time_limit")
                    total += len(block)
                    if total > end + 1:
                        raise ValueError("archive_object_size_mismatch")
                    temporary.write(block)
            if total != end + 1:
                raise ValueError("archive_download_incomplete")
            offset = total


def cached_archive(
    directory: Path, source: dict, names: list[str] | None = None
) -> tuple[dict, list[bytes]] | None:
    if (names is None or covers_names(source, set(names))) and stored_source_matches(
        directory, source
    ):
        return source, (directory / source["path"]).read_bytes().splitlines(keepends=True)
    return None


def archive_evidence(
    client: httpx.Client,
    directory: Path,
    revision: dict,
    names: set[str],
    local: Path | None,
    max_seconds: float = MAX_DOWNLOAD_SECONDS,
) -> tuple[dict, list[bytes]]:
    key = archive_key(revision)
    path = directory / "archives" / f"{key}.json"
    source = json.loads(path.read_bytes()) if path.exists() else {}
    if source.get("revision") == revision and source.get("identity_event_types") == sorted(
        IDENTITY_EVENT_TYPES
    ):
        cached = cached_archive(directory, source, sorted(names))
        if cached is not None:
            return cached

    url = f"{BUCKET_URL}/{revision['name']}?" + urlencode(
        {"alt": "media", "generation": revision["generation"]}
    )
    if local is not None:
        archive_sha, events = scan_archive(local, revision, names)
        partial = None
    else:
        partial = directory / f"{key}.part"
        download_archive(client, url, partial, revision, max_seconds)
        try:
            archive_sha, events = scan_archive(partial, revision, names)
        except (OSError, ValueError, EOFError, zlib.error):
            partial.unlink(missing_ok=True)
            raise

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
        "scan_names_sha256": names_digest(names),
        "identity_event_types": sorted(IDENTITY_EVENT_TYPES),
        "matched_names": sorted({json.loads(line)["repo"]["name"].casefold() for line in events}),
        "retrieved_at": format_utc_timestamp(datetime.now(UTC)),
    }
    write_json_atomic(path, source)
    if partial is not None:
        partial.unlink(missing_ok=True)
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


def apply_archive(
    source: dict,
    events: list[bytes],
    listing: dict,
    results: list[dict],
    unresolved: set[int],
    by_repo_name: dict,
) -> None:
    by_name = defaultdict(list)
    for line in events:
        by_name[json.loads(line)["repo"]["name"].casefold()].append(line)
    for name, lines in by_name.items():
        for index in by_repo_name.get(name, ()):
            if index not in unresolved:
                continue
            row = results[index]
            try:
                match = matching_original(row, source, lines)
            except ValueError as error:
                row["reason"] = str(error)
                if str(error) == "original_archive_repo_id_conflict":
                    row["status"] = "conflicting_original_repo_id"
                    unresolved.remove(index)
                continue
            if match is None:
                continue
            row.pop("reason", None)
            row.update(
                status="mapping_sources_matched",
                scope_quality="repository_only",
                repo_group_id=row["repo_id"],
                evidence_available_at=rounded_up_time(
                    max(
                        provider_time(row["pypi_source"]["artifact_uploaded_at"]),
                        provider_time(source["revision"]["timeCreated"]),
                    )
                ),
                archive_source=source,
                archive_event=match,
                inventory_listing_sha256=listing["content_sha256"],
            )
            unresolved.remove(index)


def archive_wave(
    hour_rows: dict,
    eligible: set[int],
    costs: dict[str, int],
    budget: int,
    workers: int,
) -> list[str]:
    """Prefer free proofs, then cover the most distinct pending rows per remaining byte."""
    selected = []
    remaining = eligible.copy()
    for _ in range(workers):
        best = None
        for hour, indices in hour_rows.items():
            cost = costs[hour]
            if hour in selected or (cost and not budget):
                continue
            covered = remaining & indices
            if not covered:
                continue
            score = (cost == 0, len(covered) / max(1, cost), len(covered), -cost)
            if best is None or score > best[0] or (score == best[0] and hour < best[1]):
                best = (score, hour, covered)
        if best is None:
            break
        _, hour, covered = best
        selected.append(hour)
        remaining.difference_update(covered)
        budget -= bool(costs[hour])
    return selected


def verify_batch(args, rows: list[dict], inventory: dict) -> tuple[list[dict], int]:
    directory = args.output_dir
    archive_pool = getattr(args, "archive_pool", None)
    shared_directory = archive_pool.directory if archive_pool is not None else directory
    workers = getattr(args, "archive_workers", DEFAULT_ARCHIVE_WORKERS)
    results = [{**row, "official_outcome_review": "required"} for row in rows]
    unresolved, checked = set(), set()
    by_repo_name, listings = defaultdict(list), {}
    unavailable_releases = {}
    downloads = 0
    names = {row["repo_url"].removeprefix("https://github.com/") for row in rows}
    if archive_pool is not None:
        archive_pool.names.update(names)
    scan_names = names | (archive_pool.names if archive_pool is not None else set())
    with (
        HTTPClient(timeout=45, max_retries=2) as http,
        httpx.Client(timeout=httpx.Timeout(45, connect=15), follow_redirects=False) as client,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        for index, result in enumerate(results):
            release = (result["package_id"], result["source_version"])
            if release in unavailable_releases:
                result.update(
                    status="pypi_evidence_unverified", reason=unavailable_releases[release]
                )
                continue
            try:
                pypi_source, _ = pypi_evidence(http, directory, result)
            except (HTTPClientError, OSError, ValueError) as error:
                if isinstance(error, HTTPClientError):
                    unavailable_releases[release] = str(error)
                result.update(status="pypi_evidence_unverified", reason=str(error))
                continue
            result.update(pypi_source=pypi_source, status="no_pre_cutoff_archive_revision")
            unresolved.add(index)
            slug = result["repo_url"].removeprefix("https://github.com/")
            by_repo_name[slug].append(index)

        # A checked original file can prove other cutoffs and other batches.
        before_replay = len(unresolved)
        if archive_pool is not None:
            proofs = archive_pool.proofs(names)
        else:
            proofs = saved_proofs(directory)
        replayed = 0
        for proof_directory, receipt in proofs:
            if not unresolved:
                break
            try:
                revision = receipt["revision"]
                match = OBJECT_NAME.fullmatch(revision["name"])
                if match is None:
                    continue
                hour = f"{match[1]}T{int(match[2]):02d}:00:00.000Z"
                if inventory.get(hour) != revision:
                    continue
                proof = cached_archive(proof_directory, receipt)
                if proof is None:
                    continue
                listing = verified_revision(args.inventory_dir, hour, revision, listings)
            except (KeyError, OSError, ValueError):
                # A bad cache receipt remains eligible for a fresh verification.
                continue
            broad_identity = any(
                row.get("identity_event_policy") == "all-public-v1" for row in rows
            )
            if (
                not broad_identity
                or receipt.get("identity_event_types") == sorted(IDENTITY_EVENT_TYPES)
            ) and (covers_names(receipt, scan_names) or covers_names(receipt, names)):
                checked.add(hour)
            if proof_directory != directory:
                proof = local_proof(directory, *proof, names)
            apply_archive(*proof, listing, results, unresolved, by_repo_name)
            replayed += 1
        if replayed:
            print(
                f"Reused {replayed} archive proofs for {before_replay - len(unresolved)} package-cutoffs",
                flush=True,
            )

        if not unresolved:
            return results, downloads
        locations = locate_hours(http, directory, rows)
        hour_rows = defaultdict(set)
        costs, local_paths, storage_directories = {}, {}, {}
        for index in sorted(unresolved):
            row = results[index]
            slug = row["repo_url"].removeprefix("https://github.com/")
            start = add_calendar_months(row["cutoff_at"], -12)
            cutoff = provider_time(row["cutoff_at"])
            for hour in locations.get((row["repo_id"], slug), ()):
                revision = inventory.get(hour)
                if (
                    revision is None
                    or not start < hour <= row["cutoff_at"]
                    or provider_time(revision["timeCreated"]) > cutoff
                ):
                    continue
                row["status"] = "archive_evidence_unverified"
                if hour in checked:
                    continue
                hour_rows[hour].add(index)
                if hour in costs:
                    continue
                local = (
                    args.archive_dir / f"{hour[:10]}-{int(hour[11:13])}.json.gz"
                    if args.archive_dir
                    else None
                )
                local_paths[hour] = local if local is not None and local.is_file() else None
                size = int(revision["size"])
                partial_path = directory / f"{archive_key(revision)}.part"
                storage = directory if partial_path.exists() else shared_directory
                storage_directories[hour] = storage
                partial_path = storage / partial_path.name
                partial = partial_path.stat().st_size if partial_path.exists() else 0
                if partial > size:
                    partial = 0
                costs[hour] = 0 if local_paths[hour] is not None else size - partial

        attempts = Counter()
        while hour_rows:
            eligible = {index for index in unresolved if attempts[index] < 3}
            wave = archive_wave(hour_rows, eligible, costs, args.max_downloads - downloads, workers)
            if not wave:
                if downloads >= args.max_downloads:
                    for indices in hour_rows.values():
                        for index in indices & eligible:
                            results[index]["status"] = "download_budget_reached"
                break

            scheduled = []
            for hour in wave:
                revision = inventory[hour]
                indices = hour_rows.pop(hour) & unresolved
                attempts.update(indices)
                try:
                    listing = verified_revision(args.inventory_dir, hour, revision, listings)
                except (KeyError, OSError, ValueError) as error:
                    for index in indices:
                        results[index]["reason"] = str(error)
                    continue
                if costs[hour]:
                    downloads += 1
                    print(
                        f"Verifying original {revision['name']} ({costs[hour] / 2**20:.0f} MiB remaining, "
                        f"up to {len(indices)} package-cutoffs, download {downloads}/{args.max_downloads})",
                        flush=True,
                    )
                task = pool.submit(
                    archive_evidence,
                    client,
                    storage_directories[hour],
                    revision,
                    scan_names,
                    local_paths[hour],
                    getattr(args, "archive_download_seconds", MAX_DOWNLOAD_SECONDS),
                )
                scheduled.append((revision, indices, listing, storage_directories[hour], task))

            for revision, indices, listing, storage, task in scheduled:
                try:
                    source, events = task.result()
                except (httpx.HTTPError, OSError, ValueError, EOFError, zlib.error) as error:
                    reason = str(error) or type(error).__name__
                    print(f"Archive attempt ended for {revision['name']}: {reason}", flush=True)
                    partial = storage / f"{archive_key(revision)}.part"
                    has_partial = partial.is_file() and partial.stat().st_size > 0
                    for index in indices & unresolved:
                        results[index]["reason"] = reason
                        if has_partial:
                            results[index]["status"] = "archive_download_pending"
                    continue
                if archive_pool is not None:
                    archive_pool.remember(storage, source)
                if storage != directory:
                    source, events = local_proof(directory, source, events, names)
                apply_archive(source, events, listing, results, unresolved, by_repo_name)
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
    parser.add_argument(
        "--archive-workers",
        type=int,
        default=DEFAULT_ARCHIVE_WORKERS,
        help=f"Concurrent archive checks (1–{MAX_ARCHIVE_WORKERS})",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        if (
            args.max_downloads < 0
            or not 1 <= args.archive_workers <= MAX_ARCHIVE_WORKERS
            or (args.archive_dir is not None and not args.archive_dir.is_dir())
        ):
            raise ValueError(
                f"expected nonnegative download budget, 1–{MAX_ARCHIVE_WORKERS} archive workers "
                "and an existing archive directory"
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
