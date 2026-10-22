"""Sequential C1-C7 runner. No registry credential or database connection."""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.coordination._common import (
    CoordinationError,
    Drive,
    atomic_bytes,
    cli_guard,
    fingerprint,
    format_utc_timestamp,
    integration_evidence,
    no_worker_dsn,
    notification,
    now,
    parser,
    read_checksums,
    read_json,
    redact,
    require,
    safe_path,
    seal_bundle,
    timestamp,
    uid,
    validate_assignment,
    validate_notification,
    validate_ready,
    verify_bundle,
    worker_id,
    write_json,
)


def run(worker, assignment_path, data_root, *, remote="reposeer:", dry_run=False, runner=None):
    no_worker_dsn()
    worker_id(worker, "crawler")
    assignment = validate_assignment(read_json(assignment_path), expected_worker=worker)
    require(assignment["kind"] == "collect_repository", "crawl_assignment_required")
    root = safe_path(data_root, uid(assignment["job_id"]) + "/" + uid(assignment["claim_token"]))
    require(not root.is_symlink() and not root.is_junction(), "unsafe_work_directory")
    if dry_run:
        return dict(
            dry_run=True,
            worker_id=worker,
            job_id=assignment["job_id"],
            targets=len(assignment["targets"]),
            data_dir=str(root),
        )
    write_json(root / "ASSIGNMENT.json", assignment)
    if (root / "READY.json").exists():
        _, existing, _ = verify_bundle(root)
        require(existing == assignment, "assignment_local_conflict")
        receipt = read_json(root / "COORDINATION_RECEIPT.json")
    else:
        manifests = sorted((root / "metadata/collection_runs").glob("integration_*.json"))
        require(len(manifests) <= 1, "multiple_local_integration_runs")
        if not manifests:
            targets_path = root / "targets.txt"
            atomic_bytes(targets_path, ("\n".join(assignment["targets"]) + "\n").encode("utf-8"))
            command = [
                "uv",
                "run",
                "reposeer",
                "collect-dataset",
                "--targets-file",
                str(targets_path),
                "--data-dir",
                str(root),
                "--mode",
                "live",
                "--max-pages",
                str(assignment["collection"]["max_pages"]),
                "--max-records",
                str(assignment["collection"]["max_records"]),
                "--discovery-sources",
                ",".join(assignment["collection"]["discovery_sources"]),
                "--discovery-max-pages",
                str(assignment["collection"]["discovery_max_pages"]),
                "--discovery-max-records",
                str(assignment["collection"]["discovery_max_records"]),
                "--max-candidates",
                str(assignment["collection"]["max_candidates"]),
            ]
            # Stream subprocess output to a local temporary file, then redact into a
            # sibling log outside the submitted data directory. No shell evaluation.
            interrupted = False
            with tempfile.TemporaryFile(mode="w+b") as output:
                try:
                    result = (runner or subprocess.run)(
                        command, stdout=output, stderr=subprocess.STDOUT, timeout=6 * 3600
                    )
                except (OSError, subprocess.SubprocessError):
                    interrupted = True
                output.seek(0)
                text = redact(output.read(16 * 1024**2).decode("utf-8", errors="replace"))
            if interrupted:
                write_json(
                    root / "failure_receipt.json",
                    dict(
                        job_id=assignment["job_id"],
                        worker_id=worker,
                        claim_token=assignment["claim_token"],
                        assignment_sha256=assignment["assignment_sha256"],
                        reason_code="collector_process_interrupted",
                        finished_at=format_utc_timestamp(now()),
                    ),
                    immutable=False,
                )
                raise CoordinationError("collector_process_interrupted")
            atomic_bytes(
                Path(data_root)
                / "logs"
                / assignment["job_id"]
                / f"{assignment['claim_token']}.log",
                text.encode("utf-8"),
                immutable=False,
            )
            manifests = sorted((root / "metadata/collection_runs").glob("integration_*.json"))
            if not manifests:
                write_json(
                    root / "failure_receipt.json",
                    dict(
                        job_id=assignment["job_id"],
                        worker_id=worker,
                        claim_token=assignment["claim_token"],
                        assignment_sha256=assignment["assignment_sha256"],
                        reason_code="integration_manifest_missing",
                        process_exit_code=result.returncode,
                        finished_at=format_utc_timestamp(now()),
                    ),
                    immutable=False,
                )
                raise CoordinationError("integration_manifest_missing")
            reported = re.findall(r"Integration run ID:\s*([a-f0-9-]{36})", text)
            if reported:
                require(
                    len(set(reported)) == 1
                    and manifests[0].stem == "integration_" + uid(reported[-1]),
                    "cli_manifest_run_mismatch",
                )
        require(len(manifests) == 1, "multiple_local_integration_runs")
        run_id = uid(manifests[0].stem.removeprefix("integration_"))
        manifest = read_json(manifests[0])
        require(
            manifest.get("status") in {"complete", "partial", "failed"},
            "unfinished_integration_run_requires_coordinator",
        )
        facts = integration_evidence(root, assignment, run_id)
        receipt = dict(
            job_id=assignment["job_id"],
            worker_id=worker,
            claim_token=assignment["claim_token"],
            assignment_sha256=assignment["assignment_sha256"],
            **facts,
        )
        write_json(root / "COORDINATION_RECEIPT.json", receipt)
        seal_bundle(root, assignment, run_id)
    drive = Drive(remote)
    drive.check()
    drive.upload(
        root, f"crawl/submissions/{worker}/{assignment['job_id']}/{receipt['integration_run_id']}"
    )
    # Timestamp is from immutable READY, so notification retries produce identical bytes.
    with tempfile.TemporaryDirectory(prefix="reposeer-inbox-") as scratch:
        notice = Path(scratch) / "notification.json"
        write_json(notice, notification(assignment, read_json(root / "READY.json")))
        drive.put(
            notice,
            f"crawl/inbox/{worker}/{assignment['job_id']}/{receipt['integration_run_id']}.json",
        )
    return dict(
        job_id=assignment["job_id"],
        integration_run_id=receipt["integration_run_id"],
        overall_status=receipt["overall_status"],
        artifact_submitted=True,
        collected_repository=receipt["collected_repository"],
        local_retained=True,
        scope_key=assignment["scope_key"],
        activity_coverage=receipt.get("activity_coverage"),
        collection_policy="bounded_from_first_page_not_cursor_resume",
    )


def default_data_root(worker):
    base = (
        Path("C:/RepoSeerData/coordination/work")
        if sys.platform == "win32"
        else Path.home() / ".local/share/reposeer/work"
    )
    return base / worker


def previous_submission(drive, assignment):
    """Find this claim's READY bundle and matching submission evidence, without a DB."""
    worker, job = assignment["worker_id"], assignment["job_id"]
    base = f"crawl/submissions/{worker}/{job}"
    entries = drive.listing(base)
    matches = []
    for name, size in sorted(entries.items()):
        parts = name.split("/")
        if len(parts) != 2 or parts[1] != "READY.json":
            continue
        run_id = uid(parts[0])
        prefix = f"{base}/{run_id}"
        marker = validate_ready(drive.get_json(prefix + "/READY.json", byte_size=size))
        saved = validate_assignment(
            drive.get_json(prefix + "/ASSIGNMENT.json"),
            expected_worker=worker,
            allow_expired=True,
        )
        require(
            marker["worker_id"] == worker
            and marker["job_id"] == job
            and marker["run_id"] == run_id
            and saved["job_id"] == job,
            "submission_assignment_mismatch",
        )
        if saved["claim_token"] != assignment["claim_token"]:
            continue
        require(saved == assignment, "submission_assignment_conflict")
        with tempfile.TemporaryDirectory(prefix="reposeer-submission-") as scratch:
            checksums = Path(scratch) / "CHECKSUMS.sha256"
            drive._run(["copyto", drive.path(prefix + "/CHECKSUMS.sha256"), str(checksums)])
            require(fingerprint(checksums)[0] == marker["payload_sha256"], "checksum_list_mismatch")
            declared = read_checksums(checksums)
        require(
            declared.get("ASSIGNMENT.json")
            == drive.remote_fingerprint(prefix + "/ASSIGNMENT.json"),
            "submission_assignment_checksum_mismatch",
        )
        submitted = False
        for inbox in ("inbox", "inbox-processed"):
            parent = f"crawl/{inbox}/{worker}/{job}"
            notice_size = drive.listing(parent).get(run_id + ".json")
            if notice_size is None:
                continue
            notice = validate_notification(
                drive.get_json(f"{parent}/{run_id}.json", byte_size=notice_size),
                f"crawl/inbox/{worker}/{job}/{run_id}.json",
            )
            require(notice == notification(assignment, marker), "notification_ready_mismatch")
            submitted = True
        matches.append((prefix, marker, submitted))
    require(len(matches) <= 1, "multiple_submissions_for_claim")
    return matches[0] if matches else None


def work(
    worker,
    data_root=None,
    *,
    remote="reposeer:",
    job_id=None,
    claim_token=None,
    dry_run=False,
    runner=None,
):
    """Retrieve one unambiguous assignment, then reuse the existing collector/upload path."""
    no_worker_dsn()
    worker_id(worker, "crawler")
    require(claim_token is None or job_id is not None, "claim_token_requires_job_id")
    if job_id is not None:
        uid(job_id)
    if claim_token is not None:
        uid(claim_token)
    require(shutil.which("uv") is not None, "uv_not_installed")
    data_root = Path(data_root) if data_root is not None else default_data_root(worker)
    drive = Drive(remote)
    drive.check()
    prefix = f"crawl/assignments/{worker}"
    if job_id is not None:
        prefix += "/" + job_id
    eligible, expired, submitted = [], 0, 0
    for name, size in sorted(drive.listing(prefix).items()):
        parts = name.split("/")
        require(len(parts) == (1 if job_id else 2), "invalid_assignment_path")
        remote_job = job_id if job_id else uid(parts[0])
        require(parts[-1].endswith(".json"), "invalid_assignment_path")
        remote_token = uid(parts[-1][:-5])
        if claim_token is not None and claim_token != remote_token:
            continue
        assignment = validate_assignment(
            drive.get_json(prefix + "/" + name, byte_size=size),
            expected_worker=worker,
            allow_expired=True,
        )
        require(assignment["kind"] == "collect_repository", "crawl_assignment_required")
        require(
            assignment["job_id"] == remote_job and assignment["claim_token"] == remote_token,
            "assignment_path_mismatch",
        )
        if timestamp(assignment["lease_expires_at"]) <= now():
            expired += 1
            continue
        root = safe_path(data_root, f"{remote_job}/{remote_token}")
        if (root / "ASSIGNMENT.json").exists():
            require(read_json(root / "ASSIGNMENT.json") == assignment, "assignment_local_conflict")
        previous = previous_submission(drive, assignment)
        if previous is not None and previous[2]:
            submitted += 1
            continue
        eligible.append((assignment, root, previous))
    require(len(eligible) <= 1, "ambiguous_assignments_use_job_id_and_claim_token")
    if not eligible:
        return dict(
            status="no_work", worker_id=worker, skipped_expired=expired, skipped_submitted=submitted
        )
    assignment, root, previous = eligible[0]
    if dry_run:
        return dict(status="assigned", dry_run=True, worker_id=worker, job_id=assignment["job_id"])
    # Reuse the bundle's own assignment file; immutable writes reject local conflicts.
    cached = safe_path(root, "ASSIGNMENT.json")
    write_json(cached, assignment)
    if previous is not None:
        # READY uploaded before a lost notification/local workspace: reuse its original run.
        drive.download(previous[0], root)
        marker, saved, _ = verify_bundle(root)
        require(marker == previous[1] and saved == assignment, "submission_assignment_conflict")
    result = run(worker, cached, data_root, remote=remote, runner=runner)
    return dict(
        status="submitted",
        worker_id=worker,
        **{
            key: result[key]
            for key in (
                "job_id",
                "integration_run_id",
                "overall_status",
                "artifact_submitted",
                "local_retained",
            )
        },
    )


def main():
    cli = parser(__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("run")
    sub.add_argument("--worker", required=True)
    sub.add_argument("--assignment", type=Path, required=True)
    sub.add_argument("--data-root", type=Path, required=True)
    sub.add_argument("--dry-run", action="store_true")
    sub.add_argument("--remote", default=argparse.SUPPRESS)
    sub = commands.add_parser("work", help="Retrieve and process one assigned job")
    sub.add_argument("--worker", required=True)
    sub.add_argument("--data-root", type=Path)
    sub.add_argument("--job-id")
    sub.add_argument("--claim-token")
    sub.add_argument("--dry-run", action="store_true")
    sub.add_argument("--remote", default=argparse.SUPPRESS)
    args = cli.parse_args()
    if args.command == "work":
        return work(
            args.worker,
            args.data_root,
            remote=args.remote,
            job_id=args.job_id,
            claim_token=args.claim_token,
            dry_run=args.dry_run,
        )
    return run(
        args.worker, args.assignment, args.data_root, remote=args.remote, dry_run=args.dry_run
    )


if __name__ == "__main__":
    cli_guard(main)
