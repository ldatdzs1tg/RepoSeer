"""Local JSON API. Frontend assets and filesystem paths are never served here."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from reposeer.analysis.application import ApplicationOrchestrator
from reposeer.analysis.application_inputs import AnalysisInputError, AnalyzeRequest
from reposeer.contracts.analysis import validate_final_report

SCENARIOS = {
    'core': ('CORE prediction', 'final_report_core.json'),
    'enhanced': ('ENHANCED prediction', 'final_report_enhanced.json'),
    'fallback': ('CORE fallback', 'final_report.json'),
    'abandoned': ('Known cessation', 'final_report_already_abandoned.json'),
    'scheduled': ('Scheduled cessation', 'final_report_scheduled_cessation.json'),
    'no-score': ('No score', 'final_report_no_score.json'),
}


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source: Literal['mock', 'orchestrator']
    scenario: str | None = None
    request: AnalyzeRequest | None = None


class AnalysisJobs:
    """One worker, bounded retention, validated reports, sanitized public failures."""

    def __init__(self, orchestrator: ApplicationOrchestrator, mock_dir: Path | None = None):
        self.orchestrator = orchestrator
        self.mocks = {}
        if mock_dir is not None:
            for key, (_, filename) in SCENARIOS.items():
                self.mocks[key] = validate_final_report((mock_dir / filename).read_bytes()).model_dump(mode='json')
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reposeer-analysis')

    def submit(self, submission: Submission) -> dict:
        if submission.source == 'mock':
            if submission.request is not None or submission.scenario not in self.mocks:
                raise ValueError('invalid_mock_scenario')
        elif submission.request is None or submission.scenario is not None:
            raise ValueError('analysis_request_required')
        with self.lock:
            if sum(j['status'] in ('queued', 'running') for j in self.jobs.values()) >= 4:
                raise OverflowError('analysis_queue_full')
            while len(self.jobs) >= 32:
                oldest = next((key for key, job in self.jobs.items() if job['status'] in ('complete', 'failed')), None)
                if oldest is None:
                    raise OverflowError('analysis_queue_full')
                del self.jobs[oldest]
            job_id = str(uuid4())
            self.jobs[job_id] = {'id': job_id, 'status': 'queued', 'source': submission.source}
            response = dict(self.jobs[job_id])
        self.executor.submit(self._run, job_id, submission)
        return response

    def _run(self, job_id: str, submission: Submission) -> None:
        with self.lock:
            self.jobs[job_id]['status'] = 'running'
        try:
            if submission.source == 'mock':
                report = self.mocks[submission.scenario]
            else:
                options = submission.request.model_dump(exclude={'identifier'})
                report = self.orchestrator.analyze_repository(submission.request.identifier, **options)
            payload = validate_final_report(report).model_dump(mode='json')
            with self.lock:
                self.jobs[job_id].update(status='complete', report=payload)
        except AnalysisInputError as error:
            safe_code = str(error)
            if safe_code not in {'github_mapping_unresolved', 'github_package_selection_required',
                                 'cutoff_cannot_be_in_the_future'}:
                safe_code = 'analysis_failed'
            with self.lock:
                self.jobs[job_id].update(status='failed', error=safe_code)
        except Exception:
            # Errors can contain provider credentials, paths or response bodies.
            with self.lock:
                self.jobs[job_id].update(status='failed', error='analysis_failed')

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)


def make_server(jobs: AnalysisJobs, port: int = 8765,
                allowed_origin: str = 'http://localhost:5173') -> ThreadingHTTPServer:
    """Bind only loopback; allow one explicit frontend origin, never wildcard CORS."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status: int, payload: dict) -> None:
            content = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            if self.headers.get('Origin') == allowed_origin:
                self.send_header('Access-Control-Allow-Origin', allowed_origin)
                self.send_header('Vary', 'Origin')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(content)

        def do_OPTIONS(self):
            if self.headers.get('Origin') != allowed_origin:
                self.respond(403, {'error': 'origin_not_allowed'})
                return
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', allowed_origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            if self.path == '/api/health':
                self.respond(200, {'status': 'ok', 'api_version': '1'})
            elif self.path == '/api/scenarios':
                self.respond(200, {'scenarios': [{'id': key, 'label': SCENARIOS[key][0]} for key in jobs.mocks]})
            elif self.path.startswith('/api/analyses/'):
                job = jobs.get(self.path.removeprefix('/api/analyses/'))
                self.respond(200 if job else 404, job or {'error': 'job_not_found'})
            else:
                self.respond(404, {'error': 'not_found'})

        def do_POST(self):
            if self.headers.get('Origin') not in (None, allowed_origin):
                self.respond(403, {'error': 'origin_not_allowed'})
                return
            if self.path != '/api/analyses':
                self.respond(404, {'error': 'not_found'})
                return
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                self.respond(415, {'error': 'application_json_required'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    self.respond(413, {'error': 'body_limit_16KiB'})
                    return
                submission = Submission.model_validate_json(self.rfile.read(length))
                result = jobs.submit(submission)
            except (ValueError, ValidationError):
                self.respond(422, {'error': 'invalid_submission'})
            except OverflowError:
                self.respond(429, {'error': 'analysis_queue_full'})
            else:
                self.respond(202, result)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server
