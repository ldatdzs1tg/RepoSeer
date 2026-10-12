"""Synthetic, offline validation of internal T-014 GitHub activity entities."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from reposeer.contracts import RawRecord, validate_record
from reposeer.schemas.github.activity_entities import (
    GitHubCommit,
    GitHubContributorObservation,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRelease,
)

REPO_ID = "github:repository:900000001"
SHA = "a" * 40
PARENT = "b" * 40
CREATED = "2024-01-01T00:00:00.000Z"
LATER = "2024-02-01T00:00:00.000Z"
MAX_COUNT = 9007199254740991

EXAMPLES: dict[type[BaseModel], dict[str, Any]] = {
    GitHubCommit: {
        "repo_id": REPO_ID,
        "commit_sha": SHA,
        "author_github_id": None,
        "author_login": None,
        "committer_github_id": None,
        "committer_login": None,
        "authored_at": None,
        "committed_at": None,
        "message": None,
        "parent_shas": None,
        "is_merge_commit": None,
        "additions_count": None,
        "deletions_count": None,
        "html_url": None,
    },
    GitHubIssue: {
        "repo_id": REPO_ID,
        "issue_id": "900000002",
        "issue_number": 1,
        "title": "Synthetic issue",
        "state_at_retrieval": "open",
        "created_at": CREATED,
        "author_github_id": None,
        "author_login": None,
        "body": None,
        "state_reason_at_retrieval": None,
        "updated_at": None,
        "closed_at": None,
        "comments_count": None,
        "label_names": None,
        "html_url": None,
    },
    GitHubPullRequest: {
        "repo_id": REPO_ID,
        "pr_id": "900000003",
        "pr_number": 2,
        "title": "Synthetic PR",
        "state_at_retrieval": "open",
        "created_at": CREATED,
        "author_github_id": None,
        "author_login": None,
        "body": None,
        "is_merged": None,
        "is_draft_at_retrieval": None,
        "updated_at": None,
        "closed_at": None,
        "merged_at": None,
        "head_sha": None,
        "base_sha": None,
        "comments_count": None,
        "review_comments_count": None,
        "label_names": None,
        "html_url": None,
    },
    GitHubContributorObservation: {
        "repo_id": REPO_ID,
        "is_anonymous": False,
        "contributions_count": 0,
        "observed_at": LATER,
        "contributor_github_id": "900000004",
        "login": None,
        "user_type": None,
        "html_url": None,
    },
    GitHubRelease: {
        "repo_id": REPO_ID,
        "release_id": "900000005",
        "tag_name": "v0.1.0",
        "is_draft": True,
        "is_prerelease": False,
        "name": None,
        "body": None,
        "author_github_id": None,
        "author_login": None,
        "created_at": None,
        "published_at": None,
        "target_commitish": None,
        "assets_count": None,
        "html_url": None,
    },
}


def entity_data(model: type[BaseModel], **changes: Any) -> dict[str, Any]:
    data = deepcopy(EXAMPLES[model])
    data.update(changes)
    return data


@pytest.mark.parametrize("model", EXAMPLES)
def test_valid_entities_serialize_and_preserve_explicit_nulls(model: type[BaseModel]) -> None:
    data = entity_data(model)
    entity = model.model_validate(data)
    assert json.loads(entity.model_dump_json()) == data
    assert model.model_validate_json(entity.model_dump_json()) == entity


@pytest.mark.parametrize("model", EXAMPLES)
def test_every_declared_key_is_required_and_extras_are_forbidden(model: type[BaseModel]) -> None:
    for field in EXAMPLES[model]:
        data = entity_data(model)
        del data[field]
        with pytest.raises(ValidationError) as error:
            model.model_validate(data)
        assert any(
            item["loc"] == (field,) and item["type"] == "missing"
            for item in error.value.errors()
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(entity_data(model, unexpected="not an entity field"))


@pytest.mark.parametrize("repo_id", ["gh_123", "github:repository:0", "github:repository:01", 123])
def test_repository_identity_is_canonical(repo_id: Any) -> None:
    with pytest.raises(ValidationError):
        GitHubCommit.model_validate(entity_data(GitHubCommit, repo_id=repo_id))


@pytest.mark.parametrize(
    "model,field",
    [
        (GitHubCommit, "author_github_id"),
        (GitHubCommit, "committer_github_id"),
        (GitHubIssue, "issue_id"),
        (GitHubIssue, "author_github_id"),
        (GitHubPullRequest, "pr_id"),
        (GitHubPullRequest, "author_github_id"),
        (GitHubContributorObservation, "contributor_github_id"),
        (GitHubRelease, "release_id"),
        (GitHubRelease, "author_github_id"),
    ],
)
def test_numeric_ids_are_positive_decimal_strings(model: type[BaseModel], field: str) -> None:
    for invalid in ("0", "01", "-1", "1.0", "", 1, True):
        with pytest.raises(ValidationError):
            model.model_validate(entity_data(model, **{field: invalid}))


@pytest.mark.parametrize(
    "model,changes",
    [
        (GitHubCommit, {"is_merge_commit": "false"}),
        (GitHubCommit, {"message": 123}),
        (GitHubCommit, {"parent_shas": (PARENT,)}),
        (GitHubIssue, {"title": 123}),
        (GitHubIssue, {"state_at_retrieval": "merged"}),
        (GitHubIssue, {"label_names": "bug"}),
        (GitHubIssue, {"label_names": [123]}),
        (GitHubPullRequest, {"is_merged": 0}),
        (GitHubPullRequest, {"is_draft_at_retrieval": "false"}),
        (GitHubContributorObservation, {"is_anonymous": 0}),
        (GitHubRelease, {"is_draft": "false"}),
        (GitHubRelease, {"is_prerelease": 1}),
    ],
)
def test_wrong_scalar_and_container_types_are_not_coerced(
    model: type[BaseModel], changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(entity_data(model, **changes))


@pytest.mark.parametrize(
    "model,field",
    [
        (GitHubCommit, "authored_at"),
        (GitHubCommit, "committed_at"),
        (GitHubIssue, "created_at"),
        (GitHubIssue, "updated_at"),
        (GitHubIssue, "closed_at"),
        (GitHubPullRequest, "created_at"),
        (GitHubPullRequest, "updated_at"),
        (GitHubPullRequest, "closed_at"),
        (GitHubPullRequest, "merged_at"),
        (GitHubContributorObservation, "observed_at"),
        (GitHubRelease, "created_at"),
        (GitHubRelease, "published_at"),
    ],
)
def test_timestamps_require_canonical_utc_strings(model: type[BaseModel], field: str) -> None:
    for invalid in (
        "2024-01-01T00:00:00",
        "2024-01-01T00:00:00Z",
        "2024-01-01T00:00:00.0000Z",
        "2024-01-01T00:00:00.000+00:00",
        "2024-02-30T00:00:00.000Z",
        datetime(2024, 1, 1),
        datetime(2024, 1, 1, tzinfo=UTC),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(entity_data(model, **{field: invalid}))


@pytest.mark.parametrize(
    "model,field",
    [
        (GitHubCommit, "additions_count"),
        (GitHubCommit, "deletions_count"),
        (GitHubIssue, "comments_count"),
        (GitHubPullRequest, "comments_count"),
        (GitHubPullRequest, "review_comments_count"),
        (GitHubContributorObservation, "contributions_count"),
        (GitHubRelease, "assets_count"),
    ],
)
def test_counters_are_bounded_strict_integers(model: type[BaseModel], field: str) -> None:
    for invalid in (-1, MAX_COUNT + 1, "0", True, 1.0, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            model.model_validate(entity_data(model, **{field: invalid}))
    for valid in (0, MAX_COUNT):
        assert getattr(model.model_validate(entity_data(model, **{field: valid})), field) == valid


@pytest.mark.parametrize(
    "model,field", [(GitHubIssue, "issue_number"), (GitHubPullRequest, "pr_number")]
)
def test_issue_and_pr_numbers_are_positive_strict_integers(model: type[BaseModel], field: str) -> None:
    for invalid in (0, -1, MAX_COUNT + 1, "1", True, 1.0):
        with pytest.raises(ValidationError):
            model.model_validate(entity_data(model, **{field: invalid}))
    for valid in (1, MAX_COUNT):
        assert getattr(model.model_validate(entity_data(model, **{field: valid})), field) == valid


@pytest.mark.parametrize(
    "model,field",
    [(GitHubCommit, "commit_sha"), (GitHubPullRequest, "head_sha"), (GitHubPullRequest, "base_sha")],
)
def test_sha_requires_full_lowercase_sha1(model: type[BaseModel], field: str) -> None:
    for invalid in ("", "abc123", "a" * 39, "a" * 41, "A" * 40, "g" * 40, 123):
        with pytest.raises(ValidationError):
            model.model_validate(entity_data(model, **{field: invalid}))
    assert getattr(model.model_validate(entity_data(model, **{field: SHA})), field) == SHA


@pytest.mark.parametrize(
    "parents,flag", [(None, None), ([], False), ([PARENT], False), ([PARENT, SHA], True)]
)
def test_commit_parent_count_defines_merge_flag(parents: list[str] | None, flag: bool | None) -> None:
    GitHubCommit.model_validate(entity_data(GitHubCommit, parent_shas=parents, is_merge_commit=flag))


@pytest.mark.parametrize(
    "parents,flag",
    [
        (None, False),
        (None, True),
        ([], None),
        ([], True),
        ([PARENT], True),
        ([PARENT, SHA], False),
        ([PARENT, PARENT], True),
        (["abc123"], False),
    ],
)
def test_commit_parent_inconsistency_is_rejected(parents: list[str] | None, flag: bool | None) -> None:
    with pytest.raises(ValidationError):
        GitHubCommit.model_validate(
            entity_data(GitHubCommit, parent_shas=parents, is_merge_commit=flag)
        )


def test_commit_source_dates_have_no_order_or_push_time_inference() -> None:
    commit = GitHubCommit.model_validate(
        entity_data(GitHubCommit, authored_at=LATER, committed_at=CREATED)
    )
    assert commit.authored_at == LATER and commit.committed_at == CREATED


@pytest.mark.parametrize(
    "model,field",
    [
        (GitHubIssue, "updated_at"),
        (GitHubIssue, "closed_at"),
        (GitHubPullRequest, "updated_at"),
        (GitHubPullRequest, "closed_at"),
        (GitHubPullRequest, "merged_at"),
    ],
)
def test_issue_and_pr_chronology(model: type[BaseModel], field: str) -> None:
    changes = {"is_merged": True, "state_at_retrieval": "closed"} if field == "merged_at" else {}
    with pytest.raises(ValidationError, match="cannot precede created_at"):
        model.model_validate(entity_data(model, **changes, **{field: "2023-12-31T23:59:59.999Z"}))
    for valid in (CREATED, LATER):
        model.model_validate(entity_data(model, **changes, **{field: valid}))


@pytest.mark.parametrize(
    "state,merged,merged_at",
    [
        ("closed", True, LATER),
        ("closed", False, None),
        ("closed", None, None),
        ("open", False, None),
        ("open", None, None),
    ],
)
def test_pr_merge_evidence_and_closed_unmerged_state(
    state: str, merged: bool | None, merged_at: str | None
) -> None:
    pr = GitHubPullRequest.model_validate(
        entity_data(
            GitHubPullRequest, state_at_retrieval=state, is_merged=merged, merged_at=merged_at
        )
    )
    assert pr.is_merged is merged and pr.merged_at == merged_at


@pytest.mark.parametrize(
    "state,merged,merged_at",
    [("open", True, LATER), ("closed", False, LATER), ("closed", None, LATER), ("closed", True, None)],
)
def test_pr_inconsistent_merge_evidence_is_rejected(
    state: str, merged: bool | None, merged_at: str | None
) -> None:
    with pytest.raises(ValidationError):
        GitHubPullRequest.model_validate(
            entity_data(
                GitHubPullRequest, state_at_retrieval=state, is_merged=merged, merged_at=merged_at
            )
        )


def test_closed_issue_can_have_unknown_closed_time_and_empty_known_labels() -> None:
    issue = GitHubIssue.model_validate(
        entity_data(GitHubIssue, state_at_retrieval="closed", label_names=[])
    )
    assert issue.closed_at is None and issue.label_names == []
    pr = GitHubPullRequest.model_validate(entity_data(GitHubPullRequest, label_names=[]))
    assert pr.label_names == [] and EXAMPLES[GitHubPullRequest]["label_names"] is None


@pytest.mark.parametrize(
    "anonymous,numeric_id,login,valid",
    [
        (True, None, None, True),
        (True, "900000004", None, False),
        (True, None, "synthetic", False),
        (False, None, None, False),
        (False, "900000004", None, True),
        (False, "900000004", "synthetic", True),
    ],
)
def test_contributor_identity_rules(
    anonymous: bool, numeric_id: str | None, login: str | None, valid: bool
) -> None:
    data = entity_data(
        GitHubContributorObservation,
        is_anonymous=anonymous,
        contributor_github_id=numeric_id,
        login=login,
    )
    if valid:
        assert GitHubContributorObservation.model_validate(data).contributor_github_id == numeric_id
    else:
        with pytest.raises(ValidationError):
            GitHubContributorObservation.model_validate(data)


@pytest.mark.parametrize("field", ["email", "committed_at", "event_at", "cutoff_at"])
def test_contributors_do_not_admit_email_or_historical_event_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        GitHubContributorObservation.model_validate(
            entity_data(GitHubContributorObservation, **{field: CREATED})
        )


def test_release_publication_is_optional_and_not_inferred_or_ordered() -> None:
    draft = GitHubRelease.model_validate(entity_data(GitHubRelease, created_at=LATER))
    assert draft.is_draft is True and draft.published_at is None and draft.assets_count is None
    release = GitHubRelease.model_validate(
        entity_data(GitHubRelease, is_draft=False, created_at=LATER, published_at=CREATED)
    )
    assert release.published_at == CREATED and release.created_at == LATER


@pytest.mark.parametrize(
    "model,field",
    [
        (GitHubCommit, "message"),
        (GitHubIssue, "title"),
        (GitHubIssue, "body"),
        (GitHubPullRequest, "title"),
        (GitHubContributorObservation, "login"),
        (GitHubRelease, "tag_name"),
    ],
)
def test_text_cannot_be_empty_or_whitespace(model: type[BaseModel], field: str) -> None:
    for invalid in ("", " "):
        with pytest.raises(ValidationError):
            model.model_validate(entity_data(model, **{field: invalid}))


@pytest.mark.parametrize("model", EXAMPLES)
def test_http_urls_reuse_canonical_validation(model: type[BaseModel]) -> None:
    for invalid in ("not-a-url", "https://user:secret@example.org/repo", "https://example.org/a b"):
        with pytest.raises(ValidationError):
            model.model_validate(entity_data(model, html_url=invalid))
    entity = model.model_validate(entity_data(model, html_url="https://github.com/Synthetic/Demo"))
    assert entity.html_url == "https://github.com/Synthetic/Demo"


@pytest.mark.parametrize(
    "source_record_id,payload,event_at",
    [
        (
            "commit:" + SHA,
            {"sha": SHA, "commit": {"committer": {"date": "2024-01-01T00:00:00Z"}}},
            CREATED,
        ),
        ("issue:900000002", {"id": 900000002, "number": 1, "state": "open"}, CREATED),
        ("pull_request:900000003", {"id": 900000003, "number": 2, "draft": False}, CREATED),
        ("contributor:900000004", {"id": 900000004, "contributions": 0}, None),
        (None, {"type": "Anonymous", "contributions": 1}, None),
        ("release:900000005", {"id": 900000005, "draft": True, "published_at": None}, None),
    ],
)
def test_native_activity_raw_envelopes_remain_t011_compatible(
    source_record_id: str | None, payload: dict[str, Any], event_at: str | None
) -> None:
    raw = validate_record(
        {
            "schema_version": "0.1.0",
            "record_type": "raw_record",
            "raw_record_id": "raw:00000000-0000-4000-8000-000000000012",
            "run_id": "00000000-0000-4000-8000-000000000001",
            "source": "github_rest",
            "source_record_type": "repository_activity",
            "source_record_id": source_record_id,
            "repo_id": REPO_ID,
            "package_id": None,
            "source_url": "https://api.github.com/repos/Synthetic/Demo",
            "retrieved_at": LATER,
            "event_at": event_at,
            "available_at": None,
            "http_status": None,
            "raw_body_sha256": None,
            "payload": deepcopy(payload),
        }
    )
    assert isinstance(raw, RawRecord)
    assert raw.payload == payload and raw.event_at == event_at
    assert raw.available_at is raw.package_id is None
    assert validate_record(raw.model_dump_json()).payload == payload
