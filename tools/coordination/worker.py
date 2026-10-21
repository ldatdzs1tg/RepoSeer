"""Sequential C1-C7 runner. No registry credential or database connection."""

import argparse
import re
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
    format_utc_timestamp,
    integration_evidence,
    no_worker_dsn,
    notification,
    now,
    parser,
    read_json,
    redact,
    require,
    safe_path,
    seal_bundle,
    uid,
    validate_assignment,
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


def main():
    cli = parser(__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("run")
    sub.add_argument("--worker", required=True)
    sub.add_argument("--assignment", type=Path, required=True)
    sub.add_argument("--data-root", type=Path, required=True)
    sub.add_argument("--dry-run", action="store_true")
    sub.add_argument("--remote", default=argparse.SUPPRESS)
    args = cli.parse_args()
    return run(
        args.worker, args.assignment, args.data_root, remote=args.remote, dry_run=args.dry_run
    )


if __name__ == "__main__":
    cli_guard(main)
