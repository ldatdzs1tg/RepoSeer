"""Unit tests for ExternalDiscoveryCollector (T-016 Sub-Task 16.4)."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from reposeer.clients.external import (
    DiscoveryPageResult,
    DiscoveryProvider,
    RSSProvider,
)
from reposeer.collection import CollectionProgress, RunManager
from reposeer.collectors.external.discovery import ExternalDiscoveryCollector
from reposeer.contracts import (
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    RawRecord,
    RepoIdentity,
    TimeRange,
    validate_record,
)
from reposeer.contracts.identity import (
    canonical_github_url,
    repo_id,
)
from reposeer.contracts.validation import validate_collection_pair
from reposeer.exceptions import HTTPClientError
from reposeer.schemas.external.candidate import ExternalCandidate
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology.models import SearchPlan, SearchTerm


class MockDiscoveryProvider(DiscoveryProvider):
    """Mock discovery provider for unit tests."""

    initial_cursor = "0"

    def __init__(
        self,
        provider_name: str = "mock_hn",
        *,
        supports_historical: bool = True,
        pages: list[DiscoveryPageResult] | None = None,
        error_to_raise: Exception | None = None,
    ):
        self.provider_name = provider_name
        self.supports_historical_window = supports_historical
        self.pages = pages or []
        self.error_to_raise = error_to_raise
        self.calls: list[dict[str, Any]] = []

    def search_page(
        self,
        query: str,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> DiscoveryPageResult:
        self.calls.append({
            "query": query,
            "window_start": window_start,
            "window_end": window_end,
            "cursor": cursor,
            "page_size": page_size,
        })
        if self.error_to_raise is not None:
            raise self.error_to_raise

        if cursor is not None and cursor.isdigit():
            page_idx = int(cursor)
        else:
            page_idx = len(self.calls) - 1

        if page_idx < len(self.pages):
            return self.pages[page_idx]
        return DiscoveryPageResult(candidates=[], page_number=page_idx + 1, has_more=False)

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        return self.search_page(query, page_size=limit).candidates[:limit]


def _make_request(
    *,
    run_id: str | None = None,
    max_pages: int = 10,
    max_records: int = 100,
    cutoff_at: str | None = None,
    time_range: TimeRange | None = None,
    cursor: str | None = None,
) -> CollectionRequest:
    return CollectionRequest(
        schema_version="0.1.0",
        record_type="collection_request",
        request_id=str(uuid4()),
        run_id=run_id or str(uuid4()),
        collector_name="external_discovery",
        source="web_article",
        subjects=[
            CollectionSubject(
                repo_id=None,
                repo_full_name="pallets/flask",
                package_id="pypi:flask",
                url=None,
            )
        ],
        record_kinds=["article"],
        time_range=time_range,
        cutoff_at=cutoff_at,
        cursor=cursor,
        max_pages=max_pages,
        max_records=max_records,
    )


def _make_repo_identity() -> RepoIdentity:
    github_id = "12345"
    return RepoIdentity(
        repo_id=repo_id(github_id),
        github_repo_id=github_id,
        github_node_id=None,
        repo_full_name="pallets/flask",
        repo_url=canonical_github_url("pallets/flask"),
        known_urls=[canonical_github_url("pallets/flask")],
    )


def test_contract_compliance():
    """Verify that every emitted RawRecord and CollectionResult pass validate_record() with 0 schema errors."""
    c1 = ExternalCandidate(
        title="Flask 3.0 Released",
        url="https://palletsprojects.com/blog/flask-3",
        source="hackernews",
        source_item_id="hn:1001",
        publication_date=datetime(2023, 10, 1, 12, 0, 0, tzinfo=UTC),
        score=42.0,
        snippet="Official Flask 3 announcement",
    )
    c2 = ExternalCandidate(
        title="How to structure large Flask applications",
        url="https://blog.miguelgrinberg.com/post/flask-structure",
        source="hackernews",
        source_item_id="hn:1002",
        publication_date=datetime(2023, 11, 1, 10, 0, 0, tzinfo=UTC),
        score=15.0,
        snippet="Design patterns for Flask",
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        supports_historical=True,
        pages=[DiscoveryPageResult(candidates=[c1, c2], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 2, 1, 0, 0, 0, tzinfo=UTC),
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        repo_full_name="pallets/flask",
        terms=[
            SearchTerm(
                query="pallets/flask release",
                intent="announcement",
                platform="hackernews",
                priority=1,
                keywords=["flask", "release"],
            )
        ],
    )
    repo_id_obj = _make_repo_identity()

    result, manifest = collector.collect_discovery(
        request, plan, repo_identity=repo_id_obj, package_id="pypi:flask"
    )

    # 1. CollectionResult validation
    validated_res = validate_record(result)
    assert isinstance(validated_res, CollectionResult)
    assert result.status == "complete"
    assert result.metadata.pagination_complete is True
    assert result.metadata.pages_fetched == 1
    assert result.metadata.records_emitted == 2
    assert result.metadata.records_rejected == 0
    assert result.metadata.coverage[0].status == "complete"
    assert result.metadata.coverage[0].records_observed == 2
    assert result.metadata.coverage[0].missing_reason is None

    # 2. RawRecord validation
    assert len(result.records) == 2
    for raw in result.records:
        validated_raw = validate_record(raw)
        assert isinstance(validated_raw, RawRecord)
        assert raw.schema_version == "0.1.0"
        assert raw.record_type == "raw_record"
        assert raw.source == "web_article"
        assert raw.source_record_type == "article"
        assert raw.repo_id is raw.package_id is None
        assert raw.payload["search_target_repo_id"] == repo_id_obj.repo_id
        assert raw.payload["search_target_package_id"] == "pypi:flask"
        assert raw.source_url.startswith("https://news.ycombinator.com/item?id=")
        assert raw.payload["candidate_url"] == raw.payload["url"]
        assert raw.available_at is None
        assert raw.http_status is None
        assert raw.raw_body_sha256 is None
        assert raw.retrieved_at == "2024-02-01T00:00:00.000Z"
        assert raw.payload["record_stage"] == "discovery_candidate"
        assert raw.payload["discovery_status"] == "unverified"
        assert raw.payload["content_status"] == "not_fetched"
        assert raw.payload["query_version"] == plan.query_version
        assert raw.payload["query_set_hash"] == plan.query_set_hash
        assert raw.payload["matched_queries"] == ["pallets/flask release"]
        assert raw.payload["discovery_provider"] == "hackernews"

    # The canonical observation pair refers to provider item URLs, not unverified search subjects.
    observation_request = request.model_copy(update={"subjects": [
        CollectionSubject(repo_id=None, repo_full_name=None, package_id=None, url=raw.source_url) for raw in result.records]})
    validate_collection_pair(observation_request, result)
    ExternalDiscoveryCollector.validate_observation_pair(request, result, job_count=1)
    corrupted_raw = result.records[0].model_copy(update={"payload": dict(result.records[0].payload, search_target_package_id="pypi:unrelated")})
    corrupted = result.model_copy(update={"records": [corrupted_raw, *result.records[1:]]})
    with pytest.raises(ValueError, match="lineage"):
        ExternalDiscoveryCollector.validate_observation_pair(request, corrupted, job_count=1)

    # 3. Manifest audit
    assert manifest["query_set_hash"] == plan.query_set_hash
    assert manifest["completeness_status"] == "complete"
    assert manifest["records_observed"] == 2
    assert manifest["records_rejected"] == 0
    assert len(manifest["per_query_stats"]) == 1
    assert manifest["per_query_stats"][0]["status"] == "complete"


def test_cutoff_safety_gate():
    """Verify that candidates with publication_date > cutoff_at are excluded and increment records_rejected."""
    c_valid = ExternalCandidate(
        title="Valid Flask Article Before Cutoff",
        url="https://example.com/flask-valid",
        source="hackernews",
        source_item_id="hn:2001",
        publication_date=datetime(2024, 1, 15, 0, 0, 0, tzinfo=UTC),
    )
    c_future = ExternalCandidate(
        title="Future Flask Article After Cutoff",
        url="https://example.com/flask-future",
        source="hackernews",
        source_item_id="hn:2002",
        publication_date=datetime(2024, 7, 1, 0, 0, 0, tzinfo=UTC),  # After cutoff
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[DiscoveryPageResult(candidates=[c_valid, c_future], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 8, 1, 0, 0, 0, tzinfo=UTC),
    )

    cutoff_str = "2024-06-01T00:00:00.000Z"
    request = _make_request(cutoff_at=cutoff_str)
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask tutorial", intent="general", platform="hackernews")],
    )

    result, manifest = collector.collect_discovery(request, plan)

    # Future candidate must be excluded
    assert len(result.records) == 1
    assert result.records[0].source_record_id == "hn:2001"
    assert result.metadata.records_emitted == 1
    assert result.metadata.records_rejected == 1
    assert len(result.errors) >= 1
    assert result.errors[0].code == "validation_error"
    assert "cutoff" in result.errors[0].message

    # Must pass validate_record
    assert isinstance(validate_record(result.records[0]), RawRecord)
    assert isinstance(validate_record(result), CollectionResult)
    assert manifest["records_rejected"] == 1


def test_multi_query_upsert_and_provenance():
    """Different native snippets create immutable observations and one enriched projection."""
    # Query 1: broad alternative query
    # Candidate discovered first via generic query
    c_from_q1 = ExternalCandidate(
        title="Building Web Apps with Flask vs Django",
        url="https://example.com/flask-vs-django",
        source="hackernews",
        source_item_id="hn:3001",
        publication_date=datetime(2023, 5, 1, tzinfo=UTC),
        snippet="Django and Flask comparison",
    )
    # Query 2: exact repository query returning same item ID with exact github URL
    c_from_q2 = ExternalCandidate(
        title="Building Web Apps with Flask vs Django",
        url="https://example.com/flask-vs-django",
        source="hackernews",
        source_item_id="hn:3001",
        publication_date=datetime(2023, 5, 1, tzinfo=UTC),
        snippet="See repo at github.com/pallets/flask for details",  # Exact URL in snippet!
    )

    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[
            DiscoveryPageResult(candidates=[c_from_q1], page_number=1, has_more=False),
            DiscoveryPageResult(candidates=[c_from_q2], page_number=1, has_more=False),
        ],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    request = _make_request()
    term1 = SearchTerm(
        query="python web framework alternatives",
        intent="alternatives",
        platform="hackernews",
        context_level="alternative_level",
    )
    term2 = SearchTerm(
        query="pallets/flask",
        intent="direct_package",
        platform="hackernews",
        context_level="package_specific",
    )
    plan = SearchPlan(
        package_id="pypi:flask",
        repo_full_name="pallets/flask",
        terms=[term1, term2],
    )
    repo_id_obj = _make_repo_identity()

    result, manifest = collector.collect_discovery(request, plan, repo_identity=repo_id_obj)

    assert len(result.records) == 2  # The provider returned genuinely different snippet content.
    assert result.metadata.records_emitted == 2 and manifest["candidate_count"] == 1
    first, second = result.records
    assert first.raw_record_id != second.raw_record_id
    assert first.payload["snippet"] == c_from_q1.snippet
    assert first.payload["matched_queries"] == [term1.query]
    assert second.payload["matched_queries"] == [term2.query]
    projection, = manifest["candidate_projections"]
    assert projection["matched_queries"] == sorted([term1.query, term2.query])
    assert projection["subject_match_status"] == "exact_url_match"
    assert set(projection["raw_record_ids"]) == {first.raw_record_id, second.raw_record_id}
    assert {link["query_id"] for link in projection["query_associations"]} == {term1.query_id, term2.query_id}
    assert {link["job_id"] for link in projection["query_associations"]} == {job["job_id"] for job in manifest["per_job_stats"]}
    assert all(raw.repo_id is raw.package_id is None for raw in result.records)
    assert all(isinstance(validate_record(raw), RawRecord) for raw in result.records)
    assert isinstance(validate_record(result), CollectionResult)


def test_pagination_and_coverage_integrity():
    """Verify that when has_more=True and max_pages is reached, pagination_complete=False, next_cursor is populated, and coverage is partial."""
    c1 = ExternalCandidate(
        title="Page 1 Item",
        url="https://example.com/p1",
        source="hackernews",
        source_item_id="hn:4001",
        publication_date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[DiscoveryPageResult(candidates=[c1], page_number=1, has_more=True, next_cursor="2")],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    # Limit to 1 page even though has_more is True
    request = _make_request(max_pages=1)
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, manifest = collector.collect_discovery(request, plan)

    assert result.metadata.pagination_complete is False
    assert result.metadata.next_cursor == "2"
    assert result.metadata.coverage[0].status == "partial"
    assert result.metadata.coverage[0].missing_reason == "not_collected"
    assert result.status == "partial"
    assert manifest["pagination_complete"] is False
    assert manifest["completeness_status"] == "partial"

    assert isinstance(validate_record(result), CollectionResult)


def test_historical_source_gate():
    """Verify that when historical_mode=True on an unsupported source (e.g., RSS), coverage is unavailable with historical_unavailable."""
    rss_provider = RSSProvider()
    assert rss_provider.supports_historical_window is False

    collector = ExternalDiscoveryCollector(
        providers={"rss": rss_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask feed", intent="announcement", platform="rss")],
    )

    result, manifest = collector.collect_discovery(
        request, plan, historical_mode=True
    )

    assert len(result.records) == 0
    assert result.metadata.records_emitted == 0
    assert result.metadata.coverage[0].status == "unavailable"
    assert result.metadata.coverage[0].missing_reason == "historical_unavailable"
    assert result.metadata.coverage[0].records_observed == 0
    assert manifest["completeness_status"] == "unavailable"
    assert len(result.errors) >= 1
    assert result.errors[0].code == "insufficient_history"

    assert isinstance(validate_record(result), CollectionResult)


def test_provider_error_handling_marks_partial_and_records_progress(tmp_path):
    """Verify that an error on provider marks coverage as partial and updates progress accounting."""
    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("err-run"), manager)

    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        error_to_raise=HTTPClientError("500 Internal Server Error", status_code=500),
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        progress=progress,
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask error test", intent="general", platform="hackernews")],
    )

    result, manifest = collector.collect_discovery(request, plan)

    assert result.status == "failed"
    assert result.metadata.coverage[0].status == "unavailable"
    assert len(result.errors) == 1
    assert result.errors[0].code == "http_error"
    assert result.errors[0].retryable is True
    assert progress.context.error_count == 1

    assert isinstance(validate_record(result), CollectionResult)


def test_save_discovery_records_persists_to_jsonl(tmp_path):
    """Verify save_discovery_records appends contract-compliant RawRecords to JSONL."""
    c1 = ExternalCandidate(
        title="Flask Article",
        url="https://example.com/flask-save-test",
        source="hackernews",
        source_item_id="hn:5001",
        publication_date=datetime(2023, 8, 1, tzinfo=UTC),
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[DiscoveryPageResult(candidates=[c1], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)

    target_file = tmp_path / "raw" / "external" / "discovery.jsonl"
    storage = JsonlStorage()
    saved_path = collector.save_discovery_records(result, target_path=target_file, storage=storage)

    assert saved_path == target_file
    assert target_file.exists()

    stored_records = storage.read_contract_records(target_file)
    assert len(stored_records) == 1
    assert isinstance(stored_records[0], RawRecord)
    assert stored_records[0].source_record_id == "hn:5001"
    assert stored_records[0].source_url == "https://news.ycombinator.com/item?id=5001"
    assert stored_records[0].payload["candidate_url"] == "https://example.com/flask-save-test"


def test_invalid_url_candidate_is_rejected():
    """Verify candidate with non-canonicalizable URL is rejected and counted."""
    c_good = ExternalCandidate(
        title="Good URL",
        url="https://example.com/valid",
        source="hackernews",
        source_item_id="hn:6001",
    )
    c_bad = ExternalCandidate(
        title="Bad URL",
        url="http://invalid url with spaces.com",
        source="hackernews",
        source_item_id="hn:6002",
    )
    mock_provider = MockDiscoveryProvider(
        pages=[DiscoveryPageResult(candidates=[c_good, c_bad], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(providers={"hackernews": mock_provider})
    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)
    assert len(result.records) == 1
    assert result.metadata.records_emitted == 1
    assert result.metadata.records_rejected == 1
    assert any(e.code == "validation_error" for e in result.errors)
    assert isinstance(validate_record(result), CollectionResult)


def test_rate_limit_error_sets_partial_coverage():
    """Verify RateLimitExceededError results in partial/unavailable coverage and retryable DataError."""
    from reposeer.exceptions import RateLimitExceededError

    mock_provider = MockDiscoveryProvider(
        error_to_raise=RateLimitExceededError("Rate limited", reset_timestamp=1700000000.0),
    )
    collector = ExternalDiscoveryCollector(providers={"hackernews": mock_provider})
    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)
    assert result.status == "failed"
    assert result.metadata.coverage[0].status == "unavailable"
    assert result.errors[0].code == "rate_limited"
    assert result.errors[0].retryable is True
    assert isinstance(validate_record(result), CollectionResult)


def test_time_range_rejection_and_admission():
    """Verify half-open time range (start_at, end_at] admits inside items and rejects outside items."""
    t_range = TimeRange(
        start_at="2023-01-01T00:00:00.000Z",
        end_at="2023-12-31T23:59:59.000Z",
    )
    c_in = ExternalCandidate(
        title="Inside window",
        url="https://example.com/inside",
        source="hackernews",
        source_item_id="hn:7001",
        publication_date=datetime(2023, 6, 1, tzinfo=UTC),
    )
    c_too_old = ExternalCandidate(
        title="Too old",
        url="https://example.com/old",
        source="hackernews",
        source_item_id="hn:7002",
        publication_date=datetime(2022, 12, 31, tzinfo=UTC),
    )
    mock_provider = MockDiscoveryProvider(
        pages=[DiscoveryPageResult(candidates=[c_in, c_too_old], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )
    request = _make_request(time_range=t_range)
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)
    assert len(result.records) == 1
    assert result.records[0].source_record_id == "hn:7001"
    assert result.metadata.records_rejected == 1
    assert isinstance(validate_record(result), CollectionResult)


def test_collect_method_alias():
    """Verify collector.collect() returns CollectionResult directly."""
    c = ExternalCandidate(
        title="Flask Item",
        url="https://example.com/flask-item",
        source="hackernews",
        source_item_id="hn:8001",
    )
    mock_provider = MockDiscoveryProvider(
        pages=[DiscoveryPageResult(candidates=[c], page_number=1, has_more=False)],
    )
    collector = ExternalDiscoveryCollector(providers={"hackernews": mock_provider})
    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result = collector.collect(request, plan)
    assert isinstance(result, CollectionResult)
    assert len(result.records) == 1



def test_missing_provider_and_completed_zero_have_independent_statuses():
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult()])
    plan = SearchPlan(terms=[SearchTerm(query="flask", intent="direct_package", platform="hackernews"),
                             SearchTerm(query="flask", intent="direct_package", platform="devto")])
    result, manifest = ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(), plan)
    jobs = {job["provider"]: job for job in manifest["per_job_stats"]}
    assert jobs["hackernews"]["status"] == "complete" and jobs["hackernews"]["candidates_found"] == 0
    assert jobs["devto"]["status"] == "unavailable" and jobs["devto"]["missing_reason"] == "no_provider"
    assert result.status != "complete" and not manifest["pagination_complete"]


def test_current_rolling_window_is_not_historical_and_future_dates_are_not_clamped():
    now = datetime(2024, 2, 1, tzinfo=UTC)
    valid = ExternalCandidate(title="Current", url="https://example.org/current", source="devto",
                              source_item_id="devto:1", source_item_url="https://dev.to/author/current",
                              publication_date=datetime(2024, 1, 30, tzinfo=UTC))
    future = valid.model_copy(update={"source_item_id": "devto:2", "publication_date": datetime(2024, 3, 1, tzinfo=UTC)})
    provider = MockDiscoveryProvider(supports_historical=False, pages=[DiscoveryPageResult(candidates=[valid, future])])
    plan = SearchPlan(terms=[SearchTerm(query="flask", intent="direct_package", platform="devto")])
    request = _make_request(cutoff_at="2024-01-31T23:59:59.999Z", time_range=TimeRange(start_at="2024-01-01T00:00:00.000Z", end_at="2024-01-31T23:59:59.999Z"))
    result, manifest = ExternalDiscoveryCollector({"devto": provider}, clock=lambda: now).collect_discovery(request, plan)
    assert manifest["temporal_mode"] == "current" and len(provider.calls) == 1
    assert len(result.records) == 1 and result.metadata.records_rejected == 1
    assert result.records[0].event_at == "2024-01-30T00:00:00.000Z"
    assert future.publication_date == datetime(2024, 3, 1, tzinfo=UTC)


def test_per_job_page_budget_and_global_record_budget_are_explicit():
    def candidate(number):
        return ExternalCandidate(title="Hit", url=f"https://example.org/{number}", source="hackernews", source_item_id=f"hn:{number}")

    plan = SearchPlan(terms=[SearchTerm(query=query, intent="direct_package", platform="hackernews") for query in ("one", "two")])
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult(candidates=[candidate(1)], has_more=True, next_cursor="1"),
                                           DiscoveryPageResult(candidates=[candidate(2)], has_more=True, next_cursor="2")])
    result, manifest = ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(max_pages=1), plan)
    assert len(provider.calls) == 2 and all(job["pages_fetched"] == 1 for job in manifest["per_job_stats"])
    assert not result.metadata.pagination_complete and all(job["status"] == "partial" for job in manifest["per_job_stats"])
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult(candidates=[candidate(1), candidate(2)])])
    result, manifest = ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(max_records=1), plan)
    assert len(result.records) == 1 and len(provider.calls) == 1
    assert manifest["per_job_stats"][0]["next_cursor"] is None  # Replays initial unfinished native page.
    assert manifest["per_job_stats"][1]["status"] == "skipped_budget"
    assert not manifest["pagination_complete"]
    limited = manifest
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult(candidates=[candidate(1), candidate(2)]), DiscoveryPageResult()])
    _, full = ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(max_records=100), plan)
    assert limited["planned_query_set_hash"] == full["planned_query_set_hash"]
    assert limited["actual_query_set_hash"] != full["actual_query_set_hash"]
    assert limited["execution_policy_hash"] == full["execution_policy_hash"]


def test_ambiguous_bare_cursor_is_rejected_before_provider_calls():
    provider = MockDiscoveryProvider()
    plan = SearchPlan(terms=[SearchTerm(query=query, intent="direct_package", platform="hackernews") for query in ("one", "two")])
    with pytest.raises(ValueError, match="ambiguous"):
        ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(cursor="1"), plan)
    assert provider.calls == []


def test_historical_api_pagination_does_not_claim_verified_historical_corpus():
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult()])
    plan = SearchPlan(terms=[SearchTerm(query="flask", intent="direct_package", platform="hackernews")])
    result, manifest = ExternalDiscoveryCollector({"hackernews": provider}, clock=lambda: datetime(2024, 2, 1, tzinfo=UTC)).collect_discovery(
        _make_request(cutoff_at="2022-01-01T00:00:00.000Z"), plan)
    assert manifest["pagination_complete"] and manifest["retrieval_complete"]
    assert not manifest["historical_content_verified"] and manifest["completeness_status"] == "partial"
    assert result.metadata.coverage[0].missing_reason == "historical_unavailable"


@pytest.mark.parametrize("context,intent", [
    ("function_level", "functional_demand"), ("alternative_level", "alternatives"),
    ("technology_level", "ecosystem"),
])
def test_unrelated_function_or_alternative_hit_has_no_canonical_subject(context, intent):
    candidate = ExternalCandidate(title="Flask framework ecosystem discussion" if context == "technology_level" else "Django ORM discussion", url="https://example.org/frameworks", source="hackernews", source_item_id="hn:42")
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult(candidates=[candidate])])
    plan = SearchPlan(package_id="pypi:flask", repo_full_name="pallets/flask", terms=[
        SearchTerm(query="Python web frameworks", intent=intent, platform="hackernews", context_level=context, tech_slug="flask", keywords=["framework"])])
    result, manifest = ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(), plan, repo_identity=_make_repo_identity())
    raw = result.records[0]
    assert raw.repo_id is raw.package_id is None
    assert raw.payload["search_target_package_id"] == "pypi:flask"
    expected = "technology_context_only" if context == "technology_level" else "function_or_alternative_only"
    assert raw.payload["subject_match_status"] == expected
    if context == "technology_level":
        assert raw.payload["context_level"] == "technology_level" and raw.payload["tech_slug"] == "flask"
        projection, = manifest["candidate_projections"]
        assert projection["query_associations"][0]["tech_slug"] == "flask"
        assert projection["query_associations"][0]["context_level"] == "technology_level"


def test_explicit_single_job_cursor_does_not_claim_unobserved_prefix_complete():
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult()])
    plan = SearchPlan(terms=[SearchTerm(query="flask", intent="direct_package", platform="hackernews")])
    result, manifest = ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(cursor="1"), plan)
    assert provider.calls[0]["cursor"] == "1"
    assert not manifest["pagination_complete"] and result.status == "partial"
    assert manifest["per_job_stats"][0]["missing_reason"] == "cursor_prefix_not_collected"


def test_raw_immutability_projection_lineage_and_append_only_replay(tmp_path):
    candidate = ExternalCandidate(title="Flask library discussion", url="https://example.org/one",
                                  source="hackernews", source_item_id="hn:1")
    blocked = True
    now = datetime(2024, 2, 1, tzinfo=UTC)

    class Provider(MockDiscoveryProvider):
        def search_page(self, query, **kwargs):
            if blocked and query == "second query":
                raise HTTPClientError("injected interruption", status_code=500)
            return super().search_page(query, **kwargs)

    manager = RunManager(tmp_path)
    context = manager.start_run(str(uuid4()))
    progress = CollectionProgress(context, manager)
    provider = Provider(pages=[DiscoveryPageResult(candidates=[candidate])] * 2)
    plan = SearchPlan(terms=[SearchTerm(query=query, intent="direct_package", platform="hackernews")
                             for query in ("first query", "second query")])
    request = _make_request(run_id=context.run_id)
    collector = ExternalDiscoveryCollector({"hackernews": provider}, progress=progress, clock=lambda: now)
    first, _ = collector.collect_discovery(request, plan)
    original = first.records[0].model_dump_json()
    path = tmp_path / "discovery.jsonl"
    unrelated = first.records[0].model_copy(update={"run_id": str(uuid4()), "raw_record_id": f"raw:{uuid4()}"})
    JsonlStorage().append_contract_records_durable(path, [unrelated])
    collector.save_discovery_records(first, path)
    prefix = path.read_bytes()

    blocked = False
    now = datetime(2024, 2, 2, tzinfo=UTC)
    resumed, manifest = ExternalDiscoveryCollector({"hackernews": provider}, progress=progress, clock=lambda: now).collect_discovery(request, plan)
    assert len(provider.calls) == 2  # Completed query A was not fetched again.
    assert len(resumed.records) == 1 and resumed.records[0].model_dump_json() == original
    projection, = manifest["candidate_projections"]
    assert projection["matched_queries"] == ["first query", "second query"]
    assert len(projection["query_associations"]) == 2
    assert projection["raw_record_ids"] == [first.records[0].raw_record_id]
    collector.save_discovery_records(resumed, path)
    collector.save_discovery_records(resumed, path)
    assert path.read_bytes() == prefix

    # A separate observation can append; neither this run nor unrelated runs are rewritten.
    provider = MockDiscoveryProvider(pages=[DiscoveryPageResult(candidates=[candidate])])
    other, _ = ExternalDiscoveryCollector({"hackernews": provider}).collect_discovery(_make_request(), plan)
    collector.save_discovery_records(other, path)
    appended = path.read_bytes()
    assert appended.startswith(prefix) and len(JsonlStorage().read_contract_records(path)) == 3
    altered = resumed.records[0].model_copy(update={"payload": dict(resumed.records[0].payload, title="Changed")})
    conflicting = resumed.model_copy(update={"records": [altered]})
    with pytest.raises(ValueError, match="integrity conflict"):
        collector.save_discovery_records(conflicting, path)
    assert path.read_bytes() == appended
