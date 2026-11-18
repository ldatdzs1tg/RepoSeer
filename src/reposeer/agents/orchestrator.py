"""T-026 bounded D07 runtime. Research and strict candidates never become ML features."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from reposeer.agents.intake import SPECIALISTS, IntakeSummary, request_id_for
from reposeer.agents.provider import (
    PROMPT_VERSION,
    PROVIDER_ERROR_CODES,
    ExtractionDraft,
    ProviderCallDiagnostic,
    ProviderError,
    ResearchDraft,
    ReviewDraft,
    ReviewInput,
    SpecialistLLM,
)
from reposeer.contracts.models import (
    ContractModel,
    Count,
    HttpUrl,
    NonEmpty,
    PackageId,
    UtcTimestamp,
    UuidText,
    unique,
)
from reposeer.contracts.specialist_agents import (
    ROUTES,
    AgentRequest,
    AgentResponse,
    ConflictReviewRecord,
    D06Evidence,
    EnrichmentSuggestion,
    ExtractedSignal,
    InformationGap,
    ResearchHandoffRequest,
    ResearchHandoffResponse,
    ResearchInsight,
    SignalCitation,
    Specialist,
    validate_agent_pair,
    validate_conflict_reviews,
    validate_research_handoff_pair,
    validate_review_references,
)

AGENT_VERSION = '0.1.0'
type TemporalState = Literal['after_cutoff', 'unknown_availability', 'as_of_not_independently_verified']
type Lane = Literal['research', 'strict', 'review']


class D06ReceiptVerifier(Protocol):
    """Trusted Python backend dependency, never a caller-selectable JSON flag."""
    def verify(self, request: AgentRequest, evidence: D06Evidence) -> bool: ...


class MultiAgentRunRequest(ContractModel):
    schema_version: Literal['0.1.0']
    record_type: Literal['multi_agent_run_request']
    analysis_run_id: UuidText
    package_id: PackageId
    cutoff_at: UtcTimestamp
    research_requests: Annotated[list[ResearchHandoffRequest], Field(max_length=3)]
    strict_requests: Annotated[list[AgentRequest], Field(max_length=3)]
    intake_summary: IntakeSummary | None = None

    @model_validator(mode='after')
    def check(self):
        for lane in (self.research_requests, self.strict_requests):
            unique([child.agent_kind for child in lane], 'specialists per lane')
            unique([child.request_id for child in lane], 'request IDs per lane')
            for child in lane:
                if any(getattr(child, field) != getattr(self, field)
                       for field in ('analysis_run_id', 'package_id', 'cutoff_at')):
                    raise ValueError('child run/package/cutoff mismatch')
        if self.intake_summary is not None:
            summary = self.intake_summary
            sources = {c.source_ref for r in self.research_requests for c in r.candidates}
            if (summary.retained_candidates != len(sources)
                    or summary.accepted_candidates != summary.retained_candidates + summary.omitted_candidates):
                raise ValueError('intake counters must match retained research sources')
        return self


class RejectedItem(ContractModel):
    lane: Lane
    agent_kind: Specialist | None
    item_id: NonEmpty
    reason_code: NonEmpty
    count: Count = 1


class AgentExecutionStatus(ContractModel):
    agent_kind: Specialist
    lane: Literal['research', 'strict']
    status: Literal['complete', 'partial', 'failed', 'not_requested']
    llm_calls: Count
    error_code: NonEmpty | None
    received_candidates: Count = 0
    processed_candidates: Count = 0
    unprocessed_candidates: Count = 0


class ResearchGroundingReceipt(ContractModel):
    agent_kind: Specialist
    request_id: UuidText
    insight_id: NonEmpty
    source_ref: NonEmpty
    supporting_quote: NonEmpty
    source_url: HttpUrl | None
    available_at: UtcTimestamp | None
    temporal_state: TemporalState


class SynthesizedInsight(ContractModel):
    insight: ResearchInsight
    agent_kinds: list[Specialist]
    limitations: list[NonEmpty]
    temporal_states: list[TemporalState]


class SynthesizedGap(ContractModel):
    gap: InformationGap
    agent_kinds: list[Specialist]


class SynthesizedSuggestion(ContractModel):
    suggestion: EnrichmentSuggestion
    agent_kinds: list[Specialist]


class SignalCandidate(ContractModel):
    cluster_id: NonEmpty
    signal: ExtractedSignal
    corroborating_signal_ids: list[NonEmpty]
    source_ids: list[NonEmpty]
    trusted_d06_verified: bool
    e2_eligible: Literal[False] = False
    disposition: Literal['provisional', 'report_only']
    reason_code: NonEmpty


class ReviewRecommendation(ContractModel):
    cluster_id: NonEmpty
    recommendation: ReviewDraft


class CoverageSummary(ContractModel):
    scope: Literal['supplied_requests_only'] = 'supplied_requests_only'
    source_coverage: Literal['unknown'] = 'unknown'
    historical_validity: Literal['unverified'] = 'unverified'
    candidates_received: Count
    candidates_processed: Count
    candidates_unprocessed: Count
    assignments_received: Count
    assignments_processed: Count
    assignments_unprocessed: Count
    processing_complete: bool
    intake: IntakeSummary | None


class MultiAgentRunResult(ContractModel):
    schema_version: Literal['0.1.0']
    record_type: Literal['multi_agent_run_result']
    analysis_run_id: UuidText
    package_id: PackageId
    cutoff_at: UtcTimestamp
    generated_at: UtcTimestamp
    execution_mode: Literal['live', 'replay']
    agent_version: NonEmpty
    model_version: NonEmpty
    prompt_version: NonEmpty
    provider_calls: Count
    overall_status: Literal['complete', 'partial', 'failed']
    research_responses: list[ResearchHandoffResponse]
    strict_responses: list[AgentResponse]
    agent_execution_statuses: list[AgentExecutionStatus]
    research_grounding: list[ResearchGroundingReceipt]
    research_insights: list[SynthesizedInsight]
    information_gaps: list[SynthesizedGap]
    enrichment_suggestions: list[SynthesizedSuggestion]
    validated_signal_candidates: list[SignalCandidate]
    conflict_reviews: list[ConflictReviewRecord]
    review_recommendations: list[ReviewRecommendation]
    excluded_or_rejected_items: list[RejectedItem]
    coverage_summary: CoverageSummary
    limitations: list[NonEmpty]
    provider_call_diagnostics: list[ProviderCallDiagnostic] = Field(default_factory=list)


def _canonical(value) -> str:
    if isinstance(value, ContractModel):
        value = value.model_dump(mode='json')
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _key(value) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _reason(error: Exception) -> str:
    allowed = PROVIDER_ERROR_CODES | {
        'replay_response_missing', 'replay_review_missing', 'llm_call_budget_exhausted',
        'missing_llm_provider',
    }
    if isinstance(error, ProviderError) and str(error) in allowed:
        return str(error)
    return 'invalid_provider_output' if isinstance(error, ValidationError) else 'provider_failed'


def _gap(request, reason: str) -> InformationGap:
    return InformationGap(
        gap_id=f'gap:{request.request_id}:{reason}',
        missing_information=f'Usable source text or supported specialist findings are missing: {reason}.',
        related_source_refs=[],
        suggested_question='Which official documentation, maintainer history or comparison sources should D03-D05 retrieve and verify?',
    )


def _research_sources(request):
    sources = {}
    for ref in request.description_source_refs:
        sources.setdefault(ref, []).append((request.package_description, None, None))
    for candidate in request.candidates:
        sources.setdefault(candidate.source_ref, []).append(
            (candidate.text_excerpt, candidate.available_at, candidate.source_url))
    return sources


def _temporal(versions, cutoff):
    dates = [date for _, date, _ in versions]
    if any(date is not None and date > cutoff for date in dates):
        return 'after_cutoff'
    if any(date is None for date in dates):
        return 'unknown_availability'
    return 'as_of_not_independently_verified'


class MultiAgentCoordinator:
    def __init__(self, llm: SpecialistLLM | None, *, execution_mode: Literal['live', 'replay'],
                 d06_verifier: D06ReceiptVerifier | None = None,
                 clock: Callable[[], datetime] | None = None, max_llm_calls: int = 7,
                 max_review_calls: int = 0, max_candidates_per_agent: int = 12,
                 max_excerpt_chars: int = 5000, max_output_items: int = 20):
        if execution_mode not in {'live', 'replay'}:
            raise ValueError('execution_mode must be live or replay')
        for name, value, lower, upper in (
            ('max_llm_calls', max_llm_calls, 0, 32), ('max_review_calls', max_review_calls, 0, 8),
            ('max_candidates_per_agent', max_candidates_per_agent, 1, 50),
            ('max_excerpt_chars', max_excerpt_chars, 1, 10000),
            ('max_output_items', max_output_items, 1, 50),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f'{name} must be between {lower} and {upper}')
        self.llm, self.execution_mode, self.d06_verifier = llm, execution_mode, d06_verifier
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_llm_calls, self.max_review_calls = max_llm_calls, max_review_calls
        self.max_candidates = max_candidates_per_agent
        self.max_excerpt, self.max_output_items = max_excerpt_chars, max_output_items

    def analyze(self, request: MultiAgentRunRequest) -> MultiAgentRunResult:
        request = MultiAgentRunRequest.model_validate(request.model_dump())
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError('coordinator clock must be timezone-aware')
        generated_at = now.astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        if generated_at < request.cutoff_at:
            raise ValueError('generation cannot precede cutoff')
        # Run-local state keeps repeated invocations independent and budgets deterministic.
        calls, reviews_used = 0, 0
        rejected, statuses, research, receipts, strict = [], [], [], [], []
        trusted, strict_by_id, processed_refs = {}, {}, set()
        diagnostics = []

        def reject(lane, kind, item_id, reason, count=1):
            rejected.append(RejectedItem(lane=lane, agent_kind=kind, item_id=item_id,
                                         reason_code=reason, count=count))

        def invoke(method, data):
            nonlocal calls
            if self.llm is None:
                raise ProviderError('missing_llm_provider')
            if calls >= self.max_llm_calls:
                raise ProviderError('llm_call_budget_exhausted')
            calls += 1
            existing = getattr(self.llm, 'call_diagnostics', None)
            offset = len(existing) if isinstance(existing, list) else None
            try:
                return getattr(self.llm, method)(data.model_copy(deep=True))
            finally:
                latest = getattr(self.llm, 'call_diagnostics', None)
                if offset is not None and isinstance(latest, list):
                    request_ref = data.cluster_id if isinstance(data, ReviewInput) else data.request_id
                    diagnostics.extend(item.model_copy(deep=True) for item in latest[offset:]
                        if isinstance(item, ProviderCallDiagnostic)
                        and item.operation == method and item.request_ref == request_ref)

        research_by_kind = {r.agent_kind: r for r in request.research_requests}
        strict_by_kind = {r.agent_kind: r for r in request.strict_requests}
        for kind in SPECIALISTS:
            child = research_by_kind.get(kind) or ResearchHandoffRequest(
                schema_version='0.1.0', record_type='specialist_research_request',
                analysis_run_id=request.analysis_run_id, request_id=request_id_for(request.analysis_run_id, kind),
                agent_kind=kind, package_id=request.package_id, repo_id=None, cutoff_at=request.cutoff_at,
                package_description=None, description_source_refs=[], candidates=[], research_questions=[],
            )
            before_calls = calls
            before_rejected = len(rejected)
            ordered = sorted(child.candidates, key=lambda c: (
                c.topic_hint != ROUTES[kind][0], c.source_ref, c.candidate_id))
            candidates = [c.model_copy(update={
                'text_excerpt': c.text_excerpt[:self.max_excerpt] if c.text_excerpt is not None else None
            }) for c in ordered[:self.max_candidates]]
            bounded = ResearchHandoffRequest.model_validate(dict(child.model_dump(), candidates=[
                c.model_dump() for c in candidates]))
            if len(ordered) > len(candidates):
                reject('research', kind, child.request_id, 'candidate_budget_exhausted', len(ordered) - len(candidates))
            truncated = sum(c.text_excerpt is not None and len(c.text_excerpt) > self.max_excerpt for c in ordered[:self.max_candidates])
            if truncated:
                reject('research', kind, child.request_id, 'excerpt_truncated', truncated)
            base = {field: getattr(child, field) for field in (
                'schema_version', 'analysis_run_id', 'request_id', 'agent_kind', 'package_id', 'cutoff_at')}
            base.update(record_type='specialist_research_response', generated_at=generated_at)
            insights, gaps, suggestions, child_receipts = [], [], [], []
            processed = 0
            sources = _research_sources(bounded)
            if not any(text and text.strip() for versions in sources.values() for text, _, _ in versions):
                status, error = 'partial', 'no_usable_text'
                gaps = [_gap(child, error)]
            else:
                try:
                    value = invoke('research', bounded)
                    draft = ResearchDraft.model_validate(value.model_dump() if isinstance(value, ResearchDraft) else value)
                    for name in ('insights', 'information_gaps', 'enrichment_suggestions'):
                        items = getattr(draft, name)
                        if len(items) > self.max_output_items:
                            reject('research', kind, child.request_id, f'{name}_output_truncated', len(items) - self.max_output_items)
                    for index, item in enumerate(draft.insights[:self.max_output_items]):
                        versions = sources.get(item.source_ref, [])
                        matching = [version for version in versions if version[0] and item.supporting_quote in version[0]]
                        if not matching:
                            reject('research', kind, f'{child.request_id}:insight:{index}',
                                   'source_reference_unknown' if not versions else 'supporting_quote_not_found')
                            continue
                        insight = ResearchInsight(
                            insight_id=f'insight:{child.request_id}:{index}', statement=item.statement,
                            source_refs=[item.source_ref], limitation=item.limitation,
                        )
                        insights.append(insight)
                        # Unknown/late versions remain visible; a quote never authenticates availability.
                        temporal = _temporal(versions, child.cutoff_at)
                        _, available, url = matching[0]
                        child_receipts.append(ResearchGroundingReceipt(
                            agent_kind=kind, request_id=child.request_id, insight_id=insight.insight_id,
                            source_ref=item.source_ref, supporting_quote=item.supporting_quote,
                            source_url=url, available_at=available, temporal_state=temporal,
                        ))
                    gaps = draft.information_gaps[:self.max_output_items]
                    suggestions = draft.enrichment_suggestions[:self.max_output_items]
                    for items, ref_field in ((gaps, 'related_source_refs'), (suggestions, 'related_source_refs')):
                        for item in list(items):
                            if not set(getattr(item, ref_field)) <= set(sources):
                                items.remove(item)
                                reject('research', kind, child.request_id, 'source_reference_unknown')
                    if not insights and not gaps and not suggestions:
                        gaps = [_gap(child, 'no_grounded_findings')]
                        reject('research', kind, child.request_id, 'no_grounded_findings')
                    status = 'partial' if len(rejected) > before_rejected else 'complete'
                    error = 'research_validation_or_budget_gap' if status == 'partial' else None
                    response = ResearchHandoffResponse(**base, status=status, insights=insights,
                        information_gaps=gaps, enrichment_suggestions=suggestions, error_code=error)
                    response = validate_research_handoff_pair(bounded, response)
                    processed = len(candidates)
                    processed_refs.update(c.source_ref for c in candidates)
                except Exception as exc:
                    status, error = 'failed', _reason(exc)
                    reject('research', kind, child.request_id, error)
                    insights, gaps, suggestions, child_receipts = [], [_gap(child, error)], [], []
            response = ResearchHandoffResponse(**base, status=status, insights=insights,
                information_gaps=gaps, enrichment_suggestions=suggestions, error_code=error)
            research.append(validate_research_handoff_pair(child, response))
            receipts.extend(child_receipts)
            statuses.append(AgentExecutionStatus(
                agent_kind=kind, lane='research', status=status, llm_calls=calls - before_calls,
                error_code=error, received_candidates=len(child.candidates),
                processed_candidates=processed, unprocessed_candidates=len(child.candidates) - processed,
            ))

            strict_child = strict_by_kind.get(kind)
            if strict_child is None:
                statuses.append(AgentExecutionStatus(agent_kind=kind, lane='strict',
                    status='not_requested', llm_calls=0, error_code=None))
                continue
            strict_by_id[strict_child.request_id] = strict_child
            verified = self.d06_verifier is not None
            for evidence in strict_child.evidence:
                try:
                    valid = self.d06_verifier is not None and self.d06_verifier.verify(
                        strict_child.model_copy(deep=True), evidence.model_copy(deep=True)) is True
                except Exception:
                    valid = False
                if not valid:
                    verified = False
                    reject('strict', kind, evidence.evidence_id, 'd06_receipt_unverified')
            trusted[strict_child.request_id] = verified
            before_calls, before_rejected = calls, len(rejected)
            signals = []
            strict_base = {field: getattr(strict_child, field) for field in (
                'schema_version', 'analysis_run_id', 'request_id', 'agent_kind', 'package_id', 'cutoff_at')}
            strict_base.update(record_type='specialist_agent_response', generated_at=generated_at,
                agent_version=AGENT_VERSION, model_version=self.llm.model_id if self.llm else 'not_run',
                prompt_version=PROMPT_VERSION)
            try:
                if not verified and self.execution_mode == 'live':
                    raise ProviderError('d06_receipt_unverified')
                if sum(len(e.passage_text) for e in strict_child.evidence) > 60000:
                    raise ProviderError('strict_context_budget_exceeded')
                value = invoke('extract', strict_child)
                draft = ExtractionDraft.model_validate(value.model_dump() if isinstance(value, ExtractionDraft) else value)
                if len(draft.signals) > self.max_output_items:
                    reject('strict', kind, strict_child.request_id, 'signal_output_truncated', len(draft.signals) - self.max_output_items)
                for index, item in enumerate(draft.signals[:self.max_output_items]):
                    try:
                        signal = self._build_signal(strict_child, item, index)
                        validate_agent_pair(strict_child, AgentResponse(**strict_base, status='complete',
                            signals=[signal], error_code=None))
                        signals.append(signal)
                    except (ValueError, TypeError):
                        reject('strict', kind, f'{strict_child.request_id}:signal:{index}', 'invalid_strict_signal_or_citation')
                status = 'partial' if len(rejected) > before_rejected else 'complete'
                error = 'strict_output_rejected' if status == 'partial' else None
            except Exception as exc:
                signals = []
                status, error = 'failed', _reason(exc)
                if isinstance(exc, ProviderError) and str(exc) in {'d06_receipt_unverified', 'strict_context_budget_exceeded'}:
                    error = str(exc)
                reject('strict', kind, strict_child.request_id, error)
            response = validate_agent_pair(strict_child, AgentResponse(
                **strict_base, status=status, signals=signals, error_code=error))
            strict.append(response)
            statuses.append(AgentExecutionStatus(agent_kind=kind, lane='strict', status=status,
                llm_calls=calls - before_calls, error_code=error))

        candidates, reviews, recommendations = [], [], []
        clusters = self._signal_clusters(strict)
        for cluster in clusters:
            signals = [signal for _, signal in cluster]
            cluster_id = 'cluster:' + _key(sorted({
                (signal.package_id, signal.subject_id, signal.signal_type, c.original_event_id)
                for signal in signals for c in signal.citations}))
            source_ids = sorted({c.source_id for signal in signals for c in signal.citations})
            fingerprints = {_canonical(dict(statement=' '.join(s.statement.split()),
                event_at=s.event_at, announced_at=s.announced_at, effective_at=s.effective_at,
                proposed_use=s.proposed_use)) for s in signals}
            conflict = len(fingerprints) > 1
            if conflict:
                calls_before = calls
                reason = 'review_budget_unavailable'
                if reviews_used < self.max_review_calls and calls < self.max_llm_calls and self.llm is not None:
                    reviews_used += 1
                    passages = {}
                    for response, signal in cluster:
                        original = strict_by_id[response.request_id]
                        for citation in signal.citations:
                            for item in original.evidence:
                                if (item.evidence_id, item.source_id, item.source_revision) == (
                                        citation.evidence_id, citation.source_id, citation.source_revision):
                                    passages[_canonical(item)] = item
                    try:
                        value = invoke('review', ReviewInput(cluster_id=cluster_id,
                            signals=signals, passages=[passages[k] for k in sorted(passages)]))
                        decision = ReviewDraft.model_validate(value.model_dump() if isinstance(value, ReviewDraft) else value)
                        if decision.selected_signal_id is not None and decision.selected_signal_id not in {s.signal_id for s in signals}:
                            raise ValueError('review invented a signal')
                        for response, _ in cluster:
                            validate_agent_pair(strict_by_id[response.request_id], response)
                        recommendations.append(ReviewRecommendation(cluster_id=cluster_id, recommendation=decision))
                        reason = 'semantic_resolution_not_independently_verified'
                    except Exception as error:
                        reason = _reason(error) if isinstance(error, ProviderError) else 'review_failed_or_invalid'
                        reject('review', None, cluster_id, reason)
                review_calls = calls - calls_before
                reviews.append(ConflictReviewRecord(
                    schema_version='0.1.0', record_type='conflict_review',
                    analysis_run_id=request.analysis_run_id, cluster_id=cluster_id,
                    subject_id=signals[0].subject_id, signal_ids=sorted(s.signal_id for s in signals),
                    source_ids=source_ids, review_calls=review_calls,
                    reviewer_version=self.llm.model_id if review_calls else None,
                    status='unresolved', deterministic_recheck_passed=False,
                    final_use='report_only', reason=reason,
                ))
            all_verified = all(trusted[r.request_id] for r, _ in cluster)
            candidates.append(SignalCandidate(
                cluster_id=cluster_id, signal=signals[0],
                corroborating_signal_ids=sorted(s.signal_id for s in signals), source_ids=source_ids,
                trusted_d06_verified=all_verified,
                disposition='provisional' if all_verified and not conflict else 'report_only',
                reason_code='conflict_unresolved' if conflict else (
                    'd08_and_t025_verification_required' if all_verified else 'd06_receipt_unverified'),
            ))
        reviews = validate_review_references(validate_conflict_reviews(reviews), strict)
        findings, gaps, suggestions = self._synthesize_research(research, receipts)
        active = [s for s in statuses if s.status != 'not_requested']
        incomplete = any(s.status != 'complete' for s in active) or bool(rejected or reviews)
        if request.intake_summary and (request.intake_summary.omitted_candidates
                or request.intake_summary.truncated_excerpts or request.intake_summary.rejected_records):
            incomplete = True
        overall = 'failed' if all(s.status == 'failed' for s in active) else ('partial' if incomplete else 'complete')
        received_refs = {c.source_ref for r in request.research_requests for c in r.candidates}
        omitted = request.intake_summary.omitted_candidates if request.intake_summary else 0
        received = len(received_refs) + omitted
        processed = len(processed_refs)
        assignments = sum(s.received_candidates for s in statuses)
        assignments_processed = sum(s.processed_candidates for s in statuses)
        return MultiAgentRunResult(
            schema_version='0.1.0', record_type='multi_agent_run_result',
            analysis_run_id=request.analysis_run_id, package_id=request.package_id,
            cutoff_at=request.cutoff_at, generated_at=generated_at, execution_mode=self.execution_mode,
            agent_version=AGENT_VERSION, model_version=self.llm.model_id if self.llm else 'not_run',
            prompt_version=PROMPT_VERSION, provider_calls=calls,
            overall_status=overall, research_responses=research, strict_responses=strict,
            agent_execution_statuses=statuses, research_grounding=receipts,
            research_insights=findings, information_gaps=gaps, enrichment_suggestions=suggestions,
            validated_signal_candidates=candidates, conflict_reviews=reviews,
            review_recommendations=recommendations, excluded_or_rejected_items=rejected,
            provider_call_diagnostics=diagnostics,
            coverage_summary=CoverageSummary(
                candidates_received=received, candidates_processed=processed,
                candidates_unprocessed=received - processed, assignments_received=assignments,
                assignments_processed=assignments_processed,
                assignments_unprocessed=assignments - assignments_processed,
                processing_complete=not incomplete, intake=request.intake_summary,
            ), limitations=[
                'Research quote grounding checks textual support, not semantic truth or historical availability.',
                'Coverage describes supplied inputs only; no complete source census or E2 processing eligibility is certified.',
                'Strict candidates still require D08 coverage and trusted T-025 agent-event/processing verification.',
                'Conflicting claims remain report-only; reviewer recommendations do not certify semantic resolution.',
                'Enrichment suggestions are plans only; no source retrieval or feedback loop is executed.',
                'Replay uses stored provider content and is not a new live LLM analysis.' if self.execution_mode == 'replay'
                else 'Live provider output remains provisional after structural and textual validation.',
            ] + ([
                'Intake includes unverified discovery associations; processing does not verify package relevance or mapping history.'
            ] if request.intake_summary and request.intake_summary.unverified_scope_candidates else []),
        )

    @staticmethod
    def _build_signal(request, item, index):
        evidence = {e.evidence_id: e for e in request.evidence}
        citations = []
        for citation in item.citations:
            source = evidence.get(citation.evidence_id)
            if source is None:
                raise ValueError('unknown evidence')
            offset = source.passage_text.find(citation.quote)
            if offset < 0 or source.passage_text.find(citation.quote, offset + 1) >= 0:
                raise ValueError('missing or ambiguous quote')
            start = source.passage_start_offset + offset
            citations.append(SignalCitation(
                evidence_id=source.evidence_id, source_id=source.source_id, source_url=source.source_url,
                source_revision=source.source_revision, source_record_id=source.source_record_id,
                original_event_id=source.original_event_id, available_at=source.available_at,
                start_offset=start, end_offset=start + len(citation.quote), quote=citation.quote,
            ))
        return ExtractedSignal(
            signal_id=f'signal:{request.request_id}:{index}', package_id=request.package_id,
            context_level=ROUTES[request.agent_kind][1],
            **item.model_dump(exclude={'citations'}), citations=citations,
        )

    @staticmethod
    def _signal_clusters(responses):
        entries = sorted([(r, s) for r in responses for s in r.signals], key=lambda x: x[1].signal_id)
        groups = []
        # Explicit shared original-event identities form connected components; prose similarity never does.
        for entry in entries:
            _, signal = entry
            keys = {(signal.package_id, signal.subject_id, signal.signal_type, c.original_event_id)
                    for c in signal.citations}
            matching = [i for i, (known, _) in enumerate(groups) if keys & known]
            members = [entry]
            for index in reversed(matching):
                known, old = groups.pop(index)
                keys.update(known)
                members.extend(old)
            groups.append((keys, sorted(members, key=lambda x: x[1].signal_id)))
        return [members for _, members in sorted(groups, key=lambda g: _canonical(sorted(g[0])))]

    @staticmethod
    def _synthesize_research(responses, receipts):
        findings, gaps, suggestions = {}, {}, {}
        temporal = {r.insight_id: r.temporal_state for r in receipts}
        for response in responses:
            kind = response.agent_kind
            for insight in response.insights:
                key = _canonical((insight.statement, sorted(insight.source_refs)))
                if key not in findings:
                    findings[key] = SynthesizedInsight(insight=insight, agent_kinds=[],
                                                      limitations=[], temporal_states=[])
                item = findings[key]
                if kind not in item.agent_kinds:
                    item.agent_kinds.append(kind)
                item.limitations = sorted(set([*item.limitations, insight.limitation]))
                item.temporal_states = sorted(set([*item.temporal_states, temporal[insight.insight_id]]))
            for gap in response.information_gaps:
                key = _canonical(gap.model_dump(exclude={'gap_id'}))
                if key not in gaps:
                    gaps[key] = SynthesizedGap(gap=gap, agent_kinds=[])
                if kind not in gaps[key].agent_kinds:
                    gaps[key].agent_kinds.append(kind)
            for suggestion in response.enrichment_suggestions:
                key = _canonical(suggestion.model_dump(exclude={'suggestion_id'}))
                if key not in suggestions:
                    suggestions[key] = SynthesizedSuggestion(suggestion=suggestion, agent_kinds=[])
                if kind not in suggestions[key].agent_kinds:
                    suggestions[key].agent_kinds.append(kind)
        return ([findings[k] for k in sorted(findings)], [gaps[k] for k in sorted(gaps)],
                [suggestions[k] for k in sorted(suggestions)])
