"""Project grounded D07 output to report-only T-021 evidence; never promote E2 events."""

from reposeer.agents import MultiAgentRunRequest, MultiAgentRunResult
from reposeer.agents.intake import SPECIALISTS, IntakeSummary, request_id_for
from reposeer.analysis.application_inputs import AnalysisBundle
from reposeer.contracts.analysis import (
    AgentProvenance,
    AnalysisCoverage,
    AnalysisEvidence,
    EvidenceCitation,
    SourceProvenance,
)
from reposeer.contracts.specialist_agents import (
    ResearchCandidate,
    ResearchHandoffRequest,
    validate_agent_pair,
    validate_research_handoff_pair,
)

CONTEXTS = {'package_specific': 'direct_package', 'function_level': 'same_function',
            'alternative_level': 'alternative'}
KINDS = dict(zip(SPECIALISTS, ('maintenance', 'functional_demand', 'migration'), strict=True))


def agent_request(bundle: AnalysisBundle, analysis_run_id: str) -> MultiAgentRunRequest:
    if bundle.agents is not None:
        data = bundle.agents.model_dump()
        data['analysis_run_id'] = analysis_run_id
        for lane in ('research_requests', 'strict_requests'):
            for child in data[lane]:
                child['analysis_run_id'] = analysis_run_id
        return MultiAgentRunRequest.model_validate(data)
    core = bundle.core
    documents = [document for document in (bundle.external.documents if bundle.external else [])
                 if document.relevance.decision == 'relevant']
    candidates = [ResearchCandidate(
        candidate_id=document.source_id, source_ref=document.source_id,
        source_url=document.article.canonical_url, source_kind='external',
        relevance_status='relevant', topic_hint=None,
        text_excerpt=document.article.content_text[:10000] if document.article.content_text else None,
        published_at=document.article.published_at, available_at=document.article.content_available_at,
        fetched_at=document.article.provenance.retrieved_at,
    ) for document in documents[:50]]
    # The D07 coordinator bounds excerpts and retains its own truncation/omission audit.
    requests = [ResearchHandoffRequest(
        schema_version='0.1.0', record_type='specialist_research_request',
        analysis_run_id=analysis_run_id, request_id=request_id_for(analysis_run_id, kind),
        agent_kind=kind, package_id=core.snapshot.package_id, repo_id=core.snapshot.repo_id,
        cutoff_at=core.snapshot.cutoff_at, package_description=None, description_source_refs=[],
        candidates=candidates,
        research_questions=[],
    ) for kind in SPECIALISTS]
    return MultiAgentRunRequest(
        schema_version='0.1.0', record_type='multi_agent_run_request',
        analysis_run_id=analysis_run_id, package_id=core.snapshot.package_id,
        cutoff_at=core.snapshot.cutoff_at, research_requests=requests, strict_requests=[],
        intake_summary=IntakeSummary(
            records_seen=len(documents), accepted_candidates=len(documents),
            retained_candidates=len(candidates), omitted_candidates=len(documents) - len(candidates),
            truncated_excerpts=sum(bool(row.article.content_text) and len(row.article.content_text) > 10000
                                   for row in documents[:50]),
        ),
    )


def report_agent_output(bundle: AnalysisBundle, request: MultiAgentRunRequest,
                        result: MultiAgentRunResult, sources: dict[str, SourceProvenance]):
    """Keep exact source metadata and offsets; discard unresolved report references."""
    evidence, used, omitted = [], set(), 0
    if any(getattr(result, field) != getattr(request, field)
           for field in ('analysis_run_id', 'package_id', 'cutoff_at')):
        raise ValueError('agent_run_identity_mismatch')
    research_requests = {row.request_id: row for row in request.research_requests}
    strict_requests = {row.request_id: row for row in request.strict_requests}
    for response in result.research_responses:
        # The coordinator may synthesize empty planning requests for missing specialists.
        if response.request_id in research_requests:
            validate_research_handoff_pair(research_requests[response.request_id], response)
        elif response.insights:
            raise ValueError('agent_research_request_missing')
    for response in result.strict_responses:
        validate_agent_pair(strict_requests[response.request_id], response)
    passages = {}
    for document in (bundle.external.documents if bundle.external else []):
        if document.article.content_text:
            passages.setdefault(document.source_id, []).append((document.article.content_text, 0))
    for child in request.strict_requests:
        for item in child.evidence:
            source = sources.get(item.source_id)
            if source is not None and (
                source.source_url == item.source_url and source.revision == item.source_revision
                and source.source_record_id == item.source_record_id
                and source.content_sha256 == item.source_content_sha256
                and source.available_at == item.available_at
            ):
                passages.setdefault(item.source_id, []).append((item.passage_text, item.passage_start_offset))
    metadata = dict(agent_version=result.agent_version, model_version=result.model_version,
                    prompt_version=result.prompt_version)
    for response in result.strict_responses:
        original_evidence = {item.evidence_id: item for item in strict_requests[response.request_id].evidence}
        for signal in response.signals:
            if any(c.source_id not in sources or any((
                sources[c.source_id].source_url != c.source_url,
                sources[c.source_id].revision != c.source_revision,
                sources[c.source_id].source_record_id != c.source_record_id,
                sources[c.source_id].content_sha256 != original_evidence[c.evidence_id].source_content_sha256,
                sources[c.source_id].available_at != c.available_at,
            )) for c in signal.citations):
                omitted += 1
                continue
            evidence.append(AnalysisEvidence(
                evidence_id='agent:' + signal.signal_id, package_id=result.package_id,
                origin='agent', context=CONTEXTS[signal.context_level],
                signal_type=KINDS[response.agent_kind], claim=signal.statement,
                event_at=signal.event_at, citations=[EvidenceCitation(
                    source_id=c.source_id, quote=c.quote, start_offset=c.start_offset, end_offset=c.end_offset,
                ) for c in signal.citations], use='report_only',
                exclusion_reason='independent_d08_d09_verification_required', uncertainty=signal.uncertainty,
                agent=AgentProvenance(agent_id=response.agent_kind, **metadata),
            ))
            used.update(c.source_id for c in signal.citations)
    insights = {item.insight_id: (response, item) for response in result.research_responses for item in response.insights}
    for receipt in result.research_grounding:
        response, insight = insights[receipt.insight_id]
        if (receipt.request_id != response.request_id or receipt.agent_kind != response.agent_kind
                or receipt.source_ref not in insight.source_refs):
            raise ValueError('research_receipt_identity_mismatch')
        source = sources.get(receipt.source_ref)
        matches = [(text.find(receipt.supporting_quote) + offset) for text, offset
                   in passages.get(receipt.source_ref, []) if receipt.supporting_quote in text]
        if source is None or not matches or (receipt.source_url is not None
                                             and source.source_url != receipt.source_url):
            omitted += 1
            continue
        start = min(matches)
        evidence.append(AnalysisEvidence(
            evidence_id='agent:' + insight.insight_id, package_id=result.package_id, origin='agent',
            context=dict(zip(SPECIALISTS, ('direct_package', 'same_function', 'alternative'),
                             strict=True))[receipt.agent_kind], signal_type=KINDS[receipt.agent_kind],
            claim=insight.statement, event_at=None, citations=[EvidenceCitation(
                source_id=receipt.source_ref, quote=receipt.supporting_quote,
                start_offset=start, end_offset=start + len(receipt.supporting_quote),
            )], use='report_only', exclusion_reason='research_not_independently_verified',
            uncertainty=insight.limitation, agent=AgentProvenance(agent_id=receipt.agent_kind, **metadata),
        ))
        used.add(receipt.source_ref)
    observed = {item.source_ref for child in request.research_requests for item in child.candidates}
    observed.update(item.source_id for child in request.strict_requests for item in child.evidence)
    # Processing completion does not authenticate historical source/processing coverage.
    coverage = AnalysisCoverage(
        coverage_id='application:agents', stage='agent', source_ids=sorted(used),
        status='partial' if observed or evidence else 'unavailable', source_coverage='unknown',
        historical_validity='unverified', window=None,
        records_observed=len(observed) if observed else None,
        eligible_documents=None, processed_documents=None,
        missing_reason='agent_processing_and_history_not_independently_verified',
    )
    return evidence, coverage, omitted
