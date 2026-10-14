"""External providers, discovery pipeline, and crawlers reuse common HTTP and contract layers."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.collection import CollectionProgress, HTTPClient, ResponseCache, RunManager
from reposeer.collectors.external.crawler.extractor import ArticleExtractor
from reposeer.collectors.external.crawler.fetcher import HTMLFetcher
from reposeer.contracts import RawRecord, validate_record
from reposeer.exceptions import HTTPClientError
from reposeer.pipelines.external import ExternalPipeline
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology.functional_profile import FunctionalClaim, FunctionalProfileResult


def test_external_discovery_and_crawler_share_http_cache_and_progress(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "42",
                            "title": "Example",
                            "url": "https://example.test/article",
                            "points": 5,
                        }
                    ]
                },
            )
        return httpx.Response(200, text="<html>Example article</html>")

    manager = RunManager(tmp_path / "runs")
    progress = CollectionProgress(manager.start_run("external"), manager)
    with HTTPClient(
        cache=ResponseCache(tmp_path / "cache"), transport=httpx.MockTransport(handler)
    ) as http:
        with HackerNewsClient(http_client=http, progress=progress) as discovery:
            candidates = discovery.search("example", limit=1)
        with HTMLFetcher(http, progress=progress) as fetcher:
            assert fetcher.fetch(str(candidates[0].url)) == "<html>Example article</html>"
            assert fetcher.fetch(str(candidates[0].url)) == "<html>Example article</html>"
        assert http.get(
            "https://example.test/article", headers={"Accept": "text/html,application/xhtml+xml"}
        ).extensions["from_cache"]
    assert len(requests) == 2
    assert requests[0].url.path == "/api/v1/search"
    assert requests[0].url.params["query"] == "example"
    assert requests[0].headers["accept"] == "application/json"
    assert requests[1].headers["accept"] == "text/html,application/xhtml+xml"
    restored = manager.resume_run("external")
    assert restored.collected_count == 3
    assert restored.error_count == 0
    assert restored.get_checkpoint("external.html.url") == "https://example.test/article"


def test_external_failure_is_logged_and_does_not_advance_checkpoint(tmp_path, caplog):
    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("external-errors"), manager)
    with (
        HTTPClient(
            max_retries=1, transport=httpx.MockTransport(lambda request: httpx.Response(404))
        ) as http,
        HTMLFetcher(http, progress=progress) as fetcher,
        HackerNewsClient(http_client=http, progress=progress) as discovery,
    ):
        assert fetcher.fetch("https://example.test/missing") is None
        with pytest.raises(HTTPClientError):
            discovery.search("example")
    restored = manager.resume_run("external-errors")
    assert restored.error_count == 2
    assert restored.collected_count == 0
    assert restored.checkpoint_state == {}
    assert "Failed to collect data" in caplog.text


@pytest.mark.parametrize("payload", [{}, {"hits": None}, {"hits": [{"title": "Bad", "url": "bad"}]}])
def test_malformed_search_is_not_empty_success(payload):
    with HTTPClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as http:
        with pytest.raises(ValueError):
            HackerNewsClient(http_client=http).search("example")


def test_empty_search_and_unknown_score_are_distinct():
    payload = {"hits": []}
    with HTTPClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as http:
        discovery = HackerNewsClient(http_client=http)
        assert discovery.search("example") == []
        payload["hits"] = [{"title": "Example", "objectID": "42"}]
        assert discovery.search("example")[0].score is None


def _load_sample_repo_raw() -> RawRecord:
    sample_file = Path(__file__).resolve().parents[2] / "examples/contracts/github_raw_record.json"
    data = json.loads(sample_file.read_text(encoding="utf-8"))
    record = RawRecord.model_validate(data)
    return validate_record(record)


# ---------------------------------------------------------------------------
# Task T-016 Integration Tests
# ---------------------------------------------------------------------------


def test_end_to_end_discovery_with_enriched_d03_functional_profile(tmp_path: Path):
    """Test 1 — End-to-End Discovery with Enriched D03 FunctionalProfile:

    Generates all 4 intents, valid query_set_hash, writes discovery.jsonl,
    validates canonical RawRecord contracts, and ensures manifest auditing.
    """
    raw_repo = _load_sample_repo_raw()
    paths = StoragePaths(tmp_path)
    storage = JsonlStorage()

    verified_profile = FunctionalProfileResult(
        status="verified",
        package_id="pypi:demo-pkg",
        repo_id="github:repository:900000001",
        scope_quality="target_package",
        primary_function=FunctionalClaim(
            text="Fast HTTP client and connection pooling",
            evidence_span_ids=["span:1"],
        ),
        solved_problems=[
            FunctionalClaim(
                text="Slow HTTP request processing",
                evidence_span_ids=["span:2"],
            )
        ],
        use_cases=[
            FunctionalClaim(
                text="Microservice API gateway communications",
                evidence_span_ids=["span:3"],
            )
        ],
        functional_category=FunctionalClaim(
            text="HTTP client library",
            evidence_span_ids=["span:4"],
        ),
        synonyms=[
            FunctionalClaim(
                text="REST client network requester",
                evidence_span_ids=["span:5"],
            )
        ],
        alternative_candidates=[
            FunctionalClaim(
                text="httpx requests",
                evidence_span_ids=["span:6"],
            )
        ],
    )

    requests_made = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request)
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "1001",
                            "title": "Show HN: Demo package release announcement",
                            "url": "https://example.com/demo-pkg-release",
                            "created_at_i": 1707000000,
                            "points": 42,
                        }
                    ],
                    "nbPages": 1,
                    "page": 0,
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    def fixed_clock() -> datetime:
        return datetime(2024, 2, 20, 12, 0, tzinfo=UTC)

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(
            http_client=http,
            storage_paths=paths,
            storage=storage,
            clock=fixed_clock,
        )
        result = pipeline.discover(
            raw_repo,
            package_id="pypi:demo-pkg",
            functional_profile=verified_profile,
            cutoff_at="2024-03-01T00:00:00.000Z",
            sources=["hackernews"],
            max_pages=2,
            max_records=50,
        )

    # 1. Assert SearchPlan generates all 4 canonical intents and a valid 64-char query_set_hash
    plan = result.search_plan
    intents = {t.intent for t in plan.terms}
    assert "direct_package" in intents
    assert "functional_demand" in intents
    assert "alternatives" in intents
    assert "migration_adoption" in intents
    assert len(plan.query_set_hash) == 64
    assert all(c in "0123456789abcdef" for c in plan.query_set_hash)
    assert plan.profile_status == "verified"

    # 2. Assert data/raw/external/discovery.jsonl is written and valid
    discovery_file = paths.raw_path("external", "discovery")
    assert discovery_file.exists()
    records = storage.read_contract_records(discovery_file)
    assert len(records) > 0

    for rec in records:
        assert isinstance(validate_record(rec), RawRecord)
        assert rec.source == "web_article"
        assert rec.source_record_type == "article"
        assert rec.available_at is None
        assert rec.http_status == 200
        assert rec.raw_body_sha256 is not None
        payload = rec.payload
        assert "query_intent" in payload
        assert payload["query_set_hash"] == plan.query_set_hash
        assert payload["subject_match_status"] in (
            "exact_url_match",
            "high_confidence_metadata",
            "ambiguous_requires_crawl",
            "function_or_alternative_only",
        )
        assert payload["discovery_status"] == "unverified"
        assert payload["content_status"] == "not_fetched"

    # 3. Assert discovery census manifest is persisted in metadata_dir / collection_runs
    manifest_file = paths.metadata_dir / "collection_runs" / f"discovery_manifest_{result.run_id}.json"
    assert manifest_file.exists()
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_data["query_set_hash"] == plan.query_set_hash
    assert manifest_data["profile_status"] == "verified"
    assert manifest_data["completeness_status"] in ("complete", "partial")


def test_d03_unavailable_fallback_gate(tmp_path: Path):
    """Test 2 — D03 Unavailable Fallback Gate:

    When FunctionalProfileResult has status="unavailable", ONLY package_specific
    (direct_package / catalog) queries are executed and recorded.
    """
    raw_repo = _load_sample_repo_raw()
    paths = StoragePaths(tmp_path)
    storage = JsonlStorage()

    unavailable_profile = FunctionalProfileResult(
        status="unavailable",
        missing_reason="no_source",
        package_id="pypi:demo-pkg",
        repo_id="github:repository:900000001",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "2001",
                        "title": "Synthetic/Demo announcement",
                        "url": "https://example.com/demo-story",
                        "created_at_i": 1707000000,
                        "points": 10,
                    }
                ],
                "nbPages": 1,
                "page": 0,
            },
        )

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(
            http_client=http,
            storage_paths=paths,
            storage=storage,
        )
        result = pipeline.discover(
            raw_repo,
            package_id="pypi:demo-pkg",
            functional_profile=unavailable_profile,
            sources=["hackernews"],
            max_pages=1,
        )

    plan = result.search_plan
    assert plan.profile_status == "unavailable"

    # Assert that ONLY package_specific terms exist
    for term in plan.terms:
        assert term.context_level == "package_specific"
        assert term.intent in ("direct_package", "announcement", "maintenance", "security", "deprecation", "migration", "ecosystem")
        assert term.intent not in ("functional_demand", "alternatives", "migration_adoption")

    # Assert manifest records only package_specific queries
    per_query = result.discovery_manifest["per_query_stats"]
    assert len(per_query) > 0
    for pq in per_query:
        assert pq["intent"] not in ("functional_demand", "alternatives", "migration_adoption")


def test_cutoff_safety_pagination_and_checkpoint_resume(tmp_path: Path):
    """Test 3 — Cutoff-Safety, Pagination & Checkpoint Resume:

    Mock HN Algolia returning stories before and after cutoff_at across 2 pages.
    First run max_pages=1: rejects post-cutoff stories, pagination_complete=False, next_cursor="1".
    Resume / run with cursor="1" or max_pages=2: fetches page 2, pagination_complete=True.
    """
    raw_repo = _load_sample_repo_raw()
    paths = StoragePaths(tmp_path)
    storage = JsonlStorage()

    # Cutoff: 2024-02-15T00:00:00Z -> timestamp 1707955200
    cutoff_ts = 1707955200
    cutoff_str = "2024-02-15T00:00:00.000Z"

    def handler(request: httpx.Request) -> httpx.Response:
        page_param = request.url.params.get("page", "0")
        if page_param == "0":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "3001",
                            "title": "Story before cutoff",
                            "url": "https://example.com/pre-cutoff",
                            "created_at_i": cutoff_ts - 50000,
                            "points": 5,
                        },
                        {
                            "objectID": "3002",
                            "title": "Story after cutoff (should be rejected)",
                            "url": "https://example.com/post-cutoff",
                            "created_at_i": cutoff_ts + 50000,
                            "points": 15,
                        },
                    ],
                    "nbPages": 2,
                    "page": 0,
                },
            )
        elif page_param == "1":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "3003",
                            "title": "Page 2 story before cutoff",
                            "url": "https://example.com/page2-story",
                            "created_at_i": cutoff_ts - 20000,
                            "points": 8,
                        }
                    ],
                    "nbPages": 2,
                    "page": 1,
                },
            )
        return httpx.Response(200, json={"hits": [], "nbPages": 2, "page": int(page_param)})

    # Pass 1: max_pages=1
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(
            http_client=http,
            storage_paths=paths,
            storage=storage,
        )
        res1 = pipeline.discover(
            raw_repo,
            cutoff_at=cutoff_str,
            sources=["hackernews"],
            max_pages=1,
            run_id="run-t016-pagination",
        )

    # Post-cutoff story was rejected
    assert res1.collection_result.metadata.records_rejected >= 1
    # max_pages=1 with nbPages=2 means pagination is incomplete
    assert res1.collection_result.metadata.pagination_complete is False
    assert res1.collection_result.metadata.next_cursor == "1"
    assert res1.collection_result.metadata.coverage[0].status == "partial"

    # Pass 2: Resume with max_pages=2 and cursor="1"
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(
            http_client=http,
            storage_paths=paths,
            storage=storage,
        )
        res2 = pipeline.discover(
            raw_repo,
            cutoff_at=cutoff_str,
            cursor="1",
            sources=["hackernews"],
            max_pages=2,
            run_id="run-t016-pagination",
        )

    assert res2.collection_result.metadata.pages_fetched >= 1
    # Check that stories recorded include page 2
    raw_records = storage.read_contract_records(res2.output_path)
    urls = {r.source_url for r in raw_records}
    assert "https://example.com/pre-cutoff" in urls
    assert "https://example.com/page2-story" in urls
    # Ensure post-cutoff story was NEVER admitted
    assert "https://example.com/post-cutoff" not in urls


def test_strict_separation_and_downstream_t017_handoff(tmp_path: Path):
    """Test 4 — Strict Separation & Downstream T-017 Handoff:

    Verifies ExternalPipeline.discover() never performs full HTML crawling.
    Saved candidate records can be passed directly to HTMLFetcher and ArticleExtractor.
    """
    raw_repo = _load_sample_repo_raw()
    paths = StoragePaths(tmp_path)
    storage = JsonlStorage()

    http_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_requests.append(request)
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "4001",
                            "title": "Article on synthetic architecture",
                            "url": "https://techblog.example.com/posts/synthetic-arch",
                            "created_at_i": 1707000000,
                            "points": 30,
                        }
                    ],
                    "nbPages": 1,
                    "page": 0,
                },
            )
        # Downstream crawler response
        if request.url.host == "techblog.example.com":
            return httpx.Response(
                200,
                text="""
                <!DOCTYPE html>
                <html>
                <head><title>Article on synthetic architecture</title></head>
                <body>
                    <article>
                        <h1>Article on synthetic architecture</h1>
                        <p>This is the extracted body text describing synthetic architecture and tools.</p>
                    </article>
                </body>
                </html>
                """,
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(
            http_client=http,
            storage_paths=paths,
            storage=storage,
        )
        result = pipeline.discover(
            raw_repo,
            cutoff_at="2024-03-01T00:00:00.000Z",
            sources=["hackernews"],
            max_pages=1,
        )

    # 1. Verify discover() made NO requests to the candidate's article URL
    article_requests = [r for r in http_requests if r.url.host == "techblog.example.com"]
    assert len(article_requests) == 0, "discover() must NEVER perform full HTML article crawling!"
    assert all(r.url.host == "hn.algolia.com" for r in http_requests)

    # 2. Read back saved candidate records and verify seamless handoff to T-017 crawler components
    records = storage.read_contract_records(result.output_path)
    assert len(records) > 0
    candidate_record = records[0]

    # Seamless T-017 handoff
    url_to_crawl = candidate_record.source_url
    assert url_to_crawl == "https://techblog.example.com/posts/synthetic-arch"
    assert candidate_record.payload["content_status"] == "not_fetched"
    assert candidate_record.payload["discovery_status"] == "unverified"

    # Now simulate downstream T-017 crawler using HTMLFetcher and ArticleExtractor
    with HTTPClient(transport=httpx.MockTransport(handler)) as crawler_http:
        fetcher = HTMLFetcher(http_client=crawler_http)
        html_content = fetcher.fetch(url_to_crawl)
        assert html_content is not None
        assert "synthetic architecture" in html_content

        extractor = ArticleExtractor()
        doc = extractor.extract(
            html=html_content,
            url=url_to_crawl,
            source=candidate_record.payload.get("source", "web"),
        )
        assert doc is not None
        assert doc.url == url_to_crawl
        assert doc.content is not None
        assert "extracted body text" in doc.content.lower()
