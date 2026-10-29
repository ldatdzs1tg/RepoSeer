"""Additive D07 specialist-agent I/O contract; no model or label decisions."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from reposeer.contracts.models import (
    ContractModel,
    Count,
    HttpUrl,
    NonEmpty,
    PackageId,
    RepoId,
    Sha256,
    UtcTimestamp,
    UuidText,
    unique,
)

AGENT_SCHEMA_VERSION = "0.1.0"
type Specialist = Literal[
    "maintenance_lifecycle", "functional_trend_demand", "alternatives_migration"
]
type AllowedUse = Literal["feature_candidate", "report_only", "status_candidate"]
type ContextLevel = Literal["package_specific", "function_level", "alternative_level"]
type SignalType = Literal[
    "maintenance_concern", "support_reduction", "maintainer_departure", "deprecation",
    "official_transition", "end_of_support_claim", "replacement_claim",
    "emerging_use_case", "changing_technical_need", "function_discussion",
    "comparison", "recommended_alternative", "explicit_migration", "user_complaint",
    "official_replacement_claim",
]

ROUTES = {
    "maintenance_lifecycle": ("maintenance", "package_specific"),
    "functional_trend_demand": ("functional", "function_level"),
    "alternatives_migration": ("alternatives", "alternative_level"),
}
SIGNAL_TYPES = {
    "maintenance_lifecycle": {
        "maintenance_concern", "support_reduction", "maintainer_departure", "deprecation",
        "official_transition", "end_of_support_claim", "replacement_claim",
    },
    "functional_trend_demand": {
        "emerging_use_case", "changing_technical_need", "function_discussion",
    },
    "alternatives_migration": {
        "comparison", "recommended_alternative", "explicit_migration", "user_complaint",
        "official_replacement_claim",
    },
}


class D06Gate(ContractModel):
    """Receipt from the deterministic as-of gate, not an agent judgment."""

    decision: Literal["eligible"]
    decision_id: NonEmpty
    policy_version: NonEmpty
    checked_at: UtcTimestamp
    availability_proof_ref: NonEmpty
    allowed_use: AllowedUse


class D06Evidence(ContractModel):
    evidence_id: NonEmpty
    package_id: PackageId
    subject_id: NonEmpty
    context_level: ContextLevel
    topic: Literal["maintenance", "functional", "alternatives"]
    source_kind: Literal["official", "external"]
    source_id: NonEmpty
    source_url: HttpUrl
    source_record_id: NonEmpty | None
    source_revision: NonEmpty
    source_content_sha256: Sha256
    original_event_id: NonEmpty
    passage_text: Annotated[str, Field(min_length=1, max_length=20000)]
    passage_start_offset: Count
    passage_sha256: Sha256
    event_at: UtcTimestamp | None
    available_at: UtcTimestamp
    valid_at: UtcTimestamp
    fetched_at: UtcTimestamp
    gate: D06Gate

    @model_validator(mode="after")
    def check(self):
        if hashlib.sha256(self.passage_text.encode("utf-8")).hexdigest() != self.passage_sha256:
            raise ValueError("passage hash must match exact UTF-8 passage bytes")
        if self.available_at > self.fetched_at or self.valid_at > self.fetched_at:
            raise ValueError("source availability/validity cannot follow retrieval")
        if self.gate.checked_at < self.fetched_at:
            raise ValueError("D06 decision cannot precede source retrieval")
        if self.gate.allowed_use == "status_candidate" and (
            self.source_kind != "official" or self.context_level != "package_specific"
        ):
            raise ValueError("status candidate requires direct official package evidence")
        return self


class AgentRequest(ContractModel):
    schema_version: Literal["0.1.0"]
    record_type: Literal["specialist_agent_request"]
    analysis_run_id: UuidText
    request_id: UuidText
    agent_kind: Specialist
    package_id: PackageId
    repo_id: RepoId
    mapping_revision: NonEmpty
    package_scope: NonEmpty
    cutoff_at: UtcTimestamp
    routing_policy_version: NonEmpty
    evidence: Annotated[list[D06Evidence], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def check(self):
        unique([item.evidence_id for item in self.evidence], "evidence IDs")
        topic, context = ROUTES[self.agent_kind]
        for item in self.evidence:
            if item.package_id != self.package_id:
                raise ValueError("routed evidence package mismatch")
            if item.topic != topic or item.context_level != context:
                raise ValueError("evidence topic/context not routed to this specialist")
            if item.source_kind == "official" and self.agent_kind != "maintenance_lifecycle":
                raise ValueError("official direct-package text routes to Agent 1")
            if item.available_at > self.cutoff_at or item.valid_at > self.cutoff_at:
                raise ValueError("D06 input must have a source revision available at cutoff")
            if item.event_at is not None and item.event_at > self.cutoff_at:
                raise ValueError("future event is not an as-of observation")
            if item.gate.allowed_use == "status_candidate" and self.agent_kind != "maintenance_lifecycle":
                raise ValueError("only Agent 1 may receive official status candidates")
        return self


class SignalCitation(ContractModel):
    evidence_id: NonEmpty
    source_id: NonEmpty
    source_url: HttpUrl
    source_revision: NonEmpty
    source_record_id: NonEmpty | None
    original_event_id: NonEmpty
    available_at: UtcTimestamp
    start_offset: Count
    end_offset: Count
    quote: NonEmpty

    @model_validator(mode="after")
    def check(self):
        if self.end_offset <= self.start_offset or len(self.quote) != self.end_offset - self.start_offset:
            raise ValueError("citation offset span must equal exact quote length")
        return self


class ExtractedSignal(ContractModel):
    signal_id: NonEmpty
    package_id: PackageId
    subject_id: NonEmpty
    context_level: ContextLevel
    signal_type: SignalType
    statement: NonEmpty
    event_at: UtcTimestamp | None
    announced_at: UtcTimestamp | None
    effective_at: UtcTimestamp | None
    citations: Annotated[list[SignalCitation], Field(min_length=1)]
    proposed_use: AllowedUse
    uncertainty: NonEmpty | None

    @model_validator(mode="after")
    def check(self):
        unique([c.evidence_id for c in self.citations], "signal citations")
        if self.proposed_use == "status_candidate" and self.signal_type not in {
            "official_transition", "end_of_support_claim", "replacement_claim"
        }:
            raise ValueError("status candidate must be an official cessation/transition claim")
        return self


class AgentResponse(ContractModel):
    schema_version: Literal["0.1.0"]
    record_type: Literal["specialist_agent_response"]
    analysis_run_id: UuidText
    request_id: UuidText
    agent_kind: Specialist
    package_id: PackageId
    cutoff_at: UtcTimestamp
    generated_at: UtcTimestamp
    agent_version: NonEmpty
    model_version: NonEmpty
    prompt_version: NonEmpty
    status: Literal["complete", "partial", "failed"]
    signals: list[ExtractedSignal]
    error_code: NonEmpty | None

    @model_validator(mode="after")
    def check(self):
        unique([signal.signal_id for signal in self.signals], "signal IDs")
        if (self.status == "complete") != (self.error_code is None):
            raise ValueError("partial/failed responses require an error code")
        if self.status == "failed" and self.signals:
            raise ValueError("failed agent response cannot contain signals")
        if self.generated_at < self.cutoff_at:
            raise ValueError("agent response cannot precede cutoff")
        for signal in self.signals:
            if signal.package_id != self.package_id or signal.signal_type not in SIGNAL_TYPES[self.agent_kind]:
                raise ValueError("signal package/type does not belong to this specialist")
            if signal.context_level != ROUTES[self.agent_kind][1]:
                raise ValueError("signal context is outside this specialist's scope")
            if signal.proposed_use == "status_candidate" and self.agent_kind != "maintenance_lifecycle":
                raise ValueError("only Agent 1 may propose a status candidate")
            if signal.proposed_use == "feature_candidate" and signal.signal_type in {
                "official_transition", "end_of_support_claim", "replacement_claim",
                "official_replacement_claim",
            }:
                raise ValueError("known-outcome or official replacement claims are report/status leads, not E2 features")
            if signal.proposed_use == "feature_candidate" and any(
                timestamp is not None and timestamp > self.cutoff_at
                for timestamp in (signal.event_at, signal.announced_at, signal.effective_at)
            ):
                raise ValueError("postcutoff events/plans cannot be predictive features")
        return self


class ConflictReviewRecord(ContractModel):
    """Coordinator review decision. The bounded reviewer is not a fourth agent."""

    schema_version: Literal["0.1.0"]
    record_type: Literal["conflict_review"]
    analysis_run_id: UuidText
    cluster_id: NonEmpty
    subject_id: NonEmpty
    signal_ids: Annotated[list[NonEmpty], Field(min_length=2)]
    source_ids: Annotated[list[NonEmpty], Field(min_length=1)]
    review_calls: Literal[0, 1]
    reviewer_version: NonEmpty | None
    status: Literal["no_conflict", "resolved", "unresolved", "rejected"]
    deterministic_recheck_passed: bool
    final_use: Literal["e2_feature", "event_candidate", "report_only", "rejected"]
    reason: NonEmpty

    @model_validator(mode="after")
    def check(self):
        unique(self.signal_ids, "review signal IDs")
        unique(self.source_ids, "review source IDs")
        if (self.review_calls == 1) != (self.reviewer_version is not None):
            raise ValueError("one reviewer call requires reviewer version; zero forbids it")
        if self.status == "no_conflict" and self.review_calls != 0:
            raise ValueError("no-conflict path cannot call reviewer")
        if self.status == "resolved" and self.review_calls != 1:
            raise ValueError("resolved conflict requires exactly one reviewer call")
        expected_use = {"unresolved": "report_only", "rejected": "rejected"}
        if self.status in expected_use and self.final_use != expected_use[self.status]:
            raise ValueError("unresolved/rejected claims cannot enter E2 features or D09 candidates")
        if self.status in {"no_conflict", "resolved"} and not self.deterministic_recheck_passed:
            raise ValueError("accepted signals require deterministic revalidation")
        if self.status in {"unresolved", "rejected"} and self.deterministic_recheck_passed:
            raise ValueError("unresolved/rejected claims cannot claim revalidation success")
        return self


REQUEST_ADAPTER = TypeAdapter(AgentRequest)
RESPONSE_ADAPTER = TypeAdapter(AgentResponse)
REVIEW_ADAPTER = TypeAdapter(ConflictReviewRecord)


def validate_conflict_reviews(
    records: list[ConflictReviewRecord | dict],
) -> list[ConflictReviewRecord]:
    """Enforce one persisted review decision per cluster in an analysis run."""
    reviewed = [
        REVIEW_ADAPTER.validate_python(record.model_dump() if isinstance(record, ConflictReviewRecord) else record)
        for record in records
    ]
    unique([(item.analysis_run_id, item.cluster_id) for item in reviewed], "review clusters")
    return reviewed


def validate_agent_pair(request: AgentRequest | dict, response: AgentResponse | dict) -> AgentResponse:
    """Recheck typed output against original D06 passages; semantic truth needs review."""
    request = REQUEST_ADAPTER.validate_python(
        request.model_dump() if isinstance(request, AgentRequest) else request
    )
    response = RESPONSE_ADAPTER.validate_python(
        response.model_dump() if isinstance(response, AgentResponse) else response
    )
    if any(getattr(request, field) != getattr(response, field) for field in (
        "analysis_run_id", "request_id", "agent_kind", "package_id", "cutoff_at"
    )):
        raise ValueError("agent request/response identity mismatch")
    evidence = {item.evidence_id: item for item in request.evidence}
    for signal in response.signals:
        for citation in signal.citations:
            item = evidence.get(citation.evidence_id)
            if item is None or any((
                citation.source_id != item.source_id,
                citation.source_url != item.source_url,
                citation.source_revision != item.source_revision,
                citation.source_record_id != item.source_record_id,
                citation.original_event_id != item.original_event_id,
                citation.available_at != item.available_at,
                signal.subject_id != item.subject_id,
            )):
                raise ValueError("citation source/subject is absent or mismatched")
            start = citation.start_offset - item.passage_start_offset
            end = citation.end_offset - item.passage_start_offset
            if start < 0 or end > len(item.passage_text) or item.passage_text[start:end] != citation.quote:
                raise ValueError("quote is not exact text at offsets in the D06 passage")
            if item.gate.checked_at > response.generated_at:
                raise ValueError("D06 gate decision cannot follow agent response")
            if signal.proposed_use == "feature_candidate" and item.gate.allowed_use != "feature_candidate":
                raise ValueError("report/status-only input cannot become a predictive feature")
            if signal.proposed_use == "status_candidate" and (
                item.gate.allowed_use != "status_candidate" or item.source_kind != "official"
            ):
                raise ValueError("D09 event leads require as-of official status-candidate input")
        if signal.event_at is not None and signal.event_at > response.cutoff_at:
            raise ValueError("signal event is after cutoff")
    return response
