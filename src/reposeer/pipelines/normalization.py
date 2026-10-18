"""Typed T-018 processing handoff; observation normalization precedes logical grouping."""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from reposeer.contracts import (
    Article,
    DataError,
    GitHubRepository,
    NormalizationRequest,
    NormalizationResult,
    RawRecord,
    RepositoryActivity,
    validate_record,
)
from reposeer.contracts.models import (
    HttpUrl,
    JsonValue,
    RawRecordId,
    Sha256,
    Source,
    UtcTimestamp,
    UuidText,
)
from reposeer.processing.normalize.external import ExternalDocumentNormalizer
from reposeer.processing.normalize.github import (
    GitHubActivityNormalizer,
    GitHubNormalizer,
    InternalActivityObservation,
    normalization_error,
    normalization_result,
)
from reposeer.processing.relevance import (
    ExternalRelevanceEvaluator,
    RelevanceAssessment,
    VerifiedTargetContext,
    validate_assessment_evidence,
)
from reposeer.schemas.github.activity_entities import (
    GitHubCommit,
    GitHubContributorObservation,
    GitHubIssue,
    GitHubPullRequest,
)
from reposeer.storage.raw.jsonl import JsonlStorage


class ProcessingModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class ObservationReference(ProcessingModel):
    raw_record_id: RawRecordId
    run_id: UuidText
    source: Source
    source_url: HttpUrl
    retrieved_at: UtcTimestamp
    content_sha256: Sha256 | None
    state_sha256: Sha256


class EntityGroup(ProcessingModel):
    logical_id: str
    raw_record_ids: list[RawRecordId]
    observations: list[ObservationReference]
    distinct_state_hashes: list[Sha256]
    primary_observation_raw_id: RawRecordId


class DocumentGroup(EntityGroup):
    article_id: str
    canonical_url: HttpUrl
    distinct_content_hashes: list[Sha256]
    near_duplicate_status: Literal["unverified"] = "unverified"


class ContentDuplicate(ProcessingModel):
    content_sha256: Sha256
    article_ids: list[str]


class SourceLineageReference(ProcessingModel):
    article_id: str
    raw_record_id: RawRecordId
    run_id: UuidText
    capture_source: str
    source_url: HttpUrl
    fetched_url: HttpUrl
    discovery_lineage_complete: bool
    metadata: dict[str, JsonValue]


class ProcessingSummary(ProcessingModel):
    input_record_count: int
    unique_raw_observation_count: int
    duplicate_input_count: int
    accepted_observation_count: int
    rejected_observation_count: int
    canonical_output_count: int
    canonical_rejected_observation_count: int
    internal_only_observation_count: int
    unique_repositories: int
    unique_activity_entities: int
    unique_documents: int
    content_revisions: int
    relevance_decisions: dict[str, int]


class NormalizationPipelineResult(ProcessingModel):
    processing_version: Literal["0.1.0-t018"] = "0.1.0-t018"
    canonical_normalization_results: list[NormalizationResult]
    internal_activity_entities: list[InternalActivityObservation]
    deduplicated_repository_view: list[EntityGroup]
    deduplicated_activity_view: list[EntityGroup]
    deduplicated_document_view: list[DocumentGroup]
    content_duplicates: list[ContentDuplicate]
    relevance_assessments: list[RelevanceAssessment]
    source_lineage_references: list[SourceLineageReference]
    processing_errors: list[DataError]
    temporal_diagnostics: dict[RawRecordId, str]
    processing_summary: ProcessingSummary

    @model_validator(mode="after")
    def verify_relevance_observations(self):
        articles = {}
        for article in self.canonical_articles:
            article = validate_record(article)
            key = (article.article_id, article.content_sha256,
                   tuple(sorted(article.provenance.raw_record_ids)))
            previous = articles.get(key)
            if previous is not None and previous.model_dump() != article.model_dump():
                raise ValueError("assessment_article_observation_conflict")
            articles[key] = article
        for assessment in self.relevance_assessments:
            key = (assessment.article_id, assessment.content_sha256,
                   tuple(sorted(assessment.raw_record_ids)))
            article = articles.get(key)
            if article is None:
                if any(item[0] == key[0] and item[2] == key[2] for item in articles):
                    raise ValueError("assessment_content_revision_mismatch")
                if any(item[:2] == key[:2] for item in articles):
                    raise ValueError("assessment_raw_reference_mismatch")
                raise ValueError("assessment_matching_article_missing")
            validate_assessment_evidence(assessment, article)
        return self

    @property
    def canonical_repositories(self) -> list[GitHubRepository]:
        return [record for result in self.canonical_normalization_results for record in result.records
                if isinstance(record, GitHubRepository)]

    @property
    def canonical_activities(self) -> list[RepositoryActivity]:
        return [record for result in self.canonical_normalization_results for record in result.records
                if isinstance(record, RepositoryActivity)]

    @property
    def canonical_articles(self) -> list[Article]:
        return [record for result in self.canonical_normalization_results for record in result.records
                if isinstance(record, Article)]


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _groups(values, raw_by_id) -> list[EntityGroup]:
    groups = []
    for logical_id, items in sorted(values.items()):
        observations = []
        for raw_id, record, content_hash in items:
            raw = raw_by_id[raw_id]
            state = record.model_dump(mode="json", exclude={"provenance"})
            observations.append(ObservationReference(
                raw_record_id=raw_id, run_id=raw.run_id, source=raw.source,
                source_url=raw.source_url, retrieved_at=raw.retrieved_at,
                content_sha256=content_hash, state_sha256=_digest(state),
            ))
        observations.sort(key=lambda item: (item.retrieved_at, item.raw_record_id))
        # Descending content quality/time, ascending stable raw ID for exact ties.
        ranked = sorted(items, key=lambda item: item[0])
        ranked.sort(key=lambda item: raw_by_id[item[0]].retrieved_at, reverse=True)
        ranked.sort(key=lambda item: getattr(item[1], "content_status", None) == "full", reverse=True)
        fields = dict(logical_id=logical_id, raw_record_ids=sorted({o.raw_record_id for o in observations}),
                      observations=observations, distinct_state_hashes=sorted({o.state_sha256 for o in observations}),
                      primary_observation_raw_id=ranked[0][0])
        if isinstance(ranked[0][1], Article):
            article = ranked[0][1]
            groups.append(DocumentGroup(**fields, article_id=article.article_id,
                                        canonical_url=article.canonical_url,
                                        distinct_content_hashes=sorted({o.content_sha256 for o in observations if o.content_sha256})))
        else:
            groups.append(EntityGroup(**fields))
    return groups


_LINEAGE_FIELDS = (
    "candidate_raw_record_id", "candidate_id", "discovery_provider", "discovery_source_item_id",
    "discovery_source_url", "discovery_references", "query_id", "query_intent", "query_set_hash",
    "query_version", "query_associations", "window_start", "window_end", "cutoff_at", "context_level",
    "repository_scope", "historical_version_status", "availability_basis",
)


def _lineage(article: Article, raw: RawRecord) -> SourceLineageReference:
    payload = raw.payload
    metadata = {key: payload[key] for key in _LINEAGE_FIELDS if key in payload}
    discovery = payload.get("discovery")
    if isinstance(discovery, dict):
        metadata["discovery"] = {key: discovery[key] for key in (
            "raw_record_id", "run_id", "source", "source_record_id", "source_url", *_LINEAGE_FIELDS
        ) if key in discovery}
        nested = discovery.get("payload")
        if isinstance(nested, dict):
            metadata["discovery"]["payload"] = {key: nested[key] for key in _LINEAGE_FIELDS if key in nested}
    return SourceLineageReference(article_id=article.article_id, raw_record_id=raw.raw_record_id,
                                  run_id=raw.run_id, capture_source=payload["source"],
                                  source_url=raw.source_url, fetched_url=payload["fetched_url"],
                                  discovery_lineage_complete=payload.get("discovery_lineage_complete", False),
                                  metadata=metadata)


class NormalizationPipeline:
    """No persistence side effects, no live collection and no historical reconstruction."""

    def process_jsonl(self, paths: Sequence[Path], **kwargs) -> NormalizationPipelineResult:
        """Use the existing JSONL reader; fail explicitly for a requested missing file."""
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(path)
        return self.process((row for path in paths for row in JsonlStorage().iter_records(path)), **kwargs)

    def process(self, records: Iterable[RawRecord] = (), *,
                repository_records: Iterable[RawRecord] = (), activity_records: Iterable[RawRecord] = (),
                external_records: Iterable[RawRecord] = (), targets: Sequence[VerifiedTargetContext] = (),
                cutoff_at: str | None = None) -> NormalizationPipelineResult:
        from reposeer.contracts.identity import checked_timestamp

        if cutoff_at is not None:
            checked_timestamp(cutoff_at)
        targets = [VerifiedTargetContext.model_validate(target.model_dump()) for target in targets]
        if len({target.package_id for target in targets}) != len(targets):
            raise ValueError("duplicate_target_package_context")
        raw_by_id, fingerprints, errors = {}, {}, []
        input_count = duplicate_count = invalid_count = 0
        for source in (records, repository_records, activity_records, external_records):
            for value in source:
                input_count += 1
                try:
                    raw = validate_record(value)
                    if not isinstance(raw, RawRecord):
                        raise ValueError("expected_raw_record")
                except (ValueError, TypeError):
                    invalid_count += 1
                    errors.append(DataError(code="validation_error", message="invalid_raw_envelope",
                                            retryable=False, source_record_id=None, raw_record_id=None))
                    continue
                digest = _digest(raw.model_dump(mode="json"))
                if raw.raw_record_id in fingerprints:
                    if fingerprints[raw.raw_record_id] != digest:
                        raise ValueError("conflicting_raw_record_id")
                    duplicate_count += 1
                    continue
                fingerprints[raw.raw_record_id] = digest
                raw_by_id[raw.raw_record_id] = raw
        partitions = defaultdict(list)
        for raw in raw_by_id.values():
            partitions[(raw.run_id, raw.source, raw.source_record_type)].append(raw)
        results, internals = [], []
        for (run_id, source, kind), observations in sorted(partitions.items()):
            observations.sort(key=lambda raw: (raw.retrieved_at, raw.raw_record_id))
            # Deterministic request UUIDv4-shaped identifier, with the actual collection run unchanged.
            request_id = str(UUID(hex=_digest([run_id, source, kind, [r.raw_record_id for r in observations]])[:32], version=4))
            request = NormalizationRequest(schema_version="0.1.0", record_type="normalization_request",
                                           request_id=request_id, run_id=run_id, records=observations)
            if source == "github_rest" and kind == "github_repository":
                result = GitHubNormalizer().normalize(request)
            elif source == "github_rest" and kind == "repository_activity":
                normalized = GitHubActivityNormalizer().normalize_with_entities(request)
                result = normalized.canonical_result
                internals.extend(normalized.internal_entities)
            elif source == "web_article" and kind == "article":
                result = ExternalDocumentNormalizer().normalize(request)
            else:
                result = normalization_result(request, [], [normalization_error(
                    raw, "unsupported_normalization_source", code="unsupported_record_type") for raw in observations])
            results.append(result)
            errors.extend(result.errors)
        internal_by_raw_id = {item.raw_record_id: item for item in internals}
        errors = [error for error in errors if not (
            (internal := internal_by_raw_id.get(error.raw_record_id)) is not None
            and error.code == "unsupported_record_type"
            and error.message == internal.outcome_code
            and internal.outcome_code in {"contributor_aggregate", "github_release", "event_time_unknown"}
        )]
        repositories, activities, documents = defaultdict(list), defaultdict(list), defaultdict(list)
        canonical_ids, lineage, diagnostics, assessments = set(), [], {}, []
        articles = []
        for result in results:
            for record in result.records:
                raw_id = record.provenance.raw_record_ids[0]
                canonical_ids.add(raw_id)
                if isinstance(record, GitHubRepository):
                    repositories[record.identity.repo_id].append((raw_id, record, None))
                elif isinstance(record, Article):
                    articles.append(record)
                    documents[record.article_id].append((raw_id, record, record.content_sha256))
                    lineage.append(_lineage(record, raw_by_id[raw_id]))
                    if cutoff_at is not None:
                        diagnostics[raw_id] = ("availability_unknown" if record.content_available_at is None else
                                               "observed_after_cutoff" if record.content_available_at > cutoff_at else
                                               "requires_d06_policy")
        for observation in internals:
            entity = observation.entity
            if isinstance(entity, GitHubCommit):
                key = "commit:" + entity.commit_sha
            elif isinstance(entity, GitHubIssue):
                key = "issue:" + entity.issue_id
            elif isinstance(entity, GitHubPullRequest):
                key = "pull_request:" + entity.pr_id
            elif isinstance(entity, GitHubContributorObservation):
                # Anonymous identities cannot be equated from unknown account fields.
                key = "contributor:" + (entity.contributor_github_id or observation.raw_record_id) + ":" + entity.observed_at
            else:
                key = "release:" + entity.release_id
            activities[entity.repo_id + ":" + key].append((observation.raw_record_id, entity, None))
        repo_groups = _groups(repositories, raw_by_id)
        activity_groups = _groups(activities, raw_by_id)
        document_groups = _groups(documents, raw_by_id)
        by_hash = defaultdict(set)
        for group in document_groups:
            for digest in group.distinct_content_hashes:
                by_hash[digest].add(group.article_id)
        for article in articles:
            raw = raw_by_id[article.provenance.raw_record_ids[0]]
            for target in sorted(targets, key=lambda target: target.package_id):
                assessments.append(ExternalRelevanceEvaluator().evaluate(article, target, raw=raw))
        accepted = canonical_ids | {observation.raw_record_id for observation in internals}
        summary = ProcessingSummary(
            input_record_count=input_count, unique_raw_observation_count=len(raw_by_id), duplicate_input_count=duplicate_count,
            accepted_observation_count=len(accepted), rejected_observation_count=len(raw_by_id)-len(accepted)+invalid_count,
            canonical_output_count=sum(result.output_record_count for result in results),
            canonical_rejected_observation_count=sum(result.rejected_record_count for result in results),
            internal_only_observation_count=len(accepted-canonical_ids), unique_repositories=len(repo_groups),
            unique_activity_entities=len(activity_groups), unique_documents=len(document_groups),
            content_revisions=sum(len(group.distinct_content_hashes) for group in document_groups),
            relevance_decisions={decision: sum(item.decision == decision for item in assessments)
                                 for decision in ("relevant", "irrelevant", "unresolved")},
        )
        return NormalizationPipelineResult(
            canonical_normalization_results=results, internal_activity_entities=internals,
            deduplicated_repository_view=repo_groups, deduplicated_activity_view=activity_groups,
            deduplicated_document_view=document_groups,
            content_duplicates=[ContentDuplicate(content_sha256=digest, article_ids=sorted(ids))
                                for digest, ids in sorted(by_hash.items()) if len(ids) > 1],
            relevance_assessments=assessments, source_lineage_references=lineage,
            processing_errors=errors, temporal_diagnostics=diagnostics, processing_summary=summary,
        )
