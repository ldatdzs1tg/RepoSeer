"""Split one frozen D09 run and exchange checkpoints through a mounted shared folder."""

import argparse
import json
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from scripts import run_d09_drive as drive
from scripts.collect_d09_pilot import run_lock, stored_source_matches
from scripts.d09_team_store import (
    SyncPending,
    filesystem_path,
    publish,
    ready_bundles,
    ready_manifest,
    unpack,
)
from scripts.verify_d09_evidence import MAX_ARCHIVE_WORKERS
from tools.coordination._common import atomic_bytes

from reposeer.collection.persistence import replace_atomic, write_json_atomic
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.exceptions import HTTPClientError, RateLimitExceededError

PROJECT = Path(__file__).resolve().parents[1]
PENDING = {"download_budget_reached", "archive_download_pending"}


def now() -> str:
    return format_utc_timestamp(datetime.now(UTC))


def json_file(path: Path) -> dict:
    return json.loads(path.read_bytes())


def accepted_plan_ids(plan: dict) -> set[str]:
    """Client updates may reuse data only when the entire frozen assignment is identical."""
    payload = {key: value for key, value in plan.items() if key not in {"plan_id", "created_at"}}
    if drive.digest(drive.canonical_json(payload)) != plan["plan_id"]:
        raise ValueError("team plan checksum differs")
    result = {plan["plan_id"]}
    fields = (
        "version",
        "members",
        "batch_limit",
        "batches_total",
        "targets_sha256",
        "bootstrap_id",
        "batch_hashes",
        "seed_completed",
        "assignments",
    )
    for previous in plan.get("previous_plans", []):
        if any(previous.get(key) != plan.get(key) for key in fields):
            raise ValueError("a client update cannot change the frozen team assignment")
        result.update(accepted_plan_ids(previous))
    return result


def same_plan(metadata: dict, plan: dict) -> bool:
    return metadata.get("plan_id") in accepted_plan_ids(plan)


def install_directory(source: Path, destination: Path, root: Path):
    source, destination, root = (filesystem_path(path) for path in (source, destination, root))
    if any(not path.resolve().is_relative_to(root.resolve()) for path in (source, destination)):
        raise ValueError("checkpoint directory move escapes the local working folder")
    replace_atomic(source, destination)


def load_plan(shared: Path, *, require_ready=True) -> dict:
    plan = json_file(shared / "team-plan.json")
    payload = {key: value for key, value in plan.items() if key not in {"plan_id", "created_at"}}
    if plan["version"] != 1 or drive.digest(drive.canonical_json(payload)) != plan["plan_id"]:
        raise ValueError("team plan checksum differs")
    accepted_plan_ids(plan)
    assigned = [index for values in plan["assignments"].values() for index in values]
    expected = set(range(plan["batch_limit"])) - set(plan["seed_completed"])
    if len(assigned) != len(set(assigned)) or set(assigned) != expected:
        raise ValueError("team assignments overlap or omit a batch")
    if set(plan["assignments"]) != {str(i) for i in range(1, plan["members"] + 1)}:
        raise ValueError("team member roster differs")
    if require_ready and (
        not (shared / "READY.json").exists()
        or json_file(shared / "READY.json").get("plan_id") != plan["plan_id"]
    ):
        raise SyncPending("the team setup is not fully published/synced yet")
    return plan


def frozen_inputs(root: Path) -> tuple[dict, list]:
    frozen = json_file(root / "drive-targets.json")
    registry, _ = drive.historical_registry(None, root, frozen)
    if drive.digest(drive.canonical_json(frozen["targets"])) != frozen["targets_sha256"]:
        raise ValueError("frozen team targets changed")
    groups = [
        [registry[row["package_id"]] for row in group if row["package_id"] in registry]
        for group in drive.target_groups(frozen["targets"])
    ]
    return frozen, list(
        drive.pack_groups([group for group in groups if group], frozen["config"]["batch_size"])
    )


def assignment_plan(
    total: int, completed: list[int], members: int, limit: int
) -> dict[str, list[int]]:
    if not 1 <= members <= 100 or not 1 <= limit <= total:
        raise ValueError(
            "expected 1–100 members and a positive batch limit within the frozen cohort"
        )
    remaining = [index for index in range(limit) if index not in completed]
    return {str(member + 1): remaining[member::members] for member in range(members)}


def runtime_files() -> dict[str, Path]:
    files = {name: PROJECT / name for name in ("pyproject.toml", "uv.lock", "README.md")}
    for folder in ("src", "scripts", "tools/coordination"):
        for path in (PROJECT / folder).rglob("*.py"):
            files[path.relative_to(PROJECT).as_posix()] = path
    return files


def cache_inputs(root: Path, shared: Path, plan: dict, member: int) -> tuple[dict, dict]:
    root = filesystem_path(root)
    seen = set()
    for _, manifest in ready_bundles(shared / "cache"):
        if same_plan(manifest["metadata"], plan):
            seen.update(manifest["metadata"].get("proof_fingerprints", []))
    files, fingerprints = {}, []
    # Batch-local excerpts remain in their batch checkpoints. The global packs
    # retain the broader scans needed by peers without duplicating those subsets.
    directories = [root / "archive-cache", *sorted((root / "archive-cache").glob("team/*/sets/*"))]
    for directory in directories:
        for path in sorted((directory / "archives").glob("*.json")):
            try:
                body = path.read_bytes()
                fingerprint = drive.digest(body)
                if fingerprint in seen:
                    continue
                source = json.loads(body)
                if not source["path"].startswith("archives/") or not stored_source_matches(
                    directory, source
                ):
                    continue
                seen.add(fingerprint)
                prefix = f"sets/{fingerprint}"
                files[f"{prefix}/archives/{path.name}"] = path
                files[f"{prefix}/{source['path']}"] = directory / source["path"]
                fingerprints.append(fingerprint)
            except (OSError, ValueError, KeyError):
                continue
    return files, {
        "kind": "cache",
        "plan_id": plan["plan_id"],
        "member": member,
        "proof_fingerprints": sorted(fingerprints),
    }


def publish_cache(root: Path, shared: Path, plan: dict, member: int) -> int:
    files, metadata = cache_inputs(root, shared, plan, member)
    if files:
        publish(shared / "cache", files, metadata)
    return len(metadata["proof_fingerprints"])


def batch_snapshot(root: Path, index: int, plan: dict, member: int, include_partials=False):
    root = filesystem_path(root)
    batch = root / "batches" / f"batch-{index:05d}"
    if drive.collection_checkpoint(batch) is None:
        return None
    summary_path = batch / "verification/summary.json"
    summary_body = summary_path.read_bytes() if summary_path.exists() else None
    summary = json.loads(summary_body) if summary_body is not None else {"statuses": {}}
    if summary_path.exists() and (
        not stored_source_matches(batch / "verification", summary)
        or summary.get("source_reviews_sha256") != drive.shard_reviews_digest(batch)
    ):
        raise SyncPending(f"batch checkpoint is being updated: {batch.name}")
    files = {}
    for path in batch.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(batch)
        if (
            "reviewed" in relative.parts
            or path.name in {"archive-hints.json", "run.lock"}
            or path.suffix in {".tmp", ".uploading"}
            or (path.suffix == ".part" and not include_partials)
        ):
            continue
        files[f"batches/{batch.name}/{relative.as_posix()}"] = path
    complete = summary_path.exists() and not PENDING.intersection(summary["statuses"])
    return files, {
        "kind": "batch",
        "plan_id": plan["plan_id"],
        "member": member,
        "batch_index": index,
        "complete": complete,
        "verified_rows": summary["statuses"].get("mapping_sources_matched", 0),
        "statuses": summary["statuses"],
        "includes_partials": include_partials,
        "required_sha256": (
            {
                f"batches/{batch.name}/verification/summary.json": drive.digest(summary_body),
                f"batches/{batch.name}/verification/{summary['path']}": summary["content_sha256"],
            }
            if summary_body is not None
            else {}
        ),
    }


def publish_batches(
    root: Path, shared: Path, plan: dict, member: int, indices: list[int], include_partials=False
):
    published = 0
    for index in indices:
        summary = root / "batches" / f"batch-{index:05d}" / "verification/summary.json"
        if summary.exists() and not include_partials:
            signature = drive.file_digest(summary)
            name = f"batches/batch-{index:05d}/verification/summary.json"
            if any(
                same_plan(manifest["metadata"], plan)
                and manifest["metadata"].get("member") in {0, member}
                and manifest["metadata"].get("required_sha256", {}).get(name) == signature
                for _, manifest in ready_bundles(shared / "batches" / f"batch-{index:05d}")
            ):
                published += 1
                continue
        snapshot = batch_snapshot(root, index, plan, member, include_partials)
        if snapshot is not None:
            publish(shared / "batches" / f"batch-{index:05d}", *snapshot)
            published += 1
    return published


def best_batches(shared: Path, plan: dict) -> dict[int, tuple[Path, dict]]:
    result = {}
    for folder in sorted((shared / "batches").glob("batch-*")):
        for path, manifest in ready_bundles(folder):
            metadata = manifest["metadata"]
            index, member = metadata["batch_index"], metadata["member"]
            if not same_plan(metadata, plan):
                continue
            if index not in (
                range(plan["batch_limit"])
                if member == 0
                else plan["assignments"].get(str(member), [])
            ):
                raise ValueError("checkpoint publisher does not own this batch")
            key = (metadata["complete"], metadata["verified_rows"], manifest["published_at"])
            previous = result.get(index)
            if previous is None or key > (
                previous[1]["metadata"]["complete"],
                previous[1]["metadata"]["verified_rows"],
                previous[1]["published_at"],
            ):
                result[index] = path, manifest
    return result


def install_bootstrap(shared: Path, root: Path, plan: dict) -> None:
    directory = shared / "bootstrap" / plan["bootstrap_id"]
    with tempfile.TemporaryDirectory(
        dir=filesystem_path(Path(tempfile.gettempdir())), prefix="d09-restore-"
    ) as temporary:
        stage = Path(temporary) / "files"
        manifest = unpack(directory, stage)
        for name in manifest["files"]:
            source, target = stage / name, root / name
            if not target.resolve().is_relative_to(root.resolve()):
                raise ValueError("bootstrap target escapes the local work directory")
            if target.exists():
                if drive.file_digest(target) != manifest["files"][name]["sha256"]:
                    raise ValueError(f"local frozen input conflicts with the team: {target}")
                continue
            atomic_bytes(target, source.read_bytes())
    frozen, batches = frozen_inputs(root)
    hashes = [
        drive.digest(drive.jsonl(package for group in batch for package in group))
        for batch in batches
    ]
    if frozen["targets_sha256"] != plan["targets_sha256"] or hashes != plan["batch_hashes"]:
        raise ValueError("local batch ordering differs from the shared team plan")


def pull_cache(shared: Path, root: Path, plan: dict) -> int:
    imported = 0
    available = None
    for directory, manifest in ready_bundles(shared / "cache"):
        if not same_plan(manifest["metadata"], plan):
            continue
        destination = root / "archive-cache/team" / manifest["bundle_id"]
        if (destination / "IMPORTED.json").exists():
            continue
        if available is None:
            native = filesystem_path(root)
            directories = [native / "archive-cache", *sorted(native.glob("batches/*/verification"))]
            available = {
                drive.digest(path.read_bytes())
                for folder in directories
                for path in (folder / "archives").glob("*.json")
            }
        if set(manifest["metadata"].get("proof_fingerprints", [])) <= available:
            continue
        if destination.exists():
            raise ValueError(f"unexpected existing cache import: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                dir=filesystem_path(destination.parent), prefix=".import-"
            ) as temporary:
                stage = Path(temporary) / "files"
                unpack(directory, stage)
                write_json_atomic(stage / "IMPORTED.json", {"bundle_id": manifest["bundle_id"]})
                install_directory(stage, destination, root)
        except SyncPending:
            continue
        imported += 1
    return imported


def restore_batches(
    shared: Path, root: Path, plan: dict, indices: list[int], *, complete_only=False
):
    restored = 0
    best = best_batches(shared, plan)
    for index in indices:
        if index not in best:
            continue
        directory, manifest = best[index]
        if complete_only and not manifest["metadata"]["complete"]:
            continue
        destination = root / "batches" / f"batch-{index:05d}"
        if destination.exists():
            # Never replace live or newer local work with an older shared checkpoint.
            continue
        prefix = f"batches/{destination.name}/"
        if any(not name.startswith(prefix) for name in manifest["files"]):
            raise ValueError("team checkpoint contains files outside its assigned batch")
        with tempfile.TemporaryDirectory(
            dir=filesystem_path(root), prefix=".team-restore-"
        ) as temporary:
            stage = Path(temporary) / "files"
            unpack(directory, stage)
            source = stage / "batches" / destination.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            install_directory(source, destination, root)
        restored += 1
    return restored


def write_instructions(shared: Path, plan: dict, *, replace=False):
    script = r"""param(
    [Parameter(Mandatory=$true)][int]$Member,
    [string]$WorkDir,
    [ValidateRange(1,16)][int]$ArchiveWorkers = 4
)
$ErrorActionPreference = 'Stop'
$shared = $PSScriptRoot
$plan = Get-Content -LiteralPath (Join-Path $shared 'team-plan.json') -Raw | ConvertFrom-Json
if ($Member -lt 1 -or $Member -gt $plan.members) { throw 'Choose one assigned team member number.' }
if (-not $WorkDir) { $WorkDir = Join-Path $env:USERPROFILE ('RepoSeer-D09\member-{0:D2}' -f $Member) }
$client = Join-Path $WorkDir ('client-' + $plan.runtime_id.Substring(0,12))
$archive = Join-Path $shared ('client\{0}\payload.zip' -f $plan.runtime_id)
$manifest = Get-Content -LiteralPath (Join-Path (Split-Path $archive) 'manifest.json') -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.payload_sha256) {
    throw 'The client ZIP has not fully synced. Wait for Google Drive and try again.'
}
if (-not (Test-Path -LiteralPath (Join-Path $client 'pyproject.toml'))) {
    Expand-Archive -LiteralPath $archive -DestinationPath $client
}
Push-Location $client
try {
    & uv run --frozen python -m scripts.d09_team run --shared-dir $shared --member $Member --work-dir $WorkDir --archive-workers $ArchiveWorkers
    if ($LASTEXITCODE -ne 0) { throw 'Member run stopped; read its message and rerun to resume.' }
} finally { Pop-Location }
"""
    atomic_bytes(shared / "Start-Member.ps1", script.encode(), immutable=not replace)
    rows = ["member,batches_1_based"] + [
        f'{member},"{" ".join(str(index + 1) for index in indices)}"'
        for member, indices in plan["assignments"].items()
    ]
    atomic_bytes(
        shared / "assignments.csv", ("\n".join(rows) + "\n").encode(), immutable=not replace
    )
    text = (
        "# D09 team collection\n\n"
        f"Frozen scope: first {plan['batch_limit']} of {plan['batches_total']} batches. "
        f"{len(plan['seed_completed'])} completed batches are preserved.\n\n"
        "Give each person a different member number from assignments.csv. Install Python 3.12+ "
        "and uv. Wait for Drive to finish syncing this folder, then run in PowerShell:\n\n"
        "```powershell\n& '<your shared folder>\\Start-Member.ps1' -Member 2\n```\n\n"
        "The supplied client uses its own local environment and local working folder. "
        "Member 1 should stop the original collector once, then use the existing run as "
        "--work-dir to preserve its partial downloads. Do not run two computers under the "
        "same member number at the same time. Checkpoints publish every 120 seconds and "
        "after each pass. Drive sync may take additional time. Complete archive proofs are "
        "shared; unfinished .part files stay local unless sync --include-partials is used.\n\n"
        "Use scripts.d09_team status for global progress, and assemble into a new local "
        "directory to review/prepare the combined completed batches. Sharing collection "
        "does not assign outcome labels. No .env files or credentials are in the client.\n"
    )
    atomic_bytes(shared / "README.md", text.encode(), immutable=not replace)


def initialize(args):
    shared, root = args.shared_dir, args.source_run
    frozen, batches = frozen_inputs(root)
    if not 1 <= args.members <= 100 or args.stop_after_batches < 1:
        raise ValueError("expected 1–100 members and a positive batch limit")
    limit = min(args.stop_after_batches or len(batches), len(batches))
    completed = [
        index
        for index in range(limit)
        if drive.verification_checkpoint(root / "batches" / f"batch-{index:05d}", False) is not None
    ]
    shared.mkdir(parents=True, exist_ok=True)
    if (shared / "team-plan.json").exists():
        plan = load_plan(shared, require_ready=False)
        if (
            plan["targets_sha256"] != frozen["targets_sha256"]
            or plan["members"] != args.members
            or plan["batch_limit"] != limit
        ):
            raise ValueError(
                "existing team plan has different settings; choose a new shared folder"
            )
    else:
        metadata_files = {
            name: root / name
            for name in ("drive-targets.json", "historical-registry.json", "archive-inventory.json")
        }
        for path in (root / "sources").rglob("*"):
            if path.is_file() and path.suffix != ".tmp":
                metadata_files[path.relative_to(root).as_posix()] = path
        for path in args.inventory_dir.rglob("*"):
            if path.is_file() and path.suffix != ".tmp":
                metadata_files[
                    f"team-inventory/{path.relative_to(args.inventory_dir).as_posix()}"
                ] = path
        if (
            drive.file_digest(args.inventory_dir / "inventory.json")
            != json_file(root / "archive-inventory.json")["sha256"]
        ):
            raise ValueError("team inventory differs from the existing run")
        _, bootstrap = publish(shared / "bootstrap", metadata_files, {"kind": "bootstrap"})
        _, runtime = publish(shared / "client", runtime_files(), {"kind": "client"})
        payload = {
            "version": 1,
            "members": args.members,
            "batch_limit": limit,
            "batches_total": len(batches),
            "targets_sha256": frozen["targets_sha256"],
            "bootstrap_id": bootstrap["bundle_id"],
            "runtime_id": runtime["bundle_id"],
            "batch_hashes": [
                drive.digest(drive.jsonl(p for group in batch for p in group)) for batch in batches
            ],
            "seed_completed": completed,
            "assignments": assignment_plan(len(batches), completed, args.members, limit),
        }
        plan = {
            **payload,
            "plan_id": drive.digest(drive.canonical_json(payload)),
            "created_at": now(),
        }
        atomic_bytes(shared / "team-plan.json", drive.canonical_json(plan))
    print(
        f"Preserving {len(plan['seed_completed'])} completed batches; publishing verified evidence.",
        flush=True,
    )
    publish_cache(root, shared, plan, 0)
    available = [
        index
        for index in range(limit)
        if drive.collection_checkpoint(root / "batches" / f"batch-{index:05d}") is not None
    ]
    publish_batches(root, shared, plan, 0, available)
    write_instructions(shared, plan)
    atomic_bytes(shared / "READY.json", drive.canonical_json({"plan_id": plan["plan_id"]}))
    return team_status(shared)


def team_status(shared: Path) -> dict:
    plan = load_plan(shared)
    batches = best_batches(shared, plan)
    complete = {
        index for index, (_, manifest) in batches.items() if manifest["metadata"]["complete"]
    }
    cache_proofs = set()
    for _, manifest in ready_bundles(shared / "cache"):
        if same_plan(manifest["metadata"], plan):
            cache_proofs.update(manifest["metadata"].get("proof_fingerprints", []))
    members = []
    for member, assigned in plan["assignments"].items():
        path = shared / "members" / f"member-{int(member):02d}.json"
        try:
            heartbeat = json_file(path)
        except (FileNotFoundError, json.JSONDecodeError):
            heartbeat = {"state": "not_started"}
        if heartbeat.get("state") == "running":
            updated = datetime.fromisoformat(heartbeat["updated_at"].replace("Z", "+00:00"))
            if datetime.now(UTC) - updated > timedelta(minutes=10):
                heartbeat = {**heartbeat, "state": "heartbeat_stale"}
        members.append(
            {
                "member": int(member),
                "assigned": len(assigned),
                "completed": len(set(assigned) & complete),
                **heartbeat,
            }
        )
    return {
        "shared_dir": str(shared),
        "plan_id": plan["plan_id"],
        "batch_limit": plan["batch_limit"],
        "completed_batches": len(complete),
        "remaining_batches": plan["batch_limit"] - len(complete),
        "shared_archive_proofs": len(cache_proofs),
        "verified_mapping_rows": sum(m["metadata"]["verified_rows"] for _, m in batches.values()),
        "members": members,
    }


class Publisher:
    def __init__(self, args, plan):
        self.args, self.plan = args, plan
        self.stop = threading.Event()
        session = args.work_dir / ".d09-team-session.json"
        if not session.exists():
            write_json_atomic(session, {"instance_id": str(uuid.uuid4())})
        self.instance = json_file(session)["instance_id"]
        self.lock = threading.Lock()
        self.last_error = None

    def sync(self, state="running", include_partials=False):
        with self.lock:
            root, shared, plan = self.args.work_dir, self.args.shared_dir, self.plan
            owner_path = shared / "members" / f"member-{self.args.member:02d}.json"
            if owner_path.exists():
                owner = json_file(owner_path)
                updated = datetime.fromisoformat(owner["updated_at"].replace("Z", "+00:00"))
                if (
                    owner.get("state") == "running"
                    and owner.get("instance_id") != self.instance
                    and datetime.now(UTC) - updated < timedelta(minutes=10)
                ):
                    raise ValueError(
                        "this member number is active on another computer; use a different member number or stop it first"
                    )
            write_json_atomic(
                owner_path,
                {
                    "state": state,
                    "instance_id": self.instance,
                    "updated_at": now(),
                    "last_sync_error": None,
                },
            )
            publish_cache(root, shared, plan, self.args.member)
            publish_batches(
                root,
                shared,
                plan,
                self.args.member,
                plan["assignments"][str(self.args.member)],
                include_partials,
            )
            if include_partials:
                files = {
                    f"archive-cache/{path.name}": path
                    for path in (root / "archive-cache").glob("*.part")
                }
                if files:
                    publish(
                        shared / "partials" / f"member-{self.args.member:02d}",
                        files,
                        {
                            "kind": "partials",
                            "member": self.args.member,
                            "plan_id": plan["plan_id"],
                        },
                    )
            write_json_atomic(
                shared / "members" / f"member-{self.args.member:02d}.json",
                {
                    "state": state,
                    "instance_id": self.instance,
                    "updated_at": now(),
                    "last_sync_error": None,
                },
            )
            self.last_error = None

    def background(self):
        while not self.stop.wait(self.args.sync_seconds):
            try:
                self.sync()
            except (OSError, ValueError) as error:
                self.last_error = str(error)
                print(
                    f"Shared checkpoint publication pending; local work continues: {error}",
                    flush=True,
                )


def prepare_member(args, plan):
    if str(args.member) not in plan["assignments"]:
        raise ValueError("member number is outside the frozen team roster")
    marker = args.work_dir / ".d09-team.json"
    identity = {"plan_id": plan["plan_id"], "member": args.member}
    if marker.exists() and json_file(marker) != identity:
        previous = json_file(marker)
        if previous.get("member") != args.member or previous.get(
            "plan_id"
        ) not in accepted_plan_ids(plan):
            raise ValueError("this local working folder belongs to another team/member")
    runtime = ready_manifest(args.shared_dir / "client" / plan["runtime_id"])
    for name in (
        "scripts/d09_team.py",
        "scripts/run_d09_drive.py",
        "scripts/d09_archive_pool.py",
        "scripts/d09_team_store.py",
    ):
        if (
            name in runtime["files"]
            and drive.file_digest(PROJECT / name) != runtime["files"][name]["sha256"]
        ):
            raise ValueError(
                "client code differs from the frozen team client; use Start-Member.ps1"
            )
    install_bootstrap(args.shared_dir, args.work_dir, plan)
    atomic_bytes(marker, drive.canonical_json(identity), immutable=False)
    pull_cache(args.shared_dir, args.work_dir, plan)
    restore_batches(args.shared_dir, args.work_dir, plan, plan["assignments"][str(args.member)])
    partials = list(ready_bundles(args.shared_dir / "partials" / f"member-{args.member:02d}"))
    if partials:
        directory, manifest = max(partials, key=lambda item: item[1]["published_at"])
        if not same_plan(manifest["metadata"], plan):
            raise ValueError("partial downloads belong to another team plan")
        with tempfile.TemporaryDirectory(
            dir=filesystem_path(args.work_dir), prefix=".team-partials-"
        ) as temporary:
            stage = Path(temporary) / "files"
            unpack(directory, stage)
            for name in manifest["files"]:
                if not name.startswith("archive-cache/") or not name.endswith(".part"):
                    raise ValueError("invalid partial-download checkpoint")
                if not (args.work_dir / name).exists():
                    target = args.work_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    replace_atomic(filesystem_path(stage / name), filesystem_path(target))


def member_run(args):
    plan = load_plan(args.shared_dir)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with run_lock(args.work_dir):
        stop_file = args.work_dir / ".d09-team-stop.json"
        stop_file.unlink(missing_ok=True)
        prepare_member(args, plan)
        publisher = Publisher(args, plan)
        publisher.sync()
        thread = threading.Thread(target=publisher.background, daemon=True)
        thread.start()
        state, previous = "stopped", None
        options = SimpleNamespace(
            output_dir=args.work_dir,
            inventory_dir=args.work_dir / "team-inventory",
            reviews=args.work_dir / "reviews.jsonl",
            archive_dir=None,
            archive_workers=args.archive_workers,
            max_downloads=args.max_downloads,
            max_batches=5,
            batch_size=json_file(args.work_dir / "drive-targets.json")["config"]["batch_size"],
            retry_unverified=False,
            stop_after_batches=plan["batch_limit"],
            batch_indices=plan["assignments"][str(args.member)],
        )
        print(
            f"Member {args.member}: global batch numbers {[i + 1 for i in options.batch_indices]}",
            flush=True,
        )
        try:
            while True:
                if stop_file.exists():
                    break
                if not drive.wait_for_provider(args.work_dir, True):
                    break
                pull_cache(args.shared_dir, args.work_dir, plan)
                frozen = json_file(args.work_dir / "drive-targets.json")
                try:
                    result = drive.run(options, frozen)
                except RateLimitExceededError as error:
                    retry_at = max(
                        time.time() + 30, (error.reset_timestamp or time.time() + 300) + 2
                    )
                    write_json_atomic(
                        args.work_dir / "provider-quota.json",
                        {
                            "status": "waiting_for_public_clickhouse_quota",
                            "retry_at_utc": format_utc_timestamp(
                                datetime.fromtimestamp(retry_at, UTC)
                            ),
                        },
                    )
                    continue
                publisher.sync()
                if result["status"] == "completed":
                    state = "completed"
                    break
                progress = (
                    result["mapping_statuses"],
                    sum(1 for _ in args.work_dir.glob("batches/*/verification/archives/*.json")),
                    sum(1 for _ in (args.work_dir / "archive-cache/archives").glob("*.json")),
                    sum(p.stat().st_size for p in (args.work_dir / "archive-cache").glob("*.part"))
                    + sum(
                        p.stat().st_size
                        for p in args.work_dir.glob("batches/*/verification/*.part")
                    ),
                )
                if progress == previous:
                    break
                previous = progress
        except KeyboardInterrupt:
            print("Stopping this member and publishing its saved checkpoint.", flush=True)
        finally:
            publisher.stop.set()
            thread.join()
            publisher.sync(state)
    return {"member": args.member, "state": state, "shared_dir": str(args.shared_dir)}


def sync_member(args):
    plan = load_plan(args.shared_dir)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with run_lock(args.work_dir):
        prepare_member(args, plan)
        Publisher(args, plan).sync("stopped", args.include_partials)
    return team_status(args.shared_dir)


def request_stop(args):
    marker = args.work_dir / ".d09-team.json"
    if not marker.exists():
        raise ValueError("this directory is not an initialized team member")
    write_json_atomic(args.work_dir / ".d09-team-stop.json", {"requested_at": now()})
    return {
        "state": "stop_requested",
        "work_dir": str(args.work_dir),
        "detail": "The member will publish its checkpoint and stop after its current bounded pass.",
    }


def assemble(args):
    if args.output_dir.exists():
        raise ValueError("assemble into a new directory to preserve previous review snapshots")
    plan = load_plan(args.shared_dir)
    args.output_dir.mkdir(parents=True)
    install_bootstrap(args.shared_dir, args.output_dir, plan)
    pull_cache(args.shared_dir, args.output_dir, plan)
    restored = restore_batches(
        args.shared_dir, args.output_dir, plan, list(range(plan["batch_limit"])), complete_only=True
    )
    return {
        "output_dir": str(args.output_dir),
        "completed_batches_restored": restored,
        "inventory_dir": str(args.output_dir / "team-inventory"),
        "outcome_labels_assigned": 0,
    }


def update_client(args):
    plan = load_plan(args.shared_dir)
    for member in team_status(args.shared_dir)["members"]:
        if member.get("state") == "running":
            raise ValueError("stop active team members before updating the frozen client")
    _, runtime = publish(args.shared_dir / "client", runtime_files(), {"kind": "client"})
    if runtime["bundle_id"] == plan["runtime_id"]:
        return team_status(args.shared_dir)
    payload = {
        key: value
        for key, value in plan.items()
        if key not in {"plan_id", "created_at", "previous_plans"}
    }
    payload.update(runtime_id=runtime["bundle_id"], previous_plans=[plan])
    updated = {
        **payload,
        "plan_id": drive.digest(drive.canonical_json(payload)),
        "created_at": now(),
    }
    accepted_plan_ids(updated)
    atomic_bytes(args.shared_dir / "team-plan.json", drive.canonical_json(updated), immutable=False)
    write_instructions(args.shared_dir, updated, replace=True)
    atomic_bytes(
        args.shared_dir / "READY.json",
        drive.canonical_json({"plan_id": updated["plan_id"]}),
        immutable=False,
    )
    return team_status(args.shared_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Freeze assignments and publish the existing run")
    init.add_argument("--source-run", type=Path, required=True)
    init.add_argument("--inventory-dir", type=Path, default=drive.DEFAULT_INVENTORY)
    init.add_argument("--members", type=int, default=10)
    init.add_argument("--stop-after-batches", type=int, default=100)
    for command in (
        init,
        commands.add_parser("status"),
        commands.add_parser("assemble"),
        commands.add_parser("run"),
        commands.add_parser("sync"),
        commands.add_parser("update-client"),
    ):
        command.add_argument("--shared-dir", type=Path, required=True)
        if command.prog.endswith((" run", " sync")):
            command.add_argument("--member", type=int, required=True)
            command.add_argument("--work-dir", type=Path, required=True)
        if command.prog.endswith(" run"):
            command.add_argument("--archive-workers", type=int, default=4)
            command.add_argument("--max-downloads", type=int, default=200)
            command.add_argument("--sync-seconds", type=int, default=120)
        elif command.prog.endswith(" sync"):
            command.add_argument("--include-partials", action="store_true")
        elif command.prog.endswith(" assemble"):
            command.add_argument("--output-dir", type=Path, required=True)
    stop = commands.add_parser("stop", help="Request a checkpointed stop after the current pass")
    stop.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    if (
        getattr(args, "archive_workers", 4) not in range(1, MAX_ARCHIVE_WORKERS + 1)
        or getattr(args, "max_downloads", 200) < 1
        or getattr(args, "sync_seconds", 120) < 30
    ):
        parser.error(
            "expected 1–16 archive workers, a positive download budget, and sync interval >=30 seconds"
        )
    try:
        action = {
            "init": initialize,
            "status": lambda a: team_status(a.shared_dir),
            "run": member_run,
            "sync": sync_member,
            "assemble": assemble,
            "update-client": update_client,
            "stop": request_stop,
        }[args.command]
        result = action(args)
    except (OSError, ValueError, KeyError, HTTPClientError) as error:
        parser.exit(1, f"D09 team operation stopped; local checkpoints are preserved: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
