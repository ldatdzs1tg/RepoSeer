"""Coordinator-only verification, immutable run acceptance and recoverable catalog."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import polars as pl

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.coordination._common import (
    CoordinationError,
    Drive,
    Registry,
    artifact_row,
    check_claim,
    cli_guard,
    fingerprint,
    finish_job,
    integration_evidence,
    parser,
    read_checksums,
    read_json,
    require,
    timestamp,
    uid,
    validate_assignment,
    validate_ready,
    verify_bundle,
    worker_id,
    write_json,
)


def validate_submission(root, *, worker=None, job_id=None, run_id=None):
    marker, assignment, inventory = verify_bundle(root)
    require(assignment["kind"] == "collect_repository", "crawl_assignment_required")
    receipt = read_json(Path(root) / "COORDINATION_RECEIPT.json")
    for field in ("job_id", "worker_id", "claim_token", "assignment_sha256"):
        require(receipt.get(field) == assignment[field], "receipt_assignment_mismatch")
    run = uid(receipt.get("integration_run_id"))
    require(
        marker.get("run_id") == run and (run_id is None or run_id == run),
        "receipt_ready_run_mismatch",
    )
    require(worker is None or worker == assignment["worker_id"], "submission_worker_mismatch")
    require(job_id is None or job_id == assignment["job_id"], "submission_job_mismatch")
    facts = integration_evidence(root, assignment, run)
    require(
        all(receipt.get(key) == value for key, value in facts.items()), "receipt_evidence_mismatch"
    )
    return assignment, receipt, inventory


def ingest_one(
    root, drive, registry, *, expected_worker=None, expected_job=None, expected_run=None
):
    assignment, receipt, inventory = validate_submission(
        root, worker=expected_worker, job_id=expected_job, run_id=expected_run
    )
    run_id = receipt["integration_run_id"]
    prefix = f"canonical/runs/{run_id}"
    bundle_path = drive.path(prefix + "/CHECKSUMS.sha256")
    sha, size = inventory["CHECKSUMS.sha256"]
    with registry.connect() as db:
        # Serialize receipts and replay for the same run, including two coordinator processes.
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (run_id,))
        existing = db.execute(
            "SELECT * FROM reposeer.artifacts WHERE storage_path=%s FOR UPDATE", (bundle_path,)
        ).fetchone()
        if existing:
            require(
                existing["sha256"] == sha
                and str(existing["run_id"]) == run_id
                and existing["artifact_kind"] == "bundle"
                and existing["status"] == "ingested"
                and existing["byte_size"] == sum(s for _, s in inventory.values()),
                "ingested_bundle_checksum_conflict",
            )
            return dict(artifact_accepted=True, duplicate=True, job_collected=False)
        job = db.execute(
            "SELECT j.*,r.repo_full_name FROM reposeer.crawl_jobs j JOIN reposeer.repositories r USING(repository_id) WHERE job_id=%s FOR UPDATE OF j,r",
            (assignment["job_id"],),
        ).fetchone()
        check_claim(job, assignment)
        numeric = receipt["verified_github_repo_id"]
        if numeric is not None:
            duplicate = db.execute(
                "SELECT repository_id FROM reposeer.repositories WHERE github_repo_id=%s AND repository_id<>%s",
                (numeric, job["repository_id"]),
            ).fetchone()
            require(not duplicate, "github_numeric_id_collision_quarantine")
            current = db.execute(
                "SELECT github_repo_id FROM reposeer.repositories WHERE repository_id=%s",
                (job["repository_id"],),
            ).fetchone()
            require(
                current["github_repo_id"] in {None, numeric}, "repository_numeric_identity_changed"
            )
        # Publishing precedes DB acceptance. A crash here leaves a reusable immutable
        # directory, never an ingested DB row pointing at an incomplete upload.
        drive.upload(root, prefix)
        db.execute(
            "INSERT INTO reposeer.crawl_runs(run_id,worker_id,job_id,run_kind,status,started_at,finished_at,error_code) VALUES (%s,%s,%s,'integration',%s,%s,%s,%s)",
            (
                run_id,
                assignment["worker_id"],
                assignment["job_id"],
                receipt["overall_status"],
                timestamp(receipt["started_at"]),
                timestamp(receipt["finished_at"]),
                None if receipt["collected_repository"] else "repository_not_collected",
            ),
        )
        if numeric is not None:
            db.execute(
                "UPDATE reposeer.repositories SET github_repo_id=%s,verification_status='verified_current',verified_at=now(),last_collected_at=%s WHERE repository_id=%s",
                (numeric, timestamp(receipt["finished_at"]), job["repository_id"]),
            )
            for package in receipt["verified_package_ids"]:
                db.execute(
                    "UPDATE reposeer.packages SET mapping_status='verified_current' WHERE package_id=%s AND repository_id=%s",
                    (package, job["repository_id"]),
                )
        inserted = artifact_row(
            db, run_id, "bundle", bundle_path, sha, sum(s for _, s in inventory.values())
        )
        for field, kind in [
            ("manifest_path", "manifest"),
            ("collection_profile_path", "profile"),
            ("data_profile_path", "profile"),
        ]:
            path = receipt[field]
            file_sha, file_size = inventory[path]
            artifact_row(db, run_id, kind, drive.path(prefix + "/" + path), file_sha, file_size)
        job_status = finish_job(
            db, job, None if receipt["collected_repository"] else "repository_not_collected"
        )
        db.execute(
            "UPDATE reposeer.workers SET last_seen_at=now() WHERE worker_id=%s",
            (assignment["worker_id"],),
        )
    return dict(
        artifact_accepted=True,
        artifacts_ingested=4 if inserted else 0,
        job_collected=receipt["collected_repository"],
        job_status=job_status,
        overall_status=receipt["overall_status"],
        integration_run_id=run_id,
    )


CATALOG_SCHEMA = dict(
    integration_run_id=pl.String,
    job_id=pl.String,
    worker_id=pl.String,
    repo_full_name=pl.String,
    github_repo_id=pl.Int64,
    package_ids=pl.List(pl.String),
    overall_status=pl.String,
    stored_artifact_path=pl.String,
    sha256=pl.String,
    ingested_at=pl.String,
)


def rebuild_catalog(drive, registry):
    rows = []
    with registry.connect(readonly=True) as db:
        accepted = db.execute(
            "SELECT a.run_id,a.storage_path,a.sha256,a.ingested_at,c.job_id,c.worker_id,c.status FROM reposeer.artifacts a JOIN reposeer.crawl_runs c USING(run_id) WHERE a.artifact_kind='bundle' AND a.status='ingested' AND c.run_kind='integration' ORDER BY a.run_id"
        ).fetchall()
    # Parquet is built on local TEMP, never as a mutable analytical DB on Drive.
    with tempfile.TemporaryDirectory(prefix="reposeer-catalog-") as scratch:
        scratch = Path(scratch)
        for row in accepted:
            run_id = uid(str(row["run_id"]))
            prefix = f"canonical/runs/{run_id}"
            checksum = scratch / "CHECKSUMS.sha256"
            receipt_path, assignment_path = scratch / "receipt.json", scratch / "assignment.json"
            drive._run(["copyto", drive.path(prefix + "/CHECKSUMS.sha256"), str(checksum)])
            require(fingerprint(checksum)[0] == row["sha256"], "catalog_bundle_checksum_mismatch")
            inventory = read_checksums(checksum)
            for name, target in [
                ("COORDINATION_RECEIPT.json", receipt_path),
                ("ASSIGNMENT.json", assignment_path),
            ]:
                drive._run(["copyto", drive.path(prefix + "/" + name), str(target)])
                require(fingerprint(target) == inventory[name], "catalog_receipt_checksum_mismatch")
            receipt, assignment = (
                read_json(receipt_path),
                validate_assignment(read_json(assignment_path), allow_expired=True),
            )
            rows.append(
                dict(
                    integration_run_id=run_id,
                    job_id=str(row["job_id"]),
                    worker_id=row["worker_id"],
                    repo_full_name=receipt["expected_repo_full_name"],
                    github_repo_id=receipt["verified_github_repo_id"],
                    package_ids=assignment["targets"],
                    overall_status=row["status"],
                    stored_artifact_path=drive.path(prefix),
                    sha256=row["sha256"],
                    ingested_at=row["ingested_at"].isoformat(),
                )
            )
        table = pl.DataFrame(rows, schema=CATALOG_SCHEMA)
        temporary, final = scratch / "catalog.tmp", scratch / "ingestion_index.parquet"
        table.write_parquet(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        sha, _ = fingerprint(final)
        version = f"canonical/catalog/snapshots/{sha}.parquet"
        drive.put(final, version)
        drive.put(final, "canonical/catalog/ingestion_index.parquet", replace=True)
        pointer = scratch / "active.json"
        write_json(
            pointer, dict(schema_version=1, relative_path=version, sha256=sha, row_count=len(rows))
        )
        drive.put(pointer, "canonical/catalog/active.json", replace=True)
    return len(rows)


def _run(remote, workspace, *, limit=25, dry_run=False, registry=None):
    require(1 <= limit <= 1000, "invalid_ingestion_limit")
    registry, drive = registry or Registry(), Drive(remote)
    drive.check()
    listing = drive.listing("crawl/submissions")
    results, pending, rejected = [], [], []
    for path in sorted(listing):
        if not path.endswith("/READY.json"):
            continue
        parts = path.split("/")
        require(len(parts) == 4, "invalid_submission_path")
        worker, job, run_id, _ = parts
        worker_id(worker, "crawler")
        uid(job)
        uid(run_id)
        prefix = "crawl/submissions/" + "/".join(parts[:3])
        # Read just the small READY marker for previously accepted submissions.
        marker = validate_ready(drive.get_json(prefix + "/READY.json", byte_size=listing[path]))
        require(
            marker.get("run_id") == run_id
            and marker.get("job_id") == job
            and marker["worker_id"] == worker,
            "ready_submission_path_mismatch",
        )
        with registry.connect(readonly=True) as db:
            previous = db.execute(
                "SELECT a.sha256,c.worker_id,c.job_id FROM reposeer.artifacts a JOIN reposeer.crawl_runs c USING(run_id) WHERE a.storage_path=%s AND a.status='ingested' AND a.artifact_kind='bundle' AND c.run_kind='integration'",
                (drive.path(f"canonical/runs/{run_id}/CHECKSUMS.sha256"),),
            ).fetchone()
        if previous:
            require(
                previous["sha256"] == marker.get("payload_sha256")
                and previous["worker_id"] == worker
                and str(previous["job_id"]) == job
                and marker.get("run_id") == run_id
                and marker.get("job_id") == job
                and marker.get("worker_id") == worker,
                "submitted_path_checksum_conflict",
            )
            continue
        sizes = {
            name: size
            for name, size in listing.items()
            if name.startswith("/".join(parts[:3]) + "/")
        }
        pending.append(
            dict(
                worker_id=worker,
                job_id=job,
                integration_run_id=run_id,
                estimated_bytes=sum(sizes.values()),
            )
        )
        if dry_run or len(results) + len(rejected) >= limit:
            continue
        root = Path(workspace) / "incoming" / run_id
        try:
            drive.download(prefix, root)
            result = ingest_one(
                root, drive, registry, expected_worker=worker, expected_job=job, expected_run=run_id
            )
            results.append(result)
            write_json(Path(workspace) / "accepted" / f"{run_id}.json", result)
        except (CoordinationError, ValueError, KeyError, TypeError, OSError) as error:
            code = (
                str(error)
                if isinstance(error, CoordinationError)
                else "invalid_or_unreadable_bundle"
            )
            rejection = dict(integration_run_id=run_id, error_code=code)
            write_json(
                Path(workspace) / "quarantine" / f"{run_id}.json", rejection, immutable=False
            )
            rejected.append(rejection)
    catalog = None if dry_run else rebuild_catalog(drive, registry)
    return dict(
        dry_run=dry_run,
        pending=pending,
        accepted=results,
        rejected=rejected,
        artifacts_ingested=sum(r.get("artifacts_ingested", 0) for r in results),
        jobs_completed=sum(r.get("job_status") == "completed" for r in results),
        jobs_retry=sum(r.get("job_status") == "retry" for r in results),
        catalog_rows=catalog,
    )


def run(remote, workspace, *, limit=25, dry_run=False, registry=None):
    registry = registry or Registry()
    if dry_run:
        return _run(remote, workspace, limit=limit, dry_run=True, registry=registry)
    # Guard catalog publication as well as run acceptance across coordinator processes.
    with registry.connect() as guard:
        acquired = guard.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
            ("reposeer:canonical-writer",),
        ).fetchone()
        require(acquired and acquired["acquired"], "another_ingest_writer_is_active")
        return _run(remote, workspace, limit=limit, registry=registry)


def main():
    cli = parser(__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("run")
    sub.add_argument("--remote", default=argparse.SUPPRESS)
    sub.add_argument("--workspace", type=Path, required=True)
    sub.add_argument("--limit", type=int, default=25)
    sub.add_argument("--dry-run", action="store_true")
    args = cli.parse_args()
    result = run(args.remote, args.workspace, limit=args.limit, dry_run=args.dry_run)
    if result["rejected"]:
        print(json.dumps(result, default=str))
        raise SystemExit(1)
    return result


if __name__ == "__main__":
    cli_guard(main)
