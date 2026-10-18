"""GitHub normalization transformer using Polars."""

import re
from typing import Literal
from urllib.parse import urlsplit

import polars as pl
from pydantic import BaseModel, ConfigDict

from reposeer.contracts import (
    DataError,
    NormalizationRequest,
    NormalizationResult,
    Provenance,
    RawRecord,
    RepositoryActivity,
    validate_normalization_pair,
    validate_record,
)
from reposeer.contracts.adapters import GitHubRepositoryContractNormalizer
from reposeer.contracts.models import NormalizedRecord, RawRecordId, UtcTimestamp, UuidText
from reposeer.schemas.github.activity_entities import (
    GitHubCommit,
    GitHubContributorObservation,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRelease,
)
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.services.activity_service import extract_activity_entity


class GitHubNormalizer:
    """Transforms raw GitHub records into structured normalized Polars DataFrames."""

    def normalize(self, request: NormalizationRequest) -> NormalizationResult:
        """Canonical repository facade; retain the established T-013 mapping."""
        return GitHubRepositoryContractNormalizer().normalize(request)

    def to_dataframe(self, records: list[RepositoryRecord] | list[dict]) -> pl.DataFrame:
        if not records:
            return pl.DataFrame()
        dicts = [r.model_dump() if hasattr(r, "model_dump") else r for r in records]
        df = pl.DataFrame(dicts)
        return df


ActivityEntity = GitHubCommit | GitHubIssue | GitHubPullRequest | GitHubContributorObservation | GitHubRelease
NORMALIZER_VERSION = "0.1.0-t018"


class InternalActivityObservation(BaseModel):
    """T-018 sidecar, never part of the T-011 NormalizedRecord union."""
    model_config = ConfigDict(strict=True, extra="forbid")
    raw_record_id: RawRecordId
    run_id: UuidText
    retrieved_at: UtcTimestamp
    entity: ActivityEntity
    outcome_code: Literal["canonical_events", "contributor_aggregate", "github_release", "event_time_unknown"]


class ActivityNormalization(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    canonical_result: NormalizationResult
    internal_entities: list[InternalActivityObservation]


def normalization_error(raw: RawRecord, reason: str, *, code="validation_error") -> DataError:
    return DataError(code=code, message=reason, retryable=False,
                     source_record_id=raw.source_record_id, raw_record_id=raw.raw_record_id)


def normalization_result(request: NormalizationRequest, records: list[NormalizedRecord],
                         errors: list[DataError]) -> NormalizationResult:
    """Count processed input observations, independently of emitted event count."""
    processed = {identifier for record in records for identifier in record.provenance.raw_record_ids}
    result = NormalizationResult(
        schema_version="0.1.0", record_type="normalization_result",
        request_id=request.request_id, run_id=request.run_id, normalizer_version=NORMALIZER_VERSION,
        status="partial" if processed and errors else "failed" if errors else "complete",
        input_record_count=len(request.records), processed_record_count=len(processed),
        rejected_record_count=len(request.records)-len(processed), output_record_count=len(records),
        records=records, errors=errors,
    )
    validate_normalization_pair(request, result)
    return result


def observation_provenance(raw: RawRecord, missing_fields=()) -> Provenance:
    return Provenance(source=raw.source, source_url=raw.source_url,
                      retrieved_at=raw.retrieved_at, valid_at=raw.retrieved_at,
                      available_at=raw.available_at, temporal_basis="observation",
                      raw_record_ids=[raw.raw_record_id], missing_fields=list(missing_fields))


class GitHubActivityNormalizer:
    """Reuse T-014 extraction and emit only events explicitly evidenced by it."""

    def normalize(self, request: NormalizationRequest) -> NormalizationResult:
        return self.normalize_with_entities(request).canonical_result

    def normalize_with_entities(self, request: NormalizationRequest) -> ActivityNormalization:
        request = validate_record(request)
        if not isinstance(request, NormalizationRequest):
            raise TypeError("expected NormalizationRequest")
        records, entities, errors = [], [], []
        for raw in request.records:
            try:
                # Endpoint and source ID are cross-checked by the shared T-014 boundary.
                endpoint = re.search(r"/repos/[^/]+/[^/]+/(commits|issues|pulls|contributors|releases)$",
                                     urlsplit(raw.source_url).path)
                if endpoint is None:
                    raise ValueError("activity_endpoint_invalid")
                group = "pull_requests" if endpoint[1] == "pulls" else endpoint[1]
                entity = extract_activity_entity(raw, group)
                native_repository = raw.payload.get("repository")
                if isinstance(native_repository, dict) and "id" in native_repository:
                    if type(native_repository["id"]) is not int or raw.repo_id != f"github:repository:{native_repository['id']}":
                        raise ValueError("activity_repository_mismatch")
                events = []
                outcome = "canonical_events"
                if isinstance(entity, GitHubCommit):
                    if entity.committed_at is not None:
                        events.append(("commit", entity.commit_sha, entity.committed_at, entity.committer_github_id))
                    else:
                        outcome = "event_time_unknown"
                elif isinstance(entity, GitHubIssue):
                    events.append(("issue_opened", entity.issue_id, entity.created_at, entity.author_github_id))
                    if entity.closed_at is not None:
                        events.append(("issue_closed", entity.issue_id, entity.closed_at, None))
                elif isinstance(entity, GitHubPullRequest):
                    events.append(("pr_opened", entity.pr_id, entity.created_at, entity.author_github_id))
                    if entity.merged_at is not None and entity.is_merged is True:
                        events.append(("pr_merged", entity.pr_id, entity.merged_at, None))
                    elif entity.closed_at is not None and entity.is_merged is False:
                        events.append(("pr_closed", entity.pr_id, entity.closed_at, None))
                else:
                    outcome = "contributor_aggregate" if isinstance(entity, GitHubContributorObservation) else "github_release"
                emitted = [validate_record(RepositoryActivity(
                    schema_version="0.1.0", record_type="repository_activity",
                    activity_id=f"github:activity:{entity.repo_id.split(':')[-1]}:{kind}:{source_id}",
                    repo_id=entity.repo_id, activity_kind=kind, source_object_id=source_id,
                    event_at=event_at, actor_id=actor_id, actor_is_bot=None,
                    provenance=observation_provenance(raw),
                )) for kind, source_id, event_at, actor_id in events]
                # Internal timestamps describe this observation; none can be in its future.
                for key, value in entity.model_dump().items():
                    if key.endswith("_at") and value is not None and value > raw.retrieved_at:
                        raise ValueError("activity_time_after_retrieval")
                entities.append(InternalActivityObservation(
                    raw_record_id=raw.raw_record_id, run_id=raw.run_id, retrieved_at=raw.retrieved_at,
                    entity=entity, outcome_code=outcome,
                ))
                records.extend(emitted)
                if not emitted:
                    errors.append(normalization_error(raw, outcome, code="unsupported_record_type"))
            except (ValueError, TypeError, KeyError):
                errors.append(normalization_error(raw, "invalid_activity_observation"))
        return ActivityNormalization(canonical_result=normalization_result(request, records, errors),
                                     internal_entities=entities)
