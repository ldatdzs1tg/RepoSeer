"""Collect a bounded, read-only D09 evidence pilot for explicit PyPI targets.

Run with python -m scripts.collect_d09_pilot --help. The output is evidence for
review. Historical mapping, window coverage and official status need separate
verification before feature snapshots or labels can be produced.
"""

import argparse
import gzip
import hashlib
import io
import json
import re
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collectors.package.repository_mapping import _root_url
from reposeer.contracts.identity import (
    add_calendar_months,
    checked_timestamp,
    format_utc_timestamp,
    package_id,
    repo_id,
)
from reposeer.exceptions import HTTPClientError

MAX_TARGETS = 30
MAX_ARCHIVE_HOURS = 4
MAX_PYPI_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_LINE_BYTES = 4 * 1024 * 1024
MAX_MATCHED_EVENTS = 10_000
SOURCE_LINK_LABELS = {"source", "repository", "code", "source code"}
GITHUB_PREFIX = "https://github.com/"


def provider_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing_provider_timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("missing_provider_timezone")
    return parsed.astimezone(UTC)


def fetch_public(http: HTTPClient, url: str, max_bytes: int) -> tuple[bytes, dict]:
    response = http.get(
        url,
        use_cache=False,
        follow_redirects=False,
        max_response_bytes=max_bytes,
        public_request=True,
    )
    if response.status_code != 200:
        raise HTTPClientError("Unexpected response", status_code=response.status_code)
    body = response.content
    return body, {
        "url": url,
        "retrieved_at": format_utc_timestamp(datetime.now(UTC)),
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "last_modified": response.headers.get("Last-Modified"),
    }


def pypi_json(http: HTTPClient, url: str, output_dir: Path, path: str) -> tuple[dict, dict]:
    body, source = fetch_public(http, url, MAX_PYPI_BYTES)
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("expected_pypi_object")
    output = output_dir / path
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(body)
    return payload, {**source, "path": path}


def observed_releases(releases: dict, cutoff_at: datetime, horizon_end_at: datetime) -> list[dict]:
    observations = []
    for version, distributions in releases.items():
        if not isinstance(distributions, list):
            raise ValueError("invalid_pypi_release_files")
        uploads = []
        for distribution in distributions:
            if not isinstance(distribution, dict):
                raise ValueError("invalid_pypi_distribution")
            uploaded_at = provider_time(distribution.get("upload_time_iso_8601"))
            if uploaded_at <= horizon_end_at:
                uploads.append((uploaded_at, distribution))
        if not uploads:
            continue
        # A release can contain multiple artifacts uploaded on different dates.
        uploaded_at, distribution = min(uploads, key=lambda item: item[0])
        digests = distribution.get("digests")
        observations.append(
            {
                "version": version,
                "observed_first_uploaded_at": distribution["upload_time_iso_8601"],
                "phase": "pre_cutoff" if uploaded_at <= cutoff_at else "outcome_horizon",
                "artifact_url": distribution.get("url"),
                "artifact_sha256": digests.get("sha256") if isinstance(digests, dict) else None,
            }
        )
    return sorted(
        observations,
        key=lambda item: (provider_time(item["observed_first_uploaded_at"]), item["version"]),
    )


def source_links(info: dict) -> list[str]:
    urls = info.get("project_urls")
    if not isinstance(urls, dict):
        return []
    roots = set()
    for label, url in urls.items():
        if label.strip().casefold() in SOURCE_LINK_LABELS:
            # Same repository URL rules used by the current PyPI resolver.
            roots.add(_root_url(url))
    return sorted(roots)


def verify_release_artifact(release: dict, selected: dict) -> None:
    """Bind the version metadata response to the artifact dated before cutoff."""
    expected_sha = selected["artifact_sha256"]
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("missing_pypi_artifact_sha256")
    files = release.get("urls")
    if not isinstance(files, list):
        raise ValueError("missing_pypi_version_files")
    for artifact in files:
        if not isinstance(artifact, dict) or artifact.get("url") != selected["artifact_url"]:
            continue
        digests = artifact.get("digests")
        if (
            isinstance(digests, dict)
            and digests.get("sha256") == expected_sha
            and provider_time(artifact.get("upload_time_iso_8601"))
            == provider_time(selected["observed_first_uploaded_at"])
        ):
            return
    raise ValueError("pypi_version_artifact_mismatch")


def collect_package(
    http: HTTPClient,
    subject: str,
    cutoff_at: datetime,
    horizon_end_at: datetime,
    output_dir: Path,
) -> dict:
    name = subject.removeprefix("pypi:")
    base_url = f"https://pypi.org/pypi/{name}"
    result = {
        "package_id": subject,
        "pypi_sources": {},
        "releases": [],
        "mapping_metadata": None,
        "repo_candidate_url": None,
        "mapping_history_verified": False,
        "archive_repo_ids_dated_by_cutoff": [],
        "archive_events_dated_by_cutoff": 0,
        "archive_events_in_horizon": 0,
        "label_status": "not_evaluated",
    }
    try:
        project, source = pypi_json(
            http, f"{base_url}/json", output_dir, f"pypi/{name}/project.json"
        )
        result["pypi_sources"]["project"] = source
        if not isinstance(project.get("releases"), dict):
            raise ValueError("missing_pypi_releases")
        releases = observed_releases(project["releases"], cutoff_at, horizon_end_at)
        result["releases"] = releases
        selected = next(
            (release for release in reversed(releases) if release["phase"] == "pre_cutoff"),
            None,
        )
        if selected is None:
            return {**result, "status": "no_observed_release_by_cutoff"}

        version = selected["version"]
        version_url = f"{base_url}/{quote(version, safe='')}/json"
        release, source = pypi_json(http, version_url, output_dir, f"pypi/{name}/version.json")
        result["pypi_sources"]["version"] = source
        info = release.get("info")
        if not isinstance(info, dict) or package_id(info.get("name")) != subject:
            raise ValueError("pypi_version_package_mismatch")
        if info.get("version") != version:
            raise ValueError("pypi_version_mismatch")
        verify_release_artifact(release, selected)
        links = source_links(info)
        result["mapping_metadata"] = {
            "version": version,
            "observed_first_uploaded_at": selected["observed_first_uploaded_at"],
            "project_source_links": links,
            "requires_python": info.get("requires_python") or None,
        }
        if len(links) != 1:
            status = "ambiguous_source_links" if links else "missing_source_link"
            return {**result, "status": status}
        return {**result, "status": "repo_candidate", "repo_candidate_url": links[0]}
    except HTTPClientError as error:
        return {**result, "status": "pypi_request_failed", "http_status": error.status_code}
    except (ValueError, TypeError) as error:
        return {**result, "status": "pypi_evidence_invalid", "reason": str(error)}


def matching_events(
    body: bytes,
    repositories: set[str],
    lookback_start_at: datetime,
    cutoff_at: datetime,
    horizon_end_at: datetime,
) -> list[dict]:
    matched = []
    expanded_bytes = 0
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
        while line := stream.readline(MAX_ARCHIVE_LINE_BYTES + 1):
            expanded_bytes += len(line)
            if len(line) > MAX_ARCHIVE_LINE_BYTES or expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ValueError("archive_decoding_limit")
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("invalid_archive_event")
            repo = event.get("repo")
            if not isinstance(repo, dict) or not isinstance(repo.get("name"), str):
                continue
            slug = repo["name"].casefold()
            if slug not in repositories:
                continue
            numeric = repo_id(str(repo.get("id")))
            occurred_at = provider_time(event.get("created_at"))
            if not lookback_start_at < occurred_at <= horizon_end_at:
                continue
            if len(matched) == MAX_MATCHED_EVENTS:
                raise ValueError("archive_matched_event_limit")
            matched.append(
                {
                    "repo_id": numeric,
                    "repo_full_name": slug,
                    "phase": "pre_cutoff" if occurred_at <= cutoff_at else "outcome_horizon",
                    "available_at": None,
                    "record_sha256": hashlib.sha256(line).hexdigest(),
                    "event": event,
                }
            )
    return matched


def collect_archive_hour(
    http: HTTPClient,
    hour: datetime,
    packages: list[dict],
    lookback_start_at: datetime,
    cutoff_at: datetime,
    horizon_end_at: datetime,
    output_dir: Path,
    *,
    archive_dir: Path | None = None,
) -> dict:
    filename = f"{hour:%Y-%m-%d}-{hour.hour}"
    url = f"https://data.gharchive.org/{filename}.json.gz"
    repositories = {
        row["repo_candidate_url"].removeprefix(GITHUB_PREFIX)
        for row in packages
        if row["repo_candidate_url"] is not None
    }
    if not repositories:
        return {"url": url, "status": "no_repo_candidates"}
    try:
        if archive_dir is None:
            body, source = fetch_public(http, url, MAX_ARCHIVE_BYTES)
        else:
            archive_file = archive_dir / f"{filename}.json.gz"
            with archive_file.open("rb") as stream:
                body = stream.read(MAX_ARCHIVE_BYTES + 1)
            if len(body) > MAX_ARCHIVE_BYTES:
                raise ValueError("archive_file_size_limit")
            source = {
                "url": None,
                "local_file": str(archive_file.resolve()),
                "retrieved_at": None,
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "last_modified": None,
            }
        events = matching_events(body, repositories, lookback_start_at, cutoff_at, horizon_end_at)
    except HTTPClientError as error:
        return {"url": url, "status": "archive_request_failed", "http_status": error.status_code}
    except (ValueError, EOFError, OSError, zlib.error) as error:
        return {"url": url, "status": "archive_evidence_invalid", "reason": str(error)}

    path = f"gharchive/{filename}.json"
    source["archive_hour_utc"] = format_utc_timestamp(hour)
    write_json_atomic(output_dir / path, {"source": source, "events": events})
    for row in packages:
        if row["repo_candidate_url"] is None:
            continue
        slug = row["repo_candidate_url"].removeprefix(GITHUB_PREFIX)
        repo_ids = set(row["archive_repo_ids_dated_by_cutoff"])
        for event in events:
            if event["repo_full_name"] != slug:
                continue
            if event["phase"] == "pre_cutoff":
                row["archive_events_dated_by_cutoff"] += 1
                repo_ids.add(event["repo_id"])
            else:
                row["archive_events_in_horizon"] += 1
        row["archive_repo_ids_dated_by_cutoff"] = sorted(repo_ids)
    return {**source, "path": path, "status": "observed", "matched_events": len(events)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="*", help="PyPI names or canonical pypi:<name> IDs")
    parser.add_argument("--targets-file", type=Path, help="Text file with one package per line")
    parser.add_argument("--cutoff-at", required=True, help="UTC timestamp YYYY-MM-DDTHH:mm:ss.sssZ")
    parser.add_argument(
        "--archive-hour",
        action="append",
        default=[],
        metavar="YYYY-MM-DD-HH",
        help=f"GH Archive hour in UTC, repeat up to {MAX_ARCHIVE_HOURS} times",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        help="Read the requested hours from this directory of GH Archive .json.gz files",
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="New pilot output directory")
    args = parser.parse_args()
    try:
        targets = args.packages
        if args.targets_file is not None:
            targets = [*targets, *args.targets_file.read_text(encoding="utf-8-sig").splitlines()]
        subjects = list(
            dict.fromkeys(
                package_id(value.strip().removeprefix("pypi:"))
                for value in targets
                if value.strip()
            )
        )
        if not 1 <= len(subjects) <= MAX_TARGETS:
            raise ValueError(f"expected 1–{MAX_TARGETS} unique PyPI packages")
        cutoff = checked_timestamp(args.cutoff_at)
        cutoff_at = datetime.fromisoformat(cutoff)
        lookback_start_at = datetime.fromisoformat(add_calendar_months(cutoff, -12))
        horizon_end_at = datetime.fromisoformat(add_calendar_months(cutoff, 12))
        if cutoff_at > datetime.now(UTC):
            raise ValueError("cutoff must be in the past")
        if len(args.archive_hour) > MAX_ARCHIVE_HOURS:
            raise ValueError(f"expected at most {MAX_ARCHIVE_HOURS} archive hours")
        hours = sorted(
            {
                datetime.strptime(value, "%Y-%m-%d-%H").replace(tzinfo=UTC)
                for value in args.archive_hour
            }
        )
        if args.archive_dir is not None and (not hours or not args.archive_dir.is_dir()):
            raise ValueError(
                "archive-dir must be a directory and requires at least one archive-hour"
            )
        if any(
            hour > horizon_end_at or hour + timedelta(hours=1) <= lookback_start_at
            for hour in hours
        ):
            raise ValueError("archive hours must intersect the lookback or outcome window")
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    with HTTPClient(timeout=45, max_retries=2) as http:
        packages = []
        for subject in subjects:
            result = collect_package(http, subject, cutoff_at, horizon_end_at, args.output_dir)
            packages.append(result)
            print(f"{subject}: {result['status']}", flush=True)

        archives = []
        for hour in hours:
            print(f"Reading GH Archive for {hour:%Y-%m-%d %H}:00 UTC", flush=True)
            result = collect_archive_hour(
                http,
                hour,
                packages,
                lookback_start_at,
                cutoff_at,
                horizon_end_at,
                args.output_dir,
                archive_dir=args.archive_dir,
            )
            archives.append(result)

    count = sum(row["status"] == "repo_candidate" for row in packages)
    collection_failed = any(row["status"].startswith("pypi_") for row in packages) or any(
        row["status"] != "observed" for row in archives
    )
    report = {
        "cutoff_at": cutoff,
        "lookback_start_at": format_utc_timestamp(lookback_start_at),
        "horizon_end_at": format_utc_timestamp(horizon_end_at),
        "target_selection": "explicit",
        "collection_status": "partial" if collection_failed else "observed",
        "coverage_status": "unverified",
        "limitations": [
            "PyPI lists files visible at retrieval. Deleted releases can be missing.",
            "Only version-specific metadata is used for repository candidates.",
            "Archive timestamps and matching repository names alone do not verify historical mapping.",
            "Selected archive hours do not establish 3/6/12-month census or follow-up coverage.",
            "Official cessation evidence must be verified separately.",
        ],
        "packages": packages,
        "archives": archives,
    }
    report_path = args.output_dir / "report.json"
    write_json_atomic(report_path, report)
    events = sum(row.get("matched_events", 0) for row in archives)
    print(f"PyPI packages: {len(packages)} | repo candidates: {count} | archive matches: {events}")
    print(f"Pilot report: {report_path.resolve()}")
    return 0 if count and not collection_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
