"""T-027 offline behavior through the real contracts, CORE model, D25/D26 and CLI."""

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reposeer.agents import MultiAgentCoordinator
from reposeer.agents.provider import ReplayBundle, ReplaySpecialistLLM
from reposeer.analysis.application import (
    ApplicationOrchestrator,
    ImpactContext,
    OfficialContext,
    analyze_repository,
)
from reposeer.analysis.application_inputs import (
    AnalysisBundle,
    AnalysisInputError,
    StoredAnalysisBackend,
)
from reposeer.analysis.collection_backend import observation_source
from reposeer.cli.app import app
from reposeer.contracts.analysis import AnalysisCoverage, ModelResult, validate_final_report
from reposeer.maintenance.labels import HistoricalOfficialStatus

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples/application'
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
CUTOFF = '2026-10-01T00:00:00.000Z'
MODEL = EXAMPLES / 'synthetic_model_a.json'


@pytest.fixture
def bundle():
    return AnalysisBundle.model_validate_json((EXAMPLES / 'synthetic_bundle.json').read_bytes())


def service(bundle, **kwargs):
    return ApplicationOrchestrator(StoredAnalysisBackend([bundle]), model_path=MODEL,
                                   clock=lambda: NOW, **kwargs)


def coordinator():
    provider = ReplaySpecialistLLM(ReplayBundle.model_validate_json(
        (EXAMPLES / 'synthetic_agent_replay.json').read_bytes()))
    return MultiAgentCoordinator(provider, execution_mode='replay', clock=lambda: NOW)


def official_context(bundle, *, scheduled=False):
    evidence = bundle.core.official_evidence
    if not evidence:
        from reposeer.contracts.analysis import AnalysisEvidence, EvidenceCitation
        evidence = [AnalysisEvidence(
            evidence_id='official:synthetic', package_id=bundle.core.snapshot.package_id,
            origin='official', context='direct_package', signal_type='maintenance',
            claim='Synthetic independently reviewed cessation notice.', event_at=CUTOFF,
            citations=[EvidenceCitation(source_id='source:core', quote='Synthetic notice.',
                                        start_offset=0, end_offset=17)], use='official_status',
            exclusion_reason=None, uncertainty=None, agent=None,
        )]
    status = HistoricalOfficialStatus(
        event_type='MAINTENANCE_CEASED', cessation_confirmed=not scheduled,
        announced_at=CUTOFF, effective_at='2027-01-01T00:00:00.000Z' if scheduled else CUTOFF,
        verified_at=CUTOFF, revoked_at=None, ledger_revision='synthetic-official-1',
        verifier='synthetic-independent-reviewer', verification_policy_version='synthetic-d09',
        evidence_ids=[evidence[0].evidence_id], available_at=CUTOFF,
    )
    return OfficialContext(statuses=[status], sources=bundle.core.sources, evidence=evidence)


class Ledger:
    def __init__(self, context, *, publish_at_call=1, fail_at_call=None):
        self.context, self.publish_at_call, self.fail_at_call = context, publish_at_call, fail_at_call
        self.calls = 0

    def check(self, package_id, cutoff_at):
        self.calls += 1
        if self.calls == self.fail_at_call:
            raise RuntimeError('sensitive provider details')
        return self.context if self.calls >= self.publish_at_call else OfficialContext()


def test_core_input_and_model_are_immutable_and_reports_have_independent_run_ids(bundle, tmp_path):
    original, model = bundle.model_dump_json(), MODEL.read_bytes()
    runtime = service(bundle, reports_dir=tmp_path)
    first = runtime.analyze_repository('Synthetic_Demo')
    second = runtime.analyze_repository('pypi:synthetic-demo')
    assert first.result_status == 'predicted' and first.prediction.probability == 0.5
    assert first.mode_used == 'CORE' and first.external_insights_status == 'not_requested'
    assert first.analysis_run_id != second.analysis_run_id
    assert first.prediction == second.prediction
    assert first.generated_at == '2026-11-24T12:00:00.000Z'
    assert bundle.model_dump_json() == original and MODEL.read_bytes() == model
    assert validate_final_report((tmp_path / f'{first.analysis_run_id}.json').read_bytes()) == first


def test_unregistered_enhanced_does_not_run_optional_stages(bundle, monkeypatch):
    monkeypatch.setattr('reposeer.analysis.application.analyze_external_context',
                        lambda *a, **kw: pytest.fail('Unregistered Model B must not trigger D25'))
    report = service(bundle).analyze_repository('synthetic-demo', mode_requested='ENHANCED')
    assert report.mode_used == 'CORE' and report.enhanced_status == 'fallback'
    assert report.fallback_reason == 'model_b_unavailable'
    assert report.prediction.probability == 0.5 and report.core_prediction is None
    assert report.external_insights_status == 'not_requested'


def test_context_and_all_three_agents_preserve_probability_and_official_leads_are_not_status(bundle):
    report = service(bundle, agent_coordinator=coordinator()).analyze_repository(
        'synthetic-demo', mode_requested='ENHANCED', include_external_insights=True,
        include_agents=True, include_dependency_impact=True)
    assert report.result_status == 'predicted' and report.prediction.probability == 0.5
    assert report.mode_used == 'CORE' and report.external_insights_status == 'partial'
    evidence = [row for row in report.evidence if row.origin == 'agent']
    assert len(evidence) == 6
    assert {row.agent.agent_id for row in evidence} == {
        'maintenance_lifecycle', 'functional_trend_demand', 'alternatives_migration'}
    assert all(row.use == 'report_only' for row in evidence)
    sources = {row.source_id for row in report.sources}
    assert all(c.source_id in sources for row in report.evidence for c in row.citations)
    assert report.prediction.feature_evidence_ids == []
    assert report.dependency_impact.impact_status == 'unavailable'
    assert not any(row.use == 'official_status' for row in report.evidence)
    assert validate_final_report(report.model_dump_json()) == report


@pytest.mark.parametrize('scheduled', [False, True])
def test_known_status_precedes_collection_and_model_load(bundle, scheduled):
    class NoCollection(StoredAnalysisBackend):
        def collect(self, *args):
            pytest.fail('Known status must return before collection')
    runtime = ApplicationOrchestrator(
        NoCollection([bundle]), model_path=Path('nonexistent.json'), clock=lambda: NOW,
        official_status_provider=Ledger(official_context(bundle, scheduled=scheduled)),
    )
    report = runtime.analyze_repository('synthetic-demo', mode_requested='ENHANCED')
    assert report.result_status == ('scheduled_cessation' if scheduled else 'already_abandoned')
    assert report.mode_used is None and report.enhanced_status == 'not_run'
    assert 'prediction' not in report.model_dump() and 'probability' not in report.model_dump()
    if scheduled:
        assert report.within_horizon


def test_official_status_discovered_after_context_removes_all_predictions(bundle):
    ledger = Ledger(official_context(bundle, scheduled=True), publish_at_call=3)
    report = service(bundle, official_status_provider=ledger).analyze_repository(
        'synthetic-demo', mode_requested='ENHANCED', include_external_insights=True)
    assert ledger.calls == 3
    assert report.result_status == 'scheduled_cessation'
    assert report.mode_used is None and report.fallback_reason is None
    assert {'prediction', 'core_prediction', 'probability'}.isdisjoint(report.model_dump())
    assert any(row.origin == 'external' for row in report.evidence)


@pytest.mark.parametrize('fail_at', [1, 3])
def test_official_ledger_failure_cannot_leave_a_prediction(bundle, fail_at):
    report = service(bundle, official_status_provider=Ledger(
        OfficialContext(), fail_at_call=fail_at)).analyze_repository('synthetic-demo')
    assert report.result_status == 'failed'
    assert 'prediction' not in report.model_dump()
    assert 'sensitive provider' not in report.model_dump_json()


def test_stored_core_official_status_short_circuits_the_model(bundle):
    context = official_context(bundle)
    bundle.core.official_status = context.statuses[0]
    bundle.core.official_evidence = context.evidence
    report = ApplicationOrchestrator(StoredAnalysisBackend([bundle]), clock=lambda: NOW).analyze_repository('synthetic-demo')
    assert report.result_status == 'already_abandoned'


def test_verified_github_reverse_mapping_and_monorepo_selection(bundle):
    runtime = service(bundle)
    report = runtime.analyze_repository('https://github.com/Synthetic/Demo')
    assert report.package_id == 'pypi:synthetic-demo' and report.result_status == 'predicted'
    second = bundle.model_copy(deep=True)
    second.core.snapshot.package_id = 'pypi:other-package'
    second.external = second.agents = None
    raw = second.mapping_evidence[0]
    raw.package_id = 'pypi:other-package'
    raw.source_url = 'https://pypi.org/pypi/other-package/json'
    raw.payload['info']['name'] = 'other-package'
    replacement = observation_source(raw).model_copy(update={'source_id': 'source:mapping:pypi'})
    second.core.sources = [replacement if row.source_id == replacement.source_id else row
                           for row in second.core.sources]
    runtime = ApplicationOrchestrator(StoredAnalysisBackend([bundle, second]), model_path=MODEL, clock=lambda: NOW)
    with pytest.raises(AnalysisInputError, match='github_package_selection_required'):
        runtime.analyze_repository('https://github.com/synthetic/demo')
    selected = runtime.analyze_repository('https://github.com/synthetic/demo', package_name='other-package')
    assert selected.package_id == 'pypi:other-package'


def test_github_does_not_infer_a_package_from_a_repository_name(bundle):
    bundle.mapping_evidence = []
    with pytest.raises(AnalysisInputError, match='github_mapping_unresolved'):
        service(bundle).analyze_repository('https://github.com/synthetic/demo')


@pytest.mark.parametrize('options', [
    {'identifier': 'https://evil.test/owner/repository'},
    {'identifier': 'synthetic-demo', 'mode_requested': 'OTHER'},
    {'identifier': 'synthetic-demo', 'purpose': 'prospective_backtest'},
    {'identifier': 'synthetic-demo', 'cutoff_at': '2027-01-01T00:00:00.000Z'},
])
def test_invalid_requests_stop_before_backend_access(options):
    class NoAccess:
        def resolve(self, request):
            pytest.fail('Invalid requests must not touch a backend')
    with pytest.raises(ValueError):
        ApplicationOrchestrator(NoAccess(), clock=lambda: NOW).analyze_repository(**options)


def test_stored_cutoff_is_never_substituted_and_unknown_package_has_structured_no_score(bundle):
    report = service(bundle).analyze_repository('synthetic-demo', cutoff_at='2026-09-01T00:00:00.000Z')
    assert report.result_status == 'insufficient_evidence'
    assert report.cutoff_at == '2026-09-01T00:00:00.000Z'
    assert report.reason_code == 'reviewed_core_input_unavailable'
    unknown = service(bundle).analyze_repository('not-in-the-dataset')
    assert unknown.package_id == 'pypi:not-in-the-dataset'
    assert unknown.result_status == 'insufficient_evidence'
    assert 'prediction' not in unknown.model_dump()


def test_mapping_and_model_eligibility_failures_return_unscored_contracts(bundle, tmp_path):
    bundle.core.mapping = None
    bundle.agents = None
    report = service(bundle).analyze_repository('synthetic-demo')
    assert report.result_status == 'mapping_unresolved' and report.mapping is None
    assert 'prediction' not in report.model_dump()


def test_bad_optional_sources_and_agent_failures_preserve_core(bundle, monkeypatch):
    bundle.external.censuses[0].source.source_id = 'source:core'
    report = service(bundle).analyze_repository('synthetic-demo', include_external_insights=True)
    assert report.result_status == 'predicted' and report.prediction.probability == 0.5
    assert report.external_insights_status == 'failed' and report.evidence == []
    assert len(report.sources) == len(bundle.core.sources)
    monkeypatch.setattr('reposeer.analysis.application.analyze_external_context',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('sensitive external failure')))
    report = service(bundle).analyze_repository('synthetic-demo', include_external_insights=True,
                                               mode_requested='ENHANCED')
    assert report.prediction.probability == 0.5
    assert 'sensitive external' not in report.model_dump_json()


def test_missing_agent_source_lineage_drops_findings_with_an_explicit_limitation(bundle):
    bundle.agent_sources = []
    report = service(bundle, agent_coordinator=coordinator()).analyze_repository('synthetic-demo', include_agents=True)
    assert report.prediction.probability == 0.5
    assert all(row.origin != 'agent' for row in report.evidence)
    assert any('6 agent findings omitted' in row for row in report.limitations)


def test_optional_provider_configuration_failure_does_not_block_core(bundle):
    def unavailable():
        raise RuntimeError('sensitive agent credential failure')
    report = service(bundle, agent_factory=unavailable).analyze_repository('synthetic-demo', include_agents=True)
    assert report.prediction.probability == 0.5 and report.external_insights_status == 'partial'
    assert report.external_insights_reason == 'agent_analysis_unavailable_or_invalid'
    assert 'sensitive agent' not in report.model_dump_json()


@pytest.mark.parametrize('invalid', [False, True])
def test_optional_impact_failure_or_invalid_graph_never_changes_prediction(bundle, invalid):
    def impact(package, cutoff):
        if not invalid:
            raise RuntimeError('sensitive impact failure')
        return ImpactContext.model_validate(dict(impact=dict(
            impact_status='available', graph_kind='observed_potential',
            graph_as_of='2026-11-01T00:00:00.000Z', corpus_id='synthetic',
            corpus_package_count=10, target_version=None, environment=None, resolver_version=None,
            direct_dependents=1, transitive_dependents=2, counts_are_lower_bounds=False,
            source_ids=['source:core'], missing_reason=None), sources=bundle.core.sources))
    report = service(bundle, impact_provider=impact).analyze_repository('synthetic-demo', include_dependency_impact=True)
    assert report.prediction.probability == 0.5
    assert report.dependency_impact.impact_status == 'unavailable'
    assert report.dependency_impact.missing_reason == 'dependency_impact_failed_or_invalid'
    assert 'sensitive impact' not in report.model_dump_json()


def test_valid_impact_fetched_during_the_run_uses_final_generation_time(bundle):
    ticks = count()
    def clock():
        return NOW + timedelta(seconds=next(ticks))
    def impact(package, cutoff):
        observed = clock().isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        source = bundle.core.sources[0].model_copy(update={
            'source_id': 'source:impact', 'retrieved_at': observed,
            'temporal_basis': 'historical_reconstruction',
        })
        return ImpactContext.model_validate(dict(impact=dict(
            impact_status='available', graph_kind='observed_potential', graph_as_of=cutoff,
            corpus_id='synthetic-corpus', corpus_package_count=10, target_version=None,
            environment=None, resolver_version=None, direct_dependents=1, transitive_dependents=2,
            counts_are_lower_bounds=False, source_ids=[source.source_id], missing_reason=None),
            sources=[source.model_dump()]))
    report = ApplicationOrchestrator(StoredAnalysisBackend([bundle]), model_path=MODEL,
                                     clock=clock, impact_provider=impact).analyze_repository(
        'synthetic-demo', include_dependency_impact=True)
    assert report.dependency_impact.impact_status == 'available'
    assert report.dependency_impact.direct_dependents == 1
    assert report.prediction.probability == 0.5
    assert next(row for row in report.sources if row.source_id == 'source:impact').retrieved_at <= report.generated_at


class SyntheticVerifier:
    def historical_document(self, *args):
        return True

    def census(self, *args):
        return True

    def agent_event(self, *args):
        return True

    def agent_processing(self, *args):
        return True


def verified_external(bundle, *, e2=False):
    document = bundle.external.documents[0]
    document.article.content_available_at = document.article.provenance.available_at = document.article.published_at
    bundle.external.censuses[0].document_source_ids = [document.source_id]
    if e2:
        bundle.external.agent_coverage = [AnalysisCoverage(
            coverage_id='synthetic:agent-complete', stage='agent', source_ids=[document.source_id],
            status='complete', source_coverage='complete', historical_validity='verified',
            window=bundle.external.censuses[0].window, records_observed=1, eligible_documents=1,
            processed_documents=1, missing_reason=None,
        )]
    return bundle


class SyntheticModelB:
    release_eligible = True

    def __init__(self, core_report, *, feature_set='E1'):
        self.manifest = core_report.prediction.model.model_copy(update={
            'model_id': 'synthetic-b', 'model_version': 'synthetic-b-1', 'feature_set': feature_set})
        self.calls = 0
        self.raise_error = False

    def compatible(self, core):
        return True

    def predict(self, core, external):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError('sensitive Model B details')
        return ModelResult(
            result_status='predicted', probability=0.25, risk_level='low', model=self.manifest,
            feature_source_ids=['source:core', external.evidence[0].citations[0].source_id],
            feature_evidence_ids=[external.evidence[0].evidence_id], explanation=None,
        )


@pytest.mark.parametrize('feature_set', ['E1', 'E2'])
def test_registered_enhanced_only_runs_after_independent_external_gates(bundle, feature_set):
    bundle = verified_external(bundle, e2=feature_set == 'E2')
    model = SyntheticModelB(service(bundle).analyze_repository('synthetic-demo'), feature_set=feature_set)
    report = service(bundle, enhanced_model=model, evidence_verifier=SyntheticVerifier()).analyze_repository(
        'synthetic-demo', mode_requested='ENHANCED')
    assert model.calls == 1
    assert report.mode_used == 'ENHANCED' and report.enhanced_status == 'used'
    assert report.prediction.probability == 0.25
    assert report.core_prediction.probability == 0.5
    assert report.prediction.model.feature_set == feature_set
    assert validate_final_report(report.model_dump_json()) == report


@pytest.mark.parametrize('failure,reason', [
    ('unreleased', 'model_b_release_gates_not_met'),
    ('incompatible', 'model_b_incompatible'),
    ('external_unknown', 'external_coverage_insufficient'),
    ('e2_processing_unknown', 'agent_processing_insufficient'),
    ('prediction_error', 'model_b_inference_or_contract_failed'),
])
def test_enhanced_failures_have_explicit_core_fallbacks(bundle, failure, reason):
    if failure != 'external_unknown':
        bundle = verified_external(bundle)
    model = SyntheticModelB(service(bundle).analyze_repository('synthetic-demo'),
                            feature_set='E2' if failure == 'e2_processing_unknown' else 'E1')
    if failure == 'unreleased':
        model.release_eligible = False
    if failure == 'incompatible':
        model.compatible = lambda core: False
    if failure == 'prediction_error':
        model.raise_error = True
    report = service(bundle, enhanced_model=model, evidence_verifier=SyntheticVerifier()).analyze_repository(
        'synthetic-demo', mode_requested='ENHANCED')
    assert report.mode_used == 'CORE' and report.fallback_reason == reason
    assert report.prediction.probability == 0.5 and report.core_prediction is None
    assert 'sensitive' not in report.model_dump_json()
    assert model.calls == (1 if failure == 'prediction_error' else 0)


def test_cli_runs_all_prepared_stages_and_refuses_overwriting_output(tmp_path):
    output = tmp_path / 'report.json'
    args = ['analyze-repository', 'synthetic-demo', '--input-bundle', str(EXAMPLES / 'synthetic_bundle.json'),
            '--model', str(MODEL), '--mode', 'ENHANCED', '--insights', '--agents',
            '--agent-replay', str(EXAMPLES / 'synthetic_agent_replay.json'), '--impact', '--output', str(output)]
    runner = CliRunner()
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert validate_final_report(result.stdout) == validate_final_report(output.read_bytes())
    assert validate_final_report(result.stdout).prediction.probability == 0.5
    original = output.read_bytes()
    assert runner.invoke(app, args).exit_code == 2
    assert output.read_bytes() == original


def test_convenience_function_reads_only_explicit_local_bundles_and_returns_no_score(tmp_path):
    report = analyze_repository('missing-package', data_dir=tmp_path)
    assert report.package_id == 'pypi:missing-package' and report.result_status == 'insufficient_evidence'
    assert validate_final_report((tmp_path / 'reports/analysis' / f'{report.analysis_run_id}.json').read_bytes()) == report


def test_collector_returning_the_wrong_package_fails_closed(bundle):
    class WrongPackage(StoredAnalysisBackend):
        def collect(self, target, request, run_id):
            other = bundle.model_copy(deep=True)
            other.core.snapshot.package_id = 'pypi:unrelated'
            other.external = other.agents = None
            return other
    report = ApplicationOrchestrator(WrongPackage([bundle]), model_path=MODEL,
                                     clock=lambda: NOW).analyze_repository('synthetic-demo')
    assert report.package_id == 'pypi:synthetic-demo'
    assert report.result_status == 'failed' and 'prediction' not in report.model_dump()


def test_agent_identity_and_source_hash_are_revalidated_at_report_boundary(bundle):
    class WrongIdentity:
        def analyze(self, request):
            result = coordinator().analyze(request)
            result.package_id = 'pypi:unrelated'
            return result
    report = service(bundle, agent_coordinator=WrongIdentity()).analyze_repository('synthetic-demo', include_agents=True)
    assert report.prediction.probability == 0.5
    assert not any(row.origin == 'agent' for row in report.evidence)
    assert report.external_insights_reason == 'agent_analysis_unavailable_or_invalid'
    bundle.agent_sources[0].content_sha256 = '0' * 64
    report = service(bundle, agent_coordinator=coordinator()).analyze_repository('synthetic-demo', include_agents=True)
    strict = [row for row in report.evidence if row.origin == 'agent' and row.evidence_id.startswith('agent:signal:')]
    assert len(strict) == 2
    assert any('2 agent findings omitted' in row for row in report.limitations)


def test_automatic_research_handoff_bounds_large_documents_and_keeps_real_source_offsets(bundle):
    from reposeer.analysis.agent_reporting import agent_request
    from reposeer.contracts.identity import text_sha256
    bundle.agents = None
    document = bundle.external.documents[0]
    document.article.content_text += ' Large passage.' * 2000
    document.article.content_sha256 = text_sha256(document.article.content_text)
    document.relevance.content_sha256 = document.article.content_sha256
    request = agent_request(bundle, '00000000-0000-4000-8000-000000000027')
    assert request.intake_summary.truncated_excerpts == 1
    assert len(request.research_requests) == 3
    assert all(len(row.candidates[0].text_excerpt) == 10000 for row in request.research_requests)
    assert all(row.candidates[0].source_ref == document.source_id for row in request.research_requests)


def test_unreleased_core_does_not_attempt_optional_model_b(bundle, tmp_path):
    data = json.loads(MODEL.read_bytes())
    data['release_eligible'] = False
    path = tmp_path / 'unreleased.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    class NoModelB:
        release_eligible = False
        manifest = None
        def compatible(self, core):
            pytest.fail('Model B cannot run when CORE did not score')
    runtime = ApplicationOrchestrator(StoredAnalysisBackend([bundle]), model_path=path,
                                     enhanced_model=NoModelB(), clock=lambda: NOW)
    report = runtime.analyze_repository('synthetic-demo', mode_requested='ENHANCED', include_external_insights=True)
    assert report.result_status == 'insufficient_evidence'
    assert report.reason_code == 'model_release_gates_not_met'
    assert report.enhanced_status == 'not_run' and report.mode_used is None
    assert report.external_insights_status == 'partial'
    assert 'prediction' not in report.model_dump()


def test_report_persistence_failure_keeps_the_validated_result(bundle, tmp_path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('existing user file', encoding='utf-8')
    report = service(bundle, reports_dir=blocked).analyze_repository('synthetic-demo')
    assert report.prediction.probability == 0.5
    assert any('report_persistence_failed' in row for row in report.limitations)
    assert blocked.read_text() == 'existing user file'
