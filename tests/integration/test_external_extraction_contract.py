"""T-017 acceptance tests: complete text, extraction invariants and source failures."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.collector import ExternalCrawlerCollector
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.contracts import Article
from reposeer.contracts.identity import article_id, canonical_article_url
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.schemas.external.candidate import ExternalCandidate
from reposeer.schemas.external.document import ArticleDocument
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage

FIXTURES = Path(__file__).parents[1] / "fixtures" / "external_crawler"
NOW = datetime(2026, 10, 16, 6, tzinfo=UTC)
HTML_URL = "https://blog.example/article"
NATIVE = {
    "hackernews": ("https://news.ycombinator.com/item?id=42", "hn:42"),
    "devto": ("https://dev.to/fixture/python-release", "devto:42"),
    "stackexchange": ("https://stackoverflow.com/questions/42/fixture", "se:42"),
}


def source_fixture(source):
    return json.loads((FIXTURES / f"{source}.json").read_text(encoding="utf-8"))


def source_candidate(source):
    url, item_id = NATIVE[source]
    return ExternalCandidate(
        title="Unverified discovery title", url=url, source=source, source_item_id=item_id
    )


def crawler(tmp_path, http, **kwargs):
    return ExternalCrawlPipeline(
        http_client=http,
        storage_paths=StoragePaths(tmp_path),
        policy=URLPolicy(lambda host: ["93.184.216.34"]),
        clock=lambda: NOW,
        **kwargs,
    )


@pytest.fixture
def captured_record(tmp_path):
    body = (FIXTURES / "article.html").read_bytes()
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "text/html; charset=utf-8"}
            )
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([HTML_URL])
    assert result.collection_result.status == "complete"
    return result.collection_result.records[0]


@pytest.mark.parametrize("source", ["web", "rss", "news"])
def test_complete_html_text_and_metadata_survive_raw_replay(tmp_path, source):
    body = (FIXTURES / "article.html").read_bytes()
    expected = (FIXTURES / "article.txt").read_text(encoding="utf-8")
    candidate = ExternalCandidate(title="Discovery title", url=HTML_URL, source=source)
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "text/html; charset=utf-8"}
            )
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([candidate])
    raw = JsonlStorage().read_contract_records(result.output_path)[0]
    validate_external_document_record(raw)
    p = raw.payload
    assert result.collection_result.status == "complete"
    assert p["content"].split() == expected.split()
    for paragraph in expected.splitlines():
        if not paragraph.startswith("|"):
            assert p["content"].count(paragraph) == 1
    assert all(
        noise not in p["content"]
        for noise in [
            "NAVIGATION_NOISE_918",
            "SCRIPT_NOISE_918",
            "STYLE_NOISE_918",
            "COMMENT_NOISE_918",
            "FOOTER_NOISE_918",
        ]
    )
    assert p["source"] == source
    assert p["title"] == "Synthetic release notes"
    assert p["author"] == "Fixture Author"
    assert p["published_at"] == "2024-03-02T04:30:00.000Z"
    assert p["published_date"] == "2024-03-02"  # UTC day crosses the source date.
    assert p["updated_at"] == "2024-03-05T12:00:00.000Z"
    assert p["word_count"] == 151
    assert p["content_sha256"] == hashlib.sha256(p["content"].encode()).hexdigest()
    assert base64.b64decode(p["response_body_base64"]) == body
    assert raw.raw_body_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("path", ["", "/blob/main/CHANGELOG.md"])
def test_github_markdown_keeps_release_headings_in_saved_content(tmp_path, path):
    body = (FIXTURES / "github_changelog.html").read_bytes()
    expected = (FIXTURES / "github_changelog.txt").read_text(encoding="utf-8")
    url = f"https://github.com/fixture/example{path}"
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "text/html; charset=utf-8"}
            )
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([url])
    assert result.collection_result.status == "complete"
    raw = JsonlStorage().read_contract_records(result.output_path)[0]
    validate_external_document_record(raw)
    p = raw.payload
    assert p["content"].split() == expected.split()
    assert p["content"].index("2.1.0") < p["content"].index("Fix a connection timeout")
    assert p["content"].index("2.0.0") < p["content"].index("removes an obsolete")
    assert "GITHUB_NAVIGATION_NOISE" not in p["content"]
    assert "GITHUB_FOOTER_NOISE" not in p["content"]
    # Version headings date individual releases, not the whole document.
    assert p["published_at"] is None and p["published_date"] is None
    assert base64.b64decode(p["response_body_base64"]) == body


@pytest.mark.parametrize("source", NATIVE)
def test_native_full_text_entities_unicode_and_metadata(tmp_path, source):
    native = source_fixture(source)
    with HTTPClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=native))
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate(source)])
    raw = JsonlStorage().read_contract_records(result.output_path)[0]
    validate_external_document_record(raw)
    p = raw.payload
    assert result.collection_result.status == "complete"
    assert p["native_payload"] == native
    assert p["content"] == "Café & Python preserve MixedCase. Second paragraph remains visible."
    assert p["word_count"] == 9
    assert p["title"] == ("Ask HN: Café & Python?" if source == "hackernews" else "Café & Python")
    assert (
        p["author"]
        == {
            "hackernews": "fixture_user",
            "devto": "Fixture Author",
            "stackexchange": "Fixture & Author",
        }[source]
    )
    assert p["published_at"] == (
        "2024-03-01T02:30:00.000Z" if source == "devto" else "2024-03-01T00:00:00.000Z"
    )
    assert (
        p["updated_at"]
        == {
            "hackernews": None,
            "devto": "2024-03-02T10:00:00.000Z",
            "stackexchange": "2024-03-02T00:00:00.000Z",
        }[source]
    )
    assert p["published_date"] == "2024-03-01"
    assert p["content_sha256"] == hashlib.sha256(p["content"].encode()).hexdigest()
    assert json.loads(base64.b64decode(p["response_body_base64"])) == native
    assert_article_compatible(raw)


def assert_article_compatible(raw):
    """Test-only projection; T-018 still owns production normalization."""
    p = raw.payload
    canonical = canonical_article_url(p["url"])
    article = Article(
        schema_version="0.1.0",
        record_type="article",
        article_id=article_id(canonical),
        original_url=raw.source_url,
        canonical_url=canonical,
        title=p["title"],
        article_kind="other",
        authors=[p["author"]] if p["author"] else None,
        language_code=None,
        published_at=p["published_at"],
        published_date=p["published_date"],
        published_precision=p["published_precision"],
        updated_at=p["updated_at"],
        content_status=p["content_status"],
        content_text=p["content"],
        content_sha256=p["content_sha256"],
        content_available_at=p["content_available_at"],
        technology_mentions=[],
        repo_ids=[],
        package_ids=[],
        provenance={
            "source": raw.source,
            "source_url": raw.source_url,
            "retrieved_at": raw.retrieved_at,
            "valid_at": raw.retrieved_at,
            "available_at": raw.available_at,
            "temporal_basis": "observation",
            "raw_record_ids": [raw.raw_record_id],
            "missing_fields": [],
        },
    )
    assert article.content_text == p["content"]


def test_html_extraction_is_compatible_with_canonical_article(captured_record):
    assert_article_compatible(captured_record)


@pytest.mark.parametrize(
    "key",
    [
        "source",
        "source_url",
        "url",
        "published_at",
        "fetched_at",
        "content",
        "word_count",
        "content_sha256",
        "author",
    ],
)
def test_capture_rejects_missing_required_extraction_keys(captured_record, key):
    corrupted = captured_record.model_copy(deep=True)
    corrupted.payload.pop(key)
    with pytest.raises(ValidationError):
        validate_external_document_record(corrupted)


@pytest.mark.parametrize(
    "patch",
    [
        {"url": "not a URL"},
        {"source": " "},
        {"author": ""},
        {"title": []},
        {"content": ""},
        {"content": "  text  "},
        {"content": "Cafe\u0301"},
        {"content": "A\r\nB"},
        {"content_sha256": "0" * 64},
        {"word_count": -1},
        {"word_count": True},
        {"word_count": 1},
        {"published_at": "2024-03-01T00:00:00"},
        {"published_date": "2024-02-30"},
        {"published_precision": "timestamp", "published_at": None},
        {"published_precision": "date"},
        {"published_precision": "unknown"},
        {"published_date": "2024-03-01"},
        {"published_at": "2030-01-01T00:00:00.000Z", "published_date": "2030-01-01"},
        {"updated_at": "2020-01-01T00:00:00.000Z"},
        {"updated_at": "2030-01-01T00:00:00.000Z"},
        {"content_available_at": "2030-01-01T00:00:00.000Z"},
        {"content_status": "unavailable"},
        {"content_status": "full", "content": None},
        {"response_body_base64": "not base64!"},
        {"fetched_at": "2026-10-16T05:00:00.000Z"},
        {"source_url": "https://blog.example/other"},
        {"source_item_id": "other-id"},
        {"candidate_raw_record_id": "raw:00000000-0000-4000-8000-000000000001"},
        {"unexpected_field": True},
    ],
)
def test_capture_rejects_incoherent_extraction(captured_record, patch):
    corrupted = captured_record.model_copy(deep=True)
    corrupted.payload.update(patch)
    with pytest.raises(ValueError):
        validate_external_document_record(corrupted)


@pytest.mark.parametrize(
    "patch",
    [
        {"http_status": 404},
        {"raw_body_sha256": "0" * 64},
        {"available_at": None},
        {"event_at": None},
        {"source_url": "https://blog.example/other"},
    ],
)
def test_capture_rejects_envelope_payload_mismatch(captured_record, patch):
    with pytest.raises(ValueError):
        validate_external_document_record(captured_record.model_copy(update=patch))


@pytest.mark.parametrize(
    "patch", [{"html": "<html>Changed source</html>"}, {"native_payload": {"changed": True}}]
)
def test_capture_rejects_changed_source_payload(captured_record, patch):
    corrupted = captured_record.model_copy(deep=True)
    corrupted.payload.update(patch)
    with pytest.raises(ValueError):
        validate_external_document_record(corrupted)


@pytest.mark.parametrize(
    "patch",
    [
        {"url": "not a URL"},
        {"content": ""},
        {"word_count": -1},
        {"word_count": True},
        {"word_count": 7},
        {"content": "Cafe\u0301"},
        {"extracted_at": NOW.replace(tzinfo=None)},
        {"published_at": datetime(2024, 3, 1)},
        {"published_precision": "timestamp"},
        {"published_precision": "date"},
        {"published_date": "2024-02-30"},
        {
            "published_at": datetime(2030, 1, 1, tzinfo=UTC),
            "published_date": "2030-01-01",
            "published_precision": "timestamp",
        },
        {"updated_at": datetime(2030, 1, 1, tzinfo=UTC)},
    ],
)
def test_article_dto_rejects_invalid_metadata_and_text(patch):
    values = dict(
        url=HTML_URL, source="web", content="Article body", word_count=2, extracted_at=NOW
    )
    values.update(patch)
    with pytest.raises(ValidationError):
        ArticleDocument(**values)


@pytest.mark.parametrize("source", NATIVE)
@pytest.mark.parametrize(
    "failure",
    [
        "malformed_json",
        "null",
        "missing_body",
        "empty_body",
        "script_only",
        "wrong_id",
        "bad_metadata",
    ],
)
def test_native_invalid_responses_retain_evidence_and_report_errors(tmp_path, source, failure):
    native = source_fixture(source)
    item = native["items"][0] if source == "stackexchange" else native
    body_key = {"hackernews": "text", "devto": "body_html", "stackexchange": "body"}[source]
    if failure == "missing_body":
        item.pop(body_key)
    elif failure == "empty_body":
        item[body_key] = " \n "
    elif failure == "script_only":
        item[body_key] = "<script>SCRIPT_NOISE_918</script>"
    elif failure == "wrong_id":
        item["question_id" if source == "stackexchange" else "id"] = 99
    elif failure == "bad_metadata":
        item["title"] = {"unexpected": "object"}
    content = (
        b'{"broken":'
        if failure == "malformed_json"
        else b"null"
        if failure == "null"
        else json.dumps(native).encode()
    )
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=content, headers={"content-type": "application/json"}
            )
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate(source)])
    collection = result.collection_result
    assert collection.status == "partial" and len(collection.errors) == 1
    assert collection.errors[0].code == "parse_error"
    raw = JsonlStorage().read_contract_records(result.output_path)[0]
    validate_external_document_record(raw)
    assert base64.b64decode(raw.payload["response_body_base64"]) == content
    assert raw.payload["content_status"] == "unavailable"
    assert all(
        raw.payload[k] is None
        for k in ["content", "content_sha256", "word_count", "content_available_at"]
    )
    assert collection.errors[0].raw_record_id == raw.raw_record_id


@pytest.mark.parametrize(
    "native",
    [
        {},
        {"items": []},
        {"items": {}},
        {"items": [{}, {}]},
        {"items": ["not an object"]},
        {"error_id": 502, "error_message": "throttle violation"},
    ],
)
def test_stackexchange_invalid_item_census_is_not_empty_success(tmp_path, native):
    with HTTPClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=native))
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate("stackexchange")])
    assert result.collection_result.status == "partial"
    assert result.collection_result.errors[0].code == "parse_error"
    assert result.collection_result.records[0].payload["content_status"] == "unavailable"


def test_real_native_parser_exception_is_caught(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private parser diagnostics")

    monkeypatch.setattr("trafilatura.html2txt", fail)
    with HTTPClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=source_fixture("devto")))
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate("devto")])
    error = result.collection_result.errors[0]
    assert error.message == "native_parser_error"
    assert "private" not in result.manifest_path.read_text()
    validate_external_document_record(result.collection_result.records[0])


def test_invalid_extraction_is_downgraded_before_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ExternalCrawlerCollector,
        "_native_document",
        staticmethod(
            lambda *args: {
                "content": "Valid body",
                "title": "Title",
                "author": None,
                "published_at": None,
                "published_date": None,
                "published_precision": "timestamp",
                "updated_at": None,
            }
        ),
    )
    with HTTPClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=source_fixture("devto")))
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate("devto")])
    assert result.collection_result.errors[0].message == "invalid_extracted_document"
    raw = JsonlStorage().read_contract_records(result.output_path)[0]
    assert raw.payload["content_status"] == "unavailable"
    assert raw.payload["published_precision"] == "unknown"
    validate_external_document_record(raw)


def test_real_html_parser_exception_is_caught_and_raw_preserved(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private parser diagnostics")

    monkeypatch.setattr("trafilatura.extract", fail)
    body = (FIXTURES / "article.html").read_bytes()
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=body, headers={"content-type": "text/html"})
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([HTML_URL])
    assert result.collection_result.status == "partial"
    assert result.collection_result.errors[0].message == "parser_error"
    raw = JsonlStorage().read_contract_records(result.output_path)[0]
    assert base64.b64decode(raw.payload["response_body_base64"]) == body
    assert raw.payload["content_status"] == "unavailable"
    assert "private" not in result.manifest_path.read_text()
    validate_external_document_record(raw)


@pytest.mark.parametrize("source", NATIVE)
@pytest.mark.parametrize("media", ["text/html", "", " "])
def test_unsupported_native_content_type_retains_valid_failure_capture(tmp_path, source, media):
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b'{"id":42}', headers={"content-type": media})
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate(source)])
    assert result.collection_result.status == "partial"
    assert result.collection_result.errors[0].message == "unsupported_content_type"
    validate_external_document_record(result.collection_result.records[0])


@pytest.mark.parametrize("content", [b'{"id":42,"score":NaN}', b'{"id":42,"score":Infinity}'])
def test_non_finite_native_json_is_parse_error(tmp_path, content):
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=content, headers={"content-type": "application/json"}
            )
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate("devto")])
    assert result.collection_result.errors[0].message == "invalid_native_json"
    validate_external_document_record(result.collection_result.records[0])


def test_storage_boundary_rejects_corrupted_collector_output(tmp_path, monkeypatch):
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=(FIXTURES / "article.html").read_bytes(),
                headers={"content-type": "text/html"},
            )
        )
    ) as http:
        pipe = crawler(tmp_path / "fresh", http)
        original = pipe.collector.collect

        def corrupted(*args):
            result = original(*args)
            result.records[0].payload["content_sha256"] = "0" * 64
            return result

        monkeypatch.setattr(pipe.collector, "collect", corrupted)
        with pytest.raises(ValueError):
            pipe.crawl([HTML_URL])
    assert not (tmp_path / "fresh" / "raw" / "external" / "documents.jsonl").exists()


@pytest.mark.parametrize("source", NATIVE)
@pytest.mark.parametrize("body", [b"", b" \r\n\t"])
def test_native_empty_http_body_is_explicit_failure(tmp_path, source, body):
    with HTTPClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        )
    ) as http:
        result = crawler(tmp_path, http).crawl([source_candidate(source)])
    assert result.collection_result.status == "failed"
    assert result.collection_result.records == []
    assert result.collection_result.errors[0].message == "empty_response"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_multi_hop_relative_redirect_preserves_original_and_final_url(tmp_path, status):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(status, headers={"location": "/nested/step"})
        if request.url.path == "/nested/step":
            return httpx.Response(307, headers={"location": "../article?view=full"})
        return httpx.Response(
            200,
            content=(FIXTURES / "article.html").read_bytes(),
            headers={"content-type": "text/html"},
        )

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        result = crawler(tmp_path, http, max_redirects=2).crawl(["https://blog.example/start"])
    assert paths == ["/start", "/nested/step", "/article"]
    assert result.collection_result.status == "complete"
    raw = result.collection_result.records[0]
    assert raw.source_url == raw.payload["source_url"] == "https://blog.example/start"
    assert (
        raw.payload["url"] == raw.payload["fetched_url"] == "https://blog.example/article?view=full"
    )
    validate_external_document_record(raw)


def test_unique_redirect_chain_stops_at_budget(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": f"/step/{len(calls)}"})

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        result = crawler(tmp_path, http, max_redirects=2).crawl(["https://blog.example/start"])
    assert len(calls) == 3
    assert result.collection_result.status == "failed"
    assert not result.collection_result.records
    assert result.collection_result.errors[0].code == "validation_error"


def test_redirect_without_location_is_not_an_empty_success(tmp_path):
    with HTTPClient(transport=httpx.MockTransport(lambda r: httpx.Response(302))) as http:
        result = crawler(tmp_path, http).crawl([HTML_URL])
    assert result.collection_result.status == "failed"
    assert len(result.collection_result.errors) == 1
    assert not result.collection_result.records


@pytest.mark.parametrize("source", ["web", *NATIVE])
@pytest.mark.parametrize("error_type", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_exhausted_timeout_per_source_continues_batch(tmp_path, source, error_type):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/good":
            return httpx.Response(
                200,
                content=(FIXTURES / "article.html").read_bytes(),
                headers={"content-type": "text/html"},
            )
        raise error_type("temporary timeout", request=request)

    candidate = HTML_URL if source == "web" else source_candidate(source)
    with HTTPClient(
        transport=httpx.MockTransport(handler),
        max_retries=2,
        backoff_factor=0,
        sleep=lambda seconds: None,
    ) as http:
        result = crawler(tmp_path, http).crawl([candidate, "https://blog.example/good"])
    assert len(calls) == 3
    assert result.collection_result.status == "partial"
    assert len(result.collection_result.records) == 1
    assert result.collection_result.errors[0].code == "network_error"
    assert result.collection_result.errors[0].retryable
    validate_external_document_record(result.collection_result.records[0])
