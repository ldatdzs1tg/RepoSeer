"""Strict structure, chronology and canonical model invariants."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from reposeer.contracts import ContractModel, validate_record
from reposeer.schemas.github.repository import RepositoryRecord

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "path", sorted((ROOT / "examples/contracts").glob("*.json")), ids=lambda p: p.stem
)
def test_every_union_alternative_and_serialization(path):
    data = path.read_bytes()
    record = validate_record(data)
    assert isinstance(record, ContractModel)
    assert validate_record(data.decode("utf-8")) == record
    assert validate_record(json.loads(data)) == record
    assert validate_record(record) == record
    assert all(field.is_required() for field in type(record).model_fields.values())


@pytest.mark.parametrize(
    "path", sorted((ROOT / "tests/fixtures/contracts/invalid").glob("*.json")), ids=lambda p: p.stem
)
def test_invalid_fixture_files(path):
    with pytest.raises(ValidationError):
        validate_record(path.read_bytes())


def test_missing_nullable_extra_keys_and_legacy_rejected(example):
    data = example("github_raw_record")
    del data["http_status"]
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("github_repository")
    data["repoId"] = "github:repository:1"
    with pytest.raises(ValidationError):
        validate_record(data)
    with pytest.raises(ValidationError):
        validate_record(RepositoryRecord(owner="a", name="b", full_name="a/b"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("stargazers_count", "0"),
        ("stargazers_count", 0.0),
        ("stargazers_count", False),
        ("forks_count", -1),
        ("forks_count", 9007199254740992),
        ("is_archived", "false"),
        ("is_fork", 0),
        ("stargazers_count", float("nan")),
        ("stargazers_count", float("inf")),
    ],
)
def test_repository_types_are_strict(example, field, value):
    data = example("github_repository")
    data[field] = value
    with pytest.raises(ValidationError):
        validate_record(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("github_repo_id", 900000001),
        ("github_repo_id", "01"),
        ("github_repo_id", "900000001.0"),
        ("github_repo_id", "900000002"),
        ("repo_url", "https://github.com/Synthetic/Demo"),
        ("repo_url", "https://github.com/synthetic/demo?utm_source=x"),
        ("repo_url", "https://github.com/synthetic/demo.git"),
    ],
)
def test_repository_identity_is_consistent(example, field, value):
    data = example("github_repository")
    data["identity"][field] = value
    with pytest.raises(ValidationError):
        validate_record(data)


@pytest.mark.parametrize("field", ["valid_at", "available_at"])
def test_provenance_temporal_rules(example, field):
    data = example("github_repository")
    data["provenance"][field] = "2027-01-01T00:00:00.000Z"
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("github_repository")
    data["provenance"]["valid_at"] = "2023-01-01T00:00:00.000Z"
    with pytest.raises(ValidationError):
        validate_record(data)


@pytest.mark.parametrize("field", ["event_at", "available_at"])
def test_raw_future_times_fail(example, field):
    data = example("github_raw_record")
    data[field] = "2027-01-01T00:00:00.000Z"
    with pytest.raises(ValidationError):
        validate_record(data)


def test_observation_zero_is_distinct_from_unknown(example):
    data = example("github_repository")
    data["forks_count"] = 0
    assert validate_record(data).forks_count == 0
    data["forks_count"] = None
    assert validate_record(data).forks_count is None
    data = example("package_release")
    assert validate_record(data).dependencies is None
    data["dependencies"] = []
    assert validate_record(data).dependencies == []


@pytest.mark.parametrize(
    "fixture,field",
    [
        ("github_repository", "updated_at"),
        ("github_repository", "pushed_at"),
        ("package_release", "uploaded_at"),
        ("repository_activity", "event_at"),
        ("article", "updated_at"),
        ("article", "content_available_at"),
    ],
)
def test_source_dates_never_exceed_retrieval(example, fixture, field):
    data = example(fixture)
    data[field] = "2027-01-01T00:00:00.000Z"
    with pytest.raises(ValidationError):
        validate_record(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("article_id", "article:sha256:" + "0" * 64),
        ("canonical_url", "https://EXAMPLE.test/News/Python?edition=1"),
        ("canonical_url", "https://example.test/News/Python?edition=1&utm_source=x"),
        ("content_sha256", "0" * 64),
        ("content_text", " Python synthetic announcement."),
        ("published_date", "2024-02-30"),
        ("published_precision", "unknown"),
        ("content_status", "unavailable"),
    ],
)
def test_article_hash_url_precision_and_content(example, field, value):
    data = example("article")
    data[field] = value
    with pytest.raises(ValidationError):
        validate_record(data)


def test_technology_lifecycle_needs_evidence_and_unique_aliases(example):
    data = example("technology_entity")
    data["lifecycle_status"] = "end_of_life"
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("technology_entity")
    data["aliases"] = ["Python", "python"]
    with pytest.raises(ValidationError):
        validate_record(data)


def test_yanked_state_needs_observation_and_activity_identity(example):
    data = example("package_release")
    data["is_yanked"] = False
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("repository_activity")
    data["activity_id"] = "github:activity:1:commit:abc123"
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("repository_activity")
    data["activity_kind"] = "comment"
    with pytest.raises(ValidationError):
        validate_record(data)


@pytest.mark.parametrize("field", ["raw_record_ids", "missing_fields"])
def test_duplicate_provenance_entries_fail(example, field):
    data = example("github_repository")
    data["provenance"][field] *= 2
    with pytest.raises(ValidationError):
        validate_record(data)


def test_snapshot_requires_exact_missing_paths_and_ordered_windows(example):
    data = example("package_snapshot")
    data["missing_fields"].pop()
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("package_snapshot")
    data["missing_fields"].append(data["missing_fields"][0])
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("package_snapshot")
    data["features"].update(release_count_3m=3, release_count_6m=2, release_count_12m=1)
    with pytest.raises(ValidationError):
        validate_record(data)
    data = example("package_snapshot")
    data["horizon_end_at"] = "2025-03-30T23:59:59.999Z"
    with pytest.raises(ValidationError):
        validate_record(data)


def test_existing_model_instances_are_revalidated(example):
    record = validate_record(example("github_repository"))
    record.stargazers_count = "unknown"
    with pytest.raises(ValidationError):
        validate_record(record)


@pytest.mark.parametrize("fixture", ["package_snapshot", "snapshot_build_request"])
@pytest.mark.parametrize("value", [12.0, "12", True, 13])
def test_horizon_literal_is_a_strict_integer(example, fixture, value):
    data = example(fixture)
    data["horizon_months"] = value
    with pytest.raises(ValidationError):
        validate_record(data)
