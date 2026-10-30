"""File integrity, mounted-Drive helpers and legacy coordinator-only Registry access."""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType, SimpleNamespace
from uuid import UUID

from reposeer.contracts.identity import canonical_github_url, format_utc_timestamp, package_id
from reposeer.storage import screening_mapping as master_mapping
from reposeer.storage.screening_mapping import MAPPING_FIELDS as MAPPING_FIELDS

COLLECTION_BUDGETS = MappingProxyType(
    {"initial": (1, 30), "followup:1": (2, 100), "followup:2": (4, 300)}
)


def collection_options(scope):
    require(scope in COLLECTION_BUDGETS, "unsupported_scope")
    pages, records = COLLECTION_BUDGETS[scope]
    return dict(
        max_pages=pages,
        max_records=records,
        discovery_sources=["hackernews"],
        discovery_max_pages=1,
        discovery_max_records=1,
        max_candidates=1,
    )


# Compatibility for callers using the initial policy; validation derives fresh options.
COLLECTION = dict(
    max_pages=1,
    max_records=30,
    discovery_sources=["hackernews"],
    discovery_max_pages=1,
    discovery_max_records=1,
    max_candidates=1,
)
# Immutable definitions; callers receive fresh option dictionaries.
COLLECTION_POLICIES = MappingProxyType({
    "legacy-initial": MappingProxyType({**COLLECTION, "discovery_sources": ("hackernews",)}),
    "distributed-v2": MappingProxyType(dict(
        max_pages=4, max_records=300,
        discovery_sources=("hackernews", "devto", "stackexchange", "rss", "news"),
        discovery_max_pages=3, discovery_max_records=150, max_candidates=20)),
})


def policy_options(policy_id):
    require(isinstance(policy_id, str) and policy_id in COLLECTION_POLICIES, "invalid_collection_policy")
    return {key: list(value) if isinstance(value, tuple) else value
            for key, value in COLLECTION_POLICIES[policy_id].items()}


def screening_policy(marker):
    if "collection_policy" not in marker:
        return "legacy-initial"
    policy = marker["collection_policy"]
    require(isinstance(policy, dict) and set(policy) == {"policy_id"}, "invalid_collection_policy")
    policy_options(policy["policy_id"])
    return policy["policy_id"]


MAX_FILE = 2 * 1024**3
MAX_BUNDLE = 20 * 1024**3
MAX_FILES = 10000
MAX_METADATA = 8 * 1024**2


class CoordinationError(ValueError):
    """Safe machine-readable failure code, without provider bodies or credentials."""

    def __init__(self, code, *, stage=None, exit_code=None):
        super().__init__(code)
        self.diagnostics = {}
        if stage is not None:
            self.diagnostics["rclone_stage"] = stage
        if exit_code is not None:
            self.diagnostics["rclone_exit_code"] = exit_code


def require(condition, code):
    if not condition:
        raise CoordinationError(code)


def now():
    return datetime.now(UTC)


def timestamp(value):
    require(isinstance(value, str), "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CoordinationError("invalid_timestamp") from None
    require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "invalid_timestamp")
    return parsed


def uid(value):
    require(isinstance(value, str), "invalid_uuid")
    try:
        require(str(UUID(value)) == value, "noncanonical_uuid")
    except ValueError:
        raise CoordinationError("invalid_uuid") from None
    return value


def worker_id(value, role=None):
    require(isinstance(value, str) and re.fullmatch(r"D[12]|C[1-7]", value), "invalid_worker")
    expected = "discovery" if value == "D1" else "screening" if value == "D2" else "crawler"
    require(role is None or role == expected, "worker_role_mismatch")
    return value


def subject(value):
    require(isinstance(value, str) and value.startswith("pypi:"), "invalid_package_id")
    require(package_id(value[5:]) == value, "noncanonical_package_id")
    return value


def slug(value):
    return canonical_github_url(value).removeprefix("https://github.com/")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(path):
    result, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            require(size <= MAX_FILE, "file_size_limit")
            result.update(block)
    return result.hexdigest(), size


def relative(value):
    require(isinstance(value, str) and value and len(value) <= 512, "unsafe_path")
    require(
        not any(ord(c) < 32 for c in value) and not any(c in value for c in "\\:"), "unsafe_path"
    )
    parts = value.split("/")
    require(
        all(p and p not in {".", ".."} and not p.endswith((" ", ".")) for p in parts), "unsafe_path"
    )
    require(not any(p.casefold() in {".env", ".env.local", ".git"} for p in parts), "secret_path")
    require(not PurePosixPath(value).is_absolute(), "unsafe_path")
    return value


def safe_path(root, name):
    path = Path(root) / relative(name)
    root = Path(root).absolute()
    for parent in (path, *path.parents):
        require(not parent.is_symlink() and not parent.is_junction(), "symlink_rejected")
        if parent == root:
            break
    require(path.resolve().is_relative_to(root.resolve()), "path_outside_root")
    return path


def atomic_bytes(path, data, *, immutable=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        require(fingerprint(path) == (digest(data), len(data)), "local_path_checksum_conflict")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value, *, immutable=True):
    atomic_bytes(path, canonical(value), immutable=immutable)


def read_json(path):
    require(not Path(path).is_symlink() and not Path(path).is_junction(), "symlink_rejected")
    require(Path(path).stat().st_size <= MAX_METADATA, "json_size_limit")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), "expected_json_object")
    return value


def sign_assignment(value):
    value = dict(value)
    value.pop("assignment_sha256", None)
    return dict(value, assignment_sha256=digest(canonical(value)))


def validate_assignment(value, *, expected_worker=None, allow_expired=False):
    require(
        type(value.get("schema_version")) is int and value["schema_version"] == 1,
        "assignment_version",
    )
    require(
        value.get("assignment_sha256") == sign_assignment(value)["assignment_sha256"],
        "assignment_checksum",
    )
    kind, worker = value.get("kind"), value.get("worker_id")
    require(kind in {"screen_package", "collect_repository"}, "unknown_assignment_kind")
    worker_id(worker, "screening" if kind == "screen_package" else "crawler")
    require(expected_worker is None or worker == expected_worker, "assignment_worker_mismatch")
    expiry = timestamp(value.get("lease_expires_at"))
    require(allow_expired or expiry > now(), "assignment_expired")
    if kind == "screen_package":
        uid(value.get("assignment_id"))
        jobs = value.get("jobs")
        require(
            isinstance(jobs, list)
            and 1 <= len(jobs) <= 100
            and all(isinstance(j, dict) for j in jobs),
            "invalid_screen_jobs",
        )
        require(len({uid(j.get("job_id")) for j in jobs}) == len(jobs), "duplicate_job_ids")
        require(
            len({subject(j.get("package_id")) for j in jobs}) == len(jobs), "duplicate_package_ids"
        )
        for job in jobs:
            uid(job.get("claim_token"))
    elif kind == "collect_repository":
        uid(value.get("job_id"))
        uid(value.get("claim_token"))
        options = collection_options(value.get("scope_key"))
        require(
            slug(value.get("expected_repo_full_name")) == value["expected_repo_full_name"],
            "noncanonical_repo",
        )
        targets = value.get("targets")
        require(isinstance(targets, list) and 1 <= len(targets) <= 100, "invalid_targets")
        require(len({subject(p) for p in targets}) == len(targets), "duplicate_targets")
        require(
            canonical(value.get("collection")) == canonical(options),
            "unsupported_collection_options",
        )
    else:
        raise CoordinationError("unknown_assignment_kind")
    return value


def desktop_metadata(name):
    return PurePosixPath(name).name.casefold() == "desktop.ini"


def files(root, *, declared=()):
    found = {}
    for path in sorted(Path(root).rglob("*")):
        require(not path.is_symlink() and not path.is_junction(), "symlink_rejected")
        if path.is_file():
            name = relative(path.relative_to(root).as_posix())
            safe_path(root, name)
            # Only undeclared OS metadata is dispensable. Validate paths/links first.
            if not desktop_metadata(name) or name in declared:
                found[name] = fingerprint(path)
    require(
        len(found) <= MAX_FILES and sum(size for _, size in found.values()) <= MAX_BUNDLE,
        "bundle_size_limit",
    )
    return found


def seal_bundle(root, assignment, run_id=None):
    checksums = safe_path(root, "CHECKSUMS.sha256")
    declared = read_checksums(checksums) if checksums.exists() else ()
    inventory = files(root, declared=declared)
    lines = "".join(
        f"{sha} {size} {name}\n"
        for name, (sha, size) in inventory.items()
        if name not in {"CHECKSUMS.sha256", "READY.json"}
    )
    atomic_bytes(Path(root) / "CHECKSUMS.sha256", lines.encode("utf-8"))
    marker = dict(
        schema_version=1,
        worker_id=assignment["worker_id"],
        payload_path="CHECKSUMS.sha256",
        payload_sha256=digest(lines.encode("utf-8")),
        created_at_utc=format_utc_timestamp(now()),
    )
    key = "job_id" if assignment["kind"] == "collect_repository" else "assignment_id"
    marker[key] = assignment[key]
    if run_id:
        marker["run_id"] = uid(run_id)
    if (Path(root) / "READY.json").exists():
        previous = read_json(Path(root) / "READY.json")
        require(
            all(previous.get(k) == v for k, v in marker.items() if k != "created_at_utc"),
            "ready_conflict",
        )
    else:
        write_json(Path(root) / "READY.json", marker)
    return read_json(Path(root) / "READY.json")


def read_checksums(path):
    require(Path(path).stat().st_size <= MAX_METADATA, "checksum_list_size_limit")
    declared = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        sha, size, name = line.split(" ", 2)
        relative(name)
        require(re.fullmatch("[a-f0-9]{64}", sha) and size.isdecimal(), "invalid_checksum_entry")
        require(
            name not in declared and name not in {"READY.json", "CHECKSUMS.sha256"},
            "duplicate_checksum_entry",
        )
        require(int(size) <= MAX_FILE, "file_size_limit")
        declared[name] = (sha, int(size))
        require(len(declared) <= MAX_FILES, "bundle_size_limit")
    require(sum(size for _, size in declared.values()) <= MAX_BUNDLE, "bundle_size_limit")
    return declared


def validate_ready(marker):
    require(
        type(marker.get("schema_version")) is int
        and marker["schema_version"] == 1
        and marker.get("payload_path") == "CHECKSUMS.sha256",
        "invalid_ready",
    )
    worker = worker_id(marker.get("worker_id"))
    require(worker != "D1", "invalid_ready_worker")
    timestamp(marker.get("created_at_utc"))
    require(
        isinstance(marker.get("payload_sha256"), str)
        and re.fullmatch("[a-f0-9]{64}", marker["payload_sha256"]),
        "invalid_ready_checksum",
    )
    uid(marker.get("assignment_id") if worker == "D2" else marker.get("job_id"))
    if worker != "D2":
        uid(marker.get("run_id"))
    return marker


def verify_bundle(root):
    marker = validate_ready(read_json(safe_path(root, "READY.json")))
    declared = read_checksums(safe_path(root, "CHECKSUMS.sha256"))
    require(
        fingerprint(safe_path(root, "CHECKSUMS.sha256"))[0] == marker.get("payload_sha256"),
        "checksum_list_mismatch",
    )
    inventory = files(root, declared=declared)
    actual = {k: v for k, v in inventory.items() if k not in {"READY.json", "CHECKSUMS.sha256"}}
    require(declared == actual, "bundle_checksum_mismatch")
    assignment = validate_assignment(read_json(Path(root) / "ASSIGNMENT.json"), allow_expired=True)
    key = "job_id" if assignment["kind"] == "collect_repository" else "assignment_id"
    require(
        marker.get(key) == assignment[key] and marker["worker_id"] == assignment["worker_id"],
        "ready_assignment_mismatch",
    )
    return marker, assignment, inventory


def no_worker_dsn():
    require("REPOSEER_REGISTRY_DSN" not in os.environ, "worker_must_not_have_registry_dsn")


def redact(text):
    from reposeer.config import Settings

    configuration = Settings.local()
    for name in ("github_token", "devto_api_key", "stackexchange_api_key", "news_api_key"):
        value = getattr(configuration, name)
        if value:
            text = text.replace(value, "[REDACTED]")
    for key, value in os.environ.items():
        if (
            key != "REPOSEER_REGISTRY_DSN"
            and re.search("TOKEN|KEY|PASSWORD|SECRET", key, re.I)
            and len(value) >= 4
        ):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", text)
    text = re.sub(
        r"(?i)(Bearer\s+|(?:api[_-]?key|access_token|password|token)[=:]\s*)[^\s&]+",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"(?i)(postgres(?:ql)?://)[^\s]+", r"\1[REDACTED]", text)


def cli_guard(function):
    try:
        result = function()
        if result is not None:
            print(json.dumps(result, default=str, sort_keys=True))
    except (
        CoordinationError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        # Never print exception bodies from subprocesses, DB, HTTP, or untrusted files.
        code = (
            str(error)
            if isinstance(error, (CoordinationError, master_mapping.ScreeningMappingError))
            else type(error).__name__
        )
        details = error.diagnostics if isinstance(error, CoordinationError) else {}
        print(json.dumps({"error_code": code, **details}), file=sys.stderr)
        raise SystemExit(1) from None


def parser(description):
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--remote", default=os.environ.get("REPOSEER_DRIVE_REMOTE", "reposeer:"))
    return result


# Mounted-Drive CSV contracts are independent of legacy Registry assignments.
DRIVE_WORKERS = tuple(f"C{number}" for number in range(1, 8))
ASSIGNMENT_FIELDS = (
    "sampling_run_id",
    "worker_id",
    "package_id",
    "package_name",
    "repo_full_name",
)


def checked_mount_path(path: Path) -> Path:
    try:
        return master_mapping.checked_mount_path(path)
    except master_mapping.ScreeningMappingError as error:
        raise CoordinationError(str(error)) from None


def read_cumulative_mapping(root: Path, *, allow_pending: bool = False):
    try:
        return master_mapping.read_cumulative_mapping(root, allow_pending=allow_pending)
    except master_mapping.ScreeningMappingError as error:
        raise CoordinationError(str(error)) from None


def mounted_display_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        return "\\" * 2 + value[8:]
    return value.removeprefix("\\\\?\\")


def local_drive_workspace(kind: str, override: Path | None, drive_root: Path) -> Path:
    base = (
        Path(override)
        if override is not None
        else (
            Path("C:/RepoSeerData") / kind
            if sys.platform == "win32"
            else Path.home() / ".local/share/reposeer" / kind
        )
    )
    base = checked_mount_path(base.absolute())
    require(not base.resolve().is_relative_to(drive_root.resolve()), "workspace_must_be_local")
    return base


def copy_verified(source: Path, destination: Path, *, replace: bool = False) -> None:
    """Single-writer mounted-file publication, without preallocation or cloud clients."""
    source, destination = checked_mount_path(source), checked_mount_path(destination)
    expected = fingerprint(source)
    if destination.exists():
        if fingerprint(destination) == expected:
            return
        require(replace, "mounted_path_checksum_conflict")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, suffix=".tmp", delete=False
        ) as out:
            temporary = Path(out.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        require(fingerprint(temporary) == expected, "mounted_copy_checksum_mismatch")
        require(fingerprint(source) == expected, "source_changed_during_copy")
        checked_mount_path(destination)
        if destination.exists() and not replace:
            require(fingerprint(destination) == expected, "mounted_path_checksum_conflict")
            return
        os.replace(temporary, destination)
        require(fingerprint(destination) == expected, "mounted_readback_checksum_mismatch")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_drive_csv(path: Path, fields: tuple[str, ...]) -> list[dict[str, str]]:
    path = checked_mount_path(path)
    require(path.stat().st_size <= MAX_METADATA, "csv_size_limit")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        require(tuple(reader.fieldnames or ()) == fields, "drive_csv_schema_mismatch")
        rows = []
        for row in reader:
            require(
                set(row) == set(fields) and all(isinstance(v, str) for v in row.values()),
                "drive_csv_row_mismatch",
            )
            rows.append(row)
            require(len(rows) <= 10000, "drive_csv_row_limit")
    return rows


def screening_candidate(evidence: dict) -> tuple[str, str, str]:
    from reposeer.pipelines.collection import _repository_candidate

    repo, status, reason = _repository_candidate(
        SimpleNamespace(package_id=evidence["package_id"], payload={"info": evidence["info"]})
    )
    return (slug(repo).casefold(), "candidate", "") if repo else ("", status, reason or "")


def allocate_drive_assignments(mapping: list[dict]) -> dict[str, list[dict]]:
    groups = {}
    for row in mapping:
        if row["screening_status"] == "candidate":
            groups.setdefault(row["repo_full_name"], []).append(row)
    assigned = {worker: [] for worker in DRIVE_WORKERS}
    repositories = dict.fromkeys(DRIVE_WORKERS, 0)
    for repo in sorted(groups, key=lambda name: (-len(groups[name]), name)):
        worker = min(
            DRIVE_WORKERS, key=lambda name: (len(assigned[name]), repositories[name], name)
        )
        for row in sorted(groups[repo], key=lambda item: item["package_id"]):
            assigned[worker].append(
                dict(
                    sampling_run_id=row["sampling_run_id"],
                    worker_id=worker,
                    package_id=row["package_id"],
                    package_name=row["package_name"],
                    repo_full_name=repo,
                )
            )
        repositories[worker] += 1
    return assigned


def verify_drive_screening(
    root: Path,
    run_id: str,
    *,
    marker: dict | None = None,
) -> tuple[dict, list[dict], dict[str, list[dict]]]:
    """Read every published CSV/evidence file; READY alone never establishes sync completion."""
    from reposeer.pipelines.package_discovery import CSV_FIELDS

    uid(run_id)
    root = checked_mount_path(root)
    base = safe_path(root, f"screening/runs/{run_id}")
    marker = read_json(safe_path(base, "READY.json")) if marker is None else marker
    require(
        type(marker.get("schema_version")) is int
        and marker["schema_version"] == 1
        and marker.get("sampling_run_id") == run_id,
        "drive_screening_ready_mismatch",
    )
    screening_policy(marker)
    timestamp(marker["generated_at_utc"])
    require(
        set(files(base)) <= {"mapping.csv", "pypi_evidence.jsonl", "READY.json"},
        "unexpected_screening_files",
    )
    require(
        type(marker.get("package_count")) is int
        and type(marker.get("assigned_package_count")) is int,
        "screening_count_mismatch",
    )
    require(set(marker["assignments_sha256"]) == set(DRIVE_WORKERS), "assignment_workers_mismatch")
    for name, field in (
        ("mapping.csv", "mapping_sha256"),
        ("pypi_evidence.jsonl", "evidence_sha256"),
    ):
        require(
            fingerprint(safe_path(base, name))[0] == marker[field],
            "drive_screening_checksum_mismatch",
        )
    origin = safe_path(root, f"discovery/submissions/{run_id}")
    for name, field in (
        ("manifest.json", "discovery_manifest_sha256"),
        ("package_candidates.csv", "discovery_csv_sha256"),
    ):
        require(
            fingerprint(safe_path(origin, name))[0] == marker[field], "discovery_source_changed"
        )
    source = read_drive_csv(safe_path(origin, "package_candidates.csv"), CSV_FIELDS)
    mapping = read_drive_csv(safe_path(base, "mapping.csv"), MAPPING_FIELDS)
    source_ids = [row["package_id"] for row in source]
    skipped = marker.get("skipped_existing_package_ids", [])
    require(isinstance(skipped, list), "invalid_skipped_existing_package_ids")
    skipped_ids = {subject(key) for key in skipped}
    require(
        len(skipped_ids) == len(skipped)
        and skipped == [key for key in source_ids if key in skipped_ids],
        "skipped_existing_package_ids_mismatch",
    )
    require(
        source_ids
        and [key for key in source_ids if key not in skipped_ids]
        == [row["package_id"] for row in mapping]
        and len(mapping) == marker["package_count"],
        "mapping_package_mismatch",
    )
    evidence = {}
    with safe_path(base, "pypi_evidence.jsonl").open("rb") as stream:
        while line := stream.readline(MAX_METADATA + 1):
            require(len(line) <= MAX_METADATA, "evidence_line_size_limit")
            item = json.loads(line)
            key = subject(item["package_id"])
            require(key not in evidence and len(evidence) < 10000, "duplicate_package_evidence")
            timestamp(item["retrieved_at_utc"])
            require(
                item["source_url"] == f"https://pypi.org/pypi/{key[5:]}/json",
                "evidence_source_mismatch",
            )
            evidence[key] = item
    seen, successful = set(), set()
    for row in mapping:
        key = subject(row["package_id"])
        require(
            key not in seen and row["package_name"] == key[5:] and row["sampling_run_id"] == run_id,
            "mapping_identity_mismatch",
        )
        seen.add(key)
        timestamp(row["retrieved_at_utc"])
        require(
            row["source_url"] == f"https://pypi.org/pypi/{key[5:]}/json", "mapping_source_mismatch"
        )
        require(
            row["screening_status"] in {"candidate", "unresolved", "ambiguous", "failed"},
            "mapping_status_mismatch",
        )
        if row["screening_status"] == "failed":
            require(
                not row["repo_full_name"]
                and not row["evidence_sha256"]
                and row["reason_code"]
                in {"pypi_request_failed", "rate_limited", "pypi_response_invalid"},
                "invalid_screening_failure",
            )
            continue
        item = evidence.get(key)
        require(
            item is not None
            and digest(canonical(item)) == row["evidence_sha256"]
            and item["retrieved_at_utc"] == row["retrieved_at_utc"],
            "mapping_evidence_mismatch",
        )
        require(
            screening_candidate(item)
            == (row["repo_full_name"], row["screening_status"], row["reason_code"]),
            "mapping_decision_mismatch",
        )
        successful.add(key)
    require(successful == set(evidence), "unexpected_package_evidence")
    assigned = {}
    for worker in DRIVE_WORKERS:
        path = safe_path(root, f"assignments/{worker}/{run_id}.csv")
        require(
            fingerprint(path)[0] == marker["assignments_sha256"][worker],
            "drive_assignment_checksum_mismatch",
        )
        assigned[worker] = read_drive_csv(path, ASSIGNMENT_FIELDS)
    require(assigned == allocate_drive_assignments(mapping), "drive_assignment_allocation_mismatch")
    require(
        sum(map(len, assigned.values())) == marker["assigned_package_count"],
        "assigned_package_count_mismatch",
    )
    return marker, mapping, assigned


class Drive:
    """Single-writer rclone transfers; compare SHA-256 by read-back, not Drive MD5."""

    def __init__(self, remote="reposeer:"):
        require(
            isinstance(remote, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+:(?:[A-Za-z0-9_/-]+)?", remote),
            "invalid_remote",
        )
        if remote.split(":", 1)[1]:
            relative(remote.split(":", 1)[1].rstrip("/"))
        self.remote = remote.rstrip("/")

    def path(self, name):
        relative(name)
        return self.remote + ("" if self.remote.endswith(":") else "/") + name

    def _run(self, args, *, missing_ok=False):
        stage = {
            "version": "version",
            "lsd": "availability",
            "lsjson": "listing",
            "mkdir": "directory_creation",
            "deletefile": "notification_removal",
        }.get(args[0], "operation")
        if args[0] == "copyto":
            stage = (
                "read_back"
                if args[1].startswith(self.remote)
                else "immutable_upload"
                if "--immutable" in args
                else "upload"
            )
        # A caller's filters must not hide undeclared files or declared payloads.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not re.fullmatch(
                r"RCLONE_(?:(?:EXCLUDE|INCLUDE|FILTER|FILES_FROM0?)(?:_.*)?|"
                r"MIN_SIZE|MAX_SIZE|MIN_AGE|MAX_AGE|MAX_DEPTH|HASH_FILTER)",
                key.upper(),
            )
        }
        try:
            result = subprocess.run(
                [
                    "rclone",
                    *args,
                    "--local-no-preallocate",
                    "--retries",
                    "2",
                    "--low-level-retries",
                    "2",
                    "--contimeout",
                    "15s",
                    "--timeout",
                    "120s",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                env=environment,
            )
        except FileNotFoundError:
            raise CoordinationError("rclone_not_installed", stage=stage) from None
        except subprocess.TimeoutExpired:
            raise CoordinationError("rclone_operation_timeout", stage=stage) from None
        except OSError:
            raise CoordinationError("rclone_operation_unavailable", stage=stage) from None
        # Only listing's documented directory-not-found code means absence.
        if missing_ok and args[0] == "lsjson" and result.returncode == 3:
            return None
        if result.returncode != 0:
            raise CoordinationError(
                "rclone_operation_failed", stage=stage, exit_code=result.returncode
            )
        return result.stdout

    def check(self):
        self._run(["version"])
        self._run(["lsd", self.remote])

    def listing(self, prefix, *, include_metadata=False):
        value = self._run(
            ["lsjson", self.path(prefix), "--recursive", "--files-only"], missing_ok=True
        )
        if value is None:
            return {}
        rows = json.loads(value)
        require(isinstance(rows, list) and len(rows) <= 200000, "remote_listing_limit")
        result, seen = {}, set()
        for row in rows:
            name = relative(row["Path"])
            require(not row.get("IsLink") and not row.get("IsDir"), "remote_symlink_rejected")
            require(type(row["Size"]) is int and 0 <= row["Size"] <= MAX_FILE, "remote_size_limit")
            require(name not in seen, "duplicate_remote_path")
            seen.add(name)
            if include_metadata or not desktop_metadata(name):
                result[name] = row["Size"]
        return result

    def children(self, prefix):
        value = self._run(["lsjson", self.path(prefix), "--dirs-only"], missing_ok=True)
        if value is None:
            return []
        rows = json.loads(value)
        require(isinstance(rows, list) and len(rows) <= 200000, "remote_listing_limit")
        names = [relative(row["Path"]) for row in rows]
        require(all("/" not in name for name in names), "invalid_remote_directory")
        return sorted(names)

    def download(self, prefix, destination):
        listing = self.listing(prefix, include_metadata=True)
        require(
            listing and len(listing) <= MAX_FILES and sum(listing.values()) <= MAX_BUNDLE,
            "remote_bundle_limit",
        )

        def download_file(name, size):
            target = safe_path(destination, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                self._run(["copyto", self.path(prefix + "/" + name), str(target)])
            require(target.is_file() and target.stat().st_size == size, "download_size_mismatch")

        declared = {}
        if "CHECKSUMS.sha256" in listing:
            download_file("CHECKSUMS.sha256", listing["CHECKSUMS.sha256"])
            declared = read_checksums(safe_path(destination, "CHECKSUMS.sha256"))
        retained = {}
        for name, size in listing.items():
            safe_path(destination, name)  # Reject links even for ignored OS metadata.
            if desktop_metadata(name) and name not in declared:
                continue
            download_file(name, size)
            retained[name] = size
        return retained

    def get_json(self, name, *, byte_size=None):
        if byte_size is None:
            parent, leaf = name.rsplit("/", 1)
            byte_size = self.listing(parent).get(leaf)
        require(type(byte_size) is int and 0 < byte_size <= MAX_METADATA, "json_size_limit")
        with tempfile.TemporaryDirectory(prefix="reposeer-remote-json-") as scratch:
            path = Path(scratch) / "metadata.json"
            self._run(["copyto", self.path(name), str(path)])
            return read_json(path)

    def remote_fingerprint(self, name):
        with tempfile.TemporaryDirectory(prefix="reposeer-drive-verify-") as scratch:
            target = Path(scratch) / "artifact"
            self._run(["copyto", self.path(name), str(target)])
            return fingerprint(target)

    def archive(self, source, destination):
        """Copy/verify then remove only the notification, never its bundle."""
        with tempfile.TemporaryDirectory(prefix="reposeer-notification-") as scratch:
            local = Path(scratch) / "notification.json"
            self._run(["copyto", self.path(source), str(local)])
            self.put(local, destination)
            require(self.remote_fingerprint(source) == fingerprint(local), "notification_changed")
            self._run(["deletefile", self.path(source)])

    def put(self, local, name, *, replace=False):
        local = Path(local)
        expected = fingerprint(local)
        parent, leaf = name.rsplit("/", 1)
        existing = self.listing(parent)
        if leaf in existing and not replace:
            if self.remote_fingerprint(name) != expected:
                raise CoordinationError(
                    "remote_path_checksum_conflict", stage="checksum_verification"
                )
            return
        self._run(["mkdir", self.path(parent)])
        # A destination appearing after listing must never be overwritten, even if
        # size/mtime happen to match. Read-back remains the authority on equality.
        self._run(
            [
                "copyto",
                str(local),
                self.path(name),
                *([] if replace else ["--immutable", "--ignore-existing"]),
            ]
        )
        if self.remote_fingerprint(name) != expected:
            raise CoordinationError("upload_checksum_mismatch", stage="checksum_verification")

    def upload(self, root, prefix):
        _, _, inventory = verify_bundle(root)
        existing = {
            name: size
            for name, size in self.listing(prefix, include_metadata=True).items()
            if not desktop_metadata(name) or name in inventory
        }
        require(set(existing) <= set(inventory), "unexpected_remote_bundle_files")
        # A READY bundle is already immutable; compare all bytes before accepting replay.
        for name in existing:
            require(
                self.remote_fingerprint(prefix + "/" + name) == inventory[name],
                "remote_path_checksum_conflict",
            )
        for name in sorted(inventory, key=lambda value: (value == "READY.json", value)):
            if name not in existing:
                self.put(safe_path(root, name), prefix + "/" + name)


SCHEMA = {
    "workers": "worker_id worker_role display_name is_enabled last_seen_at created_at",
    "packages": "package_id first_sampling_run_id source_snapshot_sha256 mapping_status candidate_repo_full_name repository_id last_screened_at created_at last_screening_run_id mapping_evidence mapping_verified_at",
    "repositories": "repository_id repo_full_name github_repo_id verification_status verified_at last_collected_at created_at activity_coverage_status activity_coverage_json last_coverage_at",
    "crawl_jobs": "job_id job_kind package_id repository_id scope_key priority status claimed_by claim_token claimed_at lease_expires_at retry_after attempt_count max_attempts last_error_code created_at updated_at assignment_targets assignment_sha256 screen_assignment_id",
    "crawl_runs": "run_id worker_id job_id run_kind status started_at finished_at error_code catalog_metadata",
    "artifacts": "artifact_id run_id artifact_kind storage_path sha256 byte_size status submitted_at ingested_at error_code",
}


class Registry:
    """Only coordinator commands instantiate this object. No implicit migrations."""

    @contextmanager
    def writer_lock(self):
        import psycopg
        from psycopg.rows import dict_row

        dsn = os.environ.get("REPOSEER_REGISTRY_DSN")
        require(bool(dsn), "registry_dsn_missing_coordinator_only")
        try:
            # Autocommit session: no transaction survives a Drive operation.
            with psycopg.connect(
                dsn, autocommit=True, row_factory=dict_row, connect_timeout=15
            ) as db:
                key = ("reposeer:canonical-writer",)
                acquired = db.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", key
                ).fetchone()
                require(acquired and acquired["acquired"], "another_ingest_writer_is_active")
                try:
                    yield
                finally:
                    db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", key)
        except psycopg.Error:
            raise CoordinationError("registry_operation_failed") from None

    @contextmanager
    def connect(self, *, readonly=False):
        import psycopg
        from psycopg.rows import dict_row

        dsn = os.environ.get("REPOSEER_REGISTRY_DSN")
        require(bool(dsn), "registry_dsn_missing_coordinator_only")
        try:
            with psycopg.connect(
                dsn, autocommit=True, row_factory=dict_row, connect_timeout=15
            ) as db:
                rows = db.execute(
                    "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=%s",
                    ("reposeer",),
                ).fetchall()
                actual = {(r["table_name"], r["column_name"]) for r in rows}
                require(
                    all(
                        (table, col) in actual
                        for table, cols in SCHEMA.items()
                        for col in cols.split()
                    ),
                    "registry_schema_mismatch_apply_migrations",
                )
                protected = db.execute(
                    "SELECT relname,relrowsecurity FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname=%s AND relkind='r'",
                    ("reposeer",),
                ).fetchall()
                require(
                    all(
                        any(r["relname"] == name and r["relrowsecurity"] for r in protected)
                        for name in SCHEMA
                    ),
                    "registry_rls_missing",
                )
                indexes = db.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname=%s", ("reposeer",)
                ).fetchall()
                require(
                    {
                        "repositories_name_ci_uq",
                        "crawl_jobs_package_scope_uq",
                        "crawl_jobs_repository_scope_uq",
                    }
                    <= {r["indexname"] for r in indexes},
                    "registry_unique_indexes_missing",
                )
                with db.transaction():
                    if readonly:
                        db.execute("SET TRANSACTION READ ONLY")
                    yield db
        except psycopg.Error:
            raise CoordinationError("registry_operation_failed") from None


def enabled_worker(db, name, role):
    worker_id(name, role)
    row = db.execute("SELECT * FROM reposeer.workers WHERE worker_id=%s", (name,)).fetchone()
    require(
        row and row["worker_role"] == role and row["is_enabled"], "worker_disabled_or_role_mismatch"
    )


def check_claim(row, assignment, item=None, *, replay=False):
    item = item or assignment
    require(row is not None, "unknown_job")
    require(
        row["status"] == "claimed" or replay and row["status"] in {"completed", "retry", "failed"},
        "job_not_claimed",
    )
    require(
        row["claimed_by"] == assignment["worker_id"]
        and str(row["claim_token"]) == item["claim_token"],
        "stale_claim",
    )
    require(
        row["assignment_sha256"] == assignment["assignment_sha256"],
        "assignment_db_checksum_mismatch",
    )
    expected_targets = (
        [item["package_id"]] if assignment["kind"] == "screen_package" else assignment["targets"]
    )
    require(
        row["assignment_targets"] == expected_targets and row["job_kind"] == assignment["kind"],
        "assignment_db_targets_mismatch",
    )
    require(str(row["job_id"]) == item["job_id"], "assignment_db_job_mismatch")
    if assignment["kind"] == "screen_package":
        require(row["package_id"] == item["package_id"], "assignment_db_package_mismatch")
    if row["status"] == "claimed":
        require(row["lease_expires_at"] > now(), "claim_lease_expired")
    require(
        timestamp(assignment["lease_expires_at"]) == row["lease_expires_at"],
        "assignment_db_lease_mismatch",
    )
    if assignment["kind"] == "collect_repository":
        require(
            row["scope_key"] == assignment["scope_key"]
            and slug(row["repo_full_name"]) == assignment["expected_repo_full_name"],
            "assignment_db_repo_mismatch",
        )


def finish_job(db, job, error=None):
    state = (
        "completed"
        if error is None
        else "retry"
        if job["attempt_count"] < job["max_attempts"]
        else "failed"
    )
    retry = (
        now() + timedelta(seconds=min(3600, 60 * 2 ** min(job["attempt_count"], 6)))
        if state == "retry"
        else None
    )
    # Retain the last claim identity for idempotent receipt replay; reclaim clears it.
    db.execute(
        "UPDATE reposeer.crawl_jobs SET status=%s,retry_after=%s,last_error_code=%s,updated_at=now() WHERE job_id=%s",
        (state, retry, error, job["job_id"]),
    )
    return state


def artifact_row(db, run_id, kind, path, sha, size):
    previous = db.execute(
        "SELECT run_id,artifact_kind,sha256,byte_size,status FROM reposeer.artifacts WHERE storage_path=%s FOR UPDATE",
        (path,),
    ).fetchone()
    if previous:
        require(
            str(previous["run_id"]) == run_id
            and previous["sha256"] == sha
            and previous["artifact_kind"] == kind
            and previous["byte_size"] == size
            and previous["status"] == "ingested",
            "artifact_path_checksum_conflict",
        )
        return False
    created = db.execute(
        "INSERT INTO reposeer.artifacts(run_id,artifact_kind,storage_path,sha256,byte_size,status,ingested_at) VALUES (%s,%s,%s,%s,%s,'ingested',now()) ON CONFLICT(storage_path) DO NOTHING RETURNING artifact_id",
        (run_id, kind, path, sha, size),
    ).fetchone()
    if not created:
        previous = db.execute(
            "SELECT run_id,artifact_kind,sha256,byte_size,status FROM reposeer.artifacts WHERE storage_path=%s",
            (path,),
        ).fetchone()
        require(
            previous
            and str(previous["run_id"]) == run_id
            and previous["sha256"] == sha
            and previous["artifact_kind"] == kind
            and previous["byte_size"] == size
            and previous["status"] == "ingested",
            "artifact_path_checksum_conflict",
        )
    return bool(created)


def notification(assignment, marker):
    worker = worker_id(assignment["worker_id"], "crawler")
    job, run = uid(assignment["job_id"]), uid(marker["run_id"])
    return dict(
        schema_version=1,
        worker_id=worker,
        job_id=job,
        integration_run_id=run,
        submission_prefix=f"crawl/submissions/{worker}/{job}/{run}",
        ready_payload_sha256=marker["payload_sha256"],
        created_at_utc=marker["created_at_utc"],
    )


def validate_notification(value, path):
    worker = worker_id(value.get("worker_id"), "crawler")
    job, run = uid(value.get("job_id")), uid(value.get("integration_run_id"))
    require(
        type(value.get("schema_version")) is int and value["schema_version"] == 1,
        "notification_version",
    )
    require(
        path == f"crawl/inbox/{worker}/{job}/{run}.json"
        and value.get("submission_prefix") == f"crawl/submissions/{worker}/{job}/{run}",
        "notification_path_mismatch",
    )
    require(
        isinstance(value.get("ready_payload_sha256"), str)
        and re.fullmatch("[a-f0-9]{64}", value["ready_payload_sha256"]),
        "notification_checksum",
    )
    timestamp(value.get("created_at_utc"))
    return value


def activity_coverage(manifest, expected_repo):
    """Bounded observations only; neither complete groups nor dates prove full history."""
    expected_groups = {"commits", "issues", "pull_requests", "contributors", "releases"}
    entries = [e for t in manifest["targets"] for e in t.get("activity_group_statuses", [])]
    entries += [
        e
        for stage in manifest.get("stages", [])
        for e in stage.get("details", {}).get("activity_groups", [])
    ]
    groups = {}
    for entry in entries:
        if slug(entry.get("repo_full_name", expected_repo)) != expected_repo:
            continue
        group = entry.get("group")
        require(group in expected_groups, "invalid_activity_group")
        summary = {
            key: entry.get(key)
            for key in ("group", "status", "missing_reason", "pages_fetched", "records_stored")
        }
        require(isinstance(summary["status"], str), "invalid_activity_status")
        require(
            summary["missing_reason"] is None or isinstance(summary["missing_reason"], str),
            "invalid_activity_reason",
        )
        for key in ("pages_fetched", "records_stored"):
            require(
                summary[key] is None or type(summary[key]) is int and summary[key] >= 0,
                "invalid_activity_counter",
            )
        require(group not in groups or groups[group] == summary, "conflicting_activity_coverage")
        groups[group] = summary
    reasons = {g["missing_reason"] for g in groups.values() if g["missing_reason"]}
    if "budget_exhausted" in reasons:
        status = "budget_limited"
    elif any(g["status"] in {"failed", "retryable_failure", "excluded"} for g in groups.values()):
        status = "collection_failed"
    elif set(groups) == expected_groups and all(g["status"] == "complete" for g in groups.values()):
        status = "bounded_no_budget_exhaustion"
    else:
        status = "unknown"
        reasons.add("activity_coverage_unverified")
    return dict(
        status=status, groups=[groups[k] for k in sorted(groups)], reason_codes=sorted(reasons)
    )


def integration_evidence(root, assignment, run_id):
    """Recompute receipt facts from T-020 manifests AND canonical provider observations."""
    from reposeer.collectors.github.repository import repository_exclusion_reasons
    from reposeer.collectors.package.repository_mapping import PackageRepositoryResolver
    from reposeer.contracts import RawRecord, validate_record
    from reposeer.contracts.identity import parse_repo_id
    from reposeer.pipelines.normalization import NormalizationPipeline

    run_id = uid(run_id)
    manifest_name = f"metadata/collection_runs/integration_{run_id}.json"
    manifest = read_json(safe_path(root, manifest_name))
    require(
        manifest.get("integration_run_id") == run_id
        and manifest.get("manifest_version") == "1"
        and manifest.get("mode") == "live"
        and manifest.get("status") in {"complete", "partial", "failed"},
        "invalid_integration_manifest",
    )
    require(
        timestamp(manifest["finished_at"]) >= timestamp(manifest["started_at"]),
        "integration_time_order",
    )
    require(
        manifest["requested_targets"] == assignment["targets"],
        "manifest_assignment_targets_mismatch",
    )
    targets = manifest["targets"]
    require(
        len(targets) == len(assignment["targets"])
        and [t["package_id"] for t in targets] == assignment["targets"],
        "manifest_target_identity_mismatch",
    )
    wanted = {t.get("repository_raw_record_id") for t in targets}
    wanted.update(i for t in targets for i in t.get("mapping_evidence_raw_ids", []))
    observations = {}
    for name in ("raw/github/repositories.jsonl", "raw/pypi/packages.jsonl"):
        path = safe_path(root, name)
        if not path.exists():
            continue
        with path.open("rb") as stream:
            while line := stream.readline(16 * 1024**2 + 1):
                require(len(line) <= 16 * 1024**2, "raw_line_size_limit")
                raw = validate_record(line)
                require(isinstance(raw, RawRecord), "noncanonical_raw_evidence")
                if raw.raw_record_id in wanted:
                    previous = observations.get(raw.raw_record_id)
                    require(previous is None or previous == raw, "raw_id_conflict")
                    observations[raw.raw_record_id] = raw
    profile_name = relative(manifest["reports"]["collection_profile"])
    data_profile_name = relative(manifest["reports"]["data_profile"])
    require(
        profile_name == f"reports/data_quality/collection_profile_{run_id}.json"
        and data_profile_name == f"reports/data_quality/data_profile_{run_id}.json",
        "profile_path_mismatch",
    )
    profile = read_json(safe_path(root, profile_name))
    quality = read_json(safe_path(root, data_profile_name))
    require(
        profile.get("integration_run_id") == run_id and quality.get("integration_run_id") == run_id,
        "profile_run_mismatch",
    )
    require(
        profile.get("overall_status") == manifest["status"]
        and profile.get("targets") == targets
        and profile.get("sources") == manifest["sources"],
        "profile_manifest_mismatch",
    )
    verified, numeric_ids = [], set()
    for target in targets:
        subject(target["package_id"])
        if target.get("mapping_status") != "verified_current":
            continue
        require(
            slug(target["repo_full_name"]) == assignment["expected_repo_full_name"],
            "unexpected_verified_repository",
        )
        raw = observations.get(target.get("repository_raw_record_id"))
        require(
            raw is not None
            and raw.source == "github_rest"
            and raw.source_record_type == "github_repository",
            "github_raw_missing",
        )
        require(
            raw.raw_record_id in target["mapping_evidence_raw_ids"]
            and raw.repo_id == target["repo_id"],
            "repository_evidence_mismatch",
        )
        require(
            raw.run_id in manifest["child_run_ids"].get("github_metadata", []),
            "repository_run_not_in_manifest",
        )
        normalized = NormalizationPipeline().process([raw])
        require(len(normalized.canonical_repositories) == 1, "invalid_github_repository")
        repository = normalized.canonical_repositories[0]
        require(not repository_exclusion_reasons(repository), "ineligible_repository_evidence")
        require(
            slug(repository.identity.repo_full_name) == assignment["expected_repo_full_name"],
            "repository_slug_mismatch",
        )
        pypi = [
            observations[i]
            for i in target["mapping_evidence_raw_ids"]
            if i in observations
            and observations[i].source == "pypi_json"
            and observations[i].package_id == target["package_id"]
        ]
        require(
            len(pypi) == 1
            and pypi[0].source_url == f"https://pypi.org/pypi/{target['package_id'][5:]}/json",
            "pypi_mapping_evidence_missing",
        )
        mapping = PackageRepositoryResolver().resolve_current(
            target["package_id"][5:], pypi[0].payload, repository
        )
        require(
            mapping.status == "verified_current" and mapping.repo_id == target["repo_id"],
            "raw_mapping_not_verified",
        )
        require(
            any(
                s["source"] == "github_metadata"
                and type(s.get("records_stored")) is int
                and s["records_stored"] > 0
                and raw.raw_record_id in s.get("raw_record_ids", [])
                for s in manifest["sources"]
            ),
            "github_not_newly_stored",
        )
        numeric_ids.add(int(parse_repo_id(target["repo_id"])))
        verified.append(target["package_id"])
    require(len(numeric_ids) <= 1, "multiple_github_identities")
    numeric = next(iter(numeric_ids), None)
    collected = bool(
        manifest["status"] in {"complete", "partial"}
        and verified
        and numeric is not None
        and type(profile["totals"]["repositories_collected"]) is int
        and profile["totals"]["repositories_collected"] >= 1
    )
    errors = sorted(
        {str(e["code"]) for e in manifest["errors"]}
        | {str(r) for t in targets for r in t.get("missing_reasons", [])}
    )
    return dict(
        integration_run_id=run_id,
        overall_status=manifest["status"],
        expected_repo_full_name=assignment["expected_repo_full_name"],
        verified_github_repo_id=numeric,
        verified_package_ids=sorted(verified),
        collected_repository=collected,
        started_at=manifest["started_at"],
        finished_at=manifest["finished_at"],
        error_codes=errors,
        activity_coverage=activity_coverage(manifest, assignment["expected_repo_full_name"]),
        manifest_path=manifest_name,
        collection_profile_path=profile_name,
        data_profile_path=data_profile_name,
    )
