"""Offline T-016 -> T-017 handoff, native bodies, failures and raw replay."""

import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.extractor import ArticleExtractor
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.collectors.external.crawler.validation import validate_external_document_record
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
        assert candidates[0].source_url == "https://news.ycombinator.com/item?id=42"
        assert candidates[0].payload["candidate_url"] == "https://blog.example/article"
        assert candidates[0].repo_id is candidates[0].package_id is None
        newer = candidates[0].model_copy(update={
            "raw_record_id": f"raw:{uuid4()}",
            "payload": dict(candidates[0].payload, title="A newer discovery observation",
                            url="https://blog.example/updated", candidate_url="https://blog.example/updated"),
        })
        newer_before = newer.model_dump(mode="json")
        # Two immutable observations of one candidate must fetch only once per crawl batch.
        batch = [*candidates, newer, "https://blog.example/article?utm_source=duplicate"]
        with pipeline(tmp_path, http) as crawler:
            first = crawler.crawl(batch)
            second = crawler.crawl(batch)
        assert not http._sync_client.is_closed
    replay = JsonlStorage().read_contract_records(first.output_path)
    assert len(replay) == 2
    assert replay[0].raw_record_id != replay[1].raw_record_id
    assert first.run_id != second.run_id
    raw = replay[0]
    assert isinstance(validate_record(raw.model_dump_json()), RawRecord)
    validate_external_document_record(raw)
    assert raw.payload["record_stage"] == "external_document"
    assert raw.repo_id is raw.package_id is None
    assert raw.source_record_id is None
    assert raw.payload["discovery_provider"] == "hackernews"
    assert raw.payload["discovery_source_item_id"] == "hn:42"
    assert raw.payload["discovery_source_url"] == candidates[0].source_url
    assert not raw.payload["discovery_lineage_complete"]
    assert {ref["candidate_raw_record_id"] for ref in raw.payload["discovery_references"]} == {
        candidates[0].raw_record_id, newer.raw_record_id,
    }
    assert calls.count("https://blog.example/article") == 2
    assert not any("firebaseio.com" in url or "news.ycombinator.com" in url for url in calls)
    assert raw.source_url == "https://blog.example/article"
    assert raw.source == "web_article" and raw.source_record_type == "article"
    assert raw.retrieved_at == raw.payload["fetched_at"] == "2026-10-16T06:00:00.000Z"
    assert raw.event_at == raw.payload["published_at"] == "2024-03-01T02:30:00.000Z"
    assert raw.available_at == raw.payload["content_available_at"] == raw.retrieved_at
    assert raw.payload["historical_version_status"] == "observation_only"
    assert raw.payload["url"] == "https://blog.example/final"
    assert raw.payload["source"] == "web"
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
    assert newer.model_dump(mode="json") == newer_before
    assert first.collection_result.status == "complete"
    manifest = json.loads(first.manifest_path.read_text())
    assert manifest["documents_extracted"] == 1
    assert manifest["input_candidate_count"] == 3
    assert manifest["unique_candidate_count"] == 1 and manifest["deduplicated_candidate_count"] == 2
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
@pytest.mark.parametrize("recorded", [False, True])
def test_native_api_body_and_revision_metadata(
    tmp_path, source, url, item_id, native_payload, endpoint, recorded
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
    original = None
    if recorded:
        observation_url = {
            "hackernews": "https://news.ycombinator.com/item?id=42",
            "devto": "https://dev.to/api/articles/search",
            "stackexchange": "https://api.stackexchange.com/2.3/search/advanced",
        }[source]
        candidate = RawRecord(
            schema_version="0.1.0", record_type="raw_record", raw_record_id=f"raw:{uuid4()}",
            run_id=str(uuid4()), source="web_article", source_record_type="article",
            source_record_id=item_id, source_url=observation_url, repo_id=None, package_id=None,
            retrieved_at="2026-10-16T06:00:00.000Z", event_at=None, available_at=None,
            http_status=None, raw_body_sha256=None,
            payload=dict(candidate.model_dump(mode="json"), record_stage="discovery_candidate",
                         discovery_provider=source, candidate_url=url),
        )
        original = candidate.model_dump(mode="json")
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
    if recorded:
        assert raw.payload["candidate_raw_record_id"] == candidate.raw_record_id
        assert raw.payload["discovery"] == original
        assert candidate.model_dump(mode="json") == original
        assert raw.repo_id is raw.package_id is None
        validate_external_document_record(raw)


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
    "candidate_paths,cursor,expected_path",
    [
        (["1", "2", "3"], "3", None),
        (["1", "2", "3"], "4", None),
        (["1", "2", "3"], "2", "/3"),
        (["1", "1", "2"], "2", None),
        (["1", "2", "3"], None, "/1"),
        (["1", "2", "3"], "0", "/1"),
        ([], None, None),
    ],
)
def test_cursor_requires_remaining_candidate_groups(tmp_path, candidate_paths, cursor, expected_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        crawler = pipeline(tmp_path, http)
        candidates = [f"https://blog.example/{path}" for path in candidate_paths]
        if expected_path is None:
            reason = "no_remaining_candidates" if candidates else "provide_at_least_one_candidate"
            with pytest.raises(ValueError, match=f"^{reason}$"):
                crawler.crawl(candidates, max_candidates=1, cursor=cursor)
            assert calls == []
            assert not (tmp_path / "raw" / "external" / "documents.jsonl").exists()
            assert not list(tmp_path.rglob("crawl_manifest_*.json"))
        else:
            result = crawler.crawl(candidates, max_candidates=1, cursor=cursor)
            assert calls == [expected_path]
            assert len(result.collection_result.records) == 1
            assert result.collection_result.records[0].source_url == f"https://blog.example{expected_path}"
            assert result.collection_result.metadata.pagination_complete == (cursor == "2")


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
            "payload": {"record_stage": "discovery_candidate", "source": "web", "title": "Article",
                        "candidate_url": "https://blog.example/article"},
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


@pytest.mark.parametrize("candidate_url", [None, "", "not-a-url", "ftp://blog.example/article",
                                            "https://user:secret@blog.example/article", 123])
def test_invalid_discovery_candidate_url_has_no_observation_url_fallback(tmp_path, candidate_url):
    payload = {"record_stage": "discovery_candidate", "discovery_provider": "hackernews"}
    if candidate_url is not None:
        payload["candidate_url"] = candidate_url
    candidate = RawRecord(
        schema_version="0.1.0", record_type="raw_record", raw_record_id=f"raw:{uuid4()}",
        run_id=str(uuid4()), source="web_article", source_record_type="article",
        source_record_id="hn:42", source_url="https://news.ycombinator.com/item?id=42",
        repo_id=None, package_id=None, retrieved_at="2026-10-16T06:00:00.000Z",
        event_at=None, available_at=None, http_status=None, raw_body_sha256=None, payload=payload,
    )
    original = candidate.model_dump(mode="json")

    def unexpected_fetch(request):
        raise AssertionError("Invalid candidate URL must fail before any HTTP fetch")

    with HTTPClient(transport=httpx.MockTransport(unexpected_fetch)) as http:
        with pytest.raises(ValueError):
            pipeline(tmp_path, http).crawl([candidate])
    assert candidate.model_dump(mode="json") == original
    assert not StoragePaths(tmp_path).raw_path("external", "documents").exists()


def discovery_observation(url, *, provider="hackernews", item_id="hn:42", query_id="query:a"):
    candidate = ExternalCandidate(title="Unverified hit", url=url, source=provider,
                                  source_item_id=item_id, query_id=query_id, query="Python packages",
                                  query_intent="direct_package", query_associations=[
                                      {"query_id": query_id, "job_id": "job:" + query_id, "provider": provider}])
    return RawRecord(
        schema_version="0.1.0", record_type="raw_record", raw_record_id=f"raw:{uuid4()}",
        run_id=str(uuid4()), source="web_article", source_record_type="article", source_record_id=item_id,
        source_url={"hackernews": "https://hn.algolia.com/api/v1/search",
                    "devto": "https://dev.to/api/articles/search",
                    "stackexchange": "https://api.stackexchange.com/2.3/search/advanced"}[provider],
        repo_id=None, package_id=None,
        retrieved_at="2026-10-16T06:00:00.000Z", event_at=None, available_at=None,
        http_status=None, raw_body_sha256=None,
        payload=dict(candidate.model_dump(mode="json"), record_stage="discovery_candidate",
                     discovery_provider=provider, candidate_url=url),
    )


@pytest.mark.parametrize("failure", [None, "content_type", "timeout"])
def test_grouped_observations_preserve_all_lineage_and_failure_attribution(tmp_path, failure):
    url = "https://blog.example/article"
    candidates = [discovery_observation(url), discovery_observation(url, query_id="query:b"),
                  discovery_observation(url, provider="devto", item_id="devto:9", query_id="query:c"),
                  discovery_observation("https://blog.example/other", item_id="hn:88")]
    originals = [candidate.model_dump_json() for candidate in candidates]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/article" and failure == "timeout":
            raise httpx.ReadTimeout("injected", request=request)
        if request.url.path == "/article" and failure == "content_type":
            return httpx.Response(200, json={"unexpected": "body"})
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        result = pipeline(tmp_path, http).crawl(iter([*candidates, candidates[0]]))
    assert calls == ["/article", "/other"]
    manifest = json.loads(result.manifest_path.read_text())
    group = manifest["candidate_groups"][0]
    references = group["discovery_references"]
    assert bool(group["errors"]) is bool(failure)
    if failure:
        assert group["errors"] == [result.collection_result.errors[0].model_dump(mode="json")]
    assert len(references) == 3 and not group["discovery_lineage_complete"]
    assert {ref["candidate_raw_record_id"] for ref in references} == {r.raw_record_id for r in candidates[:3]}
    assert {ref["query_id"] for ref in references} == {"query:a", "query:b", "query:c"}
    assert {ref["discovery_provider"] for ref in references} == {"hackernews", "devto"}
    if failure == "timeout":
        assert len(result.collection_result.records) == 1 and result.collection_result.errors
    else:
        raw = result.collection_result.records[0]
        assert raw.payload["discovery_references"] == references
        assert raw.payload["source"] == "web" and raw.source_record_id is None
        assert raw.payload["content_status"] == ("unavailable" if failure else "full")
        validate_external_document_record(raw)
    assert [candidate.model_dump_json() for candidate in candidates] == originals


@pytest.mark.parametrize("state_mode", ["available", "missing", "wrong_query", "wrong_raw", "missing_union", "missing_raw"])
def test_explicit_projection_state_retains_final_query_union(tmp_path, monkeypatch, state_mode):
    from reposeer.technology.models import SearchPlan, SearchTerm

    plan = SearchPlan(terms=[SearchTerm(query=query, intent="direct_package", platform="hackernews")
                            for query in ("query a", "query b")])
    monkeypatch.setattr("reposeer.pipelines.external.SearchTermGenerator.build_search_plan", lambda self, **kw: plan)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [{"objectID": "42", "title": "Hit",
                                                       "url": "https://blog.example/article"}], "nbPages": 1})
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        discovery = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path), clock=lambda: NOW)
        found = discovery.discover("pypi:flask", sources=["hackernews"])
        candidates = JsonlStorage().read_contract_records(found.output_path)
        source_bytes = found.output_path.read_bytes()
        assert len(candidates) == 1 and len(candidates[0].payload["query_associations"]) == 1
        state_path = Path(found.discovery_manifest["candidate_projection_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state_mode == "wrong_query":
            state["candidate_projections"][0]["query_associations"][0]["query"] = "forged"
        if state_mode == "wrong_raw":
            state["records"][0]["payload"]["title"] = "Altered snapshot"
        if state_mode == "missing_union":
            state["candidate_projections"][0]["query_associations"].pop()
        if state_mode == "missing_raw":
            state["candidate_projections"] = []
        if state_mode.startswith("wrong") or state_mode in {"missing_union", "missing_raw"}:
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with pytest.raises(ValueError, match="projection"):
                pipeline(tmp_path, http).crawl(candidates, discovery_state_paths=[state_path])
            assert calls == ["hn.algolia.com", "hn.algolia.com"]
        else:
            result = pipeline(tmp_path, http).crawl(
                candidates, discovery_state_paths=[state_path] if state_mode == "available" else [])
            raw = result.collection_result.records[0]
            references = raw.payload["discovery_references"]
            assert raw.payload["discovery_lineage_complete"] is (state_mode == "available")
            assert references[0]["discovery_run_id"] == found.run_id
            assert references[0]["candidate_id"] == candidates[0].payload["candidate_id"]
            assert len(references[0]["query_associations"]) == (2 if state_mode == "available" else 1)
            assert calls == ["hn.algolia.com", "hn.algolia.com", "blog.example"]
        assert found.output_path.read_bytes() == source_bytes


def test_large_stored_discovery_streams_bounded_unique_batches(tmp_path, monkeypatch):
    import reposeer.cli.crawl_external as command

    paths = StoragePaths(tmp_path)
    source_path = paths.raw_path("external", "discovery")
    JsonlStorage().append_contract_records_durable(source_path, (
        discovery_observation(f"https://blog.example/{index % 7}", item_id=f"hn:{index % 7 + 1}",
                              query_id=f"query:{index}") for index in range(1005)))
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    class StreamingStorage(JsonlStorage):
        def read_contract_records(self, path):
            raise AssertionError("Stored CLI must use the incremental JSONL iterator")

    monkeypatch.setattr(command, "JsonlStorage", StreamingStorage)
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(command, "ExternalCrawlPipeline", lambda **kw: pipeline(tmp_path, http))
        args = ["crawl-external", "--from-stored", "--data-dir", str(tmp_path), "--max-candidates", "5"]
        first = CliRunner().invoke(app, args)
        assert first.exit_code == 1, first.output  # More unique groups remain.
        assert calls == [f"/{index}" for index in range(5)]
        second = CliRunner().invoke(app, args + ["--cursor", "5"])
        assert second.exit_code == 0, second.output
    assert calls == [f"/{index}" for index in range(7)]
    manifests = [json.loads(path.read_text()) for path in (paths.metadata_dir / "collection_runs").glob("crawl_manifest_*.json")]
    assert {manifest["attempted_candidate_count"] for manifest in manifests} == {2, 5}
    assert all(manifest["input_candidate_count"] == 1005 and manifest["unique_candidate_count"] == 7
               and manifest["deduplicated_candidate_count"] == 998 for manifest in manifests)
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    assert len(JsonlStorage().read_contract_records(paths.raw_path("external", "documents"))) == 7


@pytest.mark.parametrize("url,native,capture_id,response", [
    ("https://news.ycombinator.com/item?id=42", "hackernews", "hn:42",
     {"id": 42, "text": "<p>A native Hacker News discussion body.</p>"}),
    ("https://stackoverflow.com/questions/42/example", "stackexchange", "se:42",
     {"items": [{"question_id": 42, "body": "<p>A native Stack Exchange question body.</p>"}]}),
])
def test_foreign_discovery_id_is_not_native_capture_identity(tmp_path, url, native, capture_id, response):
    candidate = discovery_observation(url, provider="devto", item_id="devto:9")
    with HTTPClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as http:
        result = pipeline(tmp_path, http).crawl([candidate])
    raw = result.collection_result.records[0]
    assert result.collection_result.status == "complete"
    assert raw.payload["source"] == native and raw.source_record_id == capture_id
    assert raw.payload["discovery_provider"] == "devto"
    assert raw.payload["discovery_source_item_id"] == "devto:9"
    validate_external_document_record(raw)


@pytest.mark.parametrize("mapping", ["verified", "mismatch", "missing"])
def test_search_target_handoff_requires_verified_mapping(tmp_path, mapping):
    evidence = mapping_evidence()

    def handler(request):
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [{"objectID": "44", "title": "Flask release",
                "url": "https://blog.example/flask"}], "nbPages": 1})
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        with ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path)) as discovery:
            found = discovery.discover(evidence.github, package_id="pypi:flask", sources=["hackernews"])
        candidate = found.collection_result.records[0]
        assert candidate.package_id is candidate.repo_id is None
        if mapping == "mismatch":
            candidate = candidate.model_copy(deep=True)
            candidate.payload["search_target_repo_id"] = "github:repository:999"
        before = candidate.model_dump(mode="json")
        result = pipeline(tmp_path, http, repository_evidence=[] if mapping == "missing" else [evidence]).crawl([candidate])
        document = result.collection_result.records[0]
        assert candidate.model_dump(mode="json") == before
        assert document.payload["discovery"] == before
        if mapping == "verified":
            assert document.package_id == "pypi:flask" and document.repo_id == evidence.github.repo_id
            assert document.payload["repository_scope"]["article_relevance"] == "unverified"
        else:
            assert document.package_id is document.repo_id is None


def test_candidate_budget_round_robins_and_retains_duplicate_provider_lineage(tmp_path):
    from reposeer.pipelines.external_crawl import _group_candidates, _select_candidate_groups

    candidates = [ExternalCandidate(title="Flask", url=f"https://blog.example/{provider}/{index}",
                  source=provider, subject_match_status="high_confidence_metadata")
                  for provider in ("hackernews", "news", "rss") for index in range(4)]
    duplicate = candidates[0].model_copy(update={"source": "rss"})
    groups, count, _ = _group_candidates([*candidates, duplicate])
    selected = _select_candidate_groups(groups)[:3]
    assert count == 13 and len(groups) == 12
    assert {group.primary.source for group in selected} == {"hackernews", "news", "rss"}
    shared = next(group for group in groups if group.primary.url == candidates[0].url)
    assert {ref["discovery_provider"] for ref in shared.references.values()} == {"hackernews", "rss"}
