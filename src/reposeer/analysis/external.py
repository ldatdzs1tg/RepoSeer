"""T-025: offline external context, nullable E1 census counts and validated E2 events.

Trusted backend verifiers are injected Python dependencies, never JSON flags.
No network, LLM, training, prediction, label assignment or filesystem writes.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from reposeer.contracts import Article, validate_record
from reposeer.contracts.analysis import (
    AnalysisCoverage,
    AnalysisEvidence,
    EvidenceCitation,
    SourceProvenance,
)
from reposeer.contracts.models import (
    ContractModel,
    Count,
    NonEmpty,
    PackageId,
    Sha256,
    TimeRange,
    UtcTimestamp,
    unique,
)
from reposeer.processing.relevance import RelevanceAssessment, validate_assessment_evidence

type Context = Literal["direct_package", "same_function", "alternative"]
_CONTEXTS = {
    "package_specific": "direct_package", "function_level": "same_function",
    "alternative_level": "alternative",
}
_OUTCOME = re.compile(
    r"\b(no longer maintained|maintenance ceased|end[- ]of[- ]life|"
    r"scheduled cessation|already abandoned|permanently archived|"
    r"will (?:stop|cease) (?:maintenance|support))\b", re.I,
)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _days(window: TimeRange) -> float:
    return (datetime.fromisoformat(window.end_at.replace("Z", "+00:00")) -
            datetime.fromisoformat(window.start_at.replace("Z", "+00:00"))).total_seconds() / 86400


class ExternalDocument(ContractModel):
    article: Article
    relevance: RelevanceAssessment
    provider: NonEmpty
    query_intent: NonEmpty
    query_set_hash: Sha256 | None
    # A trusted upstream syndication/original-item identity; defaults to exact content dedup.
    origin_id: NonEmpty | None = None
    outcome_disclosure: bool = False

    @model_validator(mode="after")
    def bind_revision(self):
        validate_record(self.article)
        validate_assessment_evidence(self.relevance, self.article)
        return self

    @property
    def source_id(self) -> str:
        return "external:" + _hash(dict(
            article_id=self.article.article_id, content=self.article.content_sha256,
            provenance=self.article.provenance.model_dump(),
        ))

    def source(self) -> SourceProvenance:
        return SourceProvenance(
            **self.article.provenance.model_dump(),
            source_id=self.source_id, source_record_id=self.article.article_id,
            revision=None, content_sha256=self.article.content_sha256,
            query_set_hash=self.query_set_hash,
        )

    @property
    def context(self) -> str | None:
        return _CONTEXTS.get(self.relevance.context_level)


class WindowCensus(ContractModel):
    """A persisted D04-D06 census, not a claim that API pagination is exhaustive."""
    census_id: NonEmpty
    provider: NonEmpty
    context: Context
    query_intent: NonEmpty
    window: TimeRange
    source: SourceProvenance
    retrieval_policy_version: NonEmpty
    filters_sha256: Sha256
    status: Literal["complete", "partial", "unavailable"]
    source_coverage: Literal["complete", "partial", "unknown"]
    document_source_ids: list[NonEmpty]
    missing_reason: NonEmpty | None

    @model_validator(mode="after")
    def check(self):
        unique(self.document_source_ids, "census.document_source_ids")
        if (self.status == "complete") != (self.missing_reason is None):
            raise ValueError("noncomplete census requires missing_reason")
        return self


class ValidatedAgentEvent(ContractModel):
    """D07 handoff. A flag in a request cannot certify an agent event."""
    event_id: NonEmpty
    evidence: AnalysisEvidence
    outcome_disclosure: bool = False


class ExternalContextRequest(ContractModel):
    package_id: PackageId
    cutoff_at: UtcTimestamp
    generated_at: UtcTimestamp
    documents: list[ExternalDocument]
    censuses: list[WindowCensus] = Field(default_factory=list)
    agent_events: list[ValidatedAgentEvent] = Field(default_factory=list)
    agent_coverage: list[AnalysisCoverage] = Field(default_factory=list)

    @model_validator(mode="after")
    def check(self):
        if self.generated_at < self.cutoff_at:
            raise ValueError("generation precedes cutoff")
        unique([d.source_id for d in self.documents], "document revisions")
        unique([c.census_id for c in self.censuses], "census IDs")
        unique([c.coverage_id for c in self.agent_coverage], "agent coverage IDs")
        unique([e.evidence.evidence_id for e in self.agent_events], "agent evidence IDs")
        for document in self.documents:
            if document.relevance.target_package_id != self.package_id:
                raise ValueError("relevance belongs to another package")
            if document.article.provenance.retrieved_at > self.generated_at:
                raise ValueError("document retrieved after generation")
        for census in self.censuses:
            if census.window.end_at > self.cutoff_at:
                raise ValueError("census extends past cutoff")
            if census.source.retrieved_at > self.generated_at:
                raise ValueError("census retrieved after generation")
        return self


class EvidenceVerifier(Protocol):
    """Trusted backend binds exact bytes/identity/availability, corpus and D07 review.

    No production archive/census verifier is inferred from current T-020 files.
    """
    def historical_document(self, document: ExternalDocument, cutoff_at: str) -> bool: ...
    def census(self, census: WindowCensus, documents: list[ExternalDocument]) -> bool: ...
    def agent_event(self, event: ValidatedAgentEvent) -> bool: ...
    def agent_processing(self, coverage: AnalysisCoverage, documents: list[ExternalDocument]) -> bool: ...


class RuleWindowCounts(ContractModel):
    census_id: NonEmpty
    provider: NonEmpty
    context: Context
    query_intent: NonEmpty
    query_set_hash: Sha256 | None
    retrieval_policy_version: NonEmpty
    filters_sha256: Sha256
    window: TimeRange
    count: Count | None
    rate_per_30_days: float | None
    missing_reason: NonEmpty | None


class TrendComparison(ContractModel):
    previous_census_id: NonEmpty
    recent_census_id: NonEmpty
    log1p_count_delta: float | None
    missing_reason: NonEmpty | None


class AgentWindowCounts(ContractModel):
    census_id: NonEmpty
    signal_type: Literal["maintenance", "functional_demand", "migration"]
    count: Count | None
    missing_reason: NonEmpty | None


class ExternalContextResult(ContractModel):
    package_id: PackageId
    cutoff_at: UtcTimestamp
    sources: list[SourceProvenance]
    evidence: list[AnalysisEvidence]
    coverage: list[AnalysisCoverage]
    e1: list[RuleWindowCounts]
    trends: list[TrendComparison]
    e2: list[AgentWindowCounts]
    excluded_documents: dict[str, NonEmpty]
    rejected_agent_events: dict[str, NonEmpty]
    external_insights_status: Literal["complete", "partial", "unavailable"]
    external_insights_reason: NonEmpty | None
    limitations: list[NonEmpty]

    def report_fields(self) -> dict:
        """T-021 fields for T-027; never update model results or official status."""
        return self.model_dump(mode="json", include={
            "sources", "evidence", "coverage", "external_insights_status", "external_insights_reason",
        })


def _temporal_reason(document, cutoff, verifier):
    article = document.article
    if article.content_status != "full" or not article.content_text:
        return "full_content_unavailable"
    if document.outcome_disclosure or _OUTCOME.search(article.content_text):
        return "outcome_disclosure"
    if article.published_at is None:
        return "publication_timestamp_unknown"
    if article.published_at > cutoff or (article.updated_at and article.updated_at > cutoff):
        return "document_after_cutoff"
    try:
        document.source().require_as_of(cutoff)
    except ValueError:
        return "source_availability_unverified"
    if article.content_available_at is None or article.content_available_at > cutoff:
        return "content_availability_unverified"
    if article.provenance.temporal_basis == "historical_reconstruction":
        if verifier is None or not verifier.historical_document(document, cutoff):
            return "historical_revision_unverified"
    return None


def _dedup(documents):
    # Merge all connected identities before choosing representatives; a late bridge
    # between two syndicated copies must also collapse previously separate groups.
    ordered = sorted(documents, key=lambda d: (d.article.canonical_url, d.source_id))
    parents, owners = list(range(len(ordered))), {}

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, document in enumerate(ordered):
        keys = {("article", document.article.article_id),
                ("content", document.article.content_sha256)}
        if document.origin_id is not None:
            keys.add(("origin", document.origin_id))
        for key in sorted(keys):
            if key in owners:
                left, right = root(index), root(owners[key])
                parents[max(left, right)] = min(left, right)
            else:
                owners[key] = index
    return [document for index, document in enumerate(ordered) if root(index) == index]


def _census_reason(census, request, by_id, eligible_ids, verifier):
    if census.status != "complete" or census.source_coverage != "complete":
        return census.missing_reason or "source_census_incomplete"
    if census.source.query_set_hash is None:
        return "query_set_unknown"
    try:
        census.source.require_as_of(request.cutoff_at)
    except ValueError:
        return "census_availability_unverified"
    if any(key not in by_id for key in census.document_source_ids):
        return "census_documents_missing"
    members = [by_id[key] for key in census.document_source_ids]
    for document in members:
        try:
            document.source().require_as_of(request.cutoff_at)
        except ValueError:
            return "census_member_availability_unverified"
        if document.provider != census.provider or document.query_intent != census.query_intent:
            return "census_membership_mismatch"
        if document.query_set_hash != census.source.query_set_hash:
            return "query_set_mismatch"
        # A complete processed census cannot silently omit unresolved/undated members.
        if document.relevance.decision == "unresolved":
            return "relevance_unresolved"
        if document.relevance.decision == "relevant":
            if document.context != census.context or document.source_id not in eligible_ids:
                return "census_document_ineligible"
        published = document.article.published_at
        if published is None or not (census.window.start_at < published <= census.window.end_at):
            return "census_publication_outside_window"
    observed = {d.source_id for d in request.documents
                if d.provider == census.provider and d.query_intent == census.query_intent
                and d.query_set_hash == census.source.query_set_hash
                and d.article.published_at is not None
                and census.window.start_at < d.article.published_at <= census.window.end_at
                and d.context == census.context}
    if not observed.issubset(set(census.document_source_ids)):
        return "census_omits_observed_documents"
    if verifier is None or not verifier.census(census, members):
        return "source_census_unverified"
    return None


def _rejected_census_scope(event, censuses, by_id):
    """Use bound source membership; rejected context/time cannot safely narrow it."""
    citations = {citation.source_id for citation in event.evidence.citations}
    affected, attributed = set(), set()
    for census in censuses:
        members = citations.intersection(census.document_source_ids)
        if members:
            affected.add(census.census_id)
            attributed.update(members)
    if not citations or not citations.issubset(by_id) or attributed != citations:
        # An unknown/unassigned citation cannot exclude any declared census.
        return {census.census_id for census in censuses}
    return affected


def analyze_external_context(request: ExternalContextRequest, *,
                             verifier: EvidenceVerifier | None = None) -> ExternalContextResult:
    """Pure analysis. Current/unknown captures yield report-only context and null features."""
    request = ExternalContextRequest.model_validate(request.model_dump(mode="python"))
    by_id = {d.source_id: d for d in request.documents}
    sources = {d.source_id: d.source() for d in request.documents}
    evidence, eligible, excluded = [], [], {}
    for document in sorted(request.documents, key=lambda d: d.source_id):
        if document.relevance.decision != "relevant" or document.context is None:
            excluded[document.source_id] = document.relevance.reason_code
            continue
        reason = _temporal_reason(document, request.cutoff_at, verifier)
        if reason is None:
            eligible.append(document)
        else:
            excluded[document.source_id] = reason
        for index, span in enumerate(document.relevance.evidence_spans):
            evidence.append(AnalysisEvidence(
                evidence_id=f"{document.source_id}:span:{index}", package_id=request.package_id,
                origin="external", context=document.context, signal_type="context", claim=span.text,
                event_at=document.article.published_at,
                citations=[EvidenceCitation(source_id=document.source_id, quote=span.text,
                                           start_offset=span.start_offset, end_offset=span.end_offset)],
                use="feature" if reason is None else "report_only",
                exclusion_reason=reason, uncertainty=None, agent=None,
            ))
    eligible_ids = {d.source_id for d in eligible}
    coverage, e1, measured = [], [], {}
    for census in sorted(request.censuses, key=lambda c: c.census_id):
        existing = sources.get(census.source.source_id)
        if existing is not None and existing != census.source:
            raise ValueError("conflicting source ID")
        sources[census.source.source_id] = census.source
        reason = _census_reason(census, request, by_id, eligible_ids, verifier)
        members = [by_id[key] for key in census.document_source_ids if key in by_id]
        counted = _dedup([d for d in members if d.source_id in eligible_ids])
        count = len(counted) if reason is None else None
        measured[census.census_id] = count
        e1.append(RuleWindowCounts(
            census_id=census.census_id, provider=census.provider, context=census.context,
            query_intent=census.query_intent, query_set_hash=census.source.query_set_hash,
            retrieval_policy_version=census.retrieval_policy_version, filters_sha256=census.filters_sha256,
            window=census.window, count=count,
            rate_per_30_days=30 * count / _days(census.window) if count is not None else None,
            missing_reason=reason,
        ))
        source_ids = sorted({census.source.source_id, *census.document_source_ids} & sources.keys())
        coverage.append(AnalysisCoverage(
            coverage_id="external:" + census.census_id, stage="external", source_ids=source_ids,
            status="complete" if reason is None else "partial",
            source_coverage="complete" if reason is None else (
                "partial" if census.source_coverage == "partial" else "unknown"),
            historical_validity="verified" if reason is None else "unverified",
            window=census.window, records_observed=len(members) if members or reason is None else None,
            eligible_documents=len(counted), processed_documents=len(counted), missing_reason=reason,
        ))
    if not request.censuses:
        coverage.append(AnalysisCoverage(
            coverage_id="external:uncensused", stage="external", source_ids=sorted(sources),
            status="partial" if request.documents else "unavailable", source_coverage="unknown",
            historical_validity="unverified", window=None,
            records_observed=len(request.documents) or None,
            eligible_documents=len(eligible) if request.documents else None,
            processed_documents=len(eligible) if request.documents else None,
            missing_reason="source_census_missing",
        ))
    trends = []
    for recent in e1:
        for previous in e1:
            if previous.window.end_at != recent.window.start_at:
                continue
            def key(counts):
                return (counts.provider, counts.context, counts.query_intent, counts.query_set_hash,
                        counts.retrieval_policy_version, counts.filters_sha256)
            if (previous.provider, previous.context, previous.query_intent) != (
                    recent.provider, recent.context, recent.query_intent):
                continue
            reason = None
            if key(previous) != key(recent) or _days(previous.window) != _days(recent.window):
                reason = "windows_not_comparable"
            elif previous.count is None or recent.count is None:
                reason = "window_counts_unavailable"
            trends.append(TrendComparison(
                previous_census_id=previous.census_id, recent_census_id=recent.census_id,
                log1p_count_delta=(math.log1p(recent.count) - math.log1p(previous.count)
                                  if reason is None else None), missing_reason=reason,
            ))
    rejected, events, seen_event = {}, [], {}
    rejected_censuses = set()
    for event in sorted(request.agent_events, key=lambda e: (e.event_id, e.evidence.evidence_id)):
        item = event.evidence
        reason = None
        citations = {c.source_id for c in item.citations}
        if item.package_id != request.package_id or item.origin != "agent":
            reason = "agent_identity_mismatch"
        elif item.use != "feature" or item.uncertainty is not None:
            reason = "agent_signal_not_feature_eligible"
        elif event.outcome_disclosure or _OUTCOME.search(item.claim):
            reason = "outcome_disclosure"
        elif item.event_at is None or item.event_at > request.cutoff_at:
            reason = "agent_event_time_unverified"
        elif not citations.issubset(eligible_ids):
            reason = "agent_sources_ineligible"
        elif any(by_id[c.source_id].article.content_text[c.start_offset:c.end_offset] != c.quote
                 or by_id[c.source_id].context != item.context for c in item.citations):
            reason = "agent_quote_or_context_mismatch"
        elif verifier is None or not verifier.agent_event(event):
            reason = "agent_event_unverified"
        key = (event.event_id, item.context, item.signal_type)
        fingerprint = (item.claim, item.event_at)
        if key in seen_event and seen_event[key] != fingerprint:
            raise ValueError("conflicting_agent_event_requires_review")
        if reason is not None:
            rejected[item.evidence_id] = reason
            rejected_censuses.update(_rejected_census_scope(event, request.censuses, by_id))
            continue
        if key not in seen_event:
            events.append((event.event_id, item))
            evidence.append(item)
        seen_event[key] = fingerprint
    e2, included_agent_coverage = [], set()
    for census in sorted(request.censuses, key=lambda c: c.census_id):
        relevant_ids = {key for key in census.document_source_ids if key in eligible_ids}
        matching = [c for c in request.agent_coverage if c.stage == "agent"
                    and c.window == census.window
                    and set(c.source_ids) == (relevant_ids or {census.source.source_id})]
        reason = None
        if measured[census.census_id] is None:
            reason = "source_census_unverified"
        elif (len(matching) != 1 or matching[0].status != "complete"
              or matching[0].source_coverage != "complete"
              or matching[0].historical_validity != "verified"
              or matching[0].eligible_documents != len(relevant_ids)
              or matching[0].processed_documents != len(relevant_ids)):
            reason = "agent_processing_incomplete"
        elif verifier is None or not verifier.agent_processing(
                matching[0], [by_id[key] for key in sorted(relevant_ids)]):
            reason = "agent_processing_unverified"
        elif census.census_id in rejected_censuses:
            reason = "agent_events_rejected"
        for signal in ("maintenance", "functional_demand", "migration"):
            count = len({event_id for event_id, item in events
                         if item.signal_type == signal and item.context == census.context
                         and census.window.start_at < item.event_at <= census.window.end_at
                         and {c.source_id for c in item.citations}.issubset(relevant_ids)})
            e2.append(AgentWindowCounts(census_id=census.census_id, signal_type=signal,
                                       count=count if reason is None else None, missing_reason=reason))
        if reason is None and matching[0].coverage_id not in included_agent_coverage:
            coverage.extend(matching)
            included_agent_coverage.add(matching[0].coverage_id)
    unique([item.evidence_id for item in evidence], "output evidence IDs")
    unique([item.coverage_id for item in coverage], "output coverage IDs")
    statuses = Counter(c.status for c in coverage)
    status = "complete" if not excluded and not rejected and not (
        statuses["partial"] or statuses["unavailable"]) else "partial"
    if not evidence and not any(count is not None for count in measured.values()):
        status = "unavailable"
    return ExternalContextResult(
        package_id=request.package_id, cutoff_at=request.cutoff_at,
        sources=[sources[key] for key in sorted(sources)], evidence=evidence, coverage=coverage,
        e1=e1, trends=trends, e2=e2, excluded_documents=excluded, rejected_agent_events=rejected,
        external_insights_status=status,
        external_insights_reason=None if status == "complete" else "external_evidence_or_coverage_incomplete",
        limitations=[
            "Counts describe only the verified declared source/query/window corpus, not the whole web.",
            "Current T-020 captures do not establish historical availability or a complete census.",
            "Archive/census/D07 verification requires trusted injected backend implementations.",
            "External context does not assign labels, official status or change model probabilities.",
        ],
    )
