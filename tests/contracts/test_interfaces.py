"""All four pair checkers reject valid-but-unrelated results."""

from copy import deepcopy

import pytest

from reposeer.contracts import (
    validate_collection_pair,
    validate_normalization_pair,
    validate_record,
    validate_snapshot_pair,
    validate_technology_link_pair,
)
from reposeer.contracts.identity import text_sha256

PAIRS = [
    ("collection", validate_collection_pair),
    ("normalization", validate_normalization_pair),
    ("technology_link", validate_technology_link_pair),
    ("snapshot_build", validate_snapshot_pair),
]


@pytest.mark.parametrize("prefix,checker", PAIRS)
def test_valid_pairs(example, prefix, checker):
    checker(
        validate_record(example(prefix + "_request")), validate_record(example(prefix + "_result"))
    )


@pytest.mark.parametrize("prefix,checker", PAIRS)
@pytest.mark.parametrize("field", ["request_id", "run_id", "schema_version"])
def test_common_pair_mismatches(example, prefix, checker, field):
    request = validate_record(example(prefix + "_request"))
    result = validate_record(example(prefix + "_result"))
    result = result.model_copy(
        update={
            field: "0.3" if field == "schema_version" else "00000000-0000-4000-8000-000000000099"
        }
    )
    with pytest.raises(ValueError):
        checker(request, result)


@pytest.mark.parametrize(
    "change", ["source", "collector", "scope", "kind", "pages", "range", "cutoff"]
)
def test_collection_scope_and_budgets(example, change):
    request = example("collection_request")
    result = example("collection_result")
    if change == "source":
        request["source"] = "github_graphql"
    elif change == "collector":
        request["collector_name"] = "other"
    elif change == "scope":
        request["subjects"][0]["repo_id"] = "github:repository:1"
    elif change == "kind":
        request["record_kinds"] = ["article"]
    elif change == "pages":
        result["metadata"]["pages_fetched"] = 2
    elif change == "range":
        request["time_range"] = {
            "start_at": "2023-01-01T00:00:00.000Z",
            "end_at": "2024-01-01T00:00:00.000Z",
        }
    elif change == "cutoff":
        request["cutoff_at"] = "2023-01-01T00:00:00.000Z"
    with pytest.raises(ValueError):
        validate_collection_pair(validate_record(request), validate_record(result))


@pytest.mark.parametrize(
    "change",
    ["input_count", "orphan", "processed_count", "repo_id", "retrieval", "rejected_without_error"],
)
def test_normalization_counts_and_references(example, change):
    request = example("normalization_request")
    result = example("normalization_result")
    if change == "input_count":
        result.update(
            input_record_count=2,
            rejected_record_count=1,
            status="partial",
            errors=[
                {
                    "code": "validation_error",
                    "message": "Rejected input",
                    "retryable": False,
                    "source_record_id": None,
                    "raw_record_id": "raw:00000000-0000-4000-8000-000000000099",
                }
            ],
        )
    elif change == "orphan":
        result["records"][0]["provenance"]["raw_record_ids"] = [
            "raw:00000000-0000-4000-8000-000000000099"
        ]
    elif change == "processed_count":
        result.update(records=[], output_record_count=0)
    elif change == "repo_id":
        result["records"][0]["identity"].update(repo_id="github:repository:1", github_repo_id="1")
    elif change == "retrieval":
        result["records"][0]["provenance"].update(
            retrieved_at="2025-01-01T00:00:00.000Z", valid_at="2025-01-01T00:00:00.000Z"
        )
    else:
        result.update(
            status="partial",
            records=[],
            processed_record_count=0,
            rejected_record_count=1,
            output_record_count=0,
            errors=[],
        )
    with pytest.raises(ValueError):
        validate_normalization_pair(validate_record(request), validate_record(result))


@pytest.mark.parametrize(
    "change",
    [
        "title",
        "content",
        "provenance",
        "publication",
        "unknown_technology",
        "new_article",
        "incomplete",
        "partial_no_error",
    ],
)
def test_linker_immutability_and_taxonomy(example, change):
    request = example("technology_link_request")
    result = example("technology_link_result")
    article = result["articles"][0]
    if change == "title":
        article["title"] = "Changed title"
    elif change == "content":
        article.update(
            content_text="Changed content", content_sha256=text_sha256("Changed content")
        )
    elif change == "provenance":
        article["provenance"]["available_at"] = None
    elif change == "publication":
        article["published_date"] = "2024-02-02"
    elif change == "unknown_technology":
        article["technology_mentions"][0]["technology_id"] = "tech:unknown"
    elif change == "new_article":
        request["articles"] = []
    elif change == "incomplete":
        result["articles"] = []
    else:
        result["status"] = "partial"
    with pytest.raises(ValueError):
        validate_technology_link_pair(validate_record(request), validate_record(result))


@pytest.mark.parametrize(
    "change", ["package", "repository", "cutoff", "evidence", "coverage", "unknown_to_zero"]
)
def test_snapshot_pair_grain_provenance_and_availability(example, change):
    request = example("snapshot_build_request")
    result = example("snapshot_build_result")
    snapshot = result["snapshot"]
    if change == "package":
        snapshot["package_id"] = "pypi:other"
    elif change == "repository":
        snapshot.update(repo_id="github:repository:1", repo_group_id="github:repository:1")
    elif change == "cutoff":
        snapshot.update(
            cutoff_at="2024-04-30T23:59:59.999Z", horizon_end_at="2025-04-30T23:59:59.999Z"
        )
    elif change == "evidence":
        snapshot["raw_record_ids"] = []
    elif change == "coverage":
        snapshot["coverage"] = []
    else:
        snapshot["features"]["commit_count_3m"] = 0
        snapshot["missing_fields"] = [
            item
            for item in snapshot["missing_fields"]
            if item["field_path"] != "features.commit_count_3m"
        ]
    with pytest.raises(ValueError):
        validate_snapshot_pair(validate_record(request), validate_record(result))


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_snapshot_pair_rejects_future_coverage_even_for_failure(example, status):
    request = example("snapshot_build_request")
    request["coverage"][0]["time_range"] = {
        "start_at": "2023-01-01T00:00:00.000Z",
        "end_at": "2025-01-01T00:00:00.000Z",
    }
    result = example("snapshot_build_result")
    result["status"] = status
    if status == "failed":
        result["snapshot"] = None
        result["errors"] = [
            {
                "code": "insufficient_history",
                "message": "Missing history",
                "retryable": False,
                "source_record_id": None,
                "raw_record_id": None,
            }
        ]
    else:
        result["snapshot"]["coverage"] = request["coverage"]
    with pytest.raises(ValueError):
        validate_snapshot_pair(validate_record(request), validate_record(result))


@pytest.mark.parametrize(
    "change,valid",
    [
        ("name_mismatch", False),
        ("url_mismatch", False),
        ("consistent", True),
        ("case", True),
        ("provider_name", False),
        ("missing_name", False),
        ("invalid_name", False),
        ("nonstring_name", False),
        ("name_only_source_url", False),
        ("url_only_source_url", False),
        ("second_match", True),
        ("cross_subject", False),
    ],
)
def test_collection_repository_identifiers_must_match_one_subject(example, change, valid):
    request = example("collection_request")
    result = example("collection_result")
    subject = request["subjects"][0]
    subject["url"] = "https://github.com/Synthetic/Demo/"
    raw = result["records"][0]
    if change == "name_mismatch":
        subject["repo_full_name"] = "Synthetic/Other"
    elif change == "url_mismatch":
        subject["url"] = "https://github.com/Synthetic/Other"
    elif change == "case":
        subject.update(repo_full_name="synthetic/DEMO", url="https://GitHub.com/SYNTHETIC/demo/")
        raw["payload"]["full_name"] = "SYNTHETIC/Demo"
    elif change in {"provider_name", "name_only_source_url", "url_only_source_url"}:
        raw["payload"]["full_name"] = "Synthetic/Other"
        if change == "name_only_source_url":
            subject.update(repo_id=None, url=None)
        elif change == "url_only_source_url":
            subject.update(repo_id=None, repo_full_name=None)
    elif change == "missing_name":
        raw["payload"].pop("full_name")
    elif change == "invalid_name":
        raw["payload"]["full_name"] = "invalid"
    elif change == "nonstring_name":
        raw["payload"]["full_name"] = 123
    elif change in {"second_match", "cross_subject"}:
        second = deepcopy(subject)
        subject["repo_full_name"] = "Synthetic/Other"
        if change == "cross_subject":
            second["repo_id"] = "github:repository:1"
        request["subjects"].append(second)
    request = validate_record(request)
    result = validate_record(result)
    if valid:
        validate_collection_pair(request, result)
    else:
        with pytest.raises(ValueError, match="outside requested subjects"):
            validate_collection_pair(request, result)


NORMALIZED_PAIRS = [
    ("article_raw_record", "article"),
    ("package_release_raw_record", "package_release"),
    ("repository_activity_raw_record", "repository_activity"),
    ("github_raw_record", "github_repository"),
    ("technology_raw_record", "technology_entity"),
]


def _normalization_data(example, raw_name, record_name):
    request = example("normalization_request")
    request["records"] = [example(raw_name)]
    request["run_id"] = request["records"][0]["run_id"]
    result = example("normalization_result")
    result.update(
        request_id=request["request_id"],
        run_id=request["run_id"],
        records=[example(record_name)],
        input_record_count=1,
        processed_record_count=1,
        rejected_record_count=0,
        output_record_count=1,
        status="complete",
        errors=[],
    )
    return request, result


@pytest.mark.parametrize("raw_name,record_name", NORMALIZED_PAIRS)
@pytest.mark.parametrize("change", ["same", "later", "earlier", "source"])
def test_normalization_preserves_raw_source_and_retrieval(example, raw_name, record_name, change):
    request, result = _normalization_data(example, raw_name, record_name)
    provenance = result["records"][0]["provenance"]
    if change in {"later", "earlier"}:
        timestamp = "2024-03-01T12:00:00.000Z" if change == "later" else "2024-02-28T12:00:00.000Z"
        provenance["retrieved_at"] = timestamp
        if provenance["temporal_basis"] == "observation":
            provenance.update(valid_at=timestamp, available_at=timestamp)
    elif change == "source":
        provenance["source"] = "github_graphql"
    # Both boundaries must be independently valid before exercising the pair invariant.
    request = validate_record(request)
    result = validate_record(result)
    if change == "same":
        validate_normalization_pair(request, result)
    else:
        message = "source mismatch" if change == "source" else "retrieved_at timestamp mismatch"
        with pytest.raises(ValueError, match=message):
            validate_normalization_pair(request, result)


@pytest.mark.parametrize("change", ["same", "later", "source"])
def test_normalization_multi_raw_requires_common_source_and_retrieval(example, change):
    request, result = _normalization_data(example, "article_raw_record", "article")
    extra = deepcopy(request["records"][0])
    extra["raw_record_id"] = "raw:00000000-0000-4000-8000-000000000099"
    if change == "later":
        extra["retrieved_at"] = "2024-03-01T12:00:00.000Z"
    elif change == "source":
        extra["source"] = "github_graphql"
    request["records"].append(extra)
    provenance = result["records"][0]["provenance"]
    provenance["raw_record_ids"].append(extra["raw_record_id"])
    # Even choosing the latest retrieval cannot represent raw inputs from different times.
    provenance.update(
        retrieved_at=extra["retrieved_at"],
        valid_at=extra["retrieved_at"],
        available_at=extra["retrieved_at"],
    )
    result.update(input_record_count=2, processed_record_count=2)
    request = validate_record(request)
    result = validate_record(result)
    if change == "same":
        validate_normalization_pair(request, result)
    else:
        message = "source mismatch" if change == "source" else "retrieved_at timestamp mismatch"
        with pytest.raises(ValueError, match=message):
            validate_normalization_pair(request, result)
