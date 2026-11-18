"""T-026 runtime regressions. All providers/verifiers here are synthetic test doubles."""

import base64
import copy
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from reposeer.agents.cli import main
from reposeer.agents.intake import SPECIALISTS, IntakeSummary, build_research_requests_from_raw
from reposeer.agents.orchestrator import (
    MultiAgentCoordinator,
    MultiAgentRunRequest,
    MultiAgentRunResult,
)
from reposeer.agents.provider import (
    OpenAISpecialistLLM,
    ProviderError,
    ReplayBundle,
    ReplaySpecialistLLM,
    ResearchDraft,
    ReviewDraft,
)
from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.contracts.identity import text_sha256
from reposeer.contracts.models import RawRecord
from reposeer.contracts.specialist_agents import (
    AgentRequest,
    validate_agent_pair,
    validate_review_references,
)
from reposeer.storage.raw.jsonl import JsonlStorage

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / 'examples/agents'
CUTOFF = '2026-10-01T00:00:00.000Z'
NOW = datetime(2026, 10, 1, 0, 10, tzinfo=UTC)
RUN_ID = '00000000-0000-4000-8000-000000000026'


def example(name):
    return json.loads((EXAMPLES / f'{name}.json').read_bytes())


def demo():
    return MultiAgentRunRequest.model_validate(example('multi_agent_demo'))


class FakeProvider(ReplaySpecialistLLM):
    """Explicit fake: replay fixture content plus a recorded advisory reviewer invocation."""
    def __init__(self, bundle=None):
        super().__init__(bundle or ReplayBundle.model_validate(example('multi_agent_replay')))
        self.research_calls = []
        self.extract_calls = []
        self.review_calls = []

    def research(self, request):
        self.research_calls.append(request.model_copy(deep=True))
        return super().research(request)

    def extract(self, request):
        self.extract_calls.append(request.model_copy(deep=True))
        return super().extract(request)

    def review(self, conflict):
        self.review_calls.append(conflict.model_copy(deep=True))
        return ReviewDraft(selected_signal_id=conflict.signals[0].signal_id,
                           reason='Synthetic advisory selection; semantic truth is unverified.')


def run(request=None, provider=None, **options):
    return MultiAgentCoordinator(provider or FakeProvider(), execution_mode='replay',
                                 clock=lambda: NOW, **options).analyze(request or demo())


def raw_capture(index=1, text='Offline Python data processing.', *, scoped=True):
    """Build a canonical retained observation with an explicitly synthetic scope receipt."""
    url = f'https://example.test/synthetic/{index}'
    body_value = {'body': text}
    body = json.dumps(body_value).encode()
    scope = dict(
        package_id='pypi:synthetic-demo', repo_id='github:repository:900000001',
        repository_url='https://example.test/synthetic/repository', status='verified_current',
        basis='verified_current_discovery_scope', article_relevance='unverified',
        historical_validity='unverified', observed_at=CUTOFF,
        evidence=[dict(raw_record_id=f'raw:00000000-0000-4000-8000-{i:012d}',
                       source_url=f'https://example.test/synthetic/mapping/{i}', retrieved_at=CUTOFF)
                  for i in (900, 901)],
    ) if scoped else None
    payload = dict(
        record_stage='external_document', crawler_version='0.2.0', source='synthetic',
        source_url=url, url=url, fetched_url=url, fetched_at=CUTOFF,
        source_item_id=None, candidate_raw_record_id=None, discovery=None,
        response_body_base64=base64.b64encode(body).decode(), content_type='application/json',
        etag=None, last_modified=None, native_payload=body_value, html=None,
        title='Synthetic retained capture', author=None, published_at=None, published_date=None,
        published_precision='unknown', updated_at=None, content=text,
        content_status='full' if text is not None else 'unavailable',
        content_sha256=text_sha256(text) if text is not None else None,
        word_count=len(text.split()) if text is not None else None, content_available_at=None,
        historical_version_status='unverified', relevance_status='unverified', repository_scope=scope,
    )
    return validate_external_document_record(RawRecord(
        schema_version='0.1.0', record_type='raw_record',
        raw_record_id=f'raw:00000000-0000-4000-8000-{index:012d}', run_id=RUN_ID,
        source='web_article', source_record_type='article', source_record_id=None,
        repo_id='github:repository:900000001', package_id='pypi:synthetic-demo',
        source_url=url, retrieved_at=CUTOFF, event_at=None, available_at=None, http_status=200,
        raw_body_sha256=hashlib.sha256(body).hexdigest(), payload=payload,
    ))


def replay_for_requests(requests):
    """Saved synthetic draft content for canonical raw-intake CLI tests; never a live result."""
    entries = []
    for request in requests:
        candidate = request.candidates[0]
        entries.append(dict(request_id=request.request_id, agent_kind=request.agent_kind, output=dict(
            insights=[dict(source_ref=candidate.source_ref, supporting_quote=candidate.text_excerpt,
                           statement='The retained text describes offline Python data processing.',
                           limitation='Package relevance and historical availability are unverified.')],
            information_gaps=[], enrichment_suggestions=[],
        )))
    return ReplayBundle.model_validate(dict(schema_version='0.1.0', record_type='multi_agent_replay',
        provenance='synthetic', model_id='synthetic-raw-replay', research=entries, strict=[], reviews=[]))


def strict_case():
    request = demo()
    original = example('functional_request')
    original['analysis_run_id'] = request.analysis_run_id
    child = AgentRequest.model_validate(original)
    request.strict_requests = [child]
    signal = example('functional_response')['signals'][0]
    output = {key: signal[key] for key in (
        'signal_type', 'statement', 'subject_id', 'event_at', 'announced_at',
        'effective_at', 'proposed_use', 'uncertainty')}
    output['citations'] = [{key: c[key] for key in ('evidence_id', 'quote')} for c in signal['citations']]
    bundle = example('multi_agent_replay')
    bundle['strict'] = [dict(request_id=child.request_id, agent_kind=child.agent_kind,
                             output=dict(signals=[output]))]
    return request, bundle


def test_three_specialists_grounded_replay_is_deterministic_and_preserves_inputs():
    request = demo()
    before = request.model_dump_json()
    coordinator = MultiAgentCoordinator(FakeProvider(), execution_mode='replay', clock=lambda: NOW)
    result = coordinator.analyze(request)
    assert result == coordinator.analyze(request)
    assert request.model_dump_json() == before
    assert result.execution_mode == 'replay' and result.overall_status == 'complete'
    assert result.provider_calls == 3 and result.model_version == 'synthetic-replay-only'
    assert [r.agent_kind for r in result.research_responses] == list(SPECIALISTS)
    assert all(r.analysis_run_id == request.analysis_run_id and r.package_id == request.package_id
               and r.cutoff_at == request.cutoff_at for r in result.research_responses)
    assert len(result.research_grounding) == len(result.research_insights) == 3
    assert len(result.information_gaps) == len(result.enrichment_suggestions) == 3
    assert result.coverage_summary.source_coverage == 'unknown'
    assert result.coverage_summary.processing_complete
    assert MultiAgentRunResult.model_validate_json(result.model_dump_json()) == result


def test_no_evidence_fills_all_three_requests_and_skips_provider():
    request = demo().model_copy(update={'research_requests': []})
    provider = FakeProvider()
    result = run(request, provider)
    assert not provider.research_calls
    assert len(result.research_responses) == 3
    assert all(r.insights == [] and r.information_gaps for r in result.research_responses)
    assert result.research_insights == result.research_grounding == []
    assert result.overall_status == 'partial'


def test_description_only_grounding_dedupes_without_claiming_independent_verification():
    request, bundle = demo(), example('multi_agent_replay')
    for child, entry in zip(request.research_requests, bundle['research'], strict=True):
        child.candidates = []
        entry['output']['insights'] = [dict(source_ref=child.description_source_refs[0],
            supporting_quote=child.package_description, statement=child.package_description,
            limitation='Unverified supplied description; not a historical functional profile.')]
        entry['output'].update(information_gaps=[], enrichment_suggestions=[])
    result = run(request, FakeProvider(ReplayBundle.model_validate(bundle)))
    assert len(result.research_responses) == 3 and len(result.research_insights) == 1
    assert result.research_insights[0].agent_kinds == list(SPECIALISTS)
    assert result.research_insights[0].temporal_states == ['unknown_availability']
    assert result.coverage_summary.historical_validity == 'unverified'


def test_research_distinguishes_late_unknown_and_unverified_asof_sources():
    request = demo()
    request.research_requests[0].candidates[0].available_at = '2026-11-01T00:00:00.000Z'
    request.research_requests[2].candidates[0].available_at = '2026-09-01T00:00:00.000Z'
    result = run(request)
    assert {r.temporal_state for r in result.research_grounding} == {
        'after_cutoff', 'unknown_availability', 'as_of_not_independently_verified'}
    assert result.validated_signal_candidates == []
    assert request.research_requests[1].candidates[0].available_at is None


@pytest.mark.parametrize('case', ['invented_quote', 'unknown_reference'])
def test_bad_research_grounding_is_audited_without_losing_other_agents(case):
    bundle = example('multi_agent_replay')
    target = bundle['research'][0]['output']['insights'][0]
    target['supporting_quote' if case == 'invented_quote' else 'source_ref'] = 'invented'
    result = run(provider=FakeProvider(ReplayBundle.model_validate(bundle)))
    assert len(result.research_insights) == 2
    assert result.research_responses[0].status == 'partial'
    assert result.overall_status == 'partial'
    assert result.excluded_or_rejected_items[0].reason_code == (
        'supporting_quote_not_found' if case == 'invented_quote' else 'source_reference_unknown')


def test_label_probability_and_llm_metadata_are_not_accepted_as_content():
    for field in ('probability', 'ground_truth_label', 'analysis_run_id'):
        provider = FakeProvider()
        good = provider.research
        def invalid(request, good=good, field=field):
            output = good(request).model_dump()
            if request.agent_kind == SPECIALISTS[0]:
                output[field] = 'invented'
            return output
        provider.research = invalid
        result = run(provider=provider)
        assert result.research_responses[0].status == 'failed'
        assert len(result.research_insights) == 2
    data = run().model_dump()
    data['probability'] = 0.9
    with pytest.raises(ValidationError):
        MultiAgentRunResult.model_validate(data)


def test_one_provider_failure_is_sanitized_and_preserves_two_results_and_coverage():
    provider = FakeProvider()
    good = provider.research
    def fail_one(request):
        if request.agent_kind == SPECIALISTS[0]:
            raise RuntimeError('secret-key and full provider error response')
        return good(request)
    provider.research = fail_one
    result = run(provider=provider)
    assert [r.status for r in result.research_responses] == ['failed', 'complete', 'complete']
    assert len(result.research_insights) == 2 and result.overall_status == 'partial'
    assert result.coverage_summary.assignments_processed == 2
    assert result.coverage_summary.assignments_unprocessed == 1
    assert not result.coverage_summary.processing_complete
    assert 'secret-key' not in result.model_dump_json()


def test_call_budget_and_output_limits_are_explicit():
    provider = FakeProvider()
    result = run(provider=provider, max_llm_calls=1)
    assert len(provider.research_calls) == 1
    assert result.overall_status == 'partial'
    assert any(r.reason_code == 'llm_call_budget_exhausted' for r in result.excluded_or_rejected_items)
    bundle = example('multi_agent_replay')
    bundle['research'][0]['output']['insights'] *= 2
    result = run(provider=FakeProvider(ReplayBundle.model_validate(bundle)), max_output_items=1)
    assert any(r.reason_code == 'insights_output_truncated' for r in result.excluded_or_rejected_items)
    assert len(result.research_responses[0].insights) == 1


def test_raw_intake_preserves_missing_text_and_dates_and_reports_scope_and_budget(tmp_path):
    path = tmp_path / 'captures.jsonl'
    first, second = raw_capture(1, None), raw_capture(2)
    unscoped, foreign = raw_capture(3, scoped=False), raw_capture(4)
    foreign.package_id = foreign.payload['repository_scope']['package_id'] = 'pypi:other'
    duplicate = second.model_dump(mode='json')
    duplicate['payload'] = dict(reversed(list(duplicate['payload'].items())))
    records = [foreign, second, unscoped, first, duplicate]
    JsonlStorage().write(path, records)
    before = path.read_bytes()
    summary = IntakeSummary()
    requests = build_research_requests_from_raw(path, analysis_run_id=RUN_ID,
        package_id='pypi:synthetic-demo', cutoff_at=CUTOFF, max_candidates=1, summary=summary)
    assert [r.agent_kind for r in requests] == list(SPECIALISTS)
    assert all(r.candidates[0].source_ref == first.raw_record_id for r in requests)
    assert all(r.candidates[0].text_excerpt is None and r.candidates[0].available_at is None
               and r.candidates[0].published_at is None for r in requests)
    assert summary.accepted_candidates == 2 and summary.omitted_candidates == 1
    assert summary.rejected_records == 2 and summary.duplicate_records == 1
    assert path.read_bytes() == before
    request = MultiAgentRunRequest(schema_version='0.1.0', record_type='multi_agent_run_request',
        analysis_run_id=RUN_ID, package_id='pypi:synthetic-demo', cutoff_at=CUTOFF,
        research_requests=requests, strict_requests=[], intake_summary=summary)
    provider = FakeProvider()
    result = run(request, provider)
    assert not provider.research_calls and result.research_insights == []
    assert result.coverage_summary.candidates_unprocessed == 2
    JsonlStorage().write(path, reversed(records))
    again = build_research_requests_from_raw(path, analysis_run_id=RUN_ID,
        package_id='pypi:synthetic-demo', cutoff_at=CUTOFF, max_candidates=1)
    assert again == requests


def test_raw_intake_text_budget_and_conflicting_revisions_fail_honestly(tmp_path):
    path = tmp_path / 'raw.jsonl'
    one = raw_capture(text='Offline Python data processing.' * 3)
    JsonlStorage().write(path, [one])
    summary = IntakeSummary()
    requests = build_research_requests_from_raw(path, analysis_run_id=RUN_ID,
        package_id='pypi:synthetic-demo', cutoff_at=CUTOFF, max_excerpt_chars=10, summary=summary)
    assert requests[0].candidates[0].text_excerpt == one.payload['content'][:10]
    assert summary.truncated_excerpts == 1
    two = raw_capture(text='A conflicting retained observation.')
    JsonlStorage().write(path, [one, two])
    with pytest.raises(ValueError, match='conflicting raw_record_id'):
        build_research_requests_from_raw(path, analysis_run_id=RUN_ID,
            package_id='pypi:synthetic-demo', cutoff_at=CUTOFF)


def test_coordinator_enforces_input_limits_and_identity():
    request = demo()
    duplicate = request.research_requests[0].model_copy(deep=True)
    request.research_requests = [duplicate, duplicate]
    with pytest.raises(ValidationError, match='specialists per lane'):
        run(request)
    request = demo()
    request.research_requests[0].package_id = 'pypi:other'
    with pytest.raises(ValidationError, match='child run/package/cutoff'):
        run(request)
    request = demo()
    candidate = request.research_requests[0].candidates[0]
    request.research_requests[0].candidates.append(candidate.model_copy(update={'candidate_id': 'second'}))
    result = run(request, max_candidates_per_agent=1)
    assert any(r.reason_code == 'candidate_budget_exhausted' for r in result.excluded_or_rejected_items)
    assert not result.coverage_summary.processing_complete


def test_strict_offsets_are_built_from_sources_and_untrusted_receipts_stay_report_only():
    request, bundle = strict_case()
    source = request.strict_requests[0].evidence[0]
    quote = source.passage_text[2:]
    source.passage_text = '\u03a9 ' + source.passage_text
    source.passage_sha256 = source.source_content_sha256 = text_sha256(source.passage_text)
    bundle['strict'][0]['output']['signals'][0]['citations'][0]['quote'] = quote
    result = run(request, FakeProvider(ReplayBundle.model_validate(bundle)))
    response = result.strict_responses[0]
    assert validate_agent_pair(request.strict_requests[0], response) == response
    citation = response.signals[0].citations[0]
    assert citation.start_offset == 254 and citation.end_offset == 254 + len(citation.quote)
    candidate = result.validated_signal_candidates[0]
    assert not candidate.trusted_d06_verified and not candidate.e2_eligible
    assert candidate.disposition == 'report_only'


@pytest.mark.parametrize('case', ['unknown_evidence', 'invented_quote', 'ambiguous_quote'])
def test_strict_rejects_invented_or_ambiguous_citations(case):
    request, bundle = strict_case()
    citation = bundle['strict'][0]['output']['signals'][0]['citations'][0]
    if case == 'unknown_evidence':
        citation['evidence_id'] = 'invented-source'
    elif case == 'invented_quote':
        citation['quote'] = 'A nonexistent quote'
    else:
        source = request.strict_requests[0].evidence[0]
        source.passage_text = source.passage_text + ' ' + source.passage_text
        source.passage_sha256 = text_sha256(source.passage_text)
        source.source_content_sha256 = source.passage_sha256
    result = run(request, FakeProvider(ReplayBundle.model_validate(bundle)))
    assert result.strict_responses[0].signals == []
    assert result.validated_signal_candidates == []
    assert any(r.reason_code == 'invalid_strict_signal_or_citation' for r in result.excluded_or_rejected_items)


def test_live_strict_requires_injected_receipt_backend_and_still_cannot_promote_e2():
    class TestOnlyVerifier:
        def __init__(self, accepted):
            self.accepted = accepted
        def verify(self, request, evidence):
            if self.accepted == 'error':
                raise RuntimeError('sensitive backend details')
            return self.accepted
    request, bundle = strict_case()
    for verifier in (None, TestOnlyVerifier(False), TestOnlyVerifier('error'), TestOnlyVerifier(True)):
        provider = FakeProvider(ReplayBundle.model_validate(bundle))
        result = MultiAgentCoordinator(provider, execution_mode='live', d06_verifier=verifier,
                                       clock=lambda: NOW).analyze(request)
        verified = verifier is not None and verifier.accepted is True
        assert len(provider.extract_calls) == (1 if verified else 0)
        if verified:
            candidate = result.validated_signal_candidates[0]
            assert candidate.trusted_d06_verified and not candidate.e2_eligible
            assert candidate.reason_code == 'd08_and_t025_verification_required'
        else:
            assert result.strict_responses[0].status == 'failed'
        assert 'sensitive backend details' not in result.model_dump_json()


def test_original_event_dedup_does_not_merge_different_events_with_same_prose():
    request, bundle = strict_case()
    signal = bundle['strict'][0]['output']['signals'][0]
    bundle['strict'][0]['output']['signals'].append(copy.deepcopy(signal))
    result = run(request, FakeProvider(ReplayBundle.model_validate(bundle)))
    assert len(result.validated_signal_candidates) == 1
    assert len(result.validated_signal_candidates[0].corroborating_signal_ids) == 2
    assert result.conflict_reviews == []
    source = request.strict_requests[0].evidence[0].model_copy(deep=True)
    source.evidence_id, source.source_id, source.original_event_id = 'second-evidence', 'second-source', 'second-original'
    request.strict_requests[0].evidence.append(source)
    bundle['strict'][0]['output']['signals'][1]['citations'][0]['evidence_id'] = source.evidence_id
    result = run(request, FakeProvider(ReplayBundle.model_validate(bundle)))
    assert len(result.validated_signal_candidates) == 2


def test_conflict_review_is_bounded_rechecked_and_cannot_certify_semantics():
    request, bundle = strict_case()
    first = bundle['strict'][0]['output']['signals'][0]
    conflict = copy.deepcopy(first)
    conflict['event_at'] = '2026-09-27T00:00:00.000Z'
    second_source = request.strict_requests[0].evidence[0].model_copy(deep=True)
    second_source.evidence_id, second_source.source_id, second_source.original_event_id = 'second-evidence', 'second-source', 'second-original'
    request.strict_requests[0].evidence.append(second_source)
    second, second_conflict = copy.deepcopy(first), copy.deepcopy(conflict)
    for signal in (second, second_conflict):
        signal['citations'][0]['evidence_id'] = second_source.evidence_id
    bundle['strict'][0]['output']['signals'] = [first, conflict, second, second_conflict]
    provider = FakeProvider(ReplayBundle.model_validate(bundle))
    coordinator = MultiAgentCoordinator(provider, execution_mode='replay', clock=lambda: NOW,
                                         max_review_calls=1)
    result = coordinator.analyze(request)
    assert len(provider.review_calls) == 1
    assert len(provider.review_calls[0].passages) == 1  # Only this cluster's conflicting source.
    assert len(result.conflict_reviews) == 2 and sum(r.review_calls for r in result.conflict_reviews) == 1
    assert all(r.status == 'unresolved' and r.final_use == 'report_only' for r in result.conflict_reviews)
    assert all(c.disposition == 'report_only' and not c.e2_eligible for c in result.validated_signal_candidates)
    assert validate_review_references(result.conflict_reviews, result.strict_responses) == result.conflict_reviews
    assert result == coordinator.analyze(request)
    assert len(provider.review_calls) == 2  # Budget resets only for the next run.
    provider.review_calls.clear()
    def failed_review(conflict):
        provider.review_calls.append(conflict)
        raise RuntimeError('sensitive-review-provider-details')
    provider.review = failed_review
    failed = coordinator.analyze(request)
    assert len(provider.review_calls) == 1
    assert sum(r.review_calls for r in failed.conflict_reviews) == 1
    assert all(r.status == 'unresolved' for r in failed.conflict_reviews)
    assert 'sensitive-review-provider-details' not in failed.model_dump_json()



def test_live_adapter_uses_content_only_schemas_bounded_sdk_and_safe_errors(monkeypatch):
    captured = {}
    draft = ResearchDraft.model_validate(example('multi_agent_replay')['research'][0]['output'])
    def parse(**kwargs):
        captured['parse'] = kwargs
        return SimpleNamespace(status='completed', output=[], output_parsed=draft)
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse), close=lambda: None)
    def create(**kwargs):
        captured['client'] = kwargs
        return client
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=create))
    provider = OpenAISpecialistLLM(model='explicit-test-model', api_key='test-secret')
    assert provider.research(demo().research_requests[0]) == draft
    assert captured['client']['timeout'] == 20 and captured['client']['max_retries'] == 0
    call = captured['parse']
    assert call['store'] is False and call['max_output_tokens'] == 3200
    assert call['timeout'] == 20
    assert call['text_format'] is ResearchDraft
    assert 'analysis_run_id' not in call['text_format'].model_fields
    assert 'untrusted data' in call['input'][0]['content']
    assert 'environment variables' in call['input'][0]['content']
    def fail(**kwargs):
        raise RuntimeError('test-secret provider response body')
    client.responses.parse = fail
    with pytest.raises(ProviderError, match='llm_request_or_output_failed') as error:
        provider.research(demo().research_requests[0])
    assert 'test-secret' not in str(error.value)
    provider.close()
    def with_options(**kwargs):
        captured['provided_options'] = kwargs
        return client
    client.with_options = with_options
    OpenAISpecialistLLM(model='explicit-test-model', client=client, timeout=9)
    assert captured['provided_options'] == {'timeout': 9, 'max_retries': 0}


def test_missing_live_configuration_fails_clearly_without_output(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('REPOSEER_AGENT_MODEL', raising=False)
    args = ['--mode', 'live', '--input', str(EXAMPLES / 'multi_agent_demo.json'),
            '--output', str(tmp_path / 'live.json')]
    assert main(args) == 2
    assert 'missing_agent_model_configuration' in capsys.readouterr().out
    assert main([*args, '--model', 'explicit-test-model']) == 2
    assert 'missing_openai_api_key' in capsys.readouterr().out
    assert not (tmp_path / 'live.json').exists()


def test_cli_replay_is_offline_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('REPOSEER_AGENT_MODEL', raising=False)
    monkeypatch.setitem(sys.modules, 'openai', None)
    output = tmp_path / 'run.json'
    args = ['--mode', 'replay', '--input', str(EXAMPLES / 'multi_agent_demo.json'),
            '--responses', str(EXAMPLES / 'multi_agent_replay.json'), '--output', str(output)]
    assert main(args) == 0
    result = MultiAgentRunResult.model_validate_json(output.read_bytes())
    assert result.execution_mode == 'replay' and len(result.research_responses) == 3
    before = output.read_bytes()
    assert main(args) == 2 and output.read_bytes() == before
    completed = subprocess.run([sys.executable, '-m', 'reposeer.agents.cli', *args[:-1],
                                str(tmp_path / 'module.json')], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_cli_can_read_canonical_raw_documents_and_preserve_dataset(tmp_path):
    raw, replay, output = tmp_path / 'raw.jsonl', tmp_path / 'replay.json', tmp_path / 'out.json'
    JsonlStorage().write(raw, [raw_capture()])
    before = raw.read_bytes()
    requests = build_research_requests_from_raw(raw, analysis_run_id=RUN_ID,
        package_id='pypi:synthetic-demo', cutoff_at=CUTOFF)
    replay.write_text(replay_for_requests(requests).model_dump_json(), encoding='utf-8')
    assert main(['--mode', 'replay', '--raw-documents', str(raw), '--responses', str(replay),
        '--analysis-run-id', RUN_ID, '--package-id', 'pypi:synthetic-demo', '--cutoff-at', CUTOFF,
        '--output', str(output)]) == 0
    result = MultiAgentRunResult.model_validate_json(output.read_bytes())
    assert len(result.research_grounding) == 3 and len(result.research_insights) == 1
    assert result.coverage_summary.intake.accepted_candidates == 1
    assert raw.read_bytes() == before
