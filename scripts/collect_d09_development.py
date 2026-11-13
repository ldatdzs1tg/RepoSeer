"""Collect a declared, small development cohort without changing the full Drive run."""

import argparse
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from scripts.collect_d09_batch import (
    EXTENDED_CUTOFFS,
    archive_timestamp,
    collect_official_leads,
    observe_repo_ids,
    parse_rows,
    query_rows,
    review_rows,
    sql_list,
)
from scripts.collect_d09_pilot import (
    fetch_public,
    provider_time,
    pypi_json,
    run_lock,
    stored_source_matches,
)
from scripts.d09_archive_pool import ArchivePool, saved_proofs
from scripts.d09_artifact_metadata import core_metadata
from scripts.run_d09_drive import (
    digest,
    file_digest,
    jsonl,
    prepare_batch,
    shard_reviews_digest,
)
from scripts.verify_d09_evidence import load_candidates, verify_batch
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import package_id
from reposeer.exceptions import HTTPClientError

DEFAULT_PACKAGES = [
    "django-transaction-hooks",
    "yamlfmt",
    "ciscoconfparse",
    "sretoolbox",
    "convtools",
    "logwrap",
]


def repair_mapping_metadata(http, shard, row):
    key = digest(f"{row['package_id']}:{row['source_version']}".encode())
    receipt_path = shard / "repairs" / f"{key}.json"
    packet = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
    if not packet or not stored_source_matches(shard, packet["artifact"]):
        version, src = pypi_json(
            http,
            f"https://pypi.org/pypi/{row['package_id'][5:]}/{row['source_version']}/json",
            shard,
            f"repairs/{key}-version",
        )
        if (
            package_id(version["info"]["name"]) != row["package_id"]
            or version["info"]["version"] != row["source_version"]
        ):
            raise ValueError("repair response has a different package or version")
        item = next(
            (
                item
                for item in version["urls"]
                if item["digests"]["sha256"] == row["distribution_sha256"]
            ),
            None,
        )
        if (
            item is None
            or not item["url"].startswith("https://files.pythonhosted.org/")
            or provider_time(item["upload_time_iso_8601"]) >= provider_time(row["cutoff_at"])
        ):
            raise ValueError("repair artifact is missing or not available at cutoff")
        body, artifact = fetch_public(http, item["url"], 32 * 1024 * 1024)
        if digest(body) != row["distribution_sha256"]:
            raise ValueError("repair artifact checksum mismatch")
        relative = f"repairs/{key}-{digest(body)}.distribution"
        atomic_bytes(shard / relative, body, immutable=False)
        artifact["path"] = relative
        packet = {"artifact": artifact, "version_source": src}
        write_json_atomic(receipt_path, packet)
    metadata, roots, homepage = core_metadata(
        (shard / packet["artifact"]["path"]).read_bytes(), row["package_id"], row["source_version"]
    )
    row["explicit_source_urls"] = sorted(roots)
    row["archive_names"] = [root.removeprefix("https://github.com/") for root in roots]
    if homepage:
        row["source_link_policy"] = "github-homepage-candidate-v1"
    row["metadata_repair_source"] = packet["artifact"]
    atomic_bytes(shard / "repairs" / f"{key}-metadata.txt", metadata, immutable=False)


def run(args):
    output, parent = args.output_dir, args.drive_run
    frozen = json.loads((parent / "drive-targets.json").read_bytes())
    registry = json.loads((parent / "historical-registry.json").read_bytes())
    subjects = sorted({package_id(name.removeprefix("pypi:")) for name in args.package})
    if any(subject not in registry["packages"] for subject in subjects):
        raise ValueError("development subjects must belong to the frozen historical Drive cohort")
    wanted_cutoffs = set(args.verification_cutoff)
    wanted_targets = getattr(args, "target_keys", set())
    scope_path = output / "verification-scope.json"
    scope = {
        "cutoffs": sorted(wanted_cutoffs),
        "other_rows": "audit_only_unless_previously_verified",
    }
    if wanted_targets:
        scope["package_cutoffs"] = sorted([subject, cutoff] for subject, cutoff in wanted_targets)
    if scope_path.exists() and json.loads(scope_path.read_bytes()) != scope:
        raise ValueError("development verification scope changed; use another output directory")
    write_json_atomic(scope_path, scope)

    def in_scope(row):
        if wanted_targets:
            return (row["package_id"], row["cutoff_at"]) in wanted_targets
        return row["cutoff_at"] in wanted_cutoffs

    config = {
        "subjects": subjects,
        "drive_targets_sha256": frozen["targets_sha256"],
        "cutoffs": list(EXTENDED_CUTOFFS),
        "selection": "explicit enriched development cohort; not a representative calibration population",
    }
    path = output / "development-cohort.json"
    if path.exists() and json.loads(path.read_bytes()) != config:
        raise ValueError("development cohort changed; use another output directory")
    write_json_atomic(path, config)
    for filename in ("drive-targets.json", "historical-registry.json"):
        atomic_bytes(output / filename, (parent / filename).read_bytes(), immutable=False)
    write_json_atomic(
        output / "archive-inventory.json",
        {
            "path": str((args.inventory_dir / "inventory.json").resolve()),
            "sha256": file_digest(args.inventory_dir / "inventory.json"),
        },
    )
    packages = [registry["packages"][subject] for subject in subjects]
    directory = output / "batches/batch-00000"
    if not (directory / "cohort.json").exists():
        prepare_batch(directory, packages, registry["registry_receipts"], frozen)
        cohort = json.loads((directory / "cohort.json").read_bytes())
        cohort["config"].update(
            cutoffs=list(EXTENDED_CUTOFFS), cutoff_policy="quarterly-2019-2024-v2"
        )
        write_json_atomic(directory / "cohort.json", cohort)
    shard = directory / "shard-0000"
    names = {name for package in packages for name in package["source_names"]}
    query = f"""SELECT name, version, toString(upload_time, 'UTC') AS uploaded_at,
       sha256_digest AS sha256, project_urls, home_page, requires_python
FROM pypi.projects WHERE name IN ({sql_list(names)})
  AND upload_time < toDateTime64('2026-01-01 00:00:00', 3, 'UTC')
ORDER BY name, version, uploaded_at, sha256
FORMAT JSONEachRow"""
    with HTTPClient(timeout=60, max_retries=2) as http:
        records = query_rows(http, shard, "distributions-development-v2", query)
        history = {}
        for row in records:
            row["uploaded_at"] = archive_timestamp(row["uploaded_at"])
            subject = package_id(row["name"])
            versions = history.setdefault(subject, {})
            prior = versions.get(row["version"])
            if prior is None or row["uploaded_at"] < prior["uploaded_at"]:
                versions[row["version"]] = row
        history = {
            subject: sorted(versions.values(), key=lambda row: (row["uploaded_at"], row["version"]))
            for subject, versions in history.items()
        }
        rows = list(
            review_rows(packages, history, cutoffs=EXTENDED_CUTOFFS, allow_github_homepage=True)
        )
        for row in rows:
            if not row["archive_names"] and in_scope(row):
                try:
                    repair_mapping_metadata(http, shard, row)
                except (ValueError, HTTPClientError, KeyError) as error:
                    row["metadata_repair_error"] = str(error)
        for row in rows:
            row["archive_window_start_at"] = "2018-01-01T00:00:00.000Z"
            row["identity_event_policy"] = "all-public-v1"
        ids = observe_repo_ids(
            http, shard, rows, window_start="2018-01-01T00:00:00.000Z", identity_events=True
        )
        leads = collect_official_leads(http, shard, ids)
    raw = jsonl(rows)
    atomic_bytes(shard / "review.jsonl", raw, immutable=False)
    write_json_atomic(
        shard / "collection.json",
        {
            "path": "review.jsonl",
            "content_sha256": digest(raw),
            "packages": len(packages),
            "package_cutoffs": len(rows),
            "mapping_review": dict(Counter(row["mapping_review"] for row in rows)),
            "official_leads": sum(leads.values()),
        },
    )
    write_json_atomic(
        directory / "collection.json",
        {
            "source_reviews_sha256": shard_reviews_digest(directory),
            "packages": len(packages),
            "package_cutoffs": len(rows),
            "mapping_review": dict(Counter(row["mapping_review"] for row in rows)),
            "official_leads": sum(leads.values()),
        },
    )
    pool = ArchivePool(
        output, {row["repo_full_name"] for row in frozen["targets"] if row["repo_full_name"]}
    )
    # Read existing proofs from the live run, but write downloads only in this cohort.
    for origin in [
        parent / "archive-cache",
        *parent.glob("batches/*/verification"),
        *args.proof_dir,
    ]:
        for base, source in saved_proofs(origin):
            pool.remember(base, source)
    candidates = load_candidates(directory, set())
    destination = directory / "verification"
    destination.mkdir(exist_ok=True)
    inventory = json.loads((args.inventory_dir / "inventory.json").read_bytes())
    selected = [row for row in candidates if in_scope(row)]
    results, downloads = verify_batch(
        SimpleNamespace(
            output_dir=destination,
            inventory_dir=args.inventory_dir,
            archive_dir=None,
            archive_pool=pool,
            archive_workers=2,
            archive_download_seconds=600,
            max_downloads=args.max_downloads,
        ),
        selected,
        inventory,
    )
    verified = {(row["package_id"], row["cutoff_at"]): row for row in results}
    previous = destination / "mapping_evidence.jsonl"
    old = {}
    if previous.exists():
        old = {
            (row["package_id"], row["cutoff_at"]): row
            for row in parse_rows(previous.read_bytes())
            if row["status"] == "mapping_sources_matched"
        }
    results = [
        verified.get(
            (row["package_id"], row["cutoff_at"]),
            {
                **old.get(
                    (row["package_id"], row["cutoff_at"]),
                    {"status": "verification_deferred_development_scope"},
                ),
                **row,
            },
        )
        for row in candidates
    ]
    body = jsonl(results)
    atomic_bytes(destination / "mapping_evidence.jsonl", body, immutable=False)
    summary = {
        "path": "mapping_evidence.jsonl",
        "content_sha256": digest(body),
        "candidate_rows": len(candidates),
        "source_reviews_sha256": shard_reviews_digest(directory),
        "statuses": dict(Counter(row["status"] for row in results)),
    }
    write_json_atomic(destination / "summary.json", summary)
    write_json_atomic(
        output / "summary.json",
        {
            "selection": config["selection"],
            "mapping_statuses": summary["statuses"],
            "downloads": downloads,
        },
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive-run", type=Path, default=Path("data/d09/drive-full-v1"))
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/d09/model-a-development-cohort-v1")
    )
    parser.add_argument(
        "--inventory-dir", type=Path, default=Path("data/d09/archive-inventory-2018-2025")
    )
    parser.add_argument("--package", action="append")
    parser.add_argument("--max-downloads", type=int, default=12)
    parser.add_argument("--verification-cutoff", action="append")
    parser.add_argument(
        "--target",
        action="append",
        metavar="PACKAGE@CUTOFF",
        help="Verify only the specified package-cutoff pair; repeat for other pairs",
    )
    parser.add_argument(
        "--proof-dir",
        action="append",
        type=Path,
        default=[],
        help="Read and reuse another collection's original archive receipts",
    )
    args = parser.parse_args()
    args.target_keys = set()
    if args.target:
        if args.package or args.verification_cutoff:
            parser.error("use --target or --package/--verification-cutoff")
        try:
            for value in args.target:
                name, cutoff = value.rsplit("@", 1)
                if cutoff not in EXTENDED_CUTOFFS:
                    raise ValueError("target cutoff is outside the frozen quarterly policy")
                args.target_keys.add((package_id(name.removeprefix("pypi:")), cutoff))
        except ValueError as error:
            parser.error(str(error))
        args.package = sorted({subject for subject, _ in args.target_keys})
        args.verification_cutoff = sorted({cutoff for _, cutoff in args.target_keys})
    args.package = args.package or DEFAULT_PACKAGES
    args.verification_cutoff = args.verification_cutoff or [
        "2019-12-31T23:59:59.999Z",
        "2022-12-31T23:59:59.999Z",
        "2024-09-30T23:59:59.999Z",
    ]
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output_dir):
            result = run(args)
    except (OSError, ValueError, HTTPClientError) as error:
        parser.exit(1, f"Development collection stopped; rerun to resume: {error}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
