"""Collect sharded D09 review evidence for a frozen historical PyPI sample.

The outputs are review inputs. Mapping, coverage and official outcomes still
require independent verification before publishing a D09 training dataset.
"""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from scripts.collect_d09_pilot import (
    ACTIVITY_TYPES,
    ACTIVITY_URL,
    SOURCE_LINK_LABELS,
    fetch_public,
    run_lock,
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
    package_id,
    parse_repo_id,
    repo_id,
)
from reposeer.exceptions import HTTPClientError, RateLimitExceededError

CUTOFFS = tuple(
    f"{year}-{day}T23:59:59.999Z"
    for year in range(2020, 2025)
    for day in ("03-31", "06-30", "09-30", "12-31")
)
EXTENDED_CUTOFFS = (
    tuple(f"2019-{day}T23:59:59.999Z" for day in ("03-31", "06-30", "09-30", "12-31")) + CUTOFFS
)
MAX_SAMPLE_SIZE = 10_000
MAX_SHARD_SIZE = 30
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
RESULT_PAGE_SIZE = 5000
SEED = "reposeer-d09-v1"
WINDOW_START = "2019-01-01T00:00:00.000Z"
WINDOW_END = add_calendar_months(CUTOFFS[-1], 12)
IDENTITY_EVENT_TYPES = (
    *ACTIVITY_TYPES,
    "IssueCommentEvent",
    "PullRequestReviewCommentEvent",
    "PullRequestReviewEvent",
    "CommitCommentEvent",
    "WatchEvent",
    "ForkEvent",
    "CreateEvent",
    "DeleteEvent",
    "PublicEvent",
    "MemberEvent",
    "GollumEvent",
)


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_list(values) -> str:
    return ", ".join(sql_string(value) for value in sorted(values))


def sql_time(value: str) -> str:
    literal = sql_string(checked_timestamp(value)[:-1].replace("T", " "))
    return f"toDateTime64({literal}, 3, 'UTC')"


def post_query(query: str) -> tuple[bytes, dict]:
    """Send an internally generated read-only SQL query too long for a GET URL."""
    body = bytearray()
    try:
        with httpx.stream(
            "POST",
            ACTIVITY_URL,
            params={"user": "demo"},
            content=query.encode(),
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=False,
        ) as response:
            for block in response.iter_bytes():
                if len(body) + len(block) > MAX_RESPONSE_BYTES:
                    raise HTTPClientError("Archive query exceeded its response byte limit")
                body.extend(block)
            if response.status_code != 200:
                raise HTTPClientError(
                    f"HTTP {response.status_code} from archive query",
                    status_code=response.status_code,
                    response_body=body.decode(errors="replace"),
                )
    except httpx.RequestError as error:
        raise HTTPClientError(f"Archive query failed: {type(error).__name__}") from error
    return bytes(body), {
        "retrieved_at": format_utc_timestamp(datetime.now(UTC)),
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }


def query_rows(
    http: HTTPClient, directory: Path, name: str, query: str, *, post: bool = False
) -> list[dict]:
    """Reuse only a receipt whose query and response SHA match this invocation."""

    def parse_response(body: bytes) -> list[dict]:
        rows = parse_rows(body)
        for row in rows:
            if "exception" in row:
                raise HTTPClientError(
                    "Archive query returned a partial response", response_body=str(row["exception"])
                )
        return rows

    receipt_path = directory / "sources" / f"{name}.json"
    query_sha = hashlib.sha256(query.encode()).hexdigest()
    receipt = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
    if receipt.get("query_sha256") == query_sha and stored_source_matches(directory, receipt):
        try:
            return parse_response((directory / receipt["path"]).read_bytes())
        except (ValueError, HTTPClientError):
            # A streamed provider exception is never a valid checkpoint.
            pass
    print(f"Collecting {directory.name}/{name}", flush=True)
    try:
        if post:
            body, receipt = post_query(query)
        else:
            url = ACTIVITY_URL + "?" + urlencode({"user": "demo", "query": query})
            body, receipt = fetch_public(http, url, MAX_RESPONSE_BYTES)
        rows = parse_response(body)
    except RateLimitExceededError:
        raise
    except HTTPClientError as error:
        detail = error.response_body or ""
        if "QUOTA_EXCEEDED" in detail or error.status_code == 429:
            match = re.search(r"Interval will end at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", detail)
            reset = (
                datetime.fromisoformat(match[1]).replace(tzinfo=UTC).timestamp() if match else None
            )
            raise RateLimitExceededError(
                "Public ClickHouse quota exhausted",
                reset_timestamp=reset,
                response_body=detail,
            ) from error
        raise
    path = f"sources/{name}-{receipt['content_sha256']}.jsonl"
    atomic_bytes(directory / path, body, immutable=False)
    write_json_atomic(
        receipt_path,
        {
            **receipt,
            "url": ACTIVITY_URL,
            "query": query,
            "query_sha256": query_sha,
            "path": path,
        },
    )
    return rows


def parse_rows(body: bytes) -> list[dict]:
    rows = [json.loads(line) for line in body.splitlines()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("expected JSON objects from archive query")
    return rows


def archive_timestamp(value: str) -> str:
    time = value.replace(" ", "T")
    return checked_timestamp(time + ("Z" if "." in time else ".000Z"))


def cohort_query(sample_size: int) -> str:
    return f"""WITH replaceRegexpAll(lower(name), '[-_.]+', '-') AS project
SELECT project, arraySort(groupUniqArray(name)) AS names,
       toString(min(upload_time), 'UTC') AS first_upload_at
FROM pypi.projects
WHERE upload_time < {sql_time(CUTOFFS[-1])}
  AND match(name, '^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$')
GROUP BY project
ORDER BY cityHash64(concat('{SEED}:', project)), project
LIMIT {sample_size}
FORMAT JSONEachRow"""


def freeze_cohort(http: HTTPClient, output_dir: Path, config: dict) -> dict:
    path = output_dir / "cohort.json"
    if path.exists():
        cohort = json.loads(path.read_bytes())
        if cohort["config"] != config:
            raise ValueError("resume settings differ from the frozen cohort")
        receipt = json.loads((output_dir / cohort["source"]).read_bytes())
        if not stored_source_matches(output_dir, receipt):
            raise ValueError("frozen cohort source is missing or changed")
        if cohort["packages"] != roster_rows(
            parse_rows((output_dir / receipt["path"]).read_bytes())
        ):
            raise ValueError("cohort packages differ from the frozen archive response")
        return cohort

    packages = roster_rows(
        query_rows(http, output_dir, "cohort", cohort_query(config["sample_size"]))
    )
    if len(packages) != config["sample_size"]:
        raise ValueError("archive returned fewer projects than requested")
    cohort = {"config": config, "source": "sources/cohort.json", "packages": packages}
    write_json_atomic(path, cohort)
    return cohort


def roster_rows(rows: list[dict]) -> list[dict]:
    packages = []
    seen = set()
    for row in rows:
        subject = package_id(row["project"])
        names = row["names"]
        timestamp = archive_timestamp(row["first_upload_at"])
        if (
            not names
            or any(package_id(name) != subject for name in names)
            or timestamp >= CUTOFFS[-1]
            or subject in seen
        ):
            raise ValueError("invalid archived roster entry")
        seen.add(subject)
        packages.append(
            {"package_id": subject, "source_names": names, "first_upload_at": timestamp}
        )
    return packages


def distribution_query(
    names: set[str], after: tuple[str, str] | None = None, *, page: bool = False
) -> str:
    cursor = (
        f" AND (name, version) > ({sql_string(after[0])}, {sql_string(after[1])})"
        if after is not None
        else ""
    )
    order = f"name, version\nLIMIT {RESULT_PAGE_SIZE}" if page else "name, uploaded_at, version"
    return f"""SELECT name, version,
       toString(tupleElement(artifact, 1), 'UTC') AS uploaded_at,
       tupleElement(artifact, 2) AS sha256, tupleElement(artifact, 3) AS project_urls,
       tupleElement(artifact, 4) AS requires_python
FROM (
    SELECT name, version, min(tuple(upload_time, sha256_digest, project_urls, requires_python)) AS artifact
    FROM pypi.projects
    WHERE name IN ({sql_list(names)}) AND upload_time < {sql_time(WINDOW_END)}{cursor}
    GROUP BY name, version
)
ORDER BY {order}
FORMAT JSONEachRow"""


def distribution_rows(http: HTTPClient, directory: Path, names: set[str]) -> Iterator[dict]:
    if not (directory / "sources/distributions-page-000.json").exists():
        try:
            yield from query_rows(http, directory, "distributions", distribution_query(names))
            return
        except HTTPClientError as error:
            if "TOO_MANY_ROWS_OR_BYTES" not in (error.response_body or ""):
                raise
    after = None
    for index in range(1000):
        name = f"distributions-page-{index:03d}"
        rows = query_rows(http, directory, name, distribution_query(names, after, page=True))
        if len(rows) > RESULT_PAGE_SIZE:
            raise ValueError(f"distribution page exceeds its limit: {name}")
        for row in rows:
            key = (row["name"], row["version"])
            if after is not None and key <= after:
                raise ValueError(f"distribution page is not strictly ordered: {name}")
            after = key
            yield row
        if len(rows) < RESULT_PAGE_SIZE:
            return
    raise ValueError("distribution page limit reached")


def release_history(http: HTTPClient, directory: Path, packages: list[dict]) -> dict[str, list]:
    names = {name for package in packages for name in package["source_names"]}
    grouped = defaultdict(dict)
    for row in distribution_rows(http, directory, names):
        uploaded_at = archive_timestamp(row["uploaded_at"])
        if row["name"] not in names or uploaded_at >= WINDOW_END or not row["version"]:
            raise ValueError("distribution response outside requested cohort or window")
        row["uploaded_at"] = uploaded_at
        versions = grouped[package_id(row["name"])]
        prior = versions.get(row["version"])
        if prior is None or uploaded_at < prior["uploaded_at"]:
            versions[row["version"]] = row
        elif uploaded_at == prior["uploaded_at"] and row["sha256"] != prior["sha256"]:
            raise ValueError("conflicting first distribution for the same PyPI version")
    return {
        subject: sorted(versions.values(), key=lambda row: (row["uploaded_at"], row["version"]))
        for subject, versions in grouped.items()
    }


def explicit_sources(distribution: dict) -> dict[str, set[str]]:
    result = defaultdict(set)
    if not re.fullmatch(r"[0-9a-f]{64}", distribution["sha256"]):
        return result
    for entry in distribution["project_urls"]:
        label, _, url = entry.partition(",")
        if label.strip().casefold() not in SOURCE_LINK_LABELS:
            continue
        url = url.strip()
        try:
            root = _root_url(url)
        except ValueError:
            # Preserve competing or unsupported explicit links as review reasons.
            result[url].add("")
        else:
            result[root].add(urlsplit(url).path.strip("/"))
    return result


def review_rows(
    packages: list[dict],
    history: dict[str, list],
    *,
    cutoffs=CUTOFFS,
    allow_github_homepage: bool = False,
) -> Iterator[dict]:
    for package in packages:
        subject = package["package_id"]
        releases = history.get(subject, [])
        if not releases or releases[0]["uploaded_at"] != package["first_upload_at"]:
            raise ValueError(f"release history does not match frozen roster for {subject}")
        for cutoff in cutoffs:
            if package["first_upload_at"] >= cutoff:
                continue
            horizon = add_calendar_months(cutoff, 12)
            # Mirror timestamps have millisecond precision. Leave the boundary out.
            selected = next(row for row in reversed(releases) if row["uploaded_at"] < cutoff)
            sources = explicit_sources(selected)
            homepage_candidate = False
            if not sources and allow_github_homepage and selected.get("home_page"):
                try:
                    root = _root_url(selected["home_page"])
                except ValueError:
                    pass
                else:
                    sources = {root: {urlsplit(selected["home_page"]).path.strip("/")}}
                    homepage_candidate = True
            names = sorted(set().union(*sources.values())) if len(sources) == 1 else []
            result = {
                "package_id": subject,
                "cutoff_at": cutoff,
                "horizon_end_at": horizon,
                "source_version": selected["version"],
                "distribution_uploaded_at": selected["uploaded_at"],
                "distribution_sha256": selected["sha256"],
                "explicit_source_urls": sorted(sources),
                "archive_names": [name for name in names if name],
                "observed_horizon_releases": sum(
                    cutoff < row["uploaded_at"] < horizon for row in releases
                ),
            }
            if homepage_candidate:
                result["source_link_policy"] = "github-homepage-candidate-v1"
            yield result


def chunked(values: list, size: int) -> Iterator[list]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def observe_repo_ids(
    http: HTTPClient,
    directory: Path,
    rows: list[dict],
    *,
    window_start=WINDOW_START,
    identity_events=False,
) -> set[str]:
    names = {name for row in rows for name in row["archive_names"]}
    observations = defaultdict(list)
    for index, batch in enumerate(chunked(sorted(names), 40)):
        types = IDENTITY_EVENT_TYPES if identity_events else ACTIVITY_TYPES
        query = f"""SELECT repo_name, repo_id, toYYYYMM(created_at, 'UTC') AS month, count() AS events
FROM github.events
WHERE event_type IN ({sql_list(types)}) AND repo_name IN ({sql_list(batch)})
  AND created_at >= {sql_time(window_start)} AND created_at <= {sql_time(CUTOFFS[-1])}
GROUP BY repo_name, repo_id, month
ORDER BY repo_name, repo_id, month
FORMAT JSONEachRow"""
        for item in query_rows(http, directory, f"repo-names-{index:03d}", query):
            month = item["month"]
            if item["repo_name"] not in batch or not (
                int(window_start[:7].replace("-", "")) <= month <= 202412 and 1 <= month % 100 <= 12
            ):
                raise ValueError("GitHub name response outside requested scope")
            observations[item["repo_name"]].append((month, repo_id(item["repo_id"])))

    all_ids = set()
    for row in rows:
        cutoff = row["cutoff_at"]
        end_month = int(cutoff[:7].replace("-", ""))
        start_month = int(add_calendar_months(cutoff, -12)[:7].replace("-", ""))
        ids = sorted(
            {
                identity
                for name in row["archive_names"]
                for month, identity in observations[name]
                if start_month < month <= end_month
            }
        )
        row["observed_repo_ids"] = ids
        row["mapping_review"] = (
            "single_id_observed"
            if len(ids) == 1
            else "conflicting_repo_ids"
            if ids
            else "no_matching_archive_activity"
            if row["archive_names"]
            else "unresolved_explicit_source"
        )
        if len(ids) == 1:
            all_ids.add(ids[0])
    return all_ids


def collect_official_leads(http: HTTPClient, directory: Path, identities: set[str]) -> Counter:
    counts = Counter()
    # Five results per repo-month over 84 months must fit the public endpoint's 10k-row cap.
    for index, batch in enumerate(chunked(sorted(identities), 20)):
        query = f"""SELECT repo_id, repo_name, event_type, toString(created_at, 'UTC') AS event_at,
       actor_login, toString(author_association) AS association, number, comment_id,
       title, release_tag_name, substringUTF8(body, 1, 1200) AS excerpt
FROM github.github_events
WHERE repo_id IN ({sql_list(map(parse_repo_id, batch))})
  AND event_type IN ('IssuesEvent', 'IssueCommentEvent', 'ReleaseEvent', 'PullRequestEvent')
  AND created_at >= {sql_time(WINDOW_START)} AND created_at <= {sql_time(WINDOW_END)}
  AND multiSearchAnyCaseInsensitiveUTF8(concat(title, ' ', release_name, ' ', body),
      ['unmaintained', 'end of life', 'no longer maintain', 'maintenance mode', 'deprecat', 'archiv'])
ORDER BY repo_id, toYYYYMM(created_at, 'UTC'),
         association IN ('OWNER', 'MEMBER', 'COLLABORATOR') DESC, created_at,
         event_type, number, comment_id, actor_login, title, release_tag_name, excerpt
LIMIT 5 BY repo_id, toYYYYMM(created_at, 'UTC')
FORMAT JSONEachRow"""
        for item in query_rows(http, directory, f"official-leads-{index:03d}", query):
            identity = repo_id(item["repo_id"])
            event_at = archive_timestamp(item["event_at"])
            if identity not in batch or not WINDOW_START <= event_at <= WINDOW_END:
                raise ValueError("GitHub lead response outside requested scope")
            counts[identity] += 1
    return counts


def collect_shard(http: HTTPClient, directory: Path, packages: list[dict]) -> dict:
    rows = list(review_rows(packages, release_history(http, directory, packages)))
    identities = observe_repo_ids(http, directory, rows)
    leads = collect_official_leads(http, directory, identities)
    atomic_bytes(
        directory / "review.jsonl",
        b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\n" for row in rows),
        immutable=False,
    )
    return {
        "shard": directory.name,
        "packages": len(packages),
        "package_cutoffs": len(rows),
        "mapping_review": dict(Counter(row["mapping_review"] for row in rows)),
        "repo_ids_with_official_leads": len(leads),
        "official_leads": sum(leads.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=60)
    parser.add_argument("--shard-size", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.sample_size <= MAX_SAMPLE_SIZE or not 1 <= args.shard_size <= MAX_SHARD_SIZE:
        parser.error(f"sample size must be 1–{MAX_SAMPLE_SIZE} and shard size 1–{MAX_SHARD_SIZE}")
    config = {
        "batch_version": 1,
        "seed": SEED,
        "cutoffs": list(CUTOFFS),
        "sample_size": args.sample_size,
        "shard_size": args.shard_size,
    }
    try:
        args.output_dir.mkdir(parents=True, exist_ok=args.resume)
        with run_lock(args.output_dir), HTTPClient(timeout=60, max_retries=2) as http:
            cohort = freeze_cohort(http, args.output_dir, config)
            shards = []
            for index, packages in enumerate(chunked(cohort["packages"], args.shard_size)):
                directory = args.output_dir / f"shard-{index:04d}"
                shards.append(collect_shard(http, directory, packages))
            summary = {
                "status": "review_evidence_collected",
                "historical_mappings_verified": 0,
                "core_coverage_verified": 0,
                "labels_evaluated": 0,
                "training_rows": 0,
                "shards": shards,
            }
            write_json_atomic(args.output_dir / "summary.json", summary)
    except (HTTPClientError, OSError, ValueError) as error:
        parser.exit(1, f"D09 evidence collection stopped: {error}\n")
    print(f"D09 review evidence: {args.output_dir.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
