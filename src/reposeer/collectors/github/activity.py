"""Synchronous T-014 activity extraction and canonical raw collection."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from reposeer.clients.github import GitHubClient
from reposeer.collection.http.retry import RETRYABLE_STATUS_CODES
from reposeer.contracts import (
    SCHEMA_VERSION,
    CollectionMetadata,
    CollectionRequest,
    CollectionResult,
    Coverage,
    DataError,
    GitHubRepository,
    RawRecord,
    validate_collection_pair,
    validate_record,
)
from reposeer.contracts.identity import format_utc_timestamp, validate_http_url
from reposeer.contracts.models import MissingReason
from reposeer.exceptions import HTTPClientError, RateLimitExceededError
from reposeer.schemas.github.activity_entities import (
    GitHubCommit,
    GitHubContributorObservation,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRelease,
)

ActivityGroup = Literal["commits", "issues", "pull_requests", "contributors", "releases"]
_ENDPOINTS = {
    "commits": "commits",
    "issues": "issues",
    "pull_requests": "pulls",
    "contributors": "contributors",
    "releases": "releases",
}


def _native_id(value: Any) -> str:
    if type(value) is not int or value <= 0:
        raise ValueError("Native GitHub ID must be a positive integer")
    return str(value)


def _count(value: Any) -> int | None:
    if value is not None and (type(value) is not int or not 0 <= value <= 9007199254740991):
        raise ValueError("Native count must be a bounded nonnegative integer")
    return value


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Native timestamp must be an aware timestamp string")
    return format_utc_timestamp(datetime.fromisoformat(value))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Native text must be a string")
    return value if value.strip() else None


def _object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Native nested value must be an object")
    return value


def _user(value: Any) -> tuple[str | None, str | None]:
    user = _object(value)
    numeric_id = user.get("id")
    return _native_id(numeric_id) if numeric_id is not None else None, _text(user.get("login"))


def _object_values(value: Any, key: str) -> list[Any] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("Native attributes must be a list of objects")
    values = []
    for item in value:
        if not isinstance(item, dict) or key not in item:
            raise ValueError("Native attribute entry is missing its required key")
        values.append(item[key])
    return values


def _extract_commit(payload: dict[str, Any], repo_id: str) -> GitHubCommit:
    commit = _object(payload.get("commit"))
    author_id, author_login = _user(payload.get("author"))
    committer_id, committer_login = _user(payload.get("committer"))
    parents = _object_values(payload.get("parents"), "sha")
    return GitHubCommit(
        repo_id=repo_id,
        commit_sha=payload["sha"],
        author_github_id=author_id,
        author_login=author_login,
        committer_github_id=committer_id,
        committer_login=committer_login,
        authored_at=_timestamp(_object(commit.get("author")).get("date")),
        committed_at=_timestamp(_object(commit.get("committer")).get("date")),
        message=_text(commit.get("message")),
        parent_shas=parents,
        is_merge_commit=len(parents) > 1 if parents is not None else None,
        additions_count=None,
        deletions_count=None,
        html_url=payload.get("html_url"),
    )


def _extract_issue(payload: dict[str, Any], repo_id: str) -> GitHubIssue:
    author_id, author_login = _user(payload.get("user"))
    return GitHubIssue(
        repo_id=repo_id,
        issue_id=_native_id(payload["id"]),
        issue_number=payload["number"],
        title=payload["title"],
        state_at_retrieval=payload["state"],
        created_at=_timestamp(payload["created_at"]),
        author_github_id=author_id,
        author_login=author_login,
        body=_text(payload.get("body")),
        state_reason_at_retrieval=_text(payload.get("state_reason")),
        updated_at=_timestamp(payload.get("updated_at")),
        closed_at=_timestamp(payload.get("closed_at")),
        comments_count=_count(payload.get("comments")),
        label_names=_object_values(payload.get("labels"), "name"),
        html_url=payload.get("html_url"),
    )


def _extract_pull_request(payload: dict[str, Any], repo_id: str) -> GitHubPullRequest:
    author_id, author_login = _user(payload.get("user"))
    merged_at = _timestamp(payload.get("merged_at"))
    merged = payload.get("merged")
    if merged is not None and type(merged) is not bool:
        raise ValueError("Native merged flag must be a boolean")
    if merged is not None and merged != (merged_at is not None):
        raise ValueError("Native merged flag contradicts merge timestamp")
    return GitHubPullRequest(
        repo_id=repo_id,
        pr_id=_native_id(payload["id"]),
        pr_number=payload["number"],
        title=payload["title"],
        state_at_retrieval=payload["state"],
        created_at=_timestamp(payload["created_at"]),
        author_github_id=author_id,
        author_login=author_login,
        body=_text(payload.get("body")),
        is_merged=True if merged_at is not None else merged,
        is_draft_at_retrieval=payload.get("draft"),
        updated_at=_timestamp(payload.get("updated_at")),
        closed_at=_timestamp(payload.get("closed_at")),
        merged_at=merged_at,
        head_sha=_object(payload.get("head")).get("sha"),
        base_sha=_object(payload.get("base")).get("sha"),
        comments_count=_count(payload.get("comments")),
        review_comments_count=_count(payload.get("review_comments")),
        label_names=_object_values(payload.get("labels"), "name"),
        html_url=payload.get("html_url"),
    )


def _extract_contributor(
    payload: dict[str, Any], repo_id: str, observed_at: str
) -> GitHubContributorObservation:
    numeric_id, login = _user(payload)
    user_type = _text(payload.get("type"))
    anonymous = user_type == "Anonymous"
    if anonymous and (numeric_id is not None or login is not None):
        raise ValueError("Anonymous contributor contradicts native account identity")
    if not anonymous and numeric_id is None:
        raise ValueError("Contributor identity is neither known nor verified anonymous")
    return GitHubContributorObservation(
        repo_id=repo_id,
        is_anonymous=anonymous,
        contributions_count=_count(payload["contributions"]),
        observed_at=observed_at,
        contributor_github_id=numeric_id,
        login=login,
        user_type=user_type,
        html_url=payload.get("html_url"),
    )


def _extract_release(payload: dict[str, Any], repo_id: str) -> GitHubRelease:
    author_id, author_login = _user(payload.get("author"))
    assets = payload.get("assets")
    if assets is not None and (
        not isinstance(assets, list) or any(not isinstance(item, dict) for item in assets)
    ):
        raise ValueError("Native assets must be a list of objects")
    return GitHubRelease(
        repo_id=repo_id,
        release_id=_native_id(payload["id"]),
        tag_name=payload["tag_name"],
        is_draft=payload["draft"],
        is_prerelease=payload["prerelease"],
        name=_text(payload.get("name")),
        body=_text(payload.get("body")),
        author_github_id=author_id,
        author_login=author_login,
        created_at=_timestamp(payload.get("created_at")),
        published_at=_timestamp(payload.get("published_at")),
        target_commitish=_text(payload.get("target_commitish")),
        assets_count=None,
        html_url=payload.get("html_url"),
    )


def _error(
    code: str, message: str, *, source_record_id: str | None = None, retryable: bool = False
) -> DataError:
    return DataError(
        code=code,
        message=message,
        retryable=retryable,
        source_record_id=source_record_id,
        raw_record_id=None,
    )


def _http_error(error: HTTPClientError) -> DataError:
    status = error.status_code
    if isinstance(error, RateLimitExceededError) or status == 429:
        code = "rate_limited"
    elif status == 404:
        code = "not_found"
    elif status is None:
        code = "network_error"
    elif 200 <= status < 300:
        code = "parse_error"
    else:
        code = "http_error"
    return _error(
        code,
        "GitHub activity request failed",
        retryable=code in {"rate_limited", "network_error"} or status in RETRYABLE_STATUS_CODES,
    )


def _safe_source_id(group: ActivityGroup, payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if group == "commits":
        sha = payload.get("sha")
        return "commit:" + sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None
    numeric_id = payload.get("id")
    if type(numeric_id) is not int or numeric_id <= 0:
        return None
    namespace = {
        "issues": "issue",
        "pull_requests": "pull_request",
        "contributors": "contributor",
        "releases": "release",
    }[group]
    return f"{namespace}:{numeric_id}"


def _cursor(value: str | None) -> tuple[int, int]:
    if value is None:
        return 1, 0
    match = re.fullmatch(r"p:([1-9][0-9]*):i:(0|[1-9][0-9]?)", value)
    if match is None:
        raise ValueError("Invalid activity page/offset cursor")
    return int(match[1]), int(match[2])


class GitHubActivityContractCollector:
    """One verified repository/group, with explicit budgets and caller-owned continuation."""

    def __init__(
        self,
        group: ActivityGroup,
        repository: GitHubRepository,
        client: GitHubClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if group not in ("commits", "issues", "pull_requests", "contributors", "releases"):
            raise ValueError("Unsupported GitHub activity group")
        self.group = group
        self.repository = repository
        self.client = client if client is not None else GitHubClient()
        self._owns_client = client is None
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
            self._owns_client = False

    def _raw(
        self, payload: dict[str, Any], request: CollectionRequest, source_url: str, retrieved: str
    ) -> RawRecord:
        repo_id = self.repository.identity.repo_id
        if self.group == "commits":
            entity = _extract_commit(payload, repo_id)
            source_id, event_at = "commit:" + entity.commit_sha, entity.committed_at
        elif self.group == "issues":
            entity = _extract_issue(payload, repo_id)
            source_id, event_at = "issue:" + entity.issue_id, entity.created_at
        elif self.group == "pull_requests":
            entity = _extract_pull_request(payload, repo_id)
            source_id, event_at = "pull_request:" + entity.pr_id, entity.created_at
        elif self.group == "contributors":
            entity = _extract_contributor(payload, repo_id, retrieved)
            source_id = (
                "contributor:" + entity.contributor_github_id
                if entity.contributor_github_id is not None
                else None
            )
            event_at = None
        else:
            entity = _extract_release(payload, repo_id)
            source_id = "release:" + entity.release_id
            event_at = entity.published_at if not entity.is_draft else None
        raw = RawRecord(
            schema_version=SCHEMA_VERSION,
            record_type="raw_record",
            raw_record_id="raw:" + str(uuid4()),
            run_id=request.run_id,
            source="github_rest",
            source_record_type="repository_activity",
            source_record_id=source_id,
            repo_id=repo_id,
            package_id=None,
            source_url=source_url,
            retrieved_at=retrieved,
            event_at=event_at,
            available_at=None,
            http_status=None,
            raw_body_sha256=None,
            payload=payload,
        )
        return validate_record(raw)

    def collect(self, request: CollectionRequest) -> CollectionResult:
        request = validate_record(request)
        if not isinstance(request, CollectionRequest):
            raise TypeError("Expected CollectionRequest")
        started = format_utc_timestamp(self.clock())
        records: list[RawRecord] = []
        errors: list[DataError] = []
        pages = rejected = 0
        complete = False
        next_cursor: str | None = None
        reason: MissingReason = "unavailable"
        if (
            request.source != "github_rest"
            or request.record_kinds != ["repository_activity"]
            or len(request.subjects) != 1
        ):
            errors.append(
                _error("unsupported_record_type", "Only one github_rest activity subject is supported")
            )
        elif request.time_range is not None or request.cutoff_at is not None:
            reason = "historical_unavailable"
            errors.append(
                _error("insufficient_history", "Phase 2 cannot certify historical cutoff availability")
            )
        else:
            try:
                repository = validate_record(self.repository)
                if not isinstance(repository, GitHubRepository):
                    raise ValueError("Expected canonical GitHubRepository")
                if (
                    repository.visibility != "public"
                    or repository.is_fork
                    or repository.is_template
                    or repository.mirror_url is not None
                ):
                    raise ValueError("Repository is outside the eligible public activity scope")
                subject = request.subjects[0]
                if (
                    subject.repo_id != repository.identity.repo_id
                    or subject.repo_full_name is None
                    or subject.repo_full_name.casefold()
                    != repository.identity.repo_full_name.casefold()
                ):
                    raise ValueError("Activity subject does not match verified repository context")
                page, index = _cursor(request.cursor)
                owner, repo = repository.identity.repo_full_name.split("/")
                source_url = (
                    f"{self.client.base_url.rstrip('/')}/repos/{owner}/{repo}/{_ENDPOINTS[self.group]}"
                )
                validate_http_url(source_url)
                if urlsplit(source_url).query or urlsplit(source_url).fragment:
                    raise ValueError("Activity endpoint cannot contain URL options")
                self.repository = repository
            except (ValueError, TypeError):
                reason = "parse_error"
                errors.append(
                    _error("validation_error", "Invalid repository context, subject or activity cursor")
                )
            else:
                fetch = getattr(self.client, f"list_{self.group}")
                while pages < request.max_pages and len(records) < request.max_records:
                    next_cursor = f"p:{page}:i:{index}"
                    params: dict[str, Any] = {"per_page": 100, "page": page}
                    if self.group in {"issues", "pull_requests"}:
                        params["state"] = "all"
                    elif self.group == "contributors":
                        params["anon"] = True
                    try:
                        items = fetch(owner, repo, **params)
                    except HTTPClientError as error:
                        errors.append(_http_error(error))
                        reason = "parse_error" if errors[-1].code == "parse_error" else "unavailable"
                        break
                    retrieved = format_utc_timestamp(self.clock())
                    if not isinstance(items, list) or len(items) > 100:
                        errors.append(
                            _error(
                                "parse_error",
                                "Expected a GitHub activity page of at most 100 native objects",
                            )
                        )
                        reason = "parse_error"
                        break
                    pages += 1
                    if index > len(items):
                        errors.append(_error("parse_error", "Activity cursor exceeds the returned page"))
                        reason = "parse_error"
                        break
                    while index < len(items) and len(records) < request.max_records:
                        payload = items[index]
                        index += 1
                        next_cursor = f"p:{page}:i:{index}" if index < 100 else f"p:{page + 1}:i:0"
                        if (
                            self.group == "issues"
                            and isinstance(payload, dict)
                            and "pull_request" in payload
                        ):
                            continue
                        try:
                            if not isinstance(payload, dict):
                                raise TypeError("Native item must be an object")
                            records.append(self._raw(payload, request, source_url, retrieved))
                        except (ValueError, TypeError, KeyError) as error:
                            rejected += 1
                            reason = "parse_error"
                            errors.append(
                                _error(
                                    "parse_error" if isinstance(error, TypeError) else "validation_error",
                                    "Native GitHub activity item failed validation",
                                    source_record_id=_safe_source_id(self.group, payload),
                                )
                            )
                    if index == len(items) and len(items) < 100:
                        complete = True
                        next_cursor = None
                        break
                    if index < len(items):
                        break
                    page += 1
                    index = 0
                    next_cursor = f"p:{page}:i:0"
                if not complete and not errors:
                    reason = "not_collected"
        status = "complete" if complete and not errors else "partial" if pages else "failed"
        coverage_status = "unavailable" if status == "failed" else status
        result = CollectionResult(
            schema_version=SCHEMA_VERSION,
            record_type="collection_result",
            request_id=request.request_id,
            run_id=request.run_id,
            source=request.source,
            status=status,
            records=records,
            errors=errors,
            metadata=CollectionMetadata(
                collector_name=request.collector_name,
                collector_version=SCHEMA_VERSION,
                started_at=started,
                finished_at=format_utc_timestamp(self.clock()),
                requested_time_range=request.time_range,
                pages_fetched=pages,
                records_emitted=len(records),
                records_rejected=rejected,
                pagination_complete=complete,
                next_cursor=next_cursor,
                coverage=[
                    Coverage(
                        data_group=self.group,
                        status=coverage_status,
                        time_range=request.time_range,
                        records_observed=len(records),
                        missing_reason=None if status == "complete" else reason,
                    )
                ],
                rate_limit_remaining=None,
                rate_limit_reset_at=None,
            ),
        )
        validate_collection_pair(request, result)
        return result
