"""Exercise the T-020 → T-022 adapter using real pipelines and fake HTTP only."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from tests.integration.test_external_crawl_pipeline import HTML
from tests.unit.processing.test_normalization import activity_raw, repository_raw

from reposeer.analysis.application import ApplicationOrchestrator
from reposeer.analysis.application_inputs import AnalysisInputError, AnalyzeRequest
from reposeer.analysis.collection_backend import CollectionAnalysisBackend
from reposeer.clients.github import GitHubClient
from reposeer.clients.pypi import PyPIClient
from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.pipelines.collection import CollectionPipeline
from reposeer.pipelines.external import ExternalPipeline
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.services.activity_batch_service import ActivityBatchService

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def acquisition(tmp_path):
    calls, paths = [], []
    options = {'unmapped': False, 'metadata_failure': False, 'excluded': False}

    def handler(request):
        calls.append(request)
        if request.url.host == 'pypi.org':
            return httpx.Response(200, json={'info': {'name': 'synthetic-demo', 'project_urls': {
                'Home' if options['unmapped'] else 'Source': 'https://github.com/synthetic/demo'}}})
        if request.url.host == 'api.github.com':
            parts = request.url.path.split('/')
            if len(parts) == 4:
                if options['metadata_failure']:
                    return httpx.Response(403, text='sensitive provider response')
                payload = repository_raw().payload
                payload.update(id=900000001, full_name='synthetic/demo', private=False,
                               topics=['flask'], html_url='https://github.com/synthetic/demo')
                if options['excluded']:
                    payload['fork'] = True
                return httpx.Response(200, json=payload)
            return httpx.Response(200, json=[activity_raw({'pulls': 'pull_requests'}.get(parts[4], parts[4])).payload])
        if request.url.host == 'hn.algolia.com':
            return httpx.Response(200, json={'hits': [{'objectID': '42', 'title': 'Synthetic package guide',
                'url': 'https://blog.example/synthetic-demo', 'created_at': '2026-09-01T00:00:00Z'}], 'nbPages': 1})
        if request.url.host == 'blog.example':
            html = HTML.replace('synthetic package', 'synthetic-demo package')
            html = html.replace('</article>', '<p>Use <a href="https://github.com/synthetic/demo">synthetic-demo</a> for Python workflows.</p></article>')
            return httpx.Response(200, text=html, headers={'content-type': 'text/html; charset=utf-8'})
        pytest.fail(f'Unexpected mock HTTP request: {request.url.host}')

    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        github = GitHubClient(http_client=http)

        def factory(storage_paths, *, clock):
            paths.append(storage_paths)
            return CollectionPipeline(
                storage_paths, clock=clock, pypi_factory=lambda: PyPIClient(http_client=http),
                activity_factory=lambda **kw: ActivityBatchService(client=github, **kw),
                discovery_factory=lambda **kw: ExternalPipeline(http_client=http, **kw),
                crawl_factory=lambda **kw: ExternalCrawlPipeline(http_client=http,
                    policy=URLPolicy(lambda host: ['93.184.216.34']), **kw),
            )

        backend = CollectionAnalysisBackend(tmp_path, collection_factory=factory, clock=lambda: NOW)
        yield backend, calls, paths, options


def test_current_collection_isolated_per_run_has_null_features_without_historical_proof(acquisition):
    backend, calls, paths, _ = acquisition
    runtime = ApplicationOrchestrator(backend, clock=lambda: NOW)
    first = runtime.analyze_repository('synthetic-demo', purpose='current_inference')
    second = runtime.analyze_repository('synthetic-demo', purpose='current_inference')
    assert first.result_status == second.result_status == 'insufficient_evidence'
    assert first.reason_code == 'no_usable_core_features'
    assert 'prediction' not in first.model_dump()
    assert first.mapping.scope_quality == 'repository_only'
    assert first.mapping.available_at == first.cutoff_at == '2026-11-24T12:00:00.000Z'
    assert all(row.records_observed is None for row in first.coverage)
    assert len(paths) == 2 and paths[0].base_dir != paths[1].base_dir
    assert paths[0].base_dir.name == first.analysis_run_id
    assert paths[1].base_dir.name == second.analysis_run_id
    assert all(request.url.host in {'pypi.org', 'api.github.com'} for request in calls)
    assert (paths[0].metadata_dir / 'collection_runs').is_dir()
    assert (paths[0].metadata_dir / 'analysis_bundle.json').is_file()
    assert (paths[0].features_dir / 'repository_package_snapshots/repository_package_snapshots.parquet').is_file()


def test_live_mapping_is_not_backdated_for_a_historical_cutoff(acquisition):
    backend, _, _, _ = acquisition
    report = ApplicationOrchestrator(backend, clock=lambda: NOW).analyze_repository(
        'synthetic-demo', cutoff_at='2026-10-01T00:00:00.000Z')
    assert report.result_status == 'mapping_history_unverified'
    assert report.reason_code == 'mapping_unavailable_at_cutoff'
    assert report.mapping.available_at > report.cutoff_at
    assert 'prediction' not in report.model_dump()


def test_live_github_requires_package_selection_and_rechecks_the_actual_mapping(acquisition):
    backend, calls, _, _ = acquisition
    with pytest.raises(AnalysisInputError, match='github_package_selection_required'):
        backend.resolve(AnalyzeRequest(identifier='https://github.com/synthetic/demo'))
    assert not calls
    report = ApplicationOrchestrator(backend, clock=lambda: NOW).analyze_repository(
        'https://github.com/other/repository', package_name='synthetic-demo')
    assert report.result_status == 'mapping_unresolved'
    assert report.reason_code == 'requested_repository_does_not_match_package'
    assert 'prediction' not in report.model_dump()


def test_missing_mapping_never_infers_a_repository_from_the_package_name(acquisition):
    backend, calls, _, options = acquisition
    options['unmapped'] = True
    report = ApplicationOrchestrator(backend, clock=lambda: NOW).analyze_repository('synthetic-demo')
    assert report.result_status == 'mapping_unresolved'
    assert report.reason_code == 'package_repository_mapping_unresolved'
    assert report.mapping is None
    assert not any(row.url.host == 'api.github.com' for row in calls)


@pytest.mark.parametrize('condition,expected', [('metadata_failure', 'failed'), ('excluded', 'unsupported')])
def test_acquisition_errors_distinguish_source_failure_from_unsupported_scope(acquisition, condition, expected):
    backend, _, _, options = acquisition
    options[condition] = True
    report = ApplicationOrchestrator(backend, clock=lambda: NOW).analyze_repository('synthetic-demo')
    assert report.result_status == expected
    assert 'prediction' not in report.model_dump()
    assert 'sensitive provider response' not in report.model_dump_json()


def test_live_external_context_does_not_certify_a_complete_source_census(acquisition):
    backend, calls, paths, _ = acquisition
    report = ApplicationOrchestrator(backend, clock=lambda: NOW).analyze_repository(
        'synthetic-demo', include_external_insights=True)
    assert report.result_status == 'insufficient_evidence'
    assert report.external_insights_status == 'partial'
    assert report.evidence
    sources = {row.source_id: row for row in report.sources}
    for item in report.evidence:
        if item.use == 'feature':
            for citation in item.citations:
                sources[citation.source_id].require_as_of(report.cutoff_at)
    external_coverage = [row for row in report.coverage if row.stage == 'external']
    assert external_coverage and all(row.source_coverage == 'unknown' for row in external_coverage)
    assert all(row.historical_validity == 'unverified' for row in external_coverage)
    assert any(row.url.host == 'hn.algolia.com' for row in calls)
    assert any(row.url.host == 'blog.example' for row in calls)
    manifest = next((paths[0].metadata_dir / 'collection_runs').glob('integration_*.json'))
    assert json.loads(manifest.read_bytes())['child_run_ids']['external_crawl']
