"""Read-only cumulative screening contract shared by installed D1 and D2 tools."""

import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from reposeer.contracts.identity import canonical_github_url, package_id

MAPPING_FIELDS = (
    "sampling_run_id",
    "package_id",
    "package_name",
    "repo_full_name",
    "screening_status",
    "reason_code",
    "source_url",
    "retrieved_at_utc",
    "evidence_sha256",
)
# The master outgrows per-run metadata; keep a separate bounded, streaming reader.
MAX_MASTER_BYTES = 512 * 1024**2
MAX_MASTER_LINE = 1024**2
META_FIELDS = {
    "schema_version",
    "mapping_sha256",
    "row_count",
    "updated_at_utc",
    "last_merged_run_id",
}


class ScreeningMappingError(ValueError):
    """Safe error codes for unverified shared mapping data."""


def _require(condition, code):
    if not condition:
        raise ScreeningMappingError(code)


def checked_mount_path(path: Path) -> Path:
    path = Path(path)
    _require(path.is_absolute(), "absolute_mounted_path_required")
    _require(".." not in path.parts, "unsafe_mounted_path")
    prefix = "\\\\?\\"
    if sys.platform == "win32" and not str(path).startswith(prefix):
        value = str(path)
        path = Path(prefix + ("UNC" + "\\" + value[2:] if value.startswith("\\" * 2) else value))
    for ancestor in (path, *path.parents):
        _require(not ancestor.is_symlink() and not ancestor.is_junction(), "symlink_rejected")
    return path


def master_path(root: Path, name: str) -> Path:
    _require(
        name in {"mapping.csv", "mapping.meta.json", "mapping.pending.json"}, "invalid_master_path"
    )
    return checked_mount_path(checked_mount_path(root) / "screening" / name)


def _uuid(value):
    _require(isinstance(value, str), "master_invalid_uuid")
    try:
        _require(str(UUID(value)) == value, "master_invalid_uuid")
    except ValueError:
        raise ScreeningMappingError("master_invalid_uuid") from None


def _utc(value):
    _require(isinstance(value, str), "master_invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ScreeningMappingError("master_invalid_timestamp") from None
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "master_invalid_timestamp"
    )


def validate_master_metadata(meta: dict) -> dict:
    _require(isinstance(meta, dict) and set(meta) == META_FIELDS, "master_metadata_schema_mismatch")
    _require(
        type(meta["schema_version"]) is int and meta["schema_version"] == 1,
        "master_metadata_version",
    )
    _require(
        isinstance(meta["mapping_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", meta["mapping_sha256"]),
        "master_invalid_checksum",
    )
    _require(type(meta["row_count"]) is int and meta["row_count"] >= 0, "master_invalid_row_count")
    _utc(meta["updated_at_utc"])
    if meta["row_count"]:
        _uuid(meta["last_merged_run_id"])
    else:
        _require(meta["last_merged_run_id"] is None, "master_empty_run_mismatch")
    return meta


def validate_mapping_row(row: dict) -> None:
    _require(
        set(row) == set(MAPPING_FIELDS) and all(isinstance(v, str) for v in row.values()),
        "master_csv_row_mismatch",
    )
    key = row["package_id"]
    try:
        valid = key.startswith("pypi:") and package_id(key[5:]) == key
    except ValueError:
        valid = False
    _require(valid, "master_noncanonical_package_id")
    _require(row["package_name"] == key[5:], "master_package_name_mismatch")
    _uuid(row["sampling_run_id"])
    _utc(row["retrieved_at_utc"])
    _require(row["source_url"] == f"https://pypi.org/pypi/{key[5:]}/json", "master_source_mismatch")
    status = row["screening_status"]
    _require(status in {"candidate", "unresolved", "ambiguous", "failed"}, "master_invalid_status")
    if status == "candidate":
        try:
            valid = (
                canonical_github_url(row["repo_full_name"])
                == "https://github.com/" + row["repo_full_name"]
            )
        except ValueError:
            valid = False
        _require(valid, "master_invalid_repository")
    else:
        _require(not row["repo_full_name"], "master_unmapped_repository")
    if status == "failed":
        _require(not row["evidence_sha256"], "master_failed_evidence")
    else:
        _require(
            re.fullmatch(r"[0-9a-f]{64}", row["evidence_sha256"]), "master_invalid_evidence_hash"
        )


def read_master_metadata(root: Path, *, allow_pending: bool = False) -> dict:
    if not allow_pending:
        _require(
            not master_path(root, "mapping.pending.json").exists(), "master_update_pending_retry_D2"
        )
    path = master_path(root, "mapping.meta.json")
    _require(path.is_file(), "master_not_initialized_run_bootstrap_mapping")
    _require(path.stat().st_size <= 16384, "master_metadata_size_limit")
    try:
        meta = validate_master_metadata(json.loads(path.read_text(encoding="utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        raise ScreeningMappingError("master_metadata_invalid_json") from None
    if not allow_pending:
        _require(
            not master_path(root, "mapping.pending.json").exists(), "master_update_pending_retry_D2"
        )
    return meta


def read_mapping_csv(path: Path, meta: dict) -> dict[str, dict]:
    """Validate and hash exact bytes in one streaming pass, without per-run row limits."""
    validate_master_metadata(meta)
    path = checked_mount_path(path)
    _require(path.is_file(), "master_mapping_missing")
    _require(path.stat().st_size <= MAX_MASTER_BYTES, "master_mapping_size_limit")
    sha, size = hashlib.sha256(), 0
    rows, previous = {}, ""
    with path.open("rb") as stream:

        def lines():
            nonlocal size
            while raw := stream.readline(MAX_MASTER_LINE + 1):
                size += len(raw)
                _require(
                    len(raw) <= MAX_MASTER_LINE and size <= MAX_MASTER_BYTES,
                    "master_mapping_size_limit",
                )
                sha.update(raw)
                yield raw.decode("utf-8")

        try:
            reader = csv.DictReader(lines(), strict=True)
            _require(tuple(reader.fieldnames or ()) == MAPPING_FIELDS, "master_csv_schema_mismatch")
            for row in reader:
                validate_mapping_row(row)
                key = row["package_id"]
                _require(key > previous, "master_packages_not_unique_or_sorted")
                rows[key] = row
                previous = key
        except (UnicodeError, csv.Error):
            raise ScreeningMappingError("master_csv_malformed") from None
    _require(sha.hexdigest() == meta["mapping_sha256"], "master_mapping_checksum_mismatch")
    _require(len(rows) == meta["row_count"], "master_mapping_row_count_mismatch")
    return rows


def read_cumulative_mapping(
    root: Path, *, allow_pending: bool = False
) -> tuple[dict, dict[str, dict]]:
    meta = read_master_metadata(root, allow_pending=allow_pending)
    rows = read_mapping_csv(master_path(root, "mapping.csv"), meta)
    _require(
        read_master_metadata(root, allow_pending=allow_pending) == meta,
        "master_changed_retry_sampling",
    )
    return meta, rows


def recheck_master_metadata(root: Path, expected: dict) -> None:
    _require(read_master_metadata(root) == expected, "master_changed_retry_sampling")
