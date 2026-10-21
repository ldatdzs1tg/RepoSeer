"""Coordinator queues plus DB-free D2 screening. Run from the repository root."""

import argparse
import csv
import json
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.coordination._common import (
    COLLECTION,
    MAX_METADATA,
    Drive,
    Registry,
    artifact_row,
    atomic_bytes,
    canonical,
    check_claim,
    cli_guard,
    enabled_worker,
    fingerprint,
    finish_job,
    format_utc_timestamp,
    no_worker_dsn,
    now,
    parser,
    read_json,
    relative,
    require,
    safe_path,
    seal_bundle,
    sign_assignment,
    slug,
    subject,
    timestamp,
    uid,
    validate_assignment,
    verify_bundle,
    worker_id,
    write_json,
)

from reposeer.clients.pypi import PyPIClient
from reposeer.collection.http.client import HTTPClient
from reposeer.contracts.models import RawRecord
from reposeer.exceptions import HTTPClientError
from reposeer.pipelines.collection import _repository_candidate
from reposeer.pipelines.package_discovery import CSV_FIELDS

CLAIM_SQL = """SELECT j.*,r.repo_full_name FROM reposeer.crawl_jobs j
 LEFT JOIN reposeer.repositories r USING(repository_id)
 WHERE j.job_kind=%s AND j.scope_key='initial' AND j.status IN ('queued','retry')
 AND (j.retry_after IS NULL OR j.retry_after<=now()) AND j.attempt_count<j.max_attempts
 ORDER BY j.priority DESC,j.created_at,j.job_id FOR UPDATE OF j SKIP LOCKED LIMIT %s"""


def discovery_bundle(root):
    manifest = read_json(safe_path(root, "manifest.json"))
    run = uid(manifest.get("sampling_run_id"))
    require(
        manifest.get("manifest_version") == "1" and manifest.get("status") == "complete",
        "invalid_sampling_manifest",
    )
    timestamp(manifest["created_at"])
    timestamp(manifest["finished_at"])
    csv_path = safe_path(root, "package_candidates.csv")
    artifact = manifest["artifacts"]["candidates_csv"]
    require(
        relative(artifact["relative_path"]).endswith(f"/runs/{run}/package_candidates.csv"),
        "sampling_artifact_path_mismatch",
    )
    require(csv_path.stat().st_size <= MAX_METADATA, "sampling_csv_size_limit")
    require(fingerprint(csv_path)[0] == artifact["sha256"], "sampling_csv_checksum_mismatch")
    with csv_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        require(tuple(reader.fieldnames or ()) == CSV_FIELDS, "sampling_csv_schema_mismatch")
        rows = list(reader)
    require(
        1 <= len(rows) <= 10000
        and len(rows) == manifest["sampling"]["selected_count"] == artifact["row_count"],
        "sampling_count_mismatch",
    )
    seen = set()
    for rank, row in enumerate(rows, 1):
        key = subject(row["package_id"])
        require(
            row["sampling_run_id"] == run and int(row["sample_rank"]) == rank and key not in seen,
            "sampling_rank_or_identity_mismatch",
        )
        require(
            row["package_name"] == key[5:] and row["source_kind"] == manifest["source"]["kind"],
            "sampling_name_or_source_mismatch",
        )
        require(
            row["batch_id"] == f"batch_{(rank - 1) // manifest['sampling']['batch_size'] + 1:03d}",
            "sampling_batch_mismatch",
        )
        require(
            (row["eligibility_status"], row["mapping_status"], row["collection_status"])
            == ("name_valid_only", "not_checked", "not_started"),
            "sampling_initial_states_mismatch",
        )
        seen.add(key)
    # D1 need only submit these two files. Verify other artifacts too when provided.
    for entry in [manifest["artifacts"]["targets"], *manifest["artifacts"]["batches"]]:
        name = relative(entry["relative_path"]).split(f"/runs/{run}/")
        require(len(name) == 2, "sampling_artifact_path_mismatch")
        local = safe_path(root, name[1])
        if local.exists():
            require(fingerprint(local)[0] == entry["sha256"], "sampling_artifact_checksum_mismatch")
    return manifest, rows


def import_discovery(root, *, dry_run=False, registry=None, remote="reposeer:"):
    root = Path(root)
    manifest, rows = discovery_bundle(root)
    run = manifest["sampling_run_id"]
    counters = dict(
        names_seen=len(rows), packages_inserted=0, duplicates_skipped=0, screen_jobs_created=0
    )
    drive = Drive(remote)
    with (registry or Registry()).connect(readonly=dry_run) as db:
        if not dry_run:
            enabled_worker(db, "D1", "discovery")
            db.execute(
                "INSERT INTO reposeer.crawl_runs(run_id,worker_id,run_kind,status,started_at,finished_at) VALUES (%s,'D1','sampling','complete',%s,%s) ON CONFLICT DO NOTHING",
                (run, timestamp(manifest["created_at"]), timestamp(manifest["finished_at"])),
            )
            existing = db.execute(
                "SELECT * FROM reposeer.crawl_runs WHERE run_id=%s", (run,)
            ).fetchone()
            require(
                existing["run_kind"] == "sampling" and existing["worker_id"] == "D1",
                "sampling_run_conflict",
            )
            for filename, kind in [
                ("manifest.json", "manifest"),
                ("package_candidates.csv", "raw"),
            ]:
                sha, size = fingerprint(root / filename)
                artifact_row(
                    db, run, kind, drive.path(f"discovery/submissions/{run}/{filename}"), sha, size
                )
        for row in rows:
            key = row["package_id"]
            if dry_run:
                inserted = not db.execute(
                    "SELECT 1 FROM reposeer.packages WHERE package_id=%s", (key,)
                ).fetchone()
            else:
                inserted = db.execute(
                    "INSERT INTO reposeer.packages(package_id,first_sampling_run_id,source_snapshot_sha256) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING package_id",
                    (key, run, manifest["source"]["snapshot_sha256"]),
                ).fetchone()
            if inserted:
                counters["packages_inserted"] += 1
                if not dry_run:
                    created = db.execute(
                        "INSERT INTO reposeer.crawl_jobs(job_kind,package_id) VALUES ('screen_package',%s) ON CONFLICT DO NOTHING RETURNING job_id",
                        (key,),
                    ).fetchone()
                    counters["screen_jobs_created"] += int(bool(created))
                else:
                    counters["screen_jobs_created"] += 1
            else:
                counters["duplicates_skipped"] += 1
    return dict(counters, dry_run=dry_run)


def publish_assignment(path, out_dir, drive, registry, *, expected_worker):
    assignment = validate_assignment(read_json(path), expected_worker=expected_worker)
    screening = assignment["kind"] == "screen_package"
    identity = assignment["assignment_id" if screening else "job_id"]
    final = safe_path(out_dir, f"{identity}.json")
    items = assignment["jobs"] if screening else [assignment]
    # Hold claims while updating their assignment pointers. Reclaim cannot publish
    # a newer token and then be overwritten by this older publisher.
    with registry.connect() as db:
        enabled_worker(db, assignment["worker_id"], "screening" if screening else "crawler")
        for item in items:
            row = db.execute(
                "SELECT j.*,r.repo_full_name FROM reposeer.crawl_jobs j LEFT JOIN reposeer.repositories r USING(repository_id) WHERE job_id=%s FOR UPDATE OF j",
                (item["job_id"],),
            ).fetchone()
            check_claim(row, assignment, item)
        if final.exists() and not screening:
            previous = validate_assignment(
                read_json(final), expected_worker=expected_worker, allow_expired=True
            )
            require(previous.get("job_id") == identity, "assignment_local_conflict")
            if previous["assignment_sha256"] != assignment["assignment_sha256"]:
                write_json(
                    safe_path(out_dir, f"history/{previous['assignment_sha256']}.json"), previous
                )
        write_json(final, assignment, immutable=screening)
        prefix = "screening" if screening else "crawl"
        # Crawl assignment files are current-claim pointers. Run bundles stay immutable.
        drive.put(
            final,
            f"{prefix}/assignments/{assignment['worker_id']}/{identity}.json",
            replace=not screening,
        )
    if Path(path) != final:
        Path(path).unlink()
    return str(final)


def dispatch(kind, worker, limit, out_dir, *, lease_hours=48, remote="reposeer:", registry=None):
    require(kind in {"screen_package", "collect_repository"}, "unknown_assignment_kind")
    role = "screening" if kind == "screen_package" else "crawler"
    worker_id(worker, role)
    require(1 <= limit <= (100 if role == "screening" else 10), "invalid_dispatch_limit")
    require(1 <= lease_hours <= 168, "invalid_lease_hours")
    out_dir = Path(out_dir)
    drive, registry = Drive(remote), registry or Registry()
    drive.check()
    pending = sorted((out_dir / ".pending").glob("*.json"))
    if pending:
        # A failed upload must be retried with its original token, never replaced.
        return {
            "recovered_assignments": [
                publish_assignment(p, out_dir, drive, registry, expected_worker=worker)
                for p in pending
            ]
        }
    generated = []
    with registry.connect() as db:
        enabled_worker(db, worker, role)
        jobs = db.execute(CLAIM_SQL, (kind, limit)).fetchall()
        expiry = timestamp(format_utc_timestamp(now() + timedelta(hours=lease_hours)))
        if kind == "screen_package" and jobs:
            assignment = sign_assignment(
                dict(
                    schema_version=1,
                    kind=kind,
                    assignment_id=str(uuid4()),
                    worker_id=worker,
                    lease_expires_at=format_utc_timestamp(expiry),
                    jobs=[
                        dict(
                            job_id=str(j["job_id"]),
                            package_id=j["package_id"],
                            claim_token=str(uuid4()),
                        )
                        for j in jobs
                    ],
                )
            )
            generated.append(assignment)
        elif kind == "collect_repository":
            for job in jobs:
                rows = db.execute(
                    "SELECT package_id FROM reposeer.packages WHERE repository_id=%s ORDER BY (mapping_status='candidate') DESC,package_id LIMIT 101",
                    (job["repository_id"],),
                ).fetchall()
                require(rows, "repository_has_no_package_evidence")
                targets = [subject(row["package_id"]) for row in rows[:100]]
                generated.append(
                    sign_assignment(
                        dict(
                            schema_version=1,
                            kind=kind,
                            job_id=str(job["job_id"]),
                            worker_id=worker,
                            claim_token=str(uuid4()),
                            scope_key=job["scope_key"],
                            expected_repo_full_name=slug(job["repo_full_name"]),
                            targets=targets,
                            lease_expires_at=format_utc_timestamp(expiry),
                            collection=COLLECTION,
                            package_scope_truncated=len(rows) > 100,
                        )
                    )
                )
        for assignment in generated:
            items = assignment["jobs"] if kind == "screen_package" else [assignment]
            for item in items:
                from psycopg.types.json import Jsonb

                targets = (
                    [item["package_id"]] if kind == "screen_package" else assignment["targets"]
                )
                db.execute(
                    "UPDATE reposeer.crawl_jobs SET status='claimed',claimed_by=%s,claim_token=%s,claimed_at=now(),lease_expires_at=%s,attempt_count=attempt_count+1,assignment_targets=%s,assignment_sha256=%s,updated_at=now() WHERE job_id=%s",
                    (
                        worker,
                        item["claim_token"],
                        expiry,
                        Jsonb(targets),
                        assignment["assignment_sha256"],
                        item["job_id"],
                    ),
                )
            identity = assignment["assignment_id" if kind == "screen_package" else "job_id"]
            # Durable recovery journal precedes commit; visible assignments follow commit.
            write_json(out_dir / ".pending" / f"{identity}.json", assignment)
    output = []
    for assignment in generated:
        identity = assignment["assignment_id" if kind == "screen_package" else "job_id"]
        output.append(
            publish_assignment(
                out_dir / ".pending" / f"{identity}.json",
                out_dir,
                drive,
                registry,
                expected_worker=worker,
            )
        )
    return {"assignments": output, "jobs_claimed": sum(len(a.get("jobs", [a])) for a in generated)}


def screen_local(assignment_path, out_dir, *, remote="reposeer:", client=None):
    no_worker_dsn()
    assignment = validate_assignment(read_json(assignment_path), expected_worker="D2")
    require(assignment["kind"] == "screen_package", "screening_assignment_required")
    out_dir = Path(out_dir) / uid(assignment["assignment_id"])
    if (out_dir / "READY.json").exists():
        _, saved, _ = verify_bundle(out_dir)
        require(saved == assignment, "assignment_local_conflict")
    else:
        write_json(out_dir / "ASSIGNMENT.json", assignment)
        with HTTPClient(timeout=30, max_retries=3, max_backoff_seconds=30) as http:
            pypi = client or PyPIClient(http_client=http)
            results = []
            for job in assignment["jobs"]:
                result_path = out_dir / "results" / f"{job['job_id']}.json"
                if result_path.exists():
                    results.append(read_json(result_path))
                    continue
                result = dict(
                    assignment_id=assignment["assignment_id"],
                    **job,
                    candidate_repo_full_name=None,
                    screening_status="failed",
                    reason_code=None,
                    retrieved_at_utc=format_utc_timestamp(now()),
                    pypi_response_sha256=None,
                )
                try:
                    payload = pypi.get_package_info(job["package_id"][5:])
                    result["retrieved_at_utc"] = format_utc_timestamp(now())
                    require(isinstance(payload, dict), "invalid_pypi_response")
                    raw_path = out_dir / "pypi_raw" / (job["package_id"][5:] + ".json")
                    write_json(raw_path, payload)
                    result["pypi_response_sha256"] = fingerprint(raw_path)[0]
                    raw = RawRecord(
                        schema_version="0.1.0",
                        record_type="raw_record",
                        raw_record_id=f"raw:{uuid4()}",
                        run_id=assignment["assignment_id"],
                        source="pypi_json",
                        source_record_type="dependency_metadata",
                        source_record_id=job["package_id"][5:],
                        package_id=job["package_id"],
                        repo_id=None,
                        source_url=f"https://pypi.org/pypi/{job['package_id'][5:]}/json",
                        retrieved_at=result["retrieved_at_utc"],
                        available_at=None,
                        event_at=None,
                        http_status=None,
                        raw_body_sha256=None,
                        payload=payload,
                    )
                    candidate, _, reason = _repository_candidate(raw)
                    result.update(
                        candidate_repo_full_name=slug(candidate) if candidate else None,
                        screening_status="candidate" if candidate else "unresolved",
                        reason_code=reason,
                    )
                except HTTPClientError as error:
                    result["reason_code"] = (
                        "rate_limited" if error.status_code in {403, 429} else "pypi_request_failed"
                    )
                write_json(result_path, result)
                results.append(result)
        atomic_bytes(out_dir / "results.jsonl", b"".join(canonical(row) + b"\n" for row in results))
        seal_bundle(out_dir, assignment)
    drive = Drive(remote)
    drive.check()
    drive.upload(out_dir, f"screening/submissions/D2/{assignment['assignment_id']}")
    return {
        "assignment_id": assignment["assignment_id"],
        "screened": len(assignment["jobs"]),
        "output": str(out_dir),
    }


def screening_bundle(root):
    marker, assignment, inventory = verify_bundle(root)
    require(assignment["kind"] == "screen_package", "screening_assignment_required")
    result_path = safe_path(root, "results.jsonl")
    require(result_path.stat().st_size <= MAX_METADATA, "screening_results_size_limit")
    rows = [json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()]
    expected = {j["job_id"]: j for j in assignment["jobs"]}
    require(
        len(rows) == len(expected) and len({r["job_id"] for r in rows}) == len(rows),
        "screening_results_count",
    )
    for result in rows:
        job = expected.get(result["job_id"])
        require(
            job
            and all(result.get(k) == v for k, v in job.items())
            and result["assignment_id"] == assignment["assignment_id"],
            "screening_result_identity",
        )
        timestamp(result["retrieved_at_utc"])
        require(
            result["screening_status"] in {"candidate", "unresolved", "failed"},
            "invalid_screening_status",
        )
        if result["screening_status"] != "failed":
            raw_path = safe_path(root, f"pypi_raw/{job['package_id'][5:]}.json")
            require(
                fingerprint(raw_path)[0] == result["pypi_response_sha256"],
                "screening_source_checksum",
            )
            payload = read_json(raw_path)
            # Re-run the same T-020 rule; a D2 claim alone cannot invent a candidate.
            from types import SimpleNamespace

            candidate, _, reason = _repository_candidate(
                SimpleNamespace(payload=payload, package_id=job["package_id"])
            )
            require(
                result["candidate_repo_full_name"] == (slug(candidate) if candidate else None),
                "screening_candidate_mismatch",
            )
            require(
                result["screening_status"] == ("candidate" if candidate else "unresolved")
                and result["reason_code"] == reason,
                "screening_evidence_mismatch",
            )
        else:
            require(
                result["candidate_repo_full_name"] is None
                and result["reason_code"] in {"rate_limited", "pypi_request_failed"},
                "invalid_screening_failure",
            )
    return marker, assignment, rows, inventory


def import_screening(root, *, dry_run=False, registry=None, remote="reposeer:"):
    root = Path(root)
    marker, assignment, results, inventory = screening_bundle(root)
    identity = assignment["assignment_id"]
    prefix = Drive(remote).path(f"screening/submissions/D2/{identity}")
    counters = dict(
        screened=len(results),
        candidate_repositories=0,
        jobs_queued=0,
        jobs_retry=0,
        duplicates_skipped=0,
    )
    with (registry or Registry()).connect(readonly=dry_run) as db:
        existing = db.execute(
            "SELECT sha256 FROM reposeer.artifacts WHERE storage_path=%s",
            (prefix + "/CHECKSUMS.sha256",),
        ).fetchone()
        if existing:
            require(
                existing["sha256"] == marker["payload_sha256"], "screening_replay_checksum_conflict"
            )
        jobs = {}
        for item in assignment["jobs"]:
            job = (
                db.execute(
                    "SELECT * FROM reposeer.crawl_jobs WHERE job_id=%s FOR UPDATE",
                    (item["job_id"],),
                ).fetchone()
                if not dry_run
                else db.execute(
                    "SELECT * FROM reposeer.crawl_jobs WHERE job_id=%s", (item["job_id"],)
                ).fetchone()
            )
            check_claim(job, assignment, item, replay=bool(existing))
            jobs[item["job_id"]] = job
        if existing:
            return dict(counters, duplicates_skipped=len(results))
        if dry_run:
            return dict(counters, dry_run=True)
        db.execute(
            "INSERT INTO reposeer.crawl_runs(run_id,worker_id,run_kind,status,finished_at) VALUES (%s,'D2','screening',%s,now())",
            (
                identity,
                "partial"
                if any(r["screening_status"] == "failed" for r in results)
                else "complete",
            ),
        )
        for result in results:
            job = jobs[result["job_id"]]
            package = db.execute(
                "SELECT * FROM reposeer.packages WHERE package_id=%s FOR UPDATE",
                (result["package_id"],),
            ).fetchone()
            require(package is not None, "unknown_screening_package")
            if package["mapping_status"] == "verified_current":
                # Candidate-only screening cannot relink or downgrade verified evidence.
                finish_job(db, job)
                counters["duplicates_skipped"] += 1
                continue
            if result["screening_status"] == "candidate":
                candidate = slug(result["candidate_repo_full_name"])
                row = db.execute(
                    "INSERT INTO reposeer.repositories(repo_full_name) VALUES (%s) ON CONFLICT (lower(repo_full_name)) DO UPDATE SET repo_full_name=reposeer.repositories.repo_full_name RETURNING repository_id",
                    (candidate,),
                ).fetchone()
                db.execute(
                    "UPDATE reposeer.packages SET repository_id=%s,candidate_repo_full_name=%s,mapping_status='candidate',last_screened_at=%s WHERE package_id=%s",
                    (
                        row["repository_id"],
                        candidate,
                        timestamp(result["retrieved_at_utc"]),
                        result["package_id"],
                    ),
                )
                inserted = db.execute(
                    "INSERT INTO reposeer.crawl_jobs(job_kind,repository_id) VALUES ('collect_repository',%s) ON CONFLICT DO NOTHING RETURNING job_id",
                    (row["repository_id"],),
                ).fetchone()
                counters["candidate_repositories"] += 1
                counters["jobs_queued"] += int(bool(inserted))
                finish_job(db, job)
            elif result["screening_status"] == "unresolved":
                db.execute(
                    "UPDATE reposeer.packages SET mapping_status='unresolved',last_screened_at=%s WHERE package_id=%s",
                    (timestamp(result["retrieved_at_utc"]), result["package_id"]),
                )
                finish_job(db, job)
                db.execute(
                    "UPDATE reposeer.crawl_jobs SET last_error_code=%s WHERE job_id=%s",
                    (result["reason_code"], job["job_id"]),
                )
            else:
                counters["jobs_retry"] += int(finish_job(db, job, result["reason_code"]) == "retry")
        artifact_row(
            db,
            identity,
            "bundle",
            prefix + "/CHECKSUMS.sha256",
            marker["payload_sha256"],
            sum(size for _, size in inventory.values()),
        )
    return counters


def requeue_expired(
    *, apply=False, pending_dir=Path("data/coordination"), remote="reposeer:", registry=None
):
    drive, registry = Drive(remote), registry or Registry()
    drive.check()
    # Even an incomplete remote upload protects a job until manually inspected.
    crawl, screening = drive.listing("crawl/submissions"), drive.listing("screening/submissions")
    remote_claims, unknown_jobs = [], set()
    prefixes = {}
    for name in crawl:
        parts = name.split("/")
        require(len(parts) >= 4, "invalid_submission_path")
        worker_id(parts[0], "crawler")
        uid(parts[1])
        uid(parts[2])
        prefixes["crawl/submissions/" + "/".join(parts[:3])] = (
            parts[1],
            crawl,
            "/".join(parts[:3]),
        )
    for name in screening:
        parts = name.split("/")
        require(len(parts) >= 3 and parts[0] == "D2", "invalid_submission_path")
        uid(parts[1])
        prefixes["screening/submissions/" + "/".join(parts[:2])] = (
            None,
            screening,
            "/".join(parts[:2]),
        )
    for prefix, (job_id, listing, subpath) in prefixes.items():
        if subpath + "/ASSIGNMENT.json" not in listing:
            # An incomplete upload of unknown ownership cannot justify reclamation.
            unknown_jobs.add(job_id)
            continue
        value = validate_assignment(drive.get_json(prefix + "/ASSIGNMENT.json"), allow_expired=True)
        remote_claims.append(value)
    pending = []
    for path in Path(pending_dir).rglob("ASSIGNMENT.json"):
        pending.append(validate_assignment(read_json(path), allow_expired=True))
    for path in Path(pending_dir).rglob(".pending/*.json"):
        pending.append(validate_assignment(read_json(path), allow_expired=True))
    changed, protected = [], []
    with registry.connect(readonly=not apply) as db:
        sql = "SELECT * FROM reposeer.crawl_jobs WHERE status='claimed' AND lease_expires_at<now() ORDER BY job_id"
        jobs = db.execute(sql + (" FOR UPDATE SKIP LOCKED" if apply else "")).fetchall()
        for job in jobs:
            identity = str(job["job_id"])

            def matches(value, identity=identity, token=str(job["claim_token"])):
                return any(
                    item["job_id"] == identity and item["claim_token"] == token
                    for item in value.get("jobs", [value])
                )

            has_remote = (
                identity in unknown_jobs
                or None in unknown_jobs
                and job["job_kind"] == "screen_package"
                or any(matches(a) for a in remote_claims)
            )
            has_local = any(matches(a) for a in pending)
            if has_remote or has_local:
                protected.append(identity)
                continue
            changed.append(identity)
            if apply:
                finish_job(db, job, "lease_expired_no_submission")
                db.execute(
                    "UPDATE reposeer.crawl_jobs SET claimed_by=NULL,claim_token=NULL,claimed_at=NULL,lease_expires_at=NULL,updated_at=now() WHERE job_id=%s",
                    (identity,),
                )
    return dict(dry_run=not apply, jobs_requeued=changed, protected_jobs=protected)


def status(registry):
    with registry.connect(readonly=True) as db:
        packages = db.execute(
            "SELECT mapping_status,count(*) AS count FROM reposeer.packages GROUP BY mapping_status"
        ).fetchall()
        repos = db.execute(
            "SELECT verification_status,count(*) AS count FROM reposeer.repositories GROUP BY verification_status"
        ).fetchall()
        jobs = db.execute(
            "SELECT job_kind,status,count(*) AS count FROM reposeer.crawl_jobs GROUP BY job_kind,status"
        ).fetchall()
        workers = db.execute(
            "SELECT w.worker_id,w.worker_role,w.display_name,w.is_enabled,w.last_seen_at,count(j.job_id) AS assigned FROM reposeer.workers w LEFT JOIN reposeer.crawl_jobs j ON j.claimed_by=w.worker_id AND j.status='claimed' GROUP BY w.worker_id ORDER BY w.worker_id"
        ).fetchall()
        artifacts = db.execute(
            "SELECT status,count(*) AS count FROM reposeer.artifacts GROUP BY status"
        ).fetchall()
    return dict(
        packages=packages,
        repositories=repos,
        jobs=jobs,
        workers=workers,
        artifacts=artifacts,
        source_coverage="not_verified",
        historical_validity="unverified",
        dashboard_sync="manual_status_json",
    )


def main():
    cli = parser(__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ["import-discovery", "import-screening"]:
        sub = commands.add_parser(name)
        sub.add_argument("--input-dir", type=Path, required=True)
        sub.add_argument("--dry-run", action="store_true")
    for name, default, worker in [("dispatch-screen", 100, "D2"), ("dispatch-crawl", 10, None)]:
        sub = commands.add_parser(name)
        sub.add_argument("--worker", default=worker, required=worker is None)
        sub.add_argument("--limit", type=int, default=default)
        sub.add_argument("--out-dir", type=Path, required=True)
        sub.add_argument("--lease-hours", type=int, default=48)
    sub = commands.add_parser("screen-local")
    sub.add_argument("--assignment", type=Path, required=True)
    sub.add_argument("--out-dir", type=Path, required=True)
    sub = commands.add_parser("export-exclusions")
    sub.add_argument("--output", type=Path, required=True)
    sub = commands.add_parser("status")
    sub.add_argument("--json", action="store_true")
    sub = commands.add_parser("activate")
    sub.add_argument("--worker", required=True)
    sub.add_argument("--display-name", required=True)
    sub = commands.add_parser("requeue-expired")
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    sub.add_argument("--pending-dir", type=Path, default=Path("data/coordination"))
    for sub in commands.choices.values():
        sub.add_argument("--remote", default=argparse.SUPPRESS)
    args = cli.parse_args()
    registry = Registry()
    if args.command in {"import-discovery", "import-screening"}:
        function = import_discovery if args.command == "import-discovery" else import_screening
        return function(args.input_dir, dry_run=args.dry_run, remote=args.remote, registry=registry)
    if args.command.startswith("dispatch-"):
        return dispatch(
            "screen_package" if args.command == "dispatch-screen" else "collect_repository",
            args.worker,
            args.limit,
            args.out_dir,
            lease_hours=args.lease_hours,
            remote=args.remote,
            registry=registry,
        )
    if args.command == "screen-local":
        return screen_local(args.assignment, args.out_dir, remote=args.remote)
    if args.command == "status":
        return status(registry)
    if args.command == "requeue-expired":
        return requeue_expired(
            apply=args.apply, pending_dir=args.pending_dir, remote=args.remote, registry=registry
        )
    if args.command == "activate":
        worker_id(args.worker)
        require(args.display_name.strip() and len(args.display_name) <= 120, "invalid_display_name")
        with registry.connect() as db:
            result = db.execute(
                "UPDATE reposeer.workers SET display_name=%s,is_enabled=true WHERE worker_id=%s RETURNING worker_id",
                (args.display_name, args.worker),
            ).fetchone()
            require(result, "unknown_worker")
        return {"worker_activated": args.worker}
    if args.command == "export-exclusions":
        with registry.connect(readonly=True) as db:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # Stream sorted IDs, including frames larger than the T-020A sample cap.
            import os
            import tempfile

            with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    with db.cursor(name="exclusions") as cursor:
                        cursor.execute(
                            "SELECT package_id FROM reposeer.packages ORDER BY package_id"
                        )
                        for row in cursor:
                            stream.write((subject(row["package_id"]) + "\n").encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    stream.close()
                    temporary.unlink(missing_ok=True)
                    raise
            os.replace(temporary, args.output)
        return {"exclusions": str(args.output)}


if __name__ == "__main__":
    cli_guard(main)
