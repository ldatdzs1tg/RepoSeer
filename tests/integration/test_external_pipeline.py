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
from reposeer.technology.functional_profile import FunctionalProfileResult, FunctionalSource
from reposeer.technology.functional_profiler import FunctionalProfiler


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

    Generates eligible direct/functional intents, valid query_set_hash, writes discovery.jsonl,
    validates canonical RawRecord contracts, and ensures manifest auditing.
    """
    raw_repo = _load_sample_repo_raw()
    paths = StoragePaths(tmp_path)
    storage = JsonlStorage()

    document = FunctionalSource(source_id="description", source_path="info.description", source_revision="1.0.0",
                                source_available_at=None, availability_basis="current_observation", availability_evidence_ref=None,
                                retrieved_at="2024-02-20T00:00:00.000Z", text="Fast HTTP client and connection pooling. Alternative: httpx.",
                                scope="target_package", source_url="https://pypi.org/pypi/demo-pkg/1.0.0/json")

    class ExtractiveLLM:
        model_id = "offline-test"

        def generate(self, **kwargs):
            span = kwargs["spans"][0]
            return dict(primary_function={"text": "Fast HTTP client and connection pooling.", "evidence_span_ids": [span.span_id]},
                        solved_problems=[], capabilities=[], use_cases=[], functional_category=None, synonyms=[],
                        alternative_candidates=[{"text": "httpx", "evidence_span_ids": [span.span_id]}])

    verified_profile = FunctionalProfiler().build_profile(package_id="pypi:demo-pkg", repo_id=raw_repo.repo_id,
                                                         package_scope=None, sources=[document], llm=ExtractiveLLM())

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

    # 1. Valid D03 output enables function-aware queries, but alternatives remain unverified.
    plan = result.search_plan
    intents = {t.intent for t in plan.terms}
    assert "direct_package" in intents
    assert "functional_demand" in intents
    assert not intents & {"alternatives", "migration_adoption"}
    assert "alternatives_unverified" in plan.diagnostics
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
        assert rec.http_status is None
        assert rec.raw_body_sha256 is None
        assert rec.repo_id is rec.package_id is None
        assert rec.source_url == "https://news.ycombinator.com/item?id=1001"
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
    direct_package queries are executed and recorded.
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
        assert term.intent == "direct_package"
        assert term.intent not in ("functional_demand", "alternatives", "migration_adoption")

    # Assert manifest records only package_specific queries
    per_query = result.discovery_manifest["per_query_stats"]
    assert len(per_query) > 0
    for pq in per_query:
        assert pq["intent"] not in ("functional_demand", "alternatives", "migration_adoption")


def test_cutoff_safety_pagination_and_checkpoint_resume(tmp_path: Path):
    """Test 3 — Cutoff-Safety, Pagination & Checkpoint Resume:

    Mock HN Algolia returning stories before and after cutoff_at across 2 pages.
    First run max_pages=1: rejects post-cutoff stories and retains a job-scoped cursor.
    A fresh pipeline resumes using saved run state and fetches unfinished page 2.
    """
    raw_repo = _load_sample_repo_raw()
    paths = StoragePaths(tmp_path)
    storage = JsonlStorage()

    # Cutoff: 2024-02-15T00:00:00Z -> timestamp 1707955200
    cutoff_ts = 1707955200
    cutoff_str = "2024-02-15T00:00:00.000Z"

    calls = []

    def clock():
        return datetime(2024, 2, 20, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        page_param = request.url.params.get("page", "0")
        calls.append((request.url.params["query"], page_param))
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
            clock=clock,
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
    cursor_value = res1.collection_result.metadata.next_cursor
    assert cursor_value is not None
    resume_position = json.loads(cursor_value)
    unfinished = [job for job in res1.discovery_manifest["per_job_stats"]
                  if job["provider"] == "hackernews" and job["has_more"]]
    assert unfinished and all(job["next_cursor"] == "1" for job in unfinished)
    assert resume_position["cursor"] == "1"
    assert resume_position["job_id"] == unfinished[0]["job_id"]
    assert res1.collection_result.metadata.coverage[0].status == "partial"

    persisted = storage.read_contract_records(res1.output_path)
    assert {raw.source_record_id for raw in persisted} == {"hn:3001"}
    original_ids = {raw.payload["candidate_id"]: raw.raw_record_id for raw in persisted}
    assert calls and all(page == "0" for _, page in calls)
    before_resume = len(calls)

    # Pass 2: Resume a fresh pipeline using only durable run identity, never a guessed provider cursor.
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(
            http_client=http,
            storage_paths=paths,
            storage=storage,
            clock=clock,
        )
        res2 = pipeline.discover(
            raw_repo,
            cutoff_at=cutoff_str,
            resume=True,
            sources=["hackernews"],
            max_pages=2,
            run_id=res1.run_id,
        )

    assert sorted(calls[before_resume:]) == sorted((job["query"], job["next_cursor"]) for job in unfinished)
    assert res2.run_id == res1.run_id
    assert res2.collection_result.metadata.pages_fetched == len(unfinished)
    resumed_jobs = {job["job_id"]: job for job in res2.discovery_manifest["per_job_stats"]}
    for job in unfinished:
        resumed_job = resumed_jobs[job["job_id"]]
        assert resumed_job["last_completed_cursor"] == "1"
        assert resumed_job["next_cursor"] is None and resumed_job["has_more"] is False
        # Native pagination ended; rejected items still require partial data coverage.
        assert resumed_job["status"] == "partial" and resumed_job["missing_reason"] == "invalid_items"
    # Check that stories recorded include page 2
    raw_records = storage.read_contract_records(res2.output_path)
    assert len(raw_records) == len({raw.payload["candidate_id"] for raw in raw_records}) == 2
    assert len({raw.raw_record_id for raw in raw_records}) == 2
    for raw in raw_records:
        if raw.payload["candidate_id"] in original_ids:
            assert raw.raw_record_id == original_ids[raw.payload["candidate_id"]]
    urls = {r.payload["candidate_url"] for r in raw_records}
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
    url_to_crawl = candidate_record.payload["candidate_url"]
    assert url_to_crawl == "https://techblog.example.com/posts/synthetic-arch"
    assert candidate_record.source_url == "https://news.ycombinator.com/item?id=4001"
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


@pytest.mark.parametrize("failure", ["429", "timeout"])
def test_two_provider_two_query_resume_replays_only_unfinished_job(tmp_path, monkeypatch, failure):
    from reposeer.technology.models import SearchPlan, SearchTerm

    raw_repo = _load_sample_repo_raw()
    plan = SearchPlan(repo_id=raw_repo.repo_id, repo_full_name="Synthetic/Demo", terms=[
        SearchTerm(query=query, intent="direct_package", platform="web") for query in ("first query", "second query")])
    monkeypatch.setattr("reposeer.pipelines.external.SearchTermGenerator.build_search_plan", lambda self, **kwargs: plan)
    first_calls, resumed_calls = [], []
    broken = True

    def handler(request):
        provider = "hn" if request.url.host == "hn.algolia.com" else "se"
        query = request.url.params.get("query") or request.url.params["q"]
        page = request.url.params["page"]
        first_calls.append((provider, query, page))
        if provider == "hn" and query == "second query" and page == "1" and broken:
            if failure == "429":
                return httpx.Response(429)
            raise httpx.ReadTimeout("injected timeout", request=request)
        if provider == "hn":
            number = "1001" if query == "first query" else "2001" if page == "0" else "2002"
            hits = [{"objectID": number, "title": "Discovery hit", "url": f"https://example.org/{number}"}]
            if page == "1":
                hits.append({"objectID": "1001", "title": "Rediscovered", "url": "https://example.org/1001"})
            return httpx.Response(200, json={"hits": hits, "nbPages": 1 if query == "first query" else 2})
        number = 11 if query == "first query" else 22
        return httpx.Response(200, json={"items": [{"question_id": number, "title": "Question",
                                                   "link": f"https://stackoverflow.com/questions/{number}"}], "has_more": False})

    def clock():
        return datetime(2024, 2, 20, tzinfo=UTC)

    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        first = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path), clock=clock).discover(
            raw_repo, sources=["hackernews", "stackexchange"], max_pages=2)
    jobs = first.discovery_manifest["per_job_stats"]
    assert [job["status"] for job in jobs] == ["complete", "complete", "partial", "complete"]
    unfinished = jobs[2]
    assert unfinished["last_completed_cursor"] == "0" and unfinished["next_cursor"] == "1"
    assert first.collection_result.status == "partial" and not first.discovery_manifest["pagination_complete"]
    first_prefix = first.output_path.read_bytes()
    first_records = {raw.payload["candidate_id"]: raw.raw_record_id for raw in JsonlStorage().read_contract_records(first.output_path)}
    assert len(first_records) == 4
    broken = False

    def resume_handler(request):
        resumed_calls.append(request)
        return handler(request)

    with HTTPClient(transport=httpx.MockTransport(resume_handler), max_retries=1) as http:
        resumed = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path), clock=clock).discover(
            raw_repo, sources=["hackernews", "stackexchange"], run_id=first.run_id, resume=True, max_pages=2)
    assert len(resumed_calls) == 1
    assert resumed_calls[0].url.host == "hn.algolia.com"
    assert resumed_calls[0].url.params["query"] == "second query" and resumed_calls[0].url.params["page"] == "1"
    assert resumed.collection_result.status == "complete" and resumed.discovery_manifest["pagination_complete"]
    records = JsonlStorage().read_contract_records(resumed.output_path)
    assert len(records) == 6 and len({raw.payload["candidate_id"] for raw in records}) == 5
    assert set(first_records.values()) <= {raw.raw_record_id for raw in records}
    assert resumed.output_path.read_bytes().startswith(first_prefix)
    duplicate = next(candidate for candidate in resumed.discovery_manifest["candidate_projections"]
                     if candidate["source_item_id"] == "hn:1001")
    assert set(duplicate["matched_queries"]) == {"first query", "second query"}
    assert len(duplicate["query_associations"]) == len(duplicate["raw_record_ids"]) == 2
    assert resumed.discovery_manifest["per_job_stats"][2]["error_history"]
    assert first.discovery_manifest["actual_query_set_hash"] == resumed.discovery_manifest["actual_query_set_hash"]


@pytest.mark.parametrize("interruption", ["before_state", "after_state", "before_output", "output"])
def test_durable_page_or_output_interruption_recovers_without_duplicate_candidates(tmp_path, monkeypatch, interruption):
    import reposeer.collectors.external.discovery.base as discovery_module
    from reposeer.technology.models import SearchPlan, SearchTerm

    raw_repo = _load_sample_repo_raw()
    plan = SearchPlan(terms=[SearchTerm(query="one query", intent="direct_package", platform="hackernews")])
    monkeypatch.setattr("reposeer.pipelines.external.SearchTermGenerator.build_search_plan", lambda self, **kwargs: plan)
    calls = []

    def handler(request):
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(200, json={"hits": [{"objectID": str(page + 1), "title": "Hit",
                                                  "url": f"https://example.org/{page}"}], "nbPages": 2})

    original = discovery_module.write_json_atomic

    def interrupted_write(path, data):
        if data["records"] and interruption == "before_state":
            raise OSError("interrupted before durable candidate page")
        original(path, data)
        if data["records"] and interruption == "after_state":
            raise OSError("interrupted after durable candidate page, before checkpoint pointer")

    class InterruptedStorage(JsonlStorage):
        def append_contract_records_durable(self, path, records):
            if interruption == "before_output":
                raise OSError("interrupted before output publication")
            super().append_contract_records_durable(path, records)
            raise OSError("interrupted output publication")

    def clock():
        return datetime(2024, 2, 20, tzinfo=UTC)

    if interruption in {"before_state", "after_state"}:
        monkeypatch.setattr(discovery_module, "write_json_atomic", interrupted_write)
    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        pipeline = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path), clock=clock,
                                    storage=InterruptedStorage() if interruption in {"before_output", "output"} else JsonlStorage())
        with pytest.raises(OSError, match="interrupted"):
            pipeline.discover(raw_repo, run_id="durable-interruption", sources=["hackernews"])
    state_file = next((tmp_path / "metadata/collection_runs").glob("discovery_state_*.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    saved_raw_ids = {raw["raw_record_id"] for raw in state["records"]}
    assert len(saved_raw_ids) == {"before_state": 0, "after_state": 1, "before_output": 2, "output": 2}[interruption]
    output_path = StoragePaths(tmp_path).raw_path("external", "discovery")
    prefix = output_path.read_bytes() if output_path.exists() else b""
    monkeypatch.setattr(discovery_module, "write_json_atomic", original)
    before_resume = len(calls)
    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        result = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path), clock=clock).discover(
            raw_repo, run_id="durable-interruption", resume=True, sources=["hackernews"])
    assert calls[before_resume:] == {"before_state": [0, 1], "after_state": [1], "before_output": [], "output": []}[interruption]
    records = JsonlStorage().read_contract_records(result.output_path)
    assert len(records) == len({raw.payload["candidate_id"] for raw in records}) == 2
    assert saved_raw_ids <= {raw.raw_record_id for raw in records}
    restored = {raw.raw_record_id: raw.model_dump(mode="json") for raw in records}
    assert all(restored[raw["raw_record_id"]] == raw for raw in state["records"])
    assert result.output_path.read_bytes().startswith(prefix)
    assert result.collection_result.status == "complete"


@pytest.mark.parametrize("changed", ["query", "window", "providers", "policy", "cursor", "technology_mode", "input_id", "input_contents", "legacy_state"])
def test_resume_rejects_changed_scope_and_corrupt_cursor_before_http(tmp_path, monkeypatch, changed):
    import reposeer.collectors.external.discovery.base as discovery_module
    from reposeer.technology.models import SearchPlan, SearchTerm

    plan = SearchPlan(terms=[SearchTerm(query="original", intent="direct_package", platform="hackernews")])
    monkeypatch.setattr("reposeer.pipelines.external.SearchTermGenerator.build_search_plan", lambda self, **kwargs: plan)
    raw_repo = _load_sample_repo_raw()

    def clock():
        return datetime(2024, 2, 20, tzinfo=UTC)

    with HTTPClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"hits": [], "nbPages": 2}))) as http:
        first = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path), clock=clock).discover(
            raw_repo, sources=["hackernews"], max_pages=1)
    kwargs = dict(run_id=first.run_id, resume=True, sources=["hackernews"])
    if changed == "query":
        plan = SearchPlan(terms=[SearchTerm(query="changed", intent="direct_package", platform="hackernews")])
    elif changed == "window":
        kwargs["window_days"] = 30
    elif changed == "providers":
        kwargs["sources"] = ["hackernews", "devto"]
    elif changed == "policy":
        monkeypatch.setattr(discovery_module, "SOURCE_POLICY_VERSION", "different-policy")
    elif changed == "technology_mode":
        kwargs["include_technology_context"] = True
    elif changed == "input_id":
        raw_repo = raw_repo.model_copy(update={"raw_record_id": "raw:11111111-1111-4111-8111-111111111111"})
    elif changed == "input_contents":
        raw_repo = raw_repo.model_copy(update={"payload": dict(raw_repo.payload, description="Changed observation")})
    else:
        state_file = tmp_path / "metadata/collection_runs" / f"discovery_state_{first.run_id}.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if changed == "legacy_state":
            state.pop("state_format_version")
        else:
            state["jobs"][0]["next_cursor"] = "invalid"
        state_file.write_text(json.dumps(state), encoding="utf-8")

    def unexpected_request(request):
        raise AssertionError("Unsafe resume must be rejected before HTTP")

    with HTTPClient(transport=httpx.MockTransport(unexpected_request)) as http:
        with pytest.raises(ValueError):
            ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path), clock=clock).discover(raw_repo, **kwargs)


def test_invalid_repository_observations_and_unknown_providers_are_rejected(tmp_path):
    raw = _load_sample_repo_raw()

    def unexpected_request(request):
        raise AssertionError("Invalid inputs must not trigger HTTP")

    with HTTPClient(transport=httpx.MockTransport(unexpected_request)) as http:
        pipeline = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path))
        for altered in [raw.model_copy(update={"repo_id": "github:repository:123"}),
                        raw.model_copy(update={"source": "web_article"}),
                        raw.model_copy(update={"source_record_type": "article"}),
                        raw.model_copy(update={"payload": dict(raw.payload, id="900000001")})]:
            with pytest.raises(ValueError):
                pipeline.discover(altered)
        assert set(pipeline._resolve_providers(["stackoverflow"], None)) == {"stackexchange"}
        with pytest.raises(ValueError, match="Unsupported"):
            pipeline.discover("pypi:flask", sources=["typo"])


@pytest.mark.parametrize("enabled", [False, True])
def test_pipeline_technology_context_reuses_supplied_t015_detection(tmp_path, monkeypatch, enabled):
    from reposeer.technology.models import DetectionResult
    from reposeer.technology.registry import TechnologyRegistry

    spec = TechnologyRegistry().get_by_slug("flask")
    detection = DetectionResult(resolved_specs=[spec], primary_spec=spec)

    def unexpected_detection(*args, **kwargs):
        raise AssertionError("The caller already supplied T-015 detection")

    monkeypatch.setattr("reposeer.pipelines.external.TechnologyDetector.detect", unexpected_detection)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"hits": [{"objectID": "123", "title": "Flask ecosystem",
                                                  "url": "https://example.org/flask"}], "nbPages": 1})

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path))
        result = pipeline.discover(_load_sample_repo_raw(), detection_result=detection,
                                   include_technology_context=enabled)
        before_resume = len(calls)
        original_bytes = result.output_path.read_bytes()
        replayed = pipeline.discover(_load_sample_repo_raw(), detection_result=detection,
                                     include_technology_context=enabled, run_id=result.run_id, resume=True)
    assert len(calls) == before_resume
    assert original_bytes == replayed.output_path.read_bytes()
    assert result.discovery_manifest["include_technology_context"] is enabled
    associations = [entry for candidate in result.discovery_manifest["candidate_projections"]
                    for entry in candidate["query_associations"]]
    technology = [entry for entry in associations if entry["context_level"] == "technology_level"]
    assert bool(technology) is enabled
    assert all(entry["tech_slug"] == "flask" for entry in technology)
    assert all(raw.repo_id is raw.package_id is raw.available_at is None for raw in result.collection_result.records)


@pytest.mark.parametrize("provider_name,setting_name", [
    ("devto", "devto_api_key"), ("stackexchange", "stackexchange_api_key"),
])
@pytest.mark.parametrize("api_key", [None, "", "synthetic_discovery_key"])
def test_provider_api_keys_from_settings(tmp_path, monkeypatch, provider_name, setting_name, api_key):
    from reposeer.config import settings

    monkeypatch.setattr(settings, setting_name, api_key)

    def handler(request):
        transmitted = request.headers.get("api-key") if provider_name == "devto" else request.url.params.get("key")
        assert transmitted == (api_key or None)
        payload = [] if provider_name == "devto" else {"items": [], "has_more": False}
        return httpx.Response(200, json=payload)

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        pipeline = ExternalPipeline(http_client=http, storage_paths=StoragePaths(tmp_path))
        provider = pipeline._resolve_providers([provider_name], None)[provider_name]
        assert provider.http is http and provider.clock is pipeline.clock
        assert provider.search_page("Python packages").candidates == []
        if api_key:
            assert api_key not in json.dumps(provider.execution_policy())
