"""Mounted-Drive screening/allocation plus legacy Registry coordination."""

import argparse
import csv
import io
import json
import os
import sys
import tempfile
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rich.console import Console, Group
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn
from rich.text import Text
from tools.coordination._common import (
    ASSIGNMENT_FIELDS,
    DRIVE_WORKERS,
    MAPPING_FIELDS,
    MAX_METADATA,
    CoordinationError,
    Drive,
    Registry,
    allocate_drive_assignments,
    artifact_row,
    atomic_bytes,
    canonical,
    check_claim,
    checked_mount_path,
    cli_guard,
    collection_options,
    copy_verified,
    digest,
    enabled_worker,
    files,
    fingerprint,
    finish_job,
    format_utc_timestamp,
    local_drive_workspace,
    mounted_display_path,
    no_worker_dsn,
    now,
    parser,
    read_checksums,
    read_cumulative_mapping,
    read_json,
    relative,
    require,
    safe_path,
    screening_candidate,
    seal_bundle,
    sign_assignment,
    slug,
    subject,
    timestamp,
    uid,
    validate_assignment,
    verify_bundle,
    verify_drive_screening,
    worker_id,
    write_json,
)

from reposeer.clients.pypi import PyPIClient
from reposeer.collection.http.client import HTTPClient
from reposeer.contracts.models import RawRecord
from reposeer.exceptions import HTTPClientError
from reposeer.pipelines.collection import _repository_candidate
from reposeer.pipelines.package_discovery import CSV_FIELDS
from reposeer.storage.screening_mapping import (
    read_mapping_csv,
    validate_mapping_row,
    validate_master_metadata,
)

CLAIM_SQL = """SELECT j.*,r.repo_full_name FROM reposeer.crawl_jobs j
 LEFT JOIN reposeer.repositories r USING(repository_id)
 WHERE j.job_kind=%s AND (j.scope_key='initial' OR (j.job_kind='collect_repository' AND j.scope_key IN ('followup:1','followup:2'))) AND j.status IN ('queued','retry')
 AND (j.retry_after IS NULL OR j.retry_after<=now()) AND j.attempt_count<j.max_attempts
 ORDER BY j.priority DESC,CASE j.scope_key WHEN 'initial' THEN 0 WHEN 'followup:1' THEN 1 ELSE 2 END,j.created_at,j.job_id FOR UPDATE OF j SKIP LOCKED LIMIT %s"""


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


def write_drive_table(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_bytes(path, stream.getvalue().encode("utf-8"))


def drive_screening_summary(root: Path, run_id: str, *, reused: bool) -> dict:
    marker, mapping, assigned = verify_drive_screening(root, run_id)
    skipped = marker.get("skipped_existing_package_ids", [])
    return dict(
        status="completed",
        sampling_run_id=run_id,
        reused=reused,
        package_count=len(mapping),
        input_package_count=len(mapping) + len(skipped),
        skipped_existing_count=len(skipped),
        skipped_existing_examples=skipped[:5],
        **{
            status: sum(row["screening_status"] == status for row in mapping)
            for status in ("candidate", "unresolved", "ambiguous", "failed")
        },
        assignments={
            worker: dict(packages=len(rows), repositories=len({r["repo_full_name"] for r in rows}))
            for worker, rows in assigned.items()
        },
        mapping=mounted_display_path(root / f"screening/runs/{run_id}/mapping.csv"),
    )


def _merge_master_rows(existing: dict[str, dict], new_rows: list[dict]):
    merged = dict(existing)
    added = duplicates = 0
    for row in new_rows:
        validate_mapping_row(row)
        key = row["package_id"]
        old = merged.get(key)
        if old is None:
            merged[key] = row
            added += 1
        elif old != row:
            require(
                old["sampling_run_id"] != row["sampling_run_id"], f"master_same_run_conflict:{key}"
            )
            duplicates += 1
    return merged, dict(
        previously_stored=len(existing),
        new_packages_added=added,
        historical_duplicates=duplicates,
        total_unique_packages=len(merged),
    )


def _stage_master(stage: Path, rows: dict[str, dict], run_id: str | None, *, meta=None):
    path = stage / "mapping.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MAPPING_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows[key] for key in sorted(rows))
        stream.flush()
        os.fsync(stream.fileno())
    if meta is None:
        meta = dict(
            schema_version=1,
            mapping_sha256=fingerprint(path)[0],
            row_count=len(rows),
            updated_at_utc=format_utc_timestamp(now()),
            last_merged_run_id=run_id,
        )
    require(read_mapping_csv(path, meta) == rows, "master_staging_mismatch")
    write_json(stage / "mapping.meta.json", meta)
    return meta


def _master_state(root: Path):
    path = safe_path(root, "screening/mapping.csv")
    meta = safe_path(root, "screening/mapping.meta.json")
    return (
        fingerprint(path)[0] if path.exists() else None,
        read_json(meta) if meta.exists() else None,
    )


def _publish_pending_master(root: Path, pending: dict, stage: Path):
    journal = safe_path(root, "screening/mapping.pending.json")
    require(read_json(journal) == pending, "master_journal_changed")
    current_sha, current_meta = _master_state(root)
    previous = pending["previous_metadata"]
    next_meta = pending["next_metadata"]
    require(current_meta in (previous, next_meta), "master_recovery_metadata_conflict")
    if current_sha != pending["next_mapping_sha256"]:
        require(
            current_sha == pending["previous_mapping_sha256"] and current_meta == previous,
            "master_changed_during_update",
        )
    copy_verified(stage / "mapping.csv", safe_path(root, "screening/mapping.csv"), replace=True)
    copy_verified(
        stage / "mapping.meta.json", safe_path(root, "screening/mapping.meta.json"), replace=True
    )
    actual, _ = read_cumulative_mapping(root, allow_pending=True)
    require(actual == next_meta, "master_publication_metadata_mismatch")
    require(read_json(journal) == pending, "master_journal_changed")
    journal.unlink()


def _local_master_stage(root: Path, scratch: str) -> Path:
    stage = checked_mount_path(Path(scratch).absolute())
    require(not stage.resolve().is_relative_to(root.resolve()), "master_staging_must_be_local")
    return stage


def _begin_master_update(root: Path, rows, *, previous_meta, previous_sha, run_ids, operation):
    run_id = run_ids[-1] if run_ids else None
    with tempfile.TemporaryDirectory(prefix="reposeer-master-") as scratch:
        stage = _local_master_stage(root, scratch)
        next_meta = _stage_master(stage, rows, run_id)
        pending = dict(
            schema_version=1,
            run_id=run_id,
            previous_mapping_sha256=previous_sha,
            next_mapping_sha256=next_meta["mapping_sha256"],
            next_row_count=len(rows),
            previous_metadata=previous_meta,
            next_metadata=next_meta,
            operation=operation,
            runs=run_ids,
        )
        require(
            _master_state(root) == (previous_sha, previous_meta), "master_changed_during_update"
        )
        journal = safe_path(root, "screening/mapping.pending.json")
        journal.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also catches a second writer on the same mounted filesystem.
        # Cross-machine synchronization is still governed by the documented single writer.
        with journal.open("xb") as stream:
            stream.write(canonical(pending))
            stream.flush()
            os.fsync(stream.fileno())
        _publish_pending_master(root, pending, stage)


def _verified_history(root: Path, run_ids: list[str]):
    rows, order, duplicates, total = {}, [], 0, 0
    for run_id in run_ids:
        marker, mapping, _ = verify_drive_screening(root, uid(run_id))
        order.append((timestamp(marker["generated_at_utc"]), run_id))
        rows, counts = _merge_master_rows(rows, mapping)
        duplicates += counts["historical_duplicates"]
        total += len(mapping)
    require(order == sorted(order), "bootstrap_run_order_mismatch")
    return rows, duplicates, total


def recover_pending_mapping(root: Path) -> bool:
    """Finish only a journal whose old or new state and immutable sources verify."""
    root = checked_mount_path(root)
    path = safe_path(root, "screening/mapping.pending.json")
    if not path.exists():
        return False
    pending = read_json(path)
    require(
        set(pending)
        == {
            "schema_version",
            "run_id",
            "previous_mapping_sha256",
            "next_mapping_sha256",
            "next_row_count",
            "previous_metadata",
            "next_metadata",
            "operation",
            "runs",
        },
        "master_journal_schema_mismatch",
    )
    require(
        type(pending["schema_version"]) is int and pending["schema_version"] == 1,
        "master_journal_version",
    )
    next_meta = validate_master_metadata(pending["next_metadata"])
    require(
        pending["next_mapping_sha256"] == next_meta["mapping_sha256"]
        and type(pending["next_row_count"]) is int
        and pending["next_row_count"] == next_meta["row_count"]
        and pending["run_id"] == next_meta["last_merged_run_id"],
        "master_journal_metadata_mismatch",
    )
    run_ids = pending["runs"]
    require(
        isinstance(run_ids, list) and all(isinstance(run, str) for run in run_ids),
        "master_journal_runs_mismatch",
    )
    require(
        len(set(run_ids)) == len(run_ids)
        and pending["run_id"] == (run_ids[-1] if run_ids else None),
        "master_journal_runs_mismatch",
    )
    operation, previous = pending["operation"], pending["previous_metadata"]
    previous_sha = pending["previous_mapping_sha256"]
    require(
        previous_sha is None
        or (
            isinstance(previous_sha, str)
            and len(previous_sha) == 64
            and all(c in "0123456789abcdef" for c in previous_sha)
        ),
        "master_journal_previous_checksum",
    )
    if operation == "merge":
        validate_master_metadata(previous)
        require(
            run_ids == [pending["run_id"]] and previous_sha == previous["mapping_sha256"],
            "master_journal_runs_mismatch",
        )
        _, new_rows, _ = verify_drive_screening(root, uid(pending["run_id"]))
    else:
        require(operation == "bootstrap" and previous is None, "master_journal_operation_mismatch")
        history, _, _ = _verified_history(root, run_ids)
        if previous_sha is not None:
            require(
                run_ids
                and previous_sha
                == read_json(safe_path(root, f"screening/runs/{run_ids[-1]}/READY.json"))[
                    "mapping_sha256"
                ],
                "legacy_mapping_alias_mismatch",
            )
    current_sha, current_meta = _master_state(root)
    require(current_meta in (previous, next_meta), "master_recovery_metadata_conflict")
    if current_sha == pending["next_mapping_sha256"]:
        rows = read_mapping_csv(safe_path(root, "screening/mapping.csv"), next_meta)
        if operation == "bootstrap":
            require(rows == history, "master_recovery_source_mismatch")
        else:
            merged, counts = _merge_master_rows(rows, new_rows)
            require(
                merged == rows and counts["new_packages_added"] == 0,
                "master_recovery_source_mismatch",
            )
    else:
        require(
            current_sha == previous_sha and current_meta == previous,
            "master_recovery_state_conflict",
        )
        if operation == "bootstrap":
            rows = history
        else:
            _, old_rows = read_cumulative_mapping(root, allow_pending=True)
            rows, _ = _merge_master_rows(old_rows, new_rows)
    with tempfile.TemporaryDirectory(prefix="reposeer-master-recovery-") as scratch:
        stage = _local_master_stage(root, scratch)
        _stage_master(stage, rows, pending["run_id"], meta=next_meta)
        _publish_pending_master(root, pending, stage)
    return True


def merge_cumulative_mapping(root: Path, run_id: str, *, snapshot=None, dry_run=False):
    root = checked_mount_path(root)
    if snapshot is None:
        if not dry_run:
            recover_pending_mapping(root)
        snapshot = read_cumulative_mapping(root)
    meta, existing = snapshot
    _, mapping, _ = verify_drive_screening(root, run_id)
    rows, counts = _merge_master_rows(existing, mapping)
    if counts["new_packages_added"] and not dry_run:
        _begin_master_update(
            root,
            rows,
            previous_meta=meta,
            previous_sha=meta["mapping_sha256"],
            run_ids=[run_id],
            operation="merge",
        )
    return counts


def bootstrap_mapping(drive_root: Path, *, dry_run: bool = False) -> dict:
    root = checked_mount_path(drive_root)
    if not dry_run:
        recover_pending_mapping(root)
    else:
        require(
            not safe_path(root, "screening/mapping.pending.json").exists(),
            "master_update_pending_retry_D2",
        )
    if safe_path(root, "screening/mapping.meta.json").exists():
        _, rows = read_cumulative_mapping(root)
        return dict(status="already_initialized", dry_run=dry_run, total_unique_packages=len(rows))
    runs = safe_path(root, "screening/runs")
    completed = []
    if runs.exists():
        for path in runs.iterdir():
            checked_mount_path(path)
            if path.is_dir() and (path / "READY.json").exists():
                run_id = uid(path.name)
                marker = read_json(safe_path(path, "READY.json"))
                completed.append((timestamp(marker["generated_at_utc"]), run_id))
    run_ids = [run_id for _, run_id in sorted(completed)]
    rows, duplicates, total = _verified_history(root, run_ids)
    alias = safe_path(root, "screening/mapping.csv")
    previous_sha = fingerprint(alias)[0] if alias.exists() else None
    if previous_sha is not None:
        require(
            run_ids
            and previous_sha
            == read_json(safe_path(root, f"screening/runs/{run_ids[-1]}/READY.json"))[
                "mapping_sha256"
            ],
            "legacy_mapping_alias_mismatch",
        )
    result = dict(
        status="planned" if dry_run else "initialized",
        dry_run=dry_run,
        completed_historical_runs=len(run_ids),
        total_rows_read=total,
        historical_duplicates=duplicates,
        total_unique_packages=len(rows),
    )
    if not dry_run:
        _begin_master_update(
            root,
            rows,
            previous_meta=None,
            previous_sha=previous_sha,
            run_ids=run_ids,
            operation="bootstrap",
        )
    return result


class _ScreeningProgress:
    """Report persisted rows on stderr, keeping the CLI's stdout JSON untouched."""

    def __init__(self, total: int, *, enabled: bool = True, started_at: float | None = None):
        self.total = total
        self.enabled = enabled
        self.started_at = monotonic() if started_at is None else started_at
        self.processed = self.cached = 0
        self.counts = dict.fromkeys(("candidate", "unresolved", "ambiguous", "failed"), 0)
        self.console = None
        self.live = None

    def __enter__(self):
        if self.enabled:
            interactive = sys.stderr.isatty()
            self.console = Console(stderr=True, force_terminal=interactive)
            if interactive:
                self.bar = Progress(
                    TextColumn("Screening"),
                    BarColumn(bar_width=None),
                    TextColumn("{task.completed:.0f}/{task.total:.0f} ({task.percentage:.1f}%)"),
                    console=self.console,
                    auto_refresh=False,
                )
                self.task = self.bar.add_task("Screening", total=self.total)
                self.live = Live(
                    console=self.console,
                    get_renderable=self._render,
                    refresh_per_second=4,
                    redirect_stdout=False,
                )
                self.live.start(refresh=True)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.live is not None:
            self.live.stop()

    def _details(self) -> str:
        mapped = self.counts["candidate"]
        unresolved = self.counts["unresolved"]
        ambiguous = self.counts["ambiguous"]
        failed = self.counts["failed"]
        remaining = self.total - self.processed
        elapsed = max(0, monotonic() - self.started_at)
        eta = (
            str(timedelta(seconds=int(elapsed * remaining / self.processed)))
            if self.processed
            else "--:--"
        )
        return (
            f"Mapped: {mapped} | Unresolved: {unresolved} | Ambiguous: {ambiguous} | Failed: {failed}\n"
            f"Unmapped: {unresolved + ambiguous + failed} | Cached: {self.cached} | Remaining: {remaining}\n"
            f"Elapsed: {timedelta(seconds=int(elapsed))} | ETA: {eta}"
        )

    def _render(self):
        return Group(
            Text("D2 Screening PyPI Packages"),
            self.bar.get_renderable(),
            Text(self._details()),
        )

    def advance(self, row: dict, *, is_cached: bool) -> None:
        status = row["screening_status"]
        self.counts[status] = self.counts.get(status, 0) + 1
        self.processed += 1
        self.cached += int(is_cached)
        if not self.enabled:
            return
        if self.live is not None:
            self.bar.update(self.task, completed=self.processed)
        elif self.processed % 25 == 0 or self.processed == self.total:
            self.console.print(
                f"[D2] {self.processed}/{self.total} ({self.processed / self.total * 100:.1f}%) "
                + self._details().replace("\n", " | "),
                markup=False,
                highlight=False,
                soft_wrap=True,
            )


def screen_drive(
    run_dir: Path,
    *,
    data_root: Path | None = None,
    dry_run: bool = False,
    client=None,
    no_progress: bool = False,
) -> dict:
    """Screen a mounted D1 run and publish all seven assignments without a Registry."""
    started_at = monotonic()
    run_dir = checked_mount_path(run_dir)
    run_id = uid(run_dir.name)
    require(
        run_dir.parent.name == "submissions" and run_dir.parent.parent.name == "discovery",
        "invalid_discovery_run_path",
    )
    drive_root = run_dir.parents[2]
    safe_path(drive_root, f"discovery/submissions/{run_id}/manifest.json")
    manifest, packages = discovery_bundle(run_dir)
    require(manifest["sampling_run_id"] == run_id, "sampling_directory_run_mismatch")
    source_hashes = dict(
        discovery_manifest_sha256=fingerprint(safe_path(run_dir, "manifest.json"))[0],
        discovery_csv_sha256=fingerprint(safe_path(run_dir, "package_candidates.csv"))[0],
    )
    destination = safe_path(drive_root, f"screening/runs/{run_id}")
    if not dry_run:
        recover_pending_mapping(drive_root)
    snapshot = read_cumulative_mapping(drive_root)
    if (destination / "READY.json").exists():
        result = drive_screening_summary(drive_root, run_id, reused=True)
        master = merge_cumulative_mapping(drive_root, run_id, snapshot=snapshot, dry_run=dry_run)
        return dict(result, dry_run=dry_run, master=master)
    existing_ids = set(snapshot[1])
    skipped_ids = [row["package_id"] for row in packages if row["package_id"] in existing_ids]
    new_packages = [row for row in packages if row["package_id"] not in existing_ids]
    if dry_run:
        return dict(
            status="planned",
            dry_run=True,
            sampling_run_id=run_id,
            package_count=len(packages),
            to_screen=len(new_packages),
            skipped_existing_count=len(skipped_ids),
            skipped_existing_examples=skipped_ids[:5],
        )
    stage = safe_path(local_drive_workspace("screening", data_root, drive_root), run_id)
    output = safe_path(stage, f"screening/runs/{run_id}")
    if not (output / "READY.json").exists():
        intent_path = safe_path(stage, "COLLECTION_POLICY.json")
        intent = dict(policy_id="distributed-v2", source_hashes=source_hashes)
        require(intent_path.exists() or not files(stage), "staged_policy_requires_inspection")
        write_json(intent_path, intent)
        for name in ("manifest.json", "package_candidates.csv"):
            copy_verified(
                safe_path(run_dir, name), safe_path(stage, f"discovery/submissions/{run_id}/{name}")
            )
        mapping, evidence = [], []
        with (
            (
                HTTPClient(timeout=30, max_retries=3, max_backoff_seconds=30)
                if new_packages
                else nullcontext()
            ) as http,
            (
                _ScreeningProgress(
                    len(new_packages), enabled=not no_progress, started_at=started_at
                )
                if new_packages
                else nullcontext()
            ) as progress,
        ):
            pypi = (client or PyPIClient(http_client=http)) if new_packages else None
            for package in new_packages:
                key = package["package_id"]
                checkpoint = safe_path(stage, f"results/{digest(key.encode('utf-8'))}.json")
                is_cached = checkpoint.exists()
                if is_cached:
                    saved = read_json(checkpoint)
                    require(
                        saved["source_hashes"] == source_hashes
                        and saved["row"]["package_id"] == key,
                        "screening_checkpoint_conflict",
                    )
                else:
                    row = dict(
                        sampling_run_id=run_id,
                        package_id=key,
                        package_name=key[5:],
                        repo_full_name="",
                        screening_status="failed",
                        reason_code="pypi_request_failed",
                        source_url=f"https://pypi.org/pypi/{key[5:]}/json",
                        retrieved_at_utc=format_utc_timestamp(now()),
                        evidence_sha256="",
                    )
                    item = None
                    try:
                        payload = pypi.get_package_info(key[5:])
                        require(
                            isinstance(payload, dict) and isinstance(payload.get("info"), dict),
                            "pypi_response_invalid",
                        )
                        observed = format_utc_timestamp(now())
                        info = payload["info"]
                        item = dict(
                            package_id=key,
                            source_url=row["source_url"],
                            retrieved_at_utc=observed,
                            info={field: info.get(field) for field in ("name", "project_urls")},
                        )
                        encoded = canonical(item)
                        require(len(encoded) <= MAX_METADATA, "pypi_response_invalid")
                        sha = digest(encoded)
                        repo, status, reason = screening_candidate(item)
                        row.update(
                            repo_full_name=repo,
                            screening_status=status,
                            reason_code=reason,
                            retrieved_at_utc=observed,
                            evidence_sha256=sha,
                        )
                    except HTTPClientError as error:
                        row["reason_code"] = (
                            "rate_limited"
                            if error.status_code in {403, 429}
                            else "pypi_request_failed"
                        )
                        item = None
                    except (ValueError, TypeError):
                        row["reason_code"], item = "pypi_response_invalid", None
                    saved = dict(source_hashes=source_hashes, row=row, evidence=item)
                    write_json(checkpoint, saved)
                mapping.append(saved["row"])
                if saved["evidence"] is not None:
                    evidence.append(saved["evidence"])
                progress.advance(saved["row"], is_cached=is_cached)
        assigned = allocate_drive_assignments(mapping)
        write_drive_table(safe_path(output, "mapping.csv"), MAPPING_FIELDS, mapping)
        atomic_bytes(
            safe_path(output, "pypi_evidence.jsonl"),
            b"".join(canonical(item) + b"\n" for item in evidence),
        )
        for worker, rows in assigned.items():
            write_drive_table(
                safe_path(stage, f"assignments/{worker}/{run_id}.csv"), ASSIGNMENT_FIELDS, rows
            )
        write_json(
            safe_path(output, "READY.json"),
            dict(
                schema_version=1,
                sampling_run_id=run_id,
                collection_policy=dict(policy_id="distributed-v2"),
                mapping_sha256=fingerprint(output / "mapping.csv")[0],
                evidence_sha256=fingerprint(output / "pypi_evidence.jsonl")[0],
                assignments_sha256={
                    worker: fingerprint(stage / f"assignments/{worker}/{run_id}.csv")[0]
                    for worker in DRIVE_WORKERS
                },
                package_count=len(mapping),
                skipped_existing_package_ids=skipped_ids,
                assigned_package_count=sum(map(len, assigned.values())),
                generated_at_utc=format_utc_timestamp(now()),
                **source_hashes,
            ),
        )
    local_marker, _, _ = verify_drive_screening(stage, run_id)
    require(
        all(local_marker[field] == value for field, value in source_hashes.items()),
        "discovery_source_changed",
    )
    for name in ("mapping.csv", "pypi_evidence.jsonl"):
        copy_verified(safe_path(output, name), safe_path(destination, name))
    for worker in DRIVE_WORKERS:
        name = f"assignments/{worker}/{run_id}.csv"
        copy_verified(safe_path(stage, name), safe_path(drive_root, name))
    # Verify the whole published set before exposing READY, including sync delays.
    verify_drive_screening(drive_root, run_id, marker=local_marker)
    copy_verified(safe_path(output, "READY.json"), safe_path(destination, "READY.json"))
    result = drive_screening_summary(drive_root, run_id, reused=False)
    return dict(result, master=merge_cumulative_mapping(drive_root, run_id, snapshot=snapshot))


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
    identity = (
        assignment["assignment_id"]
        if screening
        else assignment["job_id"] + "/" + assignment["claim_token"]
    )
    final = safe_path(out_dir, f"{identity}.json")
    items = assignment["jobs"] if screening else [assignment]

    def check():
        with registry.connect(readonly=True) as db:
            enabled_worker(db, assignment["worker_id"], "screening" if screening else "crawler")
            for item in items:
                row = db.execute(
                    "SELECT j.*,r.repo_full_name FROM reposeer.crawl_jobs j LEFT JOIN reposeer.repositories r USING(repository_id) WHERE job_id=%s",
                    (item["job_id"],),
                ).fetchone()
                check_claim(row, assignment, item)

    check()
    write_json(final, assignment)
    prefix = "screening" if screening else "crawl"
    # Token-specific immutable paths cannot overwrite a newer claim's assignment.
    drive.put(final, f"{prefix}/assignments/{assignment['worker_id']}/{identity}.json")
    check()  # A reclaimed claim leaves its journal for inspection, never signals success.
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
                            collection=collection_options(job["scope_key"]),
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
                    "UPDATE reposeer.crawl_jobs SET status='claimed',claimed_by=%s,claim_token=%s,claimed_at=now(),lease_expires_at=%s,attempt_count=attempt_count+1,assignment_targets=%s,assignment_sha256=%s,screen_assignment_id=%s,updated_at=now() WHERE job_id=%s",
                    (
                        worker,
                        item["claim_token"],
                        expiry,
                        Jsonb(targets),
                        assignment["assignment_sha256"],
                        assignment.get("assignment_id"),
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
    registry = registry or Registry()
    with registry.connect(readonly=dry_run) as db:
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
            db.execute(
                "UPDATE reposeer.packages SET last_screening_run_id=%s WHERE package_id=%s",
                (identity, result["package_id"]),
            )
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
    candidates = [r["package_id"] for r in results if r["screening_status"] == "candidate"]
    if candidates:
        counters["verification"] = verify_pending(
            registry=registry, remote=remote, apply=True, limit=len(candidates), packages=candidates
        )
    return counters


def verify_pending(*, registry=None, remote="reposeer:", limit=100, apply=False, packages=None):
    """Reuse immutable screening + accepted repository observations; never call providers."""
    from psycopg.types.json import Jsonb

    from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
    from reposeer.collectors.github.repository import repository_exclusion_reasons
    from reposeer.collectors.package.repository_mapping import PackageRepositoryResolver
    from reposeer.contracts import validate_record
    from reposeer.contracts.identity import parse_repo_id
    from reposeer.pipelines.normalization import NormalizationPipeline

    require(1 <= limit <= 1000, "invalid_verification_limit")
    registry, drive = registry or Registry(), Drive(remote)
    sql = "SELECT p.*,r.github_repo_id,r.repo_full_name FROM reposeer.packages p JOIN reposeer.repositories r USING(repository_id) WHERE p.mapping_status='candidate' AND r.github_repo_id IS NOT NULL"
    params = []
    if packages is not None:
        require(1 <= len(packages) <= 100, "invalid_verification_packages")
        sql += " AND p.package_id=ANY(%s)"
        params.append(packages)
    sql += " ORDER BY p.package_id LIMIT %s"
    params.append(limit)
    with registry.connect(readonly=True) as db:
        pending = db.execute(sql, params).fetchall()
    results, cache = [], {}
    with tempfile.TemporaryDirectory(prefix="reposeer-verify-") as scratch:
        scratch = Path(scratch)

        def repository_evidence(package):
            offset, batch_size = 0, 32
            while True:
                with registry.connect(readonly=True) as db:
                    candidates = db.execute(
                        "SELECT a.*,c.catalog_metadata,j.repository_id FROM reposeer.crawl_jobs j JOIN reposeer.crawl_runs c USING(job_id) JOIN reposeer.artifacts a USING(run_id) WHERE j.repository_id=%s AND c.run_kind='integration' AND a.artifact_kind='bundle' AND a.status='ingested' AND (c.catalog_metadata->>'github_repo_id' IS NULL OR c.catalog_metadata->>'github_repo_id'=%s) ORDER BY c.finished_at DESC NULLS LAST,c.run_id DESC LIMIT %s OFFSET %s",
                        (
                            package["repository_id"],
                            str(package["github_repo_id"]),
                            batch_size,
                            offset,
                        ),
                    ).fetchall()
                for accepted in candidates:
                    run_id = uid(str(accepted["run_id"]))
                    prefix = f"canonical/runs/{run_id}"
                    require(
                        accepted["storage_path"] == drive.path(prefix + "/CHECKSUMS.sha256"),
                        "accepted_repository_path_mismatch",
                    )
                    cache_key = ("github", run_id)
                    if cache_key not in cache:
                        folder = scratch / run_id
                        folder.mkdir(exist_ok=True)
                        checksums = folder / "CHECKSUMS.sha256"
                        size = drive.listing(prefix).get("CHECKSUMS.sha256")
                        require(
                            type(size) is int and 0 < size <= MAX_METADATA,
                            "checksum_list_size_limit",
                        )
                        drive._run(
                            ["copyto", drive.path(prefix + "/CHECKSUMS.sha256"), str(checksums)]
                        )
                        require(
                            fingerprint(checksums)[0] == accepted["sha256"],
                            "accepted_repository_checksum",
                        )
                        try:
                            inventory = read_checksums(checksums)
                        except (ValueError, TypeError):
                            raise CoordinationError(
                                "accepted_repository_checksum_list_invalid"
                            ) from None
                        raw_name, observations, raw_sha = "raw/github/repositories.jsonl", {}, None
                        if raw_name in inventory:
                            raw_path = folder / "repositories.jsonl"
                            drive._run(
                                ["copyto", drive.path(prefix + "/" + raw_name), str(raw_path)]
                            )
                            require(
                                fingerprint(raw_path) == inventory[raw_name], "github_raw_checksum"
                            )
                            raw_sha = inventory[raw_name][0]
                            with raw_path.open("rb") as stream:
                                while line := stream.readline(16 * 1024**2 + 1):
                                    require(len(line) <= 16 * 1024**2, "raw_line_size_limit")
                                    try:
                                        raw = validate_record(line)
                                    except ValueError:
                                        raise CoordinationError(
                                            "invalid_github_repository_record"
                                        ) from None
                                    require(
                                        isinstance(raw, RawRecord)
                                        and raw.source == "github_rest"
                                        and raw.source_record_type == "github_repository",
                                        "noncanonical_github_evidence",
                                    )
                                    normalized = NormalizationPipeline().process([raw])
                                    require(
                                        len(normalized.canonical_repositories) == 1,
                                        "invalid_github_repository",
                                    )
                                    repository = normalized.canonical_repositories[0]
                                    require(
                                        repository.identity.repo_id == raw.repo_id,
                                        "github_raw_identity_conflict",
                                    )
                                    require(
                                        raw.raw_record_id not in observations
                                        or observations[raw.raw_record_id][0] == raw,
                                        "github_raw_identity_conflict",
                                    )
                                    observations[raw.raw_record_id] = (raw, repository)
                        cache[cache_key] = (
                            accepted["sha256"],
                            list(observations.values()),
                            raw_sha,
                        )
                    sha, observations, raw_sha = cache[cache_key]
                    require(sha == accepted["sha256"], "accepted_repository_checksum")
                    matches = [
                        (raw, repository)
                        for raw, repository in observations
                        if int(parse_repo_id(raw.repo_id)) == package["github_repo_id"]
                    ]
                    if not matches:
                        continue  # Valid activity-only/mismatched legacy run: try older evidence.
                    github, repository = max(
                        matches, key=lambda pair: (pair[0].retrieved_at, pair[0].raw_record_id)
                    )
                    if not repository_exclusion_reasons(repository):
                        return run_id, github, repository, raw_sha
                if len(candidates) < batch_size:
                    raise CoordinationError("accepted_repository_evidence_missing")
                offset += len(candidates)

        for package in pending:
            key = subject(package["package_id"])
            evidence, reason, verified = None, None, False
            try:
                require(
                    package.get("last_screening_run_id") is not None, "screening_evidence_missing"
                )
                screen = uid(str(package["last_screening_run_id"]))
                with registry.connect(readonly=True) as db:
                    screen_artifact = db.execute(
                        "SELECT a.* FROM reposeer.artifacts a JOIN reposeer.crawl_runs c USING(run_id) WHERE a.storage_path=%s AND a.artifact_kind='bundle' AND a.status='ingested' AND c.run_kind='screening' AND c.worker_id='D2'",
                        (drive.path(f"screening/submissions/D2/{screen}/CHECKSUMS.sha256"),),
                    ).fetchone()
                require(screen_artifact is not None, "screening_evidence_missing")
                screen_root = scratch / screen
                if screen not in cache:
                    drive.download(f"screening/submissions/D2/{screen}", screen_root)
                    marker, _, rows, _ = screening_bundle(screen_root)
                    require(
                        marker["payload_sha256"] == screen_artifact["sha256"],
                        "screening_source_checksum",
                    )
                    cache[screen] = rows
                result = next((r for r in cache[screen] if r["package_id"] == key), None)
                require(
                    result and result["screening_status"] == "candidate",
                    "screening_candidate_missing",
                )
                run_id, github, repository, github_sha = repository_evidence(package)
                prefix = f"canonical/runs/{run_id}"
                payload = read_json(screen_root / "pypi_raw" / (key[5:] + ".json"))
                mapping = PackageRepositoryResolver().resolve_current(key[5:], payload, repository)
                reason = mapping.reason
                if mapping.status == "verified_current":
                    # Validation-only wrapper; stored provenance binds original D2 JSON bytes.
                    pypi = RawRecord(
                        schema_version="0.1.0",
                        record_type="raw_record",
                        raw_record_id="raw:" + str(uuid4()),
                        run_id=screen,
                        source="pypi_json",
                        source_record_type="dependency_metadata",
                        source_record_id=key[5:],
                        package_id=key,
                        repo_id=None,
                        source_url=f"https://pypi.org/pypi/{key[5:]}/json",
                        retrieved_at=result["retrieved_at_utc"],
                        available_at=None,
                        event_at=None,
                        http_status=None,
                        raw_body_sha256=None,
                        payload=payload,
                    )
                    resolved = RepositoryScopeEvidence(pypi=pypi, github=github).resolve()
                    require(
                        int(parse_repo_id(resolved["repo_id"])) == package["github_repo_id"],
                        "repository_numeric_identity_changed",
                    )
                    verified = True
                    evidence = dict(
                        screening_run_id=screen,
                        integration_run_id=run_id,
                        pypi_path=f"screening/submissions/D2/{screen}/pypi_raw/{key[5:]}.json",
                        pypi_sha256=result["pypi_response_sha256"],
                        pypi_observed_at=pypi.retrieved_at,
                        github_path=prefix + "/raw/github/repositories.jsonl",
                        github_sha256=github_sha,
                        github_raw_record_id=github.raw_record_id,
                        github_observed_at=github.retrieved_at,
                        historical_validity="unverified",
                    )
                else:
                    reason = reason or "mapping_unresolved"
            except (CoordinationError, ValueError, KeyError, TypeError, OSError) as error:
                reason = (
                    str(error)
                    if isinstance(error, CoordinationError)
                    else "mapping_evidence_invalid"
                )
                verified = False
            if apply:
                with registry.connect() as db:
                    duplicate = db.execute(
                        "SELECT repository_id FROM reposeer.repositories WHERE github_repo_id=%s AND repository_id<>%s",
                        (package["github_repo_id"], package["repository_id"]),
                    ).fetchone()
                    repo = db.execute(
                        "SELECT github_repo_id FROM reposeer.repositories WHERE repository_id=%s FOR UPDATE",
                        (package["repository_id"],),
                    ).fetchone()
                    current = db.execute(
                        "SELECT * FROM reposeer.packages WHERE package_id=%s FOR UPDATE", (key,)
                    ).fetchone()
                    require(
                        current
                        and current["mapping_status"] == "candidate"
                        and current["repository_id"] == package["repository_id"]
                        and str(current.get("last_screening_run_id"))
                        == str(package.get("last_screening_run_id")),
                        "mapping_changed_during_verification",
                    )
                    if duplicate or repo["github_repo_id"] != package["github_repo_id"]:
                        verified, reason = False, "github_numeric_id_collision_quarantine"
                    if current["mapping_status"] == "candidate":
                        db.execute(
                            "UPDATE reposeer.packages SET mapping_status=%s,mapping_evidence=%s,mapping_verified_at=%s WHERE package_id=%s",
                            (
                                "verified_current" if verified else "candidate",
                                Jsonb(evidence if verified else dict(reason_code=reason)),
                                now() if verified else None,
                                key,
                            ),
                        )
            results.append(dict(package_id=key, verified=verified, reason_code=reason))
    return dict(
        dry_run=not apply,
        checked=len(results),
        verified=sum(r["verified"] for r in results),
        results=results,
    )


def requeue_expired(
    *, apply=False, pending_dir=Path("data/coordination"), remote="reposeer:", registry=None
):
    registry = registry or Registry()
    with registry.connect(readonly=True) as db:
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM reposeer.crawl_jobs WHERE status='claimed' AND lease_expires_at<now() ORDER BY job_id"
            ).fetchall()
        ]
    changed, protected, reasons = [], [], {}
    if not jobs:
        return dict(dry_run=not apply, jobs_requeued=changed, protected_jobs=protected)
    pending = [
        validate_assignment(read_json(path), allow_expired=True)
        for pattern in ("ASSIGNMENT.json", ".pending/*.json")
        for path in Path(pending_dir).rglob(pattern)
    ]
    drive = Drive(remote)
    drive.check()
    remote_assignments = {}
    for job in jobs:
        identity = uid(str(job["job_id"]))
        worker = worker_id(
            job["claimed_by"], "screening" if job["job_kind"] == "screen_package" else "crawler"
        )
        token = uid(str(job["claim_token"]))
        if job["job_kind"] == "screen_package":
            if job["screen_assignment_id"] is None:
                protected.append(identity)
                reasons[identity] = "screen_assignment_missing_manual_review"
                continue
            batch = uid(str(job["screen_assignment_id"]))
            prefix = f"screening/submissions/D2/{batch}"
        else:
            prefix = f"crawl/submissions/{worker}/{identity}"
        if prefix not in remote_assignments:
            listing = drive.listing(prefix)
            candidates, unknown = [], False
            if job["job_kind"] == "screen_package":
                folders = {"": listing} if listing else {}
            else:
                folders = {}
                for name, size in listing.items():
                    parts = name.split("/", 1)
                    if len(parts) != 2:
                        unknown = True
                        continue
                    uid(parts[0])
                    folders.setdefault(parts[0] + "/", {})[parts[1]] = size
            for folder, entries in folders.items():
                if "ASSIGNMENT.json" not in entries:
                    unknown = True
                    continue
                value = validate_assignment(
                    drive.get_json(
                        prefix + "/" + folder + "ASSIGNMENT.json",
                        byte_size=entries["ASSIGNMENT.json"],
                    ),
                    allow_expired=True,
                )
                if (
                    value["worker_id"] != worker
                    or value["kind"] != job["job_kind"]
                    or (
                        value.get("assignment_id") != batch
                        if job["job_kind"] == "screen_package"
                        else value.get("job_id") != identity
                    )
                ):
                    unknown = True
                else:
                    candidates.append(value)
            remote_assignments[prefix] = candidates, unknown
        candidates, unknown = remote_assignments[prefix]
        matches = [
            value
            for value in [*candidates, *pending]
            if any(
                item["job_id"] == identity and item["claim_token"] == token
                for item in value.get("jobs", [value])
            )
        ]
        unknown = unknown or any(
            value["worker_id"] != worker or value["assignment_sha256"] != job["assignment_sha256"]
            for value in matches
        )
        # A matching token protects even if its worker/checksum is inconsistent: that
        # inconsistency requires inspection, not a conclusion that no upload exists.
        if unknown or matches:
            protected.append(identity)
            reasons[identity] = (
                "submission_identity_unknown" if unknown else "current_claim_pending"
            )
        else:
            changed.append(identity)
    if apply:
        reclaimable, changed = set(changed), []
        # All local/remote checks have completed. No Drive call is made under these locks.
        with registry.connect() as db:
            for snapshot in jobs:
                identity = str(snapshot["job_id"])
                if identity not in reclaimable:
                    continue
                current = db.execute(
                    "SELECT * FROM reposeer.crawl_jobs WHERE job_id=%s FOR UPDATE SKIP LOCKED",
                    (identity,),
                ).fetchone()
                fields = (
                    "job_kind",
                    "claimed_by",
                    "claim_token",
                    "screen_assignment_id",
                    "lease_expires_at",
                    "assignment_sha256",
                    "assignment_targets",
                )
                if (
                    not current
                    or current["status"] != "claimed"
                    or current["lease_expires_at"] >= now()
                    or any(current[field] != snapshot[field] for field in fields)
                ):
                    protected.append(identity)
                    reasons[identity] = "claim_changed_or_locked"
                    continue
                finish_job(db, current, "lease_expired_no_submission")
                db.execute(
                    "UPDATE reposeer.crawl_jobs SET claimed_by=NULL,claim_token=NULL,claimed_at=NULL,lease_expires_at=NULL,screen_assignment_id=NULL,updated_at=now() WHERE job_id=%s",
                    (identity,),
                )
                changed.append(identity)
    return dict(
        dry_run=not apply,
        jobs_requeued=changed,
        protected_jobs=protected,
        protection_reasons=reasons,
    )


def status(registry):
    with registry.connect(readonly=True) as db:
        packages = db.execute(
            "SELECT mapping_status,count(*) AS count FROM reposeer.packages GROUP BY mapping_status"
        ).fetchall()
        repos = db.execute(
            "SELECT verification_status,count(*) AS count FROM reposeer.repositories GROUP BY verification_status"
        ).fetchall()
        coverage = db.execute(
            "SELECT activity_coverage_status,count(*) AS count FROM reposeer.repositories GROUP BY activity_coverage_status"
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
        activity_coverage=coverage,
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
    sub = commands.add_parser("screen-drive", help="Screen a mounted D1 run and allocate C1-C7")
    sub.add_argument("--run-dir", type=Path, required=True)
    sub.add_argument("--data-root", type=Path)
    sub.add_argument("--dry-run", action="store_true")
    sub.add_argument("--no-progress", action="store_true", help="Suppress D2 progress on stderr")
    sub = commands.add_parser(
        "bootstrap-mapping", help="Initialize the cumulative mapping from verified history"
    )
    sub.add_argument("--drive-root", type=Path, required=True)
    sub.add_argument("--dry-run", action="store_true")
    sub = commands.add_parser("external-report", help="Write verified external coverage without changing master mapping")
    sub.add_argument("--drive-root", type=Path, required=True)
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
    sub = commands.add_parser("verify-pending")
    sub.add_argument("--limit", type=int, default=100)
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    sub = commands.add_parser("requeue-expired")
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    sub.add_argument("--pending-dir", type=Path, default=Path("data/coordination"))
    for name, sub in commands.choices.items():
        if name not in {"screen-drive", "bootstrap-mapping", "external-report"}:
            sub.add_argument("--remote", default=argparse.SUPPRESS)
    args = cli.parse_args()
    if args.command == "screen-drive":
        return screen_drive(
            args.run_dir,
            data_root=args.data_root,
            dry_run=args.dry_run,
            no_progress=args.no_progress,
        )
    if args.command == "bootstrap-mapping":
        return bootstrap_mapping(args.drive_root, dry_run=args.dry_run)
    if args.command == "external-report":
        from tools.coordination.external import external_report

        return external_report(args.drive_root)
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
    if args.command == "verify-pending":
        return verify_pending(
            registry=registry, remote=args.remote, limit=args.limit, apply=args.apply
        )
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
