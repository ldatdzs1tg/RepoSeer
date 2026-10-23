"""Mounted-Drive CSV crawlers plus the legacy C1-C7 runner; no worker DB access."""

import argparse
import json
import os
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
    checked_mount_path,
    cli_guard,
    collection_options,
    copy_verified,
    digest,
    files,
    fingerprint,
    format_utc_timestamp,
    integration_evidence,
    local_drive_workspace,
    mounted_display_path,
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
    slug,
    timestamp,
    uid,
    validate_assignment,
    validate_notification,
    validate_ready,
    verify_bundle,
    verify_drive_screening,
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


def drive_part_facts(root: Path, expected: dict, run_id: str) -> dict:
    """Reuse raw/provider verification, without constructing a legacy claim assignment."""
    facts = integration_evidence(
        root,
        dict(
            targets=expected["package_ids"],
            expected_repo_full_name=expected["repo_full_name"],
        ),
        run_id,
    )
    require(facts["overall_status"] in {"complete", "partial"}, "drive_collection_not_successful")
    require(
        facts["collected_repository"] and facts["verified_package_ids"] == expected["package_ids"],
        "drive_mapping_verification_failed",
    )
    require(
        safe_path(root, "targets.txt").read_bytes()
        == ("\n".join(expected["package_ids"]) + "\n").encode("utf-8"),
        "drive_targets_mismatch",
    )
    return facts


def verify_drive_part(root: Path, expected: dict, *, marker: dict | None = None) -> dict:
    root = checked_mount_path(root)
    marker = read_json(safe_path(root, "READY.json")) if marker is None else marker
    require(
        type(marker.get("schema_version")) is int
        and all(marker.get(key) == value for key, value in expected.items()),
        "drive_part_identity_mismatch",
    )
    require(
        read_json(safe_path(root, "COLLECTION_INPUT.json")) == expected, "drive_part_input_mismatch"
    )
    checksums = safe_path(root, "CHECKSUMS.sha256")
    declared = read_checksums(checksums)
    require(fingerprint(checksums)[0] == marker["checksums_sha256"], "checksum_list_mismatch")
    inventory = files(root, declared=declared)
    require(
        declared
        == {
            name: value
            for name, value in inventory.items()
            if name not in {"CHECKSUMS.sha256", "READY.json"}
        },
        "bundle_checksum_mismatch",
    )
    facts = drive_part_facts(root, expected, uid(marker["integration_run_id"]))
    require(
        marker["collection_status"] == facts["overall_status"]
        and marker["completed_at_utc"] == facts["finished_at"],
        "drive_part_status_mismatch",
    )
    return marker


def collect_drive_part(root: Path, expected: dict, log: Path, *, runner=None) -> dict:
    root = checked_mount_path(root)
    if (root / "READY.json").exists():
        return verify_drive_part(root, expected)
    input_path = safe_path(root, "COLLECTION_INPUT.json")
    started = input_path.exists()
    require(started or not files(root), "untracked_local_collection_requires_inspection")
    write_json(input_path, expected)
    targets = safe_path(root, "targets.txt")
    atomic_bytes(targets, ("\n".join(expected["package_ids"]) + "\n").encode("utf-8"))
    manifests = sorted(safe_path(root, "metadata/collection_runs").glob("integration_*.json"))
    require(len(manifests) <= 1, "multiple_local_integration_runs")
    if not manifests:
        require(not started, "drive_collection_interrupted_requires_inspection")
        options = collection_options("initial")
        command = [
            "uv",
            "run",
            "--frozen",
            "reposeer",
            "collect-dataset",
            "--targets-file",
            str(targets),
            "--data-dir",
            str(root),
            "--mode",
            "live",
        ]
        for name, value in options.items():
            command += [
                "--" + name.replace("_", "-"),
                ",".join(value) if isinstance(value, list) else str(value),
            ]
        # No Registry credential reaches the child, even from an operator's shell.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() != "REPOSEER_REGISTRY_DSN"
        }
        interrupted = False
        with tempfile.TemporaryFile(mode="w+b", dir=root.parent) as output:
            try:
                (runner or subprocess.run)(
                    command,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=6 * 3600,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError):
                interrupted = True
            output.seek(0)
            text = redact(output.read(16 * 1024**2).decode("utf-8", errors="replace"))
        atomic_bytes(log, text.encode("utf-8"), immutable=False)
        require(not interrupted, "drive_collection_interrupted_requires_inspection")
        manifests = sorted(safe_path(root, "metadata/collection_runs").glob("integration_*.json"))
    require(len(manifests) == 1, "integration_manifest_missing")
    manifest = read_json(manifests[0])
    require(
        manifest.get("finished_at") is not None, "drive_collection_interrupted_requires_inspection"
    )
    run_id = uid(manifests[0].stem.removeprefix("integration_"))
    facts = drive_part_facts(root, expected, run_id)
    inventory = files(root)
    lines = "".join(
        f"{sha} {size} {name}\n"
        for name, (sha, size) in inventory.items()
        if name not in {"CHECKSUMS.sha256", "READY.json"}
    )
    atomic_bytes(safe_path(root, "CHECKSUMS.sha256"), lines.encode("utf-8"))
    write_json(
        safe_path(root, "READY.json"),
        dict(
            **expected,
            integration_run_id=run_id,
            collection_status=facts["overall_status"],
            checksums_sha256=digest(lines.encode("utf-8")),
            completed_at_utc=facts["finished_at"],
        ),
    )
    return verify_drive_part(root, expected)


def publish_drive_part(source: Path, destination: Path, expected: dict) -> dict:
    source, destination = checked_mount_path(source), checked_mount_path(destination)
    marker = verify_drive_part(source, expected)
    inventory = files(source, declared=read_checksums(source / "CHECKSUMS.sha256"))
    existing = files(destination, declared=inventory)
    require(set(existing) <= set(inventory), "unexpected_mounted_bundle_files")
    require(
        all(value == inventory[name] for name, value in existing.items()),
        "mounted_path_checksum_conflict",
    )
    for name in sorted(set(inventory) - {"READY.json"}):
        copy_verified(safe_path(source, name), safe_path(destination, name))
    verify_drive_part(destination, expected, marker=marker)
    copy_verified(safe_path(source, "READY.json"), safe_path(destination, "READY.json"))
    require(verify_drive_part(destination, expected) == marker, "mounted_bundle_changed")
    return marker


def crawl_drive(
    assignment: Path,
    *,
    data_root: Path | None = None,
    dry_run: bool = False,
    runner=None,
) -> dict:
    """Collect a published mounted CSV; no Registry, lease, token or rclone remote."""
    assignment = checked_mount_path(assignment)
    worker = worker_id(assignment.parent.name, "crawler")
    run_id = uid(assignment.stem)
    require(
        assignment.suffix == ".csv" and assignment.parent.parent.name == "assignments",
        "invalid_drive_assignment_path",
    )
    drive_root = assignment.parents[2]
    safe_path(drive_root, f"assignments/{worker}/{run_id}.csv")
    ready, _, assignments = verify_drive_screening(drive_root, run_id)
    groups = {}
    for row in assignments[worker]:
        repo = slug(row["repo_full_name"]).casefold()
        groups.setdefault(repo, []).append(row["package_id"])
    base = local_drive_workspace("work", data_root, drive_root)
    plan = []
    for repo, packages in sorted(groups.items()):
        packages = sorted(packages)
        repository_id = digest(repo.encode("utf-8"))
        for index, offset in enumerate(range(0, len(packages), 100), 1):
            expected = dict(
                schema_version=1,
                sampling_run_id=run_id,
                worker_id=worker,
                repo_full_name=repo,
                repository_id=repository_id,
                part_index=index,
                package_ids=packages[offset : offset + 100],
                assignment_sha256=ready["assignments_sha256"][worker],
                collection=collection_options("initial"),
            )
            suffix = f"{worker}/{run_id}/{repository_id}/part_{index:03d}"
            plan.append(
                (expected, safe_path(base, suffix), safe_path(drive_root, "submissions/" + suffix))
            )
    summary = dict(
        status="planned" if dry_run else "completed",
        dry_run=dry_run,
        worker_id=worker,
        sampling_run_id=run_id,
        packages=len(assignments[worker]),
        repositories=len(groups),
        parts=len(plan),
        unit="repository_parts",
        completed=0,
        partial=0,
        failed=0,
        skipped_existing=0,
        output=mounted_display_path(drive_root / "submissions" / worker / run_id),
        failures=[],
    )
    if dry_run:
        return summary
    for expected, local, destination in plan:
        try:
            if (destination / "READY.json").exists():
                verify_drive_part(destination, expected)
                summary["skipped_existing"] += 1
                continue
            require(
                (local / "COLLECTION_INPUT.json").exists() or not files(destination),
                "mounted_partial_submission_requires_local_workspace",
            )
            log = safe_path(
                base,
                f"{worker}/{run_id}/logs/{expected['repository_id']}-part_{expected['part_index']:03d}.log",
            )
            collect_drive_part(local, expected, log, runner=runner)
            marker = publish_drive_part(local, destination, expected)
            summary["completed" if marker["collection_status"] == "complete" else "partial"] += 1
        except (CoordinationError, OSError, ValueError, TypeError, KeyError) as error:
            summary["failed"] += 1
            summary["failures"].append(
                dict(
                    repository_id=expected["repository_id"],
                    part_index=expected["part_index"],
                    error_code=str(error)
                    if isinstance(error, CoordinationError)
                    else type(error).__name__,
                )
            )
    if summary["failed"]:
        summary["status"] = "failed"
    return summary


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
    sub = commands.add_parser("crawl-drive", help="Collect a CSV from mounted Google Drive")
    sub.add_argument("--assignment", type=Path, required=True)
    sub.add_argument("--data-root", type=Path)
    sub.add_argument("--dry-run", action="store_true")
    args = cli.parse_args()
    if args.command == "crawl-drive":
        result = crawl_drive(args.assignment, data_root=args.data_root, dry_run=args.dry_run)
        if result["failed"]:
            print(json.dumps(result, sort_keys=True))
            raise SystemExit(1)
        return result
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
