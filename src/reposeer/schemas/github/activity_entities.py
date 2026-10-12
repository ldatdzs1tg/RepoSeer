"""Strict internal GitHub entities for T-014 extraction and future T-018 normalization.

These models are not canonical T-011 exchange records. Native provider JSON remains
in RawRecord.payload; extraction supplies explicit keys and canonical timestamps.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reposeer.contracts.models import Count, DecimalId, HttpUrl, NonEmpty, RepoId, UtcTimestamp

GitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class _ActivityEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class GitHubCommit(_ActivityEntity):
    """Commit identity and source dates; committed_at is not public push time."""

    repo_id: RepoId
    commit_sha: GitSha
    author_github_id: DecimalId | None
    author_login: NonEmpty | None
    committer_github_id: DecimalId | None
    committer_login: NonEmpty | None
    authored_at: UtcTimestamp | None
    committed_at: UtcTimestamp | None
    message: NonEmpty | None
    parent_shas: list[GitSha] | None
    is_merge_commit: bool | None
    additions_count: Count | None
    deletions_count: Count | None
    html_url: HttpUrl | None

    @model_validator(mode="after")
    def validate_parents(self) -> Self:
        if self.parent_shas is None:
            if self.is_merge_commit is not None:
                raise ValueError("Unknown parent_shas requires is_merge_commit=null")
        else:
            if len(self.parent_shas) != len(set(self.parent_shas)):
                raise ValueError("parent_shas must not contain duplicates")
            if self.is_merge_commit is not (len(self.parent_shas) > 1):
                raise ValueError("is_merge_commit must match the known parent count")
        return self


class GitHubIssue(_ActivityEntity):
    """Issue observation; current state and labels do not establish historical state."""

    repo_id: RepoId
    issue_id: DecimalId
    issue_number: Annotated[int, Field(ge=1, le=9007199254740991)]
    title: NonEmpty
    state_at_retrieval: Literal["open", "closed"]
    created_at: UtcTimestamp
    author_github_id: DecimalId | None
    author_login: NonEmpty | None
    body: NonEmpty | None
    state_reason_at_retrieval: NonEmpty | None
    updated_at: UtcTimestamp | None
    closed_at: UtcTimestamp | None
    comments_count: Count | None
    label_names: list[NonEmpty] | None
    html_url: HttpUrl | None

    @model_validator(mode="after")
    def validate_chronology(self) -> Self:
        for name in ("updated_at", "closed_at"):
            timestamp = getattr(self, name)
            if timestamp is not None and timestamp < self.created_at:
                raise ValueError(f"{name} cannot precede created_at")
        return self


class GitHubPullRequest(_ActivityEntity):
    """PR observation with explicit merge evidence; closed does not imply merged."""

    repo_id: RepoId
    pr_id: DecimalId
    pr_number: Annotated[int, Field(ge=1, le=9007199254740991)]
    title: NonEmpty
    state_at_retrieval: Literal["open", "closed"]
    created_at: UtcTimestamp
    author_github_id: DecimalId | None
    author_login: NonEmpty | None
    body: NonEmpty | None
    is_merged: bool | None
    is_draft_at_retrieval: bool | None
    updated_at: UtcTimestamp | None
    closed_at: UtcTimestamp | None
    merged_at: UtcTimestamp | None
    head_sha: GitSha | None
    base_sha: GitSha | None
    comments_count: Count | None
    review_comments_count: Count | None
    label_names: list[NonEmpty] | None
    html_url: HttpUrl | None

    @model_validator(mode="after")
    def validate_merge_and_chronology(self) -> Self:
        for name in ("updated_at", "closed_at", "merged_at"):
            timestamp = getattr(self, name)
            if timestamp is not None and timestamp < self.created_at:
                raise ValueError(f"{name} cannot precede created_at")
        if self.merged_at is not None:
            if self.is_merged is not True or self.state_at_retrieval != "closed":
                raise ValueError("merged_at requires is_merged=true and state=closed")
        elif self.is_merged is True:
            raise ValueError("is_merged=true requires merged_at")
        return self


class GitHubContributorObservation(_ActivityEntity):
    """Current contributor aggregate observed at canonical raw retrieval time."""

    repo_id: RepoId
    is_anonymous: bool
    contributions_count: Count
    observed_at: UtcTimestamp
    contributor_github_id: DecimalId | None
    login: NonEmpty | None
    user_type: NonEmpty | None
    html_url: HttpUrl | None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.is_anonymous:
            if self.contributor_github_id is not None or self.login is not None:
                raise ValueError("Anonymous contributors require null GitHub ID and login")
        elif self.contributor_github_id is None:
            raise ValueError("Known contributors require a GitHub numeric ID")
        return self


class GitHubRelease(_ActivityEntity):
    """GitHub release metadata; neither all Git tags nor PyPI package releases."""

    repo_id: RepoId
    release_id: DecimalId
    tag_name: NonEmpty
    is_draft: bool
    is_prerelease: bool
    name: NonEmpty | None
    body: NonEmpty | None
    author_github_id: DecimalId | None
    author_login: NonEmpty | None
    created_at: UtcTimestamp | None
    published_at: UtcTimestamp | None
    target_commitish: NonEmpty | None
    assets_count: Count | None
    html_url: HttpUrl | None
