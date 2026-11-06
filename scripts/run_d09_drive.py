"""Run the D09 evidence collection, verification and split audit from Drive screening."""

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
from scripts.audit_model_a_splits import audit_splits
from scripts.build_d09_reviewed import write_reviewed_run
from scripts.collect_d09_batch import (
    CUTOFFS,
    MAX_SHARD_SIZE,
    SEED,
    WINDOW_END,
    collect_shard,
    parse_rows,
    query_rows,
    roster_rows,
    sql_list,
    sql_string,
    sql_time,
)
from scripts.collect_d09_pilot import run_lock, stored_source_matches
from scripts.d09_archive_pool import ArchivePool
from scripts.verify_d09_evidence import (
    DEFAULT_ARCHIVE_WORKERS,
    MAX_ARCHIVE_WORKERS,
    load_candidates,
    verify_batch,
)
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import checked_timestamp, format_utc_timestamp, normalize_pypi_name
from reposeer.exceptions import HTTPClientError, RateLimitExceededError
from reposeer.storage.screening_mapping import (
    checked_mount_path,
    read_mapping_csv,
    validate_master_metadata,
)

DEFAULT_SCREENING = Path(
    "G:/.shortcut-targets-by-id/1fkcK7UsBtiq7-7_4_aH3FihdjYfIku0f/RepoSeer-Data/screening (1)"
)
DEFAULT_INVENTORY = Path("data/d09/archive-inventory-2019-2025")
REGISTRY_PAGE_SIZE = 5000
REGISTRY_FILTER_SQL_LIMIT = 2500
REGISTRY_TARGET_BATCH_SIZE = 2000
REGISTRY_TARGET_SQL_BYTES = 64_000
SPLIT_TIMES = {
    "fold_fit_at": "2022-12-31T23:59:59.999Z",
    "validation_start_at": "2023-01-01T00:00:00.000Z",
    "selection_freeze_at": "2024-09-30T23:59:59.999Z",
    "test_start_at": "2024-10-01T00:00:00.000Z",
    "test_end_at": "2025-01-01T00:00:00.000Z",
}


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def jsonl(rows) -> bytes:
    return b"".join(canonical_json(row) + b"\n" for row in rows)


def target_groups(rows: list[dict]) -> list[list[dict]]:
    groups = defaultdict(list)
    for row in rows:
        groups[row["repo_full_name"] or row["package_id"]].append(row)
    keys = sorted(
        groups,
        key=lambda key: (
            not groups[key][0]["repo_full_name"],
            digest(f"{SEED}:{key}".encode()),
            key,
        ),
    )
    return [groups[key] for key in keys]


def freeze_targets(screening: Path, output: Path, config: dict) -> dict:
    path = output / "drive-targets.json"
    if path.exists():
        frozen = json.loads(path.read_bytes())
        if (
            frozen["config"] != config
            or digest(canonical_json(frozen["targets"])) != frozen["targets_sha256"]
        ):
            raise ValueError(
                "resume settings or frozen Drive targets changed; use a new output directory"
            )
        return frozen

    screening = checked_mount_path(screening)
    pending = screening / "mapping.pending.json"
    metadata = screening / "mapping.meta.json"
    if pending.exists():
        raise ValueError("Drive screening is being updated; retry after the update finishes")
    meta = validate_master_metadata(json.loads(metadata.read_bytes()))
    rows = read_mapping_csv(screening / "mapping.csv", meta)
    if pending.exists() or json.loads(metadata.read_bytes()) != meta:
        raise ValueError("Drive screening changed during the read; retry")
    selected = []
    for group in target_groups(list(rows.values())):
        selected.extend(
            {key: row[key] for key in ("package_id", "repo_full_name", "screening_status")}
            for row in group
        )
        if config["limit"] is not None and len(selected) >= config["limit"]:
            break
    if not selected:
        raise ValueError("Drive screening contains no packages")
    frozen = {
        "config": config,
        "drive_metadata": meta,
        "targets": selected,
        "targets_sha256": digest(canonical_json(selected)),
    }
    write_json_atomic(path, frozen)
    return frozen


def registry_query(
    prefix: str,
    projects: set[str],
    *,
    after: str | None = None,
    limit: int | None = None,
    filter_targets: bool = False,
) -> str:
    prefixes = sorted({prefix, prefix.upper()})
    ranges = " OR ".join(
        f"(name >= {sql_string(letter)} AND name < {sql_string(chr(ord(letter) + 1))})"
        for letter in prefixes
    )
    names = sql_list(projects)
    selected = (
        f"AND project IN ({names})"
        if filter_targets or len(names) <= REGISTRY_FILTER_SQL_LIMIT
        else ""
    )
    cursor = f" AND project > {sql_string(after)}" if after is not None else ""
    page = f"\nLIMIT {limit}" if limit is not None else ""
    return f"""WITH replaceRegexpAll(lower(name), '[-_.]+', '-') AS project
SELECT project, arraySort(groupUniqArray(name)) AS names,
       toString(min(upload_time), 'UTC') AS first_upload_at
FROM pypi.projects
WHERE ({ranges}) AND upload_time < {sql_time(WINDOW_END)}
  AND match(name, '^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$') {selected}{cursor}
GROUP BY project
HAVING min(upload_time) < {sql_time(CUTOFFS[-1])}
ORDER BY project{page}
FORMAT JSONEachRow"""


def validate_registry_page(
    rows: list[dict], name: str, prefix: str, *, after: str = "", targets: set[str] | None = None
) -> str:
    previous = after
    for row in rows:
        project = row["project"]
        if (
            not project.startswith(prefix)
            or normalize_pypi_name(project) != project
            or project <= previous
            or (targets is not None and project not in targets)
        ):
            raise ValueError(
                f"registry returned an out-of-order or out-of-scope project for {name}"
            )
        previous = project
    return previous


def registry_target_batches(projects: set[str], after: str):
    batch, size = set(), 0
    for project in sorted(projects):
        if project <= after:
            continue
        size_of_name = len(project) + 4
        if batch and (
            len(batch) == REGISTRY_TARGET_BATCH_SIZE
            or size + size_of_name > REGISTRY_TARGET_SQL_BYTES
        ):
            yield batch
            batch, size = set(), 0
        batch.add(project)
        size += size_of_name
    if batch:
        yield batch


def registry_pages(http: HTTPClient, output: Path, prefix: str, projects: set[str]):
    if len(sql_list(projects)) <= REGISTRY_FILTER_SQL_LIMIT:
        name = f"registry-{prefix}"
        rows = query_rows(http, output, name, registry_query(prefix, projects))
        validate_registry_page(rows, name, prefix, targets=projects)
        yield name, rows
        return

    # Preserve already captured prefix pages, then request only the remaining frozen targets.
    after = ""
    for number in range(1000):
        name = f"registry-{prefix}-{number:03d}"
        path = output / "sources" / f"{name}.json"
        if not path.is_file():
            break
        receipt = json.loads(path.read_bytes())
        expected = registry_query(prefix, projects, after=after or None, limit=REGISTRY_PAGE_SIZE)
        if receipt["query_sha256"] != digest(expected.encode()) or not stored_source_matches(
            output, receipt
        ):
            raise ValueError(f"registry page checkpoint changed: {name}")
        rows = parse_rows((output / receipt["path"]).read_bytes())
        after = validate_registry_page(rows, name, prefix, after=after)
        yield name, rows
        if len(rows) < REGISTRY_PAGE_SIZE:
            return

    for number, batch in enumerate(registry_target_batches(projects, after)):
        name = f"registry-{prefix}-targets-{number:03d}"
        query = registry_query(prefix, batch, filter_targets=True)
        rows = query_rows(http, output, name, query, post=True)
        validate_registry_page(rows, name, prefix, targets=batch)
        yield name, rows


def historical_registry(http: HTTPClient, output: Path, frozen: dict) -> tuple[dict, dict]:
    path = output / "historical-registry.json"
    if path.exists():
        saved = json.loads(path.read_bytes())
        if saved["targets_sha256"] != frozen["targets_sha256"] or saved[
            "packages_sha256"
        ] != digest(canonical_json(saved["packages"])):
            raise ValueError("frozen historical registry changed")
        for name, expected in saved["registry_receipts"].items():
            receipt = json.loads((output / "sources" / f"{name}.json").read_bytes())
            if receipt["content_sha256"] != expected or not stored_source_matches(output, receipt):
                raise ValueError(f"frozen registry source changed: {name}")
        return saved["packages"], saved["registry_receipts"]

    buckets = defaultdict(set)
    for row in frozen["targets"]:
        project = row["package_id"].removeprefix("pypi:")
        buckets[project[0]].add(project)
    registry, receipts = {}, {}
    for prefix, projects in sorted(buckets.items()):
        for name, raw in registry_pages(http, output, prefix, projects):
            for package in roster_rows([row for row in raw if row["project"] in projects]):
                registry[package["package_id"]] = package
            receipts[name] = json.loads((output / "sources" / f"{name}.json").read_bytes())[
                "content_sha256"
            ]
    write_json_atomic(
        path,
        {
            "targets_sha256": frozen["targets_sha256"],
            "packages_sha256": digest(canonical_json(registry)),
            "registry_receipts": receipts,
            "packages": registry,
        },
    )
    return registry, receipts


def pack_groups(groups: list[list[dict]], size: int):
    batch, count = [], 0
    for group in groups:
        if batch and count + len(group) > size:
            yield batch
            batch, count = [], 0
        batch.append(group)
        count += len(group)
    if batch:
        yield batch


def prepare_batch(
    directory: Path, packages: list[dict], registry_receipts: dict, frozen: dict
) -> None:
    roster = jsonl(
        {
            "project": item["package_id"].removeprefix("pypi:"),
            "names": item["source_names"],
            "first_upload_at": item["first_upload_at"][:-1].replace("T", " "),
        }
        for item in packages
    )
    relative = "sources/cohort-drive.jsonl"
    prefixes = {item["package_id"][5] for item in packages}
    receipt = {
        "path": relative,
        "content_sha256": digest(roster),
        "drive_targets_sha256": frozen["targets_sha256"],
        "registry_receipts": {
            name.removeprefix("registry-"): sha
            for name, sha in registry_receipts.items()
            if name.removeprefix("registry-").split("-", 1)[0] in prefixes
        },
    }
    cohort = {
        "config": {
            "cutoffs": list(CUTOFFS),
            "source_kind": "drive_screening_and_historical_registry",
        },
        "source": "sources/cohort.json",
        "packages": packages,
    }
    cohort_path = directory / "cohort.json"
    if cohort_path.exists():
        if (
            json.loads(cohort_path.read_bytes()) != cohort
            or json.loads((directory / "sources/cohort.json").read_bytes()) != receipt
            or not stored_source_matches(directory, receipt)
        ):
            raise ValueError(f"frozen cohort changed for {directory.name}")
        return
    atomic_bytes(directory / relative, roster)
    write_json_atomic(directory / "sources/cohort.json", receipt)
    write_json_atomic(cohort_path, cohort)


def collection_checkpoint(directory: Path) -> dict | None:
    path = directory / "collection.json"
    if not path.is_file():
        return None
    receipt = json.loads(path.read_bytes())
    return receipt if receipt["source_reviews_sha256"] == shard_reviews_digest(directory) else None


def collect_batch(http: HTTPClient, directory: Path, groups: list[list[dict]]) -> dict:
    counts = Counter()
    package_cutoffs = official_leads = 0
    for index, packed in enumerate(pack_groups(groups, MAX_SHARD_SIZE)):
        packages = [item for group in packed for item in group]
        shard = directory / f"shard-{index:04d}"
        # Exceptionally large repo groups stay in one outer batch and use bounded queries.
        for part, offset in enumerate(range(0, len(packages), MAX_SHARD_SIZE)):
            current = shard if part == 0 else directory / f"shard-{index:04d}-{part:03d}"
            path = current / "collection.json"
            summary = json.loads(path.read_bytes()) if path.is_file() else {}
            if not stored_source_matches(current, summary):
                summary = collect_shard(http, current, packages[offset : offset + MAX_SHARD_SIZE])
                write_json_atomic(
                    path,
                    {
                        **summary,
                        "path": "review.jsonl",
                        "content_sha256": file_digest(current / "review.jsonl"),
                    },
                )
            counts.update(summary["mapping_review"])
            package_cutoffs += summary["package_cutoffs"]
            official_leads += summary["official_leads"]
    receipt = {
        "source_reviews_sha256": shard_reviews_digest(directory),
        "mapping_review": dict(counts),
        "package_cutoffs": package_cutoffs,
        "official_leads": official_leads,
    }
    write_json_atomic(directory / "collection.json", receipt)
    return receipt


def verification_checkpoint(directory: Path, retry_unverified: bool) -> dict | None:
    output = directory / "verification"
    receipt_path = output / "summary.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_bytes())
        retryable = {"download_budget_reached", "archive_download_pending"}
        if retry_unverified:
            retryable.update(("archive_evidence_unverified", "pypi_evidence_unverified"))
        if (
            stored_source_matches(output, receipt)
            and retryable.isdisjoint(receipt["statuses"])
            and receipt.get("source_reviews_sha256") == shard_reviews_digest(directory)
        ):
            return receipt
    return None


def verify_mapping(directory: Path, args, inventory: dict, budget: int) -> tuple[dict, int]:
    output = directory / "verification"
    candidates = load_candidates(directory, set())
    output.mkdir(parents=True, exist_ok=True)
    options = SimpleNamespace(
        output_dir=output,
        inventory_dir=args.inventory_dir,
        archive_dir=args.archive_dir,
        max_downloads=budget,
        archive_workers=args.archive_workers,
        archive_pool=getattr(args, "archive_pool", None),
    )
    rows, used = verify_batch(options, candidates, inventory) if candidates else ([], 0)
    body = jsonl(rows)
    atomic_bytes(output / "mapping_evidence.jsonl", body, immutable=False)
    receipt = {
        "path": "mapping_evidence.jsonl",
        "content_sha256": digest(body),
        "candidate_rows": len(candidates),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "source_reviews_sha256": shard_reviews_digest(directory),
    }
    write_json_atomic(output / "summary.json", receipt)
    return receipt, used


def shard_reviews_digest(directory: Path) -> str:
    return digest(
        canonical_json(
            {
                path.parent.name: file_digest(path)
                for path in sorted(directory.glob("shard-*/review.jsonl"))
            }
        )
    )


def index_reviews(db: sqlite3.Connection, path: Path, registry: dict, observation_end: str) -> int:
    db.execute(
        "CREATE TABLE reviews (package TEXT, cutoff TEXT, body BLOB, PRIMARY KEY (package, cutoff))"
    )
    if not path.is_file():
        return 0
    count = 0
    with path.open("rb") as stream, db:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            snapshot = row["snapshot"]
            key = (snapshot["package_id"], snapshot["cutoff_at"])
            if (
                key[0] not in registry
                or key[1] not in CUTOFFS
                or key[1] <= registry[key[0]]["first_upload_at"]
                or checked_timestamp(row["reviewed_at"]) > observation_end
            ):
                raise ValueError(f"out-of-cohort or future-dated review: {key}")
            try:
                db.execute("INSERT INTO reviews VALUES (?, ?, ?)", (*key, canonical_json(row)))
            except sqlite3.IntegrityError as error:
                raise ValueError(f"duplicate review: {key}") from error
            count += 1
    return count


def reviewed_batch(
    directory: Path, packages: list[dict], db: sqlite3.Connection, observation_end: str
) -> Path:
    body = b"".join(
        row[0] + b"\n"
        for package in packages
        for row in db.execute(
            "SELECT body FROM reviews WHERE package = ? ORDER BY cutoff", (package["package_id"],)
        )
    )
    output = directory / "reviewed" / digest(body)
    summary = output / "summary.json"
    if not summary.is_file():
        write_reviewed_run(directory, output, body or None, observation_end, resume=True)
    elif json.loads(summary.read_bytes())["cohort_sha256"] != file_digest(
        directory / "cohort.json"
    ):
        raise ValueError(f"reviewed artifact cohort changed for {directory.name}")
    return output


def audit_run(output: Path, reviewed: list[Path], observation_end: str) -> dict:
    def universe():
        for directory in reviewed:
            yield from json.loads((directory / "eligibility.json").read_bytes())

    def strict():
        for directory in reviewed:
            expected = json.loads((directory / "summary.json").read_bytes())["strict_rows"]
            parquet = directory / "strict_e0_rows.parquet"
            if not parquet.is_file() and expected == 0:
                continue
            frame = pl.scan_parquet(parquet)
            columns = [
                "package_id",
                "cutoff_at",
                "repo_id",
                "repo_group_id",
                "horizon_end_at",
                "outcome_end_at",
                "label_available_at",
                "label",
                "in_risk_set",
                "event_effective_at",
            ]
            if "event_id" in frame.collect_schema():
                columns.append("event_id")
            table = frame.select(columns).collect()
            if table.height != expected:
                raise ValueError(f"strict row count changed in {directory}")
            yield from table.iter_rows(named=True)

    audit = audit_splits(universe(), strict(), **SPLIT_TIMES, evaluation_as_of=observation_end)
    write_json_atomic(output / "split-audit.json", audit)
    return audit


def collection_plan(http: HTTPClient, args, frozen: dict) -> tuple[dict, dict, list]:
    output = args.output_dir
    targets = frozen["targets"]
    registry, receipts = historical_registry(http, output, frozen)
    missing = (row for row in targets if row["package_id"] not in registry)
    atomic_bytes(output / "no-pre-cutoff-distribution.jsonl", jsonl(missing), immutable=False)
    groups = [
        [registry[row["package_id"]] for row in group if row["package_id"] in registry]
        for group in target_groups(targets)
    ]
    batches = list(pack_groups([group for group in groups if group], args.batch_size))
    return registry, receipts, batches


def archive_hints(output: Path, targets: list[dict]) -> set[str]:
    """Names select extra lines to retain, never establish historical identity."""
    names = {row["repo_full_name"].casefold() for row in targets if row["repo_full_name"]}
    for directory in sorted(output.glob("batches/*")):
        marker = directory / "collection.json"
        if not marker.is_file():
            continue
        revision = json.loads(marker.read_bytes())["source_reviews_sha256"]
        cached = directory / "archive-hints.json"
        try:
            receipt = json.loads(cached.read_bytes())
        except (OSError, ValueError):
            receipt = {}
        if receipt.get("source_reviews_sha256") != revision:
            found = set()
            for path in directory.glob("shard-*/review.jsonl"):
                with path.open("rb") as stream:
                    for line in stream:
                        row = json.loads(line)
                        found.update(
                            url.removeprefix("https://github.com/").casefold()
                            for url in row.get("explicit_source_urls", ())
                            if url.startswith("https://github.com/")
                        )
            receipt = {"source_reviews_sha256": revision, "names": sorted(found)}
            write_json_atomic(cached, receipt)
        names.update(receipt["names"])
    return names


def run_collection(args, frozen: dict) -> dict:
    output = args.output_dir
    processed = new_batches = package_cutoffs = official_leads = 0
    counts = Counter()
    with HTTPClient(timeout=60, max_retries=2) as http:
        registry, receipts, batches = collection_plan(http, args, frozen)
        for index, groups in enumerate(batches):
            directory = output / "batches" / f"batch-{index:05d}"
            checkpoint = collection_checkpoint(directory)
            if checkpoint is None:
                if args.max_batches and new_batches >= args.max_batches:
                    break
                packages = [item for group in groups for item in group]
                prepare_batch(directory, packages, receipts, frozen)
                checkpoint = collect_batch(http, directory, groups)
                new_batches += 1
                print(f"Collected batch {index + 1}/{len(batches)}", flush=True)
            counts.update(checkpoint["mapping_review"])
            package_cutoffs += checkpoint["package_cutoffs"]
            official_leads += checkpoint["official_leads"]
            processed += 1
    complete = processed == len(batches)
    summary = {
        "status": "completed" if complete else "resume_to_continue",
        "stop_reason": None if complete else "batch_budget_reached",
        "historical_packages": len(registry),
        "batches_processed": processed,
        "batches_total": len(batches),
        "mapping_statuses": dict(counts),
        "package_cutoffs": package_cutoffs,
        "official_leads": official_leads,
    }
    write_json_atomic(output / "collection-summary.json", summary)
    print(
        f"D09 collection: {processed}/{len(batches)} batches | {package_cutoffs} package-cutoffs | {official_leads} review leads",
        flush=True,
    )
    return summary


def run(args, frozen: dict) -> dict:
    output = args.output_dir
    targets = frozen["targets"]
    now = format_utc_timestamp(datetime.now(UTC))
    budget = args.max_downloads
    inventory_path = args.inventory_dir / "inventory.json"
    if not inventory_path.is_file():
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.audit_d09_archive",
                "--output-dir",
                str(args.inventory_dir),
            ],
            check=True,
        )
    inventory_body = inventory_path.read_bytes()
    inventory_receipt = output / "archive-inventory.json"
    inventory_sha = digest(inventory_body)
    if (
        inventory_receipt.exists()
        and json.loads(inventory_receipt.read_bytes())["sha256"] != inventory_sha
    ):
        raise ValueError("the archive inventory differs from the run checkpoint")
    if not inventory_receipt.exists():
        write_json_atomic(
            inventory_receipt, {"path": str(inventory_path.resolve()), "sha256": inventory_sha}
        )
    inventory = json.loads(inventory_body)
    mapping_counts = Counter()
    reviewed = []
    pending = new_batches = 0
    stop_reason = None
    with (
        tempfile.TemporaryDirectory(dir=output, prefix=".reviews-") as temporary,
        closing(sqlite3.connect(Path(temporary) / "index.sqlite3")) as db,
        HTTPClient(timeout=60, max_retries=2) as http,
    ):
        registry, registry_receipts, batch_groups = collection_plan(http, args, frozen)
        args.archive_pool = ArchivePool(output, archive_hints(output, targets))
        print(
            f"Shared archive cache: {len(args.archive_pool.names)} repository hints, "
            f"{len(args.archive_pool.receipts)} saved proofs",
            flush=True,
        )
        reviews_count = index_reviews(db, args.reviews, registry, now)
        for index, groups_in_batch in enumerate(batch_groups):
            directory = output / "batches" / f"batch-{index:05d}"
            verification = verification_checkpoint(directory, args.retry_unverified)
            if verification is None:
                if args.max_batches and new_batches >= args.max_batches:
                    stop_reason = "batch_budget_reached"
                    break
                new_batches += 1
            packages = [item for group in groups_in_batch for item in group]
            prepare_batch(directory, packages, registry_receipts, frozen)
            if collection_checkpoint(directory) is None:
                collect_batch(http, directory, groups_in_batch)
            if verification is None:
                verification, used = verify_mapping(directory, args, inventory, budget)
                budget -= used
            mapping_counts.update(verification["statuses"])
            reviewed.append(reviewed_batch(directory, packages, db, now))
            pending = sum(
                verification["statuses"].get(status, 0)
                for status in ("download_budget_reached", "archive_download_pending")
            )
            print(
                f"Batch {index + 1}/{len(batch_groups)} | verified package-cutoffs {mapping_counts['mapping_sources_matched']} | package-cutoffs waiting for archives {pending}",
                flush=True,
            )
            if pending:
                stop_reason = (
                    "archive_download_budget_reached" if not budget else "archive_downloads_pending"
                )
                break

    audit = audit_run(output, reviewed, now)
    complete = len(reviewed) == len(batch_groups) and not pending
    summary = {
        "status": "completed" if complete else "resume_to_continue",
        "stop_reason": stop_reason,
        "screening_rows": frozen["drive_metadata"]["row_count"],
        "targets": len(targets),
        "historical_packages": len(registry),
        "no_observed_pre_cutoff_distribution": len(targets) - len(registry),
        "batches_processed": len(reviewed),
        "batches_total": len(batch_groups),
        "archive_downloads_this_invocation": args.max_downloads - budget,
        "mapping_statuses": dict(mapping_counts),
        "review_input": str(args.reviews.resolve()),
        "supplied_reviews": reviews_count,
        "reviewed_directories": [item.relative_to(output).as_posix() for item in reviewed],
        "split_audit_status": audit["status"],
        "strict_rows": audit["input_strict_rows"],
    }
    write_json_atomic(output / "summary.json", summary)
    print(f"D09 results: {output.resolve()}", flush=True)
    if not complete:
        print(f"Pass stopped at {stop_reason}.", flush=True)
    return summary


def wait_for_provider(output: Path, continuous: bool) -> bool:
    path = output / "provider-quota.json"
    if not path.is_file():
        return True
    retry_at = checked_timestamp(json.loads(path.read_bytes())["retry_at_utc"])
    delay = datetime.fromisoformat(retry_at).timestamp() - time.time()
    if delay <= 0:
        return True
    if continuous:
        print(f"Public ClickHouse quota reached. Waiting until {retry_at} to resume.", flush=True)
        time.sleep(delay)
    else:
        print(
            f"Public ClickHouse quota reached. Checkpoint saved. Retry after {retry_at}.",
            flush=True,
        )
    return continuous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--all", action="store_true", help="Use every package in the frozen Drive screening"
    )
    selection.add_argument(
        "--limit", type=int, default=1000, help="Pilot target count; keeps repository groups intact"
    )
    parser.add_argument("--screening-dir", type=Path, default=DEFAULT_SCREENING)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--inventory-dir", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument(
        "--reviews",
        type=Path,
        help="ReviewedD09Target JSONL; defaults to reviews.jsonl in the output directory",
    )
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=30,
        help="New GH Archive files allowed in this invocation",
    )
    parser.add_argument(
        "--archive-workers",
        type=int,
        default=DEFAULT_ARCHIVE_WORKERS,
        help="Concurrent archive checks (1–8)",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        help="New batches per invocation; 0 for unlimited (default 5 with --all)",
    )
    parser.add_argument(
        "--retry-unverified",
        action="store_true",
        help="Retry archived or PyPI requests that previously failed",
    )
    parser.add_argument(
        "--continuous", action="store_true", help="Automatically continue successive bounded passes"
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Collect the review queue across batches without raw archive verification",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Freeze Drive targets and display counts without network requests",
    )
    args = parser.parse_args()
    args.output_dir = args.output_dir or Path("data/d09") / (
        "drive-full-v1" if args.all else "drive-pilot-v1"
    )
    if args.reviews is not None and not args.reviews.is_file():
        parser.error("the supplied reviews file does not exist")
    args.reviews = args.reviews or args.output_dir / "reviews.jsonl"
    if args.max_batches is None:
        args.max_batches = 5 if args.all else 0
    if (
        args.limit < 1
        or not 1 <= args.batch_size <= 500
        or not 1 <= args.archive_workers <= MAX_ARCHIVE_WORKERS
        or min(args.max_downloads, args.max_batches) < 0
    ):
        parser.error(
            "expected positive target limit, batch size 1–500, 1–8 archive workers and nonnegative budgets"
        )
    if args.archive_dir is not None and not args.archive_dir.is_dir():
        parser.error("archive-dir must be an existing directory")
    if args.continuous and args.retry_unverified:
        parser.error("run --retry-unverified separately from continuous collection")
    config = {
        "version": 1,
        "screening_dir": str(args.screening_dir.absolute()),
        "limit": None if args.all else args.limit,
        "batch_size": args.batch_size,
    }
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output_dir):
            frozen = freeze_targets(args.screening_dir.absolute(), args.output_dir, config)
            print(
                f"Frozen Drive targets: {len(frozen['targets'])}/{frozen['drive_metadata']['row_count']}",
                flush=True,
            )
            if args.plan_only:
                return 0
            previous = None
            while True:
                if not wait_for_provider(args.output_dir, args.continuous):
                    break
                try:
                    summary = (
                        run_collection(args, frozen) if args.collect_only else run(args, frozen)
                    )
                except RateLimitExceededError as error:
                    now = time.time()
                    retry_at = (
                        max(now + 5, error.reset_timestamp + 2)
                        if error.reset_timestamp
                        else now + 300
                    )
                    write_json_atomic(
                        args.output_dir / "provider-quota.json",
                        {
                            "status": "waiting_for_public_clickhouse_quota",
                            "retry_at_utc": format_utc_timestamp(
                                datetime.fromtimestamp(retry_at, UTC)
                            ),
                        },
                    )
                    continue
                if summary["status"] == "completed":
                    break
                progress = (
                    summary["batches_processed"],
                    summary["mapping_statuses"],
                    sum(1 for _ in args.output_dir.glob("batches/*/verification/archives/*.json")),
                    sum(1 for _ in args.output_dir.glob("archive-cache/archives/*.json")),
                    sum(
                        path.stat().st_size
                        for path in args.output_dir.glob("batches/*/verification/*.part")
                    )
                    + sum(
                        path.stat().st_size for path in args.output_dir.glob("archive-cache/*.part")
                    ),
                )
                if (
                    not args.continuous
                    or progress == previous
                    or (
                        args.max_downloads == 0
                        and summary["stop_reason"] == "archive_download_budget_reached"
                    )
                ):
                    print("Run the same command again to continue from the checkpoint.", flush=True)
                    break
                previous = progress
                time.sleep(5)
    except (
        HTTPClientError,
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
        subprocess.CalledProcessError,
        pl.exceptions.PolarsError,
    ) as error:
        parser.exit(1, f"D09 Drive run stopped; rerun the same command to resume: {error}\n")
    except KeyboardInterrupt:
        print("D09 interrupted. Run the same command again to resume.", flush=True)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
