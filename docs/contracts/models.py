"""Canonical contract. Change here, then run scripts/export_contract.py.

All keys are required, including nullable fields. extra='forbid' prevents silent
field-name drift. No aliases and no coercion at module boundaries.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from typing_extensions import TypeAliasType

from shared.identity import (
    FULL_NAME_PATTERN, UTC_PATTERN, add_calendar_months, article_id,
    canonical_article_url, canonical_github_url, checked_timestamp,
    normalize_article_text, repo_id, text_sha256, validate_http_url,
)

SCHEMA_VERSION = "0.1.0"
JsonValue = TypeAliasType("JsonValue", "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]")
SchemaVersion: TypeAlias = Literal["0.1.0"]
UtcTimestamp = Annotated[str, Field(pattern=UTC_PATTERN, json_schema_extra={"format": "date-time"}), AfterValidator(checked_timestamp)]
HttpUrl = Annotated[str, Field(pattern=r"^https?://\S+$", json_schema_extra={"format": "uri"}), AfterValidator(validate_http_url)]
NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]
DecimalId = Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
RepoId = Annotated[str, Field(pattern=r"^github:repository:[1-9][0-9]*$")]
PackageId = Annotated[str, Field(pattern=r"^pypi:[a-z0-9]+(?:-[a-z0-9]+)*$")]
TechnologyId = Annotated[str, Field(pattern=r"^tech:[a-z0-9]+(?:-[a-z0-9]+)*$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
UuidText = Annotated[str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")]
RawRecordId = Annotated[str, Field(pattern=r"^raw:[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")]
Count = Annotated[int, Field(ge=0, le=9007199254740991)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
Source: TypeAlias = Literal["github_rest", "github_graphql", "pypi_json", "pypi_index", "pypi_bigquery", "gh_archive", "deps_dev", "osv", "project_docs", "web_article", "source_archive", "technology_catalog"]
RecordKind: TypeAlias = Literal["github_repository", "package_release", "repository_activity", "dependency_metadata", "article", "technology_entity", "security_advisory"]
RunStatus: TypeAlias = Literal["complete", "partial", "failed"]
MissingReason: TypeAlias = Literal["not_in_source", "not_collected", "unavailable", "parse_error", "not_applicable", "ambiguous", "historical_unavailable"]
DataGroup: TypeAlias = Literal["repo_identity", "releases", "commits", "contributors", "issues", "pull_requests", "dependencies", "articles", "technologies", "security_advisories"]


def unique(values: list, field: str, *, casefold: bool = False) -> None:
    keys = [v.casefold() if casefold and isinstance(v, str) else v for v in values]
    if len(keys) != len(set(keys)):
        raise ValueError(field + " must not contain duplicates")


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class TimeRange(ContractModel):
    """Half-open historical window (start_at, end_at]."""
    start_at: UtcTimestamp
    end_at: UtcTimestamp

    @model_validator(mode="after")
    def ordered(self):
        if self.start_at >= self.end_at:
            raise ValueError("time range requires start_at < end_at")
        return self


class MissingField(ContractModel):
    field_path: NonEmpty
    reason: MissingReason
    detail: NonEmpty | None


class Provenance(ContractModel):
    source: Source
    source_url: HttpUrl
    retrieved_at: UtcTimestamp
    valid_at: UtcTimestamp
    available_at: UtcTimestamp | None
    temporal_basis: Literal["observation", "historical_reconstruction"]
    raw_record_ids: Annotated[list[RawRecordId], Field(min_length=1)]
    missing_fields: list[MissingField]

    @model_validator(mode="after")
    def check(self):
        unique(self.raw_record_ids, "raw_record_ids")
        unique([x.field_path for x in self.missing_fields], "missing_fields.field_path")
        if self.valid_at > self.retrieved_at:
            raise ValueError("valid_at cannot be later than retrieved_at")
        if self.available_at is not None and self.available_at > self.retrieved_at:
            raise ValueError("available_at cannot be later than retrieved_at")
        if self.temporal_basis == "observation" and self.valid_at != self.retrieved_at:
            raise ValueError("observation valid_at must equal retrieved_at")
        return self


class RepoIdentity(ContractModel):
    repo_id: RepoId
    github_repo_id: DecimalId
    github_node_id: NonEmpty | None
    repo_full_name: Annotated[str, Field(pattern=FULL_NAME_PATTERN)]
    repo_url: HttpUrl
    known_urls: list[HttpUrl]

    @model_validator(mode="after")
    def consistent(self):
        if self.repo_id != repo_id(self.github_repo_id):
            raise ValueError("repo_id does not match github_repo_id")
        if self.repo_url != canonical_github_url(self.repo_full_name):
            raise ValueError("repo_url must be canonical https://github.com/owner/repository")
        unique(self.known_urls, "known_urls")
        return self


class RawRecord(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["raw_record"]
    raw_record_id: RawRecordId
    run_id: UuidText
    source: Source
    source_record_type: RecordKind
    source_record_id: NonEmpty | None
    repo_id: RepoId | None
    package_id: PackageId | None
    source_url: HttpUrl
    retrieved_at: UtcTimestamp
    event_at: UtcTimestamp | None
    available_at: UtcTimestamp | None
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    raw_body_sha256: Sha256 | None
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def check(self):
        import json
        json.dumps(self.payload, allow_nan=False)
        if self.available_at is not None and self.available_at > self.retrieved_at:
            raise ValueError("available_at cannot be later than retrieved_at")
        if self.event_at is not None and self.event_at > self.retrieved_at:
            raise ValueError("event_at cannot be later than retrieved_at")
        return self


class LanguageBytes(ContractModel):
    language: NonEmpty
    bytes_count: Count


class GitHubRepository(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["github_repository"]
    identity: RepoIdentity
    description: NonEmpty | None
    visibility: Literal["public", "private", "internal"]
    is_fork: bool
    is_template: bool
    is_archived: bool
    mirror_url: HttpUrl | None
    primary_language: NonEmpty | None
    language_bytes: list[LanguageBytes] | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    pushed_at: UtcTimestamp | None
    stargazers_count: Count | None
    forks_count: Count | None
    open_issues_and_prs_count: Count | None
    provenance: Provenance

    @model_validator(mode="after")
    def check(self):
        if self.created_at > self.updated_at or self.updated_at > self.provenance.retrieved_at:
            raise ValueError("require created_at <= updated_at <= retrieved_at")
        if self.pushed_at is not None and self.pushed_at > self.provenance.retrieved_at:
            raise ValueError("pushed_at cannot be later than retrieved_at")
        if self.language_bytes is not None:
            unique([x.language for x in self.language_bytes], "language_bytes.language", casefold=True)
        return self


class TechnologyEntity(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["technology_entity"]
    technology_id: TechnologyId
    slug: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    name: NonEmpty
    category: Literal["language", "framework", "library", "runtime", "database", "developer_tool", "platform", "standard"]
    aliases: list[NonEmpty]
    description: NonEmpty | None
    official_urls: Annotated[list[HttpUrl], Field(min_length=1)]
    repo_ids: list[RepoId]
    package_ids: list[PackageId]
    lifecycle_status: Literal["active", "deprecated", "end_of_life", "unknown"]
    status_evidence_url: HttpUrl | None
    provenance: Provenance

    @model_validator(mode="after")
    def check(self):
        if self.technology_id != "tech:" + self.slug:
            raise ValueError("technology_id must equal tech:<slug>")
        for field in ["aliases", "official_urls", "repo_ids", "package_ids"]:
            unique(getattr(self, field), field, casefold=field == "aliases")
        if self.lifecycle_status in {"deprecated", "end_of_life"} and self.status_evidence_url is None:
            raise ValueError("deprecated/end_of_life requires status_evidence_url")
        return self


class TechnologyMention(ContractModel):
    technology_id: TechnologyId
    matched_text: NonEmpty
    confidence: Confidence
    match_method: Literal["dictionary", "manual", "model"]
    extractor_version: NonEmpty


class Article(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["article"]
    article_id: Annotated[str, Field(pattern=r"^article:sha256:[a-f0-9]{64}$")]
    original_url: HttpUrl
    canonical_url: HttpUrl
    title: NonEmpty
    article_kind: Literal["announcement", "documentation", "blog", "changelog", "security", "news", "other"]
    authors: list[NonEmpty] | None
    language_code: Annotated[str, Field(pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$")] | None
    published_at: UtcTimestamp | None
    published_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", json_schema_extra={"format": "date"})] | None
    published_precision: Literal["timestamp", "date", "unknown"]
    updated_at: UtcTimestamp | None
    content_status: Literal["full", "snippet", "unavailable"]
    content_text: NonEmpty | None
    content_sha256: Sha256 | None
    content_available_at: UtcTimestamp | None
    technology_mentions: list[TechnologyMention]
    repo_ids: list[RepoId]
    package_ids: list[PackageId]
    provenance: Provenance

    @model_validator(mode="after")
    def check(self):
        if canonical_article_url(self.canonical_url) != self.canonical_url:
            raise ValueError("canonical_url is not normalized")
        if self.article_id != article_id(self.canonical_url):
            raise ValueError("article_id must be SHA-256 of canonical_url UTF-8 bytes")
        if self.published_precision == "timestamp":
            if self.published_at is None or self.published_date != self.published_at[:10]:
                raise ValueError("timestamp precision requires published_at and matching UTC published_date")
        elif self.published_precision == "date":
            if self.published_at is not None or self.published_date is None:
                raise ValueError("date precision requires published_date and published_at=null")
        elif self.published_at is not None or self.published_date is not None:
            raise ValueError("unknown precision requires both publication fields null")
        if self.published_date is not None:
            date.fromisoformat(self.published_date)
            if self.published_date > self.provenance.retrieved_at[:10]:
                raise ValueError("published_date cannot be in the future")
        if self.published_at is not None and self.published_at > self.provenance.retrieved_at:
            raise ValueError("published_at cannot be later than retrieved_at")
        if self.updated_at is not None:
            if self.updated_at > self.provenance.retrieved_at:
                raise ValueError("updated_at cannot be later than retrieved_at")
            if self.published_at is not None and self.updated_at < self.published_at:
                raise ValueError("updated_at cannot precede published_at")
        if self.content_status == "unavailable":
            if self.content_text is not None or self.content_sha256 is not None or self.content_available_at is not None:
                raise ValueError("unavailable content requires null text/hash/availability")
        else:
            if self.content_text is None or self.content_sha256 is None:
                raise ValueError("full/snippet content requires text and hash")
            if normalize_article_text(self.content_text) != self.content_text:
                raise ValueError("content_text must use NFC/LF and have outer whitespace stripped")
            if text_sha256(self.content_text) != self.content_sha256:
                raise ValueError("content_sha256 does not match content_text")
        if self.content_available_at is not None and self.content_available_at > self.provenance.retrieved_at:
            raise ValueError("content_available_at cannot be later than retrieved_at")
        for field in ["repo_ids", "package_ids"]:
            unique(getattr(self, field), field)
        unique([x.technology_id for x in self.technology_mentions], "technology_mentions.technology_id")
        return self


class DependencyRequirement(ContractModel):
    package_id: PackageId
    version_specifier: str
    environment_marker: NonEmpty | None
    extras: list[NonEmpty]
    dependency_kind: Literal["runtime", "optional", "build", "development"]

    @model_validator(mode="after")
    def check(self):
        unique(self.extras, "extras")
        return self


class PackageRelease(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["package_release"]
    package_id: PackageId
    repo_id: RepoId | None
    version: NonEmpty
    uploaded_at: UtcTimestamp
    requires_python: NonEmpty | None
    dependencies: list[DependencyRequirement] | None
    is_yanked: bool | None
    yanked_reason: NonEmpty | None
    yanked_status_at: UtcTimestamp | None
    provenance: Provenance

    @model_validator(mode="after")
    def check(self):
        if self.uploaded_at > self.provenance.retrieved_at:
            raise ValueError("uploaded_at cannot be later than retrieved_at")
        if self.is_yanked is None and (self.yanked_reason is not None or self.yanked_status_at is not None):
            raise ValueError("unknown yanked state requires null reason/status_at")
        if self.is_yanked is not None and self.yanked_status_at is None:
            raise ValueError("known yanked state requires yanked_status_at")
        if self.yanked_status_at is not None and self.yanked_status_at > self.provenance.retrieved_at:
            raise ValueError("yanked_status_at cannot be later than retrieved_at")
        return self


class RepositoryActivity(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["repository_activity"]
    activity_id: Annotated[str, Field(pattern=r"^github:activity:[1-9][0-9]*:(?:commit|issue_opened|issue_closed|issue_reopened|pr_opened|pr_closed|pr_reopened|pr_merged|comment):[A-Za-z0-9._-]+$")]
    repo_id: RepoId
    activity_kind: Literal["commit", "issue_opened", "issue_closed", "issue_reopened", "pr_opened", "pr_closed", "pr_reopened", "pr_merged", "comment"]
    source_object_id: NonEmpty
    event_at: UtcTimestamp
    actor_id: DecimalId | None
    actor_is_bot: bool | None
    provenance: Provenance

    @model_validator(mode="after")
    def check(self):
        if self.activity_id.split(":")[2] != self.repo_id.split(":")[-1]:
            raise ValueError("activity_id repository component does not match repo_id")
        if self.activity_id.split(":")[3] != self.activity_kind:
            raise ValueError("activity_id kind component does not match activity_kind")
        if self.event_at > self.provenance.retrieved_at:
            raise ValueError("event_at cannot be later than retrieved_at")
        return self


NormalizedRecord: TypeAlias = Annotated[GitHubRepository | TechnologyEntity | Article | PackageRelease | RepositoryActivity, Field(discriminator="record_type")]


class CollectionSubject(ContractModel):
    repo_id: RepoId | None
    repo_full_name: Annotated[str, Field(pattern=FULL_NAME_PATTERN)] | None
    package_id: PackageId | None
    url: HttpUrl | None

    @model_validator(mode="after")
    def target_required(self):
        if all(x is None for x in [self.repo_id, self.repo_full_name, self.package_id, self.url]):
            raise ValueError("a collection subject needs at least one identifier")
        return self


class CollectionRequest(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["collection_request"]
    request_id: UuidText
    run_id: UuidText
    collector_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    source: Source
    subjects: Annotated[list[CollectionSubject], Field(min_length=1, max_length=1000)]
    record_kinds: Annotated[list[RecordKind], Field(min_length=1)]
    time_range: TimeRange | None
    cutoff_at: UtcTimestamp | None
    cursor: NonEmpty | None
    max_pages: Annotated[int, Field(ge=1, le=10000)]
    max_records: Annotated[int, Field(ge=1, le=1000000)]

    @model_validator(mode="after")
    def check(self):
        unique(self.record_kinds, "record_kinds")
        if self.time_range is not None and self.cutoff_at is not None and self.time_range.end_at > self.cutoff_at:
            raise ValueError("time_range cannot end later than cutoff_at")
        return self


class Coverage(ContractModel):
    data_group: DataGroup
    status: Literal["complete", "partial", "unavailable", "not_applicable"]
    time_range: TimeRange | None
    records_observed: Count
    missing_reason: MissingReason | None

    @model_validator(mode="after")
    def check(self):
        if self.status in {"partial", "unavailable", "not_applicable"} and self.missing_reason is None:
            raise ValueError("non-complete coverage requires missing_reason")
        if self.status == "complete" and self.missing_reason is not None:
            raise ValueError("complete coverage requires missing_reason=null")
        if self.status in {"unavailable", "not_applicable"} and self.records_observed != 0:
            raise ValueError("unavailable/not_applicable cannot report observed records")
        return self


class DataError(ContractModel):
    code: Literal["network_error", "http_error", "rate_limited", "not_found", "not_mapped", "unsupported_record_type", "validation_error", "parse_error", "insufficient_history", "schema_version_mismatch"]
    message: NonEmpty
    retryable: bool
    source_record_id: NonEmpty | None
    raw_record_id: RawRecordId | None


class CollectionMetadata(ContractModel):
    collector_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    collector_version: NonEmpty
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    requested_time_range: TimeRange | None
    pages_fetched: Count
    records_emitted: Count
    records_rejected: Count
    pagination_complete: bool
    next_cursor: NonEmpty | None
    coverage: Annotated[list[Coverage], Field(min_length=1)]
    rate_limit_remaining: Count | None
    rate_limit_reset_at: UtcTimestamp | None

    @model_validator(mode="after")
    def check(self):
        if self.started_at > self.finished_at:
            raise ValueError("started_at cannot be later than finished_at")
        if self.pagination_complete and self.next_cursor is not None:
            raise ValueError("complete pagination requires next_cursor=null")
        unique([x.data_group for x in self.coverage], "coverage.data_group")
        return self


class CollectionResult(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["collection_result"]
    request_id: UuidText
    run_id: UuidText
    source: Source
    status: RunStatus
    records: list[RawRecord]
    errors: list[DataError]
    metadata: CollectionMetadata

    @model_validator(mode="after")
    def check(self):
        unique([x.raw_record_id for x in self.records], "records.raw_record_id")
        if self.metadata.records_emitted != len(self.records):
            raise ValueError("records_emitted must equal len(records)")
        for record in self.records:
            if record.run_id != self.run_id or record.source != self.source:
                raise ValueError("nested raw record run_id/source mismatch")
            if not self.metadata.started_at <= record.retrieved_at <= self.metadata.finished_at:
                raise ValueError("raw record retrieved_at must be within collection run")
        if self.status == "complete":
            if self.errors or not self.metadata.pagination_complete or self.metadata.records_rejected:
                raise ValueError("complete collection cannot have errors, rejected records or incomplete pagination")
            if any(x.status in {"partial", "unavailable"} for x in self.metadata.coverage):
                raise ValueError("complete collection cannot report partial/unavailable coverage")
        if self.status == "failed" and (self.records or not self.errors):
            raise ValueError("failed collection needs errors and no emitted records")
        if self.status == "partial" and not (self.errors or not self.metadata.pagination_complete or self.metadata.records_rejected or any(x.status in {"partial", "unavailable"} for x in self.metadata.coverage)):
            raise ValueError("partial collection needs an explicit reason")
        return self


class NormalizationRequest(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["normalization_request"]
    request_id: UuidText
    run_id: UuidText
    records: list[RawRecord]

    @model_validator(mode="after")
    def check(self):
        unique([x.raw_record_id for x in self.records], "records.raw_record_id")
        if any(x.run_id != self.run_id for x in self.records):
            raise ValueError("raw records must belong to the request run_id")
        return self


class NormalizationResult(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["normalization_result"]
    request_id: UuidText
    run_id: UuidText
    normalizer_version: NonEmpty
    status: RunStatus
    input_record_count: Count
    processed_record_count: Count
    rejected_record_count: Count
    output_record_count: Count
    records: list[NormalizedRecord]
    errors: list[DataError]

    @model_validator(mode="after")
    def check(self):
        if self.input_record_count != self.processed_record_count + self.rejected_record_count:
            raise ValueError("input count must equal processed + rejected")
        if self.output_record_count != len(self.records):
            raise ValueError("output count must equal len(records)")
        if self.status == "complete" and (self.rejected_record_count or self.errors):
            raise ValueError("complete normalization cannot contain rejection/errors")
        if self.status == "failed" and (self.records or self.processed_record_count or not self.errors):
            raise ValueError("failed normalization requires no output/processed input and an error")
        if self.status == "partial" and not (self.rejected_record_count or self.errors):
            raise ValueError("partial normalization requires rejection or an error")
        return self


class TechnologyLinkRequest(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["technology_link_request"]
    request_id: UuidText
    run_id: UuidText
    articles: list[Article]
    technology_entities: list[TechnologyEntity]

    @model_validator(mode="after")
    def check(self):
        unique([x.article_id for x in self.articles], "articles.article_id")
        unique([x.technology_id for x in self.technology_entities], "technology_entities.technology_id")
        return self


class TechnologyLinkResult(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["technology_link_result"]
    request_id: UuidText
    run_id: UuidText
    linker_version: NonEmpty
    status: RunStatus
    articles: list[Article]
    errors: list[DataError]

    @model_validator(mode="after")
    def check(self):
        unique([x.article_id for x in self.articles], "articles.article_id")
        if self.status == "complete" and self.errors:
            raise ValueError("complete linking cannot contain errors")
        if self.status == "failed" and (self.articles or not self.errors):
            raise ValueError("failed linking requires errors and no articles")
        if self.status == "partial" and not self.errors:
            raise ValueError("partial linking requires an error")
        return self


class SnapshotFeatures(ContractModel):
    package_age_days: Count | None
    releases_total: Count | None
    days_since_latest_release: Count | None
    release_count_3m: Count | None
    release_count_6m: Count | None
    release_count_12m: Count | None
    commit_count_3m: Count | None
    commit_count_6m: Count | None
    commit_count_12m: Count | None
    contributor_count_12m: Count | None
    issue_opened_count_12m: Count | None
    issue_closed_count_12m: Count | None
    pr_opened_count_12m: Count | None
    pr_merged_count_12m: Count | None

    @model_validator(mode="after")
    def check(self):
        for prefix in ["release_count", "commit_count"]:
            values = [getattr(self, prefix + "_" + suffix) for suffix in ["3m", "6m", "12m"]]
            known = [x for x in values if x is not None]
            if known != sorted(known):
                raise ValueError(prefix + " must be non-decreasing across 3m/6m/12m")
        if self.releases_total is not None and self.release_count_12m is not None and self.releases_total < self.release_count_12m:
            raise ValueError("releases_total cannot be smaller than release_count_12m")
        return self


class PackageSnapshot(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["package_snapshot"]
    package_id: PackageId
    repo_id: RepoId
    repo_group_id: RepoId
    cutoff_at: UtcTimestamp
    horizon_months: Literal[12]
    horizon_end_at: UtcTimestamp
    features: SnapshotFeatures
    coverage: list[Coverage]
    missing_fields: list[MissingField]
    raw_record_ids: list[RawRecordId]

    @model_validator(mode="after")
    def check(self):
        if self.repo_group_id != self.repo_id:
            raise ValueError("repo_group_id must equal repo_id in v0.1")
        if self.horizon_end_at != add_calendar_months(self.cutoff_at, 12):
            raise ValueError("horizon_end_at must be cutoff_at + 12 calendar months")
        unique(self.raw_record_ids, "raw_record_ids")
        unique([x.data_group for x in self.coverage], "coverage.data_group")
        unique([x.field_path for x in self.missing_fields], "missing_fields.field_path")
        missing_paths = {x.field_path for x in self.missing_fields}
        expected_missing = {"features." + name for name, value in self.features.model_dump().items() if value is None}
        if missing_paths != expected_missing:
            raise ValueError("missing_fields must exactly describe null feature fields")
        for name, value in self.features.model_dump().items():
            if value is None and "features." + name not in missing_paths:
                raise ValueError("null feature needs missing_fields reason: " + name)
        return self


class SnapshotBuildRequest(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["snapshot_build_request"]
    request_id: UuidText
    run_id: UuidText
    package_id: PackageId
    repository: RepoIdentity
    cutoff_at: UtcTimestamp
    horizon_months: Literal[12]
    releases: list[PackageRelease]
    activities: list[RepositoryActivity]
    coverage: list[Coverage]

    @model_validator(mode="after")
    def check(self):
        unique([x.activity_id for x in self.activities], "activities.activity_id")
        unique([x.version for x in self.releases], "releases.version")
        unique([x.data_group for x in self.coverage], "coverage.data_group")
        for release in self.releases:
            if release.package_id != self.package_id or release.uploaded_at > self.cutoff_at:
                raise ValueError("release package/time mismatch")
            if release.repo_id is not None and release.repo_id != self.repository.repo_id:
                raise ValueError("release repo_id mismatch")
            if release.yanked_status_at is not None and release.yanked_status_at > self.cutoff_at:
                raise ValueError("yanked state observed after cutoff is not historical input")
        for activity in self.activities:
            if activity.repo_id != self.repository.repo_id or activity.event_at > self.cutoff_at:
                raise ValueError("activity repo/time mismatch")
        for record in [*self.releases, *self.activities]:
            if record.provenance.available_at is None or record.provenance.available_at > self.cutoff_at:
                raise ValueError("historical feature input requires known available_at <= cutoff_at")
            if record.provenance.valid_at > self.cutoff_at:
                raise ValueError("record valid_at is later than cutoff_at")
        return self


class SnapshotBuildResult(ContractModel):
    schema_version: SchemaVersion
    record_type: Literal["snapshot_build_result"]
    request_id: UuidText
    run_id: UuidText
    builder_version: NonEmpty
    status: RunStatus
    snapshot: PackageSnapshot | None
    errors: list[DataError]

    @model_validator(mode="after")
    def check(self):
        if self.status == "failed" and (self.snapshot is not None or not self.errors):
            raise ValueError("failed snapshot build requires null snapshot and errors")
        if self.status != "failed" and self.snapshot is None:
            raise ValueError("successful/partial snapshot build requires snapshot")
        if self.status == "complete" and (self.errors or self.snapshot.missing_fields):
            raise ValueError("complete snapshot build requires no errors/missing features")
        if self.status == "partial" and not (self.errors or self.snapshot.missing_fields):
            raise ValueError("partial snapshot build requires missing feature or error")
        return self


ContractRecord: TypeAlias = Annotated[
    RawRecord | GitHubRepository | TechnologyEntity | Article | PackageRelease |
    RepositoryActivity | CollectionRequest | CollectionResult | NormalizationRequest |
    NormalizationResult | TechnologyLinkRequest | TechnologyLinkResult |
    PackageSnapshot | SnapshotBuildRequest | SnapshotBuildResult,
    Field(discriminator="record_type"),
]
CONTRACT_ADAPTER = TypeAdapter(ContractRecord)


class Collector(Protocol):
    def collect(self, request: CollectionRequest) -> CollectionResult: ...


class Normalizer(Protocol):
    def normalize(self, request: NormalizationRequest) -> NormalizationResult: ...


class TechnologyLinker(Protocol):
    def link(self, request: TechnologyLinkRequest) -> TechnologyLinkResult: ...


class SnapshotBuilder(Protocol):
    def build(self, request: SnapshotBuildRequest) -> SnapshotBuildResult: ...
