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
    format_utc_timestamp,
    integration_evidence,
    notification,
    now,
    parser,
    read_json,
    require,
    timestamp,
    uid,
    validate_notification,
    validate_ready,
    verify_bundle,
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
        all(
            receipt.get(key) == value
            for key, value in facts.items()
            if key != "activity_coverage" or key in receipt
        ),
        "receipt_evidence_mismatch",
    )
    return assignment, dict(receipt, **facts), inventory


def catalog_snapshot(assignment, receipt, drive, sha, ingested_at):
    return dict(
        integration_run_id=receipt["integration_run_id"],
        job_id=assignment["job_id"],
        worker_id=assignment["worker_id"],
        repo_full_name=receipt["expected_repo_full_name"],
        github_repo_id=receipt["verified_github_repo_id"],
        package_ids=list(assignment["targets"]),
        overall_status=receipt["overall_status"],
        stored_artifact_path=drive.path("canonical/runs/" + receipt["integration_run_id"]),
        sha256=sha,
        ingested_at=format_utc_timestamp(ingested_at),
    )


def check_repository(db, assignment, receipt, *, lock=False):
    job = db.execute(
        "SELECT j.*,r.repo_full_name FROM reposeer.crawl_jobs j JOIN reposeer.repositories r USING(repository_id) WHERE job_id=%s"
        + (" FOR UPDATE OF j,r" if lock else ""),
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
        require(current["github_repo_id"] in {None, numeric}, "repository_numeric_identity_changed")
    return job


def previous_run(db, bundle_path, assignment, receipt, inventory):
    existing = db.execute(
        "SELECT a.*,c.worker_id,c.job_id FROM reposeer.artifacts a JOIN reposeer.crawl_runs c USING(run_id) WHERE a.storage_path=%s AND a.artifact_kind='bundle' AND a.status='ingested' AND c.run_kind='integration'",
        (bundle_path,),
    ).fetchone()
    if existing:
        require(
            existing["sha256"] == inventory["CHECKSUMS.sha256"][0]
            and str(existing["run_id"]) == receipt["integration_run_id"]
            and existing["worker_id"] == assignment["worker_id"]
            and str(existing["job_id"]) == assignment["job_id"]
            and existing["byte_size"] == sum(size for _, size in inventory.values()),
            "ingested_bundle_checksum_conflict",
        )
    return existing


def ingest_one(
    root, drive, registry, *, expected_worker=None, expected_job=None, expected_run=None
):
    from psycopg.types.json import Jsonb

    assignment, receipt, inventory = validate_submission(
        root, worker=expected_worker, job_id=expected_job, run_id=expected_run
    )
    run_id = receipt["integration_run_id"]
    prefix = f"canonical/runs/{run_id}"
    bundle_path = drive.path(prefix + "/CHECKSUMS.sha256")
    sha = inventory["CHECKSUMS.sha256"][0]
    # Preflight is advisory only; close its transaction before any upload.
    with registry.connect(readonly=True) as db:
        if previous_run(db, bundle_path, assignment, receipt, inventory):
            return dict(artifact_accepted=True, duplicate=True, job_collected=False)
        check_repository(db, assignment, receipt)
    drive.upload(root, prefix)
    # Recompute local evidence before acquiring row locks, too.
    fresh = validate_submission(root)
    require(fresh == (assignment, receipt, inventory), "bundle_changed_during_transfer")
    # Short acceptance transaction rechecks the claim after the potentially slow transfer.
    with registry.connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (run_id,))
        if previous_run(db, bundle_path, assignment, receipt, inventory):
            return dict(artifact_accepted=True, duplicate=True, job_collected=False)
        job = check_repository(db, assignment, receipt, lock=True)
        snapshot = catalog_snapshot(assignment, receipt, drive, sha, now())
        db.execute(
            "INSERT INTO reposeer.crawl_runs(run_id,worker_id,job_id,run_kind,status,started_at,finished_at,error_code,catalog_metadata) VALUES (%s,%s,%s,'integration',%s,%s,%s,%s,%s)",
            (
                run_id,
                assignment["worker_id"],
                assignment["job_id"],
                receipt["overall_status"],
                timestamp(receipt["started_at"]),
                timestamp(receipt["finished_at"]),
                None if receipt["collected_repository"] else "repository_not_collected",
                Jsonb(snapshot),
            ),
        )
        numeric, coverage = receipt["verified_github_repo_id"], receipt["activity_coverage"]
        if numeric is not None:
            db.execute(
                "UPDATE reposeer.repositories SET github_repo_id=%s,verification_status='verified_current',verified_at=now(),last_collected_at=%s,activity_coverage_status=%s,activity_coverage_json=%s,last_coverage_at=%s WHERE repository_id=%s",
                (
                    numeric,
                    timestamp(receipt["finished_at"]),
                    coverage["status"],
                    Jsonb(coverage),
                    timestamp(receipt["finished_at"]),
                    job["repository_id"],
                ),
            )
            evidence = dict(
                integration_run_id=run_id,
                historical_validity="unverified",
                github_path=prefix + "/raw/github/repositories.jsonl",
                github_sha256=inventory["raw/github/repositories.jsonl"][0],
                pypi_path=prefix + "/raw/pypi/packages.jsonl",
                pypi_sha256=inventory["raw/pypi/packages.jsonl"][0],
            )
            for package in receipt["verified_package_ids"]:
                db.execute(
                    "UPDATE reposeer.packages SET mapping_status='verified_current',mapping_evidence=%s,mapping_verified_at=now() WHERE package_id=%s AND repository_id=%s",
                    (Jsonb(evidence), package, job["repository_id"]),
                )
        inserted = artifact_row(
            db, run_id, "bundle", bundle_path, sha, sum(size for _, size in inventory.values())
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
        followup = None
        if receipt["collected_repository"] and coverage["status"] == "budget_limited":
            followup = {"initial": "followup:1", "followup:1": "followup:2"}.get(job["scope_key"])
            if followup:
                db.execute(
                    "INSERT INTO reposeer.crawl_jobs(job_kind,repository_id,scope_key,priority) VALUES ('collect_repository',%s,%s,%s) ON CONFLICT DO NOTHING RETURNING job_id",
                    (job["repository_id"], followup, job["priority"]),
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
        activity_coverage=coverage,
        followup_scope=followup,
        collection_policy="bounded_from_first_page_not_cursor_resume",
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
    with registry.connect(readonly=True) as db:
        accepted = db.execute(
            "SELECT c.run_id,c.catalog_metadata FROM reposeer.crawl_runs c WHERE c.run_kind='integration' AND c.catalog_metadata IS NOT NULL AND EXISTS (SELECT 1 FROM reposeer.artifacts a WHERE a.run_id=c.run_id AND a.artifact_kind='bundle' AND a.status='ingested') ORDER BY c.run_id"
        ).fetchall()
        missing = db.execute(
            "SELECT count(*) AS count FROM reposeer.crawl_runs c WHERE c.run_kind='integration' AND c.catalog_metadata IS NULL AND EXISTS (SELECT 1 FROM reposeer.artifacts a WHERE a.run_id=c.run_id AND a.artifact_kind='bundle' AND a.status='ingested')"
        ).fetchone()["count"]
    require(missing == 0, "legacy_catalog_metadata_missing_run_scan_legacy")
    rows = [row["catalog_metadata"] for row in accepted]
    for row in rows:
        require(set(row) == set(CATALOG_SCHEMA), "catalog_metadata_schema_mismatch")
    # SQL snapshots are immutable; no receipt/checksum download for accepted runs.
    with tempfile.TemporaryDirectory(prefix="reposeer-catalog-") as scratch:
        scratch = Path(scratch)
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


def _run(remote, workspace, *, limit=25, dry_run=False, registry=None, refresh_catalog=False):
    require(1 <= limit <= 1000, "invalid_ingestion_limit")
    registry, drive = registry or Registry(), Drive(remote)
    drive.check()
    results, pending, rejected, retryable = [], [], [], []
    inspected = 0
    for number in range(1, 8):
        worker = f"C{number}"
        for name, size in sorted(drive.listing(f"crawl/inbox/{worker}").items()):
            if inspected >= limit:
                break
            inspected += 1
            path = f"crawl/inbox/{worker}/{name}"
            archive = path.replace("crawl/inbox/", "crawl/inbox-processed/", 1)
            run_id = None
            accepted = False
            try:
                notice = validate_notification(drive.get_json(path, byte_size=size), path)
                run_id, job = notice["integration_run_id"], notice["job_id"]
                prefix = notice["submission_prefix"]
                marker = validate_ready(drive.get_json(prefix + "/READY.json"))
                require(
                    marker["worker_id"] == worker
                    and marker["job_id"] == job
                    and marker["run_id"] == run_id
                    and marker["payload_sha256"] == notice["ready_payload_sha256"],
                    "notification_ready_mismatch",
                )
                with registry.connect(readonly=True) as db:
                    previous = db.execute(
                        "SELECT a.*,c.worker_id,c.job_id FROM reposeer.artifacts a JOIN reposeer.crawl_runs c USING(run_id) WHERE a.storage_path=%s AND a.status='ingested' AND a.artifact_kind='bundle' AND c.run_kind='integration'",
                        (drive.path(f"canonical/runs/{run_id}/CHECKSUMS.sha256"),),
                    ).fetchone()
                if previous:
                    require(
                        previous["sha256"] == marker["payload_sha256"]
                        and previous["worker_id"] == worker
                        and str(previous["job_id"]) == job,
                        "submitted_path_checksum_conflict",
                    )
                if dry_run:
                    pending.append(
                        dict(
                            worker_id=worker,
                            job_id=job,
                            integration_run_id=run_id,
                            already_accepted=bool(previous),
                        )
                    )
                    continue
                if previous:
                    result = dict(artifact_accepted=True, duplicate=True, job_collected=False)
                else:
                    root = Path(workspace) / "incoming" / run_id
                    drive.download(prefix, root)
                    result = ingest_one(
                        root,
                        drive,
                        registry,
                        expected_worker=worker,
                        expected_job=job,
                        expected_run=run_id,
                    )
                accepted = True
                results.append(result)
                write_json(Path(workspace) / "accepted" / f"{run_id}.json", result, immutable=False)
                drive.archive(path, archive)
            except (CoordinationError, ValueError, KeyError, TypeError, OSError) as error:
                code = (
                    str(error)
                    if isinstance(error, CoordinationError)
                    else "invalid_or_unreadable_bundle"
                )
                rejection = dict(
                    notification_path=path,
                    integration_run_id=run_id,
                    error_code=code,
                    **(error.diagnostics if isinstance(error, CoordinationError) else {}),
                )
                # Transport failures retain pending notifications for exact retry.
                if (
                    accepted
                    or isinstance(error, OSError)
                    or code
                    in {
                        "rclone_operation_failed",
                        "rclone_operation_timeout",
                        "rclone_operation_unavailable",
                        "rclone_not_installed",
                        "upload_checksum_mismatch",
                        "registry_operation_failed",
                    }
                ):
                    retryable.append(rejection)
                    continue
                rejected.append(rejection)
                if not dry_run:
                    from tools.coordination._common import digest

                    write_json(
                        Path(workspace) / "quarantine" / (digest(path.encode()) + ".json"),
                        rejection,
                        immutable=False,
                    )
                    drive.archive(path, path.replace("crawl/inbox/", "crawl/inbox-quarantine/", 1))
    catalog = rebuild_catalog(drive, registry) if refresh_catalog and not dry_run else None
    return dict(
        dry_run=dry_run,
        pending=pending,
        accepted=results,
        rejected=rejected,
        retryable=retryable,
        artifacts_ingested=sum(r.get("artifacts_ingested", 0) for r in results),
        jobs_completed=sum(r.get("job_status") == "completed" for r in results),
        jobs_retry=sum(r.get("job_status") == "retry" for r in results),
        catalog_rows=catalog,
    )


def run(remote, workspace, *, limit=25, dry_run=False, registry=None, refresh_catalog=False):
    registry = registry or Registry()
    if dry_run:
        return _run(remote, workspace, limit=limit, dry_run=True, registry=registry)
    with registry.writer_lock():
        return _run(
            remote, workspace, limit=limit, registry=registry, refresh_catalog=refresh_catalog
        )


def scan_legacy(remote, workspace, *, limit=25, apply=False, registry=None):
    """Explicit bounded migration/backfill; never invoked by normal ingestion."""
    from psycopg.types.json import Jsonb

    drive, registry = Drive(remote), registry or Registry()
    require(1 <= limit <= 1000, "invalid_ingestion_limit")
    drive.check()
    found = []
    for worker in (f"C{n}" for n in range(1, 8)):
        for job in drive.children(f"crawl/submissions/{worker}"):
            uid(job)
            for run_id in drive.children(f"crawl/submissions/{worker}/{job}"):
                uid(run_id)
                with registry.connect(readonly=True) as db:
                    recorded = db.execute(
                        "SELECT * FROM reposeer.crawl_runs WHERE run_id=%s", (run_id,)
                    ).fetchone()
                if recorded and recorded.get("catalog_metadata") is not None:
                    continue
                if recorded is None and run_id + ".json" in drive.listing(
                    f"crawl/inbox/{worker}/{job}"
                ):
                    continue
                prefix = f"crawl/submissions/{worker}/{job}/{run_id}"
                listing = drive.listing(prefix)
                if "READY.json" not in listing:
                    continue
                marker = validate_ready(
                    drive.get_json(prefix + "/READY.json", byte_size=listing["READY.json"])
                )
                require(
                    marker["worker_id"] == worker
                    and marker["job_id"] == job
                    and marker["run_id"] == run_id,
                    "ready_submission_path_mismatch",
                )
                notice = notification(dict(worker_id=worker, job_id=job), marker)
                path = f"crawl/inbox/{worker}/{job}/{run_id}.json"
                validate_notification(notice, path)
                found.append(path)
                if apply:
                    root = Path(workspace) / "legacy" / run_id
                    drive.download(prefix, root)
                    assignment, receipt, inventory = validate_submission(
                        root, worker=worker, job_id=job, run_id=run_id
                    )
                    with registry.connect() as db:
                        previous = previous_run(
                            db,
                            drive.path(f"canonical/runs/{run_id}/CHECKSUMS.sha256"),
                            assignment,
                            receipt,
                            inventory,
                        )
                        if previous:
                            snapshot = catalog_snapshot(
                                assignment,
                                receipt,
                                drive,
                                previous["sha256"],
                                previous["ingested_at"],
                            )
                            db.execute(
                                "UPDATE reposeer.crawl_runs SET catalog_metadata=%s WHERE run_id=%s AND catalog_metadata IS NULL",
                                (Jsonb(snapshot), run_id),
                            )
                    if not previous:
                        local = Path(workspace) / "notifications" / (run_id + ".json")
                        write_json(local, notice)
                        drive.put(local, path)
                if len(found) >= limit:
                    return dict(dry_run=not apply, notifications=found)
    return dict(dry_run=not apply, notifications=found)


def main():
    cli = parser(__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("run", "scan-legacy", "rebuild-catalog"):
        sub = commands.add_parser(name)
        sub.add_argument("--remote", default=argparse.SUPPRESS)
        if name != "rebuild-catalog":
            sub.add_argument("--workspace", type=Path, required=True)
            sub.add_argument("--limit", type=int, default=25)
        sub.add_argument("--dry-run", action="store_true")
        if name == "scan-legacy":
            group = sub.add_mutually_exclusive_group()
            group.add_argument("--apply", action="store_true")
            # --dry-run remains the default unless --apply is explicit.
        if name == "run":
            sub.add_argument("--refresh-catalog", action="store_true")
    args = cli.parse_args()
    registry = Registry()
    if args.command == "rebuild-catalog":
        if args.dry_run:
            with registry.connect(readonly=True) as db:
                return dict(
                    dry_run=True,
                    runs=db.execute(
                        "SELECT count(*) AS count FROM reposeer.crawl_runs WHERE run_kind='integration'"
                    ).fetchone()["count"],
                )
        with registry.writer_lock():
            drive = Drive(args.remote)
            drive.check()
            return dict(catalog_rows=rebuild_catalog(drive, registry))
    if args.command == "scan-legacy":
        require(not (args.apply and args.dry_run), "conflicting_apply_dry_run")
        if not args.apply:
            return scan_legacy(args.remote, args.workspace, limit=args.limit, registry=registry)
        with registry.writer_lock():
            return scan_legacy(
                args.remote, args.workspace, limit=args.limit, apply=True, registry=registry
            )
    result = run(
        args.remote,
        args.workspace,
        limit=args.limit,
        dry_run=args.dry_run,
        registry=registry,
        refresh_catalog=args.refresh_catalog,
    )
    if result["rejected"] or result["retryable"]:
        print(json.dumps(result, default=str))
        raise SystemExit(1)
    return result


if __name__ == "__main__":
    cli_guard(main)
