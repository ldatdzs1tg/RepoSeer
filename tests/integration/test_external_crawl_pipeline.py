"""Offline T-016 -> T-017 handoff, native bodies, failures and raw replay."""

import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.extractor import ArticleExtractor
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.contracts import RawRecord, validate_record
from reposeer.pipelines.external import ExternalPipeline
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.schemas.external.candidate import ExternalCandidate
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage

NOW = datetime(2026, 10, 16, 6, tzinfo=UTC)
PUBLIC = URLPolicy(lambda host: ["93.184.216.34"])
HTML = """<!doctype html><html lang="en"><head>
<title>A synthetic package maintenance article</title>
<meta name="author" content="Example Author">
<meta property="article:published_time" content="2024-03-01T09:30:00+07:00">
</head><body><nav>Navigation</nav><main><article>
<h1>A synthetic package maintenance article</h1>
<p>The synthetic package provides a useful Python interface for parsing data.
This article describes its design, release process and development workflow.
Maintainers review contributions and publish detailed documentation for users.</p>
<p>The project supports importing structured data from several source formats.
The current release includes examples and installation instructions for developers.
This is synthetic test content and does not describe a real project.</p>
</article></main></body></html>"""


def pipeline(tmp_path, http, **kwargs):
    return ExternalCrawlPipeline(
        http_client=http,
        storage_paths=StoragePaths(tmp_path),
        policy=PUBLIC,
        clock=lambda: NOW,
        **kwargs,
    )


def test_discovery_to_crawl_raw_replay_and_append(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "42",
                            "title": "Synthetic package article",
                            "url": "https://blog.example/article",
                            "created_at": "2024-03-01T02:30:00Z",
                        }
                    ],
                    "nbPages": 1,
                },
            )
        if request.url.path == "/article":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200,
            content=gzip.compress(HTML.encode()),
            headers={
                "content-type": "text/html; charset=utf-8",
                "content-encoding": "gzip",
                "etag": '"revision-2"',
            },
        )

    paths = StoragePaths(tmp_path)
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        with ExternalPipeline(
            http_client=http, storage_paths=paths, clock=lambda: NOW
        ) as discovery:
            discovered = discovery.discover("pypi:flask", sources=["hackernews"], max_pages=1)
        candidates = JsonlStorage().read_contract_records(discovered.output_path)
        assert len(candidates) == 1
        assert all("blog.example" not in url for url in calls)
        original = candidates[0].model_dump(mode="json")
        with pipeline(tmp_path, http) as crawler:
            first = crawler.crawl(candidates)
            second = crawler.crawl(candidates)
        assert not http._sync_client.is_closed
    replay = JsonlStorage().read_contract_records(first.output_path)
    assert len(replay) == 2
    assert replay[0].raw_record_id != replay[1].raw_record_id
    assert first.run_id != second.run_id
    raw = replay[0]
    assert isinstance(validate_record(raw.model_dump_json()), RawRecord)
    assert raw.source == "web_article" and raw.source_record_type == "article"
    assert raw.retrieved_at == raw.payload["fetched_at"] == "2026-10-16T06:00:00.000Z"
    assert raw.event_at == raw.payload["published_at"] == "2024-03-01T02:30:00.000Z"
    assert raw.available_at == raw.payload["content_available_at"] == raw.retrieved_at
    assert raw.payload["historical_version_status"] == "observation_only"
    assert raw.payload["url"] == "https://blog.example/final"
    assert raw.payload["source"] == "hackernews"
    assert raw.payload["discovery"] == original
    assert raw.payload["candidate_raw_record_id"] == candidates[0].raw_record_id
    assert raw.payload["html"] == HTML
    assert base64.b64decode(raw.payload["response_body_base64"]) == HTML.encode()
    assert raw.raw_body_sha256 == hashlib.sha256(HTML.encode()).hexdigest()
    assert "synthetic package" in raw.payload["content"].lower()
    assert (
        raw.payload["content_sha256"] == hashlib.sha256(raw.payload["content"].encode()).hexdigest()
    )
    assert candidates[0].model_dump(mode="json") == original
    assert first.collection_result.status == "complete"
    manifest = json.loads(first.manifest_path.read_text())
    assert manifest["documents_extracted"] == 1
    assert manifest["coverage_scope"] == "requested_candidate_batch_only"


@pytest.mark.parametrize(
    "source,url,item_id,native_payload,endpoint",
    [
        (
            "hackernews",
            "https://news.ycombinator.com/item?id=42",
            "hn:42",
            {
                "id": 42,
                "title": "Ask HN: Python maintenance",
                "time": 1709251200,
                "text": "<p>How do you maintain a Python project over several years?</p>",
                "by": "example",
                "kids": [43],
                "descendants": 1,
            },
            "hacker-news.firebaseio.com",
        ),
        (
            "devto",
            "https://dev.to/example/python-article",
            "devto:42",
            {
                "id": 42,
                "title": "Python article",
                "url": "https://dev.to/example/python-article",
                "body_html": "<p>We use Python for data processing in this example.</p>",
                "published_at": "2024-03-01T00:00:00Z",
                "edited_at": "2025-03-01T00:00:00Z",
                "user": {"name": "Example"},
                "tags": ["python"],
            },
            "dev.to",
        ),
        (
            "stackexchange",
            "https://stackoverflow.com/questions/42/example",
            "se:42",
            {
                "items": [
                    {
                        "question_id": 42,
                        "title": "Python question",
                        "creation_date": 1709251200,
                        "body": "<p>How can this Python data processing problem be solved?</p>",
                        "last_edit_date": 1740787200,
                        "tags": ["python"],
                        "owner": {"display_name": "Example"},
                    }
                ],
                "has_more": False,
            },
            "api.stackexchange.com",
        ),
    ],
)
def test_native_api_body_and_revision_metadata(
    tmp_path, source, url, item_id, native_payload, endpoint
):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == endpoint
        if source == "stackexchange":
            assert request.url.params["filter"] == "withbody"
        return httpx.Response(200, json=native_payload)

    candidate = ExternalCandidate(
        title="Discovery title", url=url, source=source, source_item_id=item_id
    )
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        result = pipeline(tmp_path, http).crawl([candidate])
    assert len(calls) == 1
    assert result.collection_result.status == "complete"
    raw = result.collection_result.records[0]
    assert raw.payload["native_payload"] == native_payload
    assert raw.payload["content"] and "<p>" not in raw.payload["content"]
    assert raw.payload["published_precision"] == "timestamp"
    assert raw.source_record_id == item_id
    assert raw.available_at == raw.retrieved_at


def test_mixed_failures_retain_raw_and_continue(tmp_path, monkeypatch):
    real_extract = ArticleExtractor.extract

    def extract(self, html, *args, **kwargs):
        if html == "parser fails":
            from reposeer.exceptions import ExtractionError

            raise ExtractionError("parser_error")
        return real_extract(self, html, *args, **kwargs)

    monkeypatch.setattr(ArticleExtractor, "extract", extract)

    def handler(request):
        path = request.url.path
        if path == "/timeout":
            raise httpx.ReadTimeout("secret query must not be printed", request=request)
        if path == "/404":
            return httpx.Response(404)
        if path == "/empty":
            return httpx.Response(204)
        content = {"/json": '{"a":1}', "/parser": "parser fails", "/blank": "<html></html>"}.get(
            path, HTML
        )
        return httpx.Response(
            200,
            text=content,
            headers={
                "content-type": "application/json" if path == "/json" else "text/html",
            },
        )

    urls = [
        f"https://blog.example/{p}"
        for p in ["timeout", "404", "empty", "json", "parser", "blank", "good"]
    ]
    with HTTPClient(max_retries=1, transport=httpx.MockTransport(handler)) as http:
        result = pipeline(tmp_path, http).crawl(urls)
    collection = result.collection_result
    assert collection.status == "partial"
    assert len(collection.records) == 4
    assert len(collection.errors) == 6
    assert {e.message for e in collection.errors} >= {
        "empty_response",
        "parser_error",
        "empty_content",
        "unsupported_content_type",
    }
    assert sum(r.payload["content_status"] == "full" for r in collection.records) == 1
    assert all(r.payload["content"] is None for r in collection.records[:-1])
    raw_ids = {r.raw_record_id for r in collection.records}
    assert sum(e.raw_record_id in raw_ids for e in collection.errors) == 3
    assert "secret" not in result.manifest_path.read_text()


def test_budget_cursor_and_failed_batch(tmp_path):
    with HTTPClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        crawler = pipeline(tmp_path, http)
        urls = ["https://blog.example/1", "https://blog.example/2"]
        result = crawler.crawl(urls, max_candidates=1)
        assert result.collection_result.status == "failed"
        assert result.collection_result.metadata.next_cursor == "1"
        assert result.collection_result.metadata.coverage[0].status == "unavailable"
        resumed = crawler.crawl(urls, max_candidates=1, cursor="1")
        assert resumed.collection_result.metadata.pagination_complete
        assert not JsonlStorage().read_contract_records(result.output_path)
        for cursor in ["-1", "bad", "3"]:
            with pytest.raises(ValueError):
                crawler.crawl(urls, cursor=cursor)


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest",
        "file:///tmp/a",
        "http://[::1]/a",
    ],
)
def test_redirect_destination_checked_before_request(tmp_path, location):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": location})

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        result = pipeline(tmp_path, http).crawl(["https://blog.example/start"])
    assert len(calls) == 1
    assert result.collection_result.status == "failed"


@pytest.mark.parametrize("limit", [0, 2])
def test_redirect_loop_or_limit(tmp_path, limit):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/start" if limit else "/next"})

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        result = pipeline(tmp_path, http, max_redirects=limit).crawl(["https://blog.example/start"])
    assert len(calls) == 1
    assert result.collection_result.status == "failed"


def test_credentials_cookies_not_forwarded_and_cache_bypassed(tmp_path):
    from reposeer.collection.http.cache import ResponseCache

    requests = []

    def handler(request):
        requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "api-key" not in request.headers
        return httpx.Response(
            200, text=HTML, headers={"content-type": "text/html", "set-cookie": "session=secret"}
        )

    with HTTPClient(
        transport=httpx.MockTransport(handler),
        cache=ResponseCache(tmp_path / "cache"),
        headers={"Authorization": "Bearer secret", "api-key": "secret", "Cookie": "a=secret"},
    ) as http:
        crawler = pipeline(tmp_path, http)
        crawler.crawl(["https://blog.example/page"])
        crawler.crawl(["https://blog.example/page"])
    assert len(requests) == 2


def test_decoded_stream_size_limit_and_close(tmp_path):
    class Stream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b"12345"
            yield b"67890"
            raise AssertionError("must stop reading oversized response")

        def close(self):
            self.closed = True

    stream = Stream()
    with HTTPClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    ) as http:
        result = pipeline(tmp_path, http, max_response_bytes=8).crawl(
            ["https://blog.example/large"]
        )
    assert stream.closed
    assert result.collection_result.status == "failed"
    assert result.collection_result.records == []


def test_cli_validation_and_offline_crawl(tmp_path, monkeypatch):
    import reposeer.cli.crawl_external as command

    runner = CliRunner()
    assert runner.invoke(app, ["crawl-external"]).exit_code == 2
    assert (
        runner.invoke(app, ["crawl-external", "https://a.example", "--from-stored"]).exit_code == 2
    )
    assert (
        runner.invoke(
            app, ["crawl-external", "--candidates-file", str(tmp_path / "missing")]
        ).exit_code
        == 2
    )
    assert runner.invoke(app, ["crawl-external", "https://user:secret@a.example"]).exit_code == 2
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=HTML, headers={"content-type": "text/html"})
        )
    ) as http:
        monkeypatch.setattr(command, "ExternalCrawlPipeline", lambda **kw: pipeline(tmp_path, http))
        result = runner.invoke(app, ["crawl-external", "https://blog.example/page"])
    assert result.exit_code == 0, result.output
    assert "extracted: 1" in result.output


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"id": 42, "deleted": True},
        {"id": 41, "text": "wrong"},
        {"id": 42},
        {"id": 42, "text": " "},
    ],
)
def test_unavailable_native_content_is_not_empty_success(tmp_path, payload):
    with HTTPClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        result = pipeline(tmp_path, http).crawl(["https://news.ycombinator.com/item?id=42"])
    assert result.collection_result.status in {"partial", "failed"}
    assert result.collection_result.errors
    assert all(r.source_record_id == "hn:42" for r in result.collection_result.records)
    assert all(r.payload["source"] == "hackernews" for r in result.collection_result.records)
    assert all(
        r.payload["content_status"] == "unavailable" for r in result.collection_result.records
    )


def test_retry_cooldown_and_timeout_use_shared_http_policy(tmp_path):
    calls = []
    time_state = [1_700_000_000.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        time_state[0] += seconds

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if len(calls) == 2:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    with HTTPClient(
        transport=httpx.MockTransport(handler),
        timeout=1.5,
        max_retries=3,
        backoff_factor=0,
        clock=lambda: time_state[0],
        sleep=sleep,
    ) as http:
        result = pipeline(tmp_path, http).crawl(["https://blog.example/retry"])
    assert len(calls) == 3
    assert waits == [2.0, 0.0]
    assert all(set(r.extensions["timeout"].values()) == {1.5} for r in calls)
    assert result.collection_result.status == "complete"


def test_collector_rejects_historical_request_before_fetch(tmp_path):
    from reposeer.contracts import CollectionRequest, CollectionSubject

    request = CollectionRequest(
        schema_version="0.1.0",
        record_type="collection_request",
        request_id="00000000-0000-4000-8000-000000000001",
        run_id="00000000-0000-4000-8000-000000000002",
        collector_name="external_crawler",
        source="web_article",
        subjects=[
            CollectionSubject(
                repo_id=None, repo_full_name=None, package_id=None, url="https://blog.example/a"
            )
        ],
        record_kinds=["article"],
        time_range=None,
        cutoff_at="2024-03-01T00:00:00.000Z",
        cursor=None,
        max_pages=1,
        max_records=1,
    )

    def unexpected(request):
        raise AssertionError("Historical live fetch is forbidden")

    with HTTPClient(transport=httpx.MockTransport(unexpected)) as http:
        with pytest.raises(ValueError, match="historical_content_reconstruction_unsupported"):
            pipeline(tmp_path, http).collector.collect(request, ["https://blog.example/a"])


def test_offline_sample_produces_replayable_dataset(tmp_path):
    from scripts.crawl_external_sample import run_sample

    result = run_sample(tmp_path)
    assert result.collection_result.status == "complete"
    assert len(JsonlStorage().read_contract_records(result.output_path)) == 1
    assert json.loads(result.manifest_path.read_text())["documents_extracted"] == 1


def mapping_evidence():
    from uuid import uuid4

    from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence

    common = dict(
        schema_version="0.1.0",
        record_type="raw_record",
        run_id=str(uuid4()),
        retrieved_at="2026-10-15T00:00:00.000Z",
        event_at=None,
        available_at=None,
        http_status=200,
        raw_body_sha256=None,
    )
    pypi = RawRecord(
        **common,
        raw_record_id=f"raw:{uuid4()}",
        source="pypi_json",
        source_record_type="dependency_metadata",
        source_record_id="flask",
        repo_id=None,
        package_id="pypi:flask",
        source_url="https://pypi.org/pypi/flask/json",
        payload={
            "info": {
                "name": "Flask",
                "project_urls": {"Source": "https://github.com/pallets/flask/"},
            }
        },
    )
    github = RawRecord(
        **common,
        raw_record_id=f"raw:{uuid4()}",
        source="github_rest",
        source_record_type="github_repository",
        source_record_id="596892",
        repo_id="github:repository:596892",
        package_id=None,
        source_url="https://api.github.com/repos/pallets/flask",
        payload={
            "id": 596892,
            "full_name": "pallets/flask",
            "html_url": "https://github.com/pallets/flask",
            "private": False,
        },
    )
    return RepositoryScopeEvidence(pypi, github)


def test_verified_scope_fills_repo_without_modifying_discovery(tmp_path):
    from uuid import uuid4

    evidence = mapping_evidence()
    candidate = evidence.pypi.model_copy(
        update={
            "raw_record_id": f"raw:{uuid4()}",
            "source": "web_article",
            "source_record_type": "article",
            "source_url": "https://blog.example/article",
            "source_record_id": None,
            "payload": {"record_stage": "discovery_candidate", "source": "web", "title": "Article"},
        }
    )
    before = candidate.model_dump(mode="json")
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=HTML, headers={"content-type": "text/html"})
        )
    ) as http:
        result = pipeline(tmp_path, http, repository_evidence=[evidence]).crawl(
            [candidate, "https://blog.example/unscoped"]
        )
    scoped, unscoped = result.collection_result.records
    assert scoped.repo_id == "github:repository:596892"
    assert scoped.payload["repository_scope"]["historical_validity"] == "unverified"
    assert scoped.payload["repository_scope"]["article_relevance"] == "unverified"
    assert scoped.payload["discovery"] == before
    assert candidate.repo_id is None
    assert unscoped.repo_id is None and unscoped.package_id is None
    assert scoped.available_at == scoped.retrieved_at


@pytest.mark.parametrize("case", ["package_mismatch", "repo_mismatch", "private", "wrong_origin"])
def test_unverified_mapping_evidence_rejected(case):
    from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence

    evidence = mapping_evidence()
    pypi = evidence.pypi.model_copy(deep=True)
    github = evidence.github.model_copy(deep=True)
    if case == "package_mismatch":
        pypi.payload["info"]["name"] = "OtherPackage"
    elif case == "repo_mismatch":
        github.payload["id"] = 42
    elif case == "private":
        github.payload["private"] = True
    else:
        pypi.source_url = "https://blog.example/pypi.json"
    with pytest.raises(ValueError):
        RepositoryScopeEvidence(pypi, github).resolve()
