"""Cutoff safety and standalone metadata/status validation."""

import pytest
from pydantic import ValidationError

from reposeer.contracts import validate_record


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "0.3"),
        ("record_type", "unknown"),
        ("raw_record_id", "raw:00000000-0000-1000-8000-000000000010"),
        ("run_id", "00000000-0000-4000-8000-00000000000A"),
        ("source_url", "https://token@example.test/a"),
        ("raw_body_sha256", "not-verifiable"),
    ],
)
def test_invalid_envelope(example, field, value):
    data = example("github_raw_record")
    data[field] = value
    with pytest.raises(ValidationError):
        validate_record(data)


@pytest.mark.parametrize("field", ["authorization", "headers", "token"])
def test_secret_header_fields_cannot_enter_envelope(example, field):
    data = example("github_raw_record")
    data[field] = "secret"
    with pytest.raises(ValidationError):
        validate_record(data)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_availability",
        "postcutoff_availability",
        "postcutoff_validity",
        "postcutoff_release",
        "postcutoff_activity",
        "postcutoff_yanked",
        "package_mismatch",
        "repo_mismatch",
        "duplicate_release",
        "duplicate_activity",
    ],
)
def test_historical_snapshot_inputs_reject_leakage(example, change):
    data = example("snapshot_build_request")
    data["cutoff_at"] = (
        "2023-11-01T00:00:00.000Z"
        if change in {"postcutoff_release", "postcutoff_activity"}
        else "2024-01-15T00:00:00.000Z"
    )
    release = data["releases"][0]
    if change == "unknown_availability":
        release["provenance"]["available_at"] = None
    elif change == "postcutoff_availability":
        release["provenance"]["available_at"] = "2024-02-01T00:00:00.000Z"
    elif change == "postcutoff_validity":
        release["provenance"]["valid_at"] = "2024-02-01T00:00:00.000Z"
    elif change == "postcutoff_yanked":
        release.update(is_yanked=False, yanked_status_at="2024-02-01T00:00:00.000Z")
    elif change == "package_mismatch":
        release["package_id"] = "pypi:other"
    elif change == "repo_mismatch":
        release["repo_id"] = "github:repository:1"
    elif change == "duplicate_release":
        data["releases"] *= 2
    elif change == "duplicate_activity":
        data["activities"] *= 2
    with pytest.raises(ValidationError):
        validate_record(data)


def test_partial_coverage_requires_reason_and_honest_counts(example):
    data = example("collection_result")
    data["metadata"]["coverage"][0]["status"] = "partial"
    data["status"] = "partial"
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("collection_result")
    data["metadata"]["records_emitted"] = 0
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("collection_result")
    data["records"] *= 2
    data["metadata"]["records_emitted"] = 2
    with pytest.raises(ValidationError):
        validate_record(data)
