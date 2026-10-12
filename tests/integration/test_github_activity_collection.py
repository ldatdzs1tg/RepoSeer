"""Offline T-014 Phase 2 collection, native validation, pagination and persistence."""

import json
from collections.abc import Callable, Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from reposeer.clients.github import GitHubClient
from reposeer.collection import HTTPClient
from reposeer.collectors.github import activity
from reposeer.collectors.github.activity import ActivityGroup, GitHubActivityContractCollector
from reposeer.config import settings
from reposeer.contracts import (
    SCHEMA_VERSION,
    CollectionRequest,
    CollectionSubject,
    GitHubRepository,
    RawRecord,
    validate_collection_pair,
    validate_record,
)
from reposeer.exceptions import CollectionError, StorageError
from reposeer.schemas.github.activity_entities import (
    GitHubCommit,
    GitHubContributorObservation,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRelease,
)
from reposeer.services.activity_service import ActivityService
from reposeer.services.repository_service import RepositoryService
from reposeer.storage.paths import StoragePaths

ROOT = Path(__file__).resolve().parents[2]
GROUPS = ("commits", "issues", "pull_requests", "contributors", "releases")
NOW = datetime(2024, 2, 29, 12, tzinfo=UTC)
RETRIEVED = "2024-02-29T12:00:00.000Z"
CREATED = "2024-01-01T00:00:00.000Z"
BASE_URL = "https://github.example/api/v3"
SHA = "a" * 40
PARENT = "b" * 40


def clock() -> datetime:
    return NOW


@pytest.fixture
def repository() -> GitHubRepository:
    value = validate_record(
        json.loads((ROOT / "examples/contracts/github_repository.json").read_text(encoding="utf-8"))
    )
    assert isinstance(value, GitHubRepository)
    return value


def native(group: ActivityGroup, index: int = 0, **changes: Any) -> dict[str, Any]:
    user = {"id": 42, "login": "synthetic-user", "provider_extra": None}
    payloads = {
        "commits": {
            "sha": f"{index + 1:040x}",
            "author": user,
            "committer": None,
            "commit": {
                "author": {"date": "2024-01-02T00:00:00Z", "email": "synthetic@example.org"},
                "committer": {"date": "2024-01-01T02:00:00+02:00"},
                "message": "Synthetic commit",
            },
            "parents": [{"sha": PARENT}, {"sha": SHA}],
            "stats": {"additions": 5, "deletions": 2},
        },
        "issues": {
            "id": 1000 + index,
            "number": index + 1,
            "title": "Synthetic issue",
            "state": "closed",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "closed_at": None,
            "user": user,
            "body": " ",
            "state_reason": "completed",
            "comments": 0,
            "labels": [{"name": "bug", "extra": 7}],
        },
        "pull_requests": {
            "id": 2000 + index,
            "number": index + 1,
            "title": "Synthetic PR",
            "state": "closed",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "closed_at": None,
            "merged_at": None,
            "merged": False,
            "user": user,
            "body": "",
            "draft": False,
            "head": {"sha": SHA},
            "base": {"sha": PARENT},
            "comments": 1,
            "review_comments": 2,
            "labels": [],
        },
        "contributors": {
            "id": 3000 + index,
            "login": "synthetic-user",
            "type": "User",
            "contributions": 3,
        },
        "releases": {
            "id": 4000 + index,
            "tag_name": f"v0.{index}.0",
            "name": "",
            "body": " ",
            "author": user,
            "created_at": "2024-02-01T00:00:00Z",
            "published_at": "2024-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "target_commitish": "main",
            "assets": [{"id": 9}],
        },
    }
    payload = payloads[group]
    payload.update(html_url="https://github.com/Synthetic/Demo", provider_extra={"null": None})
    payload.update(changes)
    return payload


def request_for(
    repository: GitHubRepository, group: ActivityGroup = "commits", **changes: Any
) -> CollectionRequest:
    data = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "collection_request",
        "request_id": str(uuid4()),
        "run_id": str(uuid4()),
        "collector_name": f"github_activity_{group}",
        "source": "github_rest",
        "subjects": [
            CollectionSubject(
                repo_id=repository.identity.repo_id,
                repo_full_name=repository.identity.repo_full_name,
                package_id=None,
                url=None,
            )
        ],
        "record_kinds": ["repository_activity"],
        "time_range": None,
        "cutoff_at": None,
        "cursor": None,
        "max_pages": 10,
        "max_records": 1000,
    }
    data.update(changes)
    return CollectionRequest.model_validate(data)


@pytest.fixture
def client_factory(monkeypatch) -> Iterator[Callable]:
    monkeypatch.setattr(settings.collection, "cache_enabled", False)
    sessions: list[HTTPClient] = []

    def make(pages: dict[int, Any]) -> tuple[GitHubClient, list[httpx.Request]]:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            value = pages[int(request.url.params["page"])]
            if isinstance(value, httpx.Response):
                return value
            if value == "network failure":
                raise httpx.ConnectTimeout("synthetic-secret", request=request)
            return httpx.Response(200, json=value)

        http = HTTPClient(
            max_retries=1,
            clock=lambda: 0,
            sleep=lambda delay: None,
            transport=httpx.MockTransport(handler),
        )
        sessions.append(http)
        return GitHubClient(token="synthetic-token", base_url=BASE_URL, http_client=http), calls

    yield make
    for session in sessions:
        session.close()


@pytest.mark.parametrize("group", GROUPS)
def test_each_group_uses_native_endpoint_and_canonical_raw(repository, client_factory, group):
    payload = native(group)
    before = deepcopy(payload)
    client, calls = client_factory({1: [payload]})
    request = request_for(repository, group)
    result = GitHubActivityContractCollector(group, repository, client, clock=clock).collect(request)
    validate_collection_pair(request, result)
    assert result.status == "complete" and result.metadata.pagination_complete
    assert result.metadata.pages_fetched == result.metadata.records_emitted == 1
    assert result.metadata.records_rejected == 0
    assert result.metadata.next_cursor is None
    assert result.metadata.started_at == result.metadata.finished_at == RETRIEVED
    assert result.metadata.rate_limit_remaining is result.metadata.rate_limit_reset_at is None
    coverage = result.metadata.coverage[0]
    assert coverage.data_group == group and coverage.status == "complete"
    assert coverage.records_observed == 1 and coverage.missing_reason is None
    raw = result.records[0]
    assert isinstance(validate_record(raw), RawRecord)
    assert raw.payload == payload == before
    assert raw.repo_id == repository.identity.repo_id
    assert raw.package_id is raw.available_at is raw.http_status is raw.raw_body_sha256 is None
    assert raw.source == "github_rest" and raw.source_record_type == "repository_activity"
    assert raw.run_id == request.run_id and raw.retrieved_at == RETRIEVED
    assert raw.event_at == (None if group == "contributors" else CREATED)
    endpoint = "pulls" if group == "pull_requests" else group
    assert raw.source_url == f"{BASE_URL}/repos/Synthetic/Demo/{endpoint}"
    assert len(calls) == 1 and calls[0].url.path.endswith("/" + endpoint)
    params = {"per_page": "100", "page": "1"}
    if group in {"issues", "pull_requests"}:
        params["state"] = "all"
    if group == "contributors":
        params["anon"] = "true"
    assert dict(calls[0].url.params) == params
    namespace = {
        "commits": "commit",
        "issues": "issue",
        "pull_requests": "pull_request",
        "contributors": "contributor",
        "releases": "release",
    }[group]
    identity = payload["sha"] if group == "commits" else str(payload["id"])
    assert raw.source_record_id == f"{namespace}:{identity}"


def test_native_mappings_satisfy_phase1_schemas(repository):
    repo_id = repository.identity.repo_id
    commit = activity._extract_commit(native("commits"), repo_id)
    assert isinstance(commit, GitHubCommit)
    assert commit.author_github_id == "42" and commit.author_login == "synthetic-user"
    assert commit.committer_github_id is commit.committer_login is None
    assert commit.committed_at == CREATED and commit.authored_at > commit.committed_at
    assert commit.parent_shas == [PARENT, SHA] and commit.is_merge_commit is True
    assert commit.additions_count is commit.deletions_count is None
    issue = activity._extract_issue(native("issues"), repo_id)
    assert isinstance(issue, GitHubIssue)
    assert issue.issue_id == "1000" and issue.issue_number == 1
    assert issue.author_github_id == "42" and issue.state_reason_at_retrieval == "completed"
    assert issue.label_names == ["bug"] and issue.comments_count == 0
    assert issue.body is issue.closed_at is None and issue.state_at_retrieval == "closed"
    pr = activity._extract_pull_request(native("pull_requests"), repo_id)
    assert isinstance(pr, GitHubPullRequest)
    assert pr.pr_id == "2000" and pr.pr_number == 1
    assert pr.head_sha == SHA and pr.base_sha == PARENT
    assert pr.comments_count == 1 and pr.review_comments_count == 2
    assert pr.is_merged is pr.is_draft_at_retrieval is False
    assert pr.body is pr.merged_at is None and pr.label_names == []
    contributor = activity._extract_contributor(native("contributors"), repo_id, RETRIEVED)
    assert isinstance(contributor, GitHubContributorObservation)
    assert contributor.contributor_github_id == "3000" and contributor.contributions_count == 3
    assert contributor.observed_at == RETRIEVED and contributor.is_anonymous is False
    release = activity._extract_release(native("releases"), repo_id)
    assert isinstance(release, GitHubRelease)
    assert release.release_id == "4000" and release.tag_name == "v0.0.0"
    assert release.created_at > release.published_at
    assert release.assets_count is release.name is release.body is None
    assert release.is_draft is release.is_prerelease is False


@pytest.mark.parametrize(
    "changes",
    [
        {"visibility": "private"},
        {"visibility": "internal"},
        {"is_fork": True},
        {"is_template": True},
        {"mirror_url": "https://example.org/mirror"},
        {"identity": {"repo_id": "github:repository:7"}},
    ],
)
def test_ineligible_or_mutated_repository_never_fetches(repository, client_factory, changes):
    request = request_for(repository)
    if "identity" in changes:
        repository.identity.repo_id = changes["identity"]["repo_id"]
    else:
        for name, value in changes.items():
            setattr(repository, name, value)
    client, calls = client_factory({})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    validate_collection_pair(request, result)
    assert result.status == "failed" and result.records == []
    assert result.metadata.coverage[0].status == "unavailable" and not calls


@pytest.mark.parametrize("change", ["id", "name", "missing_id", "missing_name"])
def test_subject_requires_both_verified_id_and_name(repository, client_factory, change):
    request = request_for(repository)
    subject = request.subjects[0].model_dump()
    if change == "id":
        subject["repo_id"] = "github:repository:7"
    elif change == "name":
        subject["repo_full_name"] = "Other/Name"
    elif change == "missing_id":
        subject["repo_id"] = None
    else:
        subject["repo_full_name"] = None
    request.subjects = [CollectionSubject.model_validate(subject)]
    client, calls = client_factory({})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    validate_collection_pair(request, result)
    assert result.status == "failed" and result.errors[0].code == "validation_error"
    assert not calls


@pytest.mark.parametrize("archived", [False, True])
def test_renamed_repository_keeps_numeric_identity_and_archived_is_eligible(
    repository, client_factory, archived
):
    repository.is_archived = archived
    repository.identity.repo_full_name = "Synthetic/Renamed"
    repository.identity.repo_url = "https://github.com/synthetic/renamed"
    request = request_for(repository)
    request.subjects[0].repo_full_name = "SYNTHETIC/RENAMED"
    client, calls = client_factory({1: [native("commits")]})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    assert result.status == "complete" and result.records[0].repo_id == "github:repository:900000001"
    assert calls[0].url.path.endswith("/repos/Synthetic/Renamed/commits")


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"source": "github_graphql"}, "unsupported_record_type"),
        ({"record_kinds": ["github_repository"]}, "unsupported_record_type"),
        ({"cutoff_at": CREATED}, "insufficient_history"),
        (
            {"time_range": {"start_at": CREATED, "end_at": "2024-02-01T00:00:00.000Z"}},
            "insufficient_history",
        ),
    ],
)
def test_unsupported_requests_fail_without_fetching(repository, client_factory, changes, code):
    request = request_for(repository, **changes)
    client, calls = client_factory({})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    validate_collection_pair(request, result)
    assert result.status == "failed" and result.errors[0].code == code and not calls


@pytest.mark.parametrize(
    "sizes,max_pages,max_records,status,pages,cursor,count",
    [
        ([0], 1, 1000, "complete", 1, None, 0),
        ([3], 1, 1000, "complete", 1, None, 3),
        ([100, 2], 2, 1000, "complete", 2, None, 102),
        ([100], 1, 1000, "partial", 1, "p:2:i:0", 100),
        ([100, 0], 2, 1000, "complete", 2, None, 100),
        ([3], 1, 2, "partial", 1, "p:1:i:2", 2),
        ([3], 1, 3, "complete", 1, None, 3),
        ([100], 2, 100, "partial", 1, "p:2:i:0", 100),
        ([100, 1], 2, 101, "complete", 2, None, 101),
    ],
)
def test_pagination_and_budgets_do_not_silently_truncate(
    repository, client_factory, sizes, max_pages, max_records, status, pages, cursor, count
):
    responses = {
        page: [native("commits", page * 100 + i) for i in range(size)]
        for page, size in enumerate(sizes, 1)
    }
    client, calls = client_factory(responses)
    request = request_for(repository, max_pages=max_pages, max_records=max_records)
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    validate_collection_pair(request, result)
    assert result.status == status and result.metadata.pages_fetched == pages
    assert len(calls) == pages and len(result.records) == count
    assert result.metadata.next_cursor == cursor
    assert result.metadata.pagination_complete is (status == "complete")
    assert result.metadata.coverage[0].missing_reason == (
        None if status == "complete" else "not_collected"
    )


def test_native_cursor_counts_skipped_prs_and_invalid_issues(repository, client_factory):
    items = [
        {"pull_request": None},
        native("issues", title=0),
        native("issues", 2),
        native("issues", 3),
    ]
    client, calls = client_factory({3: items})
    collector = GitHubActivityContractCollector("issues", repository, client, clock=clock)
    request = request_for(repository, "issues", cursor="p:3:i:0", max_records=1)
    first = collector.collect(request)
    assert first.status == "partial" and first.metadata.records_rejected == 1
    assert first.metadata.next_cursor == "p:3:i:3"
    assert first.records[0].source_record_id == "issue:1002"
    followup = request_for(repository, "issues", cursor=first.metadata.next_cursor)
    second = collector.collect(followup)
    assert second.status == "complete" and second.metadata.records_rejected == 0
    assert second.records[0].source_record_id == "issue:1003"
    assert len(calls) == 2 and all(call.url.params["page"] == "3" for call in calls)


def test_full_page_of_prs_is_not_malformed_or_proven_complete(repository, client_factory):
    client, _ = client_factory({1: [{"pull_request": {}}] * 100})
    request = request_for(repository, "issues", max_pages=1)
    result = GitHubActivityContractCollector("issues", repository, client, clock=clock).collect(request)
    assert result.status == "partial" and result.records == [] and result.errors == []
    assert result.metadata.records_rejected == 0 and result.metadata.next_cursor == "p:2:i:0"


@pytest.mark.parametrize(
    "cursor", ["page:1", "p:0:i:0", "p:1:i:-1", "p:1:i:100", "p:1:i:01", "p:1:i:0:extra"]
)
def test_invalid_cursor_never_fetches(repository, client_factory, cursor):
    client, calls = client_factory({})
    request = request_for(repository, cursor=cursor)
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    assert result.status == "failed" and result.errors[0].code == "validation_error" and not calls


@pytest.mark.parametrize("response", [{}, None, "unexpected", 123, [None] * 101])
def test_unexpected_page_shape_is_not_empty_success(repository, client_factory, response):
    client, _ = client_factory({1: response})
    request = request_for(repository)
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    assert result.status == "failed" and result.errors[0].code == "parse_error"
    assert result.metadata.pagination_complete is False
    assert result.metadata.coverage[0].status == "unavailable"


@pytest.mark.parametrize(
    "group,changes,remove",
    [
        ("commits", {"sha": "abc123"}, None),
        ("commits", {"parents": [{"sha": SHA}, {"sha": SHA}]}, None),
        ("commits", {"parents": [{}]}, None),
        ("commits", {"commit": {"committer": {"date": "2024-01-01T00:00:00"}}}, None),
        ("commits", {"author": {"id": True}}, None),
        ("issues", {"comments": "0"}, None),
        ("issues", {"number": True}, None),
        ("issues", {"labels": ["bug"]}, None),
        ("issues", {"body": 1}, None),
        ("issues", {}, "created_at"),
        ("pull_requests", {"merged": False, "merged_at": "2024-01-02T00:00:00Z"}, None),
        ("pull_requests", {"merged": True, "merged_at": None}, None),
        ("pull_requests", {"draft": "false"}, None),
        ("pull_requests", {"head": {"sha": "abc123"}}, None),
        ("contributors", {"id": None, "type": "User"}, None),
        ("contributors", {"type": "Anonymous"}, None),
        ("contributors", {"contributions": True}, None),
        ("contributors", {"id": "3000"}, None),
        ("releases", {}, "draft"),
        ("releases", {"prerelease": "false"}, None),
        ("releases", {"assets": "unknown"}, None),
    ],
)
def test_malformed_item_does_not_discard_valid_items(
    repository, client_factory, caplog, group, changes, remove
):
    bad = native(group, **changes)
    bad["sensitive"] = "synthetic-secret-payload"
    if remove:
        del bad[remove]
    good = native(group, 1)
    client, _ = client_factory({1: [bad, good]})
    request = request_for(repository, group)
    result = GitHubActivityContractCollector(group, repository, client, clock=clock).collect(request)
    validate_collection_pair(request, result)
    assert result.status == "partial" and result.metadata.pagination_complete
    assert result.metadata.records_rejected == 1 and len(result.records) == 1
    assert result.records[0].payload == good
    assert result.errors[0].code in {"validation_error", "parse_error"}
    assert result.errors[0].raw_record_id is None
    assert result.metadata.coverage[0].missing_reason == "parse_error"
    assert "synthetic-secret-payload" not in result.model_dump_json() + caplog.text


def test_invalid_nonobject_item_is_isolated(repository, client_factory):
    client, _ = client_factory({1: [None, native("commits")]})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request_for(repository))
    assert result.status == "partial" and len(result.records) == 1
    assert result.errors[0].code == "parse_error" and result.metadata.records_rejected == 1


def test_missing_commit_identity_dates_and_parents_remain_unknown(repository, client_factory):
    payload = {"sha": SHA, "author": None, "committer": None, "commit": {}, "provider_extra": None}
    client, _ = client_factory({1: [payload]})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request_for(repository))
    assert result.status == "complete" and result.records[0].event_at is None
    entity = activity._extract_commit(result.records[0].payload, repository.identity.repo_id)
    assert entity.author_github_id is entity.committer_github_id is None
    assert entity.authored_at is entity.committed_at is entity.parent_shas is entity.is_merge_commit is None
    assert result.records[0].payload == payload


@pytest.mark.parametrize("merged", [False, None, True])
def test_pr_merge_state_is_based_on_evidence(repository, client_factory, merged):
    changes = {"merged": merged, "merged_at": "2024-01-02T00:00:00Z" if merged else None}
    payload = native("pull_requests", **changes)
    if merged is None:
        del payload["merged"]
    client, _ = client_factory({1: [payload]})
    request = request_for(repository, "pull_requests")
    collector = GitHubActivityContractCollector("pull_requests", repository, client, clock=clock)
    result = collector.collect(request)
    assert result.status == "complete" and result.records[0].event_at == CREATED
    entity = activity._extract_pull_request(payload, repository.identity.repo_id)
    assert entity.is_merged is merged


def test_anonymous_contributor_uses_raw_retrieval_time_without_invented_id(
    repository, client_factory, monkeypatch
):
    payload = {"type": "Anonymous", "contributions": 1, "email": "synthetic@example.org"}
    client, _ = client_factory({1: [payload]})
    observed = []
    constructor = activity.GitHubContributorObservation

    def capture(**values):
        entity = constructor(**values)
        observed.append(entity)
        return entity

    monkeypatch.setattr(activity, "GitHubContributorObservation", capture)
    collector = GitHubActivityContractCollector("contributors", repository, client, clock=clock)
    result = collector.collect(request_for(repository, "contributors"))
    raw = result.records[0]
    assert raw.source_record_id is raw.event_at is None
    assert raw.payload == payload and observed[0].observed_at == raw.retrieved_at
    assert observed[0].contributor_github_id is observed[0].login is None
    assert "email" not in observed[0].model_dump()


def test_draft_release_does_not_invent_publication_time(repository, client_factory):
    payload = native("releases", draft=True, published_at=None)
    client, _ = client_factory({1: [payload]})
    collector = GitHubActivityContractCollector("releases", repository, client, clock=clock)
    result = collector.collect(request_for(repository, "releases"))
    assert result.status == "complete" and result.records[0].event_at is None
    assert result.records[0].payload["published_at"] is None


def test_future_commit_event_is_rejected_by_raw_boundary(repository, client_factory):
    payload = native("commits", commit={"committer": {"date": "2025-01-01T00:00:00Z"}})
    entity = activity._extract_commit(payload, repository.identity.repo_id)
    assert entity.committed_at == "2025-01-01T00:00:00.000Z"
    client, _ = client_factory({1: [payload]})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request_for(repository))
    assert result.status == "partial" and result.records == []
    assert result.errors[0].code == "validation_error" and result.metadata.records_rejected == 1


@pytest.mark.parametrize("later_page", [False, True])
@pytest.mark.parametrize(
    "response,code,retryable",
    [
        (httpx.Response(404, text="synthetic-secret"), "not_found", False),
        (httpx.Response(429, text="synthetic-secret"), "rate_limited", True),
        (
            httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, text="synthetic-secret"),
            "rate_limited",
            True,
        ),
        (httpx.Response(401, text="synthetic-secret"), "http_error", False),
        (httpx.Response(503, text="synthetic-secret"), "http_error", True),
        (httpx.Response(200, text="synthetic-secret"), "parse_error", False),
        ("network failure", "network_error", True),
    ],
)
def test_http_failures_preserve_completed_pages_and_safe_errors(
    repository, client_factory, caplog, later_page, response, code, retryable
):
    pages = (
        {1: [native("commits", i) for i in range(100)], 2: response}
        if later_page
        else {1: response}
    )
    client, calls = client_factory(pages)
    request = request_for(repository)
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    result = collector.collect(request)
    validate_collection_pair(request, result)
    assert result.status == ("partial" if later_page else "failed")
    assert len(result.records) == (100 if later_page else 0)
    assert result.metadata.pages_fetched == (1 if later_page else 0)
    assert len(calls) == (2 if later_page else 1)
    assert result.metadata.next_cursor == ("p:2:i:0" if later_page else "p:1:i:0")
    assert result.errors[-1].code == code and result.errors[-1].retryable is retryable
    assert "synthetic-secret" not in result.model_dump_json() + caplog.text


def test_page_retrieval_clock_is_used_inside_run_bounds(repository, client_factory):
    ticks = iter([NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)])
    client, _ = client_factory({1: [native("contributors")]})
    collector = GitHubActivityContractCollector(
        "contributors", repository, client, clock=lambda: next(ticks)
    )
    result = collector.collect(request_for(repository, "contributors"))
    assert result.metadata.started_at == RETRIEVED
    assert result.records[0].retrieved_at == "2024-02-29T12:00:01.000Z"
    assert result.metadata.finished_at == "2024-02-29T12:00:02.000Z"


@pytest.mark.parametrize("group", GROUPS)
def test_service_persists_each_group_in_own_append_only_dataset(
    repository, client_factory, tmp_path, group
):
    payload = native(group)
    client, calls = client_factory({1: [payload]})
    service = ActivityService(client, storage_paths=StoragePaths(tmp_path), clock=clock)
    run_id = str(uuid4())
    first = service.collect_and_store(repository, group, run_id=run_id)
    destination = tmp_path / "raw" / "github" / f"{group}.jsonl"
    previous = destination.read_bytes()
    second = service.collect_and_store(repository, group)
    replay = service.raw_storage.read_contract_records(destination)
    assert destination.read_bytes().startswith(previous)
    assert replay == first.records + second.records
    assert replay[0].payload == replay[1].payload == payload
    assert first.run_id == run_id and first.request_id != second.request_id
    assert UUID(second.run_id).version == 4 and second.run_id != first.run_id
    assert replay[0].raw_record_id != replay[1].raw_record_id
    assert list(tmp_path.rglob("*.jsonl")) == [destination]
    assert not list(tmp_path.rglob("*.parquet")) and not (tmp_path / "metadata").exists()
    assert len(calls) == 2


def test_service_does_not_write_when_collection_fails(repository, client_factory, tmp_path):
    repository.is_fork = True
    client, calls = client_factory({})
    service = ActivityService(client, storage_paths=StoragePaths(tmp_path), clock=clock)
    result = service.collect_and_store(repository, "issues")
    assert result.status == "failed" and not calls and not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.parametrize("run_id", ["", "not-a-uuid", "00000000-0000-1000-8000-000000000001"])
def test_service_validates_supplied_run_id_before_fetch(repository, client_factory, tmp_path, run_id):
    client, calls = client_factory({})
    service = ActivityService(client, storage_paths=StoragePaths(tmp_path), clock=clock)
    with pytest.raises(ValidationError):
        service.collect_and_store(repository, "commits", run_id=run_id)
    assert not calls and not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.parametrize("error_type", [OSError, StorageError])
def test_service_storage_failure_preserves_previously_appended_raw(
    repository, client_factory, tmp_path, monkeypatch, error_type
):
    client, _ = client_factory({1: [native("commits", 0)]})
    service = ActivityService(client, storage_paths=StoragePaths(tmp_path), clock=clock)
    service.collect_and_store(repository, "commits")
    destination = tmp_path / "raw/github/commits.jsonl"
    previous = destination.read_bytes()
    next_client, _ = client_factory({1: [native("commits", 1), native("commits", 2)]})
    service.client = next_client
    append = service.raw_storage.append_contract_record
    calls = []

    def fail_second(path, raw):
        calls.append(raw)
        if len(calls) == 2:
            raise error_type("synthetic-secret-storage")
        append(path, raw)

    monkeypatch.setattr(service.raw_storage, "append_contract_record", fail_second)
    with pytest.raises(CollectionError, match="Unable to persist GitHub activity raw records") as error:
        service.collect_and_store(repository, "commits")
    assert "synthetic-secret-storage" not in str(error.value)
    assert destination.read_bytes().startswith(previous)
    replay = service.raw_storage.read_contract_records(destination)
    assert len(replay) == 2 and replay[-1].payload == native("commits", 1)


def test_injected_client_is_not_closed_by_collector_or_service(repository, client_factory, tmp_path):
    client, _ = client_factory({1: []})
    collector = GitHubActivityContractCollector("commits", repository, client, clock=clock)
    service = ActivityService(client, storage_paths=StoragePaths(tmp_path), clock=clock)
    collector.close()
    service.close()
    assert collector.collect(request_for(repository)).status == "complete"
    assert service.collect_and_store(repository, "commits").status == "complete"
    assert client.http._sync_client.is_closed is False


def test_owned_clients_are_closed_once(repository, monkeypatch):
    owned = Mock(spec=GitHubClient)
    monkeypatch.setattr("reposeer.collectors.github.activity.GitHubClient", lambda: owned)
    collector = GitHubActivityContractCollector("commits", repository)
    collector.close()
    collector.close()
    owned.close.assert_called_once()
    owned_service = Mock(spec=GitHubClient)
    monkeypatch.setattr("reposeer.services.activity_service.GitHubClient", lambda: owned_service)
    service = ActivityService()
    service.close()
    service.close()
    owned_service.close.assert_called_once()


def test_contributor_positional_compatibility_and_anon_opt_in(client_factory):
    client, calls = client_factory({7: [], 8: []})
    client.list_contributors("Synthetic", "Demo", 20, 7)
    client.list_contributors("Synthetic", "Demo", 20, 8, anon=True)
    assert dict(calls[0].url.params) == {"per_page": "20", "page": "7"}
    assert dict(calls[1].url.params) == {"per_page": "20", "page": "8", "anon": "true"}


def test_t013_context_is_reused_for_all_groups_without_metadata_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.collection, "cache_enabled", False)
    metadata_payload = json.loads(
        (ROOT / "examples/contracts/github_raw_record.json").read_text(encoding="utf-8")
    )["payload"]
    calls = []

    def handler(request):
        calls.append(request)
        endpoint = request.url.path.rsplit("/", 1)[1]
        if endpoint == "Demo":
            return httpx.Response(200, json=metadata_payload)
        group = "pull_requests" if endpoint == "pulls" else endpoint
        return httpx.Response(200, json=[native(group)])

    with HTTPClient(max_retries=1, transport=httpx.MockTransport(handler)) as http:
        client = GitHubClient(token="synthetic-token", base_url=BASE_URL, http_client=http)
        metadata = RepositoryService(client, storage_paths=StoragePaths(tmp_path))
        metadata.collector.clock = clock
        repository = metadata.collect_and_store("Synthetic", "Demo")
        processed = metadata.paths.processed_path("repositories")
        previous = processed.read_bytes()
        service = ActivityService(client, storage_paths=StoragePaths(tmp_path), clock=clock)
        for group in GROUPS:
            result = service.collect_and_store(repository, group)
            assert result.status == "complete"
            assert result.records[0].repo_id == repository.identity.repo_id
        assert len(calls) == 6
        assert sum(call.url.path.endswith("/repos/Synthetic/Demo") for call in calls) == 1
        assert processed.read_bytes() == previous
        service.close()
        metadata.close()
        assert not http._sync_client.is_closed


def test_service_persists_valid_records_from_partial_collection(repository, client_factory, tmp_path):
    client, _ = client_factory({1: [native("issues", number=False), native("issues", 1)]})
    service = ActivityService(client, storage_paths=StoragePaths(tmp_path), clock=clock)
    result = service.collect_and_store(repository, "issues")
    assert result.status == "partial" and result.metadata.records_rejected == 1
    replay = service.raw_storage.read_contract_records(tmp_path / "raw/github/issues.jsonl")
    assert replay == result.records and len(replay) == 1
