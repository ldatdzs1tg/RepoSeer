"""All four pair checkers reject valid-but-unrelated results."""

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
