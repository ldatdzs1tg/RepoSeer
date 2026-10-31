"""Read-only artifact integrity shared by installed storage and coordination tools."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from uuid import UUID

from reposeer.contracts.identity import canonical_github_url, package_id
from reposeer.storage import screening_mapping as master_mapping

MAX_FILE = 2 * 1024**3
MAX_BUNDLE = 20 * 1024**3
MAX_FILES = 10000
MAX_METADATA = 8 * 1024**2


class ArtifactIntegrityError(ValueError):
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
        raise ArtifactIntegrityError(code)


def timestamp(value):
    require(isinstance(value, str), "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ArtifactIntegrityError("invalid_timestamp") from None
    require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "invalid_timestamp")
    return parsed


def uid(value):
    require(isinstance(value, str), "invalid_uuid")
    try:
        require(str(UUID(value)) == value, "noncanonical_uuid")
    except ValueError:
        raise ArtifactIntegrityError("invalid_uuid") from None
    return value


def subject(value):
    require(isinstance(value, str) and value.startswith("pypi:"), "invalid_package_id")
    require(package_id(value[5:]) == value, "noncanonical_package_id")
    return value


def slug(value):
    return canonical_github_url(value).removeprefix("https://github.com/")


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


def read_json(path):
    require(not Path(path).is_symlink() and not Path(path).is_junction(), "symlink_rejected")
    require(Path(path).stat().st_size <= MAX_METADATA, "json_size_limit")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), "expected_json_object")
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


def checked_mount_path(path: Path) -> Path:
    try:
        return master_mapping.checked_mount_path(path)
    except master_mapping.ScreeningMappingError as error:
        raise ArtifactIntegrityError(str(error)) from None


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

