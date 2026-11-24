"""T-028 HTTP boundary: real orchestrator, mock contract, isolation and failures."""

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from reposeer.analysis.application import ApplicationOrchestrator
from reposeer.analysis.application_inputs import StoredAnalysisBackend
from reposeer.cli.app import app
from reposeer.contracts.analysis import validate_final_report
from reposeer.demo.api import AnalysisJobs, Submission, make_server

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = 'http://localhost:5173'


@contextmanager
def running_api(orchestrator=None):
    runtime = orchestrator or ApplicationOrchestrator(
        StoredAnalysisBackend.from_files([ROOT / 'examples/application/synthetic_bundle.json']),
        model_path=ROOT / 'examples/application/synthetic_model_a.json',
    )
    jobs = AnalysisJobs(runtime, ROOT / 'examples/analysis')
    server = make_server(jobs, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', timeout=5) as client:
            yield client, jobs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        jobs.close()


def wait_job(client, job_id):
    for _ in range(100):
        response = client.get(f'/api/analyses/{job_id}')
        assert response.status_code == 200
        job = response.json()
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(0.01)
    pytest.fail('job did not finish')


def test_real_orchestrator_runs_through_http_and_preserves_core_fallback():
    with running_api() as (client, _):
        response = client.post('/api/analyses', json={
            'source': 'orchestrator', 'request': {
                'identifier': 'synthetic-demo', 'mode_requested': 'ENHANCED',
                'include_dependency_impact': True,
            }}, headers={'Origin': ORIGIN})
        assert response.status_code == 202
        assert response.headers['access-control-allow-origin'] == ORIGIN
        job = wait_job(client, response.json()['id'])
        assert job['source'] == 'orchestrator' and job['status'] == 'complete'
        report = validate_final_report(job['report'])
        assert report.result_status == 'predicted' and report.prediction.probability == 0.5
        assert report.mode_used == 'CORE' and report.fallback_reason == 'model_b_unavailable'
        assert report.dependency_impact.impact_status == 'unavailable'


def test_every_mock_is_validated_and_known_status_never_contains_a_score():
    with running_api() as (client, _):
        scenarios = client.get('/api/scenarios').json()['scenarios']
        assert {s['id'] for s in scenarios} == {'core', 'enhanced', 'fallback', 'abandoned', 'scheduled', 'no-score'}
        for scenario in scenarios:
            response = client.post('/api/analyses', json={'source': 'mock', 'scenario': scenario['id']})
            job = wait_job(client, response.json()['id'])
            assert job['source'] == 'mock' and job['status'] == 'complete'
            report = validate_final_report(job['report'])
            if scenario['id'] in ('abandoned', 'scheduled', 'no-score'):
                assert report.mode_used is None
                assert 'prediction' not in job['report'] and 'probability' not in job['report']


@pytest.mark.parametrize('payload', [
    {'source': 'mock', 'scenario': '../../.env'},
    {'source': 'mock', 'scenario': 'core', 'request': {'identifier': 'foo'}},
    {'source': 'orchestrator'},
    {'source': 'orchestrator', 'scenario': 'core', 'request': {'identifier': 'foo'}},
    {'source': 'orchestrator', 'request': {'identifier': 'foo', 'model': '/tmp/arbitrary.json'}},
    {'source': 'orchestrator', 'request': {'identifier': 'foo', 'include_agents': 'yes'}},
    {'source': 'orchestrator', 'request': {'identifier': 'https://evil.invalid/foo'}},
])
def test_invalid_requests_do_not_enqueue_work(payload):
    with running_api() as (client, jobs):
        response = client.post('/api/analyses', json=payload)
        assert response.status_code == 422
        assert not jobs.jobs


def test_http_methods_origins_body_limit_and_routes_are_constrained():
    with running_api() as (client, jobs):
        assert client.get('/api/health').json()['status'] == 'ok'
        assert client.get('/api/analyses/not-a-job').status_code == 404
        assert client.get('/../.env').status_code == 404
        assert client.post('/api/analyses', content='{}').status_code == 415
        assert client.post('/api/analyses', content='x' * 16385,
                           headers={'Content-Type': 'application/json'}).status_code == 413
        assert client.post('/api/analyses', content='{',
                           headers={'Content-Type': 'application/json'}).status_code == 422
        assert client.options('/api/analyses', headers={'Origin': ORIGIN}).status_code == 204
        assert client.options('/api/analyses', headers={'Origin': 'https://other.invalid'}).status_code == 403
        assert client.post('/api/analyses', json={'source': 'mock', 'scenario': 'core'},
                           headers={'Origin': 'https://other.invalid'}).status_code == 403
        assert not jobs.jobs


def test_worker_failure_is_sanitized_and_queue_is_bounded():
    entered = threading.Event()
    release = threading.Event()

    class BlockedRuntime:
        def analyze_repository(self, *args, **kwargs):
            entered.set()
            release.wait(5)
            raise RuntimeError('secret-provider-key /private/path')

    with running_api(BlockedRuntime()) as (client, _):
        try:
            request = {'source': 'orchestrator', 'request': {'identifier': 'synthetic-demo'}}
            first = client.post('/api/analyses', json=request).json()
            assert entered.wait(2)
            assert client.get(f"/api/analyses/{first['id']}").json()['status'] == 'running'
            queued = [client.post('/api/analyses', json=request) for _ in range(3)]
            assert all(response.status_code == 202 for response in queued)
            assert all(response.json()['status'] == 'queued' for response in queued)
            assert client.post('/api/analyses', json=request).status_code == 429
        finally:
            release.set()
        job = wait_job(client, first['id'])
        assert job['status'] == 'failed' and job['error'] == 'analysis_failed'
        assert 'secret' not in json.dumps(job) and 'report' not in job


def test_jobs_expire_completed_results_instead_of_growing_forever():
    jobs = AnalysisJobs(ApplicationOrchestrator(StoredAnalysisBackend([])), ROOT / 'examples/analysis')
    try:
        for _ in range(40):
            job = jobs.submit(Submission(source='mock', scenario='core'))
            for _ in range(100):
                if jobs.get(job['id'])['status'] == 'complete':
                    break
                time.sleep(0.005)
            else:
                pytest.fail('mock job did not finish')
        assert len(jobs.jobs) == 32
    finally:
        jobs.close()


@pytest.mark.parametrize(('analysis_request', 'error'), [
    ({'identifier': 'https://github.com/unmapped/repository'}, 'github_mapping_unresolved'),
    ({'identifier': 'synthetic-demo', 'cutoff_at': '2099-01-01T00:00:00.000Z'},
     'cutoff_cannot_be_in_the_future'),
])
def test_actionable_input_errors_are_returned_without_a_report(analysis_request, error):
    with running_api() as (client, _):
        response = client.post('/api/analyses', json={'source': 'orchestrator', 'request': analysis_request})
        job = wait_job(client, response.json()['id'])
        assert job['status'] == 'failed' and job['error'] == error
        assert 'report' not in job


def test_invalid_runtime_report_cannot_cross_http_boundary():
    class InvalidRuntime:
        def analyze_repository(self, *args, **kwargs):
            return {'result_status': 'mapping_unresolved', 'probability': 0}

    with running_api(InvalidRuntime()) as (client, _):
        response = client.post('/api/analyses', json={
            'source': 'orchestrator', 'request': {'identifier': 'synthetic-demo'}})
        job = wait_job(client, response.json()['id'])
        assert job['status'] == 'failed' and 'report' not in job


def test_cli_rejects_conflicting_or_unusable_configuration():
    runner = CliRunner()
    bundle = str(ROOT / 'examples/application/synthetic_bundle.json')
    replay = str(ROOT / 'examples/application/synthetic_agent_replay.json')
    for args in [
        ['--live', '--input-bundle', bundle],
        ['--agent-live', '--agent-replay', replay],
        ['--origin', 'http://localhost:5173/path'],
        ['--origin', '*'],
    ]:
        assert runner.invoke(app, ['demo-api', *args]).exit_code == 2
